"""Tests for setup and the Enody light entity."""

import asyncio
from unittest.mock import AsyncMock, Mock, patch

import pytest
from homeassistant.components.light import (
    ATTR_BRIGHTNESS,
    ATTR_COLOR_TEMP_KELVIN,
    ATTR_TRANSITION,
    ATTR_XY_COLOR,
    LightEntityFeature,
)
from homeassistant.components.light import (
    DOMAIN as LIGHT_DOMAIN,
)
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_SUPPORTED_FEATURES,
    EVENT_HOMEASSISTANT_STOP,
    SERVICE_TURN_OFF,
    SERVICE_TURN_ON,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers import entity_registry as er
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enody import async_unload_entry
from custom_components.enody.api import (
    EnodyCannotConnect,
    EnodyClient,
    EnodyDependencyError,
    EnodyDeviceInfo,
)
from custom_components.enody.const import (
    CONF_ENDPOINT,
    CONF_TOKEN,
    DOMAIN,
)

HOST_ID = "98a316b1-bf8c-4000-8000-000000000001"
TOKEN_DATA = {"host_id": HOST_ID, "key_id": "ha-test", "data": [7] * 32}
DEVICE_INFO = EnodyDeviceInfo(
    host_id=HOST_ID,
    firmware_version="0.2.0",
    fixture_ids=("fixture-1",),
)


def _entry() -> MockConfigEntry:
    """Create an Enody config entry."""
    return MockConfigEntry(
        domain=DOMAIN,
        title="Enody device",
        unique_id=HOST_ID,
        data={
            CONF_ENDPOINT: "192.0.2.10:8788",
            CONF_TOKEN: TOKEN_DATA,
        },
    )


def _client() -> Mock:
    """Create a mocked Enody client."""
    client = Mock(spec=EnodyClient)
    client.async_get_info = AsyncMock(return_value=DEVICE_INFO)
    client.async_display_fixture = AsyncMock()
    client.async_disconnect = AsyncMock()
    return client


async def _setup(hass: HomeAssistant, entry: MockConfigEntry, client: Mock) -> str:
    """Set up an entry and return its light entity ID."""
    entry.add_to_hass(hass)
    with patch("custom_components.enody.EnodyClient", return_value=client):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()

    entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
    assert len(entities) == 1
    return entities[0].entity_id


async def test_setup_creates_one_assumed_state_light(hass: HomeAssistant) -> None:
    """Setup creates the minimal entity and device model."""
    entry = _entry()
    client = _client()

    entity_id = await _setup(hass, entry, client)

    assert entry.title == DEVICE_INFO.name
    assert entry.runtime_data.client is client
    state = hass.states.get(entity_id)
    assert state.state == STATE_UNKNOWN
    assert state.attributes["assumed_state"] is True
    assert state.attributes[ATTR_SUPPORTED_FEATURES] & LightEntityFeature.TRANSITION

    devices = dr.async_entries_for_config_entry(dr.async_get(hass), entry.entry_id)
    assert len(devices) == 1
    assert devices[0].identifiers == {(DOMAIN, HOST_ID)}
    assert devices[0].sw_version == "0.2.0"


async def test_on_off_on_services_reuse_sdk_connection(hass: HomeAssistant) -> None:
    """The real adapter shares one runtime across HA services and polling."""
    fixture = Mock()
    fixture.identifier.return_value = "fixture-1"
    host = Mock()
    host.identifier.return_value = HOST_ID
    host.version.return_value = "0.2.0"
    host.fixtures.return_value = [fixture]
    runtime = Mock()
    runtime.host.return_value = host
    runtime.is_connected.return_value = True
    sdk = Mock()
    sdk.WifiConnection.runtime_from_endpoint.return_value = runtime
    entry = _entry()
    entry.add_to_hass(hass)

    with patch("custom_components.enody.api._load_enody", return_value=sdk):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        entities = er.async_entries_for_config_entry(er.async_get(hass), entry.entry_id)
        entity_id = entities[0].entity_id
        for service, expected in (
            (SERVICE_TURN_ON, "on"),
            (SERVICE_TURN_OFF, "off"),
            (SERVICE_TURN_ON, "on"),
        ):
            await hass.services.async_call(
                LIGHT_DOMAIN,
                service,
                {ATTR_ENTITY_ID: entity_id},
                blocking=True,
            )
            assert hass.states.get(entity_id).state == expected
        await entry.runtime_data.async_refresh()
        assert hass.states.get(entity_id).state == "on"
        sdk.WifiConnection.runtime_from_endpoint.assert_called_once()
        runtime.connect.assert_called_once()
        runtime.disconnect.assert_not_called()
        assert fixture.display.call_count == 3

        assert await hass.config_entries.async_unload(entry.entry_id)
        await hass.async_block_till_done()
        runtime.disconnect.assert_called_once()


