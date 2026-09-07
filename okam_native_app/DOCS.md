# O-KAM HA Proxy

O-KAM HA Proxy connects selected O-KAM Pro cameras directly to Home Assistant on
`aarch64` and `amd64` systems. It provides live H.264 video, JPEG snapshots,
automatic camera wake-up, shared viewing, and automatic idle disconnect.

## Before configuring the app

Use an O-KAM account that can view the cameras you want to expose. For more
than one account camera, `camera_uids` is required and must contain exact UIDs.

## Configuration

| Option | Description |
| --- | --- |
| `account_username` | Email address of the O-KAM account |
| `account_password` | Password of the O-KAM account |
| `camera_password` | Optional camera-level password override; normally leave blank |
| `api_token` | A random local secret of at least 16 characters chosen by you |
| `camera_id` | Home Assistant camera alias, for example `cabin` |
| `camera_uids` | Exact list of camera UIDs; required when the account has multiple cameras |
| `api_port` | HTTP API port; default `8099` |
| `rtsp_port` | RTSP-over-TCP port; default `8100` |
| `idle_timeout_seconds` | Delay before disconnecting after the final viewer closes; `120` is recommended |

The API token is not an O-KAM credential. Create a new random value and enter
the identical value when adding the Home Assistant integration.

After account enumeration, the token-protected `GET /api/devices` endpoint
lists the selected camera names and UIDs. Use that response to verify the
selection without exposing account or camera passwords.

The app normally obtains the camera-level credential automatically and has a
compatibility fallback for accounts that omit it. Set `camera_password` only
when camera authentication fails and the camera uses a changed local password.
It is not the O-KAM account password.

Leave `run_connect_test`, `run_auth_test`, `run_stream_test`, and
`run_snapshot_test` disabled during normal operation. They are bounded
diagnostic checks intended only for troubleshooting.

## Starting the app

Save the configuration and start the app. Automatic startup is enabled by
default. A successful startup log contains:

```text
native_loader_ready=true
account_enumerated=true device_count=<selected-count>
bridge_ready=true
```

The readiness page is available at:

```text
http://HOME_ASSISTANT_LAN_IP:8099/ready
```

It should report `camera_ready: true` and `phase: bridge_ready` while idle.

RTSP URLs use the selected UID:

```text
rtsp://BRIDGE_HOST:8100/CAMERA_UID
```

Frigate and go2rtc should use the bridge host address reachable from their
container and force RTSP TCP transport. The RTSP endpoint has no separate
authentication, so keep it on a trusted LAN and do not port-forward it.

## Home Assistant integration

Install **O-KAM Native Bridge** from HACS using
`https://github.com/dunlabx/okam-ha-proxy` as a custom integration repository.
Restart Home Assistant, then add the integration from **Settings → Devices &
services**.

Use:

- Bridge URL: `http://HOME_ASSISTANT_LAN_IP:8099`
- API token: the value configured above
- Camera ID: the configured alias, such as `cabin`
- Idle timeout: `120`

Do not use `localhost` for the bridge URL. With the `cabin` alias, a new
installation creates `camera.cabin` unless that entity ID is already occupied.

## Operation

A sleeping camera displays a sleeping image without waking it. Open live view
to wake the camera; a waking-up image remains visible until video arrives,
which commonly takes 20–30 seconds. Multiple viewers share one camera
connection. After the final viewer closes, the bridge stops and disconnects
after the configured idle timeout. The 120-second default allows brief page
changes and reloads to reuse the warm connection. Installations created before
version 1.1.1 retain their selected value; use the integration's **Configure**
action to change an older 30-second value to 120 seconds.

Do not expose TCP port 8099 to the internet. See the repository
[README](https://github.com/dunlabx/okam-ha-proxy#readme) and
[troubleshooting guide](https://github.com/dunlabx/okam-ha-proxy/blob/main/docs/troubleshooting.md)
for complete installation and support information.
