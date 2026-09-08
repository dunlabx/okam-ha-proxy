"""Credential-safe client for the official Eye4 account enumeration flow."""

from __future__ import annotations

import hashlib
import json
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Callable


ACCOUNT_ORIGIN = "https://api.eye4.cn"
MAX_RESPONSE_BYTES = 1024 * 1024
HTTP_TIMEOUT_SECONDS = 15.0
_MISSING = object()


class AccountError(RuntimeError):
    """A safe account-service failure that contains no request values."""


@dataclass(frozen=True, repr=False)
class AccountDevice:
    """An O-KAM camera record whose sensitive values are excluded from repr."""

    uid: str = field(repr=False)
    name: str
    device_password: str = field(repr=False)
    password_present: bool = field(default=False, repr=False, compare=False)


@dataclass(frozen=True)
class CameraSelection:
    """A selected account camera, alias, and optional local password override."""

    device: AccountDevice
    alias: str | None = None
    password: str | None = field(default=None, repr=False)
    auth_mode: str = "automatic"


def normalize_camera_uids(value: object) -> list[str] | None:
    """Normalize explicit camera selection without logging identifiers."""

    if value is None:
        return None
    if not isinstance(value, list):
        raise AccountError("camera_uids must be a list")
    result: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            raise AccountError("camera UID is invalid")
        uid = item.strip()
        if not uid:
            raise AccountError("camera UID cannot be empty")
        if uid in seen:
            raise AccountError("camera_uids contains duplicates")
        seen.add(uid)
        result.append(uid)
    if not result:
        raise AccountError("camera_uids cannot be empty")
    return result


def select_account_devices(
    devices: list[AccountDevice], camera_uids: list[str] | None = None
) -> list[AccountDevice]:
    """Select configured cameras, or all account cameras when unset."""

    if camera_uids is not None:
        camera_uids = normalize_camera_uids(camera_uids)
    if camera_uids is None:
        return list(devices)
    by_uid: dict[str, AccountDevice] = {}
    for device in devices:
        key = device.uid.casefold()
        if key in by_uid:
            raise AccountError("official account returned duplicate camera UIDs")
        by_uid[key] = device
    missing = [uid for uid in camera_uids if uid.casefold() not in by_uid]
    if missing:
        raise AccountError("one or more configured camera UIDs were not found in the account")
    return [by_uid[uid.casefold()] for uid in camera_uids]


def normalize_camera_configurations(
    value: object,
) -> list[tuple[str, str | None, str | None, str]] | None:
    """Parse the Supervisor-supported ``cameras`` list of small mappings."""

    if value is None:
        return None
    if not isinstance(value, list):
        raise AccountError("cameras must be a list")
    result: list[tuple[str, str | None, str | None, str]] = []
    seen_uids: set[str] = set()
    seen_aliases: set[str] = set()
    for item in value:
        if not isinstance(item, dict):
            raise AccountError("camera configuration must be a mapping")
        uid = item.get("uid")
        if not isinstance(uid, str) or not uid.strip():
            raise AccountError("camera UID is required")
        uid = uid.strip()
        key = uid.casefold()
        if key in seen_uids:
            raise AccountError("cameras contains duplicate UIDs")
        seen_uids.add(key)
        alias = item.get("alias")
        if alias is not None:
            if not isinstance(alias, str):
                raise AccountError("camera alias is invalid")
            alias = alias.strip() or None
            if alias is not None:
                alias_key = alias.casefold()
                if alias_key in seen_aliases:
                    raise AccountError("cameras contains duplicate aliases")
                seen_aliases.add(alias_key)
        password = item.get("password")
        if password is not None and not isinstance(password, str):
            raise AccountError("camera password is invalid")
        auth_mode = item.get("auth_mode")
        if auth_mode is None:
            auth_mode = "configured_password" if password else "automatic"
        if auth_mode not in {"automatic", "configured_password"}:
            raise AccountError("camera auth_mode is invalid")
        if auth_mode == "configured_password" and password is None:
            raise AccountError("configured_password mode requires a password")
        if auth_mode == "automatic":
            # A supplied password is intentionally ignored in automatic mode
            # rather than silently changing its authentication semantics.
            password = None
        result.append((uid, alias, password, auth_mode))
    uid_keys = {uid.casefold() for uid, _alias, _password, _mode in result}
    for uid, alias, _password, _mode in result:
        if alias is not None and alias.casefold() in uid_keys and alias.casefold() != uid.casefold():
            raise AccountError("camera alias collides with another camera UID")
    return result


