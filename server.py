#!/usr/bin/env python3
"""
server.py — Parrot Binder local API + PWA server
=================================================
Serves the PWA (index.html, sw.js, etc.) AND a local REST API that mirrors
the subset of api.pokemontcg.io/v2 that the app actually uses.

Run on a Raspberry Pi Zero 2 (or any machine):
    python server.py                # listens on 0.0.0.0:5000
    python server.py --port 8080    # custom port
    python server.py --host 0.0.0.0 --port 80   # port 80 (needs sudo / authbind)

Requires cards.db created by scripts/download_data.py.

Endpoints mirrored from api.pokemontcg.io/v2
    GET /v2/sets
    GET /v2/cards
    GET /v2/cards/<id>
    GET /img/<card_id>/<filename>   local image if downloaded, else 302 to CDN

Requirements
------------
    pip install flask
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
from pathlib import Path
from threading import local as thread_local
from typing import Any

from flask import Flask, Response, jsonify, redirect, request, send_file, send_from_directory

# ── Paths ─────────────────────────────────────────────────────────────────────
BASE_DIR  = Path(__file__).parent
DB_PATH   = BASE_DIR / "data" / "cards.db"
IMG_DIR   = BASE_DIR / "data" / "images"
STATIC_DIR = BASE_DIR          # index.html lives here

app = Flask(__name__, static_folder=None)

# ── Per-thread DB connections ─────────────────────────────────────────────────
_local = thread_local()

def get_db() -> sqlite3.Connection:
    if not hasattr(_local, "con"):
        _local.con = sqlite3.connect(DB_PATH, check_same_thread=False)
        _local.con.row_factory = sqlite3.Row
    return _local.con


# ── Query parser ──────────────────────────────────────────────────────────────
# Supports the subset of pokemontcg.io query syntax the app sends:
#   set.id:sv1
#   name:*charizard*
#   name:"Charizard ex"
#   id:sv1-1 OR id:sv1-2 OR ...

def _parse_query(q: str) -> tuple[str, list]:
    """
    Returns (WHERE clause, bind params).
    Returns ("1", []) if query is empty or unrecognised.
    """
    q = q.strip()
    if not q:
        return "1", []

    clauses: list[str] = []
    params:  list[Any] = []

    # ── Multi-ID OR list:  id:a OR id:b OR id:c ──────────────────────────────
    if re.match(r'^id:.+( OR id:.+)+$', q, re.IGNORECASE):
        ids = [part.split(":", 1)[1].strip() for part in re.split(r'\s+OR\s+', q, flags=re.IGNORECASE)]
        placeholders = ",".join("?" * len(ids))
        return f"c.id IN ({placeholders})", ids

    # ── Token-by-token parsing ────────────────────────────────────────────────
    tokens = re.findall(r'[\w.]+:"[^"]+"|\S+', q)

    for tok in tokens:
        if tok.upper() in ("AND", "OR"):
            continue

        if ":" not in tok:
            # bare word — treat as name wildcard
            clauses.append("LOWER(c.name) LIKE ?")
            params.append(f"%{tok.lower()}%")
            continue

        field, value = tok.split(":", 1)
        field = field.strip().lower()
        value = value.strip().strip('"')

        if field == "set.id":
            clauses.append("c.set_id = ?")
            params.append(value)

        elif field == "id":
            clauses.append("c.id = ?")
            params.append(value)

        elif field == "name":
            if value.startswith("*") or value.endswith("*"):
                pattern = value.replace("*", "%")
                clauses.append("LOWER(c.name) LIKE ?")
                params.append(pattern.lower())
            else:
                clauses.append("LOWER(c.name) = ?")
                params.append(value.lower())

        elif field == "supertype":
            clauses.append("LOWER(c.supertype) = ?")
            params.append(value.lower())

        elif field == "rarity":
            clauses.append("LOWER(c.rarity) = ?")
            params.append(value.lower())

        # Unknown fields are silently ignored

    if not clauses:
        return "1", []
    return " AND ".join(clauses), params


def _order_clause(order_by: str) -> str:
    mapping = {
        "-releasedate":     "json_extract(c.data, '$.set.releaseDate') DESC",
        "-set.releasedate": "json_extract(c.data, '$.set.releaseDate') DESC",
        "number":           "CAST(c.number AS INTEGER) ASC, c.number ASC",
        "-number":          "CAST(c.number AS INTEGER) DESC, c.number DESC",
        "name":             "c.name ASC",
        "-name":            "c.name DESC",
    }
    key = (order_by or "").lower().replace(" ", "")
    return mapping.get(key, "json_extract(c.data, '$.set.releaseDate') DESC")


# ── Image URL rewriter ─────────────────────────────────────────────────────────

def _rewrite_images(card: dict, base_url: str) -> dict:
    """
    Replace CDN image URLs with local /img/ paths if the files exist on disk.
    Otherwise keep the original CDN URL so the browser can still fetch them.
    """
    images = card.get("images", {})
    for size in ("small", "large"):
        url = images.get(size)
        if not url:
            continue
        # Detect file extension from CDN URL
        m = re.search(r'\.(png|jpg|jpeg|webp)$', url, re.IGNORECASE)
        ext = m.group(0) if m else ".png"
        local_path = IMG_DIR / card["id"] / f"{size}{ext}"
        if local_path.exists():
            images[size] = f"{base_url}/img/{card['id']}/{size}{ext}"
    card["images"] = images
    return card


def _base_url() -> str:
    return request.host_url.rstrip("/")


# ── /v2/sets ──────────────────────────────────────────────────────────────────

@app.route("/v2/sets")
def api_sets():
    order_raw = request.args.get("orderBy", "-releaseDate").lower()
    order_col = (
        "s.release_date ASC"  if order_raw == "releasedate"
        else "s.release_date DESC"
    )
    page      = max(1, int(request.args.get("page", 1)))
    page_size = min(250, int(request.args.get("pageSize", 250)))
    offset    = (page - 1) * page_size

    con = get_db()
    total = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    rows  = con.execute(
        f"SELECT data FROM sets ORDER BY {order_col} LIMIT ? OFFSET ?",
        (page_size, offset)
    ).fetchall()

    sets = [json.loads(r["data"]) for r in rows]
    return jsonify({
        "data":       sets,
        "page":       page,
        "pageSize":   page_size,
        "count":      len(sets),
        "totalCount": total,
    })


# ── /v2/cards ─────────────────────────────────────────────────────────────────

@app.route("/v2/cards")
def api_cards():
    q         = request.args.get("q", "")
    page      = max(1, int(request.args.get("page", 1)))
    page_size = min(250, int(request.args.get("pageSize", 25)))
    order_by  = request.args.get("orderBy", "-set.releaseDate")
    offset    = (page - 1) * page_size

    where, params = _parse_query(q)
    order  = _order_clause(order_by)

    con = get_db()
    total_row = con.execute(
        f"SELECT COUNT(*) FROM cards c WHERE {where}", params
    ).fetchone()
    total = total_row[0] if total_row else 0

    rows = con.execute(
        f"SELECT c.data FROM cards c WHERE {where} ORDER BY {order} LIMIT ? OFFSET ?",
        params + [page_size, offset]
    ).fetchall()

    base = _base_url()
    cards = [_rewrite_images(json.loads(r["data"]), base) for r in rows]

    return jsonify({
        "data":       cards,
        "page":       page,
        "pageSize":   page_size,
        "count":      len(cards),
        "totalCount": total,
    })


# ── /v2/cards/<id> ────────────────────────────────────────────────────────────

@app.route("/v2/cards/<card_id>")
def api_card(card_id: str):
    con = get_db()
    row = con.execute(
        "SELECT data FROM cards WHERE id = ?", (card_id,)
    ).fetchone()
    if not row:
        return jsonify({"error": {"message": "Card not found", "code": 404}}), 404

    base = _base_url()
    card = _rewrite_images(json.loads(row["data"]), base)
    return jsonify({"data": card})


# ── /img/<card_id>/<filename> ─────────────────────────────────────────────────

@app.route("/img/<card_id>/<filename>")
def serve_image(card_id: str, filename: str):
    # Security: only allow safe filenames
    if not re.match(r'^[\w.-]+$', card_id) or not re.match(r'^[\w.-]+$', filename):
        return "Bad request", 400

    img_path = IMG_DIR / card_id / filename
    if img_path.exists():
        return send_file(img_path)

    # Not downloaded — redirect to CDN
    con = get_db()
    row = con.execute("SELECT data FROM cards WHERE id = ?", (card_id,)).fetchone()
    if not row:
        return "Not found", 404

    card = json.loads(row["data"])
    size = "large" if "large" in filename else "small"
    cdn_url = card.get("images", {}).get(size)
    if cdn_url:
        return redirect(cdn_url, 302)
    return "Not found", 404


# ── Static PWA files ──────────────────────────────────────────────────────────

@app.route("/")
def serve_index():
    return send_from_directory(STATIC_DIR, "index.html")


@app.route("/<path:filename>")
def serve_static(filename: str):
    # Don't accidentally serve Python files or data directory
    blocked = ("server.py", "requirements.txt", ".py", ".db", ".env")
    if any(filename.endswith(b) or filename.startswith("data/") for b in blocked):
        return "Forbidden", 403
    try:
        return send_from_directory(STATIC_DIR, filename)
    except Exception:
        # SPA fallback — let the PWA handle routing
        return send_from_directory(STATIC_DIR, "index.html")


# ── Health check ──────────────────────────────────────────────────────────────

@app.route("/health")
def health():
    con = get_db()
    sets  = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    cards = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    return jsonify({"status": "ok", "sets": sets, "cards": cards})


# ── Entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="Parrot Binder local server")
    parser.add_argument("--host", default="0.0.0.0",
                        help="Bind address (default 0.0.0.0)")
    parser.add_argument("--port", type=int, default=5000,
                        help="Port (default 5000)")
    parser.add_argument("--debug", action="store_true",
                        help="Enable Flask debug mode")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"ERROR: database not found at {DB_PATH}")
        print("Run scripts/download_data.py first, then restart this server.")
        raise SystemExit(1)

    con = sqlite3.connect(DB_PATH)
    sets  = con.execute("SELECT COUNT(*) FROM sets").fetchone()[0]
    cards = con.execute("SELECT COUNT(*) FROM cards").fetchone()[0]
    con.close()

    print(f"Parrot Binder — local server")
    print(f"  Database : {DB_PATH}  ({sets} sets, {cards} cards)")
    print(f"  Images   : {IMG_DIR}  ({'present' if IMG_DIR.exists() else 'not downloaded'})")
    print(f"  Listening: http://{args.host}:{args.port}")
    print(f"  PWA URL  : http://<pi-ip>:{args.port}  (open on phone)")
    print()

    app.run(host=args.host, port=args.port, debug=args.debug, threaded=True)


if __name__ == "__main__":
    main()
