"""Tests for the Enody config flow."""

import asyncio
from ipaddress import ip_address
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest
from homeassistant.config_entries import (
    SOURCE_RECONFIGURE,
    SOURCE_USER,
    SOURCE_ZEROCONF,
)
from homeassistant.data_entry_flow import FlowResultType
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo
from pytest_homeassistant_custom_component.common import MockConfigEntry

from custom_components.enody.api import EnodyDependencyError, EnodyPairingError
from custom_components.enody.config_flow import EnodyConfigFlow
from custom_components.enody.const import (
    CONF_ENDPOINT,
    CONF_TOKEN,
    DOMAIN,
)

TOKEN_DATA = {
    "host_id": "98a316b1-bf8c-4000-8000-000000000001",
    "key_id": "ha-test",
    "data": [7] * 32,
}


async def _finish_pairing(
    hass: Any,
    progress_result: dict[str, Any],
    pairing_result: asyncio.Future[dict[str, Any]],
    outcome: dict[str, Any] | Exception,
) -> dict[str, Any]:
    """Complete a controlled pairing task and advance to its final step."""
    assert progress_result["type"] is FlowResultType.SHOW_PROGRESS
    flow_id = progress_result["flow_id"]
    if isinstance(outcome, Exception):
        pairing_result.set_exception(outcome)
    else:
        pairing_result.set_result(outcome)

    for _ in range(10):
        await asyncio.sleep(0)
        current = hass.config_entries.flow.async_get(flow_id)
        if current["step_id"] in {"pair_finish", "pair_failed"}:
            return await hass.config_entries.flow.async_configure(flow_id)

    pytest.fail("Pairing progress did not finish")


@pytest.mark.parametrize(
    ("raw", "normalized"),
    [
        ("ep01.local", "ep01.local:8788"),
        ("ep01.local.:9000", "ep01.local:9000"),
        ("2001:db8::1", "[2001:db8::1]:8788"),
        ("[2001:db8::1]", "[2001:db8::1]:8788"),
        ("[2001:db8::1]:9000", "[2001:db8::1]:9000"),
    ],
)
def test_normalize_endpoint(raw: str, normalized: str) -> None:
    """Manual endpoints are normalized consistently."""
    from custom_components.enody.config_flow import _normalize_endpoint

    assert _normalize_endpoint(raw) == normalized


@pytest.mark.parametrize(
    "endpoint",
    ["", "http://ep01.local", "host:", "host:0", "[", "[::1]bad", ":8788"],
)
async def test_invalid_manual_endpoint(hass: object, endpoint: str) -> None:
    """Invalid manual endpoints stay on the form."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENDPOINT: endpoint},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_endpoint"}


async def test_manual_pairing_creates_minimal_entry(hass: object) -> None:
    """Manual pairing stores only connection data."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )

    pairing_result = hass.loop.create_future()
    paired_endpoints: list[str] = []

    async def pair(_flow: EnodyConfigFlow, endpoint: str) -> dict[str, Any]:
        paired_endpoints.append(endpoint)
        return await pairing_result

    with (
        patch.object(EnodyConfigFlow, "_async_pair", pair),
        patch(
            "custom_components.enody.async_setup_entry",
            new=AsyncMock(return_value=True),
        ),
    ):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        result = await _finish_pairing(hass, result, pairing_result, TOKEN_DATA)
        await hass.async_block_till_done()

    assert result["type"] is FlowResultType.CREATE_ENTRY
    assert result["title"] == "Enody EP01 98A316B1"
    assert result["data"] == {
        CONF_ENDPOINT: "192.0.2.10:8788",
        CONF_TOKEN: TOKEN_DATA,
    }
    assert paired_endpoints == ["192.0.2.10:8788"]


async def test_zeroconf_updates_existing_endpoint(hass: object) -> None:
    """Discovery deduplicates by host ID and updates a changed address."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TOKEN_DATA["host_id"],
        data={
            CONF_ENDPOINT: "192.0.2.1:8788",
            CONF_TOKEN: TOKEN_DATA,
        },
    )
    entry.add_to_hass(hass)

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.20"),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "already_configured"
    assert entry.data[CONF_ENDPOINT] == "192.0.2.20:8788"


@pytest.mark.parametrize(
    "properties",
    [
        {"proto": "unsupported"},
        {"auth": "unsupported"},
        {"port": "0"},
    ],
)
async def test_zeroconf_rejects_unsupported_metadata(
    hass: object,
    properties: dict[str, str],
) -> None:
    """Discovery ignores devices with unsupported or invalid metadata."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.20", properties=properties),
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_enody_device"


