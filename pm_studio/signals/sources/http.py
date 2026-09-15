"""One tiny HTTP seam shared by the tracker adapters, replaced wholesale by tests."""

from __future__ import annotations

import base64
import json
import urllib.error
import urllib.parse
import urllib.request

HTTP_TIMEOUT_SECONDS = 120


class SourceError(Exception):
    pass


def basic_auth(username: str, token: str) -> str:
    return "Basic " + base64.b64encode(f"{username}:{token}".encode()).decode()


def get_json(url: str, headers: dict[str, str], *, timeout: int = HTTP_TIMEOUT_SECONDS) -> dict:
    request = urllib.request.Request(url, headers={**headers, "Accept": "application/json"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = ""
        try:
            detail = exc.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        raise SourceError(f"HTTP {exc.code}: {detail or exc.reason}") from exc
    except urllib.error.URLError as exc:
        raise SourceError(f"could not reach {urllib.parse.urlsplit(url).netloc}: {exc.reason}") from exc
    except TimeoutError as exc:
        raise SourceError(f"timed out after {timeout}s") from exc
    if not raw.strip():
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise SourceError(f"non-JSON response ({exc})") from exc
    return parsed if isinstance(parsed, dict) else {"value": parsed}


def qs(params: dict) -> str:
    return urllib.parse.urlencode({k: v for k, v in params.items() if v is not None and v != ""})
