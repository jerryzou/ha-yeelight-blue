"""Light entity for a Yeelight Blue II bulb.

HA is the source of truth: the state last set from HA (kept across restarts)
is re-applied whenever a poll finds the bulb elsewhere. The bulb has no
memory and powers up at CLTMP 4000,100 after a power cut, so this restores
it like a smart plug's "restore last state" power-on behaviour.
"""
from __future__ import annotations

import asyncio
from dataclasses import asdict
from datetime import timedelta
import logging
import time
from typing import Any

from bleak.exc import BleakError

from homeassistant.components import bluetooth
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_RGB_COLOR,
    ColorMode,
    LightEntity,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers.device_registry import CONNECTION_BLUETOOTH, DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.event import async_call_later, async_track_time_interval
from homeassistant.helpers.restore_state import ExtraStoredData, RestoreEntity

from . import YeelightBlueConfigEntry
from .const import (
    ADVERT_SILENCE_S,
    COMMAND_TIMEOUT_S,
    DOMAIN,
    MAX_KELVIN,
    MIN_KELVIN,
    POLL_INTERVAL_S,
    RETRY_AFTER_FAIL_S,
    VERIFY_AFTER_S,
    WATCHDOG_S,
)
from .device import BulbState, YeelightBlue

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: YeelightBlueConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    async_add_entities([YeelightBlueLight(entry.runtime_data, entry.title)])


def _to_pct(brightness: int) -> int:
    return max(1, round(brightness * 100 / 255))


def _matches(actual: BulbState, want: BulbState) -> bool:
    if actual.is_on != want.is_on:
        return False
    if not want.is_on:
        return True
    if abs(actual.brightness - want.brightness) > 1:
        return False
    if want.kelvin is not None:
        return actual.kelvin is not None and abs(actual.kelvin - want.kelvin) <= 50
    if want.rgb is not None:
        return actual.rgb is not None and all(
            abs(a - w) <= 2 for a, w in zip(actual.rgb, want.rgb)
        )
    return True


class _DesiredData(ExtraStoredData):
    def __init__(self, state: BulbState, last_pct: int) -> None:
        self.state = state
        self.last_pct = last_pct

    def as_dict(self) -> dict[str, Any]:
        return {"desired": asdict(self.state), "last_pct": self.last_pct}

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> _DesiredData | None:
        try:
            d = data["desired"]
            rgb = tuple(d["rgb"]) if d.get("rgb") else None
            return cls(BulbState(d["brightness"], rgb, d.get("kelvin")), data["last_pct"])
        except (KeyError, TypeError):
            return None


