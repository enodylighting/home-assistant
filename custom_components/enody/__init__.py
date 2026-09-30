"""The Enody Home Assistant integration."""

from asyncio import CancelledError

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EVENT_HOMEASSISTANT_STOP, Platform
from homeassistant.core import Event, HomeAssistant

from .api import EnodyClient
from .const import CONF_ENDPOINT, CONF_TOKEN
from .coordinator import EnodyCoordinator

PLATFORMS = [Platform.LIGHT]

type EnodyConfigEntry = ConfigEntry[EnodyCoordinator]


async def async_setup_entry(hass: HomeAssistant, entry: EnodyConfigEntry) -> bool:
    """Set up Enody from a config entry."""
    client = EnodyClient(
        hass,
        entry.data[CONF_TOKEN],
        entry.data[CONF_ENDPOINT],
    )
    coordinator = EnodyCoordinator(hass, entry, client)
    try:
        await coordinator.async_config_entry_first_refresh()

        if entry.title != coordinator.data.name:
            hass.config_entries.async_update_entry(entry, title=coordinator.data.name)

        entry.runtime_data = coordinator
        await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    except (Exception, CancelledError):
        await client.async_disconnect()
        raise

    async def async_stop(_event: Event) -> None:
        """Close the device connection when Home Assistant stops."""
        await client.async_disconnect()

    entry.async_on_unload(
        hass.bus.async_listen_once(EVENT_HOMEASSISTANT_STOP, async_stop)
    )
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnodyConfigEntry) -> bool:
    """Unload an Enody config entry."""
    if not await hass.config_entries.async_unload_platforms(entry, PLATFORMS):
        return False
    await entry.runtime_data.client.async_disconnect()
    return True
