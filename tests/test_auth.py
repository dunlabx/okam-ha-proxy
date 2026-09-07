import pytest

from okam_native.auth import (
    ACCOUNT_SOURCE,
    CONFIGURED_SOURCE,
    FALLBACK_SOURCE,
    AuthenticationRejected,
    CameraAuthenticator,
    CredentialSourceCache,
    build_candidates,
)
from okam_native.p2p import AuthenticationResult, P2PError


def _result(*, authenticated: bool, login_result: int | None = 0) -> AuthenticationResult:
    return AuthenticationResult(
        connected=True,
        connect_state=3,
        login_sent=True,
        login_response_received=login_result is not None,
        authenticated=authenticated,
        login_command=0x6001,
        login_result=login_result,
        disconnected=True,
    )


def _manager(tmp_path, logs=None) -> CameraAuthenticator:
    return CameraAuthenticator(
        CredentialSourceCache(tmp_path / "camera_auth_cache.json"),
        logger=(logs.append if logs is not None else lambda _line: None),
    )


def test_account_password_succeeds_first(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    calls = []
    selected, result = _manager(tmp_path).authenticate(
        "UID_A",
        candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == ACCOUNT_SOURCE
    assert result.authenticated is True
    assert calls == [ACCOUNT_SOURCE]


def test_account_rejected_then_fallback_succeeds(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    calls = []

    def attempt(candidate):
        calls.append(candidate.source)
        success = candidate.source == FALLBACK_SOURCE
        return _result(authenticated=success, login_result=0 if success else -1)

    selected, _ = _manager(tmp_path).authenticate("UID_A", candidates, attempt)
    assert selected.source == FALLBACK_SOURCE
    assert calls == [ACCOUNT_SOURCE, FALLBACK_SOURCE]


def test_explicit_password_is_authoritative(tmp_path) -> None:
    candidates = build_candidates("account-secret", "configured-secret")
    calls = []
    selected, _ = _manager(tmp_path).authenticate(
        "UID_A",
        candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == CONFIGURED_SOURCE
    assert calls == [CONFIGURED_SOURCE]


def test_duplicate_candidate_values_are_attempted_once(tmp_path) -> None:
    candidates = build_candidates("same-secret", "same-secret")
    assert [item.source for item in candidates] == [CONFIGURED_SOURCE, FALLBACK_SOURCE]
    calls = []
    selected, _ = _manager(tmp_path).authenticate(
        "UID_A",
        candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == CONFIGURED_SOURCE
    assert calls == [CONFIGURED_SOURCE]


def test_all_candidates_rejected(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    with pytest.raises(AuthenticationRejected) as caught:
        _manager(tmp_path).authenticate(
            "UID_A",
            candidates,
            lambda _candidate: _result(authenticated=False, login_result=-1),
        )
    assert caught.value.results == (-1, -1)


def test_cached_candidate_is_tried_first(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    cache = CredentialSourceCache(tmp_path / "camera_auth_cache.json")
    cache.set("UID_A", FALLBACK_SOURCE)
    calls = []
    selected, _ = CameraAuthenticator(cache, logger=lambda _line: None).authenticate(
        "UID_A",
        candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == FALLBACK_SOURCE
    assert calls == [FALLBACK_SOURCE]


def test_cached_rejection_is_invalidated_and_replaced(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    cache = CredentialSourceCache(tmp_path / "camera_auth_cache.json")
    cache.set("UID_A", ACCOUNT_SOURCE)
    calls = []

    def attempt(candidate):
        calls.append(candidate.source)
        success = candidate.source == FALLBACK_SOURCE
        return _result(authenticated=success, login_result=0 if success else -1)

    selected, _ = CameraAuthenticator(cache, logger=lambda _line: None).authenticate(
        "UID_A", candidates, attempt
    )
    assert selected.source == FALLBACK_SOURCE
    assert calls == [ACCOUNT_SOURCE, FALLBACK_SOURCE]
    assert CredentialSourceCache(tmp_path / "camera_auth_cache.json").get("UID_A") == FALLBACK_SOURCE


def test_cache_survives_simulated_restart(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    first = CredentialSourceCache(path)
    assert first.set("UID_A", ACCOUNT_SOURCE) is True
    assert CredentialSourceCache(path).get("UID_A") == ACCOUNT_SOURCE


def test_malformed_cache_does_not_crash_startup(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    path.write_text("not-json", encoding="utf-8")
    assert CredentialSourceCache(path).get("UID_A") is None


def test_cache_contains_no_plaintext_credentials(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    cache.set("UID_A", ACCOUNT_SOURCE)
    contents = path.read_text(encoding="utf-8")
    assert "account-secret" not in contents
    assert ACCOUNT_SOURCE in contents


def test_two_uids_keep_independent_sources(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    manager = _manager(tmp_path)
    manager.authenticate("UID_A", candidates, lambda _candidate: _result(authenticated=True))
    manager.authenticate(
        "UID_B",
        candidates,
        lambda candidate: _result(
            authenticated=candidate.source == FALLBACK_SOURCE,
            login_result=0 if candidate.source == FALLBACK_SOURCE else -1,
        ),
    )
    cache = CredentialSourceCache(tmp_path / "camera_auth_cache.json")
    assert cache.get("UID_A") == ACCOUNT_SOURCE
    assert cache.get("UID_B") == FALLBACK_SOURCE


def test_transport_failure_is_not_reinterpreted_as_rejection(tmp_path) -> None:
    candidates = build_candidates("account-secret")

    def attempt(_candidate):
        raise P2PError("transport failed")

    with pytest.raises(P2PError):
        _manager(tmp_path).authenticate("UID_A", candidates, attempt)


def test_camera_b_remains_operational_when_camera_a_rejects(tmp_path) -> None:
    candidates = build_candidates("account-secret")
    manager = _manager(tmp_path)
    with pytest.raises(AuthenticationRejected):
        manager.authenticate(
            "UID_A",
            candidates,
            lambda _candidate: _result(authenticated=False, login_result=-1),
        )
    selected, _ = manager.authenticate(
        "UID_B", candidates, lambda _candidate: _result(authenticated=True)
    )
    assert selected.source == ACCOUNT_SOURCE


def test_logs_identify_uid_and_source_without_password(tmp_path) -> None:
    logs = []
    candidates = build_candidates("account-secret")
    _manager(tmp_path, logs).authenticate(
        "UID_A", candidates, lambda _candidate: _result(authenticated=True)
    )
    rendered = "\n".join(logs)
    assert "uid=UID_A" in rendered
    assert "candidate=account_device_password" in rendered
    assert "account-secret" not in rendered
