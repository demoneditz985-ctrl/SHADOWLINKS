#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
╔══════════════════════════════════════════════════════════════════════════╗
║   S H A D O W L I N K S   ▸   VIP Link Resolver Telegram Bot             ║
║   ------------------------------------------------------------------     ║
║   Single-file build. Paste a shortened link, get the REAL destination    ║
║   back instantly - no ads, no timers, no captcha.                        ║
║                                                                          ║
║   Primary target : gplinks.co / gplinks.in / gplinks.com (deep handler)  ║
║   Plus 70+ other shorteners + a universal auto-detect engine for any     ║
║   domain that is not in the built-in table.                              ║
║                                                                          ║
║   Stack  : Python 3.10+ · aiogram 3 · curl_cffi (Chrome TLS              ║
║            impersonation, beats Cloudflare) · BeautifulSoup4 · sqlite3   ║
║   Run    : python shadowlinks_bot.py                                     ║
║   Config : environment variables or a .env file next to this script      ║
╚══════════════════════════════════════════════════════════════════════════╝

CONTENTS
  §1  Config / .env loader
  §2  Database (sqlite3)
  §3  HTTP client abstraction (curl_cffi primary, aiohttp fallback)
  §4  Shortener registry (70+ domains)
  §5  Resolver engine  (redirect / form / meta / js / adfly / graphql)
  §6  Telegram keyboards & text templates
  §7  Bot handlers - public
  §8  Bot handlers - VIP
  §9  Bot handlers - admin
  §10 Entrypoint
"""

from __future__ import annotations

import asyncio
import base64
import contextlib
import html
import ipaddress
import json
import logging
import os
import random
import re
import secrets
import socket
import sqlite3
import sys
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any, Optional
from urllib.parse import parse_qs, quote, unquote, urljoin, urlparse, urlunparse

from bs4 import BeautifulSoup

from aiogram import BaseMiddleware, Bot, Dispatcher, F
from aiogram.client.default import DefaultBotProperties
from aiogram.enums import ChatAction, ContentType, ParseMode
from aiogram.exceptions import TelegramBadRequest, TelegramForbiddenError, TelegramRetryAfter
from aiogram.filters import Command, CommandObject
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.types import (
    CallbackQuery,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    LinkPreviewOptions,
    Message,
    ReplyKeyboardMarkup,
)

__version__ = "1.0.0"
__app__ = "SHADOWLINKS"

log = logging.getLogger("shadowlinks")


# ══════════════════════════════════════════════════════════════════════════
# §1  CONFIG
# ══════════════════════════════════════════════════════════════════════════

def load_dotenv(path: str = ".env") -> None:
    """Minimal dependency-free .env loader (values already in env win)."""
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
                    val = val[1:-1]          # quoted -> take verbatim
                else:
                    # Strip an inline comment (" # ..."). Without this,
                    # USE_CURL_CFFI=1  # note  parsed as the whole string,
                    # which is not "1", so the browser-TLS backend silently
                    # switched itself off.
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


def _env_list(key: str, default: str = "") -> list[int]:
    """Comma/space separated list of Telegram user ids."""
    raw = _env(key, default)
    out: list[int] = []
    for part in re.split(r"[,\s;]+", raw):
        part = part.strip().lstrip("@")
        if part.isdigit():
            out.append(int(part))
    return out


load_dotenv()


@dataclass(frozen=True)
class Config:
    # --- credentials -----------------------------------------------------
    token: str = _env("BOT_TOKEN")
    admin_ids: tuple[int, ...] = tuple(_env_list("ADMIN_IDS") or _env_list("OWNER_IDS"))

    # --- storage ---------------------------------------------------------
    db_path: str = _env("DB_PATH", "shadowlinks.db")

    # --- resolver --------------------------------------------------------
    max_hops: int = _env_int("MAX_HOPS", 14)
    http_timeout: float = _env_float("HTTP_TIMEOUT", 35.0)
    impersonate: str = _env("IMPERSONATE", "chrome")
    golink_delay: float = _env_float("GOLINK_DELAY", 10.0)      # anti-bot timer wait
    form_retries: int = _env_int("FORM_RETRIES", 2)
    concurrency: int = _env_int("MAX_CONCURRENCY", 8)           # simultaneous resolves
    adaptive_delay: bool = _env_bool("ADAPTIVE_DELAY", True)
    use_curl_cffi: bool = _env_bool("USE_CURL_CFFI", True)
    resolve_chains: bool = _env_bool("RESOLVE_CHAINS", True)

    # --- safety ----------------------------------------------------------
    allow_private_nets: bool = _env_bool("ALLOW_PRIVATE_NETWORKS", False)
    max_body_bytes: int = _env_int("MAX_BODY_BYTES", 2_000_000)

    # --- VIP / quota -----------------------------------------------------
    free_daily_limit: int = _env_int("FREE_DAILY_LIMIT", 5)
    free_cooldown: int = _env_int("FREE_COOLDOWN", 45)          # seconds between reqs
    vip_daily_limit: int = _env_int("VIP_DAILY_LIMIT", 0)       # 0 = unlimited
    vip_batch_limit: int = _env_int("VIP_BATCH_LIMIT", 25)
    free_batch_limit: int = _env_int("FREE_BATCH_LIMIT", 0)     # 0 = batch is VIP-only

    # --- presentation ----------------------------------------------------
    bot_name: str = _env("BOT_NAME", "SHADOWLINKS")
    support_username: str = _env("SUPPORT_USERNAME", "")
    channel_url: str = _env("CHANNEL_URL", "")
    force_channel: bool = _env_bool("FORCE_CHANNEL", False)
    price_line: str = _env(
        "PRICE_LINE",
        "VIP = unlimited resolves, no cooldown, batch mode, full redirect chain."
    )
    show_chain_to_free: bool = _env_bool("SHOW_CHAIN_TO_FREE", False)
    log_resolves: bool = _env_bool("LOG_RESOLVES", True)
    log_level: str = _env("LOG_LEVEL", "INFO")

    # --- user-extensible registry ----------------------------------------
    # Comma separated hostnames merged into the built-in table at boot, so new
    # shorteners can be added without touching the code.
    extra_gplinks: tuple[str, ...] = tuple(
        _s for _s in re.split(r"[,\s]+", _env("EXTRA_GPLINKS_DOMAINS")) if _s)
    extra_form: tuple[str, ...] = tuple(
        _s for _s in re.split(r"[,\s]+", _env("EXTRA_FORM_DOMAINS")) if _s)
    extra_redirect: tuple[str, ...] = tuple(
        _s for _s in re.split(r"[,\s]+", _env("EXTRA_REDIRECT_DOMAINS")) if _s)
    extra_adfly: tuple[str, ...] = tuple(
        _s for _s in re.split(r"[,\s]+", _env("EXTRA_ADFLY_DOMAINS")) if _s)


CFG = Config()

# Network ranges a public bot must never be pointed at (SSRF protection).
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
# §2  DATABASE
# ══════════════════════════════════════════════════════════════════════════

PERMANENT = 4102444800  # 2100-01-01T00:00:00Z -> "never expires"

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    user_id     INTEGER PRIMARY KEY,
    username    TEXT    DEFAULT '',
    first_name  TEXT    DEFAULT '',
    chat_id     INTEGER DEFAULT 0,
    is_admin    INTEGER DEFAULT 0,
    is_banned   INTEGER DEFAULT 0,
    vip_until   INTEGER DEFAULT 0,
    vip_source  TEXT    DEFAULT '',
    quota_used  INTEGER DEFAULT 0,
    quota_day   TEXT    DEFAULT '',
    last_req_at INTEGER DEFAULT 0,
    total_reqs  INTEGER DEFAULT 0,
    created_at  INTEGER DEFAULT 0,
    last_seen   INTEGER DEFAULT 0,
    lang        TEXT    DEFAULT 'en'
);
CREATE TABLE IF NOT EXISTS codes (
    code       TEXT    PRIMARY KEY,
    days       INTEGER DEFAULT 7,
    note       TEXT    DEFAULT '',
    created_by INTEGER DEFAULT 0,
    created_at INTEGER DEFAULT 0,
    used_by    INTEGER DEFAULT 0,
    used_at    INTEGER DEFAULT 0,
    revoked    INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS logs (
    id        INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id   INTEGER DEFAULT 0,
    input_url TEXT    DEFAULT '',
    final_url TEXT    DEFAULT '',
    domain    TEXT    DEFAULT '',
    handler   TEXT    DEFAULT '',
    ok        INTEGER DEFAULT 0,
    error     TEXT    DEFAULT '',
    ms        INTEGER DEFAULT 0,
    ts        INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS domain_stats (
    domain TEXT PRIMARY KEY,
    hits   INTEGER DEFAULT 0,
    ok     INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS kv (
    k TEXT PRIMARY KEY,
    v TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_logs_ts     ON logs(ts);
CREATE INDEX IF NOT EXISTS idx_logs_user   ON logs(user_id);
CREATE INDEX IF NOT EXISTS idx_users_vip   ON users(vip_until);
"""


@dataclass
class User:
    user_id: int
    username: str = ""
    first_name: str = ""
    chat_id: int = 0
    is_admin: bool = False
    is_banned: bool = False
    vip_until: int = 0
    vip_source: str = ""
    quota_used: int = 0
    quota_day: str = ""
    last_req_at: int = 0
    total_reqs: int = 0
    created_at: int = 0
    last_seen: int = 0

    @property
    def is_vip(self) -> bool:
        return self.vip_until > int(time.time())

    def vip_remaining(self) -> timedelta:
        if not self.is_vip:
            return timedelta(0)
        return timedelta(seconds=self.vip_until - int(time.time()))

    def vip_label(self) -> str:
        if not self.is_vip:
            return "—"
        if self.vip_until >= PERMANENT:
            return "permanent"
        d = self.vip_remaining()
        if d.days >= 1:
            return f"{d.days}d {d.seconds // 3600}h left"
        return f"{(d.seconds // 60)}m left"


