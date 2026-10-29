#!/usr/bin/env python3
"""Costume contest backend. Python 3.9+, standard library only.

Run:  python3 server/server.py
Data: ./data/contest.db (SQLite) and ./data/photos/ (JPEG files)

Model
  entries     one per person: photo, name, costume title, description
  groups      entries can be linked into a set of any size (a couple, a squad), with an optional name
  categories  made by the host. "individual" categories list every entry; "group" categories list linked sets
  votes       one per voter per category

Environment variables (all optional):
  PORT               port to listen on (default 8787)
  HOST               address to bind (default 0.0.0.0, so phones on your Wi-Fi can reach it)
  CONTEST_ADMIN_KEY  host passcode; if unset, one is generated into data/admin_key.txt
  CONTEST_DATA       where to keep the data folder
"""
import base64
import hmac
import json
import mimetypes
import os
import re
import secrets
import socket
import sqlite3
import sys
import time
import traceback
from contextlib import closing
import socketserver
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

if sys.version_info < (3, 6):
    sys.exit("This needs Python 3.6 or newer (you have %d.%d)." % sys.version_info[:2])


class ThreadingHTTPServer(socketserver.ThreadingMixIn, HTTPServer):
    # http.server.ThreadingHTTPServer only exists from Python 3.7; this works everywhere.
    daemon_threads = True


ROOT = Path(__file__).resolve().parent.parent
DATA = Path(os.environ.get("CONTEST_DATA", ROOT / "data")).resolve()
SITE = (ROOT / "site").resolve()
PHOTOS = DATA / "photos"
DB_PATH = DATA / "contest.db"
PORT = int(os.environ.get("PORT", "8787"))
HOST = os.environ.get("HOST", "0.0.0.0")

MAX_BODY = 14 * 1024 * 1024      # request body cap
MAX_PHOTO = 6 * 1024 * 1024      # decoded JPEG cap
PHASES = ("setup", "open", "closed")
KINDS = ("individual", "group")
VOTER_RE = re.compile(r"^[A-Za-z0-9_-]{8,64}$")
DEFAULT_CATEGORIES = [
    ("Best Costume", "individual"),
    ("Most Creative", "individual"),
    ("Best Couple's Costume", "group"),
]

ADMIN_KEY = ""


class ApiError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def is_int(v):
    return isinstance(v, int) and not isinstance(v, bool)


# ---------------------------------------------------------------- database

def connect():
    conn = sqlite3.connect(str(DB_PATH), timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db():
    with closing(connect()) as conn:
        conn.executescript(
            """
            create table if not exists categories(
              id integer primary key autoincrement,
              name text not null,
              kind text not null default 'individual',
              position integer not null default 0
            );
            create table if not exists groups(
              id integer primary key autoincrement,
              name text not null default ''
            );
            create table if not exists costumes(
              id integer primary key autoincrement,
              title text not null,
              name text not null,
              description text not null default '',
              photo text not null default '',
              thumb text not null default '',
              group_id integer,
              created_at real not null
            );
            create table if not exists votes(
              voter text not null,
              category_id integer not null,
              target_id integer not null,
              updated_at real not null,
              primary key (voter, category_id)
            );
            create table if not exists settings(
              key text primary key,
              value text not null
            );
            """
        )
        with conn:
            if not conn.execute("select 1 from settings where key='seeded'").fetchone():
                for i, (name, kind) in enumerate(DEFAULT_CATEGORIES):
                    conn.execute("insert into categories(name, kind, position) values(?,?,?)", (name, kind, i))
                conn.execute("insert into settings(key, value) values('seeded', '1')")


def get_phase(conn):
    row = conn.execute("select value from settings where key='phase'").fetchone()
    return row["value"] if row and row["value"] in PHASES else "setup"


def list_categories(conn):
    return [
        {"id": r["id"], "name": r["name"], "kind": r["kind"]}
        for r in conn.execute("select * from categories order by position, id")
    ]


def photo_url(name):
    return "/photos/" + name if name else None


def list_costumes(conn):
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "name": r["name"],
            "description": r["description"],
            "photo": photo_url(r["photo"]),
            "thumb": photo_url(r["thumb"] or r["photo"]),
            "groupId": r["group_id"],
        }
        for r in conn.execute("select * from costumes order by id")
    ]