async def test_zeroconf_rejects_a_missing_port(hass: object) -> None:
    """A missing discovery port is handled as invalid metadata."""
    discovery_info = _service_info("192.0.2.20")
    discovery_info.port = None

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=discovery_info,
    )

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "not_enody_device"


async def test_zeroconf_without_host_id_can_be_confirmed(hass: object) -> None:
    """Discovery without a host ID is deduplicated and asks for confirmation."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.20", include_host_id=False),
    )

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "zeroconf_confirm"

    pairing_result = hass.loop.create_future()

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_concurrent_zeroconf_flow_is_rejected(hass: object) -> None:
    """Only one discovery flow can configure a host at a time."""
    first = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.20"),
    )

    second = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.21"),
    )

    assert first["type"] is FlowResultType.FORM
    assert second["type"] is FlowResultType.ABORT
    assert second["reason"] == "already_in_progress"
    hass.config_entries.flow.async_abort(first["flow_id"])


async def test_zeroconf_pairing_rejects_a_different_host(hass: object) -> None:
    """A discovered endpoint cannot pair as a different device."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_ZEROCONF},
        data=_service_info("192.0.2.20"),
    )
    pairing_result = hass.loop.create_future()
    other_token = {**TOKEN_DATA, "host_id": "different-device"}

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )
        result = await _finish_pairing(hass, result, pairing_result, other_token)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"


@pytest.mark.parametrize(
    ("error", "error_key"),
    [
        (EnodyDependencyError("missing"), "missing_dependency"),
        (EnodyPairingError("refused"), "pairing_failed"),
    ],
)
async def test_pairing_errors_show_a_retry_form(
    hass: object,
    error: Exception,
    error_key: str,
) -> None:
    """Pairing failures remain recoverable and use translated errors."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    pairing_result = hass.loop.create_future()

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        result = await _finish_pairing(hass, result, pairing_result, error)

    assert result["type"] is FlowResultType.FORM
    assert result["step_id"] == "pair_failed"
    assert result["errors"] == {"base": error_key}


async def test_failed_pairing_can_be_retried(hass: object) -> None:
    """Submitting the failure form starts a fresh pairing task."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    first_result = hass.loop.create_future()
    second_result = hass.loop.create_future()
    pairing_results = iter((first_result, second_result))

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await next(pairing_results)

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        result = await _finish_pairing(
            hass,
            result,
            first_result,
            EnodyPairingError("refused"),
        )
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {},
        )

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_approval_callback_refreshes_progress(hass: object) -> None:
    """Approval text is moved onto the HA loop and shown in progress."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    pairing_result = hass.loop.create_future()
    active_flow: list[EnodyConfigFlow] = []

    async def pair(flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        active_flow.append(flow)
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        await asyncio.sleep(0)
        active_flow[0]._handle_approval_text("  Touch the approval pad.  ")
        await asyncio.sleep(0)
        result = await hass.config_entries.flow.async_configure(result["flow_id"])

    assert result["type"] is FlowResultType.SHOW_PROGRESS
    assert result["description_placeholders"]["approval_text"] == (
        "Touch the approval pad."
    )
    hass.config_entries.flow.async_abort(result["flow_id"])


async def test_approval_refresh_tolerates_an_aborted_flow(hass: object) -> None:
    """A late executor callback cannot leave an unhandled flow error."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    pairing_result = hass.loop.create_future()
    active_flow: list[EnodyConfigFlow] = []

    async def pair(flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        active_flow.append(flow)
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        await asyncio.sleep(0)
        active_flow[0]._handle_approval_text("Touch the approval pad.")
        hass.config_entries.flow.async_abort(result["flow_id"])
        await hass.async_block_till_done()

    assert hass.config_entries.flow.async_progress_by_handler(DOMAIN) == []


async def test_pairing_adapter_runs_in_the_executor(hass: object) -> None:
    """The blocking SDK pairing call stays outside the event loop."""
    flow = EnodyConfigFlow()
    flow.hass = hass

    with patch(
        "custom_components.enody.config_flow.pair_device_sync",
        return_value=TOKEN_DATA,
    ) as pair:
        result = await flow._async_pair("192.0.2.10:8788")

    assert result == TOKEN_DATA
    pair.assert_called_once()
    assert pair.call_args.args[0] == "192.0.2.10:8788"
    assert pair.call_args.args[1].__self__ is flow


async def test_invalid_pairing_token_is_rejected(hass: object) -> None:
    """A token without a host ID cannot create a config entry."""
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={"source": SOURCE_USER},
    )
    pairing_result = hass.loop.create_future()

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.10"},
        )
        result = await _finish_pairing(hass, result, pairing_result, {})

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "pairing_failed"


