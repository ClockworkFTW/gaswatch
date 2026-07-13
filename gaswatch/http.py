"""Shared HTTP client: polite rate limiting, retries, raw-response archiving."""
from __future__ import annotations

import logging
import time
from pathlib import Path
from urllib.parse import urlsplit

import httpx

log = logging.getLogger("gaswatch.http")

USER_AGENT = "gaswatch/0.1 (personal gas-market research tool; contact: nikolasboyle@gmail.com)"
RAW_DIR = Path("data/raw")

# Minimum seconds between requests to the same host.
MIN_INTERVAL = 1.0
RETRIES = 3
BACKOFF = [2, 5, 15]


class EbbClient:
    """httpx wrapper used by all adapters.

    - one request/second/host
    - retry with backoff on 5xx and transport errors
    - optional raw archiving of every response body
    """

    def __init__(self, timeout: float = 60.0):
        self._client = httpx.Client(
            headers={"User-Agent": USER_AGENT, "Accept-Encoding": "gzip, deflate"},
            timeout=timeout,
            follow_redirects=True,
            verify=True,
        )
        self._last_hit: dict[str, float] = {}

    def close(self) -> None:
        self._client.close()

    def _throttle(self, url: str) -> None:
        host = urlsplit(url).netloc
        elapsed = time.monotonic() - self._last_hit.get(host, 0.0)
        if elapsed < MIN_INTERVAL:
            time.sleep(MIN_INTERVAL - elapsed)
        self._last_hit[host] = time.monotonic()

    def request(self, method: str, url: str, *, params: dict | None = None,
                data: dict | None = None, headers: dict | None = None,
                ok_statuses: tuple = (200,)) -> httpx.Response:
        last_exc: Exception | None = None
        for attempt in range(RETRIES + 1):
            self._throttle(url)
            try:
                resp = self._client.request(method, url, params=params, data=data, headers=headers)
                if resp.status_code in ok_statuses:
                    return resp
                if resp.status_code < 500:
                    # 4xx: retrying won't help — fail loudly with context.
                    raise httpx.HTTPStatusError(
                        f"{method} {url} -> {resp.status_code}: {resp.text[:300]}",
                        request=resp.request, response=resp)
                last_exc = httpx.HTTPStatusError(
                    f"{method} {url} -> {resp.status_code}", request=resp.request, response=resp)
            except (httpx.TransportError, httpx.HTTPStatusError) as exc:
                if isinstance(exc, httpx.HTTPStatusError) and exc.response is not None \
                        and exc.response.status_code < 500:
                    raise
                last_exc = exc
            if attempt < RETRIES:
                wait = BACKOFF[min(attempt, len(BACKOFF) - 1)]
                log.warning("retry %d for %s in %ss (%s)", attempt + 1, url, wait, last_exc)
                time.sleep(wait)
        raise RuntimeError(f"giving up on {method} {url}") from last_exc

    def get(self, url: str, **kw) -> httpx.Response:
        return self.request("GET", url, **kw)

    def post(self, url: str, **kw) -> httpx.Response:
        return self.request("POST", url, **kw)

    @staticmethod
    def archive(pipeline: str, dataset: str, name: str, content: bytes) -> str:
        """Save a raw response under data/raw/<pipeline>/<dataset>/<name>; returns path."""
        path = RAW_DIR / pipeline / dataset / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(content)
        return str(path)
