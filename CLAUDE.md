# CLAUDE.md

Guidance for Claude Code when working in this repository.

## What this is

O-KAM Native Bridge connects an O-KAM Pro camera directly to Home Assistant on
`aarch64` and `amd64` hosts. It has two shipped components that share a release
version:

- **Bridge app** (`okam_native_app/`, `src/okam_native/`) — enumerates the
  camera via the official account service, wakes it, opens a native P2P
  transport, and serves an authenticated local media API (H.264 passthrough,
  JPEG snapshots via FFmpeg only on demand).
- **HA integration** (`custom_components/okam/`) — creates the `camera.*`
  entity (live view + snapshots) that talks to the bridge over HTTP.

## Current architecture and release rules

Read `docs/amd64-development-handoff.md` before touching transport code. Key
points:

- Goal: one multi-arch image for both 64-bit arches.
  - `aarch64`: keep the proven official ARM64 transport lib behind the
    Bionic/libhybris compat layer (`native/hybris_connect/`, `native/android_compat/`).
  - `amd64`: pure-Python CS2/PPPP encrypted-UDP client — **no** emulation, Wine,
    Android, or GUI runtime.
- amd64 status: enumeration + wake work; direct encrypted UDP session
  establishes; reliable channel writes are acked. **Unresolved:** after
  transport, around auth / session-readiness / live-start sequencing/timing —
  channel 1 does not deliver video frames. Evidence does **not** point to a
  missing TCP relay.
- Camera auth/live-start results vary with camera state and rapid reconnects.
  A rejection does **not** prove the command bytes are wrong — test with a rested
  camera, one session at a time.
- 11 physical release gates (see handoff §"Required release gates") must pass
  before `1.2.0` ships. These require a real camera and cannot be verified here.

## Camera identity and authentication

- Canonical camera identity is the account UID. An alias is user-friendly
  metadata only; HACS may accept either UID or alias but stores UID.
- The add-on accepts dynamic `cameras` entries with required `uid` and optional
  alias. Empty means all account cameras.
- Account device passwords are resolved independently per UID. Known passwords
  from other devices in the same authenticated account may be boundedly
  cross-tried when the association is unreliable.
- Advance to another candidate only for an explicit login rejection. Transport
  failures and missing login responses are not credential failures.
- Successful credential sources are cached per UID (including source UID for a
  cross-camera password); no plaintext password is persisted or logged.
- There is one authoritative native authentication path. Do not restore a
  legacy auth probe or authenticate in one process before starting another.

### Working rule for protocol changes

Change one behavior at a time and add a deterministic unit test for every
protocol correction. The amd64 protocol lives in `src/okam_native/cs2.py`
(directory lookup, UDP punching, relay negotiation, packet encryption, reliable
channels, command framing, auth, H.264 parsing) with the helper contract in
`src/okam_native/amd64_helper.py` and entry point `native/amd64_connect/okam-amd64-connect`.

## Security / privacy (hard rules)

Never add or commit credentials, camera identifiers, service parameters, IP
addresses, tokens, endpoints, or captured camera payloads — not in code, tests,
docs, logs, or commit messages. Protocol notes may record only sanitized
structure: packet type, channel, sequence, payload length, command ID, response
result, timing, state transitions. `src/okam_native/redaction.py` exists for
this; see `SECURITY.md`.

## Environment & commands

No system Python on this machine — use `uv` with the repo `.venv`
(Python 3.11, `requires-python >=3.11`).

Setup (already done in this checkout; recreate with):
```bash
uv venv --python 3.11
uv pip install -e '.[test]'
```

Run the test suite (expect 59 passing):
```bash
.venv/Scripts/python.exe -m pytest -q
```

CS2-focused subset (expect 19 passing):
```bash
.venv/Scripts/python.exe -m pytest tests/test_cs2.py -q
```

Optional extras (`pyproject.toml`): `trace` (frida), `inspect`/`test`
(pyelftools). Console script: `okam-acceptance` → `okam_native.acceptance:main`.

## Layout

- `src/okam_native/` — bridge core: `account.py`, `wakeup.py`, `p2p.py`,
  `session.py`, `bridge.py`, `cs2.py`, `amd64_helper.py`, `redaction.py`,
  `acceptance.py`.
- `custom_components/okam/` — HA integration (`camera.py`, `config_flow.py`,
  `coordinator.py`, `api.py`, …).
- `okam_native_app/` — HA add-on packaging: `Dockerfile` (arch-specific final
  stage), `config.yaml`, `app_entrypoint.py`.
- `native/` — C helpers/probes per arch (arm64 hybris, amd64 connect, probes).
- `tools/` — tracing/inspection utilities (frida scripts, SDK fetch/inspect).
- `tests/` — pytest suite. `.github/workflows/` — multi-arch build/publish.

## Before proposing a release

Run source-level gates before any image build: compileall, YAML/JSON parsing,
the complete unit and production integration suites, add-on schema/HACS checks,
RTSP regressions, and `git diff --check`. Build architecture artifacts once,
smoke-test those exact artifacts, then promote the same artifacts to GHCR.

The GHCR version and `latest` tags must carry both `linux/amd64` and
`linux/arm64` manifests, the aarch64 Raspberry Pi 4 regression must still pass,
and the full unit suite + CI must be green.