class YeelightBlueLight(LightEntity, RestoreEntity):
    _attr_has_entity_name = True
    _attr_name = None
    _attr_should_poll = False
    _attr_supported_color_modes = {ColorMode.RGB, ColorMode.COLOR_TEMP}
    _attr_min_color_temp_kelvin = MIN_KELVIN
    _attr_max_color_temp_kelvin = MAX_KELVIN

    def __init__(self, bulb: YeelightBlue, title: str) -> None:
        self._bulb = bulb
        self._attr_unique_id = bulb.address
        self._attr_device_info = DeviceInfo(
            identifiers={(DOMAIN, bulb.address)},
            connections={(CONNECTION_BLUETOOTH, bulb.address)},
            name=title,
            manufacturer="Yeelight",
            model="Yeelight Blue II",
        )
        self._attr_is_on = None
        self._attr_color_mode = ColorMode.RGB
        self._desired: BulbState | None = None
        self._last_pct = 100
        self._retry_unsub = None

    @property
    def extra_restore_state_data(self) -> ExtraStoredData | None:
        if self._desired is None:
            return None
        return _DesiredData(self._desired, self._last_pct)

    async def async_added_to_hass(self) -> None:
        if (extra := await self.async_get_last_extra_data()) and (
            restored := _DesiredData.from_dict(extra.as_dict())
        ):
            self._desired, self._last_pct = restored.state, restored.last_pct
            self._apply(self._desired)
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._async_poll, timedelta(seconds=POLL_INTERVAL_S)
            )
        )
        self.async_on_remove(
            async_track_time_interval(
                self.hass, self._check_presence, timedelta(seconds=WATCHDOG_S)
            )
        )
        self.async_on_remove(self._cancel_retry)
        self.hass.async_create_task(self._async_poll())

    def _set_available(self, available: bool) -> None:
        if available != self._attr_available:
            _LOGGER.info(
                "%s: %s", self._bulb.address, "back" if available else "unavailable"
            )
            self._attr_available = available
            self.async_write_ha_state()

    @callback
    def _check_presence(self, _now=None) -> None:
        # HA's own tracker takes 5-15 min. Bulbs near an adapter advertise
        # about once a second, but on a weak proxy link adverts can go missing
        # for 2+ min while the bulb is fine, so silence only triggers a probe
        # (a status poll); a failed probe marks it unavailable. Bluetooth
        # callbacks only fire on changed adverts, so read the last-seen time.
        if self._bulb.busy:
            return
        info = bluetooth.async_last_service_info(
            self.hass, self._bulb.address, connectable=True
        )
        seen = max(info.time if info else 0.0, self._bulb.link_activity)
        silent = time.monotonic() - seen > ADVERT_SILENCE_S
        if self._attr_available and silent:
            self.hass.async_create_task(self._async_poll(probe=True))
        elif not self._attr_available and not silent:
            self._set_available(True)
            self.hass.async_create_task(self._async_poll())

    async def _async_verify(self, _now=None) -> None:
        await self._async_poll(verify=True)

    def _schedule_retry(self) -> None:
        if self._retry_unsub:
            self._retry_unsub()
        self._retry_unsub = async_call_later(self.hass, RETRY_AFTER_FAIL_S, self._async_retry)

    @callback
    def _cancel_retry(self) -> None:
        if self._retry_unsub:
            self._retry_unsub()
            self._retry_unsub = None

    async def _async_retry(self, _now=None) -> None:
        self._retry_unsub = None
        await self._async_poll()

    async def _async_poll(
        self, _now=None, verify: bool = False, probe: bool = False
    ) -> None:
        if not self._attr_available:
            return  # _check_presence polls as soon as adverts return
        try:
            actual = await self._bulb.async_query_status()
        except (BleakError, TimeoutError) as err:
            _LOGGER.debug("%s: status poll failed: %s", self._bulb.address, err)
            if probe:
                # Silent and unreachable: powered off.
                self._set_available(False)
            else:
                # Not proof it's gone: fresh links often drop ~10 s in.
                self._schedule_retry()
            return
        self._bulb.link_activity = time.monotonic()
        self._set_available(True)
        if actual is None:
            return
        want = self._desired
        if want is None:
            # Off reports a converted colour (e.g. "71,61,11,0" in CT mode): don't keep it.
            self._desired = actual if actual.is_on else BulbState(0)
        elif not _matches(actual, want):
            if verify and not self._bulb.acked_writes:
                _LOGGER.warning(
                    "%s: bulb ignored a write-without-response, using acknowledged writes",
                    self._bulb.address,
                )
                self._bulb.acked_writes = True
            _LOGGER.info(
                "%s: bulb reports %s, HA set %s (power cut?) - restoring",
                self._bulb.address, actual, want,
            )
            try:
                await self._async_send(want)
                actual = want
            except (BleakError, TimeoutError) as err:
                _LOGGER.warning("%s: restore failed: %s", self._bulb.address, err)
        self._apply(actual)
        self.async_write_ha_state()

    def _apply(self, state: BulbState) -> None:
        self._attr_is_on = state.is_on
        if not state.is_on:
            return
        self._attr_brightness = round(state.brightness * 255 / 100)
        if state.kelvin is not None:
            self._attr_color_mode = ColorMode.COLOR_TEMP
            self._attr_color_temp_kelvin = state.kelvin
        elif state.rgb is not None and state.rgb != (0, 0, 0):
            self._attr_color_mode = ColorMode.RGB
            self._attr_rgb_color = state.rgb

    async def _async_send(self, want: BulbState) -> None:
        if not want.is_on:
            await self._bulb.async_set_brightness(0)
        elif want.kelvin is not None:
            await self._bulb.async_set_kelvin(want.kelvin, want.brightness)
        elif want.rgb is not None:
            await self._bulb.async_set_rgb(*want.rgb, want.brightness)
        else:
            await self._bulb.async_set_brightness(want.brightness)

    async def _async_set(self, want: BulbState) -> None:
        # Record the intent first: if this send fails, the next poll finds
        # the bulb elsewhere and re-sends it (self-heals weak proxy links).
        self._desired = want
        if want.is_on:
            self._last_pct = want.brightness
        try:
            async with asyncio.timeout(COMMAND_TIMEOUT_S):
                await self._async_send(want)
        except (BleakError, TimeoutError) as err:
            # Don't raise: an error would abort the calling automation (and a
            # light group waits for its slowest member). The intent is stored,
            # so the retry poll re-sends it.
            _LOGGER.warning(
                "%s: command not delivered (%r), retrying in %d s",
                self._bulb.address, err, RETRY_AFTER_FAIL_S,
            )
            self._schedule_retry()
            return
        if self._bulb.last_write_unacked:
            async_call_later(self.hass, VERIFY_AFTER_S, self._async_verify)
        self._attr_available = True
        self._apply(want)
        self.async_write_ha_state()

    async def async_turn_on(self, **kwargs: Any) -> None:
        pct = _to_pct(kwargs[ATTR_BRIGHTNESS]) if ATTR_BRIGHTNESS in kwargs else self._last_pct
        if ATTR_RGB_COLOR in kwargs:
            want = BulbState(pct, rgb=tuple(kwargs[ATTR_RGB_COLOR]))
        elif ATTR_COLOR_TEMP_KELVIN in kwargs:
            want = BulbState(pct, kelvin=kwargs[ATTR_COLOR_TEMP_KELVIN])
        else:
            # Resend the colour too: after a power cut the bulb's own colour
            # is its 4000 K default, not what HA last set.
            base = self._desired or BulbState(pct)
            want = BulbState(pct, rgb=base.rgb, kelvin=base.kelvin)
        await self._async_set(want)

    async def async_turn_off(self, **kwargs: Any) -> None:
        base = self._desired or BulbState(0)
        await self._async_set(BulbState(0, rgb=base.rgb, kelvin=base.kelvin))
