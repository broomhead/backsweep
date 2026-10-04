#!/usr/bin/env python3
"""backsweep — bulk-delete your own YouTube uploads by date and view count.

Deletion is irreversible, so the workflow is split in three:

  scan    Lists every upload on the authenticated channel, applies the
          criteria, and writes a plan file. Never deletes anything.
  review  Serves a local page where you mark each candidate Keep or Delete.
          Decisions are saved into the plan file. No YouTube calls.
  delete  Takes only the videos marked Delete, re-checks each against YouTube
          *now*, shows a spot-check, makes you type a confirmation, then
          deletes and logs.

Criteria:
  --before DATE     required; only videos published strictly before local
                    midnight at the start of DATE are candidates
  --max-views N     optional; videos with MORE than N views are kept
  --keep FILE       optional; video IDs or URLs (one per line) never deleted
"""

from __future__ import annotations

import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import sys
import threading
import time
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

SCOPES = ["https://www.googleapis.com/auth/youtube.force-ssl"]
SECRETS_DIR = Path(os.environ.get("BACKSWEEP_SECRETS", "/secrets"))
STATE_DIR = Path(os.environ.get("BACKSWEEP_STATE", "/state"))
CLIENT_SECRET = SECRETS_DIR / "client_secret.json"
TOKEN = SECRETS_DIR / "token.json"
DELETE_LOG = STATE_DIR / "deleted.jsonl"
DEFAULT_TZ = os.environ.get("BACKSWEEP_TZ", "UTC")
REDIRECT_URI = "http://localhost:8765/"
PLAN_VERSION = 1
REVIEW_PORT = 8780  # must match the port mapping in docker-compose.yml
DECISIONS = ("pending", "keep", "delete")


class Abort(Exception):
    pass


# ── auth ────────────────────────────────────────────────────────────────────

def load_credentials(interactive: bool):
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials

    creds = None
    if TOKEN.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN), SCOPES)
    if creds and creds.valid:
        return creds
    if creds and creds.expired and creds.refresh_token:
        try:
            creds.refresh(Request())
            TOKEN.write_text(creds.to_json())
            return creds
        except Exception as e:  # revoked, or test-mode token past 7 days
            print(f"Stored token could not be refreshed ({e}).", file=sys.stderr)
    if not interactive:
        raise Abort("Not authenticated. Run:  docker compose run --rm backsweep auth")
    return run_auth_flow()


def run_auth_flow():
    from google_auth_oauthlib.flow import Flow

    if not CLIENT_SECRET.exists():
        raise Abort(f"Put your OAuth client JSON at {CLIENT_SECRET} "
                    "(see the README for creating one).")
    flow = Flow.from_client_secrets_file(str(CLIENT_SECRET), SCOPES, redirect_uri=REDIRECT_URI)
    url, state = flow.authorization_url(prompt="consent")
    print("\n1. Open this URL in your browser and sign in.")
    print("   If asked to pick an account or channel, pick the one to clean up.\n")
    print(f"   {url}\n")
    print("2. After approving, the browser lands on a localhost page that fails to")
    print("   load. That's expected. Copy the FULL URL from the address bar.\n")
    pasted = input("3. Paste it here: ").strip()
    qs = parse_qs(urlparse(pasted).query)
    if "error" in qs:
        raise Abort(f"Authorization failed: {qs['error'][0]}")
    if "code" not in qs:
        raise Abort("That URL has no ?code= in it.")
    if qs.get("state", [state])[0] != state:
        raise Abort("State mismatch — use the URL from THIS auth attempt.")
    flow.fetch_token(code=qs["code"][0])
    creds = flow.credentials
    TOKEN.write_text(creds.to_json())
    os.chmod(TOKEN, 0o600)
    return creds


def youtube(interactive: bool = False):
    from googleapiclient.discovery import build

    return build("youtube", "v3", credentials=load_credentials(interactive), cache_discovery=False)


def my_channel(yt) -> dict:
    resp = yt.channels().list(part="snippet,contentDetails,statistics", mine=True).execute()
    items = resp.get("items", [])
    if not items:
        raise Abort("This Google account has no YouTube channel.")
    if len(items) > 1:
        raise Abort("Token maps to multiple channels; re-run auth and pick one.")
    ch = items[0]
    return {
        "id": ch["id"],
        "title": ch["snippet"]["title"],
        "uploads": ch["contentDetails"]["relatedPlaylists"]["uploads"],
        "video_count": int(ch["statistics"].get("videoCount", 0)),
    }


