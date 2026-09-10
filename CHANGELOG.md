# Changelog

## 2.0.0-rc3

- Added native camera audio (PCMA/16000/1) to RTSP and RTSP backchannel talk.
- Physical camera validation is still required.

## 2.0.0-rc2

- Reconnects an affected RTSP client once when a battery-camera standby/live
  H.264 SPS/PPS generation changes, allowing browser recording playback to
  start a fresh media description without restarting the shared native session
  or transcoding the camera stream.
- Uses the supplied `battery_cam.png` artwork as the build-time 2304x1296
  H.264 standby frame and documents the exact source checksum.

## 2.0.0

- Keeps one native stream session per physical camera while allowing concurrent
  HTTP, RTSP, and Home Assistant consumers to attach without holding the session
  lock during wake or authentication.
- Dispatches receive-only ARP wake events asynchronously per camera, coalesces
  duplicates safely, and preserves a wake arriving while cleanup is finishing.
- Adds per-request HTTP timing, bounded media writes and Annex-B buffering, and
  generation-safe helper cleanup.
- Adds stable secondary RTSP alias routes while retaining canonical UID routes.
- Preserves battery standby/live transitions, ordinary-camera active RTSP, H.264
  passthrough, snapshot semantics, and multi-camera isolation.
- Refreshes installation, architecture, standby, ARP, RTSP, and troubleshooting
  documentation.

## 1.2.18

- Adds per-camera sleep-mode and local IPv4 configuration.
- Productionizes receive-only ARP wake routing for battery cameras and
  switches their existing standby RTSP session to the shared native stream
  without sending a second wake request.
- Normal cameras use active RTSP startup; existing H.264 passthrough and
  multi-camera session isolation are preserved.

## 1.2.17

- Restores the pre-login native timing path after diagnostic instrumentation.
- Prevents a reconnect from overlapping the previous helper's cleanup.
- Adds absolute timestamps and process boundaries to runtime diagnostics.
- Physical ARM64 Raspberry Pi verification on 2026-09-09: wake, native
  transport (`connect state = 3`), authentication (`login result = 0`), real
  H.264 SPS/PPS/IDR, Home Assistant camera-card video, and production RTSP all
  succeeded with the current camera credential.
- The 16-character native camera credential changes after a reboot or power
  cycle. A stale value can connect transport but is rejected at login and
  produces no H.264; both the historical legacy and current authenticated
  helper paths succeeded with the newly captured current value, so helper
  migration is not the root cause.
- Next investigation: capture fresh official iPhone-to-camera LAN traffic after
  reboot/power-cycle to establish how the current credential is obtained before
  implementing any retrieval mechanism.

## 1.2.16

- Adds credential-free ARM64/amd64 live-stream boundary diagnostics for the
  next real Home Assistant test.

## 1.2.15

- Fixes HTTP API authentication when the production handler receives the
  add-on's multi-camera bridge registry.

## 1.2.14

- Emits full safe diagnostics for unexpected bridge server exceptions.

## 1.2.13

- Prevents persisted diagnostic switches from authenticating cameras during startup.
- Adds runtime build fingerprints, import-path diagnostics, and reconnect state markers.

## 1.2.12

- Removes the obsolete global camera password option.
- Adds safe bridge request diagnostics and per-camera native session lifecycle markers.

## 1.2.11

- Renders camera authentication method as a Supervisor add-on dropdown.
- Keeps camera credentials in the add-on and reduces HACS setup to bridge
  connection and UID-or-alias selection.

## 1.2.10

- Keeps RTSP consumers on a passive standby H.264 frame until an active live
  viewer starts the shared native camera session.
- Keeps the shared native session available to passive RTSP consumers.

## 1.2.9

- Replaces the user-facing camera `auth_mode` setting with `auth_method:
  automatic|password`, while migrating legacy values safely.
- Makes the temporary `debug_credentials` output directly show raw and parsed
  API camera passwords, every candidate, and the final native login value.
- Adds safe AccountError stage diagnostics for vendor API failures.

## 1.2.8

- Adds temporary `debug_credentials` diagnostics for tracing camera/device
  password bytes from `/PC/device/show` through IPC and native login. Account
  passwords, API tokens, and session secrets remain excluded.
- Adds explicit per-camera `auth_mode`, including a bounded configured empty
  password and backward-compatible automatic-mode resolution.

## 1.2.7

- Adds an optional per-camera password override. Non-empty values use strict
  configured-password authentication for that UID without fallback or caching.
- Keeps missing and empty per-camera passwords in automatic authentication mode.

## 1.2.6

- Preserves an explicitly empty camera password through Python, amd64, and
  arm64 native-login framing so the camera can accept it or explicitly reject
  it before bounded fallback continues.

## 1.2.5

- Treats an explicitly empty account-device password as the bounded
  `empty_password` authentication candidate before the fixed `888888`
  fallback, with per-camera cache and rejection invalidation.

## 1.2.4

- Propagates the per-camera `password` returned by `/PC/device/show` through
  account parsing and production stream authentication.
- Boundedly cross-tries distinct known account-device passwords per UID and
  caches the successful source UID independently for each camera.
