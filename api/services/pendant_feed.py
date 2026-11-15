"""HTTP client for the pendant-direct feed (`LIFEOS_PENDANT_FEED_*`).

The feed is a local, bearer-authenticated JSON API. Its token comes from
`LIFEOS_PENDANT_FEED_TOKEN`, or else the file named by
`LIFEOS_PENDANT_FEED_TOKEN_FILE`; with neither, the feed is unconfigured.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Optional

from config.settings import settings


class PendantFeedError(Exception):
    """The feed could not be read: network failure, HTTP error or bad JSON."""


def feed_token() -> str:
    """The feed bearer token, or "" when none is configured."""
    if settings.pendant_feed_token:
        return settings.pendant_feed_token
    try:
        return Path(settings.pendant_feed_token_file).expanduser().read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def get_json(path: str, params: Optional[dict] = None, timeout: float = 10) -> dict:
    """GET `{pendant_feed_url}/{path}` and return its JSON object.

    Raises:
        PendantFeedError: on any network, HTTP or decoding failure, or a
            response that is not a JSON object. The message names the failure
            category only.
    """
    url = f"{settings.pendant_feed_url.rstrip('/')}/{path.lstrip('/')}"
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    request = urllib.request.Request(url, headers={"Authorization": f"Bearer {feed_token()}"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            data = json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        raise PendantFeedError(f"http_{e.code}") from None
    except (urllib.error.URLError, OSError) as e:
        raise PendantFeedError(f"unreachable ({type(e).__name__})") from None
    except ValueError:
        raise PendantFeedError("bad_json") from None
    if not isinstance(data, dict):
        raise PendantFeedError("bad_json")
    return data
