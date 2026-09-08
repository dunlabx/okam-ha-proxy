# Security policy

## Supported versions

Security fixes are provided for the latest published release.

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/dunlabx/okam-ha-proxy/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability.

## Credential safety

- Select only the intended camera UIDs in the `cameras` list; additional account
  cameras are never initialized when an explicit non-empty list is configured.
- A camera mapping may include an optional `password` and `auth_mode`. With the
  mode omitted, non-empty values select strict per-camera authentication and
  absent or empty values select automatic authentication. Automatic mode may
  intentionally try the symbolic empty password returned by the camera API. An
  explicit `auth_mode: configured_password` permits `password: ""` and uses
  exactly that value without fallback or cache. These values are never persisted
  or exposed during normal operation.
- `debug_credentials` is a temporary diagnosis switch. When enabled, exact
  camera/device passwords are printed at controlled diagnostic boundaries;
  startup warns that logs contain plaintext credentials. It never prints the
  O-KAM account password, API token, bearer token, cookies, or session secrets.
  Disable it immediately after a controlled test and protect any captured log.
- Use a unique random local API token of at least 16 characters.
- Never post account names, passwords, API tokens, camera identifiers, packet
  captures, media, or unredacted logs.
- Rotate a password or token immediately if it may have been disclosed.
- Supply secrets only through Home Assistant app options.

## Network safety

- Keep TCP ports 8099 (API) and 8100 (RTSP) on the trusted local network.
- Do not port-forward either port or expose them through a public reverse proxy.
- RTSP currently has no separate authentication. Knowledge of a UID is not
  authorization, so use it only on a trusted Home Assistant/LAN network.
- Camera API routes require bearer authentication. The liveness and readiness
  routes intentionally expose only non-secret operational state.

## Supply chain

Vendor artifacts are downloaded from a fixed official source and verified
against pinned checksums before use. They are not committed to this repository
or embedded in the published source archive.
