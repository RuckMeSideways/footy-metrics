"""
PILOT / DIAGNOSTIC SCRIPT - not part of the regular data pipeline, not
scheduled to run automatically. Run manually, once, to answer three open
questions before building player tracking for Footy Metrics:

  1. Is the per-player statistics endpoint (roster/{id}/statistics, whose
     $ref links we already saw embedded in team-stats files) per-match or
     season-cumulative? (Team-level stats turned out to be
     season-cumulative, not per-match - worth checking whether the same
     is true one level down before we build anything on top of it.)
  2. Is there a single "give me this player's full career" endpoint, the
     way rugbypy's fetch_player_stats(no date) was for rugby? Or does
     ESPN's soccer API scope players per-season, meaning transfer
     detection would need a different approach (e.g. diffing team
     rosters across seasons)?
  3. Does a team-roster-by-season endpoint exist, as a fallback path to
     detect transfers even if there's no direct player-history endpoint?

Prints everything to the Action log - saves nothing to the repo. Delete
this file once we've learned what we need from it.

Place this file at: scripts/pilot_players.py
No secret/API key required. Reads an existing data/team-stats/*.json
file to find real athlete/team/season/league identifiers to test with,
rather than guessing them.
"""
import glob
import json
import time

import requests

CORE_BASE = "https://sports.core.api.espn.com/v2/sports/soccer"
HEADERS = {"User-Agent": "Mozilla/5.0 (FootyMetrics data bridge)"}


def get_json(url, params=None):
    r = requests.get(url.split("?")[0], headers=HEADERS, params=params or {}, timeout=30)
    r.raise_for_status()
    return r.json()


def find_sample_refs():
    """Pull real athlete/team/season/league identifiers straight out of
    an existing team-stats file, rather than guessing them."""
    files = glob.glob("data/team-stats/*.json")
    print(f"Found {len(files)} cached team-stats file(s) to sample from.")
    if not files:
        print("No team-stats files found - run update_matches.py first.")
        return None
    with open(files[0]) as f:
        data = json.load(f)
    team_ref = (data.get("team") or {}).get("$ref", "")
    cats = ((data.get("splits") or {}).get("categories")) or []
    athlete_entry = None
    for cat in cats:
        for a in cat.get("athletes") or []:
            if a.get("athlete", {}).get("$ref") and a.get("statistics", {}).get("$ref"):
                athlete_entry = a
                break
        if athlete_entry:
            break
    if not athlete_entry:
        print("Could not find any athlete entries in the sampled file.")
        return None
    print(f"Sampled from: {files[0]}")
    print(f"  team ref: {team_ref}")
    print(f"  athlete ref: {athlete_entry['athlete']['$ref']}")
    print(f"  athlete statistics ref: {athlete_entry['statistics']['$ref']}")
    return {
        "team_ref": team_ref,
        "athlete_ref": athlete_entry["athlete"]["$ref"],
        "athlete_stats_ref": athlete_entry["statistics"]["$ref"],
    }


