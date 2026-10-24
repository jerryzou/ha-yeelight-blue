"""Config flow: Bluetooth discovery, or pick from bulbs currently in range."""
from __future__ import annotations

from typing import Any

import voluptuous as vol

from homeassistant.components.bluetooth import (
    BluetoothServiceInfoBleak,
    async_discovered_service_info,
)
from homeassistant.config_entries import ConfigFlow, ConfigFlowResult
from homeassistant.const import CONF_ADDRESS

from .const import DOMAIN


def _title(info: BluetoothServiceInfoBleak) -> str:
    return f"Yeelight Blue II {info.address[-5:].replace(':', '')}"


class YeelightBlueConfigFlow(ConfigFlow, domain=DOMAIN):
    VERSION = 1

    def __init__(self) -> None:
        self._discovery: BluetoothServiceInfoBleak | None = None
        self._candidates: dict[str, BluetoothServiceInfoBleak] = {}

    async def async_step_bluetooth(
        self, discovery_info: BluetoothServiceInfoBleak
    ) -> ConfigFlowResult:
        await self.async_set_unique_id(discovery_info.address)
        self._abort_if_unique_id_configured()
        self._discovery = discovery_info
        self.context["title_placeholders"] = {"name": _title(discovery_info)}
        return await self.async_step_bluetooth_confirm()

    async def async_step_bluetooth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        assert self._discovery
        if user_input is not None:
            return self.async_create_entry(title=_title(self._discovery), data={})
        self._set_confirm_only()
        return self.async_show_form(
            step_id="bluetooth_confirm",
            description_placeholders={"name": _title(self._discovery)},
        )

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        if user_input is not None:
            info = self._candidates[user_input[CONF_ADDRESS]]
            await self.async_set_unique_id(info.address, raise_on_progress=False)
            self._abort_if_unique_id_configured()
            return self.async_create_entry(title=_title(info), data={})

        configured = self._async_current_ids(include_ignore=False)
        for info in async_discovered_service_info(self.hass, connectable=True):
            if (info.name or "").startswith("Yeelight Blu") and info.address not in configured:
                self._candidates[info.address] = info
        if not self._candidates:
            return self.async_abort(reason="no_devices_found")
        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_ADDRESS): vol.In(
                        {a: f"{_title(i)} ({a}, {i.rssi} dBm)" for a, i in self._candidates.items()}
                    )
                }
            ),
        )
