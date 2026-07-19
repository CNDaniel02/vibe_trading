from __future__ import annotations

import json
import time
import urllib.error
import urllib.request
from typing import Any

from scripts.adapters.errors import AdapterDataError


def request_json(
    url: str,
    *,
    method: str = "GET",
    headers: dict[str, str] | None = None,
    payload: dict[str, Any] | None = None,
    timeout_seconds: float = 20,
    max_retries: int = 2,
) -> dict[str, Any]:
    encoded = json.dumps(payload).encode("utf-8") if payload is not None else None
    safe_headers = {"Accept": "application/json", **(headers or {})}
    if encoded is not None:
        safe_headers.setdefault("Content-Type", "application/json")

    last_error: Exception | None = None
    for attempt in range(max_retries + 1):
        try:
            request = urllib.request.Request(url, data=encoded, headers=safe_headers, method=method)
            with urllib.request.urlopen(request, timeout=timeout_seconds) as response:
                raw = json.loads(response.read().decode("utf-8"))
            if not isinstance(raw, dict):
                raise AdapterDataError("upstream response must be a JSON object")
            return raw
        except (urllib.error.HTTPError, urllib.error.URLError, TimeoutError, json.JSONDecodeError, AdapterDataError) as exc:
            last_error = exc
            if attempt < max_retries:
                time.sleep(min(2**attempt, 4))

    # Do not include response bodies or headers because they can contain credentials.
    raise AdapterDataError(f"HTTP JSON request failed after {max_retries + 1} attempt(s): {type(last_error).__name__}") from last_error
