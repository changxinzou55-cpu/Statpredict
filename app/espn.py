"""Thin client for ESPN's public JSON feeds (no key required)."""
import threading
import time

import requests

SITE = "https://site.api.espn.com/apis/site/v2/sports"
_local = threading.local()


class EspnError(RuntimeError):
    pass


def _session() -> requests.Session:
    s = getattr(_local, "s", None)
    if s is None:
        s = requests.Session()
        s.headers.update({"User-Agent": "StatPredict/1.0", "Accept-Encoding": "gzip"})
        _local.s = s
    return s


def get(path: str, params: dict | None = None, retries: int = 3, timeout: int = 30) -> dict:
    url = path if path.startswith("http") else f"{SITE}/{path}"
    last = None
    for attempt in range(retries):
        try:
            r = _session().get(url, params=params, timeout=timeout)
            if r.status_code == 200:
                return r.json()
            last = f"HTTP {r.status_code}"
            if r.status_code in (400, 404):
                break
        except (requests.RequestException, ValueError) as e:
            last = repr(e)
        time.sleep(0.6 * (attempt + 1))
    raise EspnError(f"{url} {params or ''}: {last}")
