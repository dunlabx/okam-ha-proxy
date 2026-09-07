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


FALLBACK_SOURCE = "fallback_888888"
CONFIGURED_SOURCE = "configured_camera_password"
ACCOUNT_SOURCE = "account_device_password"
KNOWN_SOURCES = frozenset({CONFIGURED_SOURCE, ACCOUNT_SOURCE, FALLBACK_SOURCE})


@dataclass(frozen=True)
class CredentialCandidate:
    source: str
    password: str


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
) -> tuple[CredentialCandidate, ...]:
    """Build authoritative, bounded candidates and deduplicate their values."""

    candidates: list[CredentialCandidate] = []
    seen: set[str] = set()
    if configured_password not in (None, ""):
        if not isinstance(configured_password, str):
            raise P2PError("configured camera credential is invalid")
        candidates.append(CredentialCandidate(CONFIGURED_SOURCE, configured_password))
    if isinstance(account_device_password, str) and account_device_password:
        candidates.append(CredentialCandidate(ACCOUNT_SOURCE, account_device_password))
    candidates.append(CredentialCandidate(FALLBACK_SOURCE, "888888"))
    result: list[CredentialCandidate] = []
    for candidate in candidates:
        if candidate.password in seen:
            continue
        seen.add(candidate.password)
        result.append(candidate)
    return tuple(result)


class CredentialSourceCache:
    """Persist only symbolic successful credential sources, never passwords."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._lock = threading.RLock()
        self._values = self._load()

    def _load(self) -> dict[str, str]:
        try:
            payload = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        if not isinstance(payload, dict):
            return {}
        return {
            key: value
            for key, value in payload.items()
            if isinstance(key, str)
            and isinstance(value, str)
            and value in KNOWN_SOURCES
        }

    def get(self, uid: str) -> str | None:
        with self._lock:
            return self._values.get(uid)

    def invalidate(self, uid: str) -> None:
        with self._lock:
            if uid in self._values:
                del self._values[uid]
                self._save_locked()

    def set(self, uid: str, source: str) -> bool:
        if source not in KNOWN_SOURCES:
            raise ValueError("unknown camera credential source")
        with self._lock:
            self._values[uid] = source
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

    def authenticate(
        self,
        uid: str,
        candidates: Iterable[CredentialCandidate],
        attempt: Attempt,
    ) -> tuple[CredentialCandidate, AuthenticationResult]:
        available = tuple(candidates)
        by_source = {candidate.source: candidate for candidate in available}
        cached_source = self.cache.get(uid)
        ordered: list[tuple[CredentialCandidate, bool]] = []
        if cached_source in by_source:
            ordered.append((by_source[cached_source], True))
        ordered.extend(
            (candidate, False)
            for candidate in available
            if candidate.source != cached_source
        )
        results: list[int | None] = []
        with self._lock_for(uid):
            for number, (candidate, cached) in enumerate(ordered, start=1):
                label = f"cached_{candidate.source}" if cached else candidate.source
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
                    persisted = self.cache.set(uid, candidate.source)
                    self._logger(
                        f"camera_auth_selected uid={uid} candidate={candidate.source} "
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
    ) -> tuple[CredentialCandidate, AuthenticationResult, Resource]:
        """Authenticate while retaining the successful session resource.

        Stream consumers must use the same native session that accepted the
        credential. Each rejected candidate is fully discarded before the
        next candidate is tried, so no probe/login pair can bypass this path.
        """

        available = tuple(candidates)
        by_source = {candidate.source: candidate for candidate in available}
        cached_source = self.cache.get(uid)
        ordered: list[tuple[CredentialCandidate, bool]] = []
        if cached_source in by_source:
            ordered.append((by_source[cached_source], True))
        ordered.extend(
            (candidate, False)
            for candidate in available
            if candidate.source != cached_source
        )
        results: list[int | None] = []
        with self._lock_for(uid):
            for number, (candidate, cached) in enumerate(ordered, start=1):
                label = f"cached_{candidate.source}" if cached else candidate.source
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
                    persisted = self.cache.set(uid, candidate.source)
                    self._logger(
                        f"camera_auth_selected uid={uid} candidate={candidate.source} "
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
