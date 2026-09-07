# Security policy

## Supported versions

Security fixes are provided for the latest published release.

## Reporting a vulnerability

Report vulnerabilities privately through
[GitHub Security Advisories](https://github.com/oleandor/okam-ha-native/security/advisories/new).
Do not open a public issue for an undisclosed vulnerability.

## Credential safety

- Select only the intended camera UIDs in `camera_uids`; additional account
  cameras are never initialized automatically.
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
