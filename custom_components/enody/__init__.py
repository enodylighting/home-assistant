"""The Enody Home Assistant integration."""

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant

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
    await coordinator.async_config_entry_first_refresh()

    if entry.title != coordinator.data.name:
        hass.config_entries.async_update_entry(entry, title=coordinator.data.name)

    entry.runtime_data = coordinator
    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)
    return True


async def async_unload_entry(hass: HomeAssistant, entry: EnodyConfigEntry) -> bool:
    """Unload an Enody config entry."""
    return await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
