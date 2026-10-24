"""BLE client for one Yeelight Blue II bulb."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import re
import time

from bleak import BleakClient
from bleak.exc import BleakError
from bleak.backends.device import BLEDevice
from bleak_retry_connector import BleakClientWithServiceCache, establish_connection

from homeassistant.components import bluetooth
from homeassistant.core import HomeAssistant

from .const import (
    CHAR_CONTROL,
    CHAR_STATUS_NOTIFY,
    CHAR_STATUS_QUERY,
    IDLE_DISCONNECT_S,
    PAYLOAD_LEN,
    STATUS_TIMEOUT_S,
    WRITE_TIMEOUT_S,
)

_LOGGER = logging.getLogger(__name__)

_CT_RE = re.compile(r"CLTMP\s+(\d+)\s*,\s*(\d+)", re.I)


@dataclass
class BulbState:
    brightness: int  # 0-100, 0 = off
    rgb: tuple[int, int, int] | None = None
    kelvin: int | None = None

    @property
    def is_on(self) -> bool:
        return self.brightness > 0


def parse_status(raw: str) -> BulbState | None:
    text = raw.replace("\0", "").strip()
    if m := _CT_RE.match(text):
        return BulbState(brightness=int(m[2]), kelvin=int(m[1]))
    parts = [p.strip() for p in text.split(",")]
    try:
        r, g, b, bri = (int(p) for p in parts[:4])
    except ValueError:
        return None
    return BulbState(brightness=bri, rgb=(r, g, b))


def _refused(err: Exception) -> bool:
    """The adapter rejected write-without-response, not a link problem."""
    msg = str(err).lower()
    return any(k in msg for k in ("not supported", "notsupported", "not permitted", "notpermitted"))


def _pad(cmd: str) -> bytes:
    if len(cmd) > PAYLOAD_LEN:
        raise ValueError(f"command too long: {cmd!r}")
    return (cmd + "," * (PAYLOAD_LEN - len(cmd))).encode()


class YeelightBlue:
    """Connect on demand, drop the link after IDLE_DISCONNECT_S."""

    def __init__(self, hass: HomeAssistant, ble_device: BLEDevice) -> None:
        self.hass = hass
        self.ble_device = ble_device
        self._client: BleakClient | None = None
        self._lock = asyncio.Lock()
        self._idle_handle: asyncio.TimerHandle | None = None
        self._status_future: asyncio.Future[str] | None = None
        # The bulb stops advertising while connected; advert-silence checks
        # count from whichever is later, this or its last advert.
        self.link_activity = time.monotonic()
        # Acknowledged writes take ~2.7 s (the bulb seems to answer after its
        # fade), so start with write-without-response and let the entity
        # verify by status poll; flips to True for good if the adapter refuses
        # or the bulb ignores them.
        self.acked_writes = False
        self.last_write_unacked = False

    @property
    def address(self) -> str:
        return self.ble_device.address

    @property
    def busy(self) -> bool:
        """Connected, or a connect/command/poll is under way."""
        return self._lock.locked() or bool(self._client and self._client.is_connected)

    async def _ensure_connected(self) -> BleakClient:
        if self._client and self._client.is_connected:
            return self._client
        # Use whichever adapter/proxy hears the bulb best right now.
        if fresh := bluetooth.async_ble_device_from_address(
            self.hass, self.address, connectable=True
        ):
            self.ble_device = fresh
        _LOGGER.debug("%s: connecting", self.address)
        client = await establish_connection(
            BleakClientWithServiceCache,
            self.ble_device,
            self.address,
            disconnected_callback=self._on_disconnect,
            max_attempts=3,
        )
        await client.start_notify(CHAR_STATUS_NOTIFY, self._on_notify)
        self._client = client
        self.link_activity = time.monotonic()
        return client

    def _on_disconnect(self, client: BleakClient) -> None:
        _LOGGER.debug("%s: disconnected", self.address)
        # A late callback from an earlier link (or a failed attempt) must not
        # drop the current one or count as activity.
        if self._client is client:
            self._client = None
            self.link_activity = time.monotonic()

    def _on_notify(self, _char, data: bytearray) -> None:
        text = bytes(data).decode(errors="replace")
        _LOGGER.debug("%s: status %r", self.address, text)
        if self._status_future and not self._status_future.done():
            self._status_future.set_result(text)

    def _schedule_idle_disconnect(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
        self._idle_handle = self.hass.loop.call_later(
            IDLE_DISCONNECT_S,
            lambda: self.hass.async_create_task(self.async_disconnect()),
        )

    async def async_disconnect(self) -> None:
        if self._idle_handle:
            self._idle_handle.cancel()
            self._idle_handle = None
        async with self._lock:
            if self._client and self._client.is_connected:
                await self._client.disconnect()
            self._client = None

    async def _write(self, cmd: str) -> None:
        # The bulb sometimes drops a fresh link ~10 s in, leaving the write
        # hanging until the disconnect; reconnect once and resend.
        async with self._lock:
            reconnects = 0
            while True:
                client = await self._ensure_connected()
                acked = self.acked_writes
                start = time.monotonic()
                try:
                    async with asyncio.timeout(WRITE_TIMEOUT_S):
                        await client.write_gatt_char(CHAR_CONTROL, _pad(cmd), response=acked)
                except (BleakError, TimeoutError) as err:
                    _LOGGER.debug("%s: write failed: %r", self.address, err)
                    if not acked and client.is_connected and _refused(err):
                        # Refused with the link up: BlueZ checks the
                        # characteristic's properties (FFF1 only lists "write").
                        _LOGGER.info(
                            "%s: write-without-response refused (%s), using acknowledged writes",
                            self.address, err,
                        )
                        self.acked_writes = True
                        continue
                    await self._drop_client()
                    reconnects += 1
                    if reconnects == 2:
                        raise
                    continue
                _LOGGER.debug(
                    "%s: write %r %s in %.1f s",
                    self.address, cmd, "acked" if acked else "sent", time.monotonic() - start,
                )
                self.last_write_unacked = not acked
                break
            self._schedule_idle_disconnect()

    async def _drop_client(self) -> None:
        client, self._client = self._client, None
        if client and client.is_connected:
            try:
                await client.disconnect()
            except BleakError:
                pass

    async def async_set_rgb(self, r: int, g: int, b: int, brightness: int) -> None:
        await self._write(f"{r},{g},{b},{brightness}")

    async def async_set_kelvin(self, kelvin: int, brightness: int) -> None:
        await self._write(f"CLTMP {kelvin},{brightness},")

    async def async_set_brightness(self, brightness: int) -> None:
        """Brightness only (0 = off); the bulb keeps its colour."""
        await self._write(f",,,{brightness}")

    async def async_query_status(self) -> BulbState | None:
        async with self._lock:
            client = await self._ensure_connected()
            self._status_future = self.hass.loop.create_future()
            await client.write_gatt_char(CHAR_STATUS_QUERY, b"S", response=True)
            try:
                raw = await asyncio.wait_for(self._status_future, STATUS_TIMEOUT_S)
            except TimeoutError:
                _LOGGER.debug("%s: no status reply", self.address)
                return None
            finally:
                self._status_future = None
                self._schedule_idle_disconnect()
        state = parse_status(raw)
        if state is None:
            _LOGGER.warning("%s: unparsed status %r", self.address, raw)
        return state