@pytest.mark.parametrize("transition", [None, 0, 2.5])
async def test_light_actions_wait_for_one_device_command(
    hass: HomeAssistant, transition: float | None
) -> None:
    """Light actions await one command and update state afterward."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: entity_id,
            ATTR_BRIGHTNESS: 128,
            ATTR_COLOR_TEMP_KELVIN: 3000,
            **({ATTR_TRANSITION: transition} if transition is not None else {}),
        },
        blocking=True,
    )

    client.async_display_fixture.assert_awaited_once_with(
        "fixture-1",
        128 / 255,
        color_temp_kelvin=3000,
        xy_color=None,
        transition=transition or 0,
    )
    assert hass.states.get(entity_id).state == "on"
    assert hass.states.get(entity_id).attributes[ATTR_BRIGHTNESS] == 128

    client.async_display_fixture.reset_mock()
    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_OFF,
        {
            ATTR_ENTITY_ID: entity_id,
            **({ATTR_TRANSITION: transition} if transition is not None else {}),
        },
        blocking=True,
    )

    client.async_display_fixture.assert_awaited_once_with(
        "fixture-1", 0, transition=transition or 0
    )
    assert hass.states.get(entity_id).state == "off"


@pytest.mark.parametrize("transition", [0, 1.25])
async def test_light_xy_action_uses_one_device_command(
    hass: HomeAssistant, transition: float
) -> None:
    """XY color and brightness share one command, including during fades."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {
            ATTR_ENTITY_ID: entity_id,
            ATTR_BRIGHTNESS: 64,
            ATTR_XY_COLOR: (0.25, 0.35),
            ATTR_TRANSITION: transition,
        },
        blocking=True,
    )

    client.async_display_fixture.assert_awaited_once_with(
        "fixture-1",
        64 / 255,
        color_temp_kelvin=None,
        xy_color=(0.25, 0.35),
        transition=transition,
    )
    assert hass.states.get(entity_id).attributes[ATTR_XY_COLOR] == (0.25, 0.35)


@pytest.mark.parametrize("transition", [0, 2.5])
async def test_light_failure_reaches_the_service_caller(
    hass: HomeAssistant, transition: float
) -> None:
    """Failed I/O raises and does not publish a successful state."""
    entry = _entry()
    client = _client()
    client.async_display_fixture.side_effect = EnodyCannotConnect("offline")
    entity_id = await _setup(hass, entry, client)

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_ON,
            {
                ATTR_ENTITY_ID: entity_id,
                ATTR_BRIGHTNESS: 128,
                ATTR_TRANSITION: transition,
            },
            blocking=True,
        )

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.async_display_fixture.side_effect = None
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == STATE_UNKNOWN