def list_groups(conn):
    members = {}
    for r in conn.execute("select id, group_id from costumes where group_id is not null order by id"):
        members.setdefault(r["group_id"], []).append(r["id"])
    return [
        {"id": g["id"], "name": g["name"], "members": members[g["id"]]}
        for g in conn.execute("select id, name from groups order by id")
        if g["id"] in members
    ]


def build_results(conn):
    costumes = list_costumes(conn)
    by_id = {c["id"]: c for c in costumes}
    groups = list_groups(conn)
    counts = {
        (r["category_id"], r["target_id"]): r["n"]
        for r in conn.execute("select category_id, target_id, count(*) as n from votes group by category_id, target_id")
    }
    voters = conn.execute("select count(distinct voter) from votes").fetchone()[0]
    out = []
    for cat in list_categories(conn):
        if cat["kind"] == "individual":
            cands = [
                {"targetId": c["id"], "groupName": None, "members": [c], "votes": counts.get((cat["id"], c["id"]), 0)}
                for c in costumes
            ]
        else:
            cands = [
                {
                    "targetId": g["id"], "groupName": g["name"] or None,
                    "members": [by_id[i] for i in g["members"]],
                    "votes": counts.get((cat["id"], g["id"]), 0),
                }
                for g in groups
            ]
        cands.sort(key=lambda c: (-c["votes"], c["targetId"]))
        top = cands[0]["votes"] if cands else 0
        out.append({
            **cat,
            "totalVotes": sum(c["votes"] for c in cands),
            "candidates": cands,
            "winners": [c["targetId"] for c in cands if top > 0 and c["votes"] == top],
        })
    return {"phase": get_phase(conn), "voters": voters, "categories": out}


# ---------------------------------------------------------------- linking

def prune_groups(conn):
    """A set needs at least two entries. Clean up sets that fell below that, and their votes."""
    for r in conn.execute(
        "select group_id from costumes where group_id is not null group by group_id having count(*)=1"
    ).fetchall():
        conn.execute("update costumes set group_id=null where group_id=?", (r["group_id"],))
    dead = [
        r["id"] for r in conn.execute(
            "select id from groups where id not in (select group_id from costumes where group_id is not null)"
        ).fetchall()
    ]
    for gid in dead:
        conn.execute(
            "delete from votes where target_id=? and category_id in (select id from categories where kind='group')",
            (gid,),
        )
        conn.execute("delete from groups where id=?", (gid,))


def link_instruction(body, own_id=None):
    if "linkWith" not in body:
        return ("keep", None)
    value = body["linkWith"]
    if value is None or value == "":
        return ("unlink", None)
    if not is_int(value):
        raise ApiError(400, "Pick which entry this one goes with.")
    if own_id is not None and value == own_id:
        raise ApiError(400, "An entry can't go with itself.")
    return ("join", value)


def apply_link(conn, cid, action, other_id):
    if action == "keep":
        return
    if action == "unlink":
        conn.execute("update costumes set group_id=null where id=?", (cid,))
    else:
        other = conn.execute("select id, group_id from costumes where id=?", (other_id,)).fetchone()
        if not other:
            raise ApiError(404, "The entry you picked was removed.")
        gid = other["group_id"]
        if gid is None:
            gid = conn.execute("insert into groups(name) values('')").lastrowid
            conn.execute("update costumes set group_id=? where id=?", (gid, other_id))
        conn.execute("update costumes set group_id=? where id=?", (gid, cid))
    prune_groups(conn)


# ---------------------------------------------------------------- input checks