def configured_camera_selections(
    devices: list[AccountDevice], options: dict[str, object]
) -> list[CameraSelection]:
    """Apply current and legacy options without silently dropping cameras.

    An absent or empty ``cameras`` list explicitly means auto-select every
    account camera. Legacy UID options are converted in memory so existing
    installations continue to work while the Supervisor UI shows ``cameras``.
    """

    current = normalize_camera_configurations(options.get("cameras"))
    if current is None:
        raw_uids = options.get("camera_uids")
        if raw_uids is None and "camera_uid" in options:
            raw_uids = [options.get("camera_uid")]
        legacy_uids = normalize_camera_uids(raw_uids)
        if legacy_uids is not None:
            legacy_alias = options.get("camera_id")
            alias = legacy_alias.strip() if isinstance(legacy_alias, str) else None
            current = [
                (uid, alias if len(legacy_uids) == 1 else None, None, "automatic")
                for uid in legacy_uids
            ]
    if not current:
        return [CameraSelection(device) for device in devices]

    by_uid: dict[str, AccountDevice] = {}
    for device in devices:
        key = device.uid.casefold()
        if key in by_uid:
            raise AccountError("official account returned duplicate camera UIDs")
        by_uid[key] = device
    selected: list[CameraSelection] = []
    for uid, alias, camera_password, auth_mode in current:
        device = by_uid.get(uid.casefold())
        if device is None:
            raise AccountError("one or more configured camera UIDs were not found in the account")
        selected.append(CameraSelection(device, alias, camera_password, auth_mode))
    return selected


OpenRequest = Callable[[urllib.request.Request, float], bytes]


def _open_request(request: urllib.request.Request, timeout: float) -> bytes:
    with urllib.request.urlopen(request, timeout=timeout) as response:  # noqa: S310
        if response.status != 200:
            raise AccountError("official account service rejected a request")
        payload = response.read(MAX_RESPONSE_BYTES + 1)
    if len(payload) > MAX_RESPONSE_BYTES:
        raise AccountError("official account service response was too large")
    return payload


class Eye4AccountClient:
    """Reproduce the three official WebViewer account requests over HTTPS."""

    def __init__(
        self,
        *,
        opener: OpenRequest = _open_request,
        debug_credentials: bool = False,
        logger: Callable[[str], None] = print,
    ) -> None:
        self._opener = opener
        self._debug_credentials = debug_credentials
        self._logger = logger
        self.last_raw_device_count = 0

    def _request_json(
        self,
        method: str,
        path: str,
        *,
        query: dict[str, str] | None = None,
        form: dict[str, str] | None = None,
    ) -> Any:
        if not path.startswith("/") or "//" in path or ".." in path:
            raise AccountError("invalid official account service path")
        url = ACCOUNT_ORIGIN + path
        if query:
            url += "?" + urllib.parse.urlencode(query)
        data = None
        if form is not None:
            data = urllib.parse.urlencode(form).encode("ascii")
        request = urllib.request.Request(
            url,
            data=data,
            method=method,
            headers={
                "Accept": "application/json",
                "client_version": "10.0.1",
                "Content-Type": "application/x-www-form-urlencoded",
                "User-Agent": "okam-ha-native/account-enumerator",
            },
        )
        try:
            payload = self._opener(request, HTTP_TIMEOUT_SECONDS)
            result = json.loads(payload.decode("utf-8"))
        except AccountError:
            raise
        except (OSError, UnicodeError, json.JSONDecodeError, urllib.error.URLError):
            raise AccountError("official account service request failed") from None
        return result

    def enumerate(self, username: str, password: str) -> list[AccountDevice]:
        """Authenticate and return visible devices without logging identifiers."""

        if not username or not password or len(username) > 320 or len(password) > 1024:
            raise AccountError("O-KAM account credentials are invalid")
        summary = self._request_json(
            "POST", "/user/summary", form={"name": username, "oemid": "VSTC"}
        )
        if not isinstance(summary, dict):
            raise AccountError("official account summary was invalid")
        user_id = summary.get("userid")
        if not isinstance(user_id, (str, int)) or not str(user_id).isdigit():
            raise AccountError("official account summary omitted the user identifier")

        password_digest = hashlib.md5(  # noqa: S324 - required by official protocol
            password.encode("utf-8"), usedforsecurity=False
        ).hexdigest()
        login = self._request_json(
            "GET",
            "/login/token",
            query={"userid": str(user_id), "password": password_digest, "type": "PC"},
        )
        if not isinstance(login, dict) or not isinstance(login.get("token"), str):
            raise AccountError("O-KAM account credentials were rejected")

        devices = self._request_json(
            "GET",
            "/PC/device/show",
            query={"userid": str(user_id), "pwd": password_digest},
        )
        if not isinstance(devices, list):
            raise AccountError("official account device list was invalid")
        self.last_raw_device_count = len(devices)
        result: list[AccountDevice] = []
        for item in devices:
            if not isinstance(item, dict):
                continue
            uid = item.get("uid")
            if not isinstance(uid, str) or not 4 <= len(uid) <= 256:
                continue
            name = item.get("nickname")
            if self._debug_credentials:
                raw_password = item.get("password") if "password" in item else _MISSING
                password_type = (
                    type(raw_password).__name__ if raw_password is not _MISSING else "<MISSING>"
                )
                password_repr = repr(raw_password) if raw_password is not _MISSING else "<MISSING>"
                self._logger(
                    "api_device_raw "
                    f"uid={uid} nickname={name!r} "
                    f"password_field_present={'password' in item} "
                    f"password_type={password_type} password_repr={password_repr}"
                )
            # The service returns the camera-local credential on the same
            # object as its UID.  Do not correlate a separate password array
            # by position: malformed/missing objects are simply ignored.
            device_password = item.get("password")
            result.append(
                AccountDevice(
                    uid=uid,
                    name=name if isinstance(name, str) and name else "O-KAM camera",
                    device_password=device_password if isinstance(device_password, str) else "",
                    password_present="password" in item,
                )
            )
        return result
