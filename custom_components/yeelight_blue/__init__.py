"""Yeelight Blue II (BLE-only RGB bulb) integration."""
from __future__ import annotations

from homeassistant.components import bluetooth
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant, callback
from homeassistant.exceptions import ConfigEntryNotReady

from .device import YeelightBlue

PLATFORMS = [Platform.LIGHT]

type YeelightBlueConfigEntry = ConfigEntry[YeelightBlue]


async def async_setup_entry(hass: HomeAssistant, entry: YeelightBlueConfigEntry) -> bool:
    address = entry.unique_id
    ble_device = bluetooth.async_ble_device_from_address(hass, address, connectable=True)
    if ble_device is None:
        raise ConfigEntryNotReady(f"{address} not seen by any connectable Bluetooth adapter")

    bulb = YeelightBlue(hass, ble_device)

    @callback
    def _on_advert(info: bluetooth.BluetoothServiceInfoBleak, _change) -> None:
        # Follow the bulb to whichever proxy currently hears it best.
        bulb.ble_device = info.device

    entry.async_on_unload(
        bluetooth.async_register_callback(
            hass,
            _on_advert,
            bluetooth.BluetoothCallbackMatcher(address=address, connectable=True),
            bluetooth.BluetoothScanningMode.PASSIVE,
        )
    )
    entry.runtime_data = bulb
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: YeelightBlueConfigEntry) -> bool:
    if unloaded := await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        await entry.runtime_data.async_disconnect()
    return unloaded
