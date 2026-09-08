# O-KAM HA Proxy for Home Assistant

> **Attribution:** O-KAM HA Proxy is a fork of the original O-KAM HA Native
> project by [@oleandor](https://github.com/oleandor), available at
> [oleandor/okam-ha-native](https://github.com/oleandor/okam-ha-native).
> Additional Home Assistant packaging, integration work, and subsequent
> modifications are maintained in this fork by
> [@dunlabx](https://github.com/dunlabx) at
> [dunlabx/okam-ha-proxy](https://github.com/dunlabx/okam-ha-proxy).

O-KAM Native Bridge connects selected O-KAM Pro cameras directly to Home Assistant on
64-bit ARM (`aarch64`) and x86-64 (`amd64`) systems. Live video, snapshots,
camera wake-up, and disconnects are handled by the Home Assistant host.

The project contains both required parts:

- **O-KAM Native Bridge app** — connects to the camera and provides the local
  authenticated media API.
- **O-KAM Native Bridge integration** — creates the Home Assistant camera
  entity, including live view and snapshots.

## Features

- Native operation on `aarch64` and `amd64` with no desktop or emulation layer
- Live H.264 video in Home Assistant
- Full-resolution JPEG snapshots
- Automatic wake-up for battery cameras
- Clear sleeping and waking-up images instead of a black preview
- One camera connection shared by simultaneous viewers
- Multiple selected cameras from one O-KAM account, each with an independent lifecycle
- Standard RTSP-over-TCP output for Frigate, go2rtc, VLC, and ffmpeg
- Automatic stream stop and clean disconnect after the last viewer leaves
- User-created API token protecting the local bridge
- No transcoding during live view
- No vendor identifiers, account tokens, or passwords exposed by the unauthenticated readiness API

## Requirements

- A Home Assistant system reporting the `aarch64` or `amd64` architecture
- An O-KAM Pro camera that works in the O-KAM mobile app
- An O-KAM account that can view the cameras you select; the account may own
  them or have them shared to it
- HACS for the easiest integration installation, or access to
  `/config/custom_components` for manual installation

The app is published as one multi-architecture image:

| Home Assistant architecture | Typical systems |
| --- | --- |
| `aarch64` | Raspberry Pi 3, 4, or 5 running a 64-bit OS; other 64-bit ARM hosts |
| `amd64` | Intel or AMD mini PCs, servers, and virtual machines |

Raspberry Pi 4 is physically validated. Raspberry Pi 3 is eligible when it is
running a 64-bit Home Assistant installation, but its lower performance has not
been measured. A 32-bit installation cannot install this app. Check the value
shown under **Settings → System → Repairs → System information → Architecture**
if you are unsure.

## Installation

### 1. Check the O-KAM account

Use the normal O-KAM account that can open the camera's live view. It may be
the camera owner account or an account to which the camera was shared. For an
account with more than one camera, all cameras are exposed by default. Use the
explicit `cameras` list to restrict the set or assign per-camera aliases.

Sign in with that account in the O-KAM mobile app once and confirm that live
view works. Keep its email address and password available for app
configuration.

### 2. Install the O-KAM HA Proxy app

1. In Home Assistant, open **Settings → Apps → App store**.
2. Open the app-store menu and select **Repositories**.
3. Add this repository:

   ```text
   https://github.com/dunlabx/okam-ha-proxy
   ```

4. Find and install **O-KAM HA Proxy**.
5. Open its **Configuration** tab and enter:

   | Option | What to enter |
   | --- | --- |
   | `account_username` | Email address of the O-KAM account |
   | `account_password` | Password of the O-KAM account |
   | `camera_password` | Normally leave blank; optional camera-level password override |
   | `api_token` | A new random secret of at least 16 characters that you choose |
   | `cameras` | Optional list of `{uid, alias}` mappings; empty exposes all account cameras |
   | `api_port` | HTTP API port, default `8099` |
   | `rtsp_port` | RTSP-over-TCP port, default `8100` |
   | `idle_timeout_seconds` | `120` seconds is recommended |

   Leave all four `run_*_test` options disabled during normal operation.

   A multi-camera configuration looks like:

   ```yaml
   cameras:
     - uid: CAMERA_UID_1
       alias: Front Door
     - uid: CAMERA_UID_2
       alias: Garage
   api_port: 8099
   rtsp_port: 8100
   ```

6. Save the configuration and start the app. It is configured to start
   automatically with Home Assistant.
7. Open the app log and confirm that it contains:

   ```text
   native_loader_ready=true
   account_enumerated=true raw_count=<n> parsed_count=<n> selected_count=<n>
   bridge_ready=true
   ```

The API token is a local secret created by you. It is not supplied by O-KAM and
must not be the O-KAM account password. You will enter the same token in the
integration.

The bridge normally obtains the camera-level password automatically from
O-KAM. If that value is omitted for your account, it tries the camera's common
initial value automatically. Only set `camera_password` when the app log reports
a camera-authentication failure and the camera uses a different local password.
This is not the O-KAM account password.

### 3. Install the Home Assistant integration

#### Recommended: HACS

1. Open **HACS** in Home Assistant.
2. Add `https://github.com/dunlabx/okam-ha-proxy` as a custom repository of
   type **Integration**.
3. Find and install **O-KAM Native Bridge**.
4. Restart Home Assistant when HACS asks you to.

#### Manual installation

1. Copy this repository's `custom_components/okam` directory to:

   ```text
   /config/custom_components/okam
   ```

2. Restart Home Assistant.

### 4. Add the integration

1. Open **Settings → Devices & services**.
2. Select **Add integration** and search for **O-KAM Native Bridge**.
3. Enter the following values:

   | Field | Value |
   | --- | --- |
   | Bridge URL | `http://HOME_ASSISTANT_LAN_IP:8099` |
   | API token | The same local token configured in the app |
   | Camera UID or alias | Either the canonical UID or configured alias; the UID is stored |
   | Idle timeout | `120` seconds is recommended |
   | Status refresh interval | `900` seconds is recommended |

Use the actual LAN address of Home Assistant, for example
`http://192.168.1.20:8099`. Do not use `localhost` between separate add-on
containers.

Each selected camera is also available as standard RTSP:

```text
rtsp://192.168.1.20:8100/CAMERA_UID
```

For go2rtc/Frigate, use an address reachable from the Frigate container:

```yaml
go2rtc:
  streams:
    front_cam:
      - rtsp://192.168.1.20:8100/CAMERA_UID
```

The RTSP server uses TCP interleaving and forwards native H.264 without
transcoding. Opening one URL wakes only that camera; the first consumer starts
its shared session and the final consumer starts the configured idle timeout.

Create one integration entry per selected camera when you want separate HA
entities. For legacy single-camera mode with alias `cabin`, Home Assistant
creates `camera.cabin`; multi-camera entries use their selected camera IDs.

## Daily use

Open the camera entity or add it to a dashboard using a camera card. A sleeping
battery camera shows **Camera sleeping** without waking it. Open live view to
wake the camera; **Camera waking up — please wait 20–30 seconds** is displayed
until video arrives.

Live view uses the camera's native H.264 stream. Multiple viewers share the
same connection. When the final viewer closes, the bridge waits for the
configured idle timeout, stops the stream, and disconnects from the camera.
The 120-second default lets brief page changes and reloads reuse the warm
connection. A live camera card requests a stream whenever it is displayed; the
sleeping image appears after all live viewers have closed and the idle timeout
has elapsed.

For an integration installed before version 1.1.1, open **Settings → Devices &
services → O-KAM Native Bridge → Configure** and change **Idle disconnect
delay** from `30` to `120`. The change is applied immediately.

## Updating

- Update the **app** from **Settings → Apps**.
- Update the **integration** from HACS.
- Restart Home Assistant after an integration update.

The two components use the same release version.

## Diagnostics

The app exposes two local status endpoints:

- `http://HOME_ASSISTANT_LAN_IP:8099/health` — service liveness
- `http://HOME_ASSISTANT_LAN_IP:8099/ready` — bridge and stream state

During normal idle operation, `/ready` should report:

```json
{
  "camera_ready": true,
  "phase": "bridge_ready",
  "stream_running": false,
  "stream_viewers": 0
}
```

While Home Assistant is displaying live video, it reports `phase: streaming`,
`stream_running: true`, and at least one viewer.

See [Troubleshooting](docs/troubleshooting.md) for common setup and connection
problems.

## Uninstalling

1. Remove **O-KAM Native Bridge** from **Settings → Devices & services**.
2. Stop and uninstall the **O-KAM Native Bridge** app.
3. Optionally remove the custom repositories from HACS and the app store.
4. Optionally remove the camera from the O-KAM account used by the bridge.

## Security

- Select only the intended camera UIDs in the `cameras` list.
- Keep the O-KAM password and local API token private.
- Do not expose or port-forward TCP ports 8099 or 8100 to the internet.
- Rotate the local API token if it is accidentally disclosed.
- Logs and issue reports must not contain credentials, tokens, or camera IDs.

See [SECURITY.md](SECURITY.md) for the complete security policy.

## Technical overview

The app enumerates cameras through the fixed official account service,
wakes it through the official low-power service, and opens a native P2P
transport. On `aarch64`, a minimal Bionic compatibility layer hosts the official
ARM64 transport library. On `amd64`, a small pure-Python client implements the
same encrypted camera protocol directly. H.264 is forwarded to Home Assistant
and RTSP without transcoding. FFmpeg is used for JPEG snapshots and the legacy
HTTP MPEG-TS compatibility endpoint with `-c:v copy`.
Required official artifacts are downloaded from their pinned source and
verified before use.

More detail is available in [Architecture](docs/architecture.md).

## Support and license

Open an issue at
[github.com/dunlabx/okam-ha-proxy/issues](https://github.com/dunlabx/okam-ha-proxy/issues)
with the app version, hardware model, Home Assistant version, app log, and
the redacted `/ready` response. Never include passwords, API tokens, or camera
identifiers.

The bridge source is MIT licensed. Vendor components remain subject to their
own terms and are not stored in this repository.
