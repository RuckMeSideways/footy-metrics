"""
Footy Metrics - player tracking bridge (v2)
Runs inside GitHub Actions.

v1 guessed at the shape of eventLog's "teams" field and got it wrong -
a real run confirmed it's a DICT KEYED BY TEAM ID (e.g. {"1068":
{"id":"1068","team":{"$ref":...}}, "9812": {...}}), not a list or single
object as originally guessed. This version parses the confirmed real
shape. It also surfaces something genuinely useful the wrong guess would
have hidden: a player can have more than one team ID within a single
season (a mid-season loan or transfer) - we keep every one rather than
collapsing to just one team per season.

Built from a two-round pilot (scripts/pilot_players.py) plus this one
real-run correction - the same "verify, build, then fix from real
output" cycle used throughout every bridge in this project.

Incremental and capped, same pattern as every other bridge here.

Place this file at: scripts/update_players.py
No secret/API key required. Reads data/team-stats/*.json (from
update_matches.py) to find real athlete IDs to look up.
"""
import glob
import json
import time
from pathlib import Path

import requests

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/soccer"
DATA_DIR = Path("data/players")
INDEX_FILE = DATA_DIR / "index.json"
INVALID_FILE = DATA_DIR / "invalid.json"

MAX_NEW_PLAYERS_PER_RUN = 100
MAX_SEASONS_PER_PLAYER = 6  # caps cost per player - most careers fit well within this
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


def collect_candidate_athletes():
    """Every athlete $ref that appears in any cached team-stats file,
    deduped, with the league slug extracted from the ref URL itself."""
    seen = {}
    for path in glob.glob("data/team-stats/*.json"):
        try:
            with open(path) as f:
                data = json.load(f)
        except Exception:
            continue
        cats = ((data.get("splits") or {}).get("categories")) or []
        for cat in cats:
            for a in cat.get("athletes") or []:
                ref = (a.get("athlete") or {}).get("$ref", "")
                if not ref or "/leagues/" not in ref:
                    continue
                athlete_id = ref.rstrip("/").split("/")[-1].split("?")[0]
                league_slug = ref.split("/leagues/")[1].split("/")[0]
                seen[athlete_id] = league_slug
    return seen


def extract_teams_from_eventlog(eventlog, team_name_cache):
    """Confirmed real shape (from a live run): "teams" is a dict KEYED BY
    TEAM ID, e.g. {"1068": {"id":"1068","team":{"$ref":...}}, "9812": {...}}
    - not a list, not a single object, as originally guessed. A player can
    have more than one team ID within a single season (a mid-season loan
    or transfer), which is genuinely useful signal, not noise - we surface
    every one rather than collapsing to just one team per season."""
    teams = eventlog.get("teams")
    if not isinstance(teams, dict):
        return []  # unexpected shape - nothing usable this time
    results = []
    for team_id, entry in teams.items():
        if not isinstance(entry, dict):
            continue
        team_name = None
        team_ref = (entry.get("team") or {}).get("$ref", "")
        if team_ref:
            key = team_ref.split("?")[0]
            if key in team_name_cache:
                team_name = team_name_cache[key]
            else:
                try:
                    resolved = get_json(key)
                    team_name = resolved.get("displayName") or resolved.get("name")
                    team_name_cache[key] = team_name
                    time.sleep(REQUEST_PAUSE_SECONDS)
                except Exception:
                    pass
        results.append({"team_id": entry.get("id") or team_id, "team_name": team_name})
    return results


def build_player_record(athlete_id, league_slug, team_name_cache):
    identity = get_json(f"{CORE_BASE}/leagues/{league_slug}/athletes/{athlete_id}")
    time.sleep(REQUEST_PAUSE_SECONDS)
    name = identity.get("displayName") or identity.get("fullName")
    position = (identity.get("position") or {}).get("abbreviation") or (identity.get("position") or {}).get("name")

    seasons_ref = (identity.get("seasons") or {}).get("$ref")
    season_years = []
    if seasons_ref:
        try:
            seasons_data = get_json(seasons_ref, {"limit": 50})
            for item in seasons_data.get("items", [])[:MAX_SEASONS_PER_PLAYER]:
                ref = item.get("$ref", "")
                year = ref.rstrip("/").split("/")[-1].split("?")[0]
                if year.isdigit():
                    season_years.append(year)
        except Exception as e:
            print(f"    ! failed fetching seasons list for {athlete_id}: {e}")
        time.sleep(REQUEST_PAUSE_SECONDS)

    # stints: a chronological sequence of (team, season) entries. Within
    # one season a player can have multiple teams (loan/transfer) - we
    # can't perfectly order those without match-level dates, so they're
    # appended in the order the API lists them, which is the best signal
    # available at this level of detail.
    stints = []
    for year in sorted(season_years):
        try:
            eventlog = get_json(f"{CORE_BASE}/leagues/{league_slug}/seasons/{year}/athletes/{athlete_id}/eventlog", {"limit": 100})
            teams_this_season = extract_teams_from_eventlog(eventlog, team_name_cache)
        except Exception as e:
            print(f"    ! failed fetching eventlog for {athlete_id} season {year}: {e}")
            teams_this_season = []
        time.sleep(REQUEST_PAUSE_SECONDS)

        for t in teams_this_season:
            if stints and stints[-1]["team_id"] == t["team_id"]:
                stints[-1]["end_season"] = year
            else:
                stints.append({"team_id": t["team_id"], "team_name": t["team_name"], "start_season": year, "end_season": year})

    current = stints[-1] if stints else None
    return {
        "id": athlete_id,
        "name": name,
        "position": position,
        "current_team_id": current["team_id"] if current else None,
        "current_team_name": current["team_name"] if current else None,
        "stints": stints,
    }


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    invalid_ids = set(load_json(INVALID_FILE, []))
    index = load_json(INDEX_FILE, [])
    index_ids = {p["id"] for p in index}
    team_name_cache = {}

    candidates = collect_candidate_athletes()
    print(f"Found {len(candidates)} candidate athlete(s) from cached team-stats files.")

    processed_new = 0
    for athlete_id, league_slug in candidates.items():
        if processed_new >= MAX_NEW_PLAYERS_PER_RUN:
            break
        if athlete_id in invalid_ids:
            continue
        out_path = DATA_DIR / f"{athlete_id}.json"
        if out_path.exists():
            # Only truly skip if the existing record actually has real
            # stints - a record left over from the earlier broken parser
            # (empty stints, with the old "unparsed_teams_by_season"
            # fallback field present) gets automatically reprocessed
            # instead of requiring the old files to be deleted by hand.
            existing = load_json(out_path, {})
            if existing.get("stints") or "unparsed_teams_by_season" not in existing:
                continue

        try:
            record = build_player_record(athlete_id, league_slug, team_name_cache)
            save_json(out_path, record)
            if athlete_id not in index_ids:
                index.append({"id": athlete_id, "name": record.get("name")})
                index_ids.add(athlete_id)
            processed_new += 1
            if processed_new % 10 == 0:
                print(f"  ...{processed_new} new player(s) processed this run")
        except Exception as e:
            print(f"  ! failed processing athlete {athlete_id}: {e}")
            invalid_ids.add(athlete_id)
            processed_new += 1

    save_json(INDEX_FILE, index)
    save_json(INVALID_FILE, sorted(invalid_ids))
    print(f"Done. {processed_new} new player(s) processed this run. {len(index)} total in index.")


if __name__ == "__main__":
    main()
