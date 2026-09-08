"""Bounded, credential-safe per-camera authentication selection."""

from __future__ import annotations

import json
import os
import tempfile
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Iterable, TypeVar

from .p2p import AuthenticationResult, P2PError
from .logging import timestamped_print


print = timestamped_print


FALLBACK_SOURCE = "fallback_888888"
EMPTY_PASSWORD_SOURCE = "empty_password"
MANUAL_SOURCE = "manual_password"
# Kept as an import-compatible alias for callers of the 1.2.x library.
CONFIGURED_SOURCE = MANUAL_SOURCE
ACCOUNT_SOURCE = "account_device_password"
KNOWN_SOURCES = frozenset(
    {ACCOUNT_SOURCE, EMPTY_PASSWORD_SOURCE, FALLBACK_SOURCE}
)


@dataclass(frozen=True)
class CredentialCandidate:
    source: str
    password: str
    source_uid: str | None = None


class AuthenticationRejected(P2PError):
    """Every known credential was explicitly rejected by the camera."""

    def __init__(self, uid: str, results: tuple[int | None, ...]) -> None:
        super().__init__("camera rejected every known authentication credential")
        self.uid = uid
        self.results = results


class AuthenticationTransportError(P2PError):
    """The native cycle did not produce an explicit authentication result."""


def build_candidates(
    account_device_password: object,
    configured_password: object = None,
    *,
    account_device_uid: str | None = None,
    account_devices: Iterable[object] | None = None,
    strict_configured: bool = False,
    debug_credentials: bool = False,
) -> tuple[CredentialCandidate, ...]:
    """Build bounded candidates from the authenticated account response.

    ``/PC/device/show`` associates the local password with each device object
    through that object's ``uid`` and ``password`` keys.  The associated value
    is tried first (after an explicit administrator override), followed by
    distinct non-empty passwords from the other objects in the same response.
    An explicitly present empty associated password is represented by the
    symbolic ``empty_password`` source and is tried before the fixed fallback.
    The source UID is metadata only; plaintext values never enter logs/cache.
    ``strict_configured`` returns exactly one manual candidate and bypasses all
    automatic sources for a per-camera override.
    The original two-argument form remains supported for callers/tests.
    """

    candidates: list[CredentialCandidate] = []
    seen: set[str] = set()
    if strict_configured:
        if not isinstance(configured_password, str):
            raise P2PError("configured camera credential is invalid")
        # Explicit per-camera mode is intentionally bounded to one candidate;
        # an empty string is a real credential and must remain representable.
        return (CredentialCandidate(MANUAL_SOURCE, configured_password),)
    if configured_password not in (None, ""):
        if not isinstance(configured_password, str):
            raise P2PError("configured camera credential is invalid")
        configured = CredentialCandidate(CONFIGURED_SOURCE, configured_password)

        candidates.append(configured)
    associated = account_device_password
    associated_present: bool | None = None
    if isinstance(associated, dict):
        associated_present = "password" in associated or "device_password" in associated
        associated = associated.get("password", associated.get("device_password"))
    elif not isinstance(associated, str):
        associated_present = getattr(account_device_password, "password_present", None)
        associated = getattr(account_device_password, "device_password", None)
    if isinstance(associated, str) and associated:
        candidates.append(
            CredentialCandidate(ACCOUNT_SOURCE, associated, account_device_uid)
        )
    for item in account_devices or ():
        uid = getattr(item, "uid", None)
        password = getattr(item, "device_password", None)
        if isinstance(item, dict):
            uid = item.get("uid")
            password = item.get("password", item.get("device_password"))
        if not isinstance(uid, str) or not uid:
            continue
        if account_device_uid and uid.casefold() == account_device_uid.casefold():
            continue
        if isinstance(password, str) and password:
            candidates.append(CredentialCandidate(ACCOUNT_SOURCE, password, uid))
    if associated_present is True and associated == "":
        candidates.append(CredentialCandidate(EMPTY_PASSWORD_SOURCE, ""))
    candidates.append(CredentialCandidate(FALLBACK_SOURCE, "888888"))
    result: list[CredentialCandidate] = []
    for candidate in candidates:
        if candidate.password in seen:
            continue
        seen.add(candidate.password)
        result.append(candidate)
    return tuple(result)


