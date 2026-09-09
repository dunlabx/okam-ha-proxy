import hashlib
import json
from urllib.parse import parse_qs, urlsplit

import pytest

from okam_native.account import (
    AccountDevice,
    AccountError,
    configured_camera_selections,
    Eye4AccountClient,
    normalize_camera_uids,
    select_account_devices,
)


def test_official_account_enumeration_flow() -> None:
    requests = []

    def opener(request, timeout: float) -> bytes:
        requests.append((request, timeout))
        path = urlsplit(request.full_url).path
        if path == "/user/summary":
            return json.dumps({"userid": 123456789}).encode()
        if path == "/login/token":
            return json.dumps({"token": "opaque-token"}).encode()
        if path == "/PC/device/show":
            return json.dumps(
                [{"uid": "sensitive-device-id", "nickname": "Cabin", "password": "secret"}]
            ).encode()
        raise AssertionError(path)

    devices = Eye4AccountClient(opener=opener).enumerate("viewer@example.com", "password")

    assert len(devices) == 1
    assert devices[0].name == "Cabin"
    assert devices[0].password_present is True
    assert "sensitive-device-id" not in repr(devices[0])
    assert "secret" not in repr(devices[0])
    assert [urlsplit(item[0].full_url).path for item in requests] == [
        "/user/summary",
        "/login/token",
        "/PC/device/show",
    ]
    assert parse_qs(requests[0][0].data.decode()) == {
        "name": ["viewer@example.com"],
        "oemid": ["VSTC"],
    }
    login_query = parse_qs(urlsplit(requests[1][0].full_url).query)
    assert login_query == {
        "userid": ["123456789"],
        "password": [hashlib.md5(b"password", usedforsecurity=False).hexdigest()],
        "type": ["PC"],
    }
    device_query = parse_qs(urlsplit(requests[2][0].full_url).query)
    assert device_query["userid"] == ["123456789"]
    assert device_query["pwd"] == [login_query["password"][0]]
    assert requests[0][0].headers["Client_version"] == "10.0.1"


def test_account_errors_never_include_credentials() -> None:
    def opener(_request, _timeout: float) -> bytes:
        raise OSError("network error containing viewer@example.com and secret")

    with pytest.raises(AccountError) as caught:
        Eye4AccountClient(opener=opener).enumerate("viewer@example.com", "secret")
    assert "viewer@example.com" not in str(caught.value)
    assert "secret" not in str(caught.value)


def test_account_errors_include_safe_stage_without_credentials() -> None:
    def opener(request, _timeout: float) -> bytes:
        if urlsplit(request.full_url).path == "/user/summary":
            return b'{"userid":123}'
        if urlsplit(request.full_url).path == "/login/token":
            raise OSError("temporary vendor failure")
        raise AssertionError("unexpected request")

    with pytest.raises(AccountError, match=r"stage=login_token") as caught:
        Eye4AccountClient(opener=opener).enumerate("u@example.com", "secret")
    assert "secret" not in str(caught.value)


def test_camera_uid_selection_is_exact_and_bounded_to_requested_subset() -> None:
    devices = [
        AccountDevice("A", "Front", "pw-a"),
        AccountDevice("B", "Back", "pw-b"),
        AccountDevice("C", "Side", "pw-c"),
    ]
    assert [item.uid for item in select_account_devices(devices, [" C ", "A"])] == [
        "C",
        "A",
    ]


def test_camera_uid_selection_rejects_duplicates_empty_and_missing_values() -> None:
    devices = [AccountDevice("A", "Front", "pw-a")]
    with pytest.raises(AccountError):
        normalize_camera_uids(["A", " A "])
    with pytest.raises(AccountError):
        normalize_camera_uids([])
    with pytest.raises(AccountError):
        select_account_devices(devices, ["missing"])


def test_multi_camera_accounts_without_filter_select_all() -> None:
    devices = [AccountDevice("A", "Front", "pw-a"), AccountDevice("B", "Back", "pw-b")]
    assert [item.uid for item in select_account_devices(devices)] == ["A", "B"]


def test_account_api_returns_two_parsed_cameras() -> None:
    def opener(request, _timeout: float) -> bytes:
        path = urlsplit(request.full_url).path
        if path == "/user/summary":
            return b'{"userid":123}'
        if path == "/login/token":
            return b'{"token":"opaque"}'
        if path == "/PC/device/show":
            return json.dumps(
                [
                    {"uid": "CAMERA_FRONT", "nickname": "Front", "password": "a"},
                    {"uid": "CAMERA_BACK", "nickname": "Back", "password": "b"},
                ]
            ).encode()
        raise AssertionError(path)

    client = Eye4AccountClient(opener=opener)
    devices = client.enumerate("user@example.com", "secret")
    assert client.last_raw_device_count == 2
    assert [item.uid for item in devices] == ["CAMERA_FRONT", "CAMERA_BACK"]
    assert [item.password_present for item in devices] == [True, True]


def test_account_api_distinguishes_missing_and_empty_password_fields() -> None:
    def opener(request, _timeout: float) -> bytes:
        path = urlsplit(request.full_url).path
        if path == "/user/summary":
            return b'{"userid":123}'
        if path == "/login/token":
            return b'{"token":"opaque"}'
        if path == "/PC/device/show":
            return json.dumps([
                {"uid": "CAMERA_A", "nickname": "A"},
                {"uid": "CAMERA_B", "nickname": "B", "password": ""},
                "malformed",
            ]).encode()
        raise AssertionError(path)

    devices = Eye4AccountClient(opener=opener).enumerate("u@example.com", "p")
    assert [(item.uid, item.password_present, item.device_password) for item in devices] == [
        ("CAMERA_A", False, ""),
        ("CAMERA_B", True, ""),
    ]