def main():
    refs = find_sample_refs()
    if not refs:
        return

    print("\n=== TEST 1: per-player statistics - per-match or season-cumulative? ===")
    try:
        stats = get_json(refs["athlete_stats_ref"])
        cats = ((stats.get("splits") or {}).get("categories")) or []
        print(f"  splits.name: {(stats.get('splits') or {}).get('name')}")
        for cat in cats[:2]:
            print(f"  category '{cat.get('name')}':")
            for s in (cat.get("stats") or [])[:8]:
                print(f"    {s.get('name')}: {s.get('displayValue')}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\n=== TEST 2: athlete identity endpoint - what does it contain? ===")
    try:
        athlete = get_json(refs["athlete_ref"])
        print(f"  top-level keys: {list(athlete.keys())}")
        print(f"  name: {athlete.get('displayName') or athlete.get('fullName')}")
        print(f"  team ref on athlete record: {(athlete.get('team') or {}).get('$ref')}")
        # look for anything hinting at career/multi-season history
        for key in ("statistics", "eventLog", "teams", "seasons"):
            if key in athlete:
                print(f"  found '{key}' key: {json.dumps(athlete[key], default=str)[:300]}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\n=== TEST 3: non-season-scoped athlete endpoint (if one exists) ===")
    # athlete_ref looks like .../leagues/eng.1/seasons/2022/athletes/{id}
    # - try stripping the season scoping to see if a canonical, cross-season
    # athlete record exists at .../leagues/eng.1/athletes/{id}
    try:
        athlete_id = refs["athlete_ref"].rstrip("/").split("/")[-1].split("?")[0]
        league_slug = refs["athlete_ref"].split("/leagues/")[1].split("/")[0]
        alt_url = f"{CORE_BASE}/leagues/{league_slug}/athletes/{athlete_id}"
        athlete = get_json(alt_url)
        print(f"  {alt_url} -> success")
        print(f"  top-level keys: {list(athlete.keys())}")
        for key in ("statistics", "eventLog", "teams", "seasons"):
            if key in athlete:
                print(f"  found '{key}' key: {json.dumps(athlete[key], default=str)[:300]}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\n=== TEST 4: team roster by season (fallback path for transfer detection) ===")
    try:
        team_id = refs["team_ref"].rstrip("/").split("/")[-1].split("?")[0]
        league_slug = refs["team_ref"].split("/leagues/")[1].split("/")[0]
        season = refs["team_ref"].split("/seasons/")[1].split("/")[0]
        roster_url = f"{CORE_BASE}/leagues/{league_slug}/seasons/{season}/teams/{team_id}/roster"
        roster = get_json(roster_url)
        print(f"  {roster_url} -> success")
        items = roster.get("items") or roster.get("athletes") or []
        print(f"  {len(items)} roster entr(y/ies), top-level keys: {list(roster.keys())}")
        if items:
            print(f"  first entry: {json.dumps(items[0], default=str)[:300]}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\n=== TEST 5: the 'seasons' link found on the athlete record - the key one ===")
    try:
        athlete_id = refs["athlete_ref"].rstrip("/").split("/")[-1].split("?")[0]
        league_slug = refs["athlete_ref"].split("/leagues/")[1].split("/")[0]
        seasons_url = f"{CORE_BASE}/leagues/{league_slug}/athletes/{athlete_id}/seasons"
        seasons = get_json(seasons_url, {"limit": 50})
        print(f"  {seasons_url} -> success")
        print(f"  top-level keys: {list(seasons.keys())}")
        items = seasons.get("items", [])
        print(f"  {len(items)} season(s) found")
        if items:
            first_item = items[0]
            print(f"  first item raw: {json.dumps(first_item, default=str)[:400]}")
            if isinstance(first_item, dict) and "$ref" in first_item and len(first_item) == 1:
                print("  (item is a bare $ref - fetching it to see the real shape)")
                followed = get_json(first_item["$ref"])
                print(f"  followed keys: {list(followed.keys())}")
                print(f"  followed content: {json.dumps(followed, default=str)[:500]}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\n=== TEST 6: eventLog - this player's match list for the season ===")
    try:
        athlete_id = refs["athlete_ref"].rstrip("/").split("/")[-1].split("?")[0]
        league_slug = refs["athlete_ref"].split("/leagues/")[1].split("/")[0]
        season = refs["athlete_ref"].split("/seasons/")[1].split("/")[0]
        eventlog_url = f"{CORE_BASE}/leagues/{league_slug}/seasons/{season}/athletes/{athlete_id}/eventlog"
        eventlog = get_json(eventlog_url, {"limit": 50})
        print(f"  {eventlog_url} -> success")
        print(f"  top-level keys: {list(eventlog.keys())}")
        events = (eventlog.get("events") or {}).get("items", []) if isinstance(eventlog.get("events"), dict) else eventlog.get("items", [])
        print(f"  {len(events)} event(s) found")
        if events:
            print(f"  first event raw: {json.dumps(events[0], default=str)[:400]}")
    except Exception as e:
        print(f"  ! failed: {e}")

    print("\nDone. Review the output above before deciding what to build next.")


if __name__ == "__main__":
    main()
