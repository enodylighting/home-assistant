"""Light platform for Enody fixtures."""

from logging import getLogger
from typing import Any

from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    ColorMode,
    LightEntity,
    LightEntityFeature,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from . import EnodyConfigEntry
from .api import EnodyError
from .const import (
    DEFAULT_COLOR_TEMP_KELVIN,
    DEFAULT_XY_COLOR,
    DOMAIN,
    MANUFACTURER,
    MAX_COLOR_TEMP_KELVIN,
    MIN_COLOR_TEMP_KELVIN,
    MODEL,
)
from .coordinator import EnodyCoordinator

LOGGER = getLogger(__name__)
PARALLEL_UPDATES = 1


async def async_setup_entry(
    hass: HomeAssistant,
    entry: EnodyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Enody light entities."""
    coordinator = entry.runtime_data
    fixture_count = len(coordinator.data.fixture_ids)
    async_add_entities(
        EnodyFixtureLight(coordinator, fixture_id, fixture_count)
        for fixture_id in coordinator.data.fixture_ids
    )


class EnodyFixtureLight(CoordinatorEntity[EnodyCoordinator], LightEntity):
    """Representation of an Enody fixture."""

    _attr_assumed_state = True
    _attr_has_entity_name = True
    _attr_supported_features = LightEntityFeature.TRANSITION
    _attr_supported_color_modes = {ColorMode.COLOR_TEMP, ColorMode.XY}
    _attr_min_color_temp_kelvin = MIN_COLOR_TEMP_KELVIN
    _attr_max_color_temp_kelvin = MAX_COLOR_TEMP_KELVIN

    def __init__(
        self,
        coordinator: EnodyCoordinator,
        fixture_id: str,
        fixture_count: int,
    ) -> None:
        """Initialize the light."""
        super().__init__(coordinator)
        self._fixture_id = fixture_id
        self._attr_unique_id = f"{coordinator.data.host_id}_{fixture_id}"
        self._attr_name = None if fixture_count == 1 else fixture_id
        self._attr_is_on = None
        self._attr_brightness = 255
        self._attr_color_mode = ColorMode.COLOR_TEMP
        self._attr_color_temp_kelvin = DEFAULT_COLOR_TEMP_KELVIN
        self._attr_xy_color = DEFAULT_XY_COLOR

    @property
    def device_info(self) -> DeviceInfo:
        """Return current device registry information."""
        info = self.coordinator.data
        return DeviceInfo(
            identifiers={(DOMAIN, info.host_id)},
            manufacturer=MANUFACTURER,
            model=MODEL,
            name=info.name,
            serial_number=info.serial_number,
            sw_version=info.firmware_version,
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on the light."""
        brightness = max(
            0,
            min(255, int(kwargs.get(ATTR_BRIGHTNESS, self._attr_brightness or 255))),
        )
        color_mode = self._attr_color_mode or ColorMode.COLOR_TEMP
        color_temp_kelvin = self._attr_color_temp_kelvin
        xy_color = self._attr_xy_color

        if ATTR_COLOR_TEMP_KELVIN in kwargs:
            color_mode = ColorMode.COLOR_TEMP
            color_temp_kelvin = max(
                MIN_COLOR_TEMP_KELVIN,
                min(MAX_COLOR_TEMP_KELVIN, int(kwargs[ATTR_COLOR_TEMP_KELVIN])),
            )
        elif ATTR_XY_COLOR in kwargs:
            color_mode = ColorMode.XY
            xy_color = tuple(kwargs[ATTR_XY_COLOR])

        try:
            await self.coordinator.client.async_display_fixture(
                self._fixture_id,
                brightness / 255,
                color_temp_kelvin=(
                    color_temp_kelvin if color_mode == ColorMode.COLOR_TEMP else None
                ),
                xy_color=xy_color if color_mode == ColorMode.XY else None,
                transition=kwargs.get(ATTR_TRANSITION, 0),
            )
        except EnodyError as err:
            self.coordinator.async_set_update_error(err)
            LOGGER.debug(
                "Failed to turn on fixture %s",
                self._fixture_id,
                exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
            ) from err

        self._attr_is_on = brightness > 0
        self._attr_brightness = brightness
        self._attr_color_mode = color_mode
        self._attr_color_temp_kelvin = color_temp_kelvin
        self._attr_xy_color = xy_color
        self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off the light."""
        try:
            await self.coordinator.client.async_display_fixture(
                self._fixture_id,
                0,
                transition=kwargs.get(ATTR_TRANSITION, 0),
            )
        except EnodyError as err:
            self.coordinator.async_set_update_error(err)
            LOGGER.debug(
                "Failed to turn off fixture %s",
                self._fixture_id,
                exc_info=True,
            )
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="communication_error",
            ) from err

        self._attr_is_on = False
        self.async_write_ha_state()
