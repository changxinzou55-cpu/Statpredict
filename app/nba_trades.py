"""NBA trade coverage and dated StatPredict forecasts."""
import logging
import re
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import date, datetime, timedelta, timezone
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit, urlunsplit
from xml.etree import ElementTree
from zoneinfo import ZoneInfo

import requests

from . import espn

log = logging.getLogger(__name__)
HOOPS_FEED = "https://hoopswire.com/category/nba-rumors-news/feed/"
NBC_REPORT = (
    "https://www.nbcsports.com/nba/news/"
    "nba-trade-rumors-2026-27-dejounte-murray-update-or-non-update-kyrie-irving-trey-murphy-more"
)
MURRAY_REPORT = (
    "https://hoopswire.com/suns-pelicans-discussed-jalen-green-dejounte-murray-swap-but-zero-traction/"
)
FORECASTS = [
    {
        "name": "Dejounte Murray", "pid": "3907497", "team": "New Orleans Pelicans",
        "reported": "Murray asked out. Phoenix and New Orleans reportedly spoke, but the discussions stalled.",
        "prediction": "Stays with New Orleans.",
        "reason": "Phoenix reportedly wants draft compensation to take on his contract. A buyer willing to absorb it, or a change in the Pelicans’ asking price, would move this forecast.",
        "confidence": "Moderate",
        "sources": [{"name": "NBC Sports", "url": NBC_REPORT}, {"name": "Hoops Wire", "url": MURRAY_REPORT}],
        "issued": "2026-11-23", "through": "2026-12-15",
    },
    {
        "name": "Kyrie Irving", "pid": "6442", "team": "Dallas Mavericks",
        "reported": "Utah has reportedly considered Irving internally if Dallas becomes open to offers.",
        "prediction": "No Dallas-to-Utah trade.",
        "reason": "Internal interest is an early step. I would revise this outlook if Dallas starts receiving offers from Utah and reliable reporting identifies a package.",
        "confidence": "Moderate",
        "sources": [{"name": "NBC Sports", "url": NBC_REPORT}],
        "issued": "2026-11-23", "through": "2026-12-15",
    },
    {
        "name": "Tre Jones", "pid": "4395626", "team": "Chicago Bulls",
        "reported": "Other teams have reportedly called Chicago about Jones.",
        "prediction": "Stays with Chicago.",
        "reason": "Inquiries give Chicago options, but the reporting has yet to name a firm offer. A specific buyer with a package Chicago would value is the signal to watch.",
        "confidence": "Low",
        "sources": [{"name": "NBC Sports", "url": NBC_REPORT}],
        "issued": "2026-11-23", "through": "2026-12-15",
    },
]
TRADE_TERMS = re.compile(
    r"\btrad(?:e|es|ed|ing|able)\b(?!\s+(?:shots|blows|jabs|barbs|baskets)\b)"
    r"|\broster buzz\b|\bfielding offers\b", re.I
)
COMMENTARY = re.compile(r"\b(?:proposal|hypothetical|mock trade|trade ideas?)\b", re.I)
_cache_lock = threading.Lock()
_feeds = {}
_last_attempt = 0.0
_last_success = None
_failed = ()


def _article(title, description, url, published, source, hosts):
    title = (title or "").strip()
    url = urlsplit(url or "")
    if not title or url.hostname not in hosts or not published:
        return None
    if not TRADE_TERMS.search(f"{title} {description or ''}"):
        return None
    if published.tzinfo is None:
        published = published.replace(tzinfo=timezone.utc)
    return {
        "title": title,
        "url": urlunsplit(("https", url.hostname, url.path, "", "")),
        "published": published.astimezone(timezone.utc).isoformat(),
        "source": source,
        "kind": "Analysis" if COMMENTARY.search(title) else "Coverage",
    }


def _espn_feed():
    articles = espn.get("basketball/nba/news", {"limit": 50}, retries=1, timeout=6)["articles"]
    for story in articles:
        if story.get("type") not in ("HeadlineNews", "Story") or not story.get("published"):
            continue
        try:
            published = datetime.fromisoformat(story["published"].replace("Z", "+00:00"))
            web = (story.get("links") or {}).get("web") or {}
            item = _article(story.get("headline"), story.get("description"), web.get("href"),
                            published, "ESPN", {"www.espn.com", "espn.com"})
            if item:
                yield item
        except (TypeError, ValueError):
            continue


def _hoops_feed():
    response = requests.get(HOOPS_FEED, timeout=6)
    response.raise_for_status()
    for story in ElementTree.fromstring(response.content).findall("./channel/item"):
        try:
            published = parsedate_to_datetime(story.findtext("pubDate") or "")
            item = _article(story.findtext("title"), story.findtext("description"), story.findtext("link"),
                            published, "Hoops Wire", {"hoopswire.com", "www.hoopswire.com"})
            if item:
                yield item
        except (TypeError, ValueError):
            continue


def trade_desk():
    """Refresh news on demand and retain the last successful copy of each feed."""
    global _last_attempt, _last_success, _failed
    now = datetime.now(timezone.utc)
    with _cache_lock:
        if not _last_attempt or time.monotonic() - _last_attempt >= (120 if _failed else 900):
            failures = []
            with ThreadPoolExecutor(max_workers=2) as pool:
                sources = {"ESPN": _espn_feed, "Hoops Wire": _hoops_feed}
                feeds = {name: pool.submit(lambda load=load: list(load())) for name, load in sources.items()}
                for name, future in feeds.items():
                    try:
                        _feeds[name] = future.result()
                        _last_success = now.isoformat()
                    except (espn.EspnError, requests.RequestException, ElementTree.ParseError, KeyError, TypeError, ValueError) as exc:
                        log.warning("Trade feed %s failed: %s", name, exc)
                        failures.append(name)
            _last_attempt = time.monotonic()
            _failed = tuple(failures)

        cutoff = (now - timedelta(days=21)).isoformat()
        articles = {
            item["url"]: item for feed in _feeds.values() for item in feed
            if cutoff <= item["published"] <= now.isoformat()
        }
        checked_at, unavailable = _last_success, _failed

    today = now.astimezone(ZoneInfo("America/New_York")).date()
    return {
        "reports": sorted(articles.values(), key=lambda item: item["published"], reverse=True)[:12],
        "forecasts": [
            {**forecast, "active": today <= date.fromisoformat(forecast["through"])}
            for forecast in FORECASTS if date.fromisoformat(forecast["issued"]) <= now.date()
        ],
        "checked_at": checked_at,
        "unavailable_sources": unavailable,
    }