def parse_fields(body):
    title = str(body.get("title", "")).strip()[:80]
    name = str(body.get("name", "")).strip()[:40]
    description = str(body.get("description", "")).strip()[:500]
    if not title:
        raise ApiError(400, "Give the costume a name.")
    if not name:
        raise ApiError(400, "Add the name of the person wearing it.")
    return title, name, description


def parse_category(body):
    name = str(body.get("name", "")).strip()[:40]
    kind = body.get("kind", "individual")
    if not name:
        raise ApiError(400, "Give the category a name.")
    if kind not in KINDS:
        raise ApiError(400, "Category type must be individual or group.")
    return name, kind


def decode_photo(value):
    if not isinstance(value, str):
        raise ApiError(400, "Add a photo.")
    m = re.match(r"^data:image/jpeg;base64,(.+)$", value, re.S)
    if not m:
        raise ApiError(400, "The photo has to be a JPEG.")
    try:
        raw = base64.b64decode(m.group(1), validate=False)
    except Exception:
        raise ApiError(400, "That photo couldn't be read.")
    if not raw.startswith(b"\xff\xd8\xff"):
        raise ApiError(400, "That photo couldn't be read.")
    if len(raw) > MAX_PHOTO:
        raise ApiError(413, "That photo is too large.")
    return raw


def decode_photos(body):
    full = decode_photo(body.get("photo"))
    thumb = decode_photo(body["thumb"]) if body.get("thumb") else full
    return full, thumb


def save_photos(cid, full, thumb):
    PHOTOS.mkdir(parents=True, exist_ok=True)
    tag = secrets.token_hex(4)
    full_name, thumb_name = "%d-%s.jpg" % (cid, tag), "%d-%s-t.jpg" % (cid, tag)
    (PHOTOS / full_name).write_bytes(full)
    (PHOTOS / thumb_name).write_bytes(thumb)
    return full_name, thumb_name


def remove_photos(*names):
    for name in names:
        if name:
            try:
                (PHOTOS / name).unlink()
            except FileNotFoundError:
                pass


# ---------------------------------------------------------------- http

