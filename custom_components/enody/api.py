"""Small async adapter around enody-py's blocking WiFi API."""

import importlib
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from logging import getLogger
from threading import Lock
from typing import Any

from homeassistant.core import HomeAssistant

from .const import MODEL

ApprovalCallback = Callable[[str], None]
LOGGER = getLogger(__name__)


class EnodyError(Exception):
    """Base error for Enody integration failures."""


class EnodyDependencyError(EnodyError):
    """Raised when enody-py cannot be imported."""


class EnodyCannotConnect(EnodyError):
    """Raised when an Enody device cannot be reached."""


class EnodyPairingError(EnodyError):
    """Raised when an Enody device cannot be paired."""


@dataclass(frozen=True)
class EnodyDeviceInfo:
    """Device metadata used by Home Assistant."""

    host_id: str
    firmware_version: str | None
    fixture_ids: tuple[str, ...]

    @property
    def serial_number(self) -> str:
        """Return the serial number shown by Home Assistant."""
        return self.host_id.upper()

    @property
    def name(self) -> str:
        """Return the device name shown by Home Assistant."""
        return f"Enody {MODEL} {self.serial_number[:8]}"


def _load_enody() -> Any:
    """Import enody-py lazily in an executor thread."""
    try:
        return importlib.import_module("enody")
    except Exception as err:
        raise EnodyDependencyError("Unable to import enody-py") from err


def pair_device_sync(
    endpoint: str,
    on_approval: ApprovalCallback | None = None,
) -> dict[str, Any]:
    """Pair with a device and return its verified token."""
    enody = _load_enody()
    try:
        token = enody.generate_wifi_token(
            endpoint=endpoint,
            on_approval=on_approval,
            verify=True,
            save=False,
        )
        token_data = token.to_dict()
        if not isinstance(token_data, dict) or not token_data.get("host_id"):
            raise ValueError("The generated token has no host ID")
        return token_data
    except Exception as err:
        raise EnodyPairingError("Unable to pair with the Enody device") from err


class EnodyClient:
    """Run blocking enody-py calls in Home Assistant's executor."""

    def __init__(
        self,
        hass: HomeAssistant,
        token_data: dict[str, Any],
        endpoint: str,
    ) -> None:
        """Initialize the client."""
        self._hass = hass
        self._token_data = token_data
        self._endpoint = endpoint
        self._lock = Lock()
        self._runtime: Any | None = None

    async def async_get_info(self) -> EnodyDeviceInfo:
        """Return current device metadata."""
        return await self._hass.async_add_executor_job(self._get_info_sync)

    def _get_info_sync(self) -> EnodyDeviceInfo:
        """Read host and fixture metadata."""
        with self._lock, self._connected_runtime() as (_enody, runtime):
            host = runtime.host()
            version = host.version()
            return EnodyDeviceInfo(
                host_id=str(host.identifier()).strip().lower(),
                firmware_version=str(version) if version is not None else None,
                fixture_ids=tuple(
                    str(fixture.identifier()) for fixture in host.fixtures()
                ),
            )

    async def async_display_fixture(
        self,
        fixture_id: str,
        flux: float,
        *,
        color_temp_kelvin: int | None = None,
        xy_color: tuple[float, float] | None = None,
        transition: float = 0,
    ) -> None:
        """Set a fixture target, optionally with a device-side transition."""
        await self._hass.async_add_executor_job(
            self._display_fixture_sync,
            fixture_id,
            flux,
            color_temp_kelvin,
            xy_color,
            transition,
        )

    def _display_fixture_sync(
        self,
        fixture_id: str,
        flux: float,
        color_temp_kelvin: int | None,
        xy_color: tuple[float, float] | None,
        transition: float,
    ) -> None:
        """Send one command and wait for the device to finish."""
        with self._lock, self._connected_runtime() as (enody, runtime):
            fixtures = {
                str(fixture.identifier()): fixture
                for fixture in runtime.host().fixtures()
            }
            if fixture_id not in fixtures:
                raise EnodyCannotConnect(f"Fixture {fixture_id} is unavailable")

            fixture = fixtures[fixture_id]
            configuration = _configuration(enody, color_temp_kelvin, xy_color)
            target_flux = enody.Flux.relative(max(0.0, min(1.0, flux)))
            if transition == 0:
                fixture.display(configuration, target_flux)
            else:
                fixture.transition(
                    enody.Transition.linear(configuration, target_flux, transition)
                )

    @contextmanager
    def _connected_runtime(self) -> Iterator[tuple[Any, Any]]:
        """Reuse the device connection and discard it only on failure."""
        try:
            enody = _load_enody()
            if self._runtime is not None and not self._runtime.is_connected():
                self._disconnect_locked()
            if self._runtime is None:
                token = enody.Token.from_dict(self._token_data)
                self._runtime = enody.WifiConnection.runtime_from_endpoint(
                    token,
                    self._endpoint,
                )
                self._runtime.connect()
            yield enody, self._runtime
        except EnodyError:
            self._disconnect_locked()
            raise
        except Exception as err:
            self._disconnect_locked()
            raise EnodyCannotConnect(
                "Unable to communicate with the Enody device"
            ) from err

    async def async_disconnect(self) -> None:
        """Close the connection after any pending device work finishes."""
        await self._hass.async_add_executor_job(self._disconnect_sync)

    def _disconnect_sync(self) -> None:
        """Serialize cleanup with commands and metadata polling."""
        with self._lock:
            self._disconnect_locked()

    def _disconnect_locked(self) -> None:
        """Close and forget the runtime while the executor lock is held."""
        runtime, self._runtime = self._runtime, None
        if runtime is not None:
            try:
                runtime.disconnect()
            except Exception:
                LOGGER.debug("Failed to disconnect Enody runtime", exc_info=True)


def _configuration(
    enody: Any,
    color_temp_kelvin: int | None,
    xy_color: tuple[float, float] | None,
) -> Any:
    """Build an enody-py color configuration."""
    if xy_color is not None:
        return enody.Configuration.chromatic(float(xy_color[0]), float(xy_color[1]))
    if color_temp_kelvin is not None:
        return enody.Configuration.blackbody(float(color_temp_kelvin))
    return enody.Configuration.flux()
