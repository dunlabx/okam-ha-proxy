"""Non-blocking client for the local O-KAM native bridge API."""

from __future__ import annotations

import logging
from typing import Any
from urllib.parse import urlsplit

from aiohttp import ClientError, ClientSession, ClientTimeout

_LOGGER = logging.getLogger(__name__)


class OkamApiError(RuntimeError):
    """Bridge request failed."""


class OkamAuthError(OkamApiError):
    """Bridge rejected the API token."""


class OkamInvalidResponseError(OkamApiError):
    """Bridge returned an invalid response or URL."""


def normalize_bridge_url(value: str) -> str:
    """Normalize and validate a bridge base URL without changing its host."""

    if not isinstance(value, str):
        raise OkamInvalidResponseError("bridge URL must be text")
    normalized = value.strip().rstrip("/")
    parsed = urlsplit(normalized)
    if parsed.scheme not in {"http", "https"} or not parsed.netloc:
        raise OkamInvalidResponseError("bridge URL must use http or https")
    return normalized


class OkamApi:
    def __init__(self, session: ClientSession, base_url: str, token: str) -> None:
        self._session = session
        self.base_url = normalize_bridge_url(base_url)
        self._headers = {"Authorization": f"Bearer {token.strip()}"}
        self._timeout = ClientTimeout(total=15, connect=5)

    async def _request(self, method: str, path: str, **kwargs: Any) -> Any:
        timeout = kwargs.pop("timeout", self._timeout)
        try:
            async with self._session.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers,
                timeout=timeout,
                **kwargs,
            ) as response:
                if response.status == 401:
                    raise OkamAuthError("invalid bridge API token")
                response.raise_for_status()
                if response.content_type == "application/json":
                    try:
                        return await response.json()
                    except (ValueError, TypeError) as exc:
                        raise OkamInvalidResponseError(
                            "bridge returned invalid JSON"
                        ) from exc
                return await response.read()
        except OkamAuthError:
            raise
        except OkamInvalidResponseError:
            raise
        except (ClientError, TimeoutError, OSError) as exc:
            _LOGGER.debug(
                "okam_bridge_request_failed method=%s path=%s reason=%s",
                method,
                path,
                type(exc).__name__,
            )
            raise OkamApiError("unable to reach O-KAM bridge") from exc

    async def health(self) -> dict[str, Any]:
        try:
            async with self._session.get(
                f"{self.base_url}/health", headers=self._headers, timeout=self._timeout
            ) as response:
                if response.status == 401:
                    raise OkamAuthError("invalid bridge API token")
                response.raise_for_status()
                try:
                    payload = await response.json()
                except (ValueError, TypeError) as exc:
                    raise OkamInvalidResponseError(
                        "bridge returned invalid health JSON"
                    ) from exc
                if not isinstance(payload, dict):
                    raise OkamInvalidResponseError("bridge health payload is not an object")
                return payload
        except (OkamAuthError, OkamInvalidResponseError):
            raise
        except (ClientError, TimeoutError, OSError) as exc:
            _LOGGER.debug(
                "okam_bridge_request_failed method=GET path=/health reason=%s",
                type(exc).__name__,
            )
            raise OkamApiError("unable to reach O-KAM bridge") from exc

    async def devices(self) -> list[dict[str, Any]]:
        payload = await self._request("GET", "/api/devices")
        if not isinstance(payload, list) or not all(
            isinstance(item, dict) for item in payload
        ):
            raise OkamInvalidResponseError("bridge devices payload is not a list")
        return payload

    async def status(self, camera_uid: str) -> dict[str, Any]:
        return await self._request("GET", f"/api/cameras/{camera_uid}/status")

    async def snapshot(self, camera_uid: str) -> bytes:
        return await self._request(
            "GET",
            f"/api/cameras/{camera_uid}/snapshot.jpg",
            timeout=ClientTimeout(total=90, connect=5),
        )

    async def configure(self, camera_uid: str, idle_timeout: int) -> None:
        await self._request(
            "PATCH",
            f"/api/cameras/{camera_uid}/config",
            json={"idle_timeout_seconds": idle_timeout},
        )

    async def stream_source(self, camera_uid: str) -> str:
        result = await self._request("GET", f"/api/cameras/{camera_uid}/stream/source")
        return str(result["stream_url"])