def test_debug_account_log_distinguishes_missing_none_empty_and_space() -> None:
    logs: list[str] = []

    def opener(request, _timeout: float) -> bytes:
        path = urlsplit(request.full_url).path
        if path == "/user/summary":
            return b'{"userid":123}'
        if path == "/login/token":
            return b'{"token":"opaque"}'
        if path == "/PC/device/show":
            return json.dumps([
                {"uid": "CAMERA_MISSING", "nickname": "M"},
                {"uid": "CAMERA_NONE", "nickname": "N", "password": None},
                {"uid": "CAMERA_EMPTY", "nickname": "E", "password": ""},
                {"uid": "CAMERA_SPACE", "nickname": "S", "password": " "},
            ]).encode()
        raise AssertionError(path)

    Eye4AccountClient(opener=opener, debug_credentials=True, logger=logs.append).enumerate(
        "u@example.com", "p"
    )
    rendered = "\n".join(logs)
    assert "uid=CAMERA_MISSING" in rendered and "password=<MISSING>" in rendered
    assert "uid=CAMERA_NONE" in rendered and "password=None" in rendered
    assert "uid=CAMERA_EMPTY" in rendered and "password='' " in rendered
    assert "uid=CAMERA_SPACE" in rendered and "password=' '" in rendered
    assert "password_length" not in rendered


def test_per_camera_aliases_and_legacy_migration() -> None:
    devices = [AccountDevice("A", "Front", "pw-a"), AccountDevice("B", "Back", "pw-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [{"uid": "A", "alias": "Door"}, {"uid": "B"}]},
    )
    assert [(item.device.uid, item.alias, item.password) for item in selected] == [("A", "Door", None), ("B", None, None)]
    legacy = configured_camera_selections(
        devices, {"camera_uids": ["A", "B"], "camera_id": "legacy"}
    )
    assert [(item.device.uid, item.alias, item.password) for item in legacy] == [("A", None, None), ("B", None, None)]


def test_per_camera_password_override_is_optional_and_preserves_exact_secret() -> None:
    devices = [AccountDevice("A", "Front", "account-a"), AccountDevice("B", "Back", "account-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [{"uid": "A", "alias": "Door", "password": "manual-secret"}, {"uid": "B", "password": ""}]},
    )
    assert [(item.device.uid, item.alias, item.password) for item in selected] == [
        ("A", "Door", "manual-secret"),
        ("B", None, None),
    ]
    assert "manual-secret" not in repr(selected[0])


def test_camera_auth_method_defaults_and_explicit_empty_are_distinct() -> None:
    devices = [AccountDevice("A", "Front", "account-a"), AccountDevice("B", "Back", "account-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [
            {"uid": "A", "password": "manual-secret"},
            {"uid": "B", "password": "", "auth_method": "password"},
        ]},
    )
    assert [(item.auth_method, item.password) for item in selected] == [
        ("password", "manual-secret"),
        ("password", ""),
    ]


def test_automatic_auth_method_ignores_supplied_password() -> None:
    devices = [AccountDevice("A", "Front", "account-a")]
    selected = configured_camera_selections(
        devices, {"cameras": [{"uid": "A", "password": "ignored", "auth_method": "automatic"}]}
    )
    assert selected[0].auth_method == "automatic"
    assert selected[0].password is None


def test_camera_mode_defaults_to_battery_and_normalizes_ipv4() -> None:
    devices = [AccountDevice("A", "Front", "account-a"), AccountDevice("B", "Back", "account-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [
            {"uid": "A", "ip": "192.168.1.140"},
            {"uid": "B", "battery_camera": False},
        ]},
    )
    assert selected[0].battery_camera is True and selected[0].camera_ip == "192.168.1.140"
    assert selected[1].battery_camera is False and selected[1].camera_ip is None


@pytest.mark.parametrize("ip", ["", "999.1.1.1", "example.com", "192.168.1", "2001:db8::1"])
def test_camera_ip_rejects_invalid_values(ip: str) -> None:
    with pytest.raises(AccountError):
        configured_camera_selections(
            [AccountDevice("A", "Front", "pw")],
            {"cameras": [{"uid": "A", "ip": ip}]},
        )


def test_legacy_auth_mode_values_migrate_to_auth_method() -> None:
    devices = [AccountDevice("A", "Front", "account-a"), AccountDevice("B", "Back", "account-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [
            {"uid": "A", "auth_mode": "automatic", "password": "ignored"},
            {"uid": "B", "auth_mode": "configured_password", "password": "manual"},
        ]},
    )
    assert [item.auth_method for item in selected] == ["automatic", "password"]
    assert [item.password for item in selected] == [None, "manual"]


def test_missing_auth_method_migrates_nonempty_and_empty_passwords() -> None:
    devices = [AccountDevice("A", "Front", "account-a"), AccountDevice("B", "Back", "account-b")]
    selected = configured_camera_selections(
        devices,
        {"cameras": [{"uid": "A", "password": "manual"}, {"uid": "B", "password": ""}]},
    )
    assert [item.auth_method for item in selected] == ["password", "automatic"]
    assert [item.password for item in selected] == ["manual", None]


def test_camera_configuration_rejects_duplicate_uids_and_aliases() -> None:
    devices = [AccountDevice("A", "Front", "pw-a"), AccountDevice("B", "Back", "pw-b")]
    with pytest.raises(AccountError, match="duplicate UIDs"):
        configured_camera_selections(devices, {"cameras": [{"uid": "A"}, {"uid": "a"}]})
    with pytest.raises(AccountError, match="duplicate aliases"):
        configured_camera_selections(
            devices, {"cameras": [{"uid": "A", "alias": "same"}, {"uid": "B", "alias": "SAME"}]}
        )
