# O-KAM HA Proxy

Connect selected O-KAM Pro cameras directly to Home Assistant on `aarch64` and
`amd64` systems. The app provides native live video, full-resolution snapshots,
automatic wake-up, shared viewing, and idle disconnect.

The matching Home Assistant integration and per-camera RTSP server are
included in the same repository.
See the **Documentation** tab for the complete setup guide.

## Camera modes and wake flow

`battery_camera: true` means battery/sleep mode. Passive RTSP clients remain
connected to synthetic H.264 **Standby** (режим ожидания) and do not explicitly
wake the camera. A physical PIR wake causes an ARP request; the configured
`ip` maps that sender to the canonical UID and the existing session switches to
real H.264 without a second wake request. When the camera sleeps again, the
same RTSP connection returns to standby.

`battery_camera: false` is ordinary camera mode. RTSP PLAY is an active
consumer and holds the real native stream while needed. The LAN `ip` is not
needed for ordinary video; reserve it in DHCP only when using battery-camera
ARP activation.

The canonical route is `rtsp://BRIDGE_HOST:8100/CAMERA_UID`. A configured alias
is a convenience route to the same CameraBridge and NativeStreamSession:
`rtsp://BRIDGE_HOST:8100/ALIAS`. Aliases are trimmed, NFC-normalized, and
case-insensitive; duplicate or UID-colliding aliases are rejected.