class CredentialSourceCache:
    """Persist symbolic source plus UID metadata, never passwords."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._values = self._load()

    def _load(self) -> dict[str, dict[str, str | None]]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        result: dict[str, dict[str, str | None]] = {}
        for key, value in payload.items():
            if not isinstance(key, str):
                continue
            if isinstance(value, str) and value in KNOWN_SOURCES:
                # Backward-compatible 1.2.x cache: account source means the
                # currently associated account-device password.
                result[key] = {"source": value, "source_uid": None}
            elif isinstance(value, dict):
                source = value.get("source")
                source_uid = value.get("source_uid")
                if source in KNOWN_SOURCES and (source_uid is None or isinstance(source_uid, str)):
                    result[key] = {"source": source, "source_uid": source_uid}
        return result

    def get(self, uid: str) -> str | None:
        with self._lock:
            value = self._values.get(uid)
            return value.get("source") if value else None

    def get_source_uid(self, uid: str) -> str | None:
        with self._lock:
            value = self._values.get(uid)
            source_uid = value.get("source_uid") if value else None
            return source_uid if isinstance(source_uid, str) else None

    def invalidate(self, uid: str) -> None:
        with self._lock:
            if uid in self._values:
                del self._values[uid]
                self._save_locked()

    def set(self, uid: str, source: str, source_uid: str | None = None) -> bool:
        if source not in KNOWN_SOURCES:
            raise ValueError("unknown camera credential source")
        with self._lock:
            self._values[uid] = {"source": source, "source_uid": source_uid}
            return self._save_locked()

    def _save_locked(self) -> bool:
        """Atomically replace the cache; persistence errors are non-fatal."""

        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            fd, temporary = tempfile.mkstemp(
                prefix=f".{self.path.name}.", dir=self.path.parent
            )
            try:
                os.fchmod(fd, 0o600)
                with os.fdopen(fd, "w", encoding="utf-8") as stream:
                    json.dump(self._values, stream, sort_keys=True, separators=(",", ":"))
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(temporary, self.path)
            finally:
                try:
                    os.unlink(temporary)
                except FileNotFoundError:
                    pass
        except OSError:
            return False
        return True


Attempt = Callable[[CredentialCandidate], AuthenticationResult]
Log = Callable[[str], None]
Resource = TypeVar("Resource")
ResourceAttempt = Callable[[CredentialCandidate], tuple[AuthenticationResult, Resource]]


class CameraAuthenticator:
    """Select credentials independently per UID with bounded native cycles."""

    def __init__(self, cache: CredentialSourceCache, logger: Log = print) -> None:
        self.cache = cache
        self._logger = logger
        self._lock = threading.RLock()
        self._uid_locks: dict[str, threading.Lock] = {}

    def _lock_for(self, uid: str) -> threading.Lock:
        with self._lock:
            return self._uid_locks.setdefault(uid, threading.Lock())

    @staticmethod
    def _matches_cached(candidate: CredentialCandidate, source: str | None, source_uid: str | None) -> bool:
        if candidate.source != source:
            return False
        return source_uid is None or candidate.source_uid == source_uid

    @staticmethod
    def _label(candidate: CredentialCandidate, cached: bool = False) -> str:
        label = f"cached_{candidate.source}" if cached else candidate.source
        if candidate.source_uid:
            label += f" source_uid={candidate.source_uid}"
        return label

    def _log_candidates(
        self,
        uid: str,
        candidates: tuple[CredentialCandidate, ...],
        *,
        debug_credentials: bool = False,
    ) -> None:
        sources = ",".join(
            self._label(candidate).replace(" ", "_") for candidate in candidates
        )
        mode = (
            "password"
            if len(candidates) == 1 and candidates[0].source == MANUAL_SOURCE
            else "automatic"
        )
        self._logger(
            f"camera_auth_candidates uid={uid} auth_method={mode} "
            f"candidate_count={len(candidates)} sources={sources}"
        )
        if debug_credentials:
            for candidate in candidates:
                source_uid = candidate.source_uid or ""
                self._logger(
                    "camera_auth_candidate "
                    f"uid={uid} source={candidate.source} "
                    f"source_uid={source_uid} password={candidate.password!r} "
                    f"password_length={len(candidate.password)}"
                )

    def authenticate(
        self,
        uid: str,
        candidates: Iterable[CredentialCandidate],
        attempt: Attempt,
        *,
        debug_credentials: bool = False,
    ) -> tuple[CredentialCandidate, AuthenticationResult]:
        available = tuple(candidates)
        cached_source = self.cache.get(uid)
        cached_uid = self.cache.get_source_uid(uid)
        ordered: list[tuple[CredentialCandidate, bool]] = []
        cached = next(
            (candidate for candidate in available if self._matches_cached(candidate, cached_source, cached_uid)),
            None,
        )
        if cached is not None:
            ordered.append((cached, True))
        ordered.extend((candidate, False) for candidate in available if candidate is not cached)
        self._log_candidates(uid, available, debug_credentials=debug_credentials)
        results: list[int | None] = []
        with self._lock_for(uid):
            for number, (candidate, cached) in enumerate(ordered, start=1):
                label = self._label(candidate, cached)
                self._logger(
                    f"camera_auth_attempt uid={uid} candidate={label} attempt={number}"
                )
                try:
                    result = attempt(candidate)
                except P2PError:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} "
                        "result=transport_failure"
                    )
                    raise
                explicit_rejection = (
                    result.connected
                    and result.login_sent
                    and result.login_response_received
                    and not result.authenticated
                    and result.login_result is not None
                    and result.login_result != 0
                )
                if result.authenticated:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                        f"result=success login_result={result.login_result}"
                    )
                    persisted = False
                    if candidate.source != MANUAL_SOURCE:
                        persisted = self.cache.set(uid, candidate.source, candidate.source_uid)
                    self._logger(
                        f"camera_auth_selected uid={uid} candidate={candidate.source} "
                        f"source_uid={candidate.source_uid or ''} "
                        f"persisted={str(persisted).lower()}"
                    )
                    return candidate, result
                if not explicit_rejection:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                        "result=transport_failure"
                    )
                    raise AuthenticationTransportError(
                        "native cycle did not return an explicit authentication rejection"
                    )
                results.append(result.login_result)
                self._logger(
                    f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                    f"result=failed login_result={result.login_result}"
                )
                if cached:
                    self.cache.invalidate(uid)
                    self._logger(f"camera_auth_cache_invalidated uid={uid}")
        raise AuthenticationRejected(uid, tuple(results))

    def authenticate_resource(
        self,
        uid: str,
        candidates: Iterable[CredentialCandidate],
        attempt: ResourceAttempt[Resource],
        discard: Callable[[Resource], None] | None = None,
        *,
        debug_credentials: bool = False,
    ) -> tuple[CredentialCandidate, AuthenticationResult, Resource]:
        """Authenticate while retaining the successful session resource.

        Stream consumers must use the same native session that accepted the
        credential. Each rejected candidate is fully discarded before the
        next candidate is tried, so no probe/login pair can bypass this path.
        """

        available = tuple(candidates)
        cached_source = self.cache.get(uid)
        cached_uid = self.cache.get_source_uid(uid)
        ordered: list[tuple[CredentialCandidate, bool]] = []
        cached = next(
            (candidate for candidate in available if self._matches_cached(candidate, cached_source, cached_uid)),
            None,
        )
        if cached is not None:
            ordered.append((cached, True))
        ordered.extend((candidate, False) for candidate in available if candidate is not cached)
        self._log_candidates(uid, available, debug_credentials=debug_credentials)
        results: list[int | None] = []
        with self._lock_for(uid):
            for number, (candidate, cached) in enumerate(ordered, start=1):
                label = self._label(candidate, cached)
                self._logger(
                    f"camera_auth_attempt uid={uid} candidate={label} attempt={number}"
                )
                resource: Resource | None = None
                try:
                    result, resource = attempt(candidate)
                except P2PError:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} "
                        "result=transport_failure"
                    )
                    raise
                explicit_rejection = (
                    result.connected
                    and result.login_sent
                    and result.login_response_received
                    and not result.authenticated
                    and result.login_result is not None
                    and result.login_result != 0
                )
                if result.authenticated:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                        f"result=success login_result={result.login_result}"
                    )
                    persisted = False
                    if candidate.source != MANUAL_SOURCE:
                        persisted = self.cache.set(uid, candidate.source, candidate.source_uid)
                    self._logger(
                        f"camera_auth_selected uid={uid} candidate={candidate.source} "
                        f"source_uid={candidate.source_uid or ''} "
                        f"persisted={str(persisted).lower()}"
                    )
                    return candidate, result, resource  # type: ignore[return-value]
                if resource is not None and discard is not None:
                    discard(resource)
                if not explicit_rejection:
                    self._logger(
                        f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                        "result=transport_failure"
                    )
                    raise AuthenticationTransportError(
                        "native cycle did not return an explicit authentication rejection"
                    )
                results.append(result.login_result)
                self._logger(
                    f"camera_auth_attempt uid={uid} candidate={label} attempt={number} "
                    f"result=failed login_result={result.login_result}"
                )
                if cached:
                    self.cache.invalidate(uid)
                    self._logger(f"camera_auth_cache_invalidated uid={uid}")
        raise AuthenticationRejected(uid, tuple(results))