# ── fetching ────────────────────────────────────────────────────────────────

def fetch_videos(yt, ids: list[str]) -> dict[str, dict]:
    """videos.list in batches of 50 (1 quota unit each). Missing IDs = gone."""
    out: dict[str, dict] = {}
    for i in range(0, len(ids), 50):
        resp = yt.videos().list(part="snippet,statistics,status", id=",".join(ids[i:i + 50]),
                                maxResults=50).execute()
        for v in resp.get("items", []):
            stats = v.get("statistics", {})
            out[v["id"]] = {
                "id": v["id"],
                "title": v["snippet"]["title"],
                "published_at": v["snippet"]["publishedAt"],
                "channel_id": v["snippet"]["channelId"],
                # None = YouTube didn't report one; never treated as "low"
                "views": int(stats["viewCount"]) if "viewCount" in stats else None,
                "privacy": v.get("status", {}).get("privacyStatus", "?"),
                "url": f"https://youtu.be/{v['id']}",
            }
    return out


def _playlist_ids(yt, playlist: str) -> list[str] | None:
    """All video IDs in a playlist, or None if the playlist doesn't exist."""
    from googleapiclient.errors import HttpError

    ids: list[str] = []
    token = None
    while True:
        try:
            resp = yt.playlistItems().list(part="contentDetails", playlistId=playlist,
                                           maxResults=50, pageToken=token).execute()
        except HttpError as e:
            if e.resp.status in (403, 404):
                return None
            raise
        ids += [it["contentDetails"]["videoId"] for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def _live_broadcast_ids(yt) -> list[str] | None:
    """Completed broadcasts via the Live API — catches streams the uploads
    playlist leaves out. None if the channel has no live access."""
    from googleapiclient.errors import HttpError

    ids, token = [], None
    while True:
        try:
            resp = yt.liveBroadcasts().list(part="id", mine=True, broadcastStatus="completed",
                                            broadcastType="all", maxResults=50,
                                            pageToken=token).execute()
        except HttpError as e:
            if e.resp.status in (403, 404):
                return None
            raise
        ids += [it["id"] for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            return ids


def _search_ids(yt, event_type: str | None, cap: int) -> list[str]:
    """search.list(forMine) — 100 quota units a page, and YouTube stops at
    about 500 results, so it's a backstop, not the main source."""
    ids, token = [], None
    while len(ids) < cap:
        kw = {"eventType": event_type} if event_type else {}
        resp = yt.search().list(part="id", forMine=True, type="video", order="date",
                                maxResults=50, pageToken=token, **kw).execute()
        ids += [it["id"]["videoId"] for it in resp.get("items", [])]
        token = resp.get("nextPageToken")
        if not token:
            break
    return ids[:cap]


def collect_video_ids(yt, ch: dict, deep: bool) -> tuple[list[str], list[tuple[str, int, int]]]:
    """Union of every list of the channel's own videos we can cheaply get.

    The uploads playlist misses things on some channels — live streams are the
    usual casualty — so the channel's per-tab playlists and the Live API are
    folded in. Returns the ids plus (source, found, new) for the summary.
    """
    tail = ch["id"][2:]  # UC… → the per-tab playlists UULV…/UUSH…/UULF…
    sources = [
        ("uploads playlist", lambda: _playlist_ids(yt, ch["uploads"])),
        ("live tab", lambda: _playlist_ids(yt, "UULV" + tail)),
        ("shorts tab", lambda: _playlist_ids(yt, "UUSH" + tail)),
        ("videos tab", lambda: _playlist_ids(yt, "UULF" + tail)),
        ("live broadcasts", lambda: _live_broadcast_ids(yt)),
    ]
    if deep:
        sources += [("search: live", lambda: _search_ids(yt, "completed", 500)),
                    ("search: all", lambda: _search_ids(yt, None, 500))]

    seen: dict[str, None] = {}
    report = []
    for name, fn in sources:
        ids = fn()
        if ids is None:  # playlist or API not available for this channel
            report.append((name, -1, 0))
            print(f"  {name:<18} not available")
            continue
        new = [i for i in ids if i not in seen]
        seen.update(dict.fromkeys(ids))
        report.append((name, len(ids), len(new)))
        print(f"  {name:<18} {len(ids):>5} found, {len(new):>4} new")
    return list(seen), report


# ── criteria ────────────────────────────────────────────────────────────────

def parse_published(s: str) -> dt.datetime:
    return dt.datetime.fromisoformat(s.replace("Z", "+00:00"))


def cutoff_from(date_str: str, tz: str) -> dt.datetime:
    try:
        d = dt.date.fromisoformat(date_str)
    except ValueError:
        raise Abort(f"--before must be YYYY-MM-DD, got {date_str!r}")
    return dt.datetime.combine(d, dt.time.min, tzinfo=ZoneInfo(tz))


def verdict(v: dict, criteria: dict, keep_ids: set[str]) -> str | None:
    """None if the video should be deleted, else the reason it's kept."""
    if v["id"] in keep_ids:
        return "keep-list"
    if parse_published(v["published_at"]) >= dt.datetime.fromisoformat(criteria["cutoff"]):
        return "too new"
    max_views = criteria.get("max_views")
    if max_views is not None:
        if v["views"] is None:
            return "views unknown"
        if v["views"] > max_views:
            return "too many views"
    return None


def read_keep_file(path: str | None) -> set[str]:
    if not path:
        return set()
    ids = set()
    for line in Path(path).read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.search(r"(?:v=|youtu\.be/|shorts/|live/)([\w-]{11})", line)
        ids.add(m.group(1) if m else line)
    return ids


# ── display ─────────────────────────────────────────────────────────────────

def fmt_row(v: dict, tz: str) -> str:
    when = parse_published(v["published_at"]).astimezone(ZoneInfo(tz)).strftime("%Y-%m-%d")
    views = "?" if v["views"] is None else f"{v['views']:,}"
    title = v["title"] if len(v["title"]) <= 58 else v["title"][:57] + "…"
    return f"  {when}  {views:>9}  {v['privacy']:<8}  {title:<58}  {v['url']}"


def print_table(rows: list[dict], tz: str, heading: str):
    if not rows:
        return
    print(f"\n{heading}")
    print(f"  {'published':<10}  {'views':>9}  {'privacy':<8}  {'title':<58}  url")
    for v in rows:
        print(fmt_row(v, tz))


def spot_check(cands: list[dict], tz: str, sample: int):
    """Edge cases first (nearest the thresholds), then a random sample."""
    by_date = sorted(cands, key=lambda v: v["published_at"])
    by_views = sorted(cands, key=lambda v: v["views"] or 0)
    edges = {v["id"]: v for v in by_date[-3:] + by_views[-3:]}
    print_table(sorted(edges.values(), key=lambda v: v["published_at"]), tz,
                "Edge cases — newest and most-viewed candidates:")
    rest = [v for v in cands if v["id"] not in edges]
    pick = random.sample(rest, min(sample, len(rest)))
    print_table(sorted(pick, key=lambda v: v["published_at"]), tz,
                f"Random sample ({len(pick)} of {len(rest)} others):")


# ── plan file ───────────────────────────────────────────────────────────────

_plan_lock = threading.Lock()


def load_plan(path: Path) -> dict:
    if not path.exists():
        raise Abort(f"No plan at {path}")
    plan = json.loads(path.read_text())
    if plan.get("version") != PLAN_VERSION:
        raise Abort("Unrecognised plan file version.")
    for c in plan["candidates"]:
        c.setdefault("decision", "pending")
    return plan


def save_plan(path: Path, plan: dict):
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(plan, indent=2, ensure_ascii=False))
    os.replace(tmp, path)


def update_plan(path: Path, fn):
    """Read-modify-write under a lock; review and delete may both touch it."""
    with _plan_lock:
        plan = load_plan(path)
        fn(plan)
        save_plan(path, plan)
        return plan


def decision_counts(plan: dict) -> dict:
    counts = {"pending": 0, "keep": 0, "delete": 0, "deleted": 0}
    for c in plan["candidates"]:
        counts["deleted" if c.get("deleted_at") else c["decision"]] += 1
    return counts


# ── commands ────────────────────────────────────────────────────────────────

def cmd_auth(args):
    if TOKEN.exists() and not args.force:
        print(f"{TOKEN} exists; checking it (use --force to redo auth).")
    elif args.force and TOKEN.exists():
        TOKEN.unlink()
    ch = my_channel(youtube(interactive=True))
    print(f"\nAuthenticated as channel: {ch['title']}  ({ch['id']}, {ch['video_count']} videos)")
    print("If that's not the channel you meant, run:  auth --force")


def cmd_scan(args):
    tz = args.tz
    ZoneInfo(tz)  # validate early
    cutoff = cutoff_from(args.before, tz)
    if cutoff > dt.datetime.now(dt.timezone.utc):
        raise Abort("--before is in the future; that would include everything you've posted.")
    criteria = {"before": args.before, "tz": tz, "cutoff": cutoff.isoformat(),
                "max_views": args.max_views}
    keep_ids = read_keep_file(args.keep)

    yt = youtube()
    ch = my_channel(yt)
    print(f"Channel: {ch['title']} ({ch['id']})")
    ids, sources = collect_video_ids(yt, ch, args.deep)
    print(f"  {len(ids)} distinct videos; fetching details…")
    videos = fetch_videos(yt, ids)

    cands, kept = [], {}
    for v in videos.values():
        if v["channel_id"] != ch["id"]:
            kept.setdefault("other channel", []).append(v)
            continue
        reason = verdict(v, criteria, keep_ids)
        if reason:
            kept.setdefault(reason, []).append(v)
        else:
            cands.append(v)
    cands.sort(key=lambda v: v["published_at"])

    stamp = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
    plan_path = STATE_DIR / f"plan-{stamp}.json"
    plan = {"version": PLAN_VERSION, "created": dt.datetime.now(dt.timezone.utc).isoformat(),
            "channel_id": ch["id"], "channel_title": ch["title"], "criteria": criteria,
            "keep_ids": sorted(keep_ids), "sources": sources,
            "candidates": [{**v, "decision": "pending"} for v in cands]}
    save_plan(plan_path, plan)
    csv_path = plan_path.with_suffix(".csv")
    with csv_path.open("w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["published_at", "views", "privacy", "title", "url", "id"],
                           extrasaction="ignore")
        w.writeheader()
        w.writerows(cands)

    mv = "none" if args.max_views is None else f"≤ {args.max_views:,}"
    print(f"\nCriteria: published before {args.before} ({tz}), views {mv}")
    privacy = {}
    for v in videos.values():
        privacy[v["privacy"]] = privacy.get(v["privacy"], 0) + 1
    print(f"Videos found: {len(videos)}  (" +
          ", ".join(f"{n} {k}" for k, n in sorted(privacy.items())) + ")")
    for reason, vs in sorted(kept.items()):
        print(f"  kept — {reason:<15} {len(vs):>5}")
    total_views = sum(v["views"] or 0 for v in cands)
    print(f"  DELETE candidates     {len(cands):>5}   ({total_views:,} views combined)")
    if kept.get("views unknown"):
        print("  (videos with no reported view count are always kept when --max-views is set)")
    if cands:
        spot_check(cands, tz, args.sample)
    print(f"\nPlan written: {plan_path}\n         csv: {csv_path}")
    print("Nothing was deleted. Mark each video Keep or Delete in the review page:")
    print(f"  docker compose run --rm --service-ports backsweep review {plan_path}")


def cmd_review(args):
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    plan_path = Path(args.plan)
    plan = load_plan(plan_path)
    tz = ZoneInfo(plan["criteria"]["tz"])
    page = (Path(__file__).resolve().parent / "review.html").read_bytes()

    def plan_payload(plan):
        cands = [{**c, "date": parse_published(c["published_at"]).astimezone(tz).strftime("%Y-%m-%d"),
                  "deleted": bool(c.get("deleted_at"))} for c in plan["candidates"]]
        return {"plan": plan_path.name, "channel_title": plan["channel_title"],
                "criteria": plan["criteria"], "counts": decision_counts(plan), "candidates": cands}

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *a):
            pass

        def _send(self, code, body, ctype="application/json; charset=utf-8"):
            if not isinstance(body, bytes):
                body = json.dumps(body, ensure_ascii=False).encode()
            self.send_response(code)
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def _local(self):
            # Refuse other hostnames so a web page can't reach this via DNS rebinding.
            host = (self.headers.get("Host") or "").split(":")[0]
            return host in ("localhost", "127.0.0.1")

        def do_GET(self):
            if not self._local():
                return self._send(403, {"error": "localhost only"})
            if self.path == "/":
                return self._send(200, page, "text/html; charset=utf-8")
            if self.path == "/api/plan":
                with _plan_lock:
                    return self._send(200, plan_payload(load_plan(plan_path)))
            self._send(404, {"error": "not found"})

        def do_POST(self):
            # The custom header forces a CORS preflight, which this server never
            # answers, so other sites open in the browser can't post decisions.
            if not self._local() or self.headers.get("X-Backsweep") != "1":
                return self._send(403, {"error": "forbidden"})
            if self.path != "/api/decision":
                return self._send(404, {"error": "not found"})
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length", 0))))
                ids, decision = set(body["ids"]), body["decision"]
                assert decision in DECISIONS
            except Exception:
                return self._send(400, {"error": "expected {ids: [...], decision: pending|keep|delete}"})

            def apply(plan):
                for c in plan["candidates"]:
                    if c["id"] in ids and not c.get("deleted_at"):
                        c["decision"] = decision

            self._send(200, plan_payload(update_plan(plan_path, apply)))

    server = ThreadingHTTPServer(("0.0.0.0", REVIEW_PORT), Handler)
    counts = decision_counts(plan)
    print(f"Reviewing {plan_path.name}: {counts['pending']} pending, {counts['keep']} keep, "
          f"{counts['delete']} delete, {counts['deleted']} already deleted")
    print(f"\n  Open http://localhost:{REVIEW_PORT}\n")
    print("Every click is saved to the plan file. Ctrl-C when done, then:")
    print(f"  docker compose run --rm backsweep delete {plan_path}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
    counts = decision_counts(load_plan(plan_path))
    print(f"\nSaved: {counts['pending']} pending, {counts['keep']} keep, {counts['delete']} delete.")


def confirm_word(prompt: str, expected: str) -> bool:
    try:
        return input(prompt).strip() == expected
    except EOFError:
        return False


def cmd_delete(args):
    plan_path = Path(args.plan)
    plan = load_plan(plan_path)
    criteria, tz = plan["criteria"], plan["criteria"]["tz"]
    keep_ids = set(plan.get("keep_ids", [])) | read_keep_file(args.keep)

    yt = youtube()
    ch = my_channel(yt)
    if ch["id"] != plan["channel_id"]:
        raise Abort(f"Plan is for channel {plan['channel_title']} ({plan['channel_id']}) but the "
                    f"token is for {ch['title']} ({ch['id']}). Refusing.")

    counts = decision_counts(plan)
    planned = [c["id"] for c in plan["candidates"]
               if c["decision"] == "delete" and not c.get("deleted_at")]
    print(f"Channel: {ch['title']}\nPlan: {plan_path.name}, created {plan['created'][:16]}Z — "
          f"{counts['delete']} marked delete, {counts['keep']} keep, {counts['pending']} unreviewed, "
          f"{counts['deleted']} already deleted.")
    if counts["pending"]:
        print(f"  {counts['pending']} unreviewed videos are left alone.")
    if not planned:
        print("Nothing marked Delete. Mark videos in the review page first:")
        print(f"  docker compose run --rm --service-ports backsweep review {plan_path}")
        return
    print(f"Re-checking the {len(planned)} marked Delete against YouTube now…")
    live = fetch_videos(yt, planned)

    cands, dropped = [], []
    for vid in planned:
        v = live.get(vid)
        if v is None:
            dropped.append((vid, "already gone"))
        elif v["channel_id"] != ch["id"]:
            dropped.append((vid, "not on this channel"))
        elif reason := verdict(v, criteria, keep_ids):
            dropped.append((vid, f"no longer a candidate: {reason}" + (f" ({v['views']:,} views)" if v["views"] else "")))
        else:
            cands.append(v)
    gone = sum(1 for _, why in dropped if why == "already gone")
    if gone:
        print(f"  skipping {gone} no longer on YouTube (removed some other way)")
    for vid, why in dropped:
        if why != "already gone":
            print(f"  skipping {vid}: {why}")
    if not cands:
        print("Nothing left to delete.")
        return

    if len(cands) > args.max_deletes:
        print(f"\n{len(cands)} candidates exceeds --max-deletes {args.max_deletes}; "
              f"this run deletes the oldest {args.max_deletes}. Re-run the same plan to continue.")
        cands = cands[:args.max_deletes]

    mv = "none" if criteria.get("max_views") is None else f"≤ {criteria['max_views']:,}"
    print(f"\nCriteria: published before {criteria['before']} ({tz}), views {mv}")
    spot_check(cands, tz, args.sample)

    print(f"\nAbout to PERMANENTLY delete {len(cands)} videos "
          f"({sum(v['views'] or 0 for v in cands):,} views) from {ch['title']}.")
    print("YouTube has no undo. Download anything you want to keep first (see README).")
    expected = f"delete {len(cands)}"
    if not confirm_word(f'Type "{expected}" to proceed: ', expected):
        print("Aborted. Nothing deleted.")
        return

    from googleapiclient.errors import HttpError

    done = 0
    with DELETE_LOG.open("a") as log:
        for v in cands:
            try:
                yt.videos().delete(id=v["id"]).execute()
            except HttpError as e:
                status = e.resp.status
                if status == 404:
                    print(f"  gone already  {v['id']}")
                    continue
                if status == 403 and b"quotaExceeded" in (e.content or b""):
                    print(f"\nDaily API quota used up after {done} deletions. Quota resets at "
                          "midnight Pacific; re-run this same plan then to continue.")
                    break
                print(f"\nStopping on error for {v['id']}: {e}")
                break
            done += 1
            when = dt.datetime.now(dt.timezone.utc).isoformat()
            log.write(json.dumps({**v, "deleted_at": when, "plan": plan_path.name},
                                 ensure_ascii=False) + "\n")
            log.flush()

            def mark(plan, vid=v["id"], when=when):
                for c in plan["candidates"]:
                    if c["id"] == vid:
                        c["deleted_at"] = when

            update_plan(plan_path, mark)
            print(f"  deleted {done:>4}/{len(cands)} {fmt_row(v, tz)}")
            time.sleep(args.pause)
    print(f"\nDeleted {done} videos. Log: {DELETE_LOG}")


# ── cli ─────────────────────────────────────────────────────────────────────

def main(argv=None):
    p = argparse.ArgumentParser(prog="backsweep", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    a = sub.add_parser("auth", help="sign in and confirm which channel the token is for")
    a.add_argument("--force", action="store_true", help="discard the saved token and sign in again")
    a.set_defaults(func=cmd_auth)

    s = sub.add_parser("scan", help="build a deletion plan (deletes nothing)")
    s.add_argument("--before", required=True, metavar="YYYY-MM-DD",
                   help="only videos published before this date are candidates")
    s.add_argument("--max-views", type=int, metavar="N",
                   help="keep any video with more than N views")
    s.add_argument("--keep", metavar="FILE", help="file of video IDs/URLs to never delete")
    s.add_argument("--tz", default=DEFAULT_TZ, help=f"timezone for --before (default {DEFAULT_TZ})")
    s.add_argument("--sample", type=int, default=10, help="random spot-check rows to print")
    s.add_argument("--deep", action="store_true",
                   help="also sweep search.list for anything the playlists miss "
                        "(up to 500 results each, 100 quota units per 50)")
    s.set_defaults(func=cmd_scan)

    r = sub.add_parser("review", help="local page to mark each candidate Keep or Delete")
    r.add_argument("plan", help="plan-*.json written by scan")
    r.set_defaults(func=cmd_review)

    d = sub.add_parser("delete", help="re-verify videos marked Delete, confirm, and delete")
    d.add_argument("plan", help="plan-*.json written by scan")
    d.add_argument("--keep", metavar="FILE", help="extra keep-list applied on top of the plan's")
    d.add_argument("--max-deletes", type=int, default=150,
                   help="cap per run (each delete costs 50 of the 10,000 daily quota units)")
    d.add_argument("--sample", type=int, default=10, help="random spot-check rows to print")
    d.add_argument("--pause", type=float, default=0.5, help="seconds between deletes")
    d.set_defaults(func=cmd_delete)

    args = p.parse_args(argv)
    for d in (SECRETS_DIR, STATE_DIR):  # both are volumes; make them on first run
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError:
            pass  # a later read or write will say what's actually wrong
    if getattr(args, "max_views", None) is not None and args.max_views < 0:
        p.error("--max-views must be >= 0")
    try:
        args.func(args)
    except Abort as e:
        print(f"error: {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\nInterrupted.", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
