"""Tests for the enody-py adapter."""

import asyncio
import sys
import types
import unittest
from contextlib import suppress
from threading import Event, Lock
from typing import Any
from unittest.mock import patch

from custom_components.enody import api

TOKEN_DATA = {
    "host_id": "98a316b1-bf8c-4000-8000-000000000001",
    "key_id": "ha-test",
    "data": [7] * 32,
}


class FakeToken:
    """Minimal enody.Token stand-in."""

    def __init__(self, data: dict[str, Any]) -> None:
        self._data = data

    def to_dict(self) -> dict[str, Any]:
        """Return token data."""
        return dict(self._data)


class FakeTokenFactory:
    """Fake enody.Token class."""

    @staticmethod
    def from_dict(data: dict[str, Any]) -> FakeToken:
        """Restore a token."""
        return FakeToken(data)


class FakeConfiguration:
    """Fake enody.Configuration class."""

    @staticmethod
    def blackbody(value: float) -> tuple[str, float]:
        """Build a blackbody configuration."""
        return ("blackbody", value)

    @staticmethod
    def chromatic(x: float, y: float) -> tuple[str, float, float]:
        """Build a chromatic configuration."""
        return ("chromatic", x, y)

    @staticmethod
    def flux() -> tuple[str]:
        """Build a flux-only configuration."""
        return ("flux",)


class FakeFlux:
    """Fake enody.Flux class."""

    @staticmethod
    def relative(value: float) -> tuple[str, float]:
        """Build relative flux."""
        return ("relative", value)


class FakeTransition:
    """Fake enody.Transition class."""

    @staticmethod
    def linear(
        configuration: Any, flux: Any, duration: float
    ) -> tuple[Any, Any, float]:
        """Build a linear transition."""
        return (configuration, flux, duration)


class FakeFixture:
    """Fake Enody fixture."""

    def __init__(self, identifier: str = "fixture-1") -> None:
        self._identifier = identifier
        self.display_calls: list[tuple[Any, Any]] = []
        self.display_error: Exception | None = None
        self.transition_calls: list[Any] = []
        self.transition_error: Exception | None = None
        self.transition_entered = Event()
        self.allow_transition = Event()
        self.allow_transition.set()

    def identifier(self) -> str:
        """Return the fixture ID."""
        return self._identifier

    def display(self, configuration: Any, flux: Any) -> None:
        """Record a display command."""
        if self.display_error is not None:
            raise self.display_error
        self.display_calls.append((configuration, flux))

    def transition(self, transition: Any) -> None:
        """Record a transition and wait for the device to finish."""
        self.transition_calls.append(transition)
        self.transition_entered.set()
        self.allow_transition.wait()
        if self.transition_error is not None:
            raise self.transition_error


class FakeHost:
    """Fake Enody host."""

    def __init__(self, fixtures: list[FakeFixture]) -> None:
        self._fixtures = fixtures

    def identifier(self) -> str:
        """Return the host ID."""
        return TOKEN_DATA["host_id"].upper()

    def version(self) -> str:
        """Return firmware version."""
        return "0.2.0"

    def fixtures(self) -> list[FakeFixture]:
        """Return fixtures."""
        return self._fixtures


