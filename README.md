# 🕶 SHADOWLINKS

**A VIP link-resolver Telegram bot, in one Python file.**

Paste a shortened, ad-gated link into the chat and get the **real destination URL**
back — no ads, no timer, no captcha, no browser.

```
You  ▸ https://gplinks.co/BFfOX

Bot  ▸ 🎯 Destination unlocked

       https://real-destination.example.com/files/report.pdf

       🔗 Shortener : gplinks.co
       ⚙️ Strategy   : gplinks
       🪜 Hops       : 3
       ⏱ Resolved   : 11.42s
```

Built primarily for **GPLinks**, with a universal engine that also handles
**144 shortener domains** out of the box — and any domain *not* in the table.

---

## Contents

- [What it does](#what-it-does)
- [How the GPLinks unlock works](#how-the-gplinks-unlock-works)
- [Supported shorteners](#supported-shorteners)
- [Quick start](#quick-start)
- [Commands](#commands)
- [Configuration](#configuration)
- [Deployment](#deployment)
- [Adding shorteners without redeploying](#adding-shorteners-without-redeploying)
- [Security](#security)
- [Testing](#testing)
- [Limitations](#limitations)

---

## What it does

| | Free | VIP 👑 |
|---|---|---|
| Resolves per day | 5 | unlimited |
| Cooldown between links | 45s | none |
| Batch mode | — | up to 25 links per message |
| Full redirect chain | — | ✅ shown on every result |

VIP is granted by you, the owner — either directly, or with self-serve redeem
codes you generate and sell/distribute however you like.

**The entire bot is [`shadowlinks_bot.py`](shadowlinks_bot.py).** No package
layout, no plugin directory, no build step. Config comes from the environment
or a `.env` file; state lives in one SQLite file.

---

## How the GPLinks unlock works

GPLinks does not answer a short link with a plain redirect. It puts a paywall, a
visitor-tracking cookie chain and an ad-gate domain in front of the destination.
The bot walks all of it:

```
1.  GET  https://gplinks.co/BFfOX           (redirects disabled)
      ↳ 302 Location: /BFfOX?vid=99887766   ← the visitor id
      ↳ Set-Cookie: vid=99887766

2.  GET  .../BFfOX?vid=…&skip_sub=1          ← defeats the "Go Ad-Free ₹299"
                                               paywall screen
      ↳ Set-Cookie: lid=… pid=194570 plid=1

3.  GET  https://gplinks.com/api/update-visitor-value
           ?vid=99887766&value=3&impressions=0
      ↳ tells the ad gate every step is satisfied. `value` is derived from
        the plan cookie: plid 1/11/12 → 3, plid 2 → 2, plid 3 → 1

4.  if the interstitial is a powergam-style ad gate
      ?lid=Z1B4elhteUQ&pid=MTk0NTcw&vid=…&pages=Mg
      ↳ base64url-decode lid and pid straight into
        https://gplinks.co/gPxzXmyD?pid=194570&vid=…    (no request needed)

5.  parse <form id="go-link">, collect its hidden inputs (_token, alias)

6.  POST https://gplinks.co/links/go
      headers: x-requested-with: XMLHttpRequest
               content-type: application/x-www-form-urlencoded
               referer / origin: the interstitial
      ↳ {"status":"success","url":"https://real-destination.example.com/…"}

7.  keep walking — the answer may be another shortener or ad gate
```

### The adaptive timer

GPLinks enforces a countdown before the POST is accepted. Naively waiting 10s on
every request makes the bot feel broken, but skipping the wait fails.

So it **learns**. The first attempt goes out immediately. If the server replies
`"please wait for the timer"`, the bot waits `GOLINK_DELAY` seconds, re-fetches a
fresh token and retries — then **persists that delay for the domain**. Every
later request for that domain waits the right amount on the first try.

Each wait is jittered ±8% so the traffic does not fingerprint as a loop.
Inspect and override the learned values with `/delays` and `/setdelay`.

### Why curl_cffi

Most of these sites sit behind Cloudflare and reject plain HTTP clients by TLS
fingerprint. `curl_cffi` impersonates a real Chrome handshake (JA3/JA4, HTTP/2
frame order, header casing), which is what actually gets through. If it cannot
be installed on your platform the bot silently falls back to `aiohttp` and logs
it — everything still runs, some sites just 403.

---

## Supported shorteners

144 domains registered natively, grouped by the strategy they need:

| Group | Domains | Examples |
|---|---:|---|
| 🎯 **GPLinks** | 12 | `gplinks.co` `.in` `.com` `.org` `.site` `.xyz` `.pro` `.net`, `gplink.live`, `gplink.co` |
| 📝 **Form shorteners** | 45 | `rocklinks.net`, `gyanilinks.com`, `gtlinks.me`, `shortingly.me`, `fc.lc`, `nanolinks.in`, `tryshort.in`, `droplink.co`, `exe.io`, `linkjust.com`, `shrinkme.io`, `desiupload.co`, `mplaylink.com`, `eklink.net`, `urlshortx.com`, `sharedisklinks.com` … |
| 🪙 **aylink / cpmlink** | 7 | `aylink.co`, `ay.live`, `mbky.link`, `cpmlink.co`, `cpmlink.pro`, `bildirim.online` |
| ↪️ **Plain redirectors** | 47 | `bit.ly`, `tinyurl.com`, `cutt.ly`, `is.gd`, `v.gd`, `t.ly`, `rb.gy`, `rebrand.ly`, `goo.gl`, `ow.ly`, `lnkd.in`, `t.co`, `amzn.to`, `s.id`, `shorturl.at`, `dub.sh` … |
| 🔐 **adf.ly / ysmm** | 18 | `adf.ly`, `q.gs`, `j.gs`, `adfoc.us`, `gestyy.com`, `ceesty.com`, `sh.st`, `pnd.tl`, `microify.com` … |
| 🧩 **Linkvertise** | 8 | `linkvertise.com`, `linkvertise.net`, `link-to.net`, `direct-link.net`, `the-first.net` |
| 🔁 **ouo.io** | 3 | `ouo.io`, `ouo.press`, `ouo.today` |
| 📜 **JS shorteners** | 4 | `shorte.st`, `exeo.app`, `boost.ink`, `sstool.us` |

**Anything not listed still works** through the universal auto-detect engine,
which tries, in order:

1. decode a `powergam`-style base64url ad gate (`lid`/`pid`/`vid`)
2. follow bare HTTP redirects
3. defeat a `skip_sub=1` paywall if one is detected
4. decode an adf.ly `ysmm` token
5. find and POST any hidden-input form (`#go-link`, `/links/go`, `/get/tk`, …)
6. follow `<meta http-equiv="refresh">`
7. follow JS redirects — `window.location`, `location.replace()`, `var url = …`
8. read a JSON `{"url": …}` body
9. as a last resort, on pages that are *obviously* ad gates, pick the strongest
   outbound CTA link

Loose extraction (steps 8–9) only runs when the page shows interstitial
signals — a countdown, "please wait", a captcha, a `go-link` form. On a genuine
destination page the bot never follows a random outbound link and reports
success wrongly.

Send `/sites` to the bot for the live list.

---

## Quick start

### 1. Create the bot

Talk to [@BotFather](https://t.me/BotFather) → `/newbot` → copy the token.
Then `/setjoingroups` → disable, and `/setprivacy` → disable if you want it
usable in groups.

Get your own numeric user id from [@userinfobot](https://t.me/userinfobot).

### 2. Install

```bash
git clone https://github.com/demoneditz985-ctrl/SHADOWLINKS.git
cd SHADOWLINKS

python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

Python **3.10+** required.

### 3. Configure

```bash
cp .env.example .env
nano .env        # set BOT_TOKEN and ADMIN_IDS — the only two required values
```

### 4. Verify

```bash
python shadowlinks_bot.py --check
```

```
SHADOWLINKS v1.0.0
  registry domains : 144
  handlers         : ['adfly','auto','form','gplinks','linkvertise','ouo','redirect']
  backend          : curl_cffi (chrome)
  db               : /opt/SHADOWLINKS/shadowlinks.db
  free limit       : 5/day, 45s cooldown
  vip batch        : 25 links/message
  admins           : (111111111,)
  token            : set
```

The bot refuses to start with an actionable message if `BOT_TOKEN` or
`ADMIN_IDS` is missing, rather than dying in a traceback.

### 5. Run

```bash
python shadowlinks_bot.py
```

Your admin account gets a "bot online" DM on every boot.

---

## Commands

### Everyone

| Command | What it does |
|---|---|
| *just paste a link* | The main interface. No command needed. |
| `/start` | Welcome + plan. Also redeems a code: `/start SHADOW-ABC123` |
| `/status`, `/me` | Plan, expiry, quota used, cooldown, lifetime count |
| `/sites`, `/domains` | Every registered shortener, grouped |
| `/plans`, `/vip` | What VIP includes |
| `/redeem <CODE>` | Activate a VIP code |
| `/resolve <url>`, `/r <url>` | Explicit resolve |
| `/batch` | VIP: collect links over several messages, finish with `/done` |
| `/help` | Usage + troubleshooting |
| `/cancel` | Abort batch mode |

Every result carries inline buttons: **📋 Copy link**, **🧭 Chain**,
**🔁 Resolve again**.

### Admins

| Command | What it does |
|---|---|
| `/addvip <id\|@user> [days\|perm]` | Grant VIP (default 30 days; `perm` = never expires) |
| `/delvip <id\|@user>` | Revoke |
| `/listvip` | Everyone currently VIP, with expiry |
| `/whois <id\|@user>` | Full record for one user |
| `/gencode [days] [count] [note]` | Mint up to 50 codes, e.g. `/gencode 30 5 launch` |
| `/codes` | Live unused codes |
| `/revoke <CODE>` | Kill a code |
| `/ban <id>` · `/unban <id>` | Block / restore |
| `/setadmin <id>` · `/unsetadmin <id>` | Promote / demote |
| `/stats` | Users, VIP count, success rate, avg latency, top shorteners |
| `/logs [n]` | Last *n* resolves with input, output and error |
| `/delays` | Learned per-domain anti-bot timers |
| `/setdelay <domain> <secs>` | Pin a timer manually |
| `/broadcast <text>` | Message every known user (rate-limited, reports delivery) |
| `/admin` | The admin panel cheat-sheet |

Codes look like `SHADOW-7K3M9Q` — an alphabet with no `I`, `O`, `0` or `1`, so
nobody misreads one over a screenshot.

---

## Configuration

Every knob is an environment variable (or a line in `.env`). Full annotated
list in [`.env.example`](.env.example). The ones you will actually touch:

| Variable | Default | Notes |
|---|---|---|
| `BOT_TOKEN` | — | **required** |
| `ADMIN_IDS` | — | **required.** Comma separated user ids |
| `FREE_DAILY_LIMIT` | `5` | `0` disables the cap |
| `FREE_COOLDOWN` | `45` | seconds between free resolves |
| `VIP_BATCH_LIMIT` | `25` | links per batch message |
| `GOLINK_DELAY` | `10` | anti-bot wait once a site demands one |
| `ADAPTIVE_DELAY` | `1` | learn each site's timer; turn off to always wait |
| `MAX_HOPS` | `14` | redirect chain ceiling |
| `MAX_CONCURRENCY` | `8` | resolves in flight — raise on a big VPS |
| `IMPERSONATE` | `chrome` | also `safari`, `edge`, `firefox` |
| `SUPPORT_USERNAME` | — | shown on `/start` and `/plans` as the VIP contact |
| `PRICE_LINE` | — | your own VIP pitch, shown on `/start` |
| `LOG_LEVEL` | `INFO` | `DEBUG` logs every hop |

---

## Deployment

### systemd (a plain VPS)

```ini
# /etc/systemd/system/shadowlinks.service
[Unit]
Description=SHADOWLINKS Telegram bot
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=shadowlinks
WorkingDirectory=/opt/SHADOWLINKS
EnvironmentFile=/opt/SHADOWLINKS/.env
ExecStart=/opt/SHADOWLINKS/.venv/bin/python shadowlinks_bot.py
Restart=always
RestartSec=5
# hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/opt/SHADOWLINKS

[Install]
WantedBy=multi-user.target
```

```bash
sudo systemctl daemon-reload
sudo systemctl enable --now shadowlinks
journalctl -u shadowlinks -f
```

### Docker

```dockerfile
FROM python:3.12-slim
WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY shadowlinks_bot.py .
VOLUME ["/data"]
ENV DB_PATH=/data/shadowlinks.db
CMD ["python", "shadowlinks_bot.py"]
```

```bash
docker build -t shadowlinks .
docker run -d --name shadowlinks --restart unless-stopped \
  --env-file .env -v shadowlinks-data:/data shadowlinks
```

The bot is pure long-polling — **no inbound ports, no webhook, no reverse
proxy**. It runs anywhere with outbound HTTPS.

---

## Adding shorteners without redeploying

Shorteners change domain constantly. You do not need to edit code or rebuild —
put the new hostnames in `.env` and restart:

```bash
EXTRA_GPLINKS_DOMAINS=gplinks.newdomain.in,gplinksmirror.co
EXTRA_FORM_DOMAINS=newshrink.site,anotherlink.in
EXTRA_REDIRECT_DOMAINS=tiny.new
EXTRA_ADFLY_DOMAINS=clonefly.com
```

Each list maps to the matching strategy. The bot logs the resulting registry
size at boot, and `/stats` shows it too.

If a link fails, the error message tells you which strategy ran and how far the
chain got — that is usually enough to know which `EXTRA_*` list it belongs in.

---

## Security

- **SSRF protection is on by default.** Every hostname is DNS-resolved and
  checked against loopback, RFC1918, link-local, CGNAT, multicast and reserved
  ranges *before* a socket opens. Nobody can use your bot to probe
  `169.254.169.254` or your own LAN. `ALLOW_PRIVATE_NETWORKS=1` disables this —
  do not set it outside a test box.
- **Per-request cookie isolation.** Every resolve gets its own HTTP session, so
  one user's `vid`/`lid` tracking cookies can never leak into another's.
- **Bounded everything** — hop count, body size, concurrency, per-request and
  per-resolve timeouts. A hostile link cannot pin a worker.
- **Admin commands are gated** on `ADMIN_IDS` *and* the `is_admin` DB flag, and
  every non-admin attempt is refused explicitly.
- **Bans are enforced in middleware**, before any handler runs.
- All user-controlled text is HTML-escaped before it reaches Telegram's parser.
- Logs are trimmed to 14 days on shutdown.

---

## Testing

The bot ships with no test files (single-file constraint), but the suite it was
developed against covers it end to end — **76 checks, all passing**:

- **Resolver** — a local HTTP server reproduces the real GPLinks flow
  (302 → paywall → `skip_sub` → form → timer rejection → JSON), plus plain
  redirects, multi-hop chains, meta-refresh, JS redirects, `ysmm` decoding,
  `powergam` interstitial decoding, SSRF refusal and 5-way concurrency.
- **Bot** — real `Update` objects fed through the dispatcher with a stubbed
  Telegram API: every command, quota enforcement, cooldowns, admin gating,
  code minting/redemption/reuse-rejection, deep-link redemption, VIP batch FSM,
  inline callbacks, ban enforcement and the SSRF path from inside a chat.

Two bugs this caught in development are worth knowing about, because they are
the kind that ship silently:

- aiogram calls handlers by **keyword name** and runs non-coroutine callbacks in
  a thread pool — so `dp.message.register(lambda m, u, c: ...)` never awaits.
- a `dict` comprehension calling `kw.pop("label")` only pops on the first
  iteration, which quietly mislabelled 130 domains.

---

## Limitations

Being straight with you, because these will otherwise look like bugs:

- **Linkvertise** mints its bypass tokens inside a real browser and rotates the
  scheme often. The bot attempts the publisher API and the page, but when it
  cannot win it **says so explicitly** instead of guessing and handing back a
  wrong URL.
- **Cloudflare hard challenges** (JS challenge / Turnstile) sometimes reject
  datacentre IP ranges outright. TLS impersonation beats the passive fingerprint
  check, not an active JS challenge. A residential IP fixes this.
- **Shorteners change.** The auto-detect engine absorbs most changes, and
  `EXTRA_*_DOMAINS` covers new hosts, but a site that redesigns its flow needs a
  new handler.
- `/logs` and `/stats` store the links people resolve. That is user data —
  set `LOG_RESOLVES=0` if you would rather not hold it.

Use this on links you are entitled to resolve. Ad-gated shorteners are how their
publishers get paid; bypassing them may breach the shortener's terms of service,
and running the bot is your call and your responsibility.

---

## Project layout

```
SHADOWLINKS/
├── shadowlinks_bot.py    ← the entire bot (~3,200 lines)
├── requirements.txt      ← 4 dependencies
├── .env.example          ← every setting, annotated
├── .gitignore            ← keeps .env and *.db out of git
└── README.md
```

`shadowlinks.db` is created on first run and git-ignored.

---

**v1.0.0** · Python 3.10+ · aiogram 3 · curl_cffi · BeautifulSoup 4 · SQLite
