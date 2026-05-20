#!/usr/bin/env python3
"""
download_data.py — Parrot Binder offline data seeder
=====================================================
Downloads every set and card from the Pokémon TCG API into a local SQLite
database (../data/cards.db).  Run this once on any machine with internet
access, then copy data/cards.db to the Pi.

Usage
-----
    python scripts/download_data.py              # full download
    python scripts/download_data.py --update     # skip sets already in DB
    python scripts/download_data.py --sets sv1 sv2   # specific sets only

Environment
-----------
    POKEMON_TCG_API_KEY   optional — raises rate limit from 1 000 to 20 000
                          req/day.  Get a free key at pokemontcg.io/dashboard

Requirements
------------
    pip install requests tqdm
"""

import argparse
import json
import os
import sqlite3
import sys
import time
from pathlib import Path

try:
    import requests
    from tqdm import tqdm
except ImportError:
    print("Missing dependencies.  Run:  pip install requests tqdm")
    sys.exit(1)

# ── Config ────────────────────────────────────────────────────────────────────
API_BASE    = "https://api.pokemontcg.io/v2"
DB_PATH     = Path(__file__).parent.parent / "data" / "cards.db"
API_KEY     = os.environ.get("POKEMON_TCG_API_KEY", "")
RATE_SLEEP  = 0.12   # ~8 req/s with no key (limit is 1 000/day — be polite)
PAGE_SIZE   = 250

HEADERS = {"X-Api-Key": API_KEY} if API_KEY else {}

# ── DB schema ─────────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS sets (
    id           TEXT PRIMARY KEY,
    name         TEXT NOT NULL,
    series       TEXT,
    release_date TEXT,
    total        INTEGER,
    ptcgo_code   TEXT,
    data         TEXT NOT NULL   -- full JSON blob
);

CREATE TABLE IF NOT EXISTS cards (
    id         TEXT PRIMARY KEY,
    name       TEXT NOT NULL,
    set_id     TEXT NOT NULL,
    number     TEXT,
    rarity     TEXT,
    supertype  TEXT,
    data       TEXT NOT NULL     -- full JSON blob
);

CREATE INDEX IF NOT EXISTS idx_cards_set_id ON cards(set_id);
CREATE INDEX IF NOT EXISTS idx_cards_name   ON cards(LOWER(name));
"""


def open_db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.executescript(SCHEMA)
    con.commit()
    return con


def api_get(path: str, params: dict | None = None) -> dict:
    """GET from the API with simple retry logic."""
    url = f"{API_BASE}/{path}"
    for attempt in range(4):
        try:
            r = requests.get(url, params=params, headers=HEADERS, timeout=30)
            if r.status_code == 429:
                wait = int(r.headers.get("Retry-After", 60))
                print(f"\n  Rate-limited — waiting {wait}s …", flush=True)
                time.sleep(wait)
                continue
            r.raise_for_status()
            return r.json()
        except requests.RequestException as exc:
            if attempt == 3:
                raise
            print(f"\n  Retry {attempt+1}/3 after error: {exc}", flush=True)
            time.sleep(5 * (attempt + 1))
    return {}


# ── Sets ──────────────────────────────────────────────────────────────────────

def fetch_all_sets() -> list[dict]:
    print("Fetching sets list …", flush=True)
    data = api_get("sets", {"orderBy": "-releaseDate", "pageSize": PAGE_SIZE})
    sets = data.get("data", [])
    # Filter out Online/POP sets (same logic as app)
    sets = [s for s in sets if not any(
        kw in (s.get("series", "") or "")
        for kw in ("Online", "POP Series")
    )]
    print(f"  Found {len(sets)} sets")
    return sets


def upsert_sets(con: sqlite3.Connection, sets: list[dict]) -> None:
    rows = [
        (
            s["id"],
            s.get("name", ""),
            s.get("series", ""),
            s.get("releaseDate", ""),
            s.get("total", 0),
            s.get("ptcgoCode", ""),
            json.dumps(s),
        )
        for s in sets
    ]
    con.executemany(
        "INSERT OR REPLACE INTO sets VALUES (?,?,?,?,?,?,?)", rows
    )
    con.commit()


# ── Cards ─────────────────────────────────────────────────────────────────────

CARD_SELECT = (
    "id,name,images,set,types,rarity,cardmarket,hp,attacks,abilities,"
    "weaknesses,resistances,retreatCost,legalities,regulationMark,number,"
    "artist,flavorText,evolvesFrom,supertype,subtypes,nationalPokedexNumbers,"
    "tcgplayer"
)


def fetch_cards_for_set(set_id: str) -> list[dict]:
    params = {
        "q":        f"set.id:{set_id}",
        "pageSize": PAGE_SIZE,
        "orderBy":  "number",
        "select":   CARD_SELECT,
    }
    page, cards = 1, []
    while True:
        params["page"] = page
        data = api_get("cards", params)
        batch = data.get("data", [])
        cards.extend(batch)
        total = data.get("totalCount", len(cards))
        if len(cards) >= total or not batch:
            break
        page += 1
        time.sleep(RATE_SLEEP)
    return cards


def upsert_cards(con: sqlite3.Connection, cards: list[dict]) -> None:
    rows = [
        (
            c["id"],
            c.get("name", ""),
            c.get("set", {}).get("id", ""),
            c.get("number", ""),
            c.get("rarity", ""),
            c.get("supertype", ""),
            json.dumps(c),
        )
        for c in cards
    ]
    con.executemany(
        "INSERT OR REPLACE INTO cards VALUES (?,?,?,?,?,?,?)", rows
    )
    con.commit()


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Download Pokémon TCG data")
    parser.add_argument("--update",  action="store_true",
                        help="Skip sets already present in the DB")
    parser.add_argument("--sets", nargs="*", metavar="SET_ID",
                        help="Only download specific set IDs (e.g. sv1 sv2)")
    args = parser.parse_args()

    con = open_db()

    # ── Fetch sets ────────────────────────────────────────────────────────────
    sets = fetch_all_sets()

    if args.sets:
        sets = [s for s in sets if s["id"] in args.sets]
        print(f"  Filtered to {len(sets)} requested sets")

    if args.update:
        existing = {row[0] for row in con.execute("SELECT id FROM sets")}
        sets = [s for s in sets if s["id"] not in existing]
        print(f"  {len(sets)} new/updated sets to download")

    upsert_sets(con, sets)
    time.sleep(RATE_SLEEP)

    if not sets:
        print("Nothing to do — database is up to date.")
        con.close()
        return

    # ── Fetch cards per set ───────────────────────────────────────────────────
    print(f"\nDownloading cards for {len(sets)} sets …")
    total_cards = 0

    for s in tqdm(sets, unit="set"):
        tqdm.write(f"  {s['id']:10s}  {s['name']}")
        try:
            cards = fetch_cards_for_set(s["id"])
            upsert_cards(con, cards)
            total_cards += len(cards)
            time.sleep(RATE_SLEEP)
        except Exception as exc:
            tqdm.write(f"  ERROR on {s['id']}: {exc}")

    sets_count = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    cards_count = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]

    print(f"\n✓  Done — DB: {sets_count} sets, {cards_count} total cards")
    print(f"   Wrote {total_cards} cards in this run")
    print(f"   Database: {DB_PATH}")
    con.close()


if __name__ == "__main__":
    main()
