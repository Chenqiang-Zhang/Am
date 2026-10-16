import json
import shutil
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path
from typing import Any

USER_AGENT = "Mozilla/5.0"
RETRYABLE_STATUS_CODES = (429, 500, 502, 503, 504)
MAX_ATTEMPTS = 6
BACKOFF_SECONDS = 5


def open_with_retry(url: str, timeout: int):
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    for attempt in range(MAX_ATTEMPTS):
        try:
            return urllib.request.urlopen(request, timeout=timeout)
        except urllib.error.HTTPError as error:
            if error.code not in RETRYABLE_STATUS_CODES or attempt == MAX_ATTEMPTS - 1:
                raise
        except urllib.error.URLError:
            if attempt == MAX_ATTEMPTS - 1:
                raise
        time.sleep(BACKOFF_SECONDS * 2**attempt)
    raise RuntimeError("unreachable")


def get_json(url: str, params: dict[str, Any] | None = None, pause_seconds: float = 0.0) -> dict[str, Any]:
    if params:
        url = f"{url}?{urllib.parse.urlencode(params)}"
    with open_with_retry(url, timeout=60) as response:
        body = json.load(response)
    time.sleep(pause_seconds)
    return body


def download_file(url: str, destination: Path) -> None:
    partial_path = destination.with_suffix(destination.suffix + ".part")
    with open_with_retry(url, timeout=120) as response, partial_path.open("wb") as file:
        shutil.copyfileobj(response, file)
    partial_path.rename(destination)
