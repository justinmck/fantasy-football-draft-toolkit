# Hosting this

`README.md` covers what has to be *true* before other people use this — identity,
ownership, secrets, the database split. This covers where it can actually run,
and why most of the obvious answers don't work.

## What the app needs from a host

Four properties, all verifiable in the code, and together they rule out most of
the cheap options:

**One process.** `src/limits.py` keeps rate-limit buckets, `src/api.py` keeps
sessions, and `src/jobs.py` keeps its `ThreadPoolExecutor` — all in memory. Two
workers means two sets of rate limits and a job submitted to one worker that the
other can't report on. Run `--workers 1` until there's a Redis behind it.

**A disk that survives a restart.** `data/runtime/fantasy_data.db` is 6.3 MB of
pulled league history and `data/runtime/auth.db` holds the encrypted ESPN
cookies. On an ephemeral filesystem every deploy signs everyone out and throws
away every season they pulled — which, at one pull per 20 minutes, is not a
small loss.

**Minutes-long background work.** A full history pull is one ESPN request per
season plus one per season for player stats. For a fourteen-season league that
is a couple of minutes, running after the HTTP response has already returned.

**Outbound HTTPS, from a stable address.** Every pull and every live sync goes
to `lm-api-reads.fantasy.espn.com`. ESPN throttles by IP, so a host that shares
egress IPs with thousands of other tenants is a liability.

### What this rules out

| | Why not |
|---|---|
| Vercel / Netlify functions | Ephemeral filesystem; the SQLite files don't survive. Execution caps kill the pull. |
| AWS Lambda / Cloud Run (default) | Same two problems. Cloud Run works only with a mounted volume and min-instances 1, at which point it is a small VM with extra steps. |
| Anything autoscaling | Multiple instances split the in-process state. |

### What works

A single small always-on container with a persistent volume. Cheapest credible
options, all roughly $5/month for the backend:

- **Fly.io** — one `shared-cpu-1x` machine, 256 MB, plus a 1 GB volume. Closest
  fit: volumes are first-class, and `min_machines_running = 1` is one line.
- **Railway** or **Render** — same shape, add a persistent disk mounted at
  `/data`. Slightly simpler, slightly less control.
- **A $5 VPS** (Hetzner, DigitalOcean) with systemd and Caddy. Most control,
  most maintenance, and the only one where you own the IP outright.

The **frontend is 1.3 MB of static files** and should not be on that box. Build
it and put it on Cloudflare Pages or Netlify for free.

---

## The shape

```
Cloudflare Pages          Fly.io machine (1 process, 1 volume)
┌───────────────────┐     ┌──────────────────────────────────┐
│ draft-board/dist  │────▶│ uvicorn src.api:app --workers 1  │
│ VITE_API_URL ─────┼─────│   /data/fantasy_data.db          │
└───────────────────┘     │   /data/auth.db  (0600)          │
    static, free          └───────────────┬──────────────────┘
                                          │ HTTPS
                                          ▼
                                   ESPN fantasy API
```

Two origins means **CORS is load-bearing**, and `API_ORIGINS` is validated at
startup rather than trusted: it rejects `*` with credentials, entries with a
path, and untrimmed commas, because each of those fails silently at request time
instead. In `APP_ENV=prod` it also refuses a non-https origin.

---

## Steps

### 1. Containerise

There is no Dockerfile yet — this is the one piece of new work. It needs to
install `requirements.txt`, copy `src/` and `notebooks/config.py`, and run
uvicorn with a single worker. `cryptography` needs a recent rustc if it builds
from source, so pin `--only-binary=:all:` as the local setup already does.

### 2. Set the secrets

`APP_ENV=prod` refuses to boot without these rather than generating values that
change on every deploy:

| Variable | Notes |
|---|---|
| `APP_SECRET` | Encrypts stored cookies. Rotatable via `APP_SECRET_OLD`. |
| `APP_PEPPER` | Derives `user_id` from a SWID. **Never rotate** — it orphans every session and signs everyone out mid-draft. |
| `API_ORIGINS` | The Pages URL. https only in prod. |
| `APP_ENV` | `prod`. |

Set them as platform secrets, not in an image layer.

### 3. Mount the volume

`/data`, with `DATABASE_URL` and `AUTH_DB_URL` pointing into it. First boot
seeds `fantasy_data.db` from the tracked `data/reference.db` (0.4 MB, the
league-independent tables only — see `notebooks/build_reference_db.py`, which
refuses to ship anything carrying an account id).

### 4. Deploy the frontend

`VITE_API_URL` is read at **build** time (`DraftBoard.jsx:36`), not runtime, so
it has to be set in the Pages build environment. Getting it wrong produces an
app that tries to reach `http://localhost:8000` from your users' browsers.

### 5. Back up the volume

`auth.db` is the one file whose loss is unrecoverable — everyone signs in again.
`fantasy_data.db` is rebuildable from ESPN but slowly. A nightly snapshot of
`/data` is enough; both are SQLite, so `.backup` gives a consistent copy while
the app is running.

---

## Before you invite anyone

- **`APP_ENV=prod`** — it turns three warnings into refusals: generated dev
  secrets, operator credentials from `.env`, and non-https CORS.
- **Check `data/runtime/` is not in the image.** It is gitignored, but a
  `COPY . .` in a Dockerfile ignores `.gitignore` unless `.dockerignore` says so.
  That directory has held real ESPN cookies.
- **Raise `limit_analysis_run`** if you expect several people at once. Three
  pulls an hour is generous for one person and tight for ten.
- **`data/device_tokens.json.migrated`** is the leftover from before the
  encrypted store. Checked: it holds only `created` and `kind`, no credentials,
  so it is harmless — but it is worth deleting so nobody has to check again.

## Known rough edges for other people's leagues

Worth knowing before you hand someone the link, because none of these is a
crash — they are things that will look wrong to them:

- **Sign-in is copy-pasting two cookies out of devtools.** ESPN has no public
  OAuth, so this is the only way in. It is the single biggest reason someone
  bounces.
- **The methodology sections are one league's model.** Projection reliability by
  position, unproven players, the regression diagnostics and bench depth come
  from whichever league ran the notebooks. They are labelled as such for anyone
  else (`ReferenceNote`), but they are not that person's numbers.
- **ESPN serves no ADP before 2020**, so steals, reaches and league-habit fits
  start there however long the league has run. Championships, luck and the
  career leaderboard cover the full history.
- **One ESPN account, one IP.** If several people pull at once you are sharing
  one egress address with ESPN's rate limiter.