async def test_reconfigure_rejects_a_different_device(hass: object) -> None:
    """Reconfiguration cannot replace an entry with another host."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TOKEN_DATA["host_id"],
        data={
            CONF_ENDPOINT: "192.0.2.10:8788",
            CONF_TOKEN: TOKEN_DATA,
        },
    )
    entry.add_to_hass(hass)
    other_token = {**TOKEN_DATA, "host_id": "different-device"}

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )
    pairing_result = hass.loop.create_future()

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.20:8788"},
        )
        result = await _finish_pairing(hass, result, pairing_result, other_token)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "wrong_device"
    assert entry.data[CONF_ENDPOINT] == "192.0.2.10:8788"


async def test_reconfigure_updates_the_existing_entry(hass: object) -> None:
    """Reconfiguration replaces connection data for the same device."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TOKEN_DATA["host_id"].upper(),
        data={
            CONF_ENDPOINT: "192.0.2.10:8788",
            CONF_TOKEN: TOKEN_DATA,
        },
    )
    entry.add_to_hass(hass)
    pairing_result = hass.loop.create_future()

    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    async def pair(_flow: EnodyConfigFlow, _endpoint: str) -> dict[str, Any]:
        return await pairing_result

    with patch.object(EnodyConfigFlow, "_async_pair", pair):
        result = await hass.config_entries.flow.async_configure(
            result["flow_id"],
            {CONF_ENDPOINT: "192.0.2.20"},
        )
        result = await _finish_pairing(hass, result, pairing_result, TOKEN_DATA)

    assert result["type"] is FlowResultType.ABORT
    assert result["reason"] == "reconfigure_successful"
    assert entry.data[CONF_ENDPOINT] == "192.0.2.20:8788"


async def test_reconfigure_rejects_an_invalid_endpoint(hass: object) -> None:
    """Invalid reconfiguration input remains on the form."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=TOKEN_DATA["host_id"],
        data={
            CONF_ENDPOINT: "192.0.2.10:8788",
            CONF_TOKEN: TOKEN_DATA,
        },
    )
    entry.add_to_hass(hass)
    result = await hass.config_entries.flow.async_init(
        DOMAIN,
        context={
            "source": SOURCE_RECONFIGURE,
            "entry_id": entry.entry_id,
        },
    )

    result = await hass.config_entries.flow.async_configure(
        result["flow_id"],
        {CONF_ENDPOINT: "http://not-a-native-endpoint"},
    )

    assert result["type"] is FlowResultType.FORM
    assert result["errors"] == {"base": "invalid_endpoint"}


def _service_info(
    host: str,
    *,
    properties: dict[str, str] | None = None,
    include_host_id: bool = True,
) -> ZeroconfServiceInfo:
    """Create a supported Enody mDNS result."""
    address = ip_address(host)
    service_properties = {
        "model": "ep01",
        "proto": "enody-v1",
        "auth": "noise-psk",
    }
    if include_host_id:
        service_properties["id"] = TOKEN_DATA["host_id"]
    if properties:
        service_properties.update(properties)
    return ZeroconfServiceInfo(
        ip_address=address,
        ip_addresses=[address],
        port=8788,
        hostname="ep01.local.",
        type="_enody._tcp.local.",
        name="EP01 Studio._enody._tcp.local.",
        properties=service_properties,
    )
