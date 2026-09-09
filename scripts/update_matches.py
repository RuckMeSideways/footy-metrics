"""
Footy Metrics - core match data bridge (v2)
Runs inside GitHub Actions.

v1 of this script used site.api.espn.com's scoreboard endpoint, which
looked great in manual testing but returned HTTP 403 Forbidden on every
single request when run from GitHub Actions - almost certainly bot
protection blocking cloud/datacenter IPs, since our tool's own fetches
(from a different network) worked fine. This version uses
sports.core.api.espn.com instead - the same subdomain the rugby bridge
used successfully across dozens of real runs with zero blocking issues,
and independently confirmed by community docs as the subdomain meant for
"events" (as opposed to site.api.espn.com's scoreboards/standings).

This trades away v1's convenience of getting everything in one bundled
call, back to the same multi-step pattern that worked for rugby: discover
event IDs for a league/date-range, then per event fetch competitors
(scores), each competitor's team statistics, and the match's play-by-play
(goals/cards/subs) via a dedicated endpoint - soccer has one, rugby didn't.

Team-stats and play-by-play responses are saved close to raw for now
(field names unconfirmed by a live test) - refine once we've seen a real
run's actual output, the same "verify before parsing precisely" approach
that caught every real bug in the rugby build.

Place this file at: scripts/update_matches.py
No secret/API key required.
"""
import json
import time
from pathlib import Path

import requests

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/soccer"
DATA_DIR = Path("data")
MATCHES_FILE = DATA_DIR / "events.json"
TEAM_STATS_DIR = DATA_DIR / "team-stats"
PLAYS_DIR = DATA_DIR / "plays"

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

MAX_NEW_EVENTS_PER_RUN = 150  # same conservative cap that worked for rugby
REQUEST_PAUSE_SECONDS = 0.3
HEADERS = {"User-Agent": "Mozilla/5.0 (FootyMetrics data bridge)"}


def get_json(url, params=None):
    r = requests.get(url.split("?")[0], headers=HEADERS, params=params or {}, timeout=30)
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


def discover_event_ids(slug):
    """Same year-by-year approach that worked for rugby - a single wide
    multi-year date range gets rejected by ESPN outright (confirmed
    during the rugby build), so we go one season-ish window at a time."""
    ids = []
    seen = set()
    for year in [2023, 2024, 2025, 2026, 2027]:
        params = {"limit": 300, "dates": f"{year}0101-{year}1231"}
        try:
            page = get_json(f"{CORE_BASE}/leagues/{slug}/events", params)
        except Exception as e:
            print(f"  ! failed fetching {year} events for {slug}: {e}")
            continue
        items = list(page.get("items", []))
        page_count = page.get("pageCount", 1)
        for p in range(2, page_count + 1):
            try:
                more = get_json(f"{CORE_BASE}/leagues/{slug}/events", {**params, "page": p})
                items.extend(more.get("items", []))
            except Exception as e:
                print(f"  ! failed fetching {year} events page {p} for {slug}: {e}")
            time.sleep(REQUEST_PAUSE_SECONDS)
        for ref in items:
            url = ref.get("$ref", "")
            eid = url.rstrip("/").split("/")[-1].split("?")[0]
            if eid and eid not in seen:
                seen.add(eid)
                ids.append(eid)
        time.sleep(REQUEST_PAUSE_SECONDS)
    return ids


def resolve_score(ref_url):
    try:
        return get_json(ref_url).get("value")
    except Exception:
        return None


def resolve_team_name(cache, ref_url):
    key = ref_url.split("?")[0]
    if key in cache:
        return cache[key]
    try:
        data = get_json(key)
        name = data.get("displayName") or data.get("name")
        cache[key] = name
        return name
    except Exception:
        return None


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    events_by_id = {e["event_id"]: e for e in load_json(MATCHES_FILE, [])}
    team_name_cache = {}
    processed_new = 0

    for slug, league_name in LEAGUES.items():
        if processed_new >= MAX_NEW_EVENTS_PER_RUN:
            break
        print(f"Discovering events for {league_name} ({slug})...")
        event_ids = discover_event_ids(slug)
        print(f"  {len(event_ids)} event(s) found")

        for eid in event_ids:
            already = events_by_id.get(eid)
            if already and already.get("processed") and already.get("home_score") is not None and already.get("away_score") is not None:
                continue  # already fully processed with a real score
            if processed_new >= MAX_NEW_EVENTS_PER_RUN:
                break

            try:
                detail = get_json(f"{CORE_BASE}/leagues/{slug}/events/{eid}")
                comp = (detail.get("competitions") or [{}])[0]
                competitors = comp.get("competitors", [])
                if len(competitors) != 2:
                    continue

                entry = {
                    "event_id": eid,
                    "league_slug": slug,
                    "league_name": league_name,
                    "date": detail.get("date"),
                    "name": detail.get("name"),
                    "processed": False,
                }

                sides_with_stats = 0
                for c in competitors:
                    home_away = c.get("homeAway")
                    team_ref = (c.get("team") or {}).get("$ref", "")
                    score_ref = (c.get("score") or {}).get("$ref", "")
                    stats_ref = (c.get("statistics") or {}).get("$ref", "")
                    team_id = team_ref.rstrip("/").split("/")[-1].split("?")[0]
                    team_name = resolve_team_name(team_name_cache, team_ref) if team_ref else None
                    score = resolve_score(score_ref) if score_ref else None
                    time.sleep(REQUEST_PAUSE_SECONDS)

                    entry[f"{home_away}_team_id"] = team_id
                    entry[f"{home_away}_team_name"] = team_name
                    entry[f"{home_away}_score"] = score

                    if stats_ref:
                        try:
                            stats = get_json(stats_ref)
                            save_json(TEAM_STATS_DIR / f"{eid}_{team_id}.json", stats)
                            sides_with_stats += 1
                        except Exception as e:
                            print(f"  ! failed fetching stats for event {eid} team {team_id}: {e}")
                        time.sleep(REQUEST_PAUSE_SECONDS)

                # Play-by-play (goals, cards, subs) - one call per match,
                # a dedicated endpoint soccer has that rugby didn't.
                try:
                    plays = get_json(f"{CORE_BASE}/leagues/{slug}/events/{eid}/competitions/{eid}/plays", {"limit": 300})
                    save_json(PLAYS_DIR / f"{eid}.json", plays)
                except Exception as e:
                    print(f"  ! failed fetching plays for event {eid}: {e}")
                time.sleep(REQUEST_PAUSE_SECONDS)

                entry["processed"] = True
                entry["has_stats"] = sides_with_stats == 2
                events_by_id[eid] = entry
                processed_new += 1
                if processed_new % 10 == 0:
                    print(f"  ...{processed_new} new event(s) processed this run")
            except Exception as e:
                print(f"  ! failed processing event {eid}: {e}")
            time.sleep(REQUEST_PAUSE_SECONDS)

    save_json(MATCHES_FILE, list(events_by_id.values()))
    print(f"Done. {processed_new} new event(s) processed this run. {len(events_by_id)} total cached.")


if __name__ == "__main__":
    main()