class Handler(BaseHTTPRequestHandler):
    server_version = "CostumeContest/2.0"

    # -- response helpers
    def _cors(self):
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-Admin-Key, ngrok-skip-browser-warning")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, PUT, DELETE, OPTIONS")
        self.send_header("Access-Control-Max-Age", "600")

    def _json(self, status, obj):
        body = json.dumps(obj).encode("utf-8")
        self.send_response(status)
        self._cors()
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def _read_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0:
            return {}
        if length > MAX_BODY:
            raise ApiError(413, "That request is too large.")
        try:
            data = json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            raise ApiError(400, "The request wasn't valid JSON.")
        if not isinstance(data, dict):
            raise ApiError(400, "The request body must be an object.")
        return data

    # -- auth
    def _require_admin(self):
        key = self.headers.get("X-Admin-Key", "")
        if not (key and hmac.compare_digest(key.encode("utf-8"), ADMIN_KEY.encode("utf-8"))):
            time.sleep(0.5)  # slow down passcode guessing
            raise ApiError(401, "Wrong passcode.")

    # -- routing
    def _dispatch(self):
        try:
            url = urlparse(self.path)
            path, query = url.path, parse_qs(url.query)
            if self.command == "OPTIONS":
                self.send_response(204)
                self._cors()
                self.send_header("Content-Length", "0")
                self.end_headers()
                return
            if path.startswith("/api/"):
                self._api(self.command, path, query)
            elif self.command in ("GET", "HEAD"):
                self._static(path)
            else:
                raise ApiError(405, "Method not allowed.")
        except ApiError as e:
            self._json(e.status, {"error": e.message})
        except (BrokenPipeError, ConnectionResetError):
            pass
        except Exception:
            traceback.print_exc()
            try:
                self._json(500, {"error": "Something went wrong on the server."})
            except Exception:
                pass

    do_GET = do_HEAD = do_POST = do_PUT = do_DELETE = do_OPTIONS = _dispatch

    def _api(self, method, path, query):
        with closing(connect()) as conn:
            # ---- public
            if method == "GET" and path == "/api/state":
                out = {
                    "phase": get_phase(conn),
                    "categories": list_categories(conn),
                    "costumes": list_costumes(conn),
                    "groups": list_groups(conn),
                }
                voter = (query.get("voter") or [""])[0]
                if VOTER_RE.match(voter):
                    out["myVotes"] = {
                        str(r["category_id"]): r["target_id"]
                        for r in conn.execute("select category_id, target_id from votes where voter=?", (voter,))
                    }
                return self._json(200, out)

            if method == "POST" and path == "/api/vote":
                body = self._read_json()
                voter = str(body.get("voter", ""))
                if not VOTER_RE.match(voter):
                    raise ApiError(400, "Missing voter id. Reload the page and try again.")
                if get_phase(conn) != "open":
                    raise ApiError(409, "Voting isn't open right now.")
                category_id, target = body.get("categoryId"), body.get("targetId")
                if not is_int(category_id):
                    raise ApiError(400, "Pick a category.")
                cat = conn.execute("select kind from categories where id=?", (category_id,)).fetchone()
                if not cat:
                    raise ApiError(404, "That category was removed. Reload the page.")
                with conn:
                    if target is None:
                        conn.execute("delete from votes where voter=? and category_id=?", (voter, category_id))
                    else:
                        if not is_int(target):
                            raise ApiError(400, "Pick something to vote for.")
                        table = "costumes" if cat["kind"] == "individual" else "groups"
                        if not conn.execute("select 1 from %s where id=?" % table, (target,)).fetchone():
                            raise ApiError(404, "That choice was removed. Pick another.")
                        conn.execute(
                            "insert or replace into votes(voter, category_id, target_id, updated_at) values(?,?,?,?)",
                            (voter, category_id, target, time.time()),
                        )
                return self._json(200, {"ok": True})

            if method == "POST" and path == "/api/ballot":
                # Submit a whole ballot at once: {voter, votes: {categoryId: targetId}}.
                # The voter's votes become exactly this set (categories left out are cleared),
                # and either every pick is saved or none are.
                body = self._read_json()
                voter = str(body.get("voter", ""))
                if not VOTER_RE.match(voter):
                    raise ApiError(400, "Missing voter id. Reload the page and try again.")
                if get_phase(conn) != "open":
                    raise ApiError(409, "Voting isn't open right now.")
                votes = body.get("votes")
                if not isinstance(votes, dict) or len(votes) > 200:
                    raise ApiError(400, "Send your picks as {categoryId: choice}.")
                picks = []
                for raw_cat, target in votes.items():
                    if not str(raw_cat).isdigit() or not is_int(target):
                        raise ApiError(400, "That ballot isn't valid. Reload the page and try again.")
                    cat = conn.execute("select kind from categories where id=?", (int(raw_cat),)).fetchone()
                    if not cat:
                        raise ApiError(404, "A category was removed. Reload the page and check your picks.")
                    table = "costumes" if cat["kind"] == "individual" else "groups"
                    if not conn.execute("select 1 from %s where id=?" % table, (target,)).fetchone():
                        raise ApiError(404, "A choice was removed. Reload the page and check your picks.")
                    picks.append((int(raw_cat), target))
                with conn:
                    conn.execute("delete from votes where voter=?", (voter,))
                    for category_id, target in picks:
                        conn.execute(
                            "insert into votes(voter, category_id, target_id, updated_at) values(?,?,?,?)",
                            (voter, category_id, target, time.time()),
                        )
                return self._json(200, {"ok": True, "myVotes": {str(c): t for c, t in picks}})

            # ---- host only
            if method == "GET" and path == "/api/admin/check":
                self._require_admin()
                return self._json(200, {"ok": True})

            if method == "GET" and path == "/api/results":
                self._require_admin()
                return self._json(200, build_results(conn))

            if method == "POST" and path == "/api/phase":
                self._require_admin()
                phase = self._read_json().get("phase")
                if phase not in PHASES:
                    raise ApiError(400, "Phase must be setup, open or closed.")
                with conn:
                    conn.execute(
                        "insert or replace into settings(key, value) values('phase', ?)",
                        (phase,),
                    )
                return self._json(200, {"phase": phase})

            # categories
            if method == "POST" and path == "/api/categories":
                self._require_admin()
                name, kind = parse_category(self._read_json())
                with conn:
                    pos = conn.execute("select coalesce(max(position), -1) + 1 from categories").fetchone()[0]
                    cid = conn.execute(
                        "insert into categories(name, kind, position) values(?,?,?)", (name, kind, pos)
                    ).lastrowid
                return self._json(201, {"id": cid, "name": name, "kind": kind})

            if method == "POST" and path == "/api/categories/order":
                self._require_admin()
                ids = self._read_json().get("ids")
                if not isinstance(ids, list) or not all(is_int(i) for i in ids):
                    raise ApiError(400, "Send the category ids in the order you want.")
                with conn:
                    for pos, cid in enumerate(ids):
                        conn.execute("update categories set position=? where id=?", (pos, cid))
                return self._json(200, {"categories": list_categories(conn)})

            m = re.fullmatch(r"/api/categories/(\d+)", path)
            if m:
                self._require_admin()
                cid = int(m.group(1))
                row = conn.execute("select * from categories where id=?", (cid,)).fetchone()
                if method == "PUT":
                    if not row:
                        raise ApiError(404, "That category doesn't exist.")
                    name, kind = parse_category(self._read_json())
                    with conn:
                        if kind != row["kind"]:  # votes mean something different now
                            conn.execute("delete from votes where category_id=?", (cid,))
                        conn.execute("update categories set name=?, kind=? where id=?", (name, kind, cid))
                    return self._json(200, {"id": cid, "name": name, "kind": kind})
                if method == "DELETE":
                    with conn:
                        conn.execute("delete from votes where category_id=?", (cid,))
                        conn.execute("delete from categories where id=?", (cid,))
                    return self._json(200, {"ok": True})

            # groups
            m = re.fullmatch(r"/api/groups/(\d+)", path)
            if m and method == "PUT":
                self._require_admin()
                gid = int(m.group(1))
                if not conn.execute("select 1 from groups where id=?", (gid,)).fetchone():
                    raise ApiError(404, "That group doesn't exist.")
                name = str(self._read_json().get("name", "")).strip()[:60]
                with conn:
                    conn.execute("update groups set name=? where id=?", (name, gid))
                return self._json(200, {"id": gid, "name": name})

            # entries
            if method == "POST" and path == "/api/costumes":
                self._require_admin()
                body = self._read_json()
                title, name, description = parse_fields(body)
                full, thumb = decode_photos(body)
                action, other_id = link_instruction(body)
                files = ()
                with conn:
                    cid = conn.execute(
                        "insert into costumes(title, name, description, created_at) values(?,?,?,?)",
                        (title, name, description, time.time()),
                    ).lastrowid
                    files = save_photos(cid, full, thumb)
                    try:
                        conn.execute("update costumes set photo=?, thumb=? where id=?", (files[0], files[1], cid))
                        apply_link(conn, cid, action, other_id)
                    except Exception:
                        remove_photos(*files)
                        raise
                for c in list_costumes(conn):
                    if c["id"] == cid:
                        return self._json(201, c)

            m = re.fullmatch(r"/api/costumes/(\d+)/unlink", path)
            if m and method == "POST":
                self._require_admin()
                with conn:
                    conn.execute("update costumes set group_id=null where id=?", (int(m.group(1)),))
                    prune_groups(conn)
                return self._json(200, {"ok": True})

            m = re.fullmatch(r"/api/costumes/(\d+)", path)
            if m and method in ("PUT", "DELETE"):
                self._require_admin()
                cid = int(m.group(1))
                row = conn.execute("select * from costumes where id=?", (cid,)).fetchone()
                if method == "PUT":
                    if not row:
                        raise ApiError(404, "That costume doesn't exist.")
                    body = self._read_json()
                    title, name, description = parse_fields(body)
                    action, other_id = link_instruction(body, own_id=cid)
                    photo, thumb, fresh = row["photo"], row["thumb"], ()
                    if body.get("photo"):
                        fresh = save_photos(cid, *decode_photos(body))
                        photo, thumb = fresh
                    try:
                        with conn:
                            conn.execute(
                                "update costumes set title=?, name=?, description=?, photo=?, thumb=? where id=?",
                                (title, name, description, photo, thumb, cid),
                            )
                            apply_link(conn, cid, action, other_id)
                    except Exception:
                        remove_photos(*fresh)
                        raise
                    if fresh:
                        remove_photos(row["photo"], row["thumb"])
                    return self._json(200, next(c for c in list_costumes(conn) if c["id"] == cid))
                if row:
                    with conn:
                        conn.execute(
                            "delete from votes where target_id=? and category_id in "
                            "(select id from categories where kind='individual')",
                            (cid,),
                        )
                        conn.execute("delete from costumes where id=?", (cid,))
                        prune_groups(conn)
                    remove_photos(row["photo"], row["thumb"])
                return self._json(200, {"ok": True})

            raise ApiError(404, "Not found.")

    def _static(self, path):
        if path.startswith("/photos/"):
            base, rel = PHOTOS.resolve(), path[len("/photos/"):]
            cache = "public, max-age=31536000, immutable"
        else:
            base, rel = SITE, path.lstrip("/") or "index.html"
            cache = "no-cache"
        target = (base / unquote(rel)).resolve()
        if path == "/backend.json" and not target.is_file():
            # Pages look for backend.json to find the backend. Served from here, there's nothing to override.
            return self._json(200, {})
        if base != target and base not in target.parents or not target.is_file():
            raise ApiError(404, "Not found.")
        ctype = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
        if ctype.startswith("text/") or ctype in ("application/javascript", "application/json"):
            ctype += "; charset=utf-8"
        data = target.read_bytes()
        self.send_response(200)
        self._cors()
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", cache)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def log_message(self, fmt, *args):
        sys.stderr.write("%s  %s\n" % (time.strftime("%H:%M:%S"), fmt % args))