@pytest.mark.parametrize("transition", [0, 2.5])
async def test_turn_off_failure_preserves_the_on_state(
    hass: HomeAssistant, transition: float
) -> None:
    """A failed off command is reported without publishing an off state."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)

    await hass.services.async_call(
        LIGHT_DOMAIN,
        SERVICE_TURN_ON,
        {ATTR_ENTITY_ID: entity_id},
        blocking=True,
    )
    client.async_display_fixture.side_effect = EnodyCannotConnect("offline")

    with pytest.raises(HomeAssistantError):
        await hass.services.async_call(
            LIGHT_DOMAIN,
            SERVICE_TURN_OFF,
            {ATTR_ENTITY_ID: entity_id, ATTR_TRANSITION: transition},
            blocking=True,
        )

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.async_display_fixture.side_effect = None
    await entry.runtime_data.async_refresh()
    assert hass.states.get(entity_id).state == "on"


@pytest.mark.parametrize("service", [SERVICE_TURN_ON, SERVICE_TURN_OFF])
async def test_transition_publishes_state_only_after_completion(
    hass: HomeAssistant, service: str
) -> None:
    """A pending fade leaves the last successful state intact."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)
    started = asyncio.Event()
    completed = asyncio.Event()

    async def wait_for_device(*args: object, **kwargs: object) -> None:
        started.set()
        await completed.wait()

    client.async_display_fixture.side_effect = wait_for_device
    task = asyncio.create_task(
        hass.services.async_call(
            LIGHT_DOMAIN,
            service,
            {ATTR_ENTITY_ID: entity_id, ATTR_TRANSITION: 2.5},
            blocking=True,
        )
    )
    try:
        await asyncio.wait_for(started.wait(), 1)
        assert not task.done()
        assert hass.states.get(entity_id).state == STATE_UNKNOWN
        client.async_display_fixture.assert_awaited_once()
    finally:
        completed.set()
        await asyncio.wait_for(task, 1)

    assert hass.states.get(entity_id).state == (
        "on" if service == SERVICE_TURN_ON else "off"
    )


async def test_coordinator_updates_entity_availability(hass: HomeAssistant) -> None:
    """Metadata refresh failures make entities unavailable until recovery."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)
    coordinator = entry.runtime_data

    client.async_get_info.side_effect = EnodyCannotConnect("offline")
    await coordinator.async_refresh()

    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE

    client.async_get_info.side_effect = None
    client.async_get_info.return_value = DEVICE_INFO
    await coordinator.async_refresh()

    assert hass.states.get(entity_id).state == STATE_UNKNOWN


async def test_setup_retries_when_device_is_offline(hass: HomeAssistant) -> None:
    """Initial connection failures put the entry into setup retry."""
    entry = _entry()
    client = _client()
    client.async_get_info.side_effect = EnodyCannotConnect("offline")
    entry.add_to_hass(hass)

    with patch("custom_components.enody.EnodyClient", return_value=client):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_RETRY
    client.async_disconnect.assert_awaited_once()


async def test_setup_fails_when_the_sdk_cannot_load(hass: HomeAssistant) -> None:
    """A missing or incompatible SDK is a permanent setup error."""
    entry = _entry()
    client = _client()
    client.async_get_info.side_effect = EnodyDependencyError("binary mismatch")
    entry.add_to_hass(hass)

    with patch("custom_components.enody.EnodyClient", return_value=client):
        assert not await hass.config_entries.async_setup(entry.entry_id)

    assert entry.state is ConfigEntryState.SETUP_ERROR
    client.async_disconnect.assert_awaited_once()


async def test_unload_removes_the_light(hass: HomeAssistant) -> None:
    """The integration unloads cleanly."""
    entry = _entry()
    client = _client()
    entity_id = await _setup(hass, entry, client)

    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()

    assert entry.state is ConfigEntryState.NOT_LOADED
    assert hass.states.get(entity_id).state == STATE_UNAVAILABLE
    client.async_disconnect.assert_awaited_once()


async def test_shutdown_disconnects_device(hass: HomeAssistant) -> None:
    """Home Assistant stopping closes the persistent device connection."""
    client = _client()
    await _setup(hass, _entry(), client)

    hass.bus.async_fire(EVENT_HOMEASSISTANT_STOP)
    await hass.async_block_till_done()

    client.async_disconnect.assert_awaited_once()


async def test_failed_unload_keeps_device_connected(hass: HomeAssistant) -> None:
    """A still-loaded platform must retain its connection."""
    client = _client()
    entry = _entry()
    await _setup(hass, entry, client)
    with patch.object(
        hass.config_entries, "async_unload_platforms", return_value=False
    ):
        assert not await async_unload_entry(hass, entry)
    client.async_disconnect.assert_not_awaited()
    assert await hass.config_entries.async_unload(entry.entry_id)
    await hass.async_block_till_done()


async def test_platform_setup_failure_closes_connection(hass: HomeAssistant) -> None:
    """A connection opened during setup must not leak if the platform fails."""
    client = _client()
    entry = _entry()
    entry.add_to_hass(hass)
    with (
        patch("custom_components.enody.EnodyClient", return_value=client),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=RuntimeError("platform setup failed"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    client.async_disconnect.assert_awaited_once()
