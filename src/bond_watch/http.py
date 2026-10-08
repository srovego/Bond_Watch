from __future__ import annotations

import logging
import time

import httpx

log = logging.getLogger(__name__)


class Http:
    def __init__(self, timeout: float = 12, retries: int = 2):
        self.client = httpx.Client(timeout=timeout, follow_redirects=True, headers={"User-Agent": "bond-watch/0.1 (personal portfolio monitor)"})
        self.retries = retries

    def get(self, url: str, **kwargs) -> httpx.Response:
        for attempt in range(self.retries + 1):
            try:
                response = self.client.get(url, **kwargs)
                if response.status_code in (429, 500, 502, 503, 504) and attempt < self.retries:
                    delay = min(2 ** attempt, 8)
                    log.warning("HTTP %s %s; retry in %ss", response.status_code, url, delay)
                    time.sleep(delay)
                    continue
                response.raise_for_status()
                return response
            except (httpx.TimeoutException, httpx.TransportError):
                if attempt >= self.retries:
                    raise
                time.sleep(min(2 ** attempt, 8))
        raise RuntimeError("unreachable")

    def close(self) -> None:
        self.client.close()

    def __enter__(self):
        return self

    def __exit__(self, *_):
        self.close()