# ---------------------------------------------------------------- startup

def load_admin_key():
    key = os.environ.get("CONTEST_ADMIN_KEY", "").strip()
    if key:
        return key
    key_file = DATA / "admin_key.txt"
    if key_file.exists():
        saved = key_file.read_text().strip()
        if saved:
            return saved
    alphabet = "abcdefghjkmnpqrstuvwxyz23456789"
    key = "".join(secrets.choice(alphabet) for _ in range(8))
    key_file.write_text(key + "\n")
    return key


def lan_ip():
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as s:
            s.connect(("8.8.8.8", 80))  # no packet is sent; this just picks the outbound interface
            return s.getsockname()[0]
    except OSError:
        return None


def main():
    global ADMIN_KEY
    DATA.mkdir(parents=True, exist_ok=True)
    PHOTOS.mkdir(parents=True, exist_ok=True)
    init_db()
    ADMIN_KEY = load_admin_key()
    server = ThreadingHTTPServer((HOST, PORT), Handler)
    ip = lan_ip()
    print("Costume contest server is running.")
    print("  This computer:  http://localhost:%d/" % PORT)
    if ip:
        print("  Same Wi-Fi:     http://%s:%d/" % (ip, PORT))
    print("  Host passcode:  %s" % ADMIN_KEY)
    print("Press Ctrl+C to stop.")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
