import pytest
from okam_native.account import AccountDevice

from okam_native.auth import (
    ACCOUNT_SOURCE,
    CONFIGURED_SOURCE,
    EMPTY_PASSWORD_SOURCE,
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


def test_present_empty_account_password_generates_symbolic_candidate() -> None:
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    assert [(item.source, item.password) for item in candidates] == [
        (EMPTY_PASSWORD_SOURCE, ""),
        (FALLBACK_SOURCE, "888888"),
    ]


def test_empty_password_succeeds(tmp_path) -> None:
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    calls = []
    selected, result = _manager(tmp_path).authenticate(
        "UID_A",
        candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == EMPTY_PASSWORD_SOURCE
    assert result.authenticated is True
    assert calls == [EMPTY_PASSWORD_SOURCE]


def test_empty_password_rejection_tries_fallback(tmp_path) -> None:
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    calls = []

    def attempt(candidate):
        calls.append(candidate.source)
        success = candidate.source == FALLBACK_SOURCE
        return _result(authenticated=success, login_result=0 if success else -1)

    selected, _ = _manager(tmp_path).authenticate("UID_A", candidates, attempt)
    assert selected.source == FALLBACK_SOURCE
    assert calls == [EMPTY_PASSWORD_SOURCE, FALLBACK_SOURCE]


def test_empty_password_logging_is_symbolic_and_bounded(tmp_path) -> None:
    logs = []
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    _manager(tmp_path, logs).authenticate(
        "UID_A", candidates, lambda _candidate: _result(authenticated=True)
    )
    rendered = "\n".join(logs)
    assert "camera_auth_candidates uid=UID_A auth_mode=automatic candidate_count=2 sources=empty_password,fallback_888888" in rendered
    assert "candidate=empty_password" in rendered
    assert "password=" not in rendered


def test_empty_password_success_is_cached_per_uid(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    manager = CameraAuthenticator(CredentialSourceCache(path), logger=lambda _line: None)
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    selected, _ = manager.authenticate("UID_A", candidates, lambda _candidate: _result(authenticated=True))
    assert selected.source == EMPTY_PASSWORD_SOURCE
    assert CredentialSourceCache(path).get("UID_A") == EMPTY_PASSWORD_SOURCE


def test_cached_empty_password_is_tried_first_on_next_run(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    cache.set("UID_A", EMPTY_PASSWORD_SOURCE)
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "", True),
        account_devices=[AccountDevice("UID_B", "Back", "other", True)],
    )
    calls = []
    selected, _ = CameraAuthenticator(cache, logger=lambda _line: None).authenticate(
        "UID_A", candidates,
        lambda candidate: (calls.append(candidate.source), _result(authenticated=True))[1],
    )
    assert selected.source == EMPTY_PASSWORD_SOURCE
    assert calls == [EMPTY_PASSWORD_SOURCE]


def test_cached_empty_password_rejection_invalidates_uid_and_continues(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    cache.set("UID_A", EMPTY_PASSWORD_SOURCE)
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    calls = []

    def attempt(candidate):
        calls.append(candidate.source)
        success = candidate.source == FALLBACK_SOURCE
        return _result(authenticated=success, login_result=0 if success else -1)

    selected, _ = CameraAuthenticator(cache, logger=lambda _line: None).authenticate(
        "UID_A", candidates, attempt
    )
    assert selected.source == FALLBACK_SOURCE
    assert calls == [EMPTY_PASSWORD_SOURCE, FALLBACK_SOURCE]
    assert CredentialSourceCache(path).get("UID_A") == FALLBACK_SOURCE


def test_cached_empty_password_rejection_does_not_affect_another_uid(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    cache.set("UID_A", EMPTY_PASSWORD_SOURCE)
    cache.set("UID_B", ACCOUNT_SOURCE)
    manager = CameraAuthenticator(cache, logger=lambda _line: None)
    candidates_a = build_candidates(AccountDevice("UID_A", "A", "", True))
    candidates_b = build_candidates(AccountDevice("UID_B", "B", "pw", True))

    def reject_empty(candidate):
        return _result(authenticated=False, login_result=-1)

    with pytest.raises(AuthenticationRejected):
        manager.authenticate("UID_A", candidates_a, reject_empty)
    assert cache.get("UID_A") is None
    assert cache.get("UID_B") == ACCOUNT_SOURCE


def test_transport_failure_on_empty_password_does_not_try_fallback(tmp_path) -> None:
    candidates = build_candidates(AccountDevice("UID_A", "Front", "", True))
    calls = []

    def attempt(candidate):
        calls.append(candidate.source)
        raise P2PError("transport failed")

    with pytest.raises(P2PError):
        _manager(tmp_path).authenticate("UID_A", candidates, attempt)
    assert calls == [EMPTY_PASSWORD_SOURCE]


def test_associated_nonempty_password_precedes_empty_password() -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "A", "associated", True),
        account_devices=[AccountDevice("UID_B", "B", "other", True)],
    )
    assert [item.source for item in candidates] == [
        ACCOUNT_SOURCE,
        ACCOUNT_SOURCE,
        FALLBACK_SOURCE,
    ]


def test_cross_camera_nonempty_password_precedes_empty_password() -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "A", "", True),
        account_devices=[AccountDevice("UID_B", "B", "other", True)],
    )
    assert [(item.source, item.source_uid) for item in candidates] == [
        (ACCOUNT_SOURCE, "UID_B"),
        (EMPTY_PASSWORD_SOURCE, None),
        (FALLBACK_SOURCE, None),
    ]


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


