"""
Footy Metrics - standings bridge
Runs inside GitHub Actions.

Unlike scores/events, soccer standings are only documented on ESPN's
"site" family domains (site.api.espn.com, site.web.api.espn.com) - not
on sports.core.api.espn.com, the domain that's worked reliably for
everything else so far. Since site.api.espn.com already blocked every
request outright when we tried it for scores (HTTP 403, likely bot
protection against cloud/datacenter IPs), this script tries it first and
automatically falls back to site.web.api.espn.com if it fails - these
are documented as returning identical data, and being a technically
separate service, it may not share the same blocking.

If BOTH domains get blocked when this actually runs, that's a real,
useful result too - it tells us GitHub Actions IPs are blocked
site-wide across the whole "site" family, not just one domain, which
would rule out this approach entirely rather than requiring more
guessing.

Standings data is saved close to raw for now (exact field names
unconfirmed by a live test) - same "verify before parsing precisely"
approach used throughout.

Place this file at: scripts/update_standings.py
No secret/API key required.
"""
import json
import time
from pathlib import Path

import requests

DATA_DIR = Path("data/standings")
HEADERS = {"User-Agent": "Mozilla/5.0 (FootyMetrics data bridge)"}
REQUEST_PAUSE_SECONDS = 0.5

LEAGUES = {
    "eng.1": "Premier League",
    "esp.1": "La Liga",
    "ger.1": "Bundesliga",
    "ita.1": "Serie A",
    "fra.1": "Ligue 1",
    "uefa.champions": "Champions League",
    "fifa.world": "World Cup",
    "rsa.1": "South African Premiership",
}

DOMAINS = [
    "https://site.api.espn.com/apis/v2/sports/soccer/{slug}/standings",
    "https://site.web.api.espn.com/apis/v2/sports/soccer/{slug}/standings",
]


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def fetch_standings(slug):
    """Tries each domain in order, returns (data, domain_used) or (None, None)."""
    for template in DOMAINS:
        url = template.format(slug=slug)
        try:
            r = requests.get(url, headers=HEADERS, timeout=30)
            if r.status_code != 200:
                print(f"    {url} -> HTTP {r.status_code}")
                continue
            data = r.json()
            if not data:
                print(f"    {url} -> 200 OK but empty response")
                continue
            return data, url
        except Exception as e:
            print(f"    {url} -> failed: {e}")
    return None, None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    results = {}

    for slug, league_name in LEAGUES.items():
        print(f"Fetching standings for {league_name} ({slug})...")
        data, domain_used = fetch_standings(slug)
        if data is None:
            print(f"  ! both domains failed for {slug} - skipping")
            results[slug] = "failed"
            time.sleep(REQUEST_PAUSE_SECONDS)
            continue
        save_json(DATA_DIR / f"{slug}.json", data)
        print(f"  success via {domain_used}")
        results[slug] = "ok"
        time.sleep(REQUEST_PAUSE_SECONDS)

    print("\nSummary:")
    for slug, status in results.items():
        print(f"  {LEAGUES[slug]}: {status}")


if __name__ == "__main__":
    main()
