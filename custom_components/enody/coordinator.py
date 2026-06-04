"""Coordinator for the Enody integration."""

from datetime import timedelta
from logging import getLogger

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .api import EnodyClient, EnodyDependencyError, EnodyDeviceInfo, EnodyError
from .const import DEFAULT_SCAN_INTERVAL_SECONDS, DOMAIN

LOGGER = getLogger(__name__)


class EnodyCoordinator(DataUpdateCoordinator[EnodyDeviceInfo]):
    """Refresh Enody device metadata and availability."""

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: EnodyClient,
    ) -> None:
        """Initialize the coordinator."""
        super().__init__(
            hass,
            LOGGER,
            config_entry=entry,
            name=DOMAIN,
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL_SECONDS),
            always_update=False,
        )
        self.client = client

    async def _async_update_data(self) -> EnodyDeviceInfo:
        """Fetch current device metadata."""
        try:
            return await self.client.async_get_info()
        except EnodyDependencyError as err:
            raise ConfigEntryError(
                translation_domain=DOMAIN,
                translation_key="dependency_error",
            ) from err
        except EnodyError as err:
            raise UpdateFailed("Unable to communicate with the Enody device") from err
