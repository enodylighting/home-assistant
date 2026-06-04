"""Config flow for the Enody integration."""

import asyncio
from contextlib import suppress
from dataclasses import dataclass
from logging import getLogger
from typing import Any, override

import voluptuous as vol
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    ConfigFlow,
    ConfigFlowResult,
)
from homeassistant.core import callback
from homeassistant.data_entry_flow import UnknownFlow
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .api import EnodyDependencyError, EnodyError, pair_device_sync
from .const import (
    CONF_ENDPOINT,
    CONF_TOKEN,
    DEFAULT_WIFI_PORT,
    DOMAIN,
    MODEL,
    PAIRING_PROGRESS_ACTION,
)

LOGGER = getLogger(__name__)


@dataclass(frozen=True)
class _PairingTarget:
    """Device selected for pairing."""

    endpoint: str
    expected_host_id: str | None = None


class EnodyConfigFlow(ConfigFlow, domain=DOMAIN):
    """Handle an Enody config flow."""

    VERSION = 1

    def __init__(self) -> None:
        """Initialize the flow."""
        self._target: _PairingTarget | None = None
        self._pairing_task: asyncio.Task[dict[str, Any]] | None = None
        self._token_data: dict[str, Any] | None = None
        self._pairing_error = "pairing_failed"
        self._approval_text = ""

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle manual setup."""
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                endpoint = _normalize_endpoint(str(user_input[CONF_ENDPOINT]))
            except ValueError:
                errors["base"] = "invalid_endpoint"
            else:
                self._set_target(endpoint)
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema({vol.Required(CONF_ENDPOINT): str}),
            errors=errors,
        )

    @override
    async def async_step_zeroconf(
        self,
        discovery_info: ZeroconfServiceInfo,
    ) -> ConfigFlowResult:
        """Handle mDNS discovery."""
        properties = {
            str(key).lower(): str(value)
            for key, value in discovery_info.properties.items()
        }
        if not _is_supported_device(properties, discovery_info.name):
            return self.async_abort(reason="not_enody_device")

        try:
            port = int(properties.get("port", discovery_info.port))
            endpoint = _endpoint_from_host_port(discovery_info.host, port)
        except (TypeError, ValueError):
            return self.async_abort(reason="not_enody_device")

        host_id = _normalize_host_id(properties.get("id"))
        title = _device_title(host_id)
        if host_id is not None:
            await self.async_set_unique_id(host_id)
            self._abort_if_unique_id_configured(
                updates={CONF_ENDPOINT: endpoint},
                reload_on_update=True,
            )
        else:
            await self._async_handle_discovery_without_unique_id()

        self._set_target(endpoint, host_id)
        self.context["title_placeholders"] = {"name": title}
        return await self.async_step_zeroconf_confirm()

    async def async_step_zeroconf_confirm(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Confirm a discovered device before pairing."""
        assert self._target is not None
        if user_input is not None:
            return await self.async_step_pair()

        self._set_confirm_only()
        return self.async_show_form(
            step_id="zeroconf_confirm",
            description_placeholders={
                "name": _device_title(self._target.expected_host_id),
                "endpoint": self._target.endpoint,
            },
        )

    async def async_step_reconfigure(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pair again or update the endpoint for an existing device."""
        entry = self._get_reconfigure_entry()
        expected_host_id = _normalize_host_id(entry.unique_id)
        if expected_host_id is not None and entry.unique_id != expected_host_id:
            self.hass.config_entries.async_update_entry(
                entry,
                unique_id=expected_host_id,
            )
        errors: dict[str, str] = {}
        if user_input is not None:
            try:
                endpoint = _normalize_endpoint(str(user_input[CONF_ENDPOINT]))
            except ValueError:
                errors["base"] = "invalid_endpoint"
            else:
                self._set_target(
                    endpoint,
                    expected_host_id,
                )
                return await self.async_step_pair()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_ENDPOINT,
                        default=entry.data[CONF_ENDPOINT],
                    ): str
                }
            ),
            errors=errors,
        )

    async def async_step_pair(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Pair while showing device approval instructions."""
        assert self._target is not None
        if self._pairing_task is None:
            self._pairing_task = self.hass.async_create_task(
                self._async_pair(self._target.endpoint)
            )

        if not self._pairing_task.done():
            return self.async_show_progress(
                step_id="pair",
                progress_action=PAIRING_PROGRESS_ACTION,
                description_placeholders={
                    "endpoint": self._target.endpoint,
                    "approval_text": self._approval_text,
                },
                progress_task=self._pairing_task,
            )

        try:
            self._token_data = await self._pairing_task
        except EnodyDependencyError:
            LOGGER.debug("Unable to import enody-py", exc_info=True)
            self._pairing_error = "missing_dependency"
            return self.async_show_progress_done(next_step_id="pair_failed")
        except EnodyError:
            LOGGER.debug("Unable to pair with Enody device", exc_info=True)
            self._pairing_error = "pairing_failed"
            return self.async_show_progress_done(next_step_id="pair_failed")
        finally:
            self._pairing_task = None

        return self.async_show_progress_done(next_step_id="pair_finish")

    async def async_step_pair_finish(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Create or update an entry after successful pairing."""
        assert self._target is not None
        assert self._token_data is not None

        host_id = _normalize_host_id(self._token_data.get("host_id"))
        if host_id is None:
            return self.async_abort(reason="pairing_failed")
        if (
            self._target.expected_host_id is not None
            and host_id != self._target.expected_host_id
        ):
            return self.async_abort(reason="wrong_device")

        await self.async_set_unique_id(host_id)
        data = {
            CONF_ENDPOINT: self._target.endpoint,
            CONF_TOKEN: self._token_data,
        }

        if self.source == SOURCE_RECONFIGURE:
            self._abort_if_unique_id_mismatch()
            return self.async_update_reload_and_abort(
                self._get_reconfigure_entry(),
                data_updates=data,
            )

        self._abort_if_unique_id_configured(updates=data)
        return self.async_create_entry(
            title=_device_title(host_id),
            data=data,
        )

    async def async_step_pair_failed(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Let the user retry pairing."""
        if user_input is not None:
            self._pairing_error = "pairing_failed"
            self._approval_text = ""
            return await self.async_step_pair()

        return self.async_show_form(
            step_id="pair_failed",
            data_schema=vol.Schema({}),
            errors={"base": self._pairing_error},
        )

    async def _async_pair(self, endpoint: str) -> dict[str, Any]:
        """Run blocking pairing in the executor."""
        return await self.hass.async_add_executor_job(
            pair_device_sync,
            endpoint,
            self._handle_approval_text,
        )

    def _handle_approval_text(self, approval_text: str) -> None:
        """Forward an executor-thread callback to Home Assistant's loop."""
        self.hass.loop.call_soon_threadsafe(
            self._update_approval_text,
            approval_text,
        )

    @callback
    def _update_approval_text(self, approval_text: str) -> None:
        """Update pairing progress from Home Assistant's event loop."""
        self._approval_text = approval_text.strip()
        if (
            self.flow_id is not None
            and self._pairing_task is not None
            and not self._pairing_task.done()
        ):
            self.hass.async_create_task(self._async_refresh_pairing_progress())

    async def _async_refresh_pairing_progress(self) -> None:
        """Refresh pairing text unless the user has closed the flow."""
        assert self.flow_id is not None
        with suppress(UnknownFlow):
            await self.hass.config_entries.flow.async_configure(self.flow_id)

    def _set_target(
        self,
        endpoint: str,
        expected_host_id: str | None = None,
    ) -> None:
        """Set the device to pair and clear earlier progress."""
        self._target = _PairingTarget(endpoint, expected_host_id)
        self._pairing_task = None
        self._token_data = None
        self._pairing_error = "pairing_failed"
        self._approval_text = ""


def _normalize_endpoint(endpoint: str) -> str:
    """Normalize a host or native endpoint, including IPv6."""
    value = endpoint.strip()
    if not value or "://" in value or any(character.isspace() for character in value):
        raise ValueError("Invalid endpoint")

    if value.startswith("["):
        closing_bracket = value.find("]")
        if closing_bracket < 2:
            raise ValueError("Invalid IPv6 endpoint")
        host = value[1:closing_bracket]
        remainder = value[closing_bracket + 1 :]
        if not remainder:
            port = DEFAULT_WIFI_PORT
        elif remainder.startswith(":"):
            port = _normalize_port(remainder[1:])
        else:
            raise ValueError("Invalid IPv6 endpoint")
    elif value.count(":") == 0:
        host = value
        port = DEFAULT_WIFI_PORT
    elif value.count(":") == 1:
        host, raw_port = value.rsplit(":", 1)
        port = _normalize_port(raw_port)
    else:
        host = value
        port = DEFAULT_WIFI_PORT

    return _endpoint_from_host_port(host, port)


def _normalize_port(port: str | int) -> int:
    """Validate a TCP port."""
    value = int(port)
    if not 1 <= value <= 65535:
        raise ValueError("Invalid port")
    return value


def _endpoint_from_host_port(host: str, port: str | int) -> str:
    """Build an enody-py endpoint from a host and port."""
    normalized_host = host.strip().strip("[]").rstrip(".")
    if not normalized_host:
        raise ValueError("Invalid host")
    normalized_port = _normalize_port(port)
    if ":" in normalized_host:
        normalized_host = f"[{normalized_host}]"
    return f"{normalized_host}:{normalized_port}"


def _normalize_host_id(host_id: Any) -> str | None:
    """Return one stable form of an Enody host ID."""
    if host_id is None:
        return None
    value = str(host_id).strip().lower()
    return value or None


def _is_supported_device(properties: dict[str, str], name: str) -> bool:
    """Return whether mDNS metadata describes a supported Enody device."""
    if properties.get("proto") != "enody-v1":
        return False
    if properties.get("auth") not in (None, "noise-psk"):
        return False
    return properties.get("model", "").lower() == "ep01" or name.lower().startswith(
        "ep01 "
    )


def _device_title(host_id: str | None) -> str:
    """Return a readable device title."""
    suffix = f" {host_id[:8].upper()}" if host_id else ""
    return f"Enody {MODEL}{suffix}"