- Keeps transport failures separate from explicit authentication rejection and
  adds safe account/candidate diagnostics without logging secrets.
- Accepts a HACS camera UID or configured alias while storing the canonical UID,
  and exposes deterministic UID/alias/name data from `/api/devices`.
- Adds production-path multi-camera credential and failure-isolation coverage.
- Documents the upstream O-KAM project attribution and fork maintenance.

## Next

- Adds explicit per-camera UID and alias mappings while selecting all account cameras by default.
- Runs independent on-demand camera sessions behind one shared registry.
- Adds configurable API and RTSP ports and a standard H.264 RTSP-over-TCP server
  at `rtsp://HOST:8100/<camera_uid>` without transcoding.
- Keeps HTTP/HA viewers and RTSP consumers on the same per-camera fan-out and
  idle lifecycle.

## 1.2.3

- Uses an explicit per-camera UID and alias configuration and exposes all
  enumerated cameras when the list is empty.
- Runs bounded authentication fallback in the same native stream session and
  records only symbolic credential sources in the cache.

## 1.2.2

- Tries only known per-camera login credentials with bounded native
  authentication cycles and remembers the successful credential source in
  `/data` without persisting passwords.

## 1.2.1

- Shows the release notes in the app update dialog. The supervisor reads a
  changelog from the app directory, so updating previously reported that none
  was found.

## 1.2.0

- Adds native `amd64` support alongside the existing `aarch64` runtime.
- Implements the camera's encrypted CS2/PPPP UDP transport and command framing
  directly for x86-64 Home Assistant hosts.
- Publishes one prebuilt multi-architecture image with architecture-correct
  Home Assistant labels.
- Keeps the account, wake, bridge API, streaming, snapshot, and lifecycle logic
  shared across both architectures.
- Stops reporting a disconnected client as a crash. The supervisor polls the
  bridge and closes connections abruptly, which filled the app log with
  ConnectionResetError tracebacks for normal behaviour (issue #5).
- Makes the camera password option genuinely optional. The bridge already fell
  back to the enumerated credential when it was empty, but the add-on schema
  still required a value, and its null default kept the key present and
  invalid, so the options could not be saved without one and no placeholder
  was safe (issue #5).
- Handles accounts that omit the camera-level credential, with an optional
  local camera-password override for changed device passwords.
- Uses the configured camera alias as the exact suggested entity ID on a fresh
  Home Assistant installation, for example `camera.cabin`.
- Reaches the camera's media over its relay session. A direct UDP punch yields
  a session that authenticates and acknowledges live-start but never delivers
  video, and it previously hid the relay path by winning the race.
- Addresses the relay request to the directory servers rather than to the relay
  itself, which is what makes the relay rendezvous complete.
- Notifies every endpoint a session touched when closing, and acknowledges a
  declined direct readiness request, so a camera does not hold a stale binding
  that blocks the next connection.
- Accepts relay media when NAT renumbers the peer's source port, which
  previously discarded the entire media channel while control traffic
  continued to work.
- Serves the live stream as MPEG-TS with timestamps, so Home Assistant's
  stream worker can build HLS. A raw elementary stream was rejected with
  "No dts in consecutive packets", so live view never played while snapshots
  kept working.
- Starts a newly opened live view on a decodable boundary by caching the
  stream's parameter sets and newest keyframe. Opening a second view of an
  already-streaming camera no longer waits for the camera's next keyframe.
- Adds protocol-vector, wire-format, reliable-channel, H.264, helper-contract,
  metadata, architecture-selection, relay-negotiation, session-teardown, and
  live-view priming tests.

## 1.1.1

- Refreshes the Home Assistant camera image as soon as native H.264 media
  becomes ready, preventing the waking-up image from remaining on screen.
- Reuses the last successful snapshot if a later still-image request briefly
  fails.
- Increases the default idle disconnect delay from 30 seconds to 120 seconds so
  normal page changes can reuse the warm camera connection.
- Reports the effective runtime idle timeout in camera and readiness status.

## 1.1.0

- Accepts the normal camera-owner account as well as an account with a shared
  camera, provided the account exposes exactly one camera.
- Makes the app available to all 64-bit ARM (`aarch64`) Home Assistant systems.
- Shows informative sleeping and waking-up images while live video is inactive
  or the battery camera is waking.
- Reports the distinct `camera_waking` phase and native-media readiness.
- Clarifies hardware compatibility and stream wake behavior throughout the
  installation documentation.

## 1.0.0

- Provides native ARM64 live video and snapshots for O-KAM Pro cameras.
- Supports 64-bit Home Assistant OS on Raspberry Pi 4 and Raspberry Pi 5.
- Adds automatic camera wake-up and clean idle disconnect.
- Shares one native stream between simultaneous Home Assistant viewers.
- Includes the HACS-compatible O-KAM Native Bridge integration.
- Adds authenticated local configuration, status, snapshot, and stream APIs.
- Enables automatic app startup and production release metadata.
- Documents complete installation, operation, updating, diagnostics,
  troubleshooting, security, and removal procedures.