class FakeRuntime:
    """Fake Enody runtime."""

    def __init__(self, fixtures: list[FakeFixture]) -> None:
        self._fixtures = fixtures
        self._host: FakeHost | None = None
        self.host_fetch_count = 0
        self.connect_count = 0
        self.disconnect_count = 0
        self.connect_error: Exception | None = None
        self.host_error: Exception | None = None
        self.disconnect_error: Exception | None = None
        self.connect_entered = Event()
        self.allow_connect = Event()
        self.allow_connect.set()
        self.active_connections = 0
        self.max_active_connections = 0
        self._state_lock = Lock()

    def connect(self) -> None:
        """Connect the runtime."""
        self.connect_count += 1
        with self._state_lock:
            self.active_connections += 1
            self.max_active_connections = max(
                self.max_active_connections,
                self.active_connections,
            )
        self.connect_entered.set()
        self.allow_connect.wait()
        if self.connect_error is not None:
            raise self.connect_error

    def disconnect(self) -> None:
        """Disconnect the runtime."""
        self.disconnect_count += 1
        with self._state_lock:
            self.active_connections -= 1
        if self.disconnect_error is not None:
            raise self.disconnect_error

    def is_connected(self) -> bool:
        """Return whether the fake transport is open."""
        return self.active_connections > 0

    def host(self) -> FakeHost:
        """Return the host."""
        if self.host_error is not None:
            raise self.host_error
        if self._host is None:
            self.host_fetch_count += 1
            self._host = FakeHost(self._fixtures)
        return self._host


class FakeWifiConnection:
    """Fake enody.WifiConnection class."""

    runtime: FakeRuntime

    @classmethod
    def runtime_from_endpoint(cls, token: FakeToken, endpoint: str) -> FakeRuntime:
        """Return the configured runtime."""
        assert token.to_dict() == TOKEN_DATA
        assert endpoint == "192.0.2.10:8788"
        return cls.runtime


class FakeHass:
    """Minimal Home Assistant executor stand-in."""

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        """Run executor work inline."""
        return func(*args)


class ThreadedFakeHass:
    """Home Assistant executor stand-in that uses real worker threads."""

    async def async_add_executor_job(self, func: Any, *args: Any) -> Any:
        """Run work in the default executor."""
        return await asyncio.to_thread(func, *args)


