"""
Footy Metrics - core match data bridge
Runs inside GitHub Actions. Pulls match data for 8 major competitions from
ESPN's soccer scoreboard endpoint, which (unlike rugby's core API) already
bundles scores, team stats, and goal/card events into a single response
per date range - no separate per-team "statistics" fetch needed.

This is a deliberately modest first version: the fields we're confident
about (scores, teams, completion status) are parsed cleanly; the richer
team-stats/event blocks are saved mostly as-is rather than guessed at, so
we can refine the parsing once we've seen a real run's actual output -
exactly the same "verify before refining" approach that caught every real
bug in the rugby build.

Place this file at: scripts/update_matches.py
No secret/API key required.
"""
import json
import time
from datetime import datetime, timedelta
from pathlib import Path

import requests

BASE = "https://site.api.espn.com/apis/site/v2/sports/soccer"
DATA_DIR = Path("data")
MATCHES_FILE = DATA_DIR / "matches.json"
TEAMS_FILE = DATA_DIR / "teams.json"

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

# Wide net rather than trying to guess each league's exact season
# calendar (they differ - European leagues run Aug-May, MLS-style
# calendars differ again, World Cup is its own thing entirely).
DAYS_BACK = 730       # ~2 years of history
DAYS_FORWARD = 90     # a little ahead, for scheduled fixtures
CHUNK_DAYS = 30        # one request per ~30-day window, per league
REQUEST_PAUSE_SECONDS = 0.3
HEADERS = {"User-Agent": "Mozilla/5.0 (FootyMetrics data bridge)"}

MAX_NEW_PER_RUN = 2000  # generous - this endpoint is far cheaper per match than rugby's was


def get_json(url, params=None):
    r = requests.get(url, headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def save_json(path: Path, obj) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w") as f:
        json.dump(obj, f, separators=(",", ":"))


def load_json(path: Path, default):
    if path.exists():
        try:
            with open(path) as f:
                return json.load(f)
        except Exception:
            return default
    return default


def date_chunks():
    today = datetime.now().date()
    start = today - timedelta(days=DAYS_BACK)
    end = today + timedelta(days=DAYS_FORWARD)
    cur = start
    while cur <= end:
        chunk_end = min(cur + timedelta(days=CHUNK_DAYS - 1), end)
        yield cur.strftime("%Y%m%d"), chunk_end.strftime("%Y%m%d")
        cur = chunk_end + timedelta(days=1)


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    matches_by_id = {m["id"]: m for m in load_json(MATCHES_FILE, [])}
    teams = load_json(TEAMS_FILE, {})  # team_id -> name

    new_count = 0
    for slug, league_name in LEAGUES.items():
        print(f"Fetching {league_name} ({slug})...")
        league_new = 0
        for start, end in date_chunks():
            if new_count >= MAX_NEW_PER_RUN:
                break
            try:
                data = get_json(f"{BASE}/{slug}/scoreboard", {"dates": f"{start}-{end}", "limit": 200})
            except Exception as e:
                print(f"  ! failed fetching {slug} {start}-{end}: {e}")
                continue

            for event in data.get("events", []):
                eid = event.get("id")
                if not eid or eid in matches_by_id:
                    continue  # already have this one from a previous run

                comp = (event.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                if len(competitors) != 2:
                    continue

                status = (event.get("status") or {}).get("type", {})
                entry = {
                    "id": eid,
                    "league_slug": slug,
                    "league_name": league_name,
                    "date": event.get("date"),
                    "name": event.get("name"),
                    "completed": bool(status.get("completed")),
                    "status_description": status.get("description"),
                }

                for c in competitors:
                    home_away = c.get("homeAway")
                    team = c.get("team", {})
                    team_id = team.get("id")
                    team_name = team.get("displayName") or team.get("name")
                    if team_id and team_name:
                        teams[team_id] = team_name
                    score = c.get("score")
                    entry[f"{home_away}_team_id"] = team_id
                    entry[f"{home_away}_team_name"] = team_name
                    entry[f"{home_away}_score"] = float(score) if score not in (None, "") else None
                    # Saved mostly raw for now - a list of {name, displayValue}
                    # stat entries bundled directly in the scoreboard response.
                    # We'll refine exact field parsing once we've seen a real
                    # run's actual output.
                    entry[f"{home_away}_stats_raw"] = c.get("statistics", [])

                # Goal/card events, if present - also saved close to raw for
                # the same reason.
                entry["details_raw"] = comp.get("details", [])

                matches_by_id[eid] = entry
                league_new += 1
                new_count += 1

            time.sleep(REQUEST_PAUSE_SECONDS)
            if new_count >= MAX_NEW_PER_RUN:
                break

        print(f"  {league_new} new match(es) for {league_name}")
        if new_count >= MAX_NEW_PER_RUN:
            print("Budget for this run reached - remaining leagues/dates will be picked up next run.")
            break

    save_json(MATCHES_FILE, list(matches_by_id.values()))
    save_json(TEAMS_FILE, teams)
    print(f"Done. {new_count} new match(es) this run. {len(matches_by_id)} total cached. {len(teams)} known team(s).")


if __name__ == "__main__":
    main()
