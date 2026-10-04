# backsweep

Bulk-delete your own YouTube uploads published before a date, optionally
sparing anything above a view count. Deletion on YouTube is permanent, so
backsweep splits it into scan → review → delete, and only deletes what you
marked by hand.

## One-time setup

1. **Google Cloud project** — at <https://console.cloud.google.com>, create a
   project and enable **YouTube Data API v3**.
2. **Branding / consent screen** — the old "OAuth consent screen" section is now
   **Google Auth Platform** in the left nav (<https://console.cloud.google.com/auth/overview>).
   Fill in *Branding* and pick **External** as the audience.
3. **Test users** — *Google Auth Platform → Audience*
   (<https://console.cloud.google.com/auth/audience>). With *Publishing status:
   Testing*, there's a **Test users** panel at the bottom → **Add users** → your
   own Gmail address → Save. If the page says *In production* instead, there's no
   test-user list; click **Back to testing** first.
4. **OAuth client** — *Google Auth Platform → Clients → Create client → Desktop
   app*. Download the JSON and save it as `secrets/client_secret.json`.
5. **Build and sign in**
   ```sh
   docker compose build
   docker compose run --rm backsweep auth
   ```
   Open the printed URL, approve, then paste the URL your browser ends up on
   (a localhost page that fails to load — that's expected). If your channel is
   a Brand Account, Google asks which channel to use; pick the right one.
   `auth` prints the channel name so you can confirm. `auth --force` redoes it.

   Testing-mode tokens expire after 7 days; just run `auth` again.

## Use

**1. Scan** (deletes nothing):

```sh
docker compose run --rm backsweep scan --before 2024-01-01 --max-views 500
```

- `--before YYYY-MM-DD` (required) — candidates were published before
  midnight at the start of that day, in `BACKSWEEP_TZ` (America/Chicago in
  compose; override with `--tz`).
- `--max-views N` (optional) — anything with **more than N** views is kept.
  Videos with no reported view count are kept too.
- `--deep` (optional) — also sweeps `search.list` for anything the playlists
  miss. Costs 100 quota units per 50 results and YouTube caps it near 500, so
  it's a backstop, not the main source.
- `--keep keep.txt` (optional) — video IDs or URLs, one per line (`#`
  comments allowed), that are never deleted. Put the file in `state/` and pass
  `--keep /state/keep.txt`.

It prints a summary (how many kept and why, how many candidates), a
spot-check table (the newest and most-viewed candidates, nearest the
thresholds, plus a random sample), and writes `state/plan-<time>.json` and
a `.csv` you can open in Numbers. Every candidate starts as **unreviewed**.

**2. Review** (deletes nothing, makes no YouTube API calls):

```sh
docker compose run --rm --service-ports backsweep review /state/plan-20260915-203547.json
```

Open <http://localhost:8780>. Each video shows its thumbnail, title, date,
views and privacy, with **Keep** and **Delete** buttons. Click the thumbnail to
play it inline; the title opens it on YouTube. Every click saves to the plan
JSON right away, so you can stop and pick up later. The plan file on your disk
is the source of truth.

- Filter tabs: All / Unreviewed / Keep / Delete / Deleted. Sort by date or views.
- Keys: `j`/`k` move, `d` delete, `s` keep, `u` back to unreviewed,
  `p` preview, `o` open on YouTube.
- "Unreviewed shown → Keep/Delete" bulk-marks what's on screen. It takes two
  clicks; the first says how many.
- `--service-ports` is what publishes port 8780 (on localhost only). Ctrl-C to stop.

**3. Delete**:

```sh
docker compose run --rm backsweep delete /state/plan-20260915-203547.json
```

Only videos marked **Delete** are touched. Unreviewed and Keep are left alone.
Before deleting anything it:

- refuses if the signed-in channel isn't the one the plan was made for
- re-fetches every marked video and drops any that now break the criteria
  (views went over the limit, on a keep list, already gone)
- shows the spot-check again
- makes you type `delete <count>` exactly

Options: `--keep FILE` adds a keep list, `--max-deletes N` caps a run
(default 150).

Each deletion is stamped into the plan (it shows under **Deleted** in the
review page) and appended to `state/deleted.jsonl` (id, title, date, views,
privacy, url), your record of what's gone.

## Quota

The API gives you 10,000 units a day. A delete costs 50, so about 190
deletions a day after the scan. When quota runs out, backsweep stops cleanly;
run the **same plan** again after midnight Pacific. Videos deleted earlier
count as "already gone" and are skipped.

## Before you run delete

Grab copies of anything you might want again. Google Takeout
(<https://takeout.google.com>, select YouTube) exports all your videos.

## What stays out of git

`secrets/` and `state/` are gitignored, and backsweep creates them on first
run. Nothing in the tracked files is specific to an account:

- `secrets/client_secret.json` — your OAuth client
- `secrets/token.json` — the refresh token; treat it like a password, it can
  read and delete on your channel until you revoke it at
  <https://myaccount.google.com/permissions>
- `state/plan-*.json` / `.csv` — titles and view counts of your videos
- `state/deleted.jsonl` — the record of what you deleted

## Caveats

- Videos are collected from several lists and merged, because the uploads
  playlist alone leaves things out — past live streams especially. Scan prints
  what each contributed:

  | source | what it is |
  | --- | --- |
  | uploads playlist | the channel's `UU…` playlist |
  | live tab | the `UULV…` playlist behind the channel's Live tab |
  | shorts tab, videos tab | the `UUSH…` / `UULF…` playlists |
  | live broadcasts | completed broadcasts from the Live Streaming API |

  The per-tab playlists are undocumented, so a channel may not have them;
  "not available" there is normal. If something you can see on your channel
  still doesn't appear, try `--deep`.
- The scan summary also counts videos by privacy. If private videos you know
  about don't show up, the API isn't returning them and backsweep won't touch
  them.
- The date used is YouTube's `publishedAt`.
