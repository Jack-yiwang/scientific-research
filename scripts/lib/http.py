"""
HTTP client with retry, simple token-bucket rate limiting, and on-disk JSON cache.

Used by all source clients (arXiv, CrossRef, PubMed, Semantic Scholar). Stdlib-only
so the skill works in environments without `requests` installed.

Cache layout:
  ~/.cache/scientific-research/<host>/<sha1>.json

Each cache entry stores: {"url": ..., "status": int, "headers": {...}, "body": "..."}
"""

from __future__ import annotations

import gzip
import hashlib
import io
import json
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any, Optional


DEFAULT_UA = "scientific-research-skill/0.2 (+https://github.com/Jack-yiwang/scientific-research)"
DEFAULT_TIMEOUT = 30
CACHE_ROOT = Path(os.environ.get(
    "SCI_RESEARCH_CACHE",
    str(Path.home() / ".cache" / "scientific-research"),
))


class RateLimiter:
    """Minimal sleep-based rate limiter — `min_interval` seconds between calls."""

    def __init__(self, min_interval: float = 0.0) -> None:
        self.min_interval = min_interval
        self._last = 0.0

    def wait(self) -> None:
        if self.min_interval <= 0:
            return
        now = time.monotonic()
        gap = now - self._last
        if gap < self.min_interval:
            time.sleep(self.min_interval - gap)
        self._last = time.monotonic()


def _cache_path(url: str) -> Path:
    parsed = urllib.parse.urlparse(url)
    host = parsed.netloc or "_"
    digest = hashlib.sha1(url.encode("utf-8")).hexdigest()
    return CACHE_ROOT / host / f"{digest}.json"


def _read_cache(url: str, ttl: Optional[float]) -> Optional[dict]:
    if ttl is None:
        return None
    p = _cache_path(url)
    if not p.exists():
        return None
    if ttl > 0 and (time.time() - p.stat().st_mtime) > ttl:
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _write_cache(url: str, payload: dict) -> None:
    p = _cache_path(url)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")


def http_get(
    url: str,
    *,
    headers: Optional[dict] = None,
    params: Optional[dict] = None,
    timeout: int = DEFAULT_TIMEOUT,
    cache_ttl: Optional[float] = 86400,
    rate_limiter: Optional[RateLimiter] = None,
    retries: int = 3,
    backoff: float = 1.5,
) -> tuple[int, dict, str]:
    """GET with retries, rate-limit and cache.

    Returns (status_code, response_headers, body_text). Raises urllib errors only
    after the final retry.
    """
    if params:
        sep = "&" if "?" in url else "?"
        url = url + sep + urllib.parse.urlencode(params, doseq=True)

    cached = _read_cache(url, cache_ttl)
    if cached is not None:
        return cached["status"], cached.get("headers", {}), cached.get("body", "")

    if rate_limiter is not None:
        rate_limiter.wait()

    req_headers = {"User-Agent": DEFAULT_UA, "Accept-Encoding": "gzip"}
    if headers:
        req_headers.update(headers)

    last_err: Optional[Exception] = None
    for attempt in range(retries):
        try:
            req = urllib.request.Request(url, headers=req_headers)
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                raw = resp.read()
                if resp.headers.get("Content-Encoding", "").lower() == "gzip":
                    raw = gzip.decompress(raw)
                body = raw.decode("utf-8", errors="replace")
                status = resp.getcode()
                resp_headers = {k: v for k, v in resp.headers.items()}
                if 200 <= status < 300 and cache_ttl is not None:
                    _write_cache(url, {
                        "url": url,
                        "status": status,
                        "headers": resp_headers,
                        "body": body,
                    })
                return status, resp_headers, body
        except urllib.error.HTTPError as e:
            # 429 / 5xx → retry; 4xx (other) → return immediately so caller can decide
            if e.code in (429, 500, 502, 503, 504) and attempt < retries - 1:
                time.sleep(backoff ** attempt)
                continue
            try:
                body = e.read().decode("utf-8", errors="replace")
            except Exception:
                body = ""
            return e.code, dict(e.headers or {}), body
        except (urllib.error.URLError, TimeoutError) as e:
            last_err = e
            if attempt < retries - 1:
                time.sleep(backoff ** attempt)
                continue
            raise

    if last_err:
        raise last_err
    raise RuntimeError("http_get: exhausted retries without exception")


def http_get_json(url: str, **kwargs: Any) -> Any:
    status, _, body = http_get(url, **kwargs)
    if not (200 <= status < 300):
        raise RuntimeError(f"HTTP {status} for {url}: {body[:200]}")
    return json.loads(body)