class Database:
    """Tiny synchronous SQLite wrapper.

    Every statement is sub-millisecond on a local file, so it is executed
    inline under a re-entrant lock rather than pushed to a thread pool.
    """

    def __init__(self, path: str) -> None:
        self.path = path
        self._lock = threading.RLock()
        d = os.path.dirname(os.path.abspath(path))
        if d:
            os.makedirs(d, exist_ok=True)
        self._conn = sqlite3.connect(path, check_same_thread=False, timeout=30)
        self._conn.row_factory = sqlite3.Row
        with self._lock, self._conn:
            self._conn.executescript(SCHEMA)
            self._conn.execute("PRAGMA journal_mode=WAL")
            self._conn.execute("PRAGMA synchronous=NORMAL")

    # -- low level --------------------------------------------------------
    def q(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        with self._lock:
            return list(self._conn.execute(sql, params).fetchall())

    def x(self, sql: str, params: tuple = ()) -> int:
        with self._lock, self._conn:
            cur = self._conn.execute(sql, params)
            return cur.lastrowid or cur.rowcount

    def one(self, sql: str, params: tuple = (), default: Any = None) -> Any:
        rows = self.q(sql, params)
        return rows[0][0] if rows else default

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    # -- users ------------------------------------------------------------
    @staticmethod
    def _today() -> str:
        return datetime.now(timezone.utc).strftime("%Y-%m-%d")

    def upsert_user(self, user_id: int, username: str = "", first_name: str = "",
                    chat_id: int = 0) -> User:
        now = int(time.time())
        row = self.q("SELECT * FROM users WHERE user_id=?", (user_id,))
        if row:
            self.x(
                "UPDATE users SET username=?, first_name=?, chat_id=?, last_seen=? "
                "WHERE user_id=?",
                (username or "", first_name or "", chat_id or user_id, now, user_id),
            )
        else:
            self.x(
                "INSERT INTO users (user_id, username, first_name, chat_id, is_admin,"
                " vip_until, created_at, last_seen) VALUES (?,?,?,?,?,?,?,?)",
                (user_id, username or "", first_name or "", chat_id or user_id,
                 1 if user_id in CFG.admin_ids else 0,
                 PERMANENT if user_id in CFG.admin_ids else 0,
                 now, now),
            )
            if user_id in CFG.admin_ids:
                self.x("UPDATE users SET is_admin=1, vip_until=? WHERE user_id=?",
                       (PERMANENT, user_id))
        return self.get_user(user_id)

    def get_user(self, user_id: int) -> User:
        row = self.q("SELECT * FROM users WHERE user_id=?", (user_id,))
        if not row:
            return self.upsert_user(user_id)
        r = row[0]
        u = User(
            user_id=r["user_id"], username=r["username"] or "",
            first_name=r["first_name"] or "", chat_id=r["chat_id"] or r["user_id"],
            is_admin=bool(r["is_admin"]), is_banned=bool(r["is_banned"]),
            vip_until=r["vip_until"] or 0, vip_source=r["vip_source"] or "",
            quota_used=r["quota_used"] or 0, quota_day=r["quota_day"] or "",
            last_req_at=r["last_req_at"] or 0, total_reqs=r["total_reqs"] or 0,
            created_at=r["created_at"] or 0, last_seen=r["last_seen"] or 0,
        )
        if u.is_admin:  # admins are always VIP
            u.vip_until = max(u.vip_until, PERMANENT)
        return u

    def set_vip(self, user_id: int, days: int, source: str = "") -> User:
        self.upsert_user(user_id)
        if days <= 0:
            until = PERMANENT
        else:
            cur = self.one("SELECT vip_until FROM users WHERE user_id=?",
                           (user_id,), 0) or 0
            if cur >= PERMANENT:
                until = PERMANENT           # never downgrade a permanent grant
            else:
                until = max(cur, int(time.time())) + days * 86400
        self.x("UPDATE users SET vip_until=?, vip_source=? WHERE user_id=?",
               (until, source, user_id))
        return self.get_user(user_id)

    def revoke_vip(self, user_id: int) -> None:
        self.x("UPDATE users SET vip_until=0, vip_source='' WHERE user_id=? "
               "AND is_admin=0", (user_id,))

    def set_ban(self, user_id: int, banned: bool) -> None:
        self.upsert_user(user_id)
        self.x("UPDATE users SET is_banned=? WHERE user_id=?", (1 if banned else 0, user_id))

    def set_admin(self, user_id: int, admin: bool) -> None:
        self.upsert_user(user_id)
        self.x("UPDATE users SET is_admin=?, vip_until=? WHERE user_id=?",
               (1 if admin else 0, PERMANENT if admin else 0, user_id))

    def find_by_username(self, username: str) -> Optional[User]:
        username = username.lstrip("@").lower()
        row = self.q("SELECT user_id FROM users WHERE LOWER(username)=?", (username,))
        return self.get_user(row[0]["user_id"]) if row else None

    # -- quota ------------------------------------------------------------
    def consume_quota(self, user_id: int) -> bool:
        """Roll the daily counter and charge one unit. Returns True if allowed."""
        u = self.get_user(user_id)
        today = self._today()
        if u.quota_day != today:
            self.x("UPDATE users SET quota_used=0, quota_day=? WHERE user_id=?",
                   (today, user_id))
            u.quota_used = 0
        self.x("UPDATE users SET quota_used=quota_used+1, last_req_at=?, "
               "total_reqs=total_reqs+1 WHERE user_id=?", (int(time.time()), user_id))
        return True

    def peek_quota(self, user_id: int) -> tuple[int, int]:
        """(used_today, limit) - limit 0 means unlimited."""
        u = self.get_user(user_id)
        today = self._today()
        used = u.quota_used if u.quota_day == today else 0
        if u.is_admin:
            return used, 0
        if u.is_vip:
            return used, CFG.vip_daily_limit
        return used, CFG.free_daily_limit

    def cooldown_left(self, user_id: int) -> int:
        u = self.get_user(user_id)
        if u.is_vip or u.is_admin:
            return 0
        left = CFG.free_cooldown - (int(time.time()) - (u.last_req_at or 0))
        return max(0, left)

    # -- codes ------------------------------------------------------------
    def make_code(self, days: int, admin_id: int, note: str = "") -> str:
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZ23456789"  # no I/O/0/1 confusion
        while True:
            body = "".join(secrets.choice(alphabet) for _ in range(6))
            code = f"SHADOW-{body}"
            if not self.q("SELECT 1 FROM codes WHERE code=?", (code,)):
                break
        self.x("INSERT INTO codes (code, days, note, created_by, created_at) "
               "VALUES (?,?,?,?,?)", (code, days, note, admin_id, int(time.time())))
        return code

    def redeem_code(self, code: str, user_id: int) -> tuple[bool, str, int]:
        code = code.strip().upper()
        row = self.q("SELECT * FROM codes WHERE code=?", (code,))
        if not row:
            return False, "That code does not exist. Check for typos.", 0
        c = row[0]
        if c["revoked"]:
            return False, "That code was revoked by an admin.", 0
        if c["used_by"]:
            if c["used_by"] == user_id:
                return False, "You already used this code.", c["days"]
            return False, "That code has already been redeemed.", c["days"]
        self.x("UPDATE codes SET used_by=?, used_at=? WHERE code=?",
               (user_id, int(time.time()), code))
        self.set_vip(user_id, c["days"], source=f"code:{code}")
        return True, "VIP activated!", c["days"]

    def revoke_code(self, code: str) -> bool:
        code = code.strip().upper()
        return self.x("UPDATE codes SET revoked=1 WHERE code=?", (code,)) > 0

    def list_codes(self, only_active: bool = True, limit: int = 50) -> list[sqlite3.Row]:
        sql = "SELECT * FROM codes"
        if only_active:
            sql += " WHERE revoked=0 AND used_by=0"
        sql += " ORDER BY created_at DESC LIMIT ?"
        return self.q(sql, (limit,))

    # -- logging / stats --------------------------------------------------
    def log_resolve(self, user_id: int, inp: str, out: str, domain: str,
                    handler: str, ok: bool, error: str, ms: int) -> None:
        if not CFG.log_resolves:
            return
        self.x("INSERT INTO logs (user_id,input_url,final_url,domain,handler,ok,"
               "error,ms,ts) VALUES (?,?,?,?,?,?,?,?,?)",
               (user_id, inp[:2000], (out or "")[:2000], domain, handler,
                1 if ok else 0, (error or "")[:500], ms, int(time.time())))
        self.x("INSERT INTO domain_stats (domain,hits,ok) VALUES (?,?,?) "
               "ON CONFLICT(domain) DO UPDATE SET hits=hits+1, ok=ok+?",
               (domain, 1, 1 if ok else 0, 1 if ok else 0))

    def trim_logs(self, keep_days: int = 14) -> int:
        cutoff = int(time.time()) - keep_days * 86400
        return self.x("DELETE FROM logs WHERE ts < ?", (cutoff,))

    def stats(self) -> dict[str, Any]:
        return {
            "users": self.one("SELECT COUNT(*) FROM users", (), 0),
            "vip": self.one("SELECT COUNT(*) FROM users WHERE vip_until > ?",
                            (int(time.time()),), 0),
            "banned": self.one("SELECT COUNT(*) FROM users WHERE is_banned=1", (), 0),
            "resolves": self.one("SELECT COUNT(*) FROM logs", (), 0),
            "success": self.one("SELECT COUNT(*) FROM logs WHERE ok=1", (), 0),
            "codes_live": self.one("SELECT COUNT(*) FROM codes WHERE revoked=0 "
                                   "AND used_by=0", (), 0),
            "codes_used": self.one("SELECT COUNT(*) FROM codes WHERE used_by>0", (), 0),
            "today": self.one("SELECT COUNT(*) FROM logs WHERE ts > ?",
                              (int(time.time()) - 86400,), 0),
            "avg_ms": self.one("SELECT CAST(AVG(ms) AS INTEGER) FROM logs WHERE ok=1",
                               (), 0) or 0,
        }

    def top_domains(self, limit: int = 10) -> list[sqlite3.Row]:
        return self.q("SELECT domain, hits, ok FROM domain_stats "
                      "ORDER BY hits DESC LIMIT ?", (limit,))

    def list_vip(self, limit: int = 100) -> list[sqlite3.Row]:
        return self.q("SELECT user_id, username, first_name, vip_until, vip_source "
                      "FROM users WHERE vip_until > ? ORDER BY vip_until DESC LIMIT ?",
                      (int(time.time()), limit))

    def all_user_ids(self) -> list[int]:
        return [r["user_id"] for r in self.q("SELECT user_id FROM users")]

    # -- adaptive anti-bot delay -----------------------------------------
    def get_delay(self, domain: str) -> float:
        if not CFG.adaptive_delay:
            return CFG.golink_delay
        raw = self.one("SELECT v FROM kv WHERE k=?", (f"delay:{domain}",), "")
        try:
            return float(raw) if raw else 0.0
        except ValueError:
            return 0.0

    def set_delay(self, domain: str, seconds: float) -> None:
        if not CFG.adaptive_delay:
            return
        self.x("INSERT INTO kv (k,v) VALUES (?,?) "
               "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
               (f"delay:{domain}", f"{seconds:.1f}"))

    def get_kv(self, key: str, default: str = "") -> str:
        return self.one("SELECT v FROM kv WHERE k=?", (key,), default) or default

    def set_kv(self, key: str, value: str) -> None:
        self.x("INSERT INTO kv (k,v) VALUES (?,?) "
               "ON CONFLICT(k) DO UPDATE SET v=excluded.v", (key, value))


DB = Database(CFG.db_path)


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
    Falls back to aiohttp if curl_cffi is unavailable or errors out.
    """

    def __init__(self) -> None:
        self._cffi_ok = CFG.use_curl_cffi
        self._aiohttp = None
        if self._cffi_ok:
            try:
                import curl_cffi.requests
                if not callable(curl_cffi.requests.AsyncSession):
                    raise RuntimeError("curl_cffi.requests.AsyncSession missing")
            except Exception as exc:  # pragma: no cover
                log.warning("curl_cffi unavailable (%s) - falling back to aiohttp", exc)
                self._cffi_ok = False

    # -- session ----------------------------------------------------------
    @contextlib.asynccontextmanager
    async def session(self):
        if self._cffi_ok:
            from curl_cffi.requests import AsyncSession
            async with AsyncSession(impersonate=CFG.impersonate) as s:
                yield _CffiSession(s)
                return
        if self._aiohttp is None or self._aiohttp.closed:
            import aiohttp
            self._aiohttp = aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=CFG.http_timeout),
                connector=aiohttp.TCPConnector(limit=CFG.concurrency * 2, ssl=False),
            )
        yield _AiohttpSession(self._aiohttp)

    async def close(self) -> None:
        if self._aiohttp is not None and not self._aiohttp.closed:
            await self._aiohttp.close()


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


class _AiohttpSession:
    """Fallback path - plain aiohttp, no TLS impersonation."""

    def __init__(self, s) -> None:
        self.s = s

    async def request(self, method: str, url: str, *, headers: dict | None = None,
                      data: dict | None = None, allow_redirects: bool = False,
                      referer: str | None = None, cookies: dict | None = None,
                      timeout: float | None = None,
                      params: dict | None = None) -> Response:
        import aiohttp
        parsed = urlparse(url)
        await guard_host(parsed.hostname or "")
        h = {"user-agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                            "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"),
             **BROWSER_HEADERS, **(headers or {})}
        if referer:
            h["referer"] = referer
            h["origin"] = f"{parsed.scheme}://{parsed.netloc}"
        try:
            async with self.s.request(
                method, url, headers=h, data=data, cookies=cookies, params=params,
                allow_redirects=allow_redirects,
                max_redirects=CFG.max_hops if allow_redirects else None,
                timeout=aiohttp.ClientTimeout(total=timeout or CFG.http_timeout),
                ssl=False,
            ) as r:
                raw = await r.read()
                body = raw[: CFG.max_body_bytes].decode("utf-8", "replace")
                jar = {c.key: c.value for c in r.cookies.values()}
                return Response(
                    url=url, status=r.status,
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


def plausible_destination(url: str, page_url: str) -> bool:
    """Filter out self-links, ads and the shortener's own assets."""
    if not looks_like_destination(url):
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
        delay = prof.delay if prof.delay is not None else DB.get_delay(domain)

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
                    DB.set_delay(domain, delay)
                return nxt, kind, note
            last_note = note or last_note

            # learn: if the server complained about a timer, escalate the wait
            if prof.delay is None and CFG.adaptive_delay:
                if any(h in (note or "").lower() for h in TIMER_HINTS) or attempt == 0:
                    delay = CFG.golink_delay
                    DB.set_delay(domain, delay)
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
        delay = prof.delay if prof.delay is not None else DB.get_delay(domain)
        attempts = max(1, CFG.form_retries + 1)
        last_note = ""
        for attempt in range(attempts):
            if delay > 0:
                await bot_sleep(delay)
            nxt, kind, note = await self._post_form(
                s, page_url, text, prof, out, note_prefix=f"try {attempt + 1}")
            if nxt:
                if prof.delay is None and CFG.adaptive_delay:
                    DB.set_delay(domain, delay)
                return nxt, kind, note
            last_note = note or last_note
            if prof.delay is None and CFG.adaptive_delay:
                delay = CFG.golink_delay
                DB.set_delay(domain, delay)
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

        # 2a) GPLinks-style "Go Ad-Free" paywall -> take the skip_sub door
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
        return None, "", "no destination found on page"


RESOLVER = LinkResolver()


# ══════════════════════════════════════════════════════════════════════════
# §6  TELEGRAM PRESENTATION
# ══════════════════════════════════════════════════════════════════════════

def esc(text: Any) -> str:
    return html.escape(str(text or ""), quote=False)


class ResultCache:
    """Short-lived store so inline buttons can re-open a finished resolve."""

    def __init__(self, maxsize: int = 400, ttl: int = 3600) -> None:
        self._d: dict[str, tuple[float, ResolveOutcome, str]] = {}
        self.maxsize = maxsize
        self.ttl = ttl

    def put(self, outcome: ResolveOutcome, input_url: str) -> str:
        key = secrets.token_urlsafe(9)
        now = time.time()
        for k in [k for k, (ts, _, _) in self._d.items() if now - ts > self.ttl]:
            self._d.pop(k, None)
        while len(self._d) >= self.maxsize:
            self._d.pop(next(iter(self._d)), None)
        self._d[key] = (now, outcome, input_url)
        return key

    def get(self, key: str) -> Optional[tuple[ResolveOutcome, str]]:
        hit = self._d.get(key)
        if not hit:
            return None
        ts, outcome, input_url = hit
        if time.time() - ts > self.ttl:
            self._d.pop(key, None)
            return None
        return outcome, input_url


CACHE = ResultCache()


def main_keyboard() -> ReplyKeyboardMarkup:
    return ReplyKeyboardMarkup(
        keyboard=[
            [KeyboardButton(text="🔗 Resolve"), KeyboardButton(text="💎 My VIP")],
            [KeyboardButton(text="📜 Sites"), KeyboardButton(text="ℹ️ Help")],
        ],
        resize_keyboard=True, input_field_placeholder="Paste a shortened link…",
    )


def vip_button_row(user: User) -> list[InlineKeyboardButton]:
    if user.is_vip:
        return [InlineKeyboardButton(text="👑 VIP active", callback_data="noop:vip")]
    return [InlineKeyboardButton(text="💎 Get VIP", callback_data="upsell")]


def kb_menu() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(inline_keyboard=[[
        InlineKeyboardButton(text="📜 Sites", callback_data="menu:sites"),
        InlineKeyboardButton(text="💎 VIP", callback_data="upsell"),
        InlineKeyboardButton(text="ℹ️ Help", callback_data="menu:help"),
    ]])


def kb_result(user: User, key: str, ok: bool) -> InlineKeyboardMarkup:
    rows: list[list[InlineKeyboardButton]] = []
    if ok:
        rows.append([
            InlineKeyboardButton(text="📋 Copy link", callback_data=f"copy:{key}"),
            InlineKeyboardButton(text="🧭 Chain", callback_data=f"chain:{key}"),
        ])
    rows.append([InlineKeyboardButton(text="🔁 Resolve again", callback_data=f"again:{key}")])
    rows.append(vip_button_row(user))
    return InlineKeyboardMarkup(inline_keyboard=rows)


TXT_WELCOME = """🕶 <b>{name}</b> — VIP Link Resolver

I turn shortened, ad-gated links into their <b>real destination URL</b>.
No ads, no timers, no captcha, no browser.

<b>How to use</b>
Just paste the link into this chat. That is the whole interface.
<pre>https://gplinks.co/BFfOX</pre>

<b>Commands</b>
/start — this menu
/status — your plan, quota and cooldown
/sites — every shortener I know
/redeem &lt;CODE&gt; — activate a VIP code
/batch — unlock many links at once (VIP)
/help — details and troubleshooting

{price}

{channel}{support}"""

TXT_HELP = """ℹ️ <b>Help &amp; troubleshooting</b>

<b>1. Just paste the link.</b> No command needed. You can paste up to
{batch_max} links in one message if you are VIP.

<b>2. What I do with it</b>
• follow every HTTP redirect in the chain
• defeat the GPLinks “Go Ad-Free” paywall (<code>skip_sub=1</code>)
• read the <code>vid</code>/<code>lid</code>/<code>pid</code>/<code>plid</code> visitor cookies
• ping <code>update-visitor-value</code> so the ad gate reports all steps done
• decode <code>powergam</code>-style base64url ad gates straight to gplinks.co
• submit the hidden <code>&lt;form id="go-link"&gt;</code> with the Ajax header
• learn each site's anti-bot timer and wait exactly as long as needed

<b>3. If a link fails</b>
• Some shorteners rotate their interstitial domain. Tell me the domain and
  an admin can add it with <code>EXTRA_FORM_DOMAINS</code> — no code change.
• A few sites (Linkvertise) mint bypass tokens only inside a real browser and
  cannot be solved headlessly. I will say so instead of guessing.
• Cloudflare hard challenges occasionally reject datacentre IPs. Retrying
  later usually works.

<b>4. Safety</b>
Private, loopback and reserved IP ranges are refused, so nobody can use me to
probe an internal network.

<b>5. Your quota</b>
{quota_line}"""

TXT_PLANS = """💎 <b>VIP</b>

{price}

<b>VIP gets</b>
• ♾ Unlimited resolves (free = {free_limit}/day)
• ⚡ No cooldown (free = {cooldown}s between links)
• 📦 Batch mode — up to {batch} links per message
• 🧭 Full redirect chain on every result
• 🚀 Priority in the worker queue

<b>Already have a code?</b>
<pre>/redeem SHADOW-XXXXXX</pre>

<b>Need one?</b> Contact {support}"""

TXT_STATUS = """📊 <b>Your account</b>

👤 Name    : {name}
🆔 ID      : <code>{uid}</code>
🔖 Plan    : {plan}
⏳ Expires : {expires}
🎟 Source  : {source}

📈 Today   : {used}{limit}
{cooldown_line}🧮 Lifetime: {total} resolves
📅 Member  : since {joined}"""

TXT_SITES = """📜 <b>{count} shorteners I handle natively</b>

{groups}

<b>Plus</b> — any domain not listed here still gets the universal
auto-detect engine: redirect chains, meta-refresh, JS redirects, hidden
POST forms, base64 tokens and adf.ly-style <code>ysmm</code> decoding.

Admins can extend the table without touching the code:
<code>EXTRA_GPLINKS_DOMAINS</code>, <code>EXTRA_FORM_DOMAINS</code>,
<code>EXTRA_REDIRECT_DOMAINS</code>, <code>EXTRA_ADFLY_DOMAINS</code>."""


def txt_ok(r: ResolveOutcome, user: User, key: str) -> str:
    lines = [
        "🎯 <b>Destination unlocked</b>",
        "",
        f"<code>{esc(r.final_url)}</code>",
        "",
        f"🔗 Shortener : <b>{esc(r.shortener)}</b>",
        f"⚙️ Strategy  : <code>{esc(r.handler)}</code>",
        f"🪜 Hops      : {r.depth}",
        f"⏱ Resolved  : {r.elapsed_ms / 1000:.2f}s",
    ]
    if user.is_vip and r.hops:
        lines += ["", "🧭 <b>Chain</b>"]
        for i, h in enumerate(r.hops, 1):
            note = f" — {esc(h.note)}" if h.note else ""
            lines.append(f"{i}. <code>{esc(h.kind)}</code>{note}")
        lines.append(f"{len(r.hops) + 1}. <b>final</b>")
    elif not user.is_vip and r.hops and CFG.show_chain_to_free:
        lines.append(f"\n🪜 {r.depth} hop(s) — <i>VIP sees the full chain</i>")
    lines += ["", f"🕶 <i>{esc(CFG.bot_name)}</i> · "
                  f"{'👑 VIP' if user.is_vip else '🆓 free'} · /status"]
    return "\n".join(lines)


def txt_fail(r: ResolveOutcome, user: User) -> str:
    lines = ["❌ <b>Could not unlock that link</b>", ""]
    if r.error:
        lines.append(f"<i>{esc(r.error)}</i>")
    lines += ["", f"🔗 Input     : <code>{esc(r.input_url[:300])}</code>",
              f"⚙️ Strategy  : <code>{esc(r.handler or 'auto')}</code>",
              f"🪜 Hops tried: {r.depth}"]
    if r.final_url and r.final_url != r.input_url:
        lines += ["", "🔎 <b>Furthest I got</b>", f"<code>{esc(r.final_url)}</code>"]
    if r.candidates:
        lines += ["", "🧪 <b>Possible destinations</b> (unverified)"]
        for c in r.candidates[:3]:
            lines.append(f"• <code>{esc(c)}</code>")
    lines += ["", "💡 <i>Some shorteners rotate their ad-gate domain. Send the "
                  "domain to an admin and it can be added instantly.</i>"]
    return "\n".join(lines)


def fmt_batch(results: list[ResolveOutcome], user: User) -> str:
    good = sum(1 for r in results if r.ok)
    lines = [f"📦 <b>Batch complete</b> — {good}/{len(results)} unlocked", ""]
    for i, r in enumerate(results, 1):
        if r.ok:
            lines.append(f"<b>{i}.</b> <code>{esc(r.final_url)}</code>")
        else:
            lines.append(f"<b>{i}.</b> ❌ <code>{esc(r.input_url[:80])}</code>")
            if r.error:
                lines.append(f"     <i>{esc(r.error[:120])}</i>")
    return "\n".join(lines)


def group_sites() -> str:
    buckets: dict[str, list[str]] = {}
    for host, prof in sorted(REGISTRY.items()):
        buckets.setdefault(prof.label or prof.handler, []).append(host)
    order = ["GPLinks", "GPLinks (custom)", "Form shortener", "Redirect",
             "adf.ly/ysmm", "ouo.io", "Linkvertise", "aylink/cpmlink",
             "JS shortener", "Form (custom)", "Redirect (custom)", "adf.ly (custom)"]
    keys = [k for k in order if k in buckets] + \
           [k for k in sorted(buckets) if k not in order]
    icon = {"GPLinks": "🎯", "Form shortener": "📝", "Redirect": "↪️",
            "adf.ly/ysmm": "🔐", "ouo.io": "🔁", "Linkvertise": "🧩",
            "aylink/cpmlink": "🪙", "JS shortener": "📜"}
    out = []
    for k in keys:
        hosts = buckets[k]
        mark = icon.get(k, "🔹") if not k.endswith("(custom)") else "➕"
        shown = ", ".join(hosts[:22])
        if len(hosts) > 22:
            shown += f" … (+{len(hosts) - 22} more)"
        out.append(f"{mark} <b>{esc(k)}</b> ({len(hosts)})\n<code>{esc(shown)}</code>")
    return "\n\n".join(out)


# ══════════════════════════════════════════════════════════════════════════
# §7  BOT PLUMBING
# ══════════════════════════════════════════════════════════════════════════

class BotStates(StatesGroup):
    broadcasting = State()
    batch = State()
    awaiting_user = State()


class UserMiddleware(BaseMiddleware):
    """Attach the DB user record to every event, and enforce the ban list.

    Registered on the message and callback_query observers (not on `update`),
    because an Update object carries no `from_user` - only its inner event does.
    """

    async def __call__(self, handler, event, data):
        tg_user = getattr(event, "from_user", None)
        if tg_user is None:                      # channel post, chat member, …
            return await handler(event, data)

        src = event.message if isinstance(event, CallbackQuery) else event
        chat = getattr(src, "chat", None)
        chat_id = getattr(chat, "id", None) or tg_user.id

        user = DB.upsert_user(tg_user.id, tg_user.username or "",
                              tg_user.first_name or "", chat_id)
        data["user"] = user

        if user.is_banned:
            if isinstance(event, CallbackQuery):
                await event.answer("🚫 Blocked.", show_alert=True)
            else:
                with contextlib.suppress(Exception):
                    await event.answer("🚫 You are blocked from using this bot.")
            return None
        return await handler(event, data)


def quota_line(user: User) -> str:
    used, limit = DB.peek_quota(user.user_id)
    return f"{used}/{limit}" if limit else f"{used}/∞"


async def send(msg: Message, text: str, **kw) -> Optional[Message]:
    try:
        return await msg.answer(text, **kw)
    except TelegramRetryAfter as e:
        await asyncio.sleep(e.retry_after + 0.4)
        return await msg.answer(text, **kw)
    except TelegramBadRequest:
        kw.pop("reply_markup", None)
        kw.pop("parse_mode", None)
        return await msg.answer(text, **kw)


async def guard_quota(message: Message, user: User) -> bool:
    """Return True when the user may spend another resolve."""
    used, limit = DB.peek_quota(user.user_id)
    if limit and used >= limit:
        wait = 0
        now = datetime.now(timezone.utc)
        nxt = (now + timedelta(days=1)).replace(hour=0, minute=0, second=0,
                                                microsecond=0)
        wait = int((nxt - now).total_seconds())
        await send(message,
                   "⛔ <b>Daily limit reached</b>\n\n"
                   f"You used all <b>{limit}</b> free resolves today.\n"
                   f"Resets in <b>{wait // 3600}h {(wait % 3600) // 60}m</b>.\n\n"
                   "💎 VIP removes the limit entirely.\n/redeem &lt;CODE&gt; if you "
                   "have one, or /plans for details.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[vip_button_row(user)]))
        return False
    left = DB.cooldown_left(user.user_id)
    if left > 0:
        await send(message, f"⏳ <b>Slow down</b> — try again in <b>{left}s</b>.\n"
                            f"💎 VIP has no cooldown. /plans")
        return False
    return True


async def do_resolve(message: Message, user: User, url: str,
                     quiet: bool = False) -> Optional[ResolveOutcome]:
    """Run one resolve with typing indicator + a progress message."""
    if not await guard_quota(message, user):
        return None
    DB.consume_quota(user.user_id)

    progress: Optional[Message] = None
    prof = profile_for(url)
    if not quiet:
        with contextlib.suppress(Exception):
            await message.bot.send_chat_action(message.chat.id, ChatAction.TYPING)
        with contextlib.suppress(Exception):
            progress = await message.answer(
                f"⏳ <b>Unlocking</b> <code>{esc(host_of(url))}</code>\n"
                f"⚙️ strategy <code>{esc(prof.handler)}</code> — walking the redirect "
                f"chain, clearing the ad gate…\n<i>This can take ~15s on sites with "
                f"an enforced timer.</i>")

    hard_timeout = CFG.http_timeout * 2 + CFG.golink_delay * (CFG.form_retries + 1) + 25
    try:
        outcome = await asyncio.wait_for(RESOLVER.resolve(url), timeout=hard_timeout)
    except asyncio.TimeoutError:
        outcome = ResolveOutcome(input_url=url, handler=prof.handler,
                                 shortener=host_of(url),
                                 error=f"Timed out after {int(hard_timeout)}s.")
    except Exception as exc:
        log.exception("resolve crashed for %s", url)
        outcome = ResolveOutcome(input_url=url, handler=prof.handler,
                                 shortener=host_of(url), error=str(exc))

    DB.log_resolve(user.user_id, url, outcome.final_url if outcome.ok else "",
                   outcome.shortener or host_of(url), outcome.handler,
                   outcome.ok, outcome.error, outcome.elapsed_ms)

    key = CACHE.put(outcome, url)
    if outcome.ok:
        text = txt_ok(outcome, user, key)
        markup = kb_result(user, key, True)
    else:
        text = txt_fail(outcome, user)
        markup = kb_result(user, key, False)

    if progress is not None:
        try:
            await progress.edit_text(text, reply_markup=markup)
            return outcome
        except Exception:
            with contextlib.suppress(Exception):
                await progress.delete()
    await send(message, text, reply_markup=markup)
    return outcome


# ── public commands ────────────────────────────────────────────────────────

async def cmd_start(message: Message, user: User, command: CommandObject) -> None:
    arg = (command.args or "").strip() if command else ""
    if arg and re.match(r"^[A-Za-z0-9_-]{4,}$", arg):
        ok, note, days = DB.redeem_code(arg, user.user_id)
        user = DB.get_user(user.user_id)
        prefix = ("🎉 <b>Code accepted</b> — " if ok else "⚠️ <b>Code rejected</b> — ")
        await send(message, prefix + esc(note) +
                   (f"\n💎 VIP for <b>{days} day(s)</b>." if ok else ""))
        if ok:
            return
    channel_line = f"📣 Channel : {CFG.channel_url}\n" if CFG.channel_url else ""
    support_line = (f"🆘 Support : @{esc(CFG.support_username)}\n"
                    if CFG.support_username else "")
    await send(message, TXT_WELCOME.format(
        name=esc(CFG.bot_name), price=esc(CFG.price_line),
        channel=channel_line, support=support_line),
        reply_markup=main_keyboard())


async def cmd_help(message: Message, user: User) -> None:
    used, limit = DB.peek_quota(user.user_id)
    ql = (f"Plan <b>{'VIP 👑' if user.is_vip else 'Free 🆓'}</b> · "
          f"{'unlimited' if not limit else f'{used}/{limit} used today'}")
    await send(message, TXT_HELP.format(batch_max=CFG.vip_batch_limit, quota_line=ql),
               reply_markup=kb_menu())


async def cmd_plans(message: Message, user: User) -> None:
    await send(message, TXT_PLANS.format(
        price=esc(CFG.price_line), free_limit=CFG.free_daily_limit,
        cooldown=CFG.free_cooldown, batch=CFG.vip_batch_limit,
        support=(f"@{esc(CFG.support_username)}" if CFG.support_username
                 else "the bot owner")),
        reply_markup=InlineKeyboardMarkup(inline_keyboard=[vip_button_row(user)]))


async def cmd_status(message: Message, user: User) -> None:
    used, limit = DB.peek_quota(user.user_id)
    cd = DB.cooldown_left(user.user_id)
    await send(message, TXT_STATUS.format(
        name=esc(user.first_name or user.username or "user"),
        uid=user.user_id,
        plan="👑 <b>VIP</b>" if user.is_vip else "🆓 <b>Free</b>",
        expires=esc(user.vip_label()),
        source=esc(user.vip_source or "—"),
        used=used, limit=(f"/{limit}" if limit else "/∞"),
        cooldown_line=(f"⏳ Cooldown : {cd}s remaining\n" if cd else ""),
        total=user.total_reqs,
        joined=datetime.fromtimestamp(user.created_at or int(time.time()),
                                      timezone.utc).strftime("%d %b %Y"),
    ), reply_markup=InlineKeyboardMarkup(inline_keyboard=[vip_button_row(user)]))


async def cmd_sites(message: Message, user: User) -> None:
    await send(message, TXT_SITES.format(count=len(REGISTRY), groups=group_sites()))


async def cmd_redeem(message: Message, user: User, command: CommandObject) -> None:
    code = (command.args or "").strip() if command else ""
    if not code:
        await send(message, "🎟 Send it as <pre>/redeem SHADOW-XXXXXX</pre>\n"
                            "Or ask the owner for a code.")
        return
    ok, note, days = DB.redeem_code(code, user.user_id)
    body = ("🎉 <b>VIP activated</b>\n\n" if ok else "⚠️ <b>Code rejected</b>\n\n")
    body += esc(note)
    if ok:
        body += f"\n💎 <b>{days}</b> day(s) added. Unlimited resolves, no cooldown."
    await send(message, body,
               reply_markup=InlineKeyboardMarkup(inline_keyboard=[vip_button_row(user)]))


async def cmd_resolve(message: Message, user: User, command: CommandObject) -> None:
    args = (command.args or "").strip() if command else ""
    urls = extract_urls(args)
    if not urls:
        await send(message, "Send a link with the command, or just paste the link:\n"
                            "<pre>/resolve https://gplinks.co/BFfOX</pre>")
        return
    await handle_links(message, user, urls)


async def handle_links(message: Message, user: User, urls: list[str]) -> None:
    """Resolve 1..N URLs found in a message."""
    if len(urls) == 1:
        await do_resolve(message, user, urls[0])
        return
    cap = CFG.vip_batch_limit if user.is_vip else CFG.free_batch_limit
    if not cap:
        await send(message,
                   f"📦 You sent <b>{len(urls)}</b> links.\n\n"
                   "Batch mode is a <b>VIP</b> feature — free accounts resolve one "
                   "link at a time.\n\n💎 /plans · or send them one per message.",
                   reply_markup=InlineKeyboardMarkup(inline_keyboard=[vip_button_row(user)]))
        await do_resolve(message, user, urls[0])
        return
    if not await guard_quota(message, user):
        return
    batch = urls[:cap]
    if len(urls) > cap:
        await send(message, f"✂️ Capped at <b>{cap}</b> links per message — "
                            f"ignoring the other {len(urls) - cap}.")
    progress = await send(message, f"⏳ <b>Batch</b> — working through "
                                   f"{len(batch)} links…")
    results = await run_batch(message, user, batch, progress)
    await replace_or_send(progress, message, fmt_batch(results, user)[:4000])


# ── inline callbacks ──────────────────────────────────────────────────────

async def cb_copy(c: CallbackQuery, user: User) -> None:
    key = c.data.split(":", 1)[1]
    hit = CACHE.get(key)
    if not hit:
        return await c.answer("That result expired — resolve it again.", show_alert=True)
    r, _ = hit
    if not r.ok:
        return await c.answer("Nothing to copy for a failed resolve.", show_alert=True)
    with contextlib.suppress(Exception):
        await c.message.answer(f"<code>{esc(r.final_url)}</code>")
    await c.answer("Copied to chat ✅")


async def cb_chain(c: CallbackQuery, user: User) -> None:
    key = c.data.split(":", 1)[1]
    hit = CACHE.get(key)
    if not hit:
        return await c.answer("That result expired.", show_alert=True)
    r, _ = hit
    if not r.hops:
        return await c.answer("Single hop — no chain to show.", show_alert=True)
    if not user.is_vip and not CFG.show_chain_to_free:
        return await c.answer(f"🧭 The full chain is a VIP feature. It had "
                              f"{r.depth} hop(s).\n/plans", show_alert=True)
    lines = ["🧭 <b>Redirect chain</b>", ""]
    for i, h in enumerate(r.hops, 1):
        lines.append(f"{i}. <code>{esc(h.url[:120])}</code>")
        lines.append(f"   ↳ <b>{esc(h.kind)}</b>" + (f" — {esc(h.note)}" if h.note else ""))
    lines.append(f"{len(r.hops) + 1}. <code>{esc(r.final_url[:120])}</code>")
    lines.append("   ↳ <b>final destination</b>")
    lines += ["", f"⏱ total {r.elapsed_ms / 1000:.2f}s · strategy "
                  f"<code>{esc(r.handler)}</code>"]
    with contextlib.suppress(Exception):
        await c.message.answer("\n".join(lines)[:4000])
    await c.answer()


async def cb_again(c: CallbackQuery, user: User) -> None:
    key = c.data.split(":", 1)[1]
    hit = CACHE.get(key)
    if not hit:
        return await c.answer("Expired.", show_alert=True)
    _, input_url = hit
    await c.answer("Re-resolving…")
    msg = c.message
    if not isinstance(msg, Message):
        return
    await do_resolve(msg, user, input_url, quiet=True)


async def cb_menu(c: CallbackQuery, user: User) -> None:
    what = c.data.split(":", 1)[1]
    msg = c.message
    if not isinstance(msg, Message):
        return await c.answer()
    if what == "sites":
        await msg.answer(TXT_SITES.format(count=len(REGISTRY), groups=group_sites()))
    elif what == "help":
        await cmd_help(msg, user)
    await c.answer()


async def cb_upsell(c: CallbackQuery, user: User) -> None:
    msg = c.message
    if isinstance(msg, Message):
        await cmd_plans(msg, user)
    await c.answer()


async def cb_noop(c: CallbackQuery) -> None:
    await c.answer()


BATCH_TIMEOUT_PAD = 20


async def run_batch(message: Message, user: User, urls: list[str],
                    progress: Optional[Message] = None) -> list[ResolveOutcome]:
    """Resolve a list of links sequentially, charging quota and logging each.

    Sequential on purpose: these sites rate-limit aggressively and a burst of
    parallel POSTs to one shortener is how an IP gets banned.
    """
    budget = CFG.http_timeout * 2 + CFG.golink_delay * (CFG.form_retries + 1) \
        + BATCH_TIMEOUT_PAD
    results: list[ResolveOutcome] = []
    total = len(urls)
    for i, u in enumerate(urls, 1):
        DB.consume_quota(user.user_id)
        try:
            r = await asyncio.wait_for(RESOLVER.resolve(u), timeout=budget)
        except asyncio.TimeoutError:
            r = ResolveOutcome(input_url=u, shortener=host_of(u),
                               error=f"Timed out after {int(budget)}s.")
        except Exception as exc:
            log.exception("batch resolve crashed for %s", u)
            r = ResolveOutcome(input_url=u, shortener=host_of(u),
                               error=f"{type(exc).__name__}: {exc}")
        DB.log_resolve(user.user_id, u, r.final_url if r.ok else "",
                       r.shortener or host_of(u), r.handler, r.ok, r.error,
                       r.elapsed_ms)
        results.append(r)
        if progress is not None:
            with contextlib.suppress(Exception):
                await progress.edit_text(
                    f"⏳ <b>Batch</b> — {i}/{total} done"
                    f" ({sum(1 for x in results if x.ok)} unlocked)…")
    return results


async def replace_or_send(progress: Optional[Message], message: Message,
                          text: str) -> bool:
    """Edit the progress bubble into the final answer; fall back to a new post.

    Telegram rejects an edit whose text is byte-identical to the current one, and
    network hiccups happen mid-resolve - neither may cost the user their result.
    """
    if progress is not None:
        try:
            await progress.edit_text(text)
            return True
        except Exception:
            with contextlib.suppress(Exception):
                await progress.delete()
    with contextlib.suppress(Exception):
        await send(message, text)
        return True
    return False


# ── batch FSM ─────────────────────────────────────────────────────────────

async def cmd_batch(message: Message, user: User, state: FSMContext) -> None:
    if not user.is_vip:
        return await send(message, "📦 <b>Batch mode is VIP-only.</b>\n\n"
                                   "Free accounts resolve one link per message.\n"
                                   "/plans · /redeem &lt;CODE&gt;",
                          reply_markup=InlineKeyboardMarkup(
                              inline_keyboard=[vip_button_row(user)]))
    await state.set_state(BotStates.batch)
    await state.update_data(urls=[])
    await send(message, "📦 <b>Batch mode ON</b>\n\n"
                        f"Paste links (one per line, up to {CFG.vip_batch_limit}).\n"
                        "Finish with /done · abort with /cancel")


async def cmd_done(message: Message, user: User, state: FSMContext) -> None:
    data = await state.get_data()
    urls = list(dict.fromkeys(data.get("urls") or []))
    await state.clear()
    if not urls:
        return await send(message, "No links collected. /batch to start over.")
    urls = urls[:CFG.vip_batch_limit]
    progress = await send(message, f"⏳ <b>Batch</b> — {len(urls)} links queued…")
    results = await run_batch(message, user, urls, progress)
    await replace_or_send(progress, message, fmt_batch(results, user)[:4000])


async def cmd_cancel(message: Message, user: User, state: FSMContext) -> None:
    st = await state.get_state()
    await state.clear()
    await send(message, "✅ Cancelled." if st else "Nothing was in progress.")


# ── plain text (the main interface) ───────────────────────────────────────

async def on_text(message: Message, user: User, state: FSMContext) -> None:
    text = (message.text or "").strip()

    if await state.get_state() == BotStates.batch:
        urls = extract_urls(text, message.entities)
        if urls:
            data = await state.get_data()
            acc = list(dict.fromkeys((data.get("urls") or []) + urls))
            await state.update_data(urls=acc)
            await send(message, f"➕ Added <b>{len(urls)}</b> · total "
                                f"<b>{len(acc)}</b>. /done when ready.")
        else:
            await send(message, "No links in that message. /done to finish, "
                                "/cancel to abort.")
        return

    low = text.lower()
    if low in ("🔗 resolve", "resolve", "🔗"):
        return await send(message, "Paste the shortened link and I will unlock it.\n"
                                   "<pre>https://gplinks.co/BFfOX</pre>")
    if low in ("💎 my vip", "💎 vip", "vip"):
        return await cmd_status(message, user)
    if low in ("📜 sites", "sites"):
        return await cmd_sites(message, user)
    if low in ("ℹ️ help", "help"):
        return await cmd_help(message, user)

    urls = extract_urls(text, message.entities)
    if not urls:
        if low in ("hi", "hello", "hey", "start"):
            return await cmd_start(message, user, None)
        return await send(message,
                          "I did not spot a link in that message.\n\n"
                          "Paste a shortened URL, for example:\n"
                          "<pre>https://gplinks.co/BFfOX</pre>\n\n"
                          "/help · /sites · /status")
    await handle_links(message, user, urls)


# ══════════════════════════════════════════════════════════════════════════
# §9  ADMIN PANEL
# ══════════════════════════════════════════════════════════════════════════

def is_admin(user: User) -> bool:
    return bool(user.is_admin) or user.user_id in CFG.admin_ids


async def target_id(bot: Bot, arg: str) -> Optional[int]:
    """Accept a numeric id, @username, or t.me link."""
    arg = (arg or "").strip()
    if not arg:
        return None
    if re.fullmatch(r"-?\d+", arg):
        return int(arg)
    m = re.search(r"t\.me/([A-Za-z0-9_]{4,})", arg)
    if m:
        arg = "@" + m.group(1)
    if arg.startswith("@"):
        known = DB.find_by_username(arg)
        if known:
            return known.user_id
        with contextlib.suppress(Exception):
            chat = await bot.get_chat(arg)
            return chat.id
    return None


async def deny(message: Message) -> None:
    await send(message, "🔒 That command is for bot administrators only.")


async def admin_target(message: Message, command: CommandObject,
                       verb: str) -> Optional[User]:
    args = (command.args or "").split() if command else []
    if not args:
        await send(message, f"Usage: <pre>/{verb} &lt;user id or @username&gt; "
                            f"[days]</pre>")
        return None
    tid = await target_id(message.bot, args[0])
    if tid is None:
        await send(message, f"❓ Could not resolve <code>{esc(args[0])}</code>. "
                            f"Use a numeric user ID, or make sure they have "
                            f"messaged the bot at least once.")
        return None
    return DB.upsert_user(tid)


async def cmd_addvip(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    args = (command.args or "").split() if command else []
    tgt = await admin_target(message, command, "addvip")
    if tgt is None:
        return
    days = 30
    if len(args) > 1:
        if args[1].lower() in ("0", "perm", "permanent", "inf", "forever"):
            days = 0
        elif args[1].isdigit():
            days = int(args[1])
    updated = DB.set_vip(tgt.user_id, days, source=f"admin:{user.user_id}")
    await send(message, f"✅ <b>VIP granted</b>\n\n"
                        f"👤 <code>{updated.user_id}</code> "
                        f"(@{esc(updated.username or 'n/a')})\n"
                        f"⏳ <b>{updated.vip_label()}</b>")


async def cmd_delvip(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    tgt = await admin_target(message, command, "delvip")
    if tgt is None:
        return
    if tgt.is_admin:
        return await send(message, "ℹ️ That user is an admin — VIP cannot be revoked.")
    DB.revoke_vip(tgt.user_id)
    await send(message, f"🗑 VIP revoked for <code>{tgt.user_id}</code>.")


async def cmd_listvip(message: Message, user: User) -> None:
    if not is_admin(user):
        return await deny(message)
    rows = DB.list_vip()
    if not rows:
        return await send(message, "No active VIP members.")
    lines = [f"👑 <b>{len(rows)} active VIP</b>", ""]
    for r in rows:
        who = f"@{r['username']}" if r["username"] else (r["first_name"] or "?")
        if r["vip_until"] >= PERMANENT:
            exp = "permanent"
        else:
            exp = datetime.fromtimestamp(r["vip_until"], timezone.utc).strftime("%d %b %Y")
        lines.append(f"• <code>{r['user_id']}</code> {esc(who)} — {esc(exp)}"
                     + (f" <i>({esc(r['vip_source'])})</i>" if r["vip_source"] else ""))
    await send(message, "\n".join(lines)[:4000])


async def cmd_gencode(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    args = (command.args or "").split() if command else []
    days = int(args[0]) if args and args[0].isdigit() else 7
    count = int(args[1]) if len(args) > 1 and args[1].isdigit() else 1
    count = max(1, min(count, 50))
    days = max(1, min(days, 3650))
    note = " ".join(args[2:])[:120]
    codes = [DB.make_code(days, user.user_id, note) for _ in range(count)]
    await send(message, "🎟 <b>Codes generated</b>\n\n"
                        f"💎 {days} day(s) of VIP each\n\n"
                        + "\n".join(f"<code>{c}</code>" for c in codes)
                        + "\n\nUsers redeem with <pre>/redeem CODE</pre>\n"
                          "Shareable deep link: <pre>/start CODE</pre>")


async def cmd_codes(message: Message, user: User) -> None:
    if not is_admin(user):
        return await deny(message)
    rows = DB.list_codes()
    if not rows:
        return await send(message, "No unused codes. /gencode to create some.")
    lines = [f"🎟 <b>{len(rows)} live code(s)</b>", ""]
    for r in rows:
        lines.append(f"• <code>{r['code']}</code> — {r['days']}d")
    await send(message, "\n".join(lines)[:4000])


async def cmd_revoke(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    code = ((command.args or "") if command else "").strip()
    if not code:
        return await send(message, "Usage: <pre>/revoke SHADOW-XXXXXX</pre>")
    ok = DB.revoke_code(code)
    await send(message, f"✅ Revoked <code>{esc(code.upper())}</code>."
                        if ok else "❓ No such code.")


async def cmd_ban(message: Message, user: User, command: CommandObject) -> None:
    await _ban_impl(message, user, command, True)


async def cmd_unban(message: Message, user: User, command: CommandObject) -> None:
    await _ban_impl(message, user, command, False)


async def _ban_impl(message: Message, user: User, command: CommandObject,
                    ban: bool) -> None:
    if not is_admin(user):
        return await deny(message)
    tgt = await admin_target(message, command, "ban" if ban else "unban")
    if tgt is None:
        return
    if tgt.is_admin:
        return await send(message, "ℹ️ Admins cannot be banned.")
    DB.set_ban(tgt.user_id, ban)
    await send(message, f"{'🚫 Banned' if ban else '✅ Unbanned'} "
                        f"<code>{tgt.user_id}</code>.")


async def cmd_setadmin(message: Message, user: User, command: CommandObject) -> None:
    await _setadmin_impl(message, user, command, True)


async def cmd_unsetadmin(message: Message, user: User, command: CommandObject) -> None:
    await _setadmin_impl(message, user, command, False)


async def _setadmin_impl(message: Message, user: User, command: CommandObject,
                         grant: bool) -> None:
    if user.user_id not in CFG.admin_ids and not user.is_admin:
        return await deny(message)
    tgt = await admin_target(message, command, "setadmin" if grant else "unsetadmin")
    if tgt is None:
        return
    DB.set_admin(tgt.user_id, grant)
    await send(message, f"{'🛡 Admin rights granted' if grant else '🗑 Admin rights removed'}"
                        f" for <code>{tgt.user_id}</code>.")


async def cmd_stats(message: Message, user: User) -> None:
    if not is_admin(user):
        return await deny(message)
    s = DB.stats()
    rate = (s["success"] / s["resolves"] * 100) if s["resolves"] else 0
    lines = [
        "📊 <b>SHADOWLINKS stats</b>", "",
        f"👥 Users        : <b>{s['users']}</b>",
        f"👑 Active VIP   : <b>{s['vip']}</b>",
        f"🚫 Banned       : {s['banned']}",
        f"🎟 Codes live   : {s['codes_live']}  (used {s['codes_used']})", "",
        f"🔗 Resolves     : <b>{s['resolves']}</b>",
        f"✅ Succeeded    : {s['success']}  ({rate:.1f}%)",
        f"🕐 Last 24h     : {s['today']}",
        f"⏱ Avg success   : {s['avg_ms']} ms", "",
        f"🧩 Registry     : {len(REGISTRY)} domains",
        f"⚙️ Backend      : {'curl_cffi' if HTTP._cffi_ok else 'aiohttp'} "
        f"(impersonate={esc(CFG.impersonate)})",
    ]
    top = DB.top_domains(8)
    if top:
        lines += ["", "🏆 <b>Top shorteners</b>"]
        for r in top:
            pct = (r["ok"] / r["hits"] * 100) if r["hits"] else 0
            lines.append(f"• <code>{esc(r['domain'])}</code> — {r['hits']} hits, "
                         f"{pct:.0f}% ok")
    await send(message, "\n".join(lines)[:4000])


async def cmd_logs(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    args = (command.args or "").split() if command else []
    n = int(args[0]) if args and args[0].isdigit() else 12
    rows = DB.q("SELECT user_id, input_url, final_url, ok, error, ms, ts FROM logs "
                "ORDER BY id DESC LIMIT ?", (min(n, 50),))
    if not rows:
        return await send(message, "No log rows yet.")
    lines = [f"🧾 <b>last {len(rows)} resolves</b>", ""]
    for r in rows:
        when = datetime.fromtimestamp(r["ts"], timezone.utc).strftime("%H:%M:%S")
        mark = "✅" if r["ok"] else "❌"
        lines.append(f"{mark} {when} <code>{r['user_id']}</code> {r['ms']}ms")
        lines.append(f"   in  <code>{esc((r['input_url'] or '')[:90])}</code>")
        lines.append(f"   out <code>{esc((r['final_url'] or r['error'] or '-')[:90])}</code>")
    await send(message, "\n".join(lines)[:4000])


async def cmd_delays(message: Message, user: User) -> None:
    if not is_admin(user):
        return await deny(message)
    rows = DB.q("SELECT k, v FROM kv WHERE k LIKE 'delay:%' ORDER BY v DESC")
    if not rows:
        return await send(message, "No learned anti-bot delays yet. They appear "
                                   "after the first timed-out form POST.")
    lines = ["⏱ <b>Learned anti-bot delays</b>", ""]
    for r in rows:
        lines.append(f"• <code>{esc(r['k'][6:])}</code> → {esc(r['v'])}s")
    lines += ["", "<i>Set with</i> <pre>/setdelay domain seconds</pre>"]
    await send(message, "\n".join(lines)[:4000])


async def cmd_setdelay(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    args = (command.args or "").split() if command else []
    if len(args) < 2:
        return await send(message, "Usage: <pre>/setdelay gplinks.co 10</pre>")
    domain = args[0].lower().lstrip(".")
    try:
        secs = max(0.0, min(float(args[1]), 60.0))
    except ValueError:
        return await send(message, "Seconds must be a number.")
    DB.set_delay(domain, secs)
    DB.set_kv(f"delay:{domain}", f"{secs:.1f}")
    await send(message, f"⏱ <code>{esc(domain)}</code> delay pinned to "
                        f"<b>{secs}s</b>.\n<i>Note: adaptive learning is "
                        f"{'on' if CFG.adaptive_delay else 'off'}.</i>")


async def cmd_whois(message: Message, user: User, command: CommandObject) -> None:
    if not is_admin(user):
        return await deny(message)
    tgt = await admin_target(message, command, "whois")
    if tgt is None:
        return
    reqs = DB.one("SELECT COUNT(*) FROM logs WHERE user_id=?", (tgt.user_id,), 0)
    okc = DB.one("SELECT COUNT(*) FROM logs WHERE user_id=? AND ok=1", (tgt.user_id,), 0)
    await send(message, TXT_STATUS.format(
        name=esc(tgt.first_name or tgt.username or "?"), uid=tgt.user_id,
        plan="👑 <b>VIP</b>" if tgt.is_vip else "🆓 <b>Free</b>",
        expires=esc(tgt.vip_label()), source=esc(tgt.vip_source or "—"),
        used=okc, limit=f" ({reqs} attempts)", cooldown_line="",
        total=reqs,
        joined=datetime.fromtimestamp(tgt.created_at or int(time.time()),
                                      timezone.utc).strftime("%d %b %Y"))
        + f"\n\n👤 @<code>{esc(tgt.username or 'none')}</code>"
          f"\n💬 chat <code>{tgt.chat_id}</code>"
          f"\n🛡 admin {'yes' if tgt.is_admin else 'no'}"
          f"\n🚫 banned {'yes' if tgt.is_banned else 'no'}")


async def cmd_broadcast(message: Message, user: User, command: CommandObject,
                        state: FSMContext) -> None:
    if not is_admin(user):
        return await deny(message)
    text = (command.args or "").strip() if command else ""
    if not text:
        await state.set_state(BotStates.broadcasting)
        return await send(message, "📣 Send the message to broadcast.\n"
                                   "HTML is allowed. /cancel to abort.")
    await run_broadcast(message, text)


async def run_broadcast(message: Message, text: str) -> None:
    ids = DB.all_user_ids()
    if not ids:
        return await send(message, "Nobody to broadcast to yet.")
    ok = fail = 0
    for i, uid in enumerate(ids, 1):
        try:
            await message.bot.send_message(uid, f"📣 {text}",
                                           disable_notification=True)
            ok += 1
        except TelegramForbiddenError:
            fail += 1
        except TelegramRetryAfter as e:
            await asyncio.sleep(e.retry_after + 0.5)
            with contextlib.suppress(Exception):
                await message.bot.send_message(uid, f"📣 {text}",
                                               disable_notification=True)
                ok += 1
        except Exception:
            fail += 1
        if i % 20 == 0:
            await asyncio.sleep(1.0)   # stay well under Telegram's ~30 msg/s
    await send(message, f"📣 <b>Broadcast done</b>\n\n✅ sent {ok}\n❌ failed {fail}"
                        f"\n👥 audience {len(ids)}")


async def on_broadcast_text(message: Message, user: User, state: FSMContext) -> None:
    if not is_admin(user):
        await state.clear()
        return await deny(message)
    text = message.text or message.caption or ""
    await state.clear()
    if not text.strip():
        return await send(message, "Empty broadcast aborted.")
    await run_broadcast(message, text)


async def cmd_adminhelp(message: Message, user: User) -> None:
    if not is_admin(user):
        return await deny(message)
    await send(message, """🛡 <b>Admin panel</b>

<b>VIP management</b>
/addvip &lt;id|@user&gt; [days|perm]
/delvip &lt;id|@user&gt;
/listvip
/whois &lt;id|@user&gt;

<b>Redeem codes</b>
/gencode [days] [count] [note]
/codes
/revoke &lt;CODE&gt;

<b>Moderation</b>
/ban &lt;id&gt; · /unban &lt;id&gt;
/setadmin &lt;id&gt; · /unsetadmin &lt;id&gt;

<b>Insight</b>
/stats — users, success rate, top shorteners
/logs [n] — last n resolves
/delays — learned anti-bot timers
/setdelay &lt;domain&gt; &lt;secs&gt; — pin a timer

<b>Reach</b>
/broadcast &lt;text&gt; — or /broadcast then type it

<b>Extending the registry without redeploying</b>
Set <code>EXTRA_GPLINKS_DOMAINS</code>, <code>EXTRA_FORM_DOMAINS</code>,
<code>EXTRA_REDIRECT_DOMAINS</code> or <code>EXTRA_ADFLY_DOMAINS</code> to a
comma separated host list and restart.""")


async def on_non_text(message: Message, user: User) -> None:
    await send(message, "Send me the link as <b>text</b> — I cannot read "
                        "photos of links reliably.\n\n"
                        "<pre>https://gplinks.co/BFfOX</pre>")


async def on_error(event, exception: Optional[Exception] = None, **kwargs) -> bool:
    """Last line of defence: log it, never crash the polling loop."""
    exc = exception or kwargs.get("exception") or getattr(event, "exception", None)
    log.error("unhandled update error: %s", exc, exc_info=exc)
    return True


# ══════════════════════════════════════════════════════════════════════════
# §10  ENTRYPOINT
# ══════════════════════════════════════════════════════════════════════════

def build_dispatcher() -> Dispatcher:
    dp = Dispatcher(storage=MemoryStorage())
    user_mw = UserMiddleware()
    dp.message.middleware(user_mw)
    dp.callback_query.middleware(user_mw)

    # public
    dp.message.register(cmd_start, Command("start"))
    dp.message.register(cmd_help, Command("help"))
    dp.message.register(cmd_plans, Command("plans"))
    dp.message.register(cmd_status, Command("status"))
    dp.message.register(cmd_status, Command("me"))
    dp.message.register(cmd_sites, Command("sites"))
    dp.message.register(cmd_sites, Command("domains"))
    dp.message.register(cmd_redeem, Command("redeem"))
    dp.message.register(cmd_resolve, Command("resolve"))
    dp.message.register(cmd_resolve, Command("r"))
    dp.message.register(cmd_resolve, Command("bypass"))
    dp.message.register(cmd_resolve, Command("unshort"))
    dp.message.register(cmd_batch, Command("batch"))
    dp.message.register(cmd_done, Command("done"))
    dp.message.register(cmd_cancel, Command("cancel"))

    # admin
    dp.message.register(cmd_addvip, Command("addvip"))
    dp.message.register(cmd_delvip, Command("delvip"))
    dp.message.register(cmd_listvip, Command("listvip"))
    dp.message.register(cmd_gencode, Command("gencode"))
    dp.message.register(cmd_codes, Command("codes"))
    dp.message.register(cmd_revoke, Command("revoke"))
    dp.message.register(cmd_ban, Command("ban"))
    dp.message.register(cmd_unban, Command("unban"))
    dp.message.register(cmd_setadmin, Command("setadmin"))
    dp.message.register(cmd_unsetadmin, Command("unsetadmin"))
    dp.message.register(cmd_stats, Command("stats"))
    dp.message.register(cmd_logs, Command("logs"))
    dp.message.register(cmd_delays, Command("delays"))
    dp.message.register(cmd_setdelay, Command("setdelay"))
    dp.message.register(cmd_whois, Command("whois"))
    dp.message.register(cmd_broadcast, Command("broadcast"))
    dp.message.register(cmd_adminhelp, Command("admin"))
    dp.message.register(cmd_adminhelp, Command("adminhelp"))

    # FSM catch-alls (must precede the generic text handler)
    dp.message.register(on_broadcast_text, BotStates.broadcasting, F.text)
    dp.message.register(on_text, BotStates.batch, F.text)

    # inline callbacks
    dp.callback_query.register(cb_copy, F.data.startswith("copy:"))
    dp.callback_query.register(cb_chain, F.data.startswith("chain:"))
    dp.callback_query.register(cb_again, F.data.startswith("again:"))
    dp.callback_query.register(cb_menu, F.data.startswith("menu:"))
    dp.callback_query.register(cb_upsell, F.data == "upsell")
    dp.callback_query.register(cb_noop, F.data.startswith("noop:"))

    # generic text last
    dp.message.register(on_text, F.text)

    dp.message.register(on_non_text, F.content_type != ContentType.TEXT)
    dp.errors.register(on_error)

    return dp


async def on_startup(bot: Bot) -> None:
    me = await bot.get_me()
    log.info("logged in as @%s (id=%s)", me.username, me.id)
    log.info("registry: %d domains · backend: %s · impersonate: %s",
             len(REGISTRY), "curl_cffi" if HTTP._cffi_ok else "aiohttp", CFG.impersonate)
    if CFG.admin_ids:
        for aid in CFG.admin_ids:
            DB.set_admin(aid, True)
        log.info("admins: %s", CFG.admin_ids)
        with contextlib.suppress(Exception):
            await bot.send_message(
                CFG.admin_ids[0],
                f"🕶 <b>{esc(CFG.bot_name)} v{__version__} online</b>\n\n"
                f"🧩 {len(REGISTRY)} shorteners registered\n"
                f"⚙️ {'curl_cffi' if HTTP._cffi_ok else 'aiohttp'} / "
                f"{esc(CFG.impersonate)}\n"
                f"👥 {DB.stats()['users']} known users\n\n/admin for the panel",
                disable_notification=True)
    else:
        log.warning("ADMIN_IDS is empty - nobody can manage VIP. Set it in .env")


async def on_shutdown(bot: Bot) -> None:
    await HTTP.close()
    with contextlib.suppress(Exception):
        DB.trim_logs(14)
    DB.close()
    log.info("shut down cleanly")


def preflight() -> bool:
    """Fail fast with an actionable message instead of a traceback."""
    problems = []
    if not CFG.token or ":" not in CFG.token:
        problems.append("BOT_TOKEN is missing. Create a bot with @BotFather, then put "
                        "the token in .env as BOT_TOKEN=123456:ABC-DEF…")
    if not CFG.admin_ids:
        problems.append("ADMIN_IDS is empty. Set ADMIN_IDS=<your telegram user id> so "
                        "you can grant VIP. Get your id from @userinfobot.")
    for p in problems:
        print(f"[SHADOWLINKS] ⚠️  {p}", file=sys.stderr)
    return not problems


def main() -> int:
    logging.basicConfig(
        level=getattr(logging, CFG.log_level.upper(), logging.INFO),
        format="%(asctime)s %(levelname)-7s %(name)s :: %(message)s",
        datefmt="%H:%M:%S",
    )
    logging.getLogger("aiogram.event").setLevel(logging.WARNING)
    logging.getLogger("aiohttp.access").setLevel(logging.WARNING)

    if "--check" in sys.argv:
        print(f"{__app__} v{__version__}")
        print(f"  registry domains : {len(REGISTRY)}")
        print(f"  handlers         : "
              f"{sorted({p.handler for p in REGISTRY.values()})}")
        print(f"  backend          : "
              f"{'curl_cffi' if HTTP._cffi_ok else 'aiohttp'} ({CFG.impersonate})")
        print(f"  db               : {os.path.abspath(CFG.db_path)}")
        print(f"  free limit       : {CFG.free_daily_limit}/day, "
              f"{CFG.free_cooldown}s cooldown")
        print(f"  vip batch        : {CFG.vip_batch_limit} links/message")
        print(f"  admins           : {CFG.admin_ids or '(none)'}")
        print(f"  token            : {'set' if CFG.token else 'MISSING'}")
        return 0 if CFG.token else 2

    if not preflight():
        print("\nRefusing to start. Fix .env and re-run "
              "(or pass --check to inspect the build).", file=sys.stderr)
        return 2

    bot = Bot(token=CFG.token, default=DefaultBotProperties(
        parse_mode=ParseMode.HTML,
        link_preview=LinkPreviewOptions(is_disabled=True),
    ))
    dp = build_dispatcher()
    dp.startup.register(on_startup)
    dp.shutdown.register(on_shutdown)

    print(f"\n🕶  {__app__} v{__version__} starting — "
          f"{len(REGISTRY)} shorteners, Ctrl+C to stop\n")
    try:
        asyncio.run(dp.start_polling(
            bot, allowed_updates=dp.resolve_used_update_types()))
    except (KeyboardInterrupt, SystemExit):
        print("\n👋 stopped")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