def test_explicit_configured_empty_password_is_single_candidate(tmp_path) -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret"),
        "",
        strict_configured=True,
    )
    assert [(item.source, item.password) for item in candidates] == [
        (CONFIGURED_SOURCE, "")
    ]
    calls = []
    selected, _ = _manager(tmp_path).authenticate(
        "UID_A", candidates,
        lambda candidate: (calls.append(candidate.source) or _result(authenticated=True)),
    )
    assert selected.source == CONFIGURED_SOURCE
    assert calls == [CONFIGURED_SOURCE]
    assert calls == [CONFIGURED_SOURCE]


def test_strict_configured_password_creates_one_candidate() -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        account_devices=[AccountDevice("UID_B", "Back", "other-secret", True)],
        strict_configured=True,
    )
    assert [(item.source, item.password, item.source_uid) for item in candidates] == [
        (CONFIGURED_SOURCE, "manual-secret", None)
    ]


def test_strict_configured_password_rejection_has_no_fallback(tmp_path) -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        strict_configured=True,
    )
    calls = []
    with pytest.raises(AuthenticationRejected):
        _manager(tmp_path).authenticate(
            "UID_A",
            candidates,
            lambda candidate: (calls.append(candidate.source) or _result(authenticated=False, login_result=-1)),
        )
    assert calls == [CONFIGURED_SOURCE]


def test_strict_configured_password_logging_is_symbolic(tmp_path) -> None:
    logs = []
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        strict_configured=True,
    )
    _manager(tmp_path, logs).authenticate(
        "UID_A", candidates, lambda _candidate: _result(authenticated=True)
    )
    rendered = "\n".join(logs)
    assert "auth_mode=configured_password" in rendered
    assert "sources=configured_password" in rendered
    assert "manual-secret" not in rendered
    assert "password_length" not in rendered


def test_strict_configured_password_transport_failure_has_no_fallback(tmp_path) -> None:
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        strict_configured=True,
    )
    calls = []
    with pytest.raises(P2PError):
        _manager(tmp_path).authenticate(
            "UID_A", candidates,
            lambda candidate: (calls.append(candidate.source) or (_ for _ in ()).throw(P2PError("transport"))),
        )
    assert calls == [CONFIGURED_SOURCE]


def test_strict_configured_password_is_never_cached(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        strict_configured=True,
    )
    _manager(tmp_path).authenticate("UID_A", candidates, lambda _candidate: _result(authenticated=True))
    assert CredentialSourceCache(path).get("UID_A") is None
    assert not path.exists() or "manual-secret" not in path.read_text(encoding="utf-8")


def test_strict_configured_password_resource_path_is_never_cached(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    candidates = build_candidates(
        AccountDevice("UID_A", "Front", "account-secret", True),
        "manual-secret",
        strict_configured=True,
    )
    manager = CameraAuthenticator(CredentialSourceCache(path), logger=lambda _line: None)
    selected, _result_value, resource = manager.authenticate_resource(
        "UID_A",
        candidates,
        lambda _candidate: (_result(authenticated=True), object()),
    )
    assert selected.source == CONFIGURED_SOURCE
    assert resource is not None
    assert CredentialSourceCache(path).get("UID_A") is None


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


def test_all_account_device_passwords_are_bounded_and_uid_tagged() -> None:
    candidates = build_candidates(
        "password-a",
        account_device_uid="UID_A",
        account_devices=[
            AccountDevice("UID_A", "A", "password-a"),
            AccountDevice("UID_B", "B", "password-b"),
            AccountDevice("UID_C", "C", "password-b"),
            AccountDevice("UID_D", "D", ""),
        ],
    )
    assert [(item.source, item.source_uid) for item in candidates] == [
        (ACCOUNT_SOURCE, "UID_A"),
        (ACCOUNT_SOURCE, "UID_B"),
        (FALLBACK_SOURCE, None),
    ]


def test_cross_camera_success_is_cached_with_source_uid(tmp_path) -> None:
    candidates = build_candidates(
        "password-a",
        account_device_uid="UID_A",
        account_devices=[AccountDevice("UID_B", "B", "password-b")],
    )
    cache = CredentialSourceCache(tmp_path / "camera_auth_cache.json")
    manager = CameraAuthenticator(cache, logger=lambda _line: None)
    selected, _ = manager.authenticate(
        "UID_A",
        candidates,
        lambda candidate: _result(
            authenticated=candidate.source_uid == "UID_B",
            login_result=0 if candidate.source_uid == "UID_B" else -1,
        ),
    )
    assert selected.source == ACCOUNT_SOURCE
    assert selected.source_uid == "UID_B"
    assert cache.get("UID_A") == ACCOUNT_SOURCE
    assert cache.get_source_uid("UID_A") == "UID_B"


def test_cross_camera_cache_is_reconstructed_from_fresh_enumeration(tmp_path) -> None:
    path = tmp_path / "camera_auth_cache.json"
    cache = CredentialSourceCache(path)
    cache.set("UID_A", ACCOUNT_SOURCE, "UID_B")
    candidates = build_candidates(
        "new-a",
        account_device_uid="UID_A",
        account_devices=[AccountDevice("UID_B", "B", "known-b")],
    )
    calls = []
    selected, _ = CameraAuthenticator(cache, logger=lambda _line: None).authenticate(
        "UID_A", candidates,
        lambda candidate: (calls.append(candidate.source_uid) or _result(authenticated=True)),
    )
    assert selected.source_uid == "UID_B"
    assert calls == ["UID_B"]
