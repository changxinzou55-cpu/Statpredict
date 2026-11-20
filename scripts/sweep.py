"""Open every page of the running app in Chrome and report anything that fails.

Usage: python3 -m scripts.sweep [base_url]   (needs Chrome's CDP endpoint on localhost:29229)
"""
import json
import sys
import urllib.request

from playwright.sync_api import sync_playwright

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://localhost:8101"


def get(path):
    return json.load(urllib.request.urlopen(f"{BASE}/api/{path}", timeout=30))


def routes():
    day = get("nba/day")
    out = ["/nba/games", "/nba/players", "/nba/teams", "/nba/playoffs", "/nba/awards", "/nba/accuracy", "/football"]
    days = [d["day"] for d in day["days"]]
    i = days.index(day["day"])
    for d in days[max(i - 3, 0):i + 4] + days[:1] + days[-1:]:
        out += [f"/nba/games?day={d}", f"/nba/players?day={d}"]
        for g in get(f"nba/day?day={d}")["games"][:3]:
            out.append(f"/nba/game/{g['id']}")
    players = get("nba/players")["players"]
    out += [f"/nba/player/{p['id']}" for p in players[::max(len(players) // 40, 1)]]
    for a in get("nba/awards")["awards"].values():
        out += [f"/nba/player/{r['pid']}" for r in a[:2]]
    for lg in get("football/leagues")["leagues"]:
        v = get(f"football/league/{lg['id']}")
        out.append(f"/football?league={lg['id']}")
        out += [f"/football/match/{m['id']}" for m in v["upcoming"][:2] + v["recent"][:2]]
    return list(dict.fromkeys(out))


def main():
    bad = []
    with sync_playwright() as pw:
        browser = pw.chromium.connect_over_cdp("http://localhost:29229")
        for size in ((1280, 900), (390, 800)):
            page = browser.contexts[0].new_page()
            page.set_viewport_size({"width": size[0], "height": size[1]})
            page.set_default_timeout(15000)
            errors = []
            page.on("pageerror", lambda e: errors.append(f"js: {e}"))
            page.on("console", lambda m: errors.append(f"console: {m.text}") if m.type == "error" else None)
            page.on("response", lambda r: errors.append(f"http {r.status}: {r.url}") if r.status >= 400 else None)
            page.goto(BASE + "/")
            todo = routes()
            print(f"{size[0]}px: {len(todo)} pages to check", flush=True)
            for r in todo:
                errors.clear()
                page.evaluate("h => { location.hash = h }", "#" + r)
                ok = False
                for _ in range(60):   # poll from Python: background tabs throttle in-page waits
                    page.wait_for_timeout(150)
                    if page.evaluate("() => document.querySelector('#bar').hidden && !!document.querySelector('main h1, main .empty')"):
                        ok = True
                        break
                if not ok:
                    errors.append("timed out waiting for the page")
                page.wait_for_timeout(250)
                text = page.inner_text("main")
                for needle in ("Could not load", "undefined", "NaN", "[object", "null"):
                    if needle in text:
                        errors.append(f"text contains {needle!r}: {text[max(text.find(needle) - 40, 0):text.find(needle) + 40]!r}")
                wide = page.evaluate("() => document.documentElement.scrollWidth - document.documentElement.clientWidth")
                if wide > 2:
                    errors.append(f"page scrolls sideways by {wide}px")
                if errors:
                    bad.append((size[0], r, list(dict.fromkeys(errors))[:4]))
            print(f"{size[0]}px: checked {len(todo)} pages", flush=True)
            page.close()
    for width, r, errs in bad:
        print(f"FAIL {width}px {r}")
        for e in errs:
            print("   ", e[:220])
    print("problems:", len(bad))
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
