"""
Footy Metrics - player tracking bridge
Runs inside GitHub Actions.

Built from a two-round pilot (scripts/pilot_players.py) that confirmed:
  - A league-scoped, non-season-specific athlete identity endpoint exists
    (.../leagues/{league}/athletes/{id}) - a stable per-player record.
  - Each athlete has a "seasons" link listing every season they have a
    record for in that league.
  - Each season has an "eventLog" link listing that player's matches for
    the season, which itself carries a "teams" field - the mechanism we
    use to detect a transfer (team affiliation changing between seasons,
    or even within one season for a mid-season move).

One thing the pilot could NOT confirm precisely: the exact shape of that
"teams" field. Rather than guess and risk silently losing data, team
extraction below tries a few reasonable shapes and falls back to saving
the raw value if none match - so nothing is lost even if this needs
refining once we see real output.

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


def extract_team_from_eventlog(eventlog):
    """The 'teams' field's exact shape wasn't confirmed by the pilot -
    try a few reasonable possibilities, and if none match, keep the raw
    value so nothing is silently lost."""
    teams = eventlog.get("teams")
    if teams is None:
        return None, None, teams

    # Possibility 1: a single {$ref, id, displayName} dict
    if isinstance(teams, dict) and ("$ref" in teams or "displayName" in teams):
        return teams.get("id"), teams.get("displayName") or teams.get("name"), teams

    # Possibility 2: a list of team dicts/refs (mid-season transfer would
    # show more than one)
    if isinstance(teams, list) and teams:
        first = teams[0]
        if isinstance(first, dict):
            team_id = first.get("id")
            team_name = first.get("displayName") or first.get("name")
            if not team_name and "$ref" in first:
                try:
                    resolved = get_json(first["$ref"])
                    team_id = resolved.get("id")
                    team_name = resolved.get("displayName") or resolved.get("name")
                    time.sleep(REQUEST_PAUSE_SECONDS)
                except Exception:
                    pass
            return team_id, team_name, teams

    return None, None, teams


def build_player_record(athlete_id, league_slug):
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

    stints = []
    raw_teams_by_season = {}
    for year in sorted(season_years):
        try:
            eventlog = get_json(f"{CORE_BASE}/leagues/{league_slug}/seasons/{year}/athletes/{athlete_id}/eventlog", {"limit": 100})
            team_id, team_name, raw = extract_team_from_eventlog(eventlog)
            if team_id or team_name:
                if stints and stints[-1]["team_id"] == team_id:
                    stints[-1]["end_season"] = year
                else:
                    stints.append({"team_id": team_id, "team_name": team_name, "start_season": year, "end_season": year})
            else:
                raw_teams_by_season[year] = raw  # couldn't parse - keep raw so nothing's lost
        except Exception as e:
            print(f"    ! failed fetching eventlog for {athlete_id} season {year}: {e}")
        time.sleep(REQUEST_PAUSE_SECONDS)

    current = stints[-1] if stints else None
    record = {
        "id": athlete_id,
        "name": name,
        "position": position,
        "current_team_id": current["team_id"] if current else None,
        "current_team_name": current["team_name"] if current else None,
        "stints": stints,
    }
    if raw_teams_by_season:
        record["unparsed_teams_by_season"] = raw_teams_by_season
    return record


def main():
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    invalid_ids = set(load_json(INVALID_FILE, []))
    index = load_json(INDEX_FILE, [])
    index_ids = {p["id"] for p in index}

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
            continue  # already processed in a previous run

        try:
            record = build_player_record(athlete_id, league_slug)
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
