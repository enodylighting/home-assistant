# Enody for Home Assistant

Local Home Assistant control for Enody EP01 lights.

Requires Home Assistant 2025.4.0 or newer.

The integration discovers EP01 devices over mDNS, pairs through the device's
physical approval flow, and exposes each fixture as a light with brightness,
color temperature, XY color control, and device-side transitions. Communication
stays on the local network.

## Install with HACS

Until Enody is included in the default HACS catalog, add this repository as a
custom repository:

1. In HACS, open the menu and choose **Custom repositories**.
2. Add `https://github.com/enodylighting/home-assistant` as an **Integration**.
3. Install **Enody** and restart Home Assistant.
4. Open **Settings > Devices & services > Add integration**, then select
   **Enody**.

## Install manually

Copy `custom_components/enody` into the `custom_components` directory in your
Home Assistant configuration, restart Home Assistant, and add **Enody** from
**Settings > Devices & services**.

## Pair a device

Home Assistant normally discovers an EP01 automatically. You can also enter its
local endpoint manually, such as `192.168.1.50:8788` or `ep01.local:8788`.

When pairing starts, follow the approval instruction shown by Home Assistant and
confirm it on the device. The integration stores the resulting local token in
the Home Assistant config entry.

If the device address or token changes, open the integration entry's menu and
choose **Reconfigure** to pair it again.

## Behavior

- Each Home Assistant action waits for one device command to finish.
- Device failures are returned to the caller instead of being hidden.
- Home Assistant marks the entity as assumed state and tracks the last
  successful target while the integration is running.
- The `transition` parameter on `light.turn_on` and `light.turn_off` specifies a
  linear fade duration in seconds. Each fade sends one SDK Transition command;
  the device performs the interpolation. Home Assistant waits for completion
  before updating its assumed state. Commands to the same device are serialized.
- Omitting `transition`, or setting it to `0`, sends one immediate display command.
- Device firmware must support the Transition API. Unsupported transitions are
  reported as service errors; the integration does not stream software updates.

For example, fade to warm white at half brightness over two seconds:

```yaml
action: light.turn_on
target:
  entity_id: light.enody_ep01
data:
  brightness: 128
  color_temp_kelvin: 2700
  transition: 2
```

## Troubleshooting

Confirm that Home Assistant and the EP01 are on the same local network and that
TCP port `8788` is reachable. To collect diagnostic logs, add:

```yaml
logger:
  default: info
  logs:
    custom_components.enody: debug
    enody: debug
```

Restart Home Assistant, reproduce the problem, and attach the relevant log lines
to an issue. Tokens should never be included in logs or issue reports.

## Development

Use Python 3.13 to test the minimum supported Home Assistant version, 2025.4.0:

```bash
uv sync --python 3.13 --extra dev
uv run pytest
uv run ruff check custom_components tests
uv run ruff format --check custom_components tests
```

The development dependencies use one test-fixture pin for HA 2025.4.0. CI also
tests HA 2026.9.4 in a separate Python 3.14 environment; its fixture pin is kept
in the workflow, not in the development dependencies. The integration does not
require HA 2026 or override HA's `aiohttp` version.

The runtime dependency is pinned to `enody==0.2.3` in both `pyproject.toml` and
the Home Assistant manifest. This release exposes the device-side Transition API
and releases the Python GIL during blocking device operations.

## Releases

When updating the SDK dependency, keep both exact dependency pins in sync and
verify that the release provides Python 3.13- and 3.14-compatible `musllinux`
wheels for Home Assistant OS and Container on both `amd64` and `arm64`.

Before publishing a GitHub release, update `version` in
`custom_components/enody/manifest.json` to match the release tag. HACS uses
GitHub releases to offer updates to customers.
