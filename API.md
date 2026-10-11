# SHADOWLINKS Solver API

POST a shortened link, get the real destination back. 144 shortener domains
with a deep handler for GPLinks, plus a universal engine for anything not in
the table.

```bash
curl -X POST https://YOUR-APP.onrender.com/solve \
     -H "X-API-Key: shk...." \
     -H "Content-Type: application/json" \
     -d '{"url":"https://gplinks.co/BFfOX"}'
```

```json
{
  "ok": true,
  "input": "https://gplinks.co/BFfOX",
  "destination": "https://drive.google.com/file/d/1abc/view",
  "shortener": "gplinks.co",
  "handler": "gplinks",
  "hops": 4,
  "elapsed_ms": 6120,
  "cached": false,
  "partial": false,
  "error": ""
}
```

One file: **`solver_api.py`**. No database. No Telegram dependency.

---

## Table of contents

1. [Deploy to Render](#1-deploy-to-render)
2. [Keep it awake with UptimeRobot](#2-keep-it-awake-with-uptimerobot)
3. [Issue API keys](#3-issue-api-keys)
4. [Endpoint reference](#4-endpoint-reference)
5. [Response fields](#5-response-fields)
6. [Error codes](#6-error-codes)
7. [Configuration](#7-configuration)
8. [Does it actually bypass?](#8-does-it-actually-bypass)
9. [Running locally](#9-running-locally)
10. [Testing](#10-testing)
11. [Relationship to the Telegram bot](#11-relationship-to-the-telegram-bot)

---

## 1. Deploy to Render

`render.yaml` is a Blueprint, so Render creates the service and its
environment for you.

```
1.  push this repo to GitHub
2.  Render -> New -> Blueprint -> pick the repo -> Apply
3.  when prompted, set AUTH_SECRET. Generate one first:
        python solver_api.py --print-secret
4.  wait for the build, then open  https://YOUR-APP.onrender.com/health
```

`AUTH_SECRET` is the only variable you must supply. You need to know its value
yourself, because minting a customer key requires the *same* secret the server
holds — the signature is the only record of a key, so a secret you cannot read
is a secret you cannot issue keys with.

Manual deploy instead of the Blueprint:

```
Runtime        Python 3
Build command  pip install -r requirements-api.txt
Start command  python solver_api.py
Health check   /health
Env vars       AUTH_SECRET=<generated>   ADMIN_SECRET=<generated>
```

Render injects `PORT` automatically; the app reads it. Do not hardcode a port.

### Why the free tier is usable here

| Free-tier limit | Effect on this service |
|---|---|
| Spins down after 15 idle minutes, ~1 min to wake | Fixed by UptimeRobot (below) |
| Ephemeral filesystem, wiped on every restart | **No effect** — there is no database. Keys are signed tokens; delays, cache and rate counters live in RAM and rebuild themselves |
| Cannot attach a persistent disk | No effect, same reason |
| 750 instance-hours/month per workspace | 24/7 ≈ 720h. It fits, but only for this one service |
| Render may restart a free service at any time | Costs you learned delays and cache warmth, not data |

Going to the $7/mo instance removes the spin-down and the hour cap. Do it
before you sell access: a caller who hits a waking service gets a Render HTML
loading page instead of JSON.

---

## 2. Keep it awake with UptimeRobot

```
Monitor type    HTTP(s)
URL             https://YOUR-APP.onrender.com/health
Interval        5 minutes
```

Two things that matter:

**Ping `/health`, never `/robots.txt`.** While a free service is asleep Render
intercepts `/robots.txt` itself and answers with a disallow-all. That request
never reaches your app, so it never wakes it — a monitor pointed there reports
healthy forever while the service sleeps.

**`/health` takes no auth and touches nothing.** It returns
`{"status":"ok","version":"2.0.0","uptime_s":N}` in about a millisecond. It was
deliberately kept free of network calls, database access and key verification
so a keep-alive ping can never fail for an unrelated reason.

Note that UptimeRobot keeps the process warm; it does not make the disk
durable. Restarts still wipe local files. That is fine here by design.

---

## 3. Issue API keys

Keys are HMAC-signed tokens that carry their own tier, limits and expiry:

```
shk.<base64url payload>.<base64url HMAC-SHA256 signature>
```

Verification is one hash and a constant-time compare. There is no key table to
store, query, back up or lose.

### Mint

```bash
# locally, using the same AUTH_SECRET the server holds
AUTH_SECRET=<your secret> python solver_api.py --mint acme-corp --tier vip --days 90
```

```
  key      : shk.eyJ0aWVyIjoidmlwIiwiZXhwIjoxNzk5NTA1MzU4fQ.<64-char-hmac>
  id       : k_7dedd7b1   (add to AUTH_REVOKED_IDS to kill it)
  name     : acme-corp
  tier     : vip
  expires  : 2027-01-09 14:35 UTC
  limits   : 120/min, unlimited/day, batch 25
```

**Store the key when you mint it.** It cannot be retrieved later, because it is
not stored anywhere — the signature *is* the record.

Over HTTP instead (needs `ADMIN_SECRET`):

```bash
curl -X POST "https://YOUR-APP.onrender.com/admin/keys?name=acme-corp&tier=vip&days=90" \
     -H "X-Admin-Secret: $ADMIN_SECRET"
```

The CLI is preferable: it never puts a customer key in a request log.

### Tiers

| | free | vip |
|---|---|---|
| requests/min | `RATE_RPM` (30) | 4× that (120) |
| per day | `FREE_DAILY_LIMIT` (100) | unlimited |
| URLs per batch | 5 | `BATCH_LIMIT` (25) |
| expiry | `days`, or 30 if `days=0` | `days`, or **never** if `days=0` |

Override any of them per key: `--rpd 5000 --rpm 60 --batch 10`.

### Verify

```bash
AUTH_SECRET=<secret> python solver_api.py --verify shk....
curl "https://YOUR-APP.onrender.com/admin/keys/verify?key=shk...." -H "X-Admin-Secret: $S"
```

### Revoke

* **One key** — add its id to `AUTH_REVOKED_IDS` (comma-separated) and
  redeploy. The id is printed when you mint, and is the `id` field in the
  payload.
* **Every key** — change `AUTH_SECRET`.

There is no revocation list in a database, because there is no database. That
is the trade you accepted by not needing one: revoking a single key is a config
change and a redeploy rather than a row delete.

---

## 4. Endpoint reference

### Public

| Method | Path | Auth | Purpose |
|---|---|---|---|
| `GET` | `/health` | none | Liveness probe. Point UptimeRobot here |
| `GET` | `/` | none | Service info: version, domain count, backend |
| `GET` | `/docs` | none | Interactive OpenAPI docs — try it in a browser |
| `POST` | `/solve` | key | Resolve one link |
| `GET` | `/solve?url=` | key | Same, browser/curl-testable |
| `POST` | `/batch` | key | Resolve many links |
| `GET` | `/domains` | key | Supported shorteners, grouped by handler |
| `GET` | `/stats` | key | Live counters plus your own usage |

### Admin (`X-Admin-Secret`; the whole surface returns 503 if `ADMIN_SECRET` is unset)

| Method | Path | Purpose |
|---|---|---|
| `POST` | `/admin/keys` | Mint a key |
| `GET` | `/admin/keys/verify` | Inspect a key |
| `GET` | `/admin/delays` | The anti-bot waits the resolver has learned |
| `PUT` | `/admin/delays/{domain}?seconds=N` | Pin a domain's wait |
| `DELETE` | `/admin/cache` | Clear cache, learned delays and rate counters |

### Authentication

Any of these, in this order of preference:

```
X-API-Key: shk....
Authorization: Bearer shk....
GET /solve?url=...&key=shk....        # convenience; avoid in production code
```

### `/batch`

```bash
curl -X POST https://YOUR-APP.onrender.com/batch \
     -H "X-API-Key: shk...." -H "Content-Type: application/json" \
     -d '{"urls":["https://gplinks.co/BFfOX","https://shrinkme.io/abc"]}'
```

```json
{"ok": true, "count": 2, "solved": 2, "failed": 0, "results": [ ... ]}
```

`results` is always in request order, and one bad link never fails the batch —
each entry carries its own `ok` and `error`.

**`parallel` defaults to `false` on purpose.** Bursting many requests at one
shortener from a single datacentre IP is the fastest way to get that IP banned.
Sequential is slower and far more reliable. Set `"parallel": true` only if you
understand that trade and are resolving across many *different* domains.

---

## 5. Response fields

| Field | Type | Meaning |
|---|---|---|
| `ok` | bool | Did it reach a real destination? |
| `input` | str | The URL as normalised (`gplinks.co/x` → `https://gplinks.co/x`) |
| `destination` | str | The final URL. `""` when `ok` is false |
| `shortener` | str | Host of the input link |
| `handler` | str | `gplinks` `form` `redirect` `adfly` `ouo` `linkvertise` `auto` |
| `hops` | int | Number of steps taken |
| `elapsed_ms` | int | Time spent. `0` on a cache hit |
| `cached` | bool | Served from cache |
| `partial` | bool | Failed, but there are candidates worth a look |
| `error` | str | `""` on success, otherwise a specific reason |
| `challenge` | str | Anti-bot system that stopped it — **only present when `ok` is false** |
| `candidates` | list | Possible destinations, on a partial failure |
| `chain` | list | Hop-by-hop trail, only with `"chain": true` |

### Why `challenge` only appears on failures

A walk can legitimately reach a valid destination and *then* hit a wall while
probing for a further hop. `http://github.com` 301s to `https://github.com/`,
and that second page is DataDome-guarded. The destination is still the right
answer, so `ok: true` is correct and `challenge` would only confuse the caller.
The field means exactly one thing: **this is why your request failed.** Use
`chain: true` when you want to see a wall that was hit but did not matter.

---

## 6. Error codes

Every failure — including schema violations — uses one envelope:

```json
{"ok": false, "error": {"code": "expired", "message": "key expired on 2026-09-30"}}
```

| HTTP | `code` | Meaning |
|---|---|---|
| 400 | `bad_url` | Input is not an http(s) URL |
| 400 | `empty` | Batch had no usable URLs |
| 400 | `batch_too_large` | Over this key's batch limit; the response includes `limit` |
| 401 | `missing_key` | No key supplied |
| 401 | `malformed_key` | Not `shk.<payload>.<signature>` |
| 401 | `bad_signature` | Forged, or `AUTH_SECRET` changed since it was minted |
| 401 | `bad_payload` | Signature valid but the payload is unreadable |
| 403 | `expired` | Past its expiry date |
| 403 | `revoked` | Its id is in `AUTH_REVOKED_IDS` |
| 403 | `bad_admin_secret` | Wrong `X-Admin-Secret` |
| 422 | `validation_error` | Body did not match the schema; `details` says which field |
| 429 | `rate_limited` | Over requests/minute. Carries a `Retry-After` header |
| 429 | `quota_exhausted` | Over the daily quota. Resets 00:00 UTC |
| 500 | `server_unconfigured` | Server has no `AUTH_SECRET` |
| 503 | `admin_disabled` | Server has no `ADMIN_SECRET`, so `/admin` is off |
| 504 | `timeout` | Resolver exceeded `SOLVE_TIMEOUT` |

**A link that cannot be resolved is not an error.** It returns HTTP 200 with
`"ok": false` and a specific `error`. Reserve 4xx/5xx for problems with the
*request* or the *server*, so a client can distinguish "your key is dead" from
"this shortener blocked us".

---

## 7. Configuration

All environment variables. Set them in the Render dashboard.

| Variable | Default | Notes |
|---|---|---|
| `AUTH_SECRET` | — | **Required.** Signs and verifies every key. 32+ chars |
| `ADMIN_SECRET` | — | Enables `/admin`. Unset → the surface returns 503 |
| `PORT` | `8080` | Render injects this; do not hardcode |
| `HOST` | `0.0.0.0` | Render only routes to a `0.0.0.0` listener |
| `SOLVE_TIMEOUT` | `90` | Seconds before one link gives up |
| `MAX_CONCURRENCY` | `8` | Parallel outbound resolves. Raising this from one datacentre IP invites bans |
| `HTTP_TIMEOUT` | `20` | Per-request timeout |
| `MAX_HOPS` | `12` | Redirect-chain cap; what stops an infinite loop |
| `GOLINK_DELAY` | `5` | Default anti-bot wait. The resolver learns the real value per domain |
| `ADAPTIVE_DELAY` | `true` | Keep the learned waits |
| `BATCH_LIMIT` | `25` | Max URLs per `/batch` for a VIP key |
| `RATE_RPM` | `30` | Free-tier requests/minute (VIP gets 4×) |
| `RATE_BURST` | `10` | Tolerance above the per-minute rate |
| `FREE_DAILY_LIMIT` | `100` | Free-tier solves per UTC day |
| `CACHE_TTL` | `3600` | Seconds a success is cached. Failures are never cached |
| `CACHE_MAX` | `2000` | Cache entries |
| `USE_CURL_CFFI` | `true` | **Leave on.** Off → no TLS impersonation → Cloudflare sites 403 |
| `IMPERSONATE` | `chrome` | curl_cffi browser profile |
| `PROXY_URL` | — | Outbound proxy for both HTTP backends. See §8 |
| `CORS_ORIGINS` | `*` | Tighten to your own domain before selling |
| `AUTH_REVOKED_IDS` | — | Comma-separated key ids to reject |
| `AUTH_REQUIRED` | `true` | `false` opens the endpoint — local testing only |
| `API_KEY` | — | Optional shared static key, accepted alongside signed keys |
| `ALLOW_PRIVATE_NETWORKS` | `false` | **The service refuses to start if true.** See below |
| `EXTRA_GPLINKS_DOMAINS`<br>`EXTRA_FORM_DOMAINS`<br>`EXTRA_REDIRECT_DOMAINS`<br>`EXTRA_ADFLY_DOMAINS` | — | Add domains without touching code |
| `MAX_BODY_BYTES` | `3000000` | Response truncation |
| `LOG_LEVEL` | `INFO` | |
| `TRUST_FORWARDED_HEADERS` | `true` | Read the client IP from `X-Forwarded-For` |
| `USE_DOTENV` | `false` | Read a local `.env` (development only) |

### SSRF guard

A public resolver is pointed at URLs chosen by strangers. Without the guard,
`POST /solve {"url":"http://169.254.169.254/latest/meta-data/iam/"}` hands an
attacker your cloud credentials. So every outbound request is DNS-resolved
first and refused if it lands on loopback, RFC1918, link-local, CGNAT,
multicast or reserved ranges — including the cloud metadata endpoint.

`ALLOW_PRIVATE_NETWORKS=true` disables it, and **`preflight()` refuses to start
the service** rather than serve an open relay. It exists only so the test suite
can drive fixtures on `127.0.0.1`.

`TRUST_FORWARDED_HEADERS` matters because Render puts the service behind a
proxy: without it every caller shares the proxy's IP and therefore one
rate-limit bucket.

---

## 8. Does it actually bypass?

The honest answer, split into what is verified and what is not.

### Implemented and tested

The GPLinks flow follows that site's real protocol step by step: `302` to
`/<code>?vid=…` → append `skip_sub=1`, which is what defeats the "Go Ad-Free
₹299" paywall → read the `lid`/`pid`/`plid` cookies → call
`/api/update-visitor-value` with `plid {1,11,12}→3, {2}→2, {3}→1` → optionally
decode a base64url ad gate → parse the hidden `<form id="go-link">` inputs →
`POST /links/go` with the XMLHttpRequest header → read
`{"status":"success","url":…}` → keep walking if the answer is another
shortener.

Plus a universal engine for the other 143 domains: redirect chains,
meta-refresh, JS location redirects, hidden POST forms, adf.ly-style `ysmm`
decoding, and per-domain adaptive timer learning.

435 automated checks pass, including 141 that drive this API over real HTTP.

### Not verified — assume it may fail

* **It has never been run against live `gplinks.co`.** The environment it was
  built in cannot reach that site. The protocol is implemented from
  documentation and observed behaviour, not confirmed end-to-end against
  today's production site. **There is no empirical evidence it currently
  works.** Test your own links before you sell anything.
* **Deploying to Render makes the Cloudflare problem worse, not better.**
  This is the single most important thing on this page. Render runs on AWS/GCP
  datacentre IPs. Cloudflare scores those ranges far more harshly than the
  residential or mobile IP your phone uses. A shortener that resolves fine
  from a phone can return 403 from here — and no amount of TLS impersonation
  changes how Cloudflare scores your IP range.
* **`PROXY_URL` is the real fix for that.** A residential or mobile proxy
  (Bright Data, Oxylabs, IPRoyal, Smartproxy) is a paid service. Both HTTP
  backends route through it. Budget for it if GPLinks matters to you.
* **GPLinks rotates** its interstitial domains and flow. A new domain goes in
  `EXTRA_FORM_DOMAINS`; a changed protocol needs code.
* **Linkvertise usually will not work.** It mints tokens inside a real browser.
  Do not advertise it.

### What was found by testing live

The one live test that could be run — resolving `http://github.com` from this
sandbox — immediately exposed a real bug: the resolver hit github.com's
DataDome challenge page and returned **the captcha URL as the destination with
`ok: true`**. A solver that confidently returns garbage is worse than one that
admits failure, so the resolver now identifies 15 anti-bot systems (DataDome,
Cloudflare Turnstile, hCaptcha, reCAPTCHA, GeeTest, Arkose, PerimeterX,
Kasada, Imperva, DDoS-Guard, …), refuses to return any of their URLs as an
answer, and reports `"challenge": "DataDome"` with the reason and the fix.

That bug is worth dwelling on, because it is exactly what will happen to you on
Cloudflare-protected shorteners from a datacentre IP: **they will serve a
challenge page.** The difference is that now the API tells you so instead of
handing your customers a captcha link.

### Find out in two minutes

Deploy, mint a key, then paste 5–10 of your own real links:

| Response | Meaning |
|---|---|
| `ok: true` with a destination | Working for that domain |
| `"challenge": "Cloudflare"` | IP reputation. You need `PROXY_URL` |
| `error` mentioning HTTP 403 | Same, or `USE_CURL_CFFI` is off |
| `timeout` | Anti-bot timer longer than `SOLVE_TIMEOUT`. Retry — it learns. Or pin it with `PUT /admin/delays/{domain}` |
| `partial: true` with candidates | Got close but could not confirm |
| Linkvertise refuses | Expected |

Then decide what to charge for, and do not promise domains you have not watched
succeed.

---

## 9. Running locally

```bash
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements-api.txt

export AUTH_SECRET=$(python solver_api.py --print-secret)
export ADMIN_SECRET=$(python solver_api.py --print-secret)

python solver_api.py --selfcheck     # 34 offline assertions
python solver_api.py                 # serve on :8080, docs at /docs
```

In another shell:

```bash
KEY=$(python solver_api.py --mint me --tier vip --days 1 | grep -oP 'key\s+: \K\S+')
curl -s "http://127.0.0.1:8080/solve?url=https://gplinks.co/BFfOX&key=$KEY" | jq
```

---

## 10. Testing

```bash
python tests/test_api.py         # 141 checks — this API over real HTTP
python tests/test_resolver.py    #  92 — the HTTP layer, on BOTH backends
python tests/test_config.py      #  88 — the bot's config model
python tests/test_ptb.py         # 114 — the bot's Telegram layer
```

`tests/test_api.py` starts a real local HTTP server as a fixture shortener and
drives the API through the full ASGI stack: middleware, dependencies,
validation, handlers. It covers auth (forged, tampered, expired, revoked,
Bearer, query-string), per-key rate limits and daily quotas, batch ordering
and limits, cache behaviour, the SSRF guard against the cloud metadata
endpoint, the admin surface, every deploy mode, and the anti-bot challenge
contract. Nothing reaches the internet.

The fixtures use two hostnames for one server — `127.0.0.1` as the shortener
and `localhost` as the destination — because registering `127.0.0.1` as a
shortener would make the resolver believe it never escaped, and `ok` would be
false.

---

## 11. Relationship to the Telegram bot

`shadowlinks_bot.py` is still in this repo and still works. The API was built by
lifting the bot's resolver engine — the HTTP client, the 144-domain registry
and the resolver itself — **byte for byte**, so the two cannot drift:

```
solver_api.py = new config + DelayStore
              + bot §3 (HTTP client, with PROXY_URL added)
              + bot §4 (registry, verbatim)
              + bot §5 (resolver, verbatim, with DB -> DelayStore)
              + new auth, rate limiting, FastAPI app, CLI
```

Two consequences worth knowing:

* **A resolver fix belongs in the bot and propagates to the API.** The
  anti-bot challenge detection above was fixed in `shadowlinks_bot.py` first
  and the API inherited it. Fixing only the API would have left the bot
  handing captchas to your Telegram users.
* **The bot's SQLite tables (`users`, `codes`, `logs`) are not used here.** The
  only thing the resolver ever touched in the database was the learned
  per-domain delay, so that alone was replaced — with `DelayStore`, in RAM,
  which on Render's ephemeral disk is strictly more reliable than a file.

If you want to drop the bot, delete `shadowlinks_bot.py`, `tests/test_ptb.py`
and `tests/test_config.py`. Nothing in the API imports them.
