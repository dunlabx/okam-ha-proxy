# Architecture

O-KAM Native Bridge is composed of a Home Assistant app and a custom
integration. Both are distributed from this repository, support `aarch64` and
`amd64`, and use matching release versions.

## Data flow

```text
Home Assistant camera entities
        │
        │ authenticated local HTTP
        ▼
O-KAM Native Bridge app
        │
        ├── account enumeration and low-power wake
        │
        └── per-camera registry
              ├── NativeStreamSession A ── O-KAM camera A
              ├── NativeStreamSession B ── O-KAM camera B
              └── shared RTSP-over-TCP fan-out
```

The integration polls a lightweight status endpoint. While the camera is idle
or waking, it supplies a generated state image without opening a camera
connection. Opening live view asks the app for a live source and wakes the
camera. Once media is flowing, still-image requests attach to the existing
session and produce a real snapshot.

The authenticated HTTP API accepts either the legacy local alias or the
selected UID in `/api/cameras/<identifier>/...`. The RTSP listener uses the
canonical UID path and an optional alias: `rtsp://HOST:8100/<camera_uid>` or
`rtsp://HOST:8100/<alias>`. Both routes select the same per-camera session.

## Camera lifecycle

1. Account enumeration returns all devices, then the optional `cameras` list selects an exact subset and assigns aliases.
2. The first viewer of one selected camera acquires a stream subscription.
3. The app requests a low-power wake for an active consumer and starts only
   that camera's native session. Passive battery RTSP remains on standby.
4. The entity reports `waking` until the first H.264 bytes arrive.
5. Annex-B H.264 frames are distributed to HTTP and RTSP viewers.
6. A snapshot request attaches to the existing session and decodes one frame to
   JPEG in memory.
7. When the final subscription closes, an idle timer starts for that camera.
8. At the end of the idle timeout, the app sends the camera's stream-stop
  request and disconnects the P2P client cleanly.

ARP wake dispatch is receive-only. A bounded queue and one worker per configured
camera keep the raw packet reader independent from slow native startup. While a
camera is starting, connected, or streaming, duplicate ARPs are coalesced. An
ARP observed during STOPPING is retained once and starts only after cleanup.

Queue sizes and request bodies are bounded. A slow viewer drops older queued
chunks instead of allowing unbounded memory growth.

## Native runtime

The container selects a transport at build time for the Home Assistant host:

| Architecture | Native transport |
| --- | --- |
| `aarch64` | The official Android ARM64 camera library with a small, checksum-pinned Bionic and `libhybris` compatibility layer |
| `amd64` | A pure-Python implementation of the camera's encrypted CS2/PPPP UDP transport and command protocol |

Both transports implement the same credential-safe helper contract and feed
the same session, API, snapshot, and lifecycle code. Neither image contains a
desktop environment or a general-purpose emulation runtime.

Live video is forwarded without transcoding. The bundled minimal FFmpeg build
contains only the H.264 decoder, MJPEG encoder, pipe protocols, image-pipe
muxer, and required filters used to create snapshots.

## Network boundaries

- Account enumeration uses the fixed official HTTPS account origin.
- Camera wake and P2P traffic are outbound from the app.
- TCP port 8099 exposes the bridge API and TCP port 8100 exposes RTSP on the
  local Home Assistant host (both are configurable for standalone deployments).
- Camera API routes require the user-created bearer token.
- The raw stream uses a separate random token generated at each app start.
- `/health` and `/ready` are intentionally unauthenticated and contain no
  credentials, vendor camera identifiers, or account tokens. RTSP is intended
  for a trusted LAN and currently has no separate authentication.

## Secret handling

O-KAM credentials are read from Home Assistant app options. Camera identifiers,
service parameters, and device credentials are passed to the native helper over
length-prefixed standard input rather than process arguments. Secret-bearing
objects exclude values from their representations, and user-facing errors are
sanitized.

## Distribution

GitHub Actions builds `linux/arm64` and `linux/amd64` images, then publishes one
multi-architecture version tag and `latest` tag to GitHub Container Registry.
Home Assistant selects and downloads the matching prebuilt image, so the host
does not compile the native runtime.

The custom integration lives at `custom_components/okam`, which permits HACS or
manual installation from the same release.
