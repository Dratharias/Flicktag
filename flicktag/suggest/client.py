"""HTTP client for the suggestion container, with offline backoff."""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
import numpy as np

DEFAULT_URL = "http://127.0.0.1:8765"


class Offline(RuntimeError):
    pass


@dataclass(frozen=True)
class ServiceInfo:
    model: str
    device: str
    dim: int
    logit_scale: float
    logit_bias: float


class SuggestClient:
    """Thread-safe. After a failure, calls fail fast until the backoff delay expires."""

    def __init__(
        self,
        base_url: str = DEFAULT_URL,
        timeout: float = 30.0,
        transport: httpx.BaseTransport | None = None,
        clock=time.monotonic,
    ):
        self.base_url = base_url.rstrip("/")
        self._http = httpx.Client(base_url=self.base_url, timeout=timeout, transport=transport)
        self._clock = clock
        self._lock = threading.Lock()
        self._info: ServiceInfo | None = None
        self._retry_at = 0.0
        self._delay = 1.0

    @property
    def cached_info(self) -> ServiceInfo | None:
        """Last health answer, without any network call (None when offline)."""
        return self._info

    @property
    def online(self) -> bool:
        return self._info is not None

    def _fail(self, error: Exception) -> Offline:
        with self._lock:
            self._info = None
            self._retry_at = self._clock() + self._delay
            self._delay = min(self._delay * 2, 60.0)
        return Offline(str(error))

    def _request(self, method: str, url: str, **kwargs) -> dict:
        if self._clock() < self._retry_at:
            raise Offline("suggestion service unavailable (backing off)")
        try:
            response = self._http.request(method, url, **kwargs)
            response.raise_for_status()
            return response.json()
        except (httpx.HTTPError, ValueError) as error:
            raise self._fail(error) from error

    def info(self) -> ServiceInfo:
        if self._info is None:
            data = self._request("GET", "/health")
            with self._lock:
                self._info = ServiceInfo(
                    model=data["model"],
                    device=data["device"],
                    dim=int(data["dim"]),
                    logit_scale=float(data["logit_scale"]),
                    logit_bias=float(data["logit_bias"]),
                )
                self._delay = 1.0
        return self._info

    def embed_images(self, paths: list[Path]) -> list[np.ndarray | None]:
        self.info()
        files = []
        for path in paths:
            try:
                files.append(("files", (path.name, path.read_bytes(), "application/octet-stream")))
            except OSError:
                files.append(("files", (path.name, b"", "application/octet-stream")))
        data = self._request("POST", "/embed/images", files=files)
        return [None if v is None else np.asarray(v, dtype=np.float32) for v in data["vectors"]]

    def embed_texts(self, texts: list[str]) -> list[np.ndarray]:
        self.info()
        data = self._request("POST", "/embed/texts", json={"texts": texts})
        return [np.asarray(v, dtype=np.float32) for v in data["vectors"]]

    def close(self) -> None:
        self._http.close()