class EnodyApiTest(unittest.IsolatedAsyncioTestCase):
    """Test the SDK adapter boundary."""

    def setUp(self) -> None:
        """Install a fake enody module."""
        self.fixture = FakeFixture()
        self.runtime = FakeRuntime([self.fixture])
        FakeWifiConnection.runtime = self.runtime
        self.generate_arguments: dict[str, Any] = {}

        def generate_wifi_token(**kwargs: Any) -> FakeToken:
            self.generate_arguments = kwargs
            if callback := kwargs.get("on_approval"):
                callback("Touch the approval pad.")
            return FakeToken(TOKEN_DATA)

        self.fake_enody = types.SimpleNamespace(
            Configuration=FakeConfiguration,
            Flux=FakeFlux,
            Transition=FakeTransition,
            Token=FakeTokenFactory,
            WifiConnection=FakeWifiConnection,
            generate_wifi_token=generate_wifi_token,
        )
        self.previous_enody = sys.modules.get("enody")
        sys.modules["enody"] = self.fake_enody

    def tearDown(self) -> None:
        """Restore the enody module."""
        if self.previous_enody is None:
            sys.modules.pop("enody", None)
        else:
            sys.modules["enody"] = self.previous_enody

    def client(self) -> Any:
        """Create a client for the fake device."""
        return api.EnodyClient(FakeHass(), TOKEN_DATA, "192.0.2.10:8788")

    def test_pairing_uses_sdk_verification(self) -> None:
        """Pairing delegates token verification to enody-py."""
        approvals: list[str] = []

        token = api.pair_device_sync("192.0.2.10:8788", approvals.append)

        self.assertEqual(token, TOKEN_DATA)
        self.assertEqual(approvals, ["Touch the approval pad."])
        self.assertEqual(self.generate_arguments["endpoint"], "192.0.2.10:8788")
        self.assertEqual(self.generate_arguments["on_approval"], approvals.append)
        self.assertIs(self.generate_arguments["verify"], True)
        self.assertIs(self.generate_arguments["save"], False)

    def test_pairing_errors_are_normalized(self) -> None:
        """SDK pairing errors have one integration exception."""
        self.fake_enody.generate_wifi_token = lambda **_kwargs: (_ for _ in ()).throw(
            RuntimeError("pairing refused")
        )

        with self.assertRaises(api.EnodyPairingError) as result:
            api.pair_device_sync("192.0.2.10:8788")

        self.assertIsInstance(result.exception.__cause__, RuntimeError)

    def test_invalid_generated_token_is_normalized(self) -> None:
        """A malformed SDK token is reported as a pairing failure."""
        self.fake_enody.generate_wifi_token = lambda **_kwargs: FakeToken({})

        with self.assertRaises(api.EnodyPairingError) as result:
            api.pair_device_sync("192.0.2.10:8788")

        self.assertIsInstance(result.exception.__cause__, ValueError)

    def test_import_errors_are_normalized(self) -> None:
        """Import-time dependency errors do not escape the adapter."""
        with patch.object(
            api.importlib,
            "import_module",
            side_effect=ValueError("binary mismatch"),
        ):
            with self.assertRaises(api.EnodyDependencyError) as result:
                api._load_enody()

        self.assertIsInstance(result.exception.__cause__, ValueError)

    async def test_get_info_reuses_connection_and_refreshes_metadata(self) -> None:
        """Polling performs fresh reads without replacing the connection."""
        client = self.client()
        info = await client.async_get_info()
        self.assertEqual(await client.async_get_info(), info)

        self.assertEqual(info.host_id, TOKEN_DATA["host_id"])
        self.assertEqual(info.firmware_version, "0.2.0")
        self.assertEqual(info.fixture_ids, ("fixture-1",))
        self.assertEqual(info.name, "Enody EP01 98A316B1")
        self.assertEqual(self.runtime.connect_count, 1)
        self.assertEqual(self.runtime.disconnect_count, 0)
        self.assertEqual(self.runtime.host_fetch_count, 2)

    async def test_on_off_on_and_poll_share_one_connection(self) -> None:
        """Rapid toggles never open another EP01 connection."""
        client = self.client()
        await client.async_get_info()
        for flux in (1, 0, 1):
            await client.async_display_fixture("fixture-1", flux)
        await client.async_get_info()

        self.assertEqual(self.runtime.connect_count, 1)
        self.assertEqual(self.runtime.disconnect_count, 0)
        self.assertEqual(self.runtime.max_active_connections, 1)
        self.assertEqual(len(self.fixture.display_calls), 3)

    async def test_display_color_temperature(self) -> None:
        """Color temperature and brightness map to enody-py types."""
        await self.client().async_display_fixture(
            "fixture-1",
            0.5,
            color_temp_kelvin=3000,
        )

        self.assertEqual(
            self.fixture.display_calls,
            [(("blackbody", 3000.0), ("relative", 0.5))],
        )
        self.assertEqual(self.runtime.disconnect_count, 0)

    async def test_display_xy_clamps_flux(self) -> None:
        """XY color maps to chromaticity and relative flux is clamped."""
        await self.client().async_display_fixture(
            "fixture-1",
            2,
            xy_color=(0.3, 0.4),
        )

        self.assertEqual(
            self.fixture.display_calls,
            [(("chromatic", 0.3, 0.4), ("relative", 1.0))],
        )

    async def test_display_off_uses_flux_only(self) -> None:
        """Turning off uses one flux-only command."""
        await self.client().async_display_fixture("fixture-1", 0)

        self.assertEqual(
            self.fixture.display_calls,
            [(("flux",), ("relative", 0.0))],
        )

    async def test_transition_sends_one_device_command(self) -> None:
        """Color, dimming, and off fades send only one transition each."""
        cases = [
            (0.5, {"color_temp_kelvin": 3000}, ("blackbody", 3000.0), 0.5),
            (2, {"xy_color": (0.3, 0.4)}, ("chromatic", 0.3, 0.4), 1.0),
            (0.25, {}, ("flux",), 0.25),
            (0, {}, ("flux",), 0.0),
        ]
        client = self.client()
        for flux, color, configuration, expected_flux in cases:
            with self.subTest(flux=flux, color=color):
                self.fixture.transition_calls.clear()
                await client.async_display_fixture(
                    "fixture-1", flux, transition=2.5, **color
                )

                self.assertEqual(
                    self.fixture.transition_calls,
                    [(configuration, ("relative", expected_flux), 2.5)],
                )
                self.assertEqual(self.fixture.display_calls, [])
        self.assertEqual(self.runtime.connect_count, 1)
        self.assertEqual(self.runtime.disconnect_count, 0)

    async def test_zero_duration_uses_one_immediate_display(self) -> None:
        """An explicit zero duration preserves immediate light control."""
        await self.client().async_display_fixture("fixture-1", 0.5, transition=0)

        self.assertEqual(
            self.fixture.display_calls,
            [(("flux",), ("relative", 0.5))],
        )
        self.assertEqual(self.fixture.transition_calls, [])

    async def test_transition_failure_does_not_fall_back_to_display(self) -> None:
        """Unsupported commands and timeouts fail once and disconnect."""
        for error in (
            RuntimeError("Unsupported"),
            TimeoutError("transition timed out"),
        ):
            with self.subTest(error=error):
                self.fixture.transition_calls.clear()
                self.fixture.transition_error = error

                with self.assertRaises(api.EnodyCannotConnect) as result:
                    await self.client().async_display_fixture(
                        "fixture-1", 0.5, transition=2.5
                    )

                self.assertIs(result.exception.__cause__, error)
                self.assertEqual(len(self.fixture.transition_calls), 1)
                self.assertEqual(self.fixture.display_calls, [])
        self.assertEqual(self.runtime.disconnect_count, 2)

    async def test_transition_keeps_connection_until_completion(self) -> None:
        """A fade waits off the event loop and keeps its runtime connected."""
        self.fixture.allow_transition.clear()
        client = api.EnodyClient(ThreadedFakeHass(), TOKEN_DATA, "192.0.2.10:8788")
        task = asyncio.create_task(
            client.async_display_fixture("fixture-1", 0.5, transition=2.5)
        )
        try:
            entered = await asyncio.to_thread(self.fixture.transition_entered.wait, 1)
            self.assertTrue(entered)
            self.assertFalse(task.done())
            self.assertEqual(self.runtime.disconnect_count, 0)
            self.assertEqual(len(self.fixture.transition_calls), 1)
            self.assertEqual(self.fixture.display_calls, [])
        finally:
            self.fixture.allow_transition.set()
            await asyncio.wait_for(task, 1)

        self.assertEqual(self.runtime.disconnect_count, 0)
        await client.async_disconnect()
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_display_errors_are_normalized_and_disconnected(self) -> None:
        """Display failures reach Home Assistant and still clean up."""
        self.fixture.display_error = BrokenPipeError("connection closed")

        with self.assertRaises(api.EnodyCannotConnect) as result:
            await self.client().async_display_fixture("fixture-1", 0.5)

        self.assertIsInstance(result.exception.__cause__, BrokenPipeError)
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_failed_command_is_not_replayed_and_next_poll_reconnects(
        self,
    ) -> None:
        """Failure drops the stale runtime; only later work reconnects."""
        client = self.client()
        await client.async_get_info()
        self.fixture.display_error = TimeoutError("response lost")

        with self.assertRaises(api.EnodyCannotConnect):
            await client.async_display_fixture("fixture-1", 0.5)

        self.assertEqual(self.runtime.connect_count, 1)
        self.assertEqual(self.runtime.disconnect_count, 1)
        self.fixture.display_error = None
        await client.async_get_info()
        await client.async_display_fixture("fixture-1", 0.5)

        self.assertEqual(self.runtime.connect_count, 2)
        self.assertEqual(self.runtime.disconnect_count, 1)
        self.assertEqual(len(self.fixture.display_calls), 1)

    async def test_metadata_failure_drops_connection(self) -> None:
        """A polling failure cleans up before a subsequent refresh."""
        client = self.client()
        await client.async_get_info()
        self.runtime.host_error = TimeoutError("device offline")
        with self.assertRaises(api.EnodyCannotConnect):
            await client.async_get_info()
        self.assertEqual(self.runtime.disconnect_count, 1)

        self.runtime.host_error = None
        await client.async_get_info()
        self.assertEqual(self.runtime.connect_count, 2)

    async def test_closed_transport_is_discarded_before_reconnect(self) -> None:
        """A transport reported closed is not reused."""
        client = self.client()
        await client.async_get_info()
        with patch.object(self.runtime, "is_connected", return_value=False):
            await client.async_get_info()
        self.assertEqual(self.runtime.connect_count, 2)
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_disconnect_is_idempotent(self) -> None:
        """Unload or stop closes the connection at most once."""
        client = self.client()
        await client.async_get_info()
        await client.async_disconnect()
        await client.async_disconnect()
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_missing_fixture_is_reported(self) -> None:
        """A removed fixture produces a clear adapter failure."""
        with self.assertRaises(api.EnodyCannotConnect):
            await self.client().async_display_fixture("missing", 0.5)

        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_connect_errors_are_normalized_and_disconnected(self) -> None:
        """Connection failures are normalized across the whole SDK boundary."""
        self.runtime.connect_error = ConnectionRefusedError("refused")

        with self.assertRaises(api.EnodyCannotConnect) as result:
            await self.client().async_get_info()

        self.assertIsInstance(result.exception.__cause__, ConnectionRefusedError)
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_disconnect_error_does_not_mask_success(self) -> None:
        """Best-effort cleanup cannot turn a successful command into failure."""
        self.runtime.disconnect_error = OSError("already closed")

        client = self.client()
        await client.async_display_fixture("fixture-1", 0.5)
        await client.async_disconnect()
        await client.async_disconnect()

        self.assertEqual(len(self.fixture.display_calls), 1)
        self.assertEqual(self.runtime.disconnect_count, 1)

    async def test_cancelled_call_keeps_executor_work_serialized(self) -> None:
        """Cancellation cannot let a second SDK call overlap the first."""
        self.runtime.allow_connect.clear()
        client = api.EnodyClient(
            ThreadedFakeHass(),
            TOKEN_DATA,
            "192.0.2.10:8788",
        )

        first = asyncio.create_task(client.async_display_fixture("fixture-1", 0.5))
        connected = await asyncio.to_thread(self.runtime.connect_entered.wait, 1)
        self.assertTrue(connected)

        first.cancel()
        with suppress(asyncio.CancelledError):
            await first
        second = asyncio.create_task(client.async_display_fixture("fixture-1", 0.5))
        await asyncio.sleep(0)
        self.runtime.allow_connect.set()
        await second

        self.assertEqual(self.runtime.max_active_connections, 1)
        self.assertEqual(len(self.fixture.display_calls), 2)
        self.assertEqual(self.runtime.connect_count, 1)

    async def test_disconnect_waits_for_pending_transition(self) -> None:
        """Unloading cannot disconnect during an in-flight command."""
        self.fixture.allow_transition.clear()
        client = api.EnodyClient(ThreadedFakeHass(), TOKEN_DATA, "192.0.2.10:8788")
        command = asyncio.create_task(
            client.async_display_fixture("fixture-1", 0.5, transition=2.5)
        )
        disconnect = None
        try:
            entered = await asyncio.to_thread(self.fixture.transition_entered.wait, 1)
            self.assertTrue(entered)
            disconnect = asyncio.create_task(client.async_disconnect())
            await asyncio.sleep(0)
            self.assertFalse(disconnect.done())
            self.assertEqual(self.runtime.disconnect_count, 0)
        finally:
            self.fixture.allow_transition.set()
            await asyncio.wait_for(command, 1)
            if disconnect is not None:
                await asyncio.wait_for(disconnect, 1)
        self.assertEqual(self.runtime.disconnect_count, 1)
