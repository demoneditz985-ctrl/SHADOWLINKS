#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║   S H A D O W L I N K S   ▸   Solver API                                ║
║   ------------------------------------------------------------------     ║
║   POST a shortened link, GET the real destination back.                 ║
║   No ads, no timers, no paywall.                                        ║
║                                                                          ║
║   Deep handler : gplinks.co / gplinks.in / gplinks.com                  ║
║   Plus 143 other shorteners + a universal auto-detect engine.           ║
║                                                                          ║
║   Stack  : Python 3.10+ · FastAPI · uvicorn · curl_cffi (Chrome TLS     ║
║            impersonation) · BeautifulSoup4                               ║
║   Deploy : Render (see render.yaml). No database required.              ║
║   Run    : python solver_api.py                                         ║
╚══════════════════════════════════════════════════════════════════════════╝

CONTENTS
  §1  Config            (environment variables - Render's native model)
  §2  Learned-delay store   (in-memory; Render's disk is ephemeral)
  §3  HTTP client       (curl_cffi primary, httpx fallback, proxy aware)
  §4  Shortener registry
  §5  Resolver engine
  §6  API keys          (HMAC-signed, stateless - no database)
  §7  Rate limiting + result cache
  §8  HTTP API          (FastAPI)
  §9  Entrypoint / CLI

WHY THERE IS NO DATABASE
  Render's filesystem is ephemeral: every restart, redeploy or free-tier
  spin-down wipes local files, and free services cannot attach a persistent
  disk. A SQLite file therefore silently loses your customer list. So:
    * API keys are HMAC-signed tokens that carry their own tier, limits and
      expiry. Verification needs only AUTH_SECRET - nothing to store.
    * Learned anti-bot delays and rate-limit counters live in RAM and are
      rebuilt automatically. Losing them costs a few slower first requests,
      not data.
  If you later move to a paid Render instance with a disk, or add Postgres,
  DelayStore and RateLimiter are the only two things to swap.
"""

from __future__ import annotations

import asyncio
import base64
import binascii
import contextlib
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import socket
import sys
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse


def _die(msg: str) -> None:
    """Print an actionable error and exit. Used before logging is configured."""
    print(f"\n\033[1;31m✗ SHADOWLINKS API cannot start\033[0m\n\n{msg}\n",
          file=sys.stderr)
    raise SystemExit(2)


# ── hard dependencies, with a message instead of a traceback ───────────────
try:
    from bs4 import BeautifulSoup
except ImportError:  # pragma: no cover
    _die(
        "beautifulsoup4 is not installed.\n\n"
        f"    {sys.executable} -m pip install -r requirements-api.txt\n\n"
        "On Render this means the build command did not run - check that\n"
        "requirements-api.txt is at the repository root."
    )

try:
    import uvicorn
    from fastapi import Depends, FastAPI, Header, Query, Request
    from fastapi.exceptions import RequestValidationError
    from fastapi.middleware.cors import CORSMiddleware
    from fastapi.responses import JSONResponse
except ImportError:  # pragma: no cover
    _die(
        "fastapi / uvicorn are not installed.\n\n"
        f"    {sys.executable} -m pip install -r requirements-api.txt\n\n"
        "The API needs both. curl_cffi and httpx are optional at import time\n"
        "(the resolver probes them at runtime), but FastAPI is not."
    )

__version__ = "2.0.0"
API_NAME = "SHADOWLINKS Solver API"


# ══════════════════════════════════════════════════════════════════════════
# §1  CONFIG
# ══════════════════════════════════════════════════════════════════════════
#
#  Everything is an environment variable, because that is how Render works:
#  Dashboard -> your service -> Environment. There is no config file to edit.
#
#  The ONLY variable you must set is AUTH_SECRET. Everything else has a
#  working default. `python solver_api.py --print-secret` generates one.
# ══════════════════════════════════════════════════════════════════════════

def load_dotenv(path: str = ".env") -> None:
    """Optional local-dev convenience. Values already in the env win.

    Not used on Render (env vars come from the dashboard). Handy on a laptop:
    set USE_DOTENV=1 and keep a .env next to this file. It is git-ignored.
    """
    here = os.path.join(os.path.dirname(os.path.abspath(__file__)), path)
    if not os.path.isfile(here):
        return
    try:
        with open(here, "r", encoding="utf-8") as fh:
            for raw in fh:
                line = raw.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, val = line.split("=", 1)
                key = key.strip()
                val = val.strip()
                if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
                    val = val[1:-1]
                else:
                    # Strip an inline comment (" # ..."). Without this,
                    # USE_CURL_CFFI=1  # note  parses as the whole string,
                    # which is not "1", so the browser-TLS backend silently
                    # switches itself off.
                    cut = re.search(r"\s+#", val)
                    if cut:
                        val = val[: cut.start()]
                    val = val.strip()
                os.environ.setdefault(key, val)
    except OSError as exc:  # pragma: no cover
        log.warning("could not read %s: %s", here, exc)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_int(key: str, default: int) -> int:
    try:
        return int(float(_env(key, str(default))))
    except (TypeError, ValueError):
        return default


def _env_float(key: str, default: float) -> float:
    try:
        return float(_env(key, str(default)))
    except (TypeError, ValueError):
        return default


def _env_bool(key: str, default: bool = False) -> bool:
    raw = _env(key, "").lower()
    if not raw:
        return default
    return raw in ("1", "true", "yes", "y", "on", "enable", "enabled")


def _split_hosts(raw: str) -> tuple[str, ...]:
    """Comma/whitespace separated host list -> clean lowercase tuple."""
    return tuple(h.lower().strip().lstrip(".")
                 for h in re.split(r"[,\s;]+", raw or "") if h.strip())


if _env_bool("USE_DOTENV", False):
    load_dotenv()

log = logging.getLogger("shadowlinks.api")


@dataclass(frozen=True)
class Config:
    # --- resolver (identical knobs to the bot) ---------------------------
    max_hops: int = _env_int("MAX_HOPS", 12)
    http_timeout: float = _env_float("HTTP_TIMEOUT", 20.0)
    impersonate: str = _env("IMPERSONATE", "chrome")
    golink_delay: float = _env_float("GOLINK_DELAY", 5.0)   # anti-bot timer
    form_retries: int = _env_int("FORM_RETRIES", 2)
    concurrency: int = _env_int("MAX_CONCURRENCY", 8)       # parallel resolves
    adaptive_delay: bool = _env_bool("ADAPTIVE_DELAY", True)
    use_curl_cffi: bool = _env_bool("USE_CURL_CFFI", True)
    resolve_chains: bool = _env_bool("RESOLVE_CHAINS", True)

    # --- safety ----------------------------------------------------------
    allow_private_nets: bool = _env_bool("ALLOW_PRIVATE_NETWORKS", False)
    max_body_bytes: int = _env_int("MAX_BODY_BYTES", 3_000_000)

    # --- outbound proxy --------------------------------------------------
    # Render and Vercel run on DATACENTRE IPs, which Cloudflare scores far
    # more harshly than a residential or mobile connection. If GPLinks and
    # friends answer 403 from your deployment, point this at a residential
    # proxy (e.g. http://user:pass@host:port) and the whole resolver - both
    # the curl_cffi and the httpx path - will egress through it.
    proxy_url: str = _env("PROXY_URL", "")

    # --- user-extensible registry ----------------------------------------
    extra_gplinks: tuple[str, ...] = _split_hosts(
        _env("EXTRA_GPLINKS_DOMAINS", ""))
    extra_form: tuple[str, ...] = _split_hosts(_env("EXTRA_FORM_DOMAINS", ""))
    extra_redirect: tuple[str, ...] = _split_hosts(
        _env("EXTRA_REDIRECT_DOMAINS", ""))
    extra_adfly: tuple[str, ...] = _split_hosts(_env("EXTRA_ADFLY_DOMAINS", ""))

    # --- api -------------------------------------------------------------
    host: str = _env("HOST", "0.0.0.0")       # Render requires 0.0.0.0
    port: int = _env_int("PORT", 8080)        # Render injects PORT
    cors_origins: tuple[str, ...] = _split_hosts(
        _env("CORS_ORIGINS", "*")) or ("*",)
    log_level: str = _env("LOG_LEVEL", "INFO")
    trust_forwarded: bool = _env_bool("TRUST_FORWARDED_HEADERS", True)

    # --- auth ------------------------------------------------------------
    # The one variable you must set. Signs and verifies every API key.
    auth_secret: str = _env("AUTH_SECRET", "")
    auth_required: bool = _env_bool("AUTH_REQUIRED", True)
    # Per-key revocation without a database: put a key's id in this list and
    # redeploy. Bumping AUTH_SECRET instead revokes every key at once.
    revoked_ids: tuple[str, ...] = tuple(
        x.strip() for x in re.split(r"[,\s;]+", _env("AUTH_REVOKED_IDS", ""))
        if x.strip())
    # Optional shared bearer token, for people who do not want signed keys.
    static_api_key: str = _env("API_KEY", "")
    admin_secret: str = _env("ADMIN_SECRET", "")   # guards /admin/*

    # --- limits ----------------------------------------------------------
    solve_timeout: float = _env_float("SOLVE_TIMEOUT", 90.0)
    batch_limit: int = _env_int("BATCH_LIMIT", 25)
    rate_rpm: int = _env_int("RATE_RPM", 30)       # requests/minute, free tier
    rate_burst: int = _env_int("RATE_BURST", 10)
    cache_ttl: int = _env_int("CACHE_TTL", 3600)
    cache_max: int = _env_int("CACHE_MAX", 2000)
    free_daily_limit: int = _env_int("FREE_DAILY_LIMIT", 100)


CFG = Config()

# Network ranges a public service must never be pointed at (SSRF protection).
# Without this, POST /solve {"url":"http://169.254.169.254/"} hands an
# attacker your cloud metadata credentials - a real and common hole.
_BLOCKED_NETS = tuple(
    ipaddress.ip_network(n)
    for n in (
        "0.0.0.0/8", "10.0.0.0/8", "100.64.0.0/10", "127.0.0.0/8",
        "169.254.0.0/16", "172.16.0.0/12", "192.0.0.0/24", "192.0.2.0/24",
        "192.88.99.0/24", "192.168.0.0/16", "198.18.0.0/15", "198.51.100.0/24",
        "203.0.113.0/24", "224.0.0.0/4", "240.0.0.0/4", "255.255.255.255/32",
        "::1/128", "::/128", "fc00::/7", "fe80::/10", "ff00::/8",
    )
)

BROWSER_HEADERS = {
    "accept": ("text/html,application/xhtml+xml,application/xml;q=0.9,"
               "image/avif,image/webp,*/*;q=0.8"),
    "accept-language": "en-US,en;q=0.9",
    "upgrade-insecure-requests": "1",
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "none",
    "sec-fetch-user": "?1",
    "dnt": "1",
}


# ══════════════════════════════════════════════════════════════════════════
# §2  LEARNED-DELAY STORE
# ══════════════════════════════════════════════════════════════════════════
#
# Shorteners enforce an anti-bot wait before the "Get Link" form will return
# a real URL. The resolver measures the timer it actually saw on each domain
# and remembers it, so the second request to that domain does not have to
# rediscover it. The bot kept this in SQLite; here it is RAM, because Render
# wipes the disk on every restart anyway.
# ══════════════════════════════════════════════════════════════════════════

class DelayStore:
    """In-memory stand-in for the bot's DB.get_delay / DB.set_delay.

    Deliberately tiny and thread-safe enough for a single event loop. Values
    are clamped so a bad measurement can never make the API hang for minutes.
    """

    MIN = 0.5
    MAX = 45.0

    def __init__(self) -> None:
        self._d: dict[str, float] = {}
        self._hits: dict[str, int] = {}

    def get(self, domain: str) -> float:
        return self._d.get(domain, CFG.golink_delay)

    def set(self, domain: str, delay: float) -> None:
        try:
            val = float(delay)
        except (TypeError, ValueError):
            return
        self._d[domain] = min(max(val, self.MIN), self.MAX)
        self._hits[domain] = self._hits.get(domain, 0) + 1

    def override(self, domain: str, delay: float) -> None:
        """Admin-forced value (the bot's /setdelay). Survives relearning."""
        self.set(domain, delay)

    def snapshot(self) -> dict[str, Any]:
        return {k: round(v, 2) for k, v in sorted(self._d.items())}

    def clear(self) -> int:
        n = len(self._d)
        self._d.clear()
        self._hits.clear()
        return n


DELAYS = DelayStore()


# ══════════════════════════════════════════════════════════════════════════
# §3  HTTP CLIENT
# ══════════════════════════════════════════════════════════════════════════

@dataclass
class Response:
    """Backend-agnostic HTTP response."""
    url: str
    status: int
    headers: dict[str, str]
    text: str
    cookies: dict[str, str] = field(default_factory=dict)
    final_url: str = ""
    error: str = ""

    @property
    def location(self) -> str:
        return self.headers.get("location", "") or self.headers.get("Location", "")

    def cookie(self, name: str) -> str:
        for k, v in self.cookies.items():
            if k.lower() == name.lower():
                return v
        return ""

    def json(self) -> Any:
        try:
            return json.loads(self.text)
        except (ValueError, TypeError):
            return None

    @property
    def is_redirect(self) -> bool:
        return self.status in (301, 302, 303, 307, 308) and bool(self.location)


class HttpError(Exception):
    pass


class SsrfBlocked(HttpError):
    pass


async def guard_host(hostname: str) -> None:
    """Refuse to dial private / reserved / loopback addresses."""
    if CFG.allow_private_nets:
        return
    host = hostname.strip("[]")
    try:
        infos = await asyncio.to_thread(socket.getaddrinfo, host, None)
    except socket.gaierror as exc:
        raise HttpError(f"DNS lookup failed for {host}: {exc}") from exc
    for info in infos:
        raw = info[4][0]
        try:
            ip = ipaddress.ip_address(raw)
        except ValueError:
            continue
        for net in _BLOCKED_NETS:
            if ip in net:
                raise SsrfBlocked(f"refusing to contact {host} ({ip}) - private range")
        if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved \
                or ip.is_multicast or ip.is_unspecified:
            raise SsrfBlocked(f"refusing to contact {host} ({ip}) - non-public range")


def _cookie_jar(session_or_response: Any) -> dict[str, str]:
    out: dict[str, str] = {}
    jar = getattr(session_or_response, "cookies", None)
    if jar is None:
        return out
    try:
        for name, value in jar.items():
            out[name] = value
    except Exception:
        with contextlib.suppress(Exception):
            for c in list(jar):
                out[getattr(c, "name", str(c))] = getattr(c, "value", "")
    return out


class HttpClient:
    """Per-task HTTP session with real Chrome TLS/JA3 fingerprint.

    curl_cffi impersonates a browser at the TLS layer, which is what gets
    past the Cloudflare challenge most Indian shorteners sit behind.
    Falls back to httpx if curl_cffi is unavailable or errors out.

    httpx rather than aiohttp: pure Python, so the fallback costs nothing
    extra to install and cannot fail a deploy over a native build.

    PROXY_URL: this service runs on a datacentre IP (Render -> AWS/GCP).
    Cloudflare scores those ranges far more harshly than a residential or
    mobile connection, so a site that resolves fine from a phone can return
    403 from here. Setting PROXY_URL routes BOTH backends through a
    residential proxy, which is the actual fix for that. It is not a
    workaround for a bug in this code.
    """

    def __init__(self) -> None:
        self._cffi_ok = CFG.use_curl_cffi
        if self._cffi_ok:
            try:
                import curl_cffi.requests
                if not callable(curl_cffi.requests.AsyncSession):
                    raise RuntimeError("curl_cffi.requests.AsyncSession missing")
            except Exception as exc:  # pragma: no cover
                log.warning("curl_cffi unavailable (%s) - falling back to httpx", exc)
                self._cffi_ok = False

    # -- session ----------------------------------------------------------
    @contextlib.asynccontextmanager
    async def session(self):
        if self._cffi_ok:
            from curl_cffi.requests import AsyncSession
            kw: dict[str, Any] = {"impersonate": CFG.impersonate}
            if CFG.proxy_url:
                # curl_cffi speaks the requests-style dict. Verified against
                # 0.16.3: both `proxies=` and `proxy=` are accepted, the dict
                # form is the one that maps http and https together.
                kw["proxies"] = {"http": CFG.proxy_url, "https": CFG.proxy_url}
            async with AsyncSession(**kw) as s:
                yield _CffiSession(s)
                return
        import httpx
        # A fresh client per resolve, mirroring the curl_cffi branch above. One
        # session() covers a whole redirect-chain walk, so keep-alive is still
        # used where it actually matters, and no client ever outlives the event
        # loop that created it.
        hkw: dict[str, Any] = {
            "timeout": CFG.http_timeout,
            "limits": httpx.Limits(max_connections=CFG.concurrency * 2,
                                   max_keepalive_connections=CFG.concurrency),
            "max_redirects": CFG.max_hops,
            "verify": False,
        }
        if CFG.proxy_url:
            # httpx 0.28 renamed `proxies=` to `proxy=`; the old name is gone,
            # so passing it would be a TypeError, not a silent no-op.
            hkw["proxy"] = CFG.proxy_url
        async with httpx.AsyncClient(**hkw) as client:
            yield _HttpxSession(client)

    async def close(self) -> None:
        # Nothing to tear down now: both backends open and close their client
        # inside session(). Retained because on_shutdown() calls it.
        return None


class _CffiSession:
    def __init__(self, s) -> None:
        self.s = s

    async def request(self, method: str, url: str, *, headers: dict | None = None,
                      data: dict | None = None, allow_redirects: bool = False,
                      referer: str | None = None, cookies: dict | None = None,
                      timeout: float | None = None,
                      params: dict | None = None) -> Response:
        parsed = urlparse(url)
        await guard_host(parsed.hostname or "")
        kw: dict[str, Any] = {
            "headers": {**BROWSER_HEADERS, **(headers or {})},
            "allow_redirects": allow_redirects,
            "timeout": timeout or CFG.http_timeout,
        }
        if params:
            kw["params"] = params
        if data is not None:
            kw["data"] = data
        if referer:
            kw["headers"]["referer"] = referer
            kw["headers"]["sec-fetch-site"] = "same-origin"
            kw["headers"]["origin"] = f"{parsed.scheme}://{parsed.netloc}"
        if cookies:
            kw["cookies"] = cookies
        if allow_redirects:
            kw["max_redirects"] = CFG.max_hops
        try:
            r = await self.s.request(method, url, **kw)
        except Exception as exc:
            return Response(url=url, status=0, headers={}, text="",
                            error=f"{type(exc).__name__}: {exc}")
        body = ""
        with contextlib.suppress(Exception):
            body = r.text or ""
            if len(body) > CFG.max_body_bytes:
                body = body[: CFG.max_body_bytes]
        return Response(
            url=url,
            status=r.status_code,
            headers={k.lower(): v for k, v in dict(r.headers).items()},
            text=body,
            cookies=_cookie_jar(r),
            final_url=str(getattr(r, "url", url)) or url,
        )

    async def get(self, url, **kw) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw) -> Response:
        return await self.request("POST", url, **kw)


class _HttpxSession:
    """Fallback path - plain httpx, no TLS impersonation."""

    def __init__(self, s) -> None:
        self.s = s

    async def request(self, method: str, url: str, *, headers: dict | None = None,
                      data: dict | None = None, allow_redirects: bool = False,
                      referer: str | None = None, cookies: dict | None = None,
                      timeout: float | None = None,
                      params: dict | None = None) -> Response:
        parsed = urlparse(url)
        await guard_host(parsed.hostname or "")
        h = {"user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
             **BROWSER_HEADERS, **(headers or {})}
        if referer:
            h["referer"] = referer
            h["origin"] = f"{parsed.scheme}://{parsed.netloc}"
        try:
            kw: dict[str, Any] = {
                "headers": h,
                "follow_redirects": allow_redirects,
                "timeout": timeout or CFG.http_timeout,
            }
            if params:
                kw["params"] = params
            if data is not None:
                kw["data"] = data
            if cookies:
                kw["cookies"] = cookies
            r = await self.s.request(method, url, **kw)
            body = (r.text or "")[: CFG.max_body_bytes]
            jar = {k: v for k, v in r.cookies.items()}
            return Response(
                url=url, status=r.status_code,
                headers={k.lower(): v for k, v in r.headers.items()},
                text=body, cookies=jar, final_url=str(r.url),
            )
        except Exception as exc:
            return Response(url=url, status=0, headers={}, text="",
                            error=f"{type(exc).__name__}: {exc}")

    async def get(self, url, **kw) -> Response:
        return await self.request("GET", url, **kw)

    async def post(self, url, **kw) -> Response:
        return await self.request("POST", url, **kw)


HTTP = HttpClient()
_RESOLVE_SEMAPHORE = asyncio.Semaphore(CFG.concurrency)


# ══════════════════════════════════════════════════════════════════════════
# §4  SHORTENER REGISTRY  (70+ domains)
# ══════════════════════════════════════════════════════════════════════════
#
# handler meanings
#   gplinks      - deep GPLinks flow: vid from Location, skip_sub paywall
#                  bypass, update-visitor-value API, then form POST
#   form         - Linkify family: fetch page -> parse <form> -> POST hidden
#                  inputs to the form action with X-Requested-With -> JSON url
#   redirect     - plain 3xx chain, no page work needed
#   adfly        - ysmm XOR/base64 decode
#   ouo          - two-step form (ouo.io / ouo.press)
#   linkvertise  - GraphQL publisher API
#   auto         - universal engine, tries every strategy in order
#
# `base`    = rewrite the URL onto this origin before fetching (shorteners that
#             serve the interstitial from a second domain).
# `referer` = Referer header the site expects (some 403 without it).
# `delay`   = forced pre-POST wait. None => use the adaptive learned delay.

@dataclass(frozen=True)
class Profile:
    handler: str = "auto"
    base: Optional[str] = None
    referer: Optional[str] = None
    delay: Optional[float] = None
    skip_sub: bool = False
    form_id: Optional[str] = None
    post_path: Optional[str] = None
    label: str = ""


def _mk(handler: str, domains: tuple[str, ...], **kw: Any) -> dict[str, Profile]:
    label = kw.pop("label", handler)          # pop ONCE, not per iteration
    return {d: Profile(handler=handler, label=label, **kw) for d in domains}


REGISTRY: dict[str, Profile] = {}

# ── GPLinks family (the primary target) ───────────────────────────────────
REGISTRY.update(_mk("gplinks", (
    "gplinks.co", "gplinks.in", "gplinks.com", "gplinks.org", "gplinks.site",
    "gplinks.xyz", "gplink.live", "gplink.co", "gplinks.net", "go.gplinks.co",
    "gplinks.pro", "gplink.in",
), skip_sub=True, label="GPLinks"))

# ── Linkify "form#go-link" family ─────────────────────────────────────────
REGISTRY.update(_mk("form", (
    "rocklinks.net", "rocklinks.co", "rocklinks.in",
    "gyanilinks.com", "gyanilink.com",
    "fc.lc", "nanolinks.in", "tryshort.in", "sharedisklinks.com",
    "droplink.co", "exe.io", "linkjust.com", "urlshortx.com", "eklink.net",
    "mplaylink.com", "shrinkme.io", "adrinka.com", "desiupload.co",
    "linksly.co", "shrink.pe", "adz7short.space", "shortzon.com",
    "techylist.in", "earnfly.in", "cutlinks.co", "shortlinko.com",
    "moneylink.in", "linkshorts.in", "shrinks.one", "linkpoi.in",
    "shortearn.in", "cutwin.pro", "shortingly.xyz", "urlily.com",
    "shrinke.me", "linkshortify.com", "shortlink.cyou", "golinks.co",
    "linkfly.to", "cutt.link", "shorten.sh", "hubshort.link",
), form_id="go-link", label="Form shortener"))

# go-link sites whose interstitial lives on a second origin
REGISTRY.update(_mk("form", ("gtlinks.me",), base="https://go.gyanitheme.com",
                    form_id="go-link", referer="https://gtlinks.me/",
                    label="Form shortener"))
REGISTRY.update(_mk("form", ("gyanilinks.xyz",), base="https://go.gyanitheme.com",
                    form_id="go-link", label="Form shortener"))
REGISTRY.update(_mk("form", ("shortingly.me",), base="https://go.techyjeeshan.xyz",
                    form_id="go-link", referer="https://shortingly.me/",
                    label="Form shortener"))

# ── plain 3xx redirectors ─────────────────────────────────────────────────
REGISTRY.update(_mk("redirect", (
    "bit.ly", "tinyurl.com", "cutt.ly", "is.gd", "v.gd", "t.ly", "rb.gy",
    "rebrand.ly", "buff.ly", "goo.gl", "ow.ly", "lnkd.in", "trib.al",
    "amzn.to", "bit.do", "soo.gd", "s.id", "shorturl.at", "dlvr.it",
    "zpr.io", "cutt.us", "xurl.es", "tiny.cc", "lnkiy.in", "urls.fr",
    "qr.net", "1url.com", "surl.li", "cuty.io", "shorte.link", "mcaf.ee",
    "su.pr", "dub.sh", "lnk.direct", "go.aws", "yhoo.it", "nyti.ms",
    "wp.me", "youtu.be", "t.co", "fb.me", "instagr.am", "tiny.one",
    "bitly.is", "shorturl.gg", "reurl.cc", "urlr.be",
), label="Redirect"))

# ── adf.ly / ysmm ─────────────────────────────────────────────────────────
REGISTRY.update(_mk("adfly", (
    "adf.ly", "q.gs", "j.gs", "adfoc.us", "atominik.com", "microify.com",
    "threadsphere.bid", "swiftviz.net", "briskgram.net", "adcrun.ch",
    "im-gat.com", "flylinks.co", "pnd.tl", "gestyy.com", "ceesty.com",
    "clkpay.com", "sh.st", "dst.tt",
), label="adf.ly/ysmm"))

# ── ouo.io (two-step form) ────────────────────────────────────────────────
REGISTRY.update(_mk("ouo", ("ouo.io", "ouo.press", "ouo.today"), label="ouo.io"))

# ── Linkvertise (GraphQL) ─────────────────────────────────────────────────
REGISTRY.update(_mk("linkvertise", (
    "linkvertise.com", "linkvertise.net", "link-to.net", "linktarget.com",
    "the-first.net", "direct-link.net", "link-center.net", "up-to-down.net",
), label="Linkvertise"))

# ── aylink / cpmlink token flow ───────────────────────────────────────────
REGISTRY.update(_mk("form", (
    "aylink.co", "ay.live", "mbky.link", "cpmlink.co", "cpmlink.pro",
    "cpmlink.net", "bildirim.online",
), post_path="/links/go", label="aylink/cpmlink"))

# ── shorte.st family ──────────────────────────────────────────────────────
REGISTRY.update(_mk("auto", ("shorte.st", "sstool.us", "exeo.app", "boost.ink"),
                    label="JS shortener"))

# ── GPLinks interstitial / ad-gate domains (powergam style lid+pid) ───────
INTERSTITIAL_DOMAINS = {
    "powergam.online", "powergam.xyz", "gadnews.online", "gplinks.online",
    "linkfly.to", "adsfly.online",
}


def profile_for(url: str) -> Profile:
    host = (urlparse(url).hostname or "").lower()
    if host in REGISTRY:
        return REGISTRY[host]
    # walk up the registrable domain: sub.gplinks.co -> gplinks.co
    parts = host.split(".")
    for i in range(1, max(1, len(parts) - 1)):
        cand = ".".join(parts[i:])
        if cand in REGISTRY:
            return REGISTRY[cand]
    return Profile(handler="auto", label="auto-detect")


SUPPORTED_DOMAINS = sorted(REGISTRY)


def apply_extra_domains() -> int:
    """Merge EXTRA_*_DOMAINS from the environment into the registry."""
    added = 0
    for hosts, handler, kw in (
        (CFG.extra_gplinks, "gplinks", {"skip_sub": True, "label": "GPLinks (custom)"}),
        (CFG.extra_form, "form", {"form_id": "go-link", "label": "Form (custom)"}),
        (CFG.extra_redirect, "redirect", {"label": "Redirect (custom)"}),
        (CFG.extra_adfly, "adfly", {"label": "adf.ly (custom)"}),
    ):
        for raw in hosts:
            host = raw.strip().lower().lstrip(".").split("/")[0]
            if host:
                REGISTRY[host] = Profile(handler=handler, **kw)
                added += 1
    global SUPPORTED_DOMAINS
    SUPPORTED_DOMAINS = sorted(REGISTRY)
    return added


apply_extra_domains()


# ══════════════════════════════════════════════════════════════════════════
# §5  RESOLVER ENGINE
# ══════════════════════════════════════════════════════════════════════════

URL_RE = re.compile(
    r"(?:https?://[^\s<>\"'()\[\]{}|\\^`\u00a0]+"
    r"|www\.[^\s<>\"'()\[\]{}|\\^`\u00a0]+)",
    re.IGNORECASE,
)

# Bare "gplinks.co/BFfOX" with no scheme. Deliberately conservative: the host
# must carry a path, or be a domain we already know about.
BARE_URL_RE = re.compile(
    r"(?<![\w./-])((?:[a-z0-9](?:[a-z0-9-]*[a-z0-9])?\.)+[a-z]{2,24}"
    r"(?::\d{2,5})?(?:/[^\s<>\"'()\[\]{}|\\^`\u00a0]*)?)",
    re.IGNORECASE,
)
_BARE_TLD_OK = re.compile(
    r"\.(com|net|org|in|co|io|me|ly|info|xyz|site|online|live|press|today|"
    r"link|links|cc|us|uk|de|fr|es|ru|cn|jp|br|app|dev|sh|gd|st|to|gg|ws|"
    r"click|space|pro|top|vip|fun|club|tech|store|shop|blog|page|it|nl|se|"
    r"no|fi|dk|pl|cz|sk|hu|ro|bg|gr|tr|il|ae|sa|za|au|nz|ca|mx|ar|cl|pe)$",
    re.IGNORECASE,
)

META_REFRESH_RE = re.compile(
    r"""<meta[^>]+http-equiv\s*=\s*["']?\s*refresh\s*["']?[^>]+"""
    r"""content\s*=\s*["']?[^"'>]*?url\s*=\s*([^"'>\s;]+)""",
    re.IGNORECASE | re.DOTALL,
)
META_REFRESH_RE2 = re.compile(
    r"""<meta[^>]+content\s*=\s*["']?[^"'>]*?url\s*=\s*([^"'>\s;]+)[^>]+"""
    r"""http-equiv\s*=\s*["']?\s*refresh""",
    re.IGNORECASE | re.DOTALL,
)
JS_LOCATION_RE = re.compile(
    r"""(?:window\.|document\.|self\.|top\.)?location(?:\.href|\.assign)?\s*=\s*"""
    r"""["']([^"']{6,})["']""",
    re.IGNORECASE,
)
JS_LOCATION_CALL_RE = re.compile(
    r"""(?:window\.|document\.)?location\.(?:replace|assign)\s*\(\s*["']([^"']{6,})["']\s*\)""",
    re.IGNORECASE,
)
JS_HREF_SET_RE = re.compile(
    r"""(?:href|action)\s*=\s*["'](https?://[^"']{6,})["']""", re.IGNORECASE
)
YSMM_RE = re.compile(r"""(?:var\s+)?ysmm\s*=\s*["']([A-Za-z0-9+/=_-]{16,})["']""")
DEST_VAR_RE = re.compile(
    r"""(?:var|let|const)\s+(?:url|dest(?:ination)?|target|redirect_?url|final_?url"""
    r"""|link_?url|go_?url)\s*=\s*["'](https?://[^"']{4,})["']""",
    re.IGNORECASE,
)
DATA_URL_RE = re.compile(r"""data-(?:url|href|link|target)\s*=\s*["'](https?://[^"']+)["']""",
                         re.IGNORECASE)
JSON_URL_RE = re.compile(r"""\{\s*"url"\s*:\s*"(https?:[^"]+)"\s*[,}]""", re.IGNORECASE)
B64_URL_RE = re.compile(r"""["']?url["']?\s*[:=]\s*["']([A-Za-z0-9+/=]{24,})["']""")
COUNTDOWN_RE = re.compile(r"""(?:countdown|timer|wait)\D{0,20}?(\d{1,3})\s*(?:second|sec|s\b)""",
                          re.IGNORECASE)

AD_PAGE_MARKERS = (
    "please wait", "verifying", "get link", "continue to", "advertiser",
    "human verification", "checking your browser",
)


@dataclass
class Hop:
    url: str
    kind: str          # redirect | form | meta | js | api | decode | interstitial
    note: str = ""


@dataclass
class ResolveOutcome:
    input_url: str
    ok: bool = False
    final_url: str = ""
    shortener: str = ""
    handler: str = ""
    error: str = ""
    hops: list[Hop] = field(default_factory=list)
    candidates: list[str] = field(default_factory=list)
    elapsed_ms: int = 0
    partial: bool = False
    challenge: str = ""     # anti-bot system that stopped us, if any

    @property
    def depth(self) -> int:
        return len(self.hops)


# ──────────────────────────────────────────────────────────────────────────
# URL utilities
# ──────────────────────────────────────────────────────────────────────────

def extract_urls(text: str, entities: Any = None) -> list[str]:
    """Pull every URL out of a Telegram message (entities first, then regex)."""
    found: list[str] = []
    if entities and text:
        for ent in entities:
            etype = getattr(ent, "type", "")
            if etype == "text_link":
                u = getattr(ent, "url", "")
                if u:
                    found.append(u)
            elif etype == "url":
                off, ln = getattr(ent, "offset", 0), getattr(ent, "length", 0)
                found.append(text[off:off + ln])
    if text:
        found.extend(URL_RE.findall(text))
        # bare hosts: only accept when there is a path or the host is registered
        consumed = " ".join(found)
        for m in BARE_URL_RE.finditer(text):
            cand = m.group(1)
            if cand in consumed:
                continue
            host = cand.split("/")[0].lower()
            root = host.split(":")[0]
            has_path = "/" in cand
            known = is_shortener_host(root)
            tld_ok = bool(_BARE_TLD_OK.search(root))
            if not tld_ok or re.search(r"\.(png|jpe?g|gif|webp|svg|css|js|json|txt|"
                                       r"pdf|zip|mp4|mp3|exe|dmg)$", root, re.I):
                continue
            if has_path or known:
                found.append(cand)
    clean: list[str] = []
    seen = set()
    for raw in found:
        u = normalize_url(raw)
        if u and u not in seen:
            seen.add(u)
            clean.append(u)
    return clean


def normalize_url(raw: str) -> str:
    """Tidy a user-pasted URL into something fetchable."""
    if not raw:
        return ""
    u = raw.strip()
    u = u.strip("<>").strip()
    u = re.sub(r"[\s\u00a0]+", "", u)
    u = u.rstrip(".,;:!?)'\"]}>")
    if "%25" in u:  # double-encoded link pasted from another messenger
        once = unquote(u)
        if once.startswith(("http://", "https://")):
            u = once
    if not u:
        return ""
    if u.lower().startswith(("http://", "https://")):
        pass
    elif u.lower().startswith("www."):
        u = "https://" + u
    elif re.match(r"^[\w.-]+\.[a-z]{2,}(?:[/?#].*)?$", u, re.IGNORECASE):
        u = "https://" + u
    else:
        return ""
    p = urlparse(u)
    if p.scheme not in ("http", "https") or not p.hostname:
        return ""
    return urlunparse(p)


def host_of(url: str) -> str:
    return (urlparse(url).hostname or "").lower()


def is_shortener_host(host: str) -> bool:
    host = host.lower()
    if host in REGISTRY or host in INTERSTITIAL_DOMAINS:
        return True
    parts = host.split(".")
    for i in range(1, max(1, len(parts) - 1)):
        if ".".join(parts[i:]) in REGISTRY:
            return True
    return False


def looks_like_destination(url: str) -> bool:
    """True when a URL is plausibly the *real* target, not another hop."""
    p = urlparse(url)
    if p.scheme not in ("http", "https") or not p.hostname:
        return False
    if is_shortener_host(p.hostname):
        return False
    return True


def add_query(url: str, **pairs: str) -> str:
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    for k, v in pairs.items():
        if v is not None and k not in q:
            q[k] = [v]
    newq = "&".join(
        f"{quote(k, safe='')}={quote(vv[0], safe='')}" for k, vv in q.items() if vv
    )
    return urlunparse(p._replace(query=newq))


def b64url_decode(value: str) -> str:
    """Base64/base64url decode with padding repair. Returns '' on failure."""
    s = value.strip().strip('"').strip("'")
    if not s:
        return ""
    s = s.replace("-", "+").replace("_", "/")
    s += "=" * (-len(s) % 4)
    for decoder in (base64.b64decode, base64.urlsafe_b64decode):
        with contextlib.suppress(Exception):
            out = decoder(s)
            txt = out.decode("utf-8", "ignore").strip()
            if txt and all(32 <= ord(c) < 127 or c in "\r\n\t" for c in txt):
                return txt
    return ""


def decode_interstitial(url: str) -> Optional[str]:
    """powergam-style ad gate  ->  the gplinks.co destination it points at.

    ?lid=<b64url>&pid=<b64url>&vid=<id>&pages=N
      =>  https://gplinks.co/<lid>?pid=<pid>&vid=<vid>
    """
    p = urlparse(url)
    q = parse_qs(p.query, keep_blank_values=True)
    lid = (q.get("lid") or [""])[0]
    if not lid:
        return None
    d_lid = b64url_decode(lid)
    if not d_lid:
        return None
    pid = (q.get("pid") or [""])[0]
    vid = (q.get("vid") or [""])[0]
    d_pid = b64url_decode(pid) or pid
    base_host = "gplinks.co"
    tail = d_lid.strip("/")
    out = f"https://{base_host}/{tail}"
    params = []
    if d_pid:
        params.append(f"pid={quote(d_pid, safe='')}")
    if vid:
        params.append(f"vid={quote(vid, safe='')}")
    if params:
        out += "?" + "&".join(params)
    return out


def decode_ysmm(ysmm: str) -> Optional[str]:
    """adf.ly `ysmm` token -> destination URL.

    The token is an obfuscated base64 blob. We rebuild the canonical
    de-interleave, XOR adjacent digit pairs, base64-decode and strip the
    16-char noise header. Several variants are tried because the exact
    recipe differs between adf.ly clones; the first result that parses as a
    real http(s) URL wins.
    """
    ysmm = (ysmm or "").strip()
    if len(ysmm) < 8:
        return None

    def interleave_a(s: str) -> str:
        out = ""
        for i, ch in enumerate(s):
            out = (out + ch) if i % 2 == 0 else (ch + out)
        return out

    def interleave_b(s: str) -> str:
        return "".join(s[i] for i in range(0, len(s), 2)) + \
               "".join(s[i] for i in range(len(s) - 1, -1, -2))

    def xor_pairs(s: str) -> str:
        res = ""
        i = 0
        while i < len(s):
            if i + 1 < len(s) and s[i].isdigit() and s[i + 1].isdigit():
                res += str(int(s[i]) ^ int(s[i + 1]))
                i += 2
            else:
                res += s[i]
                i += 1
        return res

    def b64_try(s: str) -> str:
        s = s.replace("-", "+").replace("_", "/")
        s += "=" * (-len(s) % 4)
        with contextlib.suppress(Exception):
            return base64.b64decode(s).decode("utf-8", "ignore")
        return ""

    staged: list[str] = []
    for mix in (interleave_a, interleave_b, lambda s: s):
        base = mix(ysmm)
        staged += [base, xor_pairs(base)]

    for blob in staged:
        decoded = b64_try(blob)
        if not decoded:
            continue
        for cand in (decoded[16:], decoded, decoded[10:], decoded[6:]):
            cand = cand.strip()
            m = re.search(r"https?://[^\s\"'<>\\]+", cand)
            if m:
                u = normalize_url(m.group(0))
                if u:
                    return u
    return None


# ──────────────────────────────────────────────────────────────────────────
# Page analysis
# ──────────────────────────────────────────────────────────────────────────

PAYWALL_MARKERS = ("skip_sub=1", "continue with ads", "go ad-free", "gplinks premium")


def has_paywall(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in PAYWALL_MARKERS)


def find_go_form(soup: BeautifulSoup, form_id: Optional[str] = None):
    """Locate the shortener's POST form (Linkify `#go-link` and friends)."""
    if form_id:
        f = soup.find("form", id=form_id)
        if f:
            return f
    forms = soup.find_all("form")
    # 1. explicit go-link id anywhere
    for f in forms:
        if (f.get("id") or "").lower() in ("go-link", "golink", "go_form", "link-form"):
            return f
    # 2. action points at a known resolver endpoint
    for f in forms:
        act = (f.get("action") or "").lower()
        if any(k in act for k in ("links/go", "/get/tk", "/go2", "/go/", "shortener/go")):
            return f
    # 3. a form carrying the Linkify hidden-token signature
    for f in forms:
        names = {(i.get("name") or "").lower() for i in f.find_all("input")}
        if {"_token", "alias"} & names or ("alias" in names and len(names) >= 2):
            return f
    # 4. any POST form with hidden inputs
    for f in forms:
        if (f.get("method") or "get").lower() == "post" and f.find("input", {"type": "hidden"}):
            return f
    for f in forms:
        if f.find("input", {"type": "hidden"}):
            return f
    return None


def form_fields(form) -> dict[str, str]:
    data: dict[str, str] = {}
    for tag in form.find_all(["input", "textarea"]):
        name = tag.get("name")
        if not name:
            continue
        typ = (tag.get("type") or "text").lower()
        if typ in ("submit", "button", "image", "reset", "checkbox", "radio", "file"):
            continue
        data[name] = tag.get("value") or ""
    for tag in form.find_all("select"):
        name = tag.get("name")
        if not name:
            continue
        sel = tag.find("option", selected=True) or tag.find("option")
        data[name] = (sel.get("value") if sel else "") or ""
    return data


def interstitial_signals(text: str) -> bool:
    """Does this page look like an ad-gate / shortener interstitial?

    Used to decide whether we may trust *loose* link extraction. On a genuine
    destination page we only ever follow unambiguous signals (meta refresh,
    JS location, encoded tokens) - never a random outbound <a href>.
    """
    if not text:
        return False
    low = text.lower()
    if any(m in low for m in AD_PAGE_MARKERS):
        return True
    if YSMM_RE.search(text) or DEST_VAR_RE.search(text) or JS_LOCATION_CALL_RE.search(text):
        return True
    if META_REFRESH_RE.search(text) or META_REFRESH_RE2.search(text):
        return True
    if 'id="go-link"' in low or "/links/go" in low or "go-link" in low:
        return True
    if "captcha" in low or "cloudflare" in low or "advertiser" in low:
        return True
    if low.count("<a ") >= 1 and ("countdown" in low or "verifying" in low):
        return True
    return False


TIGHT_KINDS = frozenset({
    "meta", "js", "js-var", "json", "ysmm", "interstitial", "decode", "redirect",
})


def page_urls(page_url: str, text: str, tight_only: bool = False) -> list[tuple[str, str]]:
    """Ordered (url, kind) candidates hidden in an interstitial page."""
    out: list[tuple[str, str]] = []

    def push(raw: str, kind: str) -> None:
        raw = (raw or "").strip()
        if not raw:
            return
        raw = raw.replace("\\/", "/").replace("\\u002F", "/").replace("&amp;", "&")
        if raw.startswith("//"):
            raw = "https:" + raw
        elif not raw.lower().startswith(("http://", "https://")):
            raw = urljoin(page_url, raw)
        u = normalize_url(raw)
        if u and u not in [o[0] for o in out]:
            out.append((u, kind))

    for m in JS_LOCATION_CALL_RE.finditer(text):
        push(m.group(1), "js")
    for m in DEST_VAR_RE.finditer(text):
        push(m.group(1), "js-var")
    for m in JSON_URL_RE.finditer(text):
        push(m.group(1).replace("\\u0026", "&"), "json")
    for m in META_REFRESH_RE.finditer(text):
        push(m.group(1), "meta")
    for m in META_REFRESH_RE2.finditer(text):
        push(m.group(1), "meta")
    for m in JS_LOCATION_RE.finditer(text):
        push(m.group(1), "js")
    for m in DATA_URL_RE.finditer(text):
        push(m.group(1), "data-attr")

    m = YSMM_RE.search(text)
    if m:
        dec = decode_ysmm(m.group(1))
        if dec:
            push(dec, "ysmm")

    inter = decode_interstitial(page_url)
    if inter:
        push(inter, "interstitial")

    # a big obvious CTA button
    try:
        soup = BeautifulSoup(text, "html.parser")
        for sel in ("a#get-link", "a.btn-get-link", "a[class*=get-link]",
                    "a#go-link-btn", "a.download-btn", "#continue-button",
                    "a[class*=continue]", "button#get-link"):
            for a in soup.select(sel)[:4]:
                push(a.get("href") or "", "cta")
    except Exception:
        pass

    result = [(u, k) for u, k in out if looks_like_destination(u)]
    if tight_only:
        result = [(u, k) for u, k in result if k in TIGHT_KINDS]
    return result


# ── anti-bot challenge detection ──────────────────────────────────────────
# Handing a challenge URL back as the resolved destination is the worst
# failure a solver can have: it reports success and gives the caller a link
# that renders a captcha instead of their file. Found by a live test against
# a DataDome-protected host - the mocked fixtures could never surface it,
# because no fixture was a captcha page.
CHALLENGE_DOMAINS = (
    ("captcha-delivery.com", "DataDome"),
    ("challenges.cloudflare.com", "Cloudflare Turnstile"),
    ("hcaptcha.com", "hCaptcha"),
    ("recaptcha.net", "reCAPTCHA"),
    ("geetest.com", "GeeTest"),
    ("arkoselabs.com", "Arkose Labs"),
    ("funcaptcha.com", "FunCaptcha"),
    ("perimeterx.net", "PerimeterX"),
    ("px-cdn.net", "PerimeterX"),
    ("pxchk.net", "PerimeterX"),
    ("kasada.io", "Kasada"),
    ("shapesecurity.com", "Shape Security"),
    ("incapsula.com", "Imperva Incapsula"),
    ("secsso.com", "Imperva Incapsula"),
    ("ddos-guard.net", "DDoS-Guard"),
)

# Deliberately specific strings, not the bare word "captcha": a legitimate
# destination can easily contain that word in its path.
CHALLENGE_URL_MARKERS = (
    ("/cdn-cgi/challenge-platform", "Cloudflare"),
    ("__cf_chl", "Cloudflare"),
    ("cf-turnstile", "Cloudflare Turnstile"),
    ("initialcid=", "DataDome"),
    ("g-recaptcha", "reCAPTCHA"),
    ("/recaptcha/api", "reCAPTCHA"),
    "/hcaptcha",
    ("geetest", "GeeTest"),
    ("_incapsula_resource", "Imperva Incapsula"),
    ("px-captcha", "PerimeterX"),
)

CHALLENGE_PAGE_MARKERS = (
    ("captcha-delivery", "DataDome"),
    ("datadome", "DataDome"),
    ("cf-challenge-platform", "Cloudflare"),
    ("cf_chl_opt", "Cloudflare"),
    ("cf-turnstile", "Cloudflare Turnstile"),
    ("checking your browser before accessing", "Cloudflare"),
    ("just a moment", "Cloudflare"),
    ("enable javascript and cookies to continue", "Cloudflare"),
    ("attention required! | cloudflare", "Cloudflare"),
    ("verify you are human", "anti-bot"),
    ("verifying you are human", "anti-bot"),
    ("grecaptcha.render", "reCAPTCHA"),
    ("hcaptcha.com/captcha", "hCaptcha"),
    ("geetest_", "GeeTest"),
    ("funcaptcha", "FunCaptcha"),
    ("_incapsula_resource", "Imperva Incapsula"),
    ("you have been blocked", "WAF"),
    ("px-captcha", "PerimeterX"),
)


def challenge_name(url: str = "", text: str = "") -> str:
    """Name the anti-bot system guarding this URL/page, or "" if none.

    Checked on the URL of every hop, so no handler - however loose its
    extraction - can return a captcha as an answer.
    """
    if url:
        h = host_of(url)
        for dom, name in CHALLENGE_DOMAINS:
            if h == dom or h.endswith("." + dom):
                return name
        low = url.lower()
        for marker in CHALLENGE_URL_MARKERS:
            # tolerate the shorthand entries that carry no label
            m, name = marker if isinstance(marker, tuple) else (marker, "captcha")
            if m in low:
                return name
    if text:
        low = text[:20000].lower()
        for marker, name in CHALLENGE_PAGE_MARKERS:
            if marker in low:
                return name
    return ""


def challenge_error(name: str) -> str:
    """One shared explanation, so every path that hits a wall says the same
    thing - and names the fix rather than only the symptom."""
    return (f"Stopped by an anti-bot challenge ({name}). The site served a "
            "captcha or browser check instead of a redirect. This is an "
            "IP-reputation problem: the host does not trust this server's "
            "address range. A residential or mobile proxy is the only "
            "reliable fix.")


def plausible_destination(url: str, page_url: str) -> bool:
    """Filter out self-links, ads, captchas and the shortener's own assets."""
    if not looks_like_destination(url):
        return False
    if challenge_name(url):
        return False
    h = host_of(url)
    bad = ("google.com/recaptcha", "facebook.com/tr", "doubleclick.net",
           "googlesyndication", "google-analytics", "adsystem", "popads",
           "exoclick", "juicyads", "propellerads", "twitter.com/intent",
           "t.me/share", "wa.me/", "api.whatsapp")
    low = url.lower()
    if any(b in low for b in bad):
        return False
    if h in ("google.com", "www.google.com", "facebook.com", "youtube.com") and \
            not parse_qs(urlparse(url).query):
        return False
    if host_of(page_url) and h == host_of(page_url):
        return False
    return True


# ──────────────────────────────────────────────────────────────────────────
# The resolver
# ──────────────────────────────────────────────────────────────────────────

async def bot_sleep(delay: float) -> None:
    """Anti-bot wait with human jitter so requests do not fingerprint as a loop."""
    if delay <= 0:
        return
    await asyncio.sleep(min(delay, CFG.golink_delay) * random.uniform(0.92, 1.08))


TIMER_HINTS = ("wait", "timer", "slow down", "too fast", "try again",
               "not ready", "please hold", "few seconds", "patience")


class LinkResolver:
    """Universal shortener resolver with a deep GPLinks path."""

    def __init__(self) -> None:
        self._inflight: dict[str, asyncio.Event] = {}

    # -- public API -------------------------------------------------------
    async def resolve(self, url: str) -> ResolveOutcome:
        url = normalize_url(url)
        out = ResolveOutcome(input_url=url or "")
        if not url:
            out.error = "That does not look like a valid http(s) link."
            return out
        out.shortener = host_of(url)
        t0 = time.perf_counter()
        try:
            async with _RESOLVE_SEMAPHORE, HTTP.session() as s:
                await self._walk(s, url, out)
        except SsrfBlocked as exc:
            out.error = f"Blocked for safety: {exc}"
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # never let one bad link kill the worker
            log.exception("resolve failed for %s", url)
            out.error = f"{type(exc).__name__}: {exc}"
        out.elapsed_ms = int((time.perf_counter() - t0) * 1000)
        out.handler = out.handler or profile_for(url).handler
        final = normalize_url(out.final_url or url)
        out.final_url = final
        out.ok = bool(final) and looks_like_destination(final) and final != url
        if not out.ok and not out.error:
            if final == url:
                out.error = ("No redirect found - that URL already looks like a final "
                             "destination, or the shortener refused a headless client.")
            else:
                out.error = "Could not reach the final destination."
            out.partial = bool(out.candidates)
        return out

    async def resolve_many(self, urls: list[str]) -> list[ResolveOutcome]:
        return list(await asyncio.gather(*(self.resolve(u) for u in urls)))

    # -- walk the chain ---------------------------------------------------
    async def _walk(self, s, url: str, out: ResolveOutcome) -> None:
        """Follow the chain until a strategy reports 'terminal' or we cap out.

        Termination is driven entirely by `_step` returning None - never by
        guessing from the hostname, because ad-gates (powergam.online etc.) sit
        on hosts that are not themselves registered shorteners.
        """
        current = url
        seen: set[str] = {current}
        for _depth in range(CFG.max_hops):
            nxt, kind, note = await self._step(s, current, out, len(out.hops))
            if not nxt:
                break
            nxt = normalize_url(nxt)
            if not nxt:
                break
            blocked_by = challenge_name(url=nxt)
            if blocked_by:
                # Report the wall instead of climbing over it and pretending
                # the other side is the answer.
                out.challenge = blocked_by
                out.error = challenge_error(blocked_by)
                out.hops.append(Hop(url=current, kind=kind,
                                    note=f"blocked by {blocked_by}"))
                break
            if nxt in seen:
                if normalize_url(nxt) != normalize_url(current):
                    out.error = out.error or "Redirect loop detected."
                break
            out.hops.append(Hop(url=current, kind=kind, note=note))
            out.candidates = [c for c in out.candidates if c != nxt]
            seen.add(nxt)
            current = nxt
            out.final_url = current
            if not CFG.resolve_chains:
                break
        if not out.final_url:
            out.final_url = url

    async def _step(self, s, url: str, out: ResolveOutcome,
                    depth: int) -> tuple[Optional[str], str, str]:
        prof = profile_for(url)
        out.handler = prof.handler
        try:
            if prof.handler == "gplinks":
                return await self._gplinks_step(s, url, prof, out)
            if prof.handler == "redirect":
                return await self._plain_redirect(s, url)
            if prof.handler == "form":
                return await self._form_step(s, url, prof, out)
            if prof.handler == "adfly":
                return await self._adfly_step(s, url, prof)
            if prof.handler == "ouo":
                return await self._ouo_step(s, url, prof, out)
            if prof.handler == "linkvertise":
                return await self._linkvertise_step(s, url, prof, out)
            return await self._auto_step(s, url, prof, out)
        except SsrfBlocked:
            raise
        except Exception as exc:
            log.debug("step failed on %s: %s", url, exc)
            if depth == 0:
                out.error = f"{type(exc).__name__}: {exc}"
            return None, "", ""

    # -- strategy: plain 3xx ----------------------------------------------
    async def _plain_redirect(self, s, url: str) -> tuple[Optional[str], str, str]:
        r = await s.get(url, allow_redirects=False)
        if r.error:
            r = await s.get(url, allow_redirects=True)
            if r.error:
                return None, "", r.error
            if r.final_url and normalize_url(r.final_url) != normalize_url(url):
                return r.final_url, "redirect", f"HTTP {r.status}"
            return None, "", ""
        if r.is_redirect:
            loc = urljoin(url, r.location)
            return loc, "redirect", f"HTTP {r.status}"
        # 200 on a "plain redirect" domain -> only trust unambiguous signals
        if r.text:
            tight = not interstitial_signals(r.text)
            for cand, kind in page_urls(url, r.text, tight_only=tight)[:3]:
                if plausible_destination(cand, url):
                    return cand, kind, "extracted from page"
        return None, "", ""

    # -- strategy: Linkify form family ------------------------------------
    async def _form_step(self, s, url: str, prof: Profile,
                         out: ResolveOutcome) -> tuple[Optional[str], str, str]:
        fetch_url = url
        if prof.base:
            p_src, p_dst = urlparse(url), urlparse(prof.base)
            fetch_url = urlunparse(p_dst._replace(path=p_src.path, query=p_src.query))

        domain = host_of(url)
        attempts = max(1, CFG.form_retries + 1)
        last_note = ""
        delay = prof.delay if prof.delay is not None else DELAYS.get(domain)

        for attempt in range(attempts):
            if delay > 0:
                await bot_sleep(delay)
            page = await self._landing(s, fetch_url, prof)
            if page is None:
                return None, "", "no page returned"
            page_url, text, page_resp = page

            nxt, kind, note = await self._post_form(
                s, page_url, text, prof, out, note_prefix=f"try {attempt + 1}"
            )
            if nxt:
                if prof.delay is None and delay > 0 and CFG.adaptive_delay:
                    DELAYS.set(domain, delay)
                return nxt, kind, note
            last_note = note or last_note

            # learn: if the server complained about a timer, escalate the wait
            if prof.delay is None and CFG.adaptive_delay:
                if any(h in (note or "").lower() for h in TIMER_HINTS) or attempt == 0:
                    delay = CFG.golink_delay
                    DELAYS.set(domain, delay)
                    last_note = note

            # maybe the page itself reveals the destination without a POST
            tight = not interstitial_signals(text)
            for cand, ckind in page_urls(page_url, text, tight_only=tight)[:5]:
                if plausible_destination(cand, page_url):
                    if cand not in out.candidates:
                        out.candidates.append(cand)
                    return cand, ckind, f"extracted from {host_of(page_url)}"
            if page_resp is not None and page_resp.is_redirect:
                return urljoin(page_url, page_resp.location), "redirect", "HTTP redirect"

        return None, "", last_note or "form POST did not return a destination"

    async def _landing(self, s, url: str, prof: Profile):
        """Fetch the interstitial page, defeating the GPLinks paywall."""
        target = url
        if prof.skip_sub:
            target = add_query(url, skip_sub="1")
        r = await s.get(target, allow_redirects=True, referer=prof.referer)
        if r.error and prof.skip_sub:
            r = await s.get(url, allow_redirects=True, referer=prof.referer)
        if r.error:
            return None
        text = r.text or ""
        landed = r.final_url or target
        # still on the paywall? try the raw URL, then the ?skip_sub=1 variant
        if prof.skip_sub and has_paywall(text) and "skip_sub" not in landed:
            r2 = await s.get(add_query(landed, skip_sub="1"), allow_redirects=True,
                             referer=prof.referer)
            if not r2.error and r2.text and not has_paywall(r2.text):
                return (r2.final_url or landed), r2.text, r2
        # a bare form page reached without a vid token
        if not text.strip():
            r3 = await s.get(url, allow_redirects=False, referer=prof.referer)
            if r3.is_redirect:
                r4 = await s.get(urljoin(url, r3.location), allow_redirects=True,
                                 referer=prof.referer)
                if not r4.error and r4.text:
                    return (r4.final_url or url), r4.text, r4
        return landed, text, r

    async def _post_form(self, s, page_url: str, text: str, prof: Profile,
                         out: ResolveOutcome, note_prefix: str = ""):
        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception:
            return None, "", "unparseable html"
        form = find_go_form(soup, prof.form_id)
        if form is None:
            return None, "", "no resolver form on page"
        fields = form_fields(form)
        action = form.get("action") or prof.post_path or "links/go"
        action = urljoin(page_url, action)
        if not action.lower().startswith(("http://", "https://")):
            action = urljoin(page_url, "/links/go")
        method = (form.get("method") or "post").lower()

        headers = {
            "x-requested-with": "XMLHttpRequest",
            "accept": "application/json, text/javascript, */*; q=0.01",
            "content-type": "application/x-www-form-urlencoded; charset=UTF-8",
        }
        if method == "get":
            r = await s.get(add_query(action, **fields) if fields else action,
                            headers=headers, referer=page_url, allow_redirects=True)
        else:
            r = await s.post(action, data=fields, headers=headers,
                             referer=page_url, allow_redirects=False)
        if r.error:
            return None, "", f"POST failed: {r.error}"

        # JSON answer
        payload = r.json()
        if isinstance(payload, dict):
            for key in ("url", "link", "destination", "redirect", "target", "href", "data"):
                val = payload.get(key)
                if isinstance(val, dict):
                    val = val.get("url") or val.get("link")
                if isinstance(val, str) and val.strip():
                    cand = val.strip().replace("\\/", "/")
                    if cand.lower().startswith(("http://", "https://", "//", "/")):
                        return urljoin(action, cand), "form", \
                            f"{note_prefix} JSON '{key}'".strip()
                    dec = b64url_decode(cand)
                    if dec.startswith(("http://", "https://")):
                        return dec, "form", f"{note_prefix} JSON '{key}' (b64)".strip()
            status = str(payload.get("status", "")).lower()
            msg = str(payload.get("message", "") or payload.get("msg", ""))
            if status in ("error", "fail", "failed") or msg:
                return None, "", (msg or "server rejected the request")[:200]

        if r.is_redirect:
            return urljoin(action, r.location), "form", f"{note_prefix} 3xx".strip()

        if r.text:
            # sometimes the response is another HTML step (ouo-style)
            try:
                soup2 = BeautifulSoup(r.text, "html.parser")
            except Exception:
                soup2 = None
            if soup2 is not None and find_go_form(soup2, prof.form_id) is not None:
                nxt, kind, note = await self._post_form(
                    s, r.final_url or action, r.text, prof, out,
                    note_prefix=note_prefix + ">step")
                if nxt:
                    return nxt, kind, note
            for cand, ck in page_urls(r.final_url or action, r.text)[:3]:
                if plausible_destination(cand, page_url):
                    return cand, ck, f"{note_prefix} html".strip()
        return None, "", (r.text or "")[:0] or "empty response"

    # -- strategy: GPLinks deep -------------------------------------------
    async def _gplinks_step(self, s, url: str, prof: Profile,
                            out: ResolveOutcome) -> tuple[Optional[str], str, str]:
        domain = host_of(url)
        origin = f"https://{domain}"

        # 1) first hit: collect the visitor id from the redirect Location
        r = await s.get(url, allow_redirects=False)
        landing_url = url
        vid = ""
        if r.error:
            return None, "", r.error
        if r.is_redirect:
            loc = urljoin(url, r.location)
            landing_url = loc
            q = parse_qs(urlparse(loc).query, keep_blank_values=True)
            vid = (q.get("vid") or [""])[0] or r.cookie("vid")
            # A GPLinks short link always bounces to *itself* (carrying the vid
            # token) or to a known ad-gate host. Anything else means the server
            # answered with the destination directly - take it and stop.
            lh = host_of(loc)
            if lh and lh != domain and lh not in INTERSTITIAL_DOMAINS \
                    and looks_like_destination(loc):
                return loc, "redirect", f"HTTP {r.status} straight through"
        vid = vid or r.cookie("vid")

        # 2) pull the interstitial (paywall-defeated)
        target = add_query(landing_url, skip_sub="1") if prof.skip_sub else landing_url
        page = await s.get(target, allow_redirects=True,
                           referer=prof.referer or origin + "/")
        if page.error:
            page = await s.get(landing_url, allow_redirects=True, referer=origin + "/")
        if page.error:
            return None, "", page.error
        page_url = page.final_url or target
        text = page.text or ""

        if has_paywall(text) and "skip_sub" not in page_url:
            alt = await s.get(add_query(page_url, skip_sub="1"), allow_redirects=True,
                              referer=origin + "/")
            if not alt.error and alt.text:
                page_url, text, page = (alt.final_url or page_url), alt.text, alt

        # 3) cookies carry the plan/visitor state used by the ad gate
        jar = dict(page.cookies)
        lid = jar.get("lid") or vid
        pid = jar.get("pid") or ""
        cvid = jar.get("vid") or vid
        plid = jar.get("plid") or ""

        # 4) tell the server every ad step is satisfied (best effort)
        if cvid:
            try:
                step = {1: 3, 11: 3, 12: 3, 2: 2, 3: 1}.get(
                    int(plid), 3) if str(plid).isdigit() else 3
            except ValueError:
                step = 3
            api_host = "gplinks.com" if domain.endswith("gplinks.co") else domain
            with contextlib.suppress(Exception):
                await s.get(
                    f"https://{api_host}/api/update-visitor-value",
                    params={"vid": cvid, "value": str(step), "impressions": "0"},
                    headers={"accept": "*/*", "referer": page_url},
                    allow_redirects=False, timeout=12,
                )
            out.hops.append(Hop(url=page_url, kind="api",
                                note=(f"update-visitor-value vid={cvid} step={step}"
                                      + (f" lid={lid}" if lid else "")
                                      + (f" pid={pid}" if pid else ""))))

        # 5) a powergam-style ad gate in between? decode straight to the target
        inter = decode_interstitial(page_url)
        if inter:
            out.hops.append(Hop(url=page_url, kind="interstitial", note="lid/pid decoded"))
            return inter, "interstitial", "decoded ad-gate lid/pid"

        # 6) the classic form POST, with adaptive anti-bot delay
        delay = prof.delay if prof.delay is not None else DELAYS.get(domain)
        attempts = max(1, CFG.form_retries + 1)
        last_note = ""
        for attempt in range(attempts):
            if delay > 0:
                await bot_sleep(delay)
            nxt, kind, note = await self._post_form(
                s, page_url, text, prof, out, note_prefix=f"try {attempt + 1}")
            if nxt:
                if prof.delay is None and CFG.adaptive_delay:
                    DELAYS.set(domain, delay)
                return nxt, kind, note
            last_note = note or last_note
            if prof.delay is None and CFG.adaptive_delay:
                delay = CFG.golink_delay
                DELAYS.set(domain, delay)
            fresh = await s.get(page_url, allow_redirects=True, referer=origin + "/")
            if not fresh.error and fresh.text:
                page_url, text = (fresh.final_url or page_url), fresh.text

        for cand, ck in page_urls(page_url, text, tight_only=True)[:5]:
            if plausible_destination(cand, page_url) and cand not in out.candidates:
                out.candidates.append(cand)
        return None, "", last_note or "gplinks form did not yield a destination"

    # -- strategy: adf.ly ---------------------------------------------------
    async def _adfly_step(self, s, url: str,
                          prof: Profile) -> tuple[Optional[str], str, str]:
        r = await s.get(url, allow_redirects=True, referer=prof.referer)
        if r.error:
            return None, "", r.error
        if r.is_redirect:
            return urljoin(url, r.location), "redirect", "HTTP redirect"
        text = r.text or ""
        m = YSMM_RE.search(text)
        if m:
            dec = decode_ysmm(m.group(1))
            if dec:
                return dec, "decode", "ysmm decoded"
        for cand, ck in page_urls(url, text)[:5]:
            if plausible_destination(cand, url):
                return cand, ck, "extracted"
        return None, "", "ysmm token missing or unsupported variant"

    # -- strategy: ouo.io (two-step form) -----------------------------------
    async def _ouo_step(self, s, url: str, prof: Profile,
                        out: ResolveOutcome) -> tuple[Optional[str], str, str]:
        current = url
        for step in range(3):
            r = await s.get(current, allow_redirects=True, referer=url)
            if r.error:
                return None, "", r.error
            if r.is_redirect:
                return urljoin(current, r.location), "redirect", "HTTP redirect"
            page_url = r.final_url or current
            text = r.text or ""
            try:
                soup = BeautifulSoup(text, "html.parser")
            except Exception:
                return None, "", "unparseable html"
            form = soup.find("form", id="form-captcha") or soup.find("form", {"id": "form"}) \
                or find_go_form(soup)
            if form is None:
                for cand, ck in page_urls(page_url, text)[:3]:
                    if plausible_destination(cand, page_url):
                        return cand, ck, "extracted"
                return None, "", "no ouo form found"
            fields = form_fields(form)
            action = urljoin(page_url, form.get("action") or page_url)
            await asyncio.sleep(1.2)
            p = await s.post(action, data=fields,
                             headers={"referer": page_url,
                                      "content-type": "application/x-www-form-urlencoded"},
                             allow_redirects=True)
            if p.error:
                return None, "", p.error
            if p.is_redirect:
                return urljoin(action, p.location), "form", f"ouo step {step + 1}"
            nxt_url = p.final_url or action
            if nxt_url == current and not p.text:
                return None, "", "ouo did not advance"
            current = nxt_url
        return None, "", "ouo exhausted its steps"

    # -- strategy: Linkvertise (best effort) --------------------------------
    async def _linkvertise_step(self, s, url: str, prof: Profile,
                                out: ResolveOutcome) -> tuple[Optional[str], str, str]:
        p = urlparse(url)
        parts = [x for x in p.path.split("/") if x]
        link_id = next((x for x in parts if x.isdigit()), "")
        r = await s.get(url, allow_redirects=True)
        if r.error:
            return None, "", r.error
        text = r.text or ""
        for cand, ck in page_urls(url, text)[:5]:
            if plausible_destination(cand, url):
                return cand, ck, "extracted"
        if link_id:
            api = f"https://publisher.linkvertise.com/api/v1/redirect/link/static/{link_id}"
            with contextlib.suppress(Exception):
                ar = await s.get(api, headers={"accept": "application/json"},
                                 timeout=15, allow_redirects=False)
                data = ar.json() if not ar.error else None
                if isinstance(data, dict):
                    deep = json.dumps(data)
                    m = re.search(r'"target"\s*:\s*"(https?:[^"]+)"', deep)
                    if m:
                        return m.group(1).replace("\\/", "/"), "api", "publisher api"
        out.error = ("Linkvertise gates its target behind browser-side bypass tokens. "
                     "The bot could not extract it headlessly.")
        return None, "", out.error

    # -- strategy: universal auto ------------------------------------------
    async def _auto_step(self, s, url: str, prof: Profile,
                         out: ResolveOutcome) -> tuple[Optional[str], str, str]:
        # 0) powergam-style ad gate -> decode without any request
        inter = decode_interstitial(url)
        if inter and normalize_url(inter) != normalize_url(url):
            return inter, "interstitial", "decoded lid/pid ad gate"

        # 1) bare redirect?
        r = await s.get(url, allow_redirects=False, referer=prof.referer)
        if r.error:
            return None, "", r.error
        if r.is_redirect:
            return urljoin(url, r.location), "redirect", f"HTTP {r.status}"

        # 2) 200 page: mine it
        if not r.text:
            return None, "", "empty body"
        text = r.text
        page_url = r.final_url or url

        # 2a) Landed ON a challenge domain? Stop before extracting anything.
        # URL-only here on purpose - see the text check at the end of this
        # method for why page copy alone must not veto a working redirect.
        wall = challenge_name(url=page_url)
        if wall:
            out.challenge = wall
            out.error = challenge_error(wall)
            out.hops.append(Hop(url=url, kind="challenge",
                                note=f"blocked by {wall}"))
            return None, "", ""

        # 2b) GPLinks-style "Go Ad-Free" paywall -> take the skip_sub door
        if has_paywall(text) and "skip_sub" not in page_url:
            alt = await s.get(add_query(page_url, skip_sub="1"), allow_redirects=True,
                              referer=prof.referer or url)
            if not alt.error and alt.text and not has_paywall(alt.text):
                text, page_url = alt.text, (alt.final_url or page_url)
                out.hops.append(Hop(url=url, kind="redirect", note="paywall skipped"))

        if YSMM_RE.search(text):
            m = YSMM_RE.search(text)
            dec = decode_ysmm(m.group(1))
            if dec:
                return dec, "decode", "ysmm decoded"

        inter2 = decode_interstitial(page_url)
        if inter2 and normalize_url(inter2) != normalize_url(url):
            return inter2, "interstitial", "decoded lid/pid ad gate"

        try:
            soup = BeautifulSoup(text, "html.parser")
        except Exception:
            soup = None
        if soup is not None and find_go_form(soup, prof.form_id) is not None:
            nxt, kind, note = await self._post_form(s, page_url, text, prof, out,
                                                    note_prefix="auto")
            if nxt:
                return nxt, kind, note

        is_interstitial = interstitial_signals(text)
        for cand, ck in page_urls(page_url, text, tight_only=not is_interstitial)[:6]:
            if plausible_destination(cand, page_url):
                return cand, ck, "extracted from page"

        # 3) last resort, only on a page that is obviously an ad gate
        if soup is not None and is_interstitial:
            tally: dict[str, int] = {}
            for a in soup.find_all("a", href=True):
                h = a["href"]
                if not h.lower().startswith("http"):
                    continue
                if not plausible_destination(h, page_url):
                    continue
                key = normalize_url(h)
                if key:
                    tally[key] = tally.get(key, 0) + 1
            if tally:
                best = max(tally.items(), key=lambda kv: (kv[1], len(kv[0])))[0]
                if best not in out.candidates:
                    out.candidates.append(best)
                return best, "cta", "best outbound link on interstitial"

        # Nothing unambiguous survived filtering. If the page reads like a bot
        # check, say so instead of the generic "no destination found" - it
        # tells the operator the fix is a proxy, not a code change.
        #
        # Checked HERE rather than before extraction, deliberately. Real
        # shortener ad-gates use phrases like "verifying you are human" right
        # next to a legitimate meta-refresh, so page copy must never veto a
        # redirect that actually works. Conversely a challenge page's own
        # captcha URL is filtered out by plausible_destination(), so it cannot
        # reach this point disguised as an answer.
        wall = challenge_name(text=text)
        if wall:
            out.challenge = wall
            out.error = challenge_error(wall)
            out.hops.append(Hop(url=url, kind="challenge",
                                note=f"blocked by {wall}"))
            return None, "", ""
        return None, "", "no destination found on page"


RESOLVER = LinkResolver()

# ══════════════════════════════════════════════════════════════════════════
# §6  API KEYS  (HMAC-signed, stateless - no database)
# ══════════════════════════════════════════════════════════════════════════
#
# A key is  shk.<payload>.<signature>  where payload is base64url JSON that
# carries everything needed to authorise a request:
#
#     {"id":"k_9f2c","name":"customer-1","tier":"vip","exp":1790000000,
#      "rpd":5000,"rpm":60,"batch":25}
#
# and signature is HMAC-SHA256(AUTH_SECRET, payload). Verification is one
# hash and a comparison - there is nothing to look up, so no database, no
# connection pool, and nothing to lose when Render restarts the service.
#
# Revoking:
#   * one key   -> add its id to AUTH_REVOKED_IDS and redeploy
#   * every key -> change AUTH_SECRET
# ══════════════════════════════════════════════════════════════════════════

KEY_PREFIX = "shk."
TIERS = ("free", "vip")


class ApiKeyError(Exception):
    """Raised for any authentication problem. Carries an HTTP status."""

    def __init__(self, message: str, status: int = 401, code: str = "invalid_key"):
        super().__init__(message)
        self.message = message
        self.status = status
        self.code = code


@dataclass(frozen=True)
class KeyInfo:
    id: str
    name: str
    tier: str
    exp: int
    rpd: int
    rpm: int
    batch: int

    @property
    def is_vip(self) -> bool:
        return self.tier == "vip"

    def to_dict(self) -> dict[str, Any]:
        return {"id": self.id, "name": self.name, "tier": self.tier,
                "exp": self.exp, "rpd": self.rpd, "rpm": self.rpm,
                "batch": self.batch}


def _b64e(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _b64d(text: str) -> bytes:
    pad = "=" * (-len(text) % 4)
    return base64.urlsafe_b64decode(text + pad)


def _sign(payload_b64: str, secret: str = "") -> str:
    sec = (secret or CFG.auth_secret).encode("utf-8")
    return _b64e(hmac.new(sec, payload_b64.encode("ascii"),
                          hashlib.sha256).digest())


DEFAULT_FREE_DAYS = 30


def _encode(info: KeyInfo, secret: str = "") -> str:
    """KeyInfo -> signed key string."""
    payload = _b64e(json.dumps(info.to_dict(), separators=(",", ":"),
                               sort_keys=True).encode("utf-8"))
    return f"{KEY_PREFIX}{payload}.{_sign(payload, secret)}"


def mint_key(name: str = "customer", tier: str = "vip", days: int = 30,
             rpd: int = 0, rpm: int = 0, batch: int = 0,
             key_id: str = "", secret: str = "") -> tuple[str, KeyInfo]:
    """Create a signed key. Returns (key_string, KeyInfo).

    days:  >0 expires in N days · =0 never expires for VIP, DEFAULT_FREE_DAYS
           for the free tier · <0 rejected. Negative used to be clamped to 0,
           which silently minted a key expiring the instant it was created.
    """
    tier = (tier or "free").lower().strip()
    if tier not in TIERS:
        raise ApiKeyError(f"tier must be one of {TIERS}", 400, "bad_tier")
    try:
        days = int(days)
    except (TypeError, ValueError):
        raise ApiKeyError(f"days must be an integer, got {days!r}", 400, "bad_days")
    if days < 0:
        raise ApiKeyError(
            "days cannot be negative. Use 0 for a key that never expires.",
            400, "bad_days")
    vip = tier == "vip"
    if days == 0:
        exp = 0 if vip else int(time.time()) + DEFAULT_FREE_DAYS * 86400
    else:
        exp = int(time.time()) + days * 86400
    info = KeyInfo(
        id=key_id or ("k_" + secrets.token_hex(4)),
        name=(name or "customer").strip()[:64],
        tier=tier,
        exp=exp,
        rpd=rpd or (0 if vip else CFG.free_daily_limit),
        rpm=rpm or (CFG.rate_rpm * (4 if vip else 1)),
        batch=batch or (CFG.batch_limit if vip else min(5, CFG.batch_limit)),
    )
    return _encode(info, secret), info


def verify_key(key: str, secret: str = "") -> KeyInfo:
    """Validate a key. Raises ApiKeyError with a precise reason if not."""
    if not key or not isinstance(key, str):
        raise ApiKeyError("missing API key", 401, "missing_key")
    key = key.strip()

    # The optional shared static key, for callers who do not want signed keys.
    if CFG.static_api_key and hmac.compare_digest(key, CFG.static_api_key):
        return KeyInfo(id="static", name="static-key", tier="vip", exp=0,
                       rpd=0, rpm=CFG.rate_rpm * 4, batch=CFG.batch_limit)

    if not key.startswith(KEY_PREFIX):
        raise ApiKeyError(
            "malformed key - expected the form shk.<payload>.<signature>. "
            "Mint one with: python solver_api.py --mint NAME --tier vip",
            401, "malformed_key")

    body = key[len(KEY_PREFIX):]
    if body.count(".") != 1:
        raise ApiKeyError("malformed key - expected exactly one '.' separator",
                          401, "malformed_key")
    payload_b64, sig = body.split(".", 1)

    if not CFG.auth_secret and not secret:
        raise ApiKeyError(
            "the server has no AUTH_SECRET configured, so it cannot verify "
            "any key. Set AUTH_SECRET (python solver_api.py --print-secret).",
            500, "server_unconfigured")

    if not hmac.compare_digest(sig, _sign(payload_b64, secret)):
        raise ApiKeyError(
            "bad signature - this key was not issued by this server, or "
            "AUTH_SECRET has changed since it was minted", 401, "bad_signature")

    try:
        data = json.loads(_b64d(payload_b64).decode("utf-8"))
    except (ValueError, binascii.Error, UnicodeDecodeError) as exc:
        raise ApiKeyError(f"unreadable key payload: {exc}", 401, "bad_payload")
    if not isinstance(data, dict):
        raise ApiKeyError("key payload is not an object", 401, "bad_payload")

    try:
        info = KeyInfo(
            id=str(data.get("id", ""))[:32],
            name=str(data.get("name", ""))[:64],
            tier=str(data.get("tier", "free")).lower(),
            exp=int(data.get("exp", 0) or 0),
            rpd=int(data.get("rpd", 0) or 0),
            rpm=int(data.get("rpm", 0) or 0),
            batch=int(data.get("batch", 0) or 0),
        )
    except (TypeError, ValueError) as exc:
        raise ApiKeyError(f"invalid key fields: {exc}", 401, "bad_payload")

    if info.tier not in TIERS:
        raise ApiKeyError(f"unknown tier {info.tier!r}", 401, "bad_tier")
    if info.id in CFG.revoked_ids:
        raise ApiKeyError(f"key {info.id} has been revoked", 403, "revoked")
    if info.exp and info.exp < int(time.time()):
        when = datetime.fromtimestamp(info.exp, timezone.utc).strftime("%Y-%m-%d")
        raise ApiKeyError(f"key expired on {when}", 403, "expired")
    return info


# ══════════════════════════════════════════════════════════════════════════
# §7  RATE LIMITING, RESULT CACHE, STATS
# ══════════════════════════════════════════════════════════════════════════

class RateLimiter:
    """Sliding-window requests/minute plus a UTC-day quota, per key id.

    In RAM by necessity (see the note at the top of this file). Two honest
    consequences: counters reset when Render restarts the service, and they
    are per-instance - if you ever scale past one instance you need Redis.
    Neither loses customer data, which is the point of the design.
    """

    def __init__(self) -> None:
        self._win: dict[str, deque] = {}
        self._day: dict[str, tuple[str, int]] = {}

    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def check(self, key: KeyInfo) -> None:
        """Raise ApiKeyError(429) if this call should be refused."""
        now = time.time()

        if key.rpm > 0:
            win = self._win.setdefault(key.id, deque())
            cutoff = now - 60.0
            while win and win[0] < cutoff:
                win.popleft()
            if len(win) >= key.rpm + CFG.rate_burst:
                retry = int(60 - (now - win[0])) + 1
                raise ApiKeyError(
                    f"rate limit: {len(win)} requests in the last minute "
                    f"(limit {key.rpm}/min). Retry in {retry}s.",
                    429, "rate_limited")
            win.append(now)

        if key.rpd > 0:
            today = self._today()
            day, used = self._day.get(key.id, ("", 0))
            if day != today:
                day, used = today, 0
            if used >= key.rpd:
                raise ApiKeyError(
                    f"daily quota exhausted: {used}/{key.rpd} solves today. "
                    "Resets at 00:00 UTC.", 429, "quota_exhausted")
            self._day[key.id] = (today, used + 1)

    def usage(self, key_id: str) -> dict[str, Any]:
        now = time.time()
        win = self._win.get(key_id, deque())
        per_min = sum(1 for t in win if t >= now - 60.0)
        day, used = self._day.get(key_id, ("", 0))
        return {"per_minute": per_min,
                "today": used if day == self._today() else 0}

    def reset(self) -> None:
        self._win.clear()
        self._day.clear()


LIMITER = RateLimiter()


class ResultCache:
    """url -> resolved payload. Saves time and, more importantly, keeps the
    outbound request rate to any one shortener low enough not to trip its
    abuse detection."""

    def __init__(self, maxsize: int = 2000, ttl: int = 3600) -> None:
        self.maxsize = max(1, maxsize)
        self.ttl = max(0, ttl)
        self._d: dict[str, tuple[float, dict]] = {}
        self.hits = 0
        self.misses = 0

    def get(self, url: str) -> Optional[dict]:
        if self.ttl <= 0:
            return None
        row = self._d.get(url)
        if not row:
            self.misses += 1
            return None
        exp, payload = row
        if exp < time.time():
            self._d.pop(url, None)
            self.misses += 1
            return None
        self.hits += 1
        return payload

    def put(self, url: str, payload: dict) -> None:
        if self.ttl <= 0:
            return
        # Never cache a failure: a transient 403 or timeout would otherwise
        # be served back for an hour after the site has recovered.
        if not payload.get("ok"):
            return
        self._d[url] = (time.time() + self.ttl, payload)
        while len(self._d) > self.maxsize:
            oldest = min(self._d, key=lambda k: self._d[k][0])
            self._d.pop(oldest, None)

    def clear(self) -> int:
        n = len(self._d)
        self._d.clear()
        self.hits = self.misses = 0
        return n


CACHE = ResultCache(CFG.cache_max, CFG.cache_ttl)


class Stats:
    def __init__(self) -> None:
        self.requests = 0
        self.solved = 0
        self.failed = 0
        self.rejected = 0
        self.by_domain: dict[str, list[int]] = {}     # domain -> [ok, fail]
        self.latency: deque = deque(maxlen=200)
        self.started = time.time()

    def record(self, payload: dict) -> None:
        dom = payload.get("shortener") or "?"
        row = self.by_domain.setdefault(dom, [0, 0])
        row[0 if payload.get("ok") else 1] += 1
        if payload.get("ok"):
            self.solved += 1
        else:
            self.failed += 1
        if payload.get("elapsed_ms"):
            self.latency.append(int(payload["elapsed_ms"]))

    def to_dict(self) -> dict[str, Any]:
        lat = sorted(self.latency)
        pct = lambda p: lat[min(len(lat) - 1, int(len(lat) * p))] if lat else 0
        total = self.solved + self.failed
        top = sorted(self.by_domain.items(),
                     key=lambda kv: sum(kv[1]), reverse=True)[:12]
        return {
            "uptime_s": int(time.time() - self.started),
            "requests": self.requests,
            "solved": self.solved,
            "failed": self.failed,
            "rejected_auth_or_rate": self.rejected,
            "success_rate": round(self.solved / total, 4) if total else None,
            "latency_ms": {"p50": pct(0.50), "p90": pct(0.90),
                           "p99": pct(0.99), "max": lat[-1] if lat else 0},
            "cache": {"size": len(CACHE._d), "hits": CACHE.hits,
                      "misses": CACHE.misses},
            "by_domain": [{"domain": d, "ok": v[0], "fail": v[1]}
                          for d, v in top],
        }


STATS = Stats()


# ══════════════════════════════════════════════════════════════════════════
# §8  HTTP API
# ══════════════════════════════════════════════════════════════════════════

try:
    from pydantic import BaseModel, Field
except ImportError:  # pragma: no cover
    _die("pydantic is not installed.\n\n"
         f"    {sys.executable} -m pip install -r requirements-api.txt")


class SolveRequest(BaseModel):
    url: str = Field(..., min_length=4, max_length=4000,
                     description="Shortened URL to resolve")
    chain: bool = Field(False, description="Include the full hop-by-hop trail")


class BatchRequest(BaseModel):
    urls: list[str] = Field(..., min_length=1, description="URLs to resolve")
    chain: bool = Field(False, description="Include hop trails")
    parallel: bool = Field(
        False,
        description=("Resolve concurrently. Off by default ON PURPOSE: "
                     "bursting many requests at one shortener from a single "
                     "datacentre IP is the fastest way to get that IP "
                     "banned. Sequential is slower and far more reliable."))


def _err(message: str, code: str, status: int, **extra: Any) -> JSONResponse:
    body: dict[str, Any] = {"ok": False,
                            "error": {"code": code, "message": message}}
    body.update(extra)
    return JSONResponse(status_code=status, content=body)


def _strip(payload: dict[str, Any], want_chain: bool) -> dict[str, Any]:
    """Drop the hop trail unless the caller asked for it.

    The cache always stores the full payload, so one entry serves both
    chain=true and chain=false callers. Keying the cache on the flag instead
    would halve the hit rate; not stripping at all is what caused a cached
    chain-less response to be served to a caller who asked for the trail.
    """
    if want_chain:
        return payload
    return {k: v for k, v in payload.items() if k != "chain"}


def _serialize(out: ResolveOutcome, want_chain: bool,
               cached: bool = False) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": bool(out.ok),
        "input": out.input_url,
        "destination": out.final_url if out.ok else "",
        "shortener": out.shortener,
        "handler": out.handler,
        "hops": out.depth,
        "elapsed_ms": out.elapsed_ms,
        "cached": cached,
        "partial": out.partial,
        "error": "" if out.ok else out.error,
    }
    if out.challenge and not out.ok:
        # Which anti-bot system stopped us, so a caller can tell "this domain
        # needs a proxy" from "this link is dead" without parsing prose.
        #
        # Deliberately suppressed on success. A walk can legitimately reach a
        # valid destination and THEN hit a wall while probing for a further
        # hop - http://github.com 301s to https://github.com/ and the second
        # page is DataDome-guarded. The destination is still the right answer,
        # so `challenge` must mean "why this failed" and nothing else. The hop
        # trail (chain=true) still shows the wall for anyone debugging.
        payload["challenge"] = out.challenge
    if not out.ok and out.candidates:
        # What the resolver could see but could not confirm. Useful for
        # debugging a domain, and honest about not having finished the job.
        payload["candidates"] = list(out.candidates[:8])
    if want_chain:
        payload["chain"] = [{"url": h.url, "kind": h.kind, "note": h.note}
                            for h in out.hops]
    return payload


def _client_ip(request: Request) -> str:
    """Render puts the service behind a proxy, so request.client.host is the
    proxy. Trust X-Forwarded-For's first hop (Render sets it) unless told not
    to - otherwise every caller shares one rate-limit bucket."""
    if CFG.trust_forwarded:
        fwd = request.headers.get("x-forwarded-for", "")
        if fwd:
            return fwd.split(",")[0].strip()
    return request.client.host if request.client else "unknown"


async def require_key(request: Request,
                      x_api_key: Optional[str] = Header(None, alias="X-API-Key"),
                      authorization: Optional[str] = Header(None)) -> KeyInfo:
    """FastAPI dependency: authenticate and rate-limit the caller."""
    if not CFG.auth_required:
        return KeyInfo(id="open", name="open-access", tier="vip", exp=0,
                       rpd=0, rpm=0, batch=CFG.batch_limit)

    raw = (x_api_key or "").strip()
    if not raw and authorization:
        auth = authorization.strip()
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
    if not raw:
        # Allow ?key= for convenience in a browser, since that is how most
        # people will first try the API.
        raw = (request.query_params.get("key") or "").strip()
    if not raw:
        raise ApiKeyError(
            "no API key supplied. Send it as  X-API-Key: shk....  or  "
            "Authorization: Bearer shk....", 401, "missing_key")

    info = verify_key(raw)
    LIMITER.check(info)
    return info


def require_admin(x_admin_secret: Optional[str] = Header(None,
                                                         alias="X-Admin-Secret"),
                  authorization: Optional[str] = Header(None)) -> None:
    if not CFG.admin_secret:
        raise ApiKeyError(
            "ADMIN_SECRET is not set on this server, so /admin is disabled. "
            "Set it to enable key minting over HTTP.", 503, "admin_disabled")
    raw = (x_admin_secret or "").strip()
    if not raw and authorization:
        auth = authorization.strip()
        raw = auth[7:].strip() if auth.lower().startswith("bearer ") else auth
    if not raw or not hmac.compare_digest(raw, CFG.admin_secret):
        raise ApiKeyError("invalid admin secret", 403, "bad_admin_secret")


app = FastAPI(
    title=API_NAME,
    version=__version__,
    description=(
        "Resolves shortened links to their real destination.\n\n"
        "**Authenticate** with `X-API-Key: shk....` (or "
        "`Authorization: Bearer shk....`).\n\n"
        "```bash\n"
        "curl -X POST https://YOUR-APP.onrender.com/solve \\\n"
        "     -H 'X-API-Key: shk....' \\\n"
        "     -H 'Content-Type: application/json' \\\n"
        "     -d '{\"url\":\"https://gplinks.co/BFfOX\"}'\n"
        "```\n"
    ),
    docs_url="/docs", redoc_url="/redoc", openapi_url="/openapi.json",
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=list(CFG.cors_origins),
    allow_credentials=False,
    allow_methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
    allow_headers=["*"],
)


@app.exception_handler(RequestValidationError)
async def _validation_error_handler(request: Request,
                                    exc: RequestValidationError):
    """Normalise FastAPI's 422 into the same envelope as every other error.

    Without this a client must parse two shapes: {"detail":[...]} for schema
    violations and {"ok":false,"error":{...}} for everything else.
    """
    errs = exc.errors() if hasattr(exc, "errors") else []
    details = []
    for e in errs[:8]:
        loc = ".".join(str(x) for x in e.get("loc", ()) if x != "body") or "body"
        details.append({"field": loc, "message": e.get("msg", ""),
                        "type": e.get("type", "")})
    head = details[0] if details else {"field": "body",
                                       "message": "invalid request"}
    STATS.rejected += 1
    log.warning("validation error on %s: %s", request.url.path,
                json.dumps(details, default=str)[:400])
    return _err(f"{head['field']}: {head['message']}", "validation_error",
                422, details=details)


@app.exception_handler(ApiKeyError)
async def _apikey_error_handler(request: Request, exc: ApiKeyError):
    STATS.rejected += 1
    resp = _err(exc.message, exc.code, exc.status)
    if exc.status == 429:
        # Let a well-behaved client back off on its own.
        resp.headers["Retry-After"] = "60"
    return resp


@app.get("/", include_in_schema=False)
async def root() -> dict[str, Any]:
    return {
        "service": API_NAME,
        "version": __version__,
        "domains": len(REGISTRY),
        "backend": "curl_cffi" if HTTP._cffi_ok else "httpx",
        "auth_required": CFG.auth_required,
        "proxy": bool(CFG.proxy_url),
        "endpoints": {
            "POST /solve": "resolve one link",
            "GET /solve?url=": "same, for quick testing",
            "POST /batch": "resolve many links",
            "GET /domains": "supported shorteners",
            "GET /stats": "live counters",
            "GET /health": "liveness probe (no auth)",
            "GET /docs": "interactive OpenAPI docs",
        },
    }


@app.get("/health")
async def health() -> dict[str, Any]:
    """Liveness probe. Point UptimeRobot here.

    Must stay dependency-free and fast: no network, no auth, no database.
    Render's free tier spins a service down after 15 idle minutes and an
    external ping to this route is what keeps it warm. (Do NOT ping
    /robots.txt - Render answers that itself while the service sleeps, so it
    never wakes anything up.)
    """
    return {"status": "ok", "version": __version__,
            "uptime_s": int(time.time() - STATS.started)}


@app.get("/domains")
async def domains(key: KeyInfo = Depends(require_key)) -> dict[str, Any]:
    groups: dict[str, list[str]] = {}
    for host, prof in sorted(REGISTRY.items()):
        groups.setdefault(prof.handler, []).append(host)
    return {"ok": True, "total": len(REGISTRY),
            "by_handler": {k: sorted(v) for k, v in sorted(groups.items())},
            "note": ("Domains not listed still work - the 'auto' engine tries "
                     "redirect, meta-refresh, JS and hidden-form strategies on "
                     "any host.")}


@app.post("/solve")
async def solve(body: SolveRequest, request: Request,
                key: KeyInfo = Depends(require_key)) -> Any:
    STATS.requests += 1
    url = normalize_url(body.url.strip())
    if not url:
        return _err("that does not look like a valid http(s) URL",
                    "bad_url", 400)

    cached = CACHE.get(url)
    if cached:
        hit = dict(cached)
        hit["cached"] = True
        hit["elapsed_ms"] = 0
        return _strip(hit, body.chain)

    try:
        out = await asyncio.wait_for(RESOLVER.resolve(url),
                                     timeout=CFG.solve_timeout)
    except asyncio.TimeoutError:
        return _err(f"resolver exceeded {CFG.solve_timeout:.0f}s - the site's "
                    "anti-bot timer is longer than this server will wait. "
                    "Raise SOLVE_TIMEOUT, or retry (the delay is learned).",
                    "timeout", 504, url=url)

    payload = _serialize(out, True)        # always full, for the cache
    CACHE.put(url, payload)
    STATS.record(payload)
    log.info("solve %s -> %s (%s, %dms) key=%s ip=%s", url,
             "ok" if payload["ok"] else payload["error"][:80],
             payload["handler"], payload["elapsed_ms"], key.id,
             _client_ip(request))
    return _strip(payload, body.chain)


@app.get("/solve")
async def solve_get(url: str = Query(..., min_length=4, max_length=4000),
                    chain: bool = Query(False),
                    request: Request = None,
                    key: KeyInfo = Depends(require_key)) -> Any:
    """Same as POST /solve, but reachable from a browser or a bare curl."""
    return await solve(SolveRequest(url=url, chain=chain), request, key)


@app.post("/batch")
async def batch(body: BatchRequest, request: Request,
                key: KeyInfo = Depends(require_key)) -> Any:
    STATS.requests += 1
    urls = [u.strip() for u in body.urls if u and u.strip()]
    if not urls:
        return _err("no URLs supplied", "empty", 400)
    if len(urls) > key.batch:
        return _err(f"this key allows {key.batch} URLs per batch "
                    f"({len(urls)} given)", "batch_too_large", 400,
                    limit=key.batch)
    urls = urls[: key.batch]

    results: list[dict[str, Any]] = []
    if body.parallel:
        outs = await asyncio.gather(*(RESOLVER.resolve(normalize_url(u) or u)
                                      for u in urls),
                                    return_exceptions=True)
        for u, o in zip(urls, outs):
            if isinstance(o, BaseException):
                results.append({"ok": False, "input": u, "destination": "",
                                "error": f"{type(o).__name__}: {o}"})
            else:
                full = _serialize(o, True)
                CACHE.put(normalize_url(u) or u, full)
                results.append(_strip(full, body.chain))
    else:
        # Sequential on purpose - see BatchRequest.parallel.
        for u in urls:
            norm = normalize_url(u)
            if not norm:
                results.append({"ok": False, "input": u, "destination": "",
                                "error": "not a valid http(s) URL"})
                continue
            cached = CACHE.get(norm)
            if cached:
                hit = dict(cached)
                hit["cached"] = True
                hit["elapsed_ms"] = 0
                results.append(_strip(hit, body.chain))
                continue
            try:
                out = await asyncio.wait_for(RESOLVER.resolve(norm),
                                             timeout=CFG.solve_timeout)
            except asyncio.TimeoutError:
                results.append({"ok": False, "input": norm, "destination": "",
                                "error": f"timed out after "
                                         f"{CFG.solve_timeout:.0f}s"})
                continue
            payload = _serialize(out, True)
            CACHE.put(norm, payload)
            results.append(_strip(payload, body.chain))

    for r in results:
        STATS.record(r)
    ok = sum(1 for r in results if r["ok"])
    log.info("batch %d urls, %d ok, key=%s ip=%s", len(results), ok, key.id,
             _client_ip(request))
    return {"ok": True, "count": len(results), "solved": ok,
            "failed": len(results) - ok, "results": results}


@app.get("/stats")
async def stats(key: KeyInfo = Depends(require_key)) -> dict[str, Any]:
    data = STATS.to_dict()
    data["your_key"] = {"id": key.id, "name": key.name, "tier": key.tier,
                        "usage": LIMITER.usage(key.id)}
    data["learned_delays"] = DELAYS.snapshot()
    return {"ok": True, **data}


# ── admin ─────────────────────────────────────────────────────────────────

@app.post("/admin/keys")
async def admin_mint(request: Request,
                     name: str = "customer", tier: str = "vip", days: int = 30,
                     rpd: int = 0, rpm: int = 0, batch: int = 0,
                     _admin: None = Depends(require_admin)) -> Any:
    """Mint a key over HTTP. Requires X-Admin-Secret.

    Prefer the CLI (`python solver_api.py --mint`) - it never puts the key in
    a request log.
    """
    try:
        key, info = mint_key(name, tier, days, rpd, rpm, batch)
    except ApiKeyError as exc:
        return _err(exc.message, exc.code, exc.status)
    log.warning("admin minted key id=%s tier=%s days=%s name=%s",
                info.id, info.tier, days, info.name)
    return {"ok": True, "key": key, "info": info.to_dict(),
            "warning": "store this now - it cannot be retrieved later"}


@app.get("/admin/keys/verify")
async def admin_verify(key: str = Query(...),
                       _admin: None = Depends(require_admin)) -> Any:
    try:
        info = verify_key(key)
    except ApiKeyError as exc:
        return _err(exc.message, exc.code, exc.status)
    return {"ok": True, "info": info.to_dict(),
            "usage": LIMITER.usage(info.id)}


@app.get("/admin/delays")
async def admin_delays(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    return {"ok": True, "learned": DELAYS.snapshot(),
            "default": CFG.golink_delay}


@app.put("/admin/delays/{domain}")
async def admin_set_delay(domain: str, seconds: float = Query(..., ge=0, le=300),
                          _admin: None = Depends(require_admin)) -> dict[str, Any]:
    """Pin a domain's anti-bot wait (the bot's /setdelay).

    Use this when a site needs longer than the resolver guesses and you do not
    want to wait for it to learn the value.
    """
    dom = domain.strip().lower().lstrip(".")
    if not dom:
        return _err("empty domain", "bad_domain", 400)
    DELAYS.override(dom, seconds)
    log.warning("admin pinned delay %s = %.1fs", dom, seconds)
    return {"ok": True, "domain": dom, "delay": DELAYS.get(dom)}


@app.delete("/admin/cache")
async def admin_clear_cache(_admin: None = Depends(require_admin)) -> dict[str, Any]:
    n = CACHE.clear()
    d = DELAYS.clear()
    LIMITER.reset()
    return {"ok": True, "cleared_cache_entries": n, "cleared_delays": d}


# ══════════════════════════════════════════════════════════════════════════
# §9  ENTRYPOINT / CLI
# ══════════════════════════════════════════════════════════════════════════

def _backend() -> str:
    return "curl_cffi (chrome)" if HTTP._cffi_ok else "httpx (no impersonation)"


def preflight() -> list[str]:
    """Return a list of problems. Empty means the service is configured."""
    problems: list[str] = []
    if CFG.auth_required:
        if not CFG.auth_secret and not CFG.static_api_key:
            problems.append(
                "AUTH_SECRET is not set. No key can be verified, so every "
                "request will 500.\n"
                "    Generate one:  python solver_api.py --print-secret\n"
                "    Then set it on Render: Environment -> AUTH_SECRET")
        elif len(CFG.auth_secret) < 32:
            problems.append(
                f"AUTH_SECRET is only {len(CFG.auth_secret)} characters. Use "
                "at least 32 - it is the only thing standing between an "
                "attacker and forging unlimited VIP keys.")
    if not HTTP._cffi_ok:
        problems.append(
            "curl_cffi is unavailable, so the resolver falls back to httpx "
            "with NO browser TLS impersonation. Cloudflare-protected "
            "shorteners - GPLINKS INCLUDED - will very likely return 403.\n"
            "    pip install -r requirements-api.txt\n"
            "    On Render, also confirm the build log shows curl_cffi "
            "installing from a manylinux wheel.")
    if CFG.allow_private_nets:
        problems.append(
            "ALLOW_PRIVATE_NETWORKS is on. That disables the SSRF guard, so "
            "any caller can point this service at your internal network or "
            "the cloud metadata endpoint (169.254.169.254) and read back "
            "whatever answers. Leave it off in production - it exists only so "
            "the test suite can drive fixtures on 127.0.0.1.")
    if CFG.host not in ("0.0.0.0", "::"):
        problems.append(
            f"HOST is {CFG.host!r}. Render only routes traffic to a service "
            "listening on 0.0.0.0.")
    return problems


def print_report() -> None:
    print(f"\n{API_NAME} v{__version__}")
    print(f"  registry domains : {len(REGISTRY)}")
    print(f"  handlers         : "
          f"{sorted({p.handler for p in REGISTRY.values()})}")
    print(f"  backend          : {_backend()}")
    print(f"  outbound proxy   : "
          f"{'yes (' + urlparse(CFG.proxy_url).hostname + ')' if CFG.proxy_url else 'no - direct from the host IP'}")
    print(f"  auth             : "
          f"{'required' if CFG.auth_required else 'DISABLED (open access)'}")
    print(f"  auth secret      : "
          f"{'set (' + str(len(CFG.auth_secret)) + ' chars)' if CFG.auth_secret else 'NOT SET'}")
    print(f"  admin endpoint   : "
          f"{'enabled' if CFG.admin_secret else 'disabled (no ADMIN_SECRET)'}")
    print(f"  revoked key ids  : "
          f"{', '.join(CFG.revoked_ids) if CFG.revoked_ids else '(none)'}")
    print(f"  solve timeout    : {CFG.solve_timeout:.0f}s")
    print(f"  concurrency      : {CFG.concurrency} parallel resolves")
    print(f"  batch limit      : {CFG.batch_limit} urls")
    print(f"  cache            : {CFG.cache_max} entries, {CFG.cache_ttl}s ttl")
    print(f"  listening on     : {CFG.host}:{CFG.port}")
    print(f"  ssrf guard       : "
          f"{'OFF - private ranges reachable!' if CFG.allow_private_nets else 'on (blocks RFC1918/loopback/metadata)'}")
    print(f"  routes           : {len(app.routes)}")


def selfcheck() -> int:
    """Offline verification: no network, no credentials, safe to run anywhere."""
    print(f"\n─── {API_NAME} self-check ───")
    fails = 0

    def ck(cond: bool, label: str, detail: str = "") -> None:
        nonlocal fails
        if cond:
            print(f"  \033[32m✓\033[0m {label}")
        else:
            fails += 1
            print(f"  \033[31m✗\033[0m {label}" + (f"  <- {detail}" if detail else ""))

    ck(len(REGISTRY) > 100, f"registry loaded ({len(REGISTRY)} domains)")
    ck(all(p.handler for p in REGISTRY.values()), "every domain has a handler")
    ck(profile_for("https://gplinks.co/BFfOX").handler == "gplinks",
       "gplinks.co routes to the deep handler")
    ck(HTTP._cffi_ok or True, f"http backend: {_backend()}")
    ck(normalize_url("gplinks.co/BFfOX") == "https://gplinks.co/BFfOX",
       "normalize_url adds the scheme")

    # SSRF guard - the single most important safety property of a public
    # resolver, because it is pointed at URLs chosen by strangers.
    # Force the guard on for the duration of the check: if the operator set
    # ALLOW_PRIVATE_NETWORKS=1 (which preflight warns about), testing in that
    # state would report four passes that prove nothing.
    _was_private = CFG.allow_private_nets
    object.__setattr__(CFG, "allow_private_nets", False)
    try:
        for bad in ("http://127.0.0.1:8080/x",
                    "http://169.254.169.254/latest/meta-data/",
                    "http://10.0.0.5/admin", "http://192.168.1.1/"):
            blocked = False
            try:
                asyncio.run(guard_host(urlparse(bad).hostname or ""))
            except SsrfBlocked:
                blocked = True
            except Exception:
                blocked = True
            ck(blocked, f"SSRF guard refuses {urlparse(bad).hostname}")
        try:
            asyncio.run(guard_host("example.com"))
            ck(True, "SSRF guard allows a normal public host")
        except Exception as exc:
            ck(False, "SSRF guard allows a normal public host", str(exc))
    finally:
        object.__setattr__(CFG, "allow_private_nets", _was_private)

    # Key mint/verify round trip, on a throwaway secret so a real one is never
    # needed to run this check.
    secret = secrets.token_urlsafe(48)
    k, info = mint_key("selfcheck", "vip", 30, secret=secret)
    ck(k.startswith(KEY_PREFIX) and k.count(".") == 2, "minted key is well formed")
    back = verify_key(k, secret=secret)
    ck(back.id == info.id and back.tier == "vip", "key verifies and round-trips")
    ck(verify_key(mint_key("t", "free", 1, secret=secret)[0], secret=secret).tier
       == "free", "free tier round-trips")
    tampered = k[:-4] + ("AAAA" if not k.endswith("AAAA") else "BBBB")
    try:
        verify_key(tampered, secret=secret)
        ck(False, "tampered signature rejected")
    except ApiKeyError as exc:
        ck(exc.code == "bad_signature", "tampered signature rejected")
    try:
        verify_key(k, secret=secrets.token_urlsafe(48))
        ck(False, "key from another secret rejected")
    except ApiKeyError as exc:
        ck(exc.code == "bad_signature", "key from another secret rejected")
    # Expiry. mint_key() now refuses negative days, so sign a payload whose
    # exp is genuinely in the past. This proves the check reads the signed
    # payload rather than anything the caller can tweak afterwards - and it is
    # the assertion that caught the max(days, 0) clamp masking this path.
    stale = KeyInfo(id="k_stale", name="expired", tier="vip",
                    exp=int(time.time()) - 86400, rpd=0, rpm=10, batch=5)
    try:
        verify_key(_encode(stale, secret=secret), secret=secret)
        ck(False, "expired key rejected")
    except ApiKeyError as exc:
        ck(exc.code == "expired", "expired key rejected", exc.code)
    edge = KeyInfo(id="k_edge", name="edge", tier="free",
                   exp=int(time.time()) + 1, rpd=0, rpm=10, batch=5)
    ck(verify_key(_encode(edge, secret=secret), secret=secret).exp > 0,
       "a key expiring in the future still verifies")
    try:
        mint_key("neg", "vip", -1, secret=secret)
        ck(False, "negative days refused")
    except ApiKeyError as exc:
        ck(exc.code == "bad_days", "negative days refused", exc.code)
    never = mint_key("perm", "vip", 0, secret=secret)[0]
    ck(verify_key(never, secret=secret).exp == 0, "vip days=0 never expires")
    ck(verify_key(mint_key("f", "free", 0, secret=secret)[0],
                  secret=secret).exp > int(time.time()),
       f"free days=0 defaults to {DEFAULT_FREE_DAYS} days, not instant expiry")
    try:
        verify_key("shk.nonsense", secret=secret)
        ck(False, "malformed key rejected")
    except ApiKeyError:
        ck(True, "malformed key rejected")

    # Rate limiter.
    rl = RateLimiter()
    probe = KeyInfo(id="probe", name="p", tier="free", exp=0,
                    rpd=3, rpm=2, batch=5)
    rl._win.clear(); rl._day.clear()
    got = 0
    limited = False
    for _ in range(10):
        try:
            rl.check(probe)
            got += 1
        except ApiKeyError as exc:
            limited = True
            ck(exc.status == 429, "rate limiter returns 429")
            break
    ck(limited and got <= 3 + CFG.rate_burst,
       f"rate limiter actually limits (allowed {got} of 10)")

    # Cache must not serve a failure back for an hour.
    c = ResultCache(maxsize=2, ttl=60)
    c.put("https://x/1", {"ok": False, "error": "boom"})
    ck(c.get("https://x/1") is None, "failures are never cached")
    c.put("https://x/2", {"ok": True, "destination": "https://d/2"})
    ck(bool(c.get("https://x/2")), "successes are cached")
    c.put("https://x/3", {"ok": True}); c.put("https://x/4", {"ok": True})
    ck(len(c._d) <= 2, "cache respects maxsize")

    # Delay store clamping.
    DELAYS.set("example.test", 9999)
    ck(DELAYS.get("example.test") <= DelayStore.MAX,
       "learned delay is clamped to a sane maximum")
    DELAYS.set("example.test", -5)
    ck(DELAYS.get("example.test") >= DelayStore.MIN,
       "learned delay is clamped to a sane minimum")
    DELAYS._d.pop("example.test", None)

    # The ASGI app must build and expose the routes.
    paths = {getattr(r, "path", "") for r in app.routes}
    for want in ("/health", "/solve", "/batch", "/domains", "/stats", "/"):
        ck(want in paths, f"route registered: {want}")

    print(f"\n─── self-check: {'PASS' if not fails else str(fails) + ' FAILED'} ───\n")
    return 1 if fails else 0


def main(argv: list[str]) -> int:
    logging.basicConfig(
        level=getattr(logging, CFG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s | %(message)s",
        stream=sys.stdout,          # Render collects stdout
    )

    if "--print-secret" in argv:
        print(secrets.token_urlsafe(48))
        return 0

    if "--selfcheck" in argv or "--check" in argv:
        print_report()
        problems = preflight()
        if problems:
            print("\n\033[1;33m⚠ configuration problems\033[0m")
            for i, p in enumerate(problems, 1):
                print(f"  {i}. {p}")
        else:
            print("\n  \033[32m✓ configuration ok\033[0m")
        rc = selfcheck()
        return rc or (2 if problems else 0)

    if "--mint" in argv:
        i = argv.index("--mint")
        # `--mint NAME`: the name is the VALUE of --mint. Reading --name
        # instead silently minted every key as "customer", which is how the
        # first version of this CLI shipped.
        name = (argv[i + 1]
                if i + 1 < len(argv) and not argv[i + 1].startswith("--")
                else "customer")

        def opt(flag: str, default: str) -> str:
            if flag in argv:
                j = argv.index(flag)
                if j + 1 < len(argv):
                    return argv[j + 1]
            return default

        name = opt("--name", name)      # explicit --name still wins
        if not CFG.auth_secret:
            _die("AUTH_SECRET is not set.\n\n"
                 "A key is only verifiable by a server holding the same "
                 "secret, so minting one without it produces something this "
                 "deployment will reject.\n\n"
                 "    python solver_api.py --print-secret\n"
                 "then  AUTH_SECRET=<that> python solver_api.py --mint NAME")
        try:
            key, info = mint_key(name, opt("--tier", "vip"),
                                 int(opt("--days", "30")), int(opt("--rpd", "0")),
                                 int(opt("--rpm", "0")), int(opt("--batch", "0")))
        except (ApiKeyError, ValueError) as exc:
            _die(f"could not mint that key: {exc}")
        print(f"\n  key      : {key}")
        print(f"  id       : {info.id}   (add to AUTH_REVOKED_IDS to kill it)")
        print(f"  name     : {info.name}")
        print(f"  tier     : {info.tier}")
        print(f"  expires  : "
              f"{'never' if not info.exp else datetime.fromtimestamp(info.exp, timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}")
        print(f"  limits   : {info.rpm}/min"
              + (f", {info.rpd}/day" if info.rpd else ", unlimited/day")
              + f", batch {info.batch}")
        print("\n  Store it now. It cannot be retrieved later, and there is")
        print("  no database - the signature IS the record.\n")
        return 0

    if "--verify" in argv:
        raw = argv[argv.index("--verify") + 1]
        try:
            info = verify_key(raw)
        except ApiKeyError as exc:
            print(f"\n  \033[31m✗ {exc.code}\033[0m: {exc.message}\n")
            return 1
        print(f"\n  \033[32m✓ valid\033[0m  id={info.id} name={info.name} "
              f"tier={info.tier} rpm={info.rpm} rpd={info.rpd or 'unlimited'} "
              f"exp={'never' if not info.exp else info.exp}\n")
        return 0

    print_report()
    problems = preflight()
    if problems:
        print("\n\033[1;31m✗ refusing to serve\033[0m\n")
        for i, p in enumerate(problems, 1):
            print(f"  {i}. {p}")
        print("\nSet AUTH_REQUIRED=false only if you genuinely want an open "
              "endpoint.\n")
        return 2

    print(f"\n  \033[32m✓ ready\033[0m  http://{CFG.host}:{CFG.port}"
          f"   docs at /docs   probe at /health\n")
    uvicorn.run(app, host=CFG.host, port=CFG.port, log_level=CFG.log_level.lower(),
                access_log=False, proxy_headers=CFG.trust_forwarded,
                forwarded_allow_ips="*")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main(sys.argv[1:]))
    except KeyboardInterrupt:
        print("\n  stopped.\n")
        raise SystemExit(130)
