#!/usr/bin/env python3
"""Dispatch-level tests for SHADOWLINKS on python-telegram-bot 22.

These drive the REAL PTB dispatcher: a fabricated `Update` goes through
`Application.process_update()`, so filters, handler ordering, the state
filters and the `tg()` adapter are all exercised - not called directly.

Outgoing Telegram API calls are recorded by stubbing the Bot's methods, and
the resolver is stubbed to return canned outcomes, so nothing touches the
network.

Run:  python tests/test_ptb.py
"""
from __future__ import annotations

import asyncio
import os
import sys
import tempfile
import time
import traceback

# ── environment must be set BEFORE shadowlinks_bot is imported (CFG is a
#    frozen dataclass built at import time) ────────────────────────────────
_TMP = tempfile.mkdtemp(prefix="sl_ptb_")
os.environ.update({
    "BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "ADMIN_IDS": "700000001",
    "DB_PATH": os.path.join(_TMP, "test.db"),
    "ALLOW_PRIVATE_NETWORKS": "1",
    "FREE_DAILY_LIMIT": "5",
    "FREE_COOLDOWN": "45",
    "VIP_BATCH_LIMIT": "25",
    "FREE_BATCH_LIMIT": "0",
    "LOG_RESOLVES": "1",
})
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import shadowlinks_bot as B                                    # noqa: E402
from telegram import Chat, Message, Update, User as TgUser      # noqa: E402
from telegram.ext import (                                       # noqa: E402
    Application, ExtBot, MessageHandler,
)

ADMIN = 700000001
VIP = 555000111
FREE = 555000222
BANNED = 555000333

PASS = FAIL = 0
FAILED: list[str] = []


def check(cond: bool, label: str, extra: str = "") -> None:
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  \033[32mPASS\033[0m  {label}")
    else:
        FAIL += 1
        FAILED.append(label)
        print(f"  \033[31mFAIL\033[0m  {label}" + (f"\n        {extra}" if extra else ""))


# ══════════════════════════════════════════════════════════════════════════
#  harness
# ══════════════════════════════════════════════════════════════════════════

class Recorder:
    """Records every Telegram API call the bot makes."""

    def __init__(self) -> None:
        self.calls: list[dict] = []
        self._mid = 1000

    def api(self, name: str, **kw) -> None:
        self.calls.append({"api": name, **kw})

    def of(self, name: str) -> list[dict]:
        return [c for c in self.calls if c["api"] == name]

    def texts(self, name: str = "sendMessage") -> list[str]:
        return [c.get("text", "") for c in self.of(name)]

    def last_text(self, name: str = "sendMessage") -> str:
        t = self.texts(name)
        return t[-1] if t else ""

    def any_text_contains(self, needle: str) -> bool:
        return any(needle in t for t in self.texts() + self.texts("editMessageText"))


REC = Recorder()


class RecBot(ExtBot):
    """ExtBot with every outgoing API call recorded instead of sent.

    PTB's TelegramObject blocks monkeypatching methods, so the bot is
    subclassed. Returning real Message objects (not dicts) is what lets
    handlers keep doing `progress = await message.reply_text(...)` and then
    `progress.edit_text(...)`.
    """

    async def send_message(self, chat_id, text, **kw):
        REC.api("sendMessage", chat_id=chat_id, text=text, **kw)
        return _mk_msg(chat_id, text)

    async def edit_message_text(self, chat_id=None, message_id=None, text=None, **kw):
        REC.api("editMessageText", chat_id=chat_id, message_id=message_id,
                text=text, **kw)
        return True

    async def delete_message(self, chat_id=None, message_id=None, **kw):
        REC.api("deleteMessage", chat_id=chat_id, message_id=message_id, **kw)
        return True

    async def send_chat_action(self, chat_id=None, action=None, **kw):
        REC.api("chatAction", chat_id=chat_id, action=action)
        return True

    async def answer_callback_query(self, callback_query_id=None, text=None,
                                    show_alert=None, **kw):
        REC.api("answerCallbackQuery", id=callback_query_id, text=text,
                show_alert=show_alert)
        return True

    async def get_chat(self, chat_id=None, **kw):
        REC.api("getChat", chat_id=chat_id)
        return Chat.de_json({"id": 999000, "type": "private"}, self)

    async def get_me(self, **kw):
        return TgUser.de_json({"id": 123456, "is_bot": True,
                               "first_name": "ShadowLinks",
                               "username": "shadowlinks_bot"}, self)


def _mk_msg(chat_id: int, text: str) -> Message:
    REC._mid += 1
    return Message.de_json({
        "message_id": REC._mid,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "text": text,
    }, BOT)


BOT = RecBot(token="123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
             defaults=B.default_bot_properties())


_UID = [0]


def _next_uid() -> int:
    _UID[0] += 1
    return _UID[0]


def tg_user(uid: int, username: str = "someone", first: str = "Some One") -> dict:
    return {"id": uid, "is_bot": False, "first_name": first, "username": username}


def msg_update(text: str | None = None, uid: int = FREE, chat_id: int | None = None,
               username: str = "someone", first: str = "Some One",
               msg_id: int = 7, extra: dict | None = None) -> Update:
    chat_id = chat_id if chat_id is not None else uid
    m: dict = {
        "message_id": msg_id,
        "date": int(time.time()),
        "chat": {"id": chat_id, "type": "private"},
        "from": tg_user(uid, username, first),
    }
    if text is not None:
        m["text"] = text
        # Real Telegram attaches a bot_command entity to every "/cmd" message,
        # and PTB's filters.COMMAND requires it. Without this, commands fall
        # through to the generic text handler - a harness artefact that would
        # hide a genuine dispatch bug.
        if text.startswith("/"):
            first = text.split(" ")[0]
            m["entities"] = [{"type": "bot_command", "offset": 0,
                              "length": len(first)}]
    if extra:
        m.update(extra)
    return Update.de_json({"update_id": _next_uid(), "message": m}, APP.bot)


def cb_update(data: str, uid: int = FREE, msg_text: str = "result") -> Update:
    m = {
        "message_id": 42,
        "date": int(time.time()),
        "chat": {"id": uid, "type": "private"},
        "from": tg_user(uid),
        "text": msg_text,
    }
    q = {"id": f"cb{_next_uid()}", "chat_instance": "ci", "data": data,
         "from": tg_user(uid), "message": m}
    return Update.de_json({"update_id": _next_uid(), "callback_query": q}, APP.bot)


def run(coro) -> None:
    asyncio.run(coro)


def reset() -> None:
    """Clear recorded API calls only.

    Deliberately does NOT clear B._STATE: /batch and /broadcast are
    multi-message conversations, and wiping the FSM between dispatches made
    the follow-up message fall through to the generic handler. Use
    reset_state() when a test genuinely needs a clean slate.
    """
    REC.calls.clear()


def reset_state() -> None:
    B._STATE.clear()


def dispatch(update: Update) -> None:
    """Push an update through the real PTB dispatcher."""
    async def _go():
        await APP.process_update(update)
    run(_go())


# ── canned resolver ───────────────────────────────────────────────────────

def stub_resolver(final: str = "https://example.com/real-page",
                  ok: bool = True, error: str = "",
                  handler: str = "gplinks", shortener: str = "gplinks.co",
                  hops: list | None = None, ms: int = 1234) -> list[str]:
    """Make RESOLVER.resolve return a canned outcome; return the URLs asked for."""
    asked: list[str] = []

    async def fake_resolve(url: str):
        asked.append(url)
        return B.ResolveOutcome(
            input_url=url, ok=ok,
            final_url=final if ok else "",
            shortener=shortener, handler=handler,
            error=error, hops=hops or [], elapsed_ms=ms)

    B.RESOLVER.resolve = fake_resolve
    return asked


APP = B.build_application(bot=BOT)

# `Application.process_update()` refuses to run before initialize(), and
# initialize() would hit the network (getMe). Mark both objects initialized and
# seed the bot identity by hand so the real dispatcher can be driven offline.
BOT._bot_user = TgUser(id=123456, is_bot=True, first_name="ShadowLinks",
                       username="shadowlinks_bot")
BOT._initialized = True
APP._initialized = True


def grant_vip(uid: int = VIP, days: int = 30) -> None:
    B.DB.upsert_user(uid, "vipuser", "Vip User", uid)
    B.DB.set_vip(uid, days, source="test")


# ══════════════════════════════════════════════════════════════════════════
#  tests
# ══════════════════════════════════════════════════════════════════════════

def t_framework_and_wiring():
    print("\n— framework & wiring —")
    check(B.telegram_ver.startswith("22."), f"running on PTB {B.telegram_ver}")
    n = sum(len(v) for v in APP.handlers.values())
    check(n >= 40, f"{n} handlers registered", f"got {n}")
    check(len(APP.error_handlers) == 1, "exactly one error handler registered")
    check(APP._update_processor.max_concurrent_updates > 1,
          f"concurrent updates ON ({APP._update_processor.max_concurrent_updates})",
          "PTB defaults to 1 => a 15s resolve would block every other user")
    d = APP.bot.defaults
    check(d.parse_mode == "HTML", f"default parse_mode={d.parse_mode}")
    check(d.do_quote is False, "do_quote=False (matches aiogram's .answer())",
          f"got {d.do_quote}")
    check(d.link_preview_options.is_disabled is True, "link previews disabled")
    check(APP.post_init is B.on_startup, "post_init wired to on_startup")
    check(APP.post_shutdown is B.on_shutdown, "post_shutdown wired to on_shutdown")


def t_start():
    print("\n— /start —")
    reset()
    dispatch(msg_update("/start"))
    txt = REC.last_text()
    check("VIP Link Resolver" in txt, "/start sends the welcome text", txt[:120])
    check("gplinks.co/BFfOX" in txt, "welcome shows the example link")
    rm = REC.of("sendMessage")[0].get("reply_markup")
    check(rm is not None and hasattr(rm, "keyboard"),
          "/start attaches a reply keyboard", f"got {rm!r}")


def t_start_redeems_code():
    print("\n— /start CODE (deep-link redeem) —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    code = B.DB.make_code(10, ADMIN, "deeplink")
    dispatch(msg_update(f"/start {code}"))
    txt = REC.last_text()
    check("Code accepted" in txt, f"/start {code[:12]}… redeems", txt[:120])
    check(B.DB.get_user(FREE).is_vip, "user is now VIP")
    B.DB.revoke_vip(FREE)


def t_command_args_multiword():
    """PTB gives context.args as a LIST; aiogram gave a STRING. The CommandArgs
    re-join must preserve multi-word arguments."""
    print("\n— multi-word command args —")
    reset()
    B.DB.upsert_user(ADMIN, "admin", "Admin", ADMIN)
    B.DB.set_admin(ADMIN, True)
    dispatch(msg_update("/gencode 30 5 birthday giveaway", uid=ADMIN))
    txt = REC.last_text()
    codes = [l for l in txt.splitlines() if l.startswith("<code>")]
    check(len(codes) == 5, "/gencode 30 5 … made 5 codes", f"got {len(codes)}")
    rows = B.DB.list_codes()
    check(any(r["days"] == 30 for r in rows), "days parsed as 30 (first arg)")
    check(any("birthday giveaway" in (r["note"] or "") for r in rows),
          "note kept BOTH trailing words ('birthday giveaway')",
          str([r["note"] for r in rows]))


def t_status_and_help():
    print("\n— /status, /help, /sites, /plans —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    dispatch(msg_update("/status"))
    check("Your account" in REC.last_text(), "/status works")
    reset()
    dispatch(msg_update("/help"))
    check("troubleshooting" in REC.last_text(), "/help works")
    reset()
    dispatch(msg_update("/sites"))
    t = REC.last_text()
    check("shorteners I handle natively" in t, "/sites works")
    check(str(len(B.REGISTRY)) in t, f"/sites reports {len(B.REGISTRY)} domains")
    reset()
    dispatch(msg_update("/plans"))
    check("VIP" in REC.last_text(), "/plans works")
    reset()
    dispatch(msg_update("/me"))
    check("Your account" in REC.last_text(), "/me aliases to /status")
    reset()
    dispatch(msg_update("/domains"))
    check("shorteners" in REC.last_text(), "/domains aliases to /sites")


def t_admin_gating():
    print("\n— admin gating —")
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    B.DB.upsert_user(ADMIN, "admin", "Admin", ADMIN)
    B.DB.set_admin(ADMIN, True)
    reset()
    dispatch(msg_update("/stats", uid=FREE))
    check("administrators only" in REC.last_text(), "/stats denied to non-admin")
    reset()
    dispatch(msg_update("/stats", uid=ADMIN))
    check("SHADOWLINKS stats" in REC.last_text(), "/stats allowed for admin")
    check("python-telegram-bot" not in REC.last_text(), "stats has no stale framework text")
    reset()
    dispatch(msg_update("/admin", uid=ADMIN))
    check("Admin panel" in REC.last_text(), "/admin shows the panel")
    reset()
    dispatch(msg_update("/logs 3", uid=ADMIN))
    check(len(REC.texts()) == 1, "/logs responds")


def t_resolve_single():
    print("\n— paste a link (the core flow) —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    B.DB.set_vip(FREE, 0)
    B.DB.x("UPDATE users SET quota_used=0, last_req_at=0 WHERE user_id=?", (FREE,))
    asked = stub_resolver(final="https://real.example/destination")
    dispatch(msg_update("https://gplinks.co/BFfOX"))
    check(asked == ["https://gplinks.co/BFfOX"], "resolver got the pasted URL",
          str(asked))
    check(REC.of("chatAction") and REC.of("chatAction")[0]["action"] == "typing",
          "typing indicator sent")
    edits = REC.of("editMessageText")
    check(bool(edits), "progress bubble was EDITED into the result",
          f"calls={[c['api'] for c in REC.calls]}")
    check("https://real.example/destination" in (edits[0]["text"] if edits else ""),
          "final destination present in the edited message")
    check("Destination unlocked" in (edits[0]["text"] if edits else ""),
          "success headline shown")
    rm = edits[0].get("reply_markup") if edits else None
    check(rm is not None, "result carries inline buttons")
    check(not REC.of("sendMessage")[-1].get("reply_parameters"),
          "replies are NOT quoted (do_quote=False honoured)")


def t_resolve_failure():
    print("\n— failed resolve —")
    reset()
    B.DB.x("UPDATE users SET quota_used=0, last_req_at=0 WHERE user_id=?", (FREE,))
    stub_resolver(ok=False, error="HTTP 403 from Cloudflare", handler="auto")
    dispatch(msg_update("https://gplinks.co/zzzzz"))
    edits = REC.of("editMessageText")
    txt = edits[0]["text"] if edits else REC.last_text()
    check("Could not unlock" in txt, "failure headline shown", txt[:120])
    check("403" in txt, "underlying error surfaced to the user")


def t_unknown_command_falls_through():
    print("\n— unknown command —")
    reset()
    dispatch(msg_update("/nonsensecommand"))
    txt = REC.last_text()
    check("did not spot a link" in txt,
          "/nonsensecommand reaches on_text (not silently dropped)", txt[:120])


def t_non_text():
    print("\n— non-text message —")
    reset()
    dispatch(msg_update(None, extra={"photo": [{"file_id": "", "file_unique_id": "u",
                                                "width": 10, "height": 10}]}))
    check("as <b>text</b>" in REC.last_text(), "photo gets the text-only hint",
          REC.last_text()[:120])


def t_greeting():
    print("\n— small talk —")
    reset()
    dispatch(msg_update("hello"))
    check("VIP Link Resolver" in REC.last_text(), "'hello' maps to /start")
    reset()
    dispatch(msg_update("📜 Sites"))
    check("shorteners" in REC.last_text(), "keyboard button '📜 Sites' works")
    reset()
    dispatch(msg_update("💎 My VIP"))
    check("Your account" in REC.last_text(), "keyboard button '💎 My VIP' works")


def t_quota_and_cooldown():
    print("\n— quota & cooldown —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    B.DB.x("UPDATE users SET vip_until=0, quota_used=99, quota_day=? WHERE user_id=?",
           (B.DB._today(), FREE))
    asked = stub_resolver()
    dispatch(msg_update("https://gplinks.co/BFfOX"))
    check(not asked, "resolver NOT called when the daily limit is spent", str(asked))
    check("Daily limit reached" in REC.last_text(), "limit message shown",
          REC.last_text()[:120])

    reset()
    B.DB.x("UPDATE users SET quota_used=0, last_req_at=? WHERE user_id=?",
           (int(time.time()), FREE))
    asked = stub_resolver()
    dispatch(msg_update("https://gplinks.co/BFfOX"))
    check(not asked, "resolver NOT called during cooldown")
    check("Slow down" in REC.last_text(), "cooldown message shown")
    B.DB.x("UPDATE users SET quota_used=0, last_req_at=0 WHERE user_id=?", (FREE,))


def t_banned():
    print("\n— banned user —")
    reset()
    B.DB.upsert_user(BANNED, "bad", "Bad Actor", BANNED)
    B.DB.set_ban(BANNED, True)
    asked = stub_resolver()
    dispatch(msg_update("https://gplinks.co/BFfOX", uid=BANNED))
    check(not asked, "banned user cannot resolve")
    check("blocked from using this bot" in REC.last_text(), "ban message shown",
          REC.last_text()[:120])
    reset()
    dispatch(cb_update("upsell", uid=BANNED))
    a = REC.of("answerCallbackQuery")
    check(bool(a) and a[0].get("show_alert") is True, "banned callback gets an alert")
    B.DB.set_ban(BANNED, False)


def t_batch_fsm_nonvip():
    print("\n— /batch as a free user —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    B.DB.x("UPDATE users SET vip_until=0 WHERE user_id=?", (FREE,))
    dispatch(msg_update("/batch"))
    check("VIP-only" in REC.last_text(), "batch refused for free users",
          REC.last_text()[:120])


def t_batch_fsm_vip():
    print("\n— /batch as VIP (state machine) —")
    reset()
    reset_state()
    grant_vip(VIP)
    dispatch(msg_update("/batch", uid=VIP))
    check("Batch mode ON" in REC.last_text(), "batch mode enabled")
    check(B._STATE.get(VIP, {}).get("state") == B.BotStates.batch,
          "state recorded as 'batch'", str(B._STATE.get(VIP)))

    reset()
    dispatch(msg_update("https://gplinks.co/aaa", uid=VIP))
    txt = REC.last_text()
    check("Added" in txt and "total" in txt, "link accumulated in batch state",
          txt[:120])
    acc = B._STATE[VIP]["data"]["urls"]
    check(len(acc) == 1, "1 URL buffered", str(acc))

    reset()
    dispatch(msg_update("https://gplinks.co/bbb\nhttps://gplinks.co/ccc", uid=VIP))
    acc = B._STATE[VIP]["data"]["urls"]
    check(len(acc) == 3, "3 URLs buffered after a 2-link message", str(acc))

    reset()
    asked = stub_resolver(final="https://real.example/x")
    dispatch(msg_update("/done", uid=VIP))
    check(len(asked) == 3, "/done resolved all 3 buffered links", str(asked))
    body = " ".join(REC.texts() + REC.texts("editMessageText"))
    check("Batch complete" in body and "3/3" in body, "batch summary shows 3/3",
          body[:200])
    check(B._STATE.get(VIP) is None, "state cleared after /done")


def t_batch_cancel():
    print("\n— /cancel —")
    reset()
    reset_state()
    grant_vip(VIP)
    dispatch(msg_update("/batch", uid=VIP))
    check(B._STATE.get(VIP, {}).get("state") == B.BotStates.batch, "in batch state")
    reset()
    dispatch(msg_update("/cancel", uid=VIP))
    check("Cancelled" in REC.last_text(), "/cancel confirms")
    check(B._STATE.get(VIP) is None, "state cleared by /cancel")
    reset()
    dispatch(msg_update("/cancel", uid=VIP))
    check("Nothing was in progress" in REC.last_text(),
          "/cancel with no state says so")


def t_multi_link_message_nonvip():
    print("\n— several links from a free user —")
    reset()
    B.DB.x("UPDATE users SET vip_until=0, quota_used=0, last_req_at=0 WHERE user_id=?",
           (FREE,))
    asked = stub_resolver()
    dispatch(msg_update("https://gplinks.co/aaa https://gplinks.co/bbb"))
    check("Batch mode is a <b>VIP</b> feature" in " ".join(REC.texts()),
          "free user told batch is VIP-only")
    check(len(asked) == 1, "only the FIRST link resolved for a free user", str(asked))


def t_broadcast_fsm():
    print("\n— /broadcast FSM —")
    reset()
    reset_state()
    B.DB.upsert_user(ADMIN, "admin", "Admin", ADMIN)
    B.DB.set_admin(ADMIN, True)
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    dispatch(msg_update("/broadcast", uid=ADMIN))
    check(B._STATE.get(ADMIN, {}).get("state") == B.BotStates.broadcasting,
          "broadcast state armed")
    check("Send the message to broadcast" in REC.last_text(), "prompt shown")

    reset()
    dispatch(msg_update("hello everyone", uid=ADMIN))
    sent = [c for c in REC.of("sendMessage") if c["chat_id"] != ADMIN]
    check(any("📣 hello everyone" in c["text"] for c in sent),
          "next message broadcast to the audience", str([c["text"] for c in sent]))
    check("Broadcast done" in " ".join(REC.texts()), "summary reported")
    check(B._STATE.get(ADMIN) is None, "broadcast state cleared")

    reset()
    dispatch(msg_update("/broadcast direct message text", uid=ADMIN))
    sent = [c for c in REC.of("sendMessage") if c["chat_id"] != ADMIN]
    check(any("direct message text" in c["text"] for c in sent),
          "/broadcast <text> works inline too")


def t_broadcast_denied_nonadmin():
    print("\n— /broadcast by a non-admin —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    dispatch(msg_update("/broadcast", uid=FREE))
    check("administrators only" in REC.last_text(), "non-admin denied")
    check(B._STATE.get(FREE) is None, "no state armed for a non-admin")


def t_callbacks():
    print("\n— inline callbacks —")
    grant_vip(VIP)
    r = B.ResolveOutcome(input_url="https://gplinks.co/BFfOX", ok=True,
                         final_url="https://real.example/dest",
                         shortener="gplinks.co", handler="gplinks",
                         elapsed_ms=900,
                         hops=[B.Hop(url="https://gplinks.co/x", kind="redirect",
                                     note="302")])
    key = B.CACHE.put(r, "https://gplinks.co/BFfOX")

    reset()
    dispatch(cb_update(f"copy:{key}", uid=VIP))
    a = REC.of("answerCallbackQuery")
    check(bool(a) and "Copied" in (a[0].get("text") or ""), "copy: answers the query")
    check(any("https://real.example/dest" in t for t in REC.texts()),
          "copy: posts the destination")

    reset()
    dispatch(cb_update(f"chain:{key}", uid=VIP))
    check(any("Redirect chain" in t for t in REC.texts()), "chain: shows the chain")
    check(any("final destination" in t for t in REC.texts()),
          "chain: marks the final hop")

    reset()
    asked = stub_resolver()
    dispatch(cb_update(f"again:{key}", uid=VIP))
    check(asked == ["https://gplinks.co/BFfOX"], "again: re-resolves the ORIGINAL url",
          str(asked))

    reset()
    dispatch(cb_update("menu:sites", uid=VIP))
    check(any("shorteners I handle natively" in t for t in REC.texts()),
          "menu:sites works")
    check(bool(REC.of("answerCallbackQuery")), "menu:sites answers the query")

    reset()
    dispatch(cb_update("menu:help", uid=VIP))
    check(any("troubleshooting" in t for t in REC.texts()), "menu:help works")

    reset()
    dispatch(cb_update("upsell", uid=VIP))
    check(any("VIP" in t for t in REC.texts()), "upsell shows plans")

    reset()
    dispatch(cb_update("noop:vip", uid=VIP))
    check(bool(REC.of("answerCallbackQuery")), "noop:vip just acknowledges")
    check(not REC.texts(), "noop:vip sends no message")


def t_callback_expired_key():
    print("\n— callback with an expired cache key —")
    reset()
    dispatch(cb_update("copy:doesnotexist", uid=VIP))
    a = REC.of("answerCallbackQuery")
    check(bool(a) and a[0].get("show_alert") is True, "expired key raises an alert")
    check("expired" in (a[0].get("text") or "").lower(), "alert explains it expired",
          str(a))


def t_callback_unknown_prefix_ignored():
    print("\n— callback with an unregistered prefix —")
    reset()
    dispatch(cb_update("bogus:x", uid=VIP))
    check(not REC.calls, "unknown callback data is ignored (no stray API calls)",
          str(REC.calls))


def t_vip_admin_ops():
    print("\n— VIP admin operations —")
    B.DB.upsert_user(ADMIN, "admin", "Admin", ADMIN)
    B.DB.set_admin(ADMIN, True)
    reset()
    dispatch(msg_update(f"/addvip {FREE} 15", uid=ADMIN))
    u = B.DB.get_user(FREE)
    check(u.is_vip, "/addvip granted VIP")
    check("VIP granted" in REC.last_text(), "/addvip confirms")
    check("15 day" in REC.last_text().lower() or u.vip_remaining().days >= 14,
          "/addvip honoured the 15-day argument", REC.last_text()[:120])

    reset()
    dispatch(msg_update(f"/addvip {FREE} perm", uid=ADMIN))
    check(B.DB.get_user(FREE).vip_until >= B.PERMANENT,
          "'perm' grants permanent VIP")

    reset()
    dispatch(msg_update(f"/whois {FREE}", uid=ADMIN))
    check("Your account" in REC.last_text(), "/whois renders the status template")

    reset()
    dispatch(msg_update(f"/delvip {FREE}", uid=ADMIN))
    check(not B.DB.get_user(FREE).is_vip, "/delvip revoked VIP")

    reset()
    dispatch(msg_update(f"/ban {FREE}", uid=ADMIN))
    check(B.DB.get_user(FREE).is_banned, "/ban set the flag")
    reset()
    dispatch(msg_update(f"/unban {FREE}", uid=ADMIN))
    check(not B.DB.get_user(FREE).is_banned, "/unban cleared the flag")

    reset()
    dispatch(msg_update("/addvip", uid=ADMIN))
    check("Usage:" in REC.last_text(), "/addvip with no target shows usage")

    reset()
    dispatch(msg_update(f"/ban {ADMIN}", uid=ADMIN))
    check("Admins cannot be banned" in REC.last_text(),
          "an admin cannot be banned", REC.last_text()[:120])
    reset()
    dispatch(msg_update(f"/delvip {ADMIN}", uid=ADMIN))
    check("cannot be revoked" in REC.last_text(),
          "an admin's VIP cannot be revoked", REC.last_text()[:120])


def t_redeem_flow():
    print("\n— /redeem —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    dispatch(msg_update("/redeem", uid=FREE))
    check("SHADOW-XXXXXX" in REC.last_text(), "/redeem with no code shows usage")

    code = B.DB.make_code(7, ADMIN, "test")
    reset()
    dispatch(msg_update(f"/redeem {code}", uid=FREE))
    check("VIP activated" in REC.last_text(), f"/redeem {code[:12]}… accepted",
          REC.last_text()[:120])
    check(B.DB.get_user(FREE).is_vip, "redeemer is VIP")

    reset()
    dispatch(msg_update(f"/redeem {code}", uid=FREE))
    check("rejected" in REC.last_text().lower(), "a used code is rejected")

    reset()
    dispatch(msg_update("/redeem SHADOW-NOPE00", uid=FREE))
    check("rejected" in REC.last_text().lower(), "an unknown code is rejected")
    B.DB.revoke_vip(FREE)


def t_setdelay_and_delays():
    print("\n— /setdelay, /delays —")
    B.DB.upsert_user(ADMIN, "admin", "Admin", ADMIN)
    B.DB.set_admin(ADMIN, True)
    reset()
    dispatch(msg_update("/setdelay gplinks.co 12", uid=ADMIN))
    check("pinned" in REC.last_text(), "/setdelay confirms")
    check(abs(B.DB.get_delay("gplinks.co") - 12.0) < 0.01,
          "delay persisted as 12s", str(B.DB.get_delay("gplinks.co")))
    reset()
    dispatch(msg_update("/setdelay gplinks.co", uid=ADMIN))
    check("Usage:" in REC.last_text(), "/setdelay with 1 arg shows usage")
    reset()
    dispatch(msg_update("/setdelay gplinks.co notanumber", uid=ADMIN))
    check("must be a number" in REC.last_text(), "/setdelay rejects a non-numeric")
    reset()
    dispatch(msg_update("/delays", uid=ADMIN))
    check("gplinks.co" in REC.last_text(), "/delays lists the pinned delay")


def t_no_quoting_in_groups():
    print("\n— group chat (quoting behaviour) —")
    reset()
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    dispatch(msg_update("/help", uid=FREE, chat_id=-100200300,
                        extra={}))
    c = REC.of("sendMessage")[0]
    check(c["chat_id"] == -100200300, "reply goes to the group chat id")
    check(not c.get("reply_parameters"),
          "group replies are NOT quoted either", str(c.get("reply_parameters")))


def t_state_isolated_per_user():
    print("\n— state isolation between users —")
    reset()
    reset_state()
    grant_vip(VIP)
    B.DB.upsert_user(FREE, "someone", "Some One", FREE)
    B.DB.x("UPDATE users SET vip_until=? WHERE user_id=?",
           (int(time.time()) + 86400, FREE))
    dispatch(msg_update("/batch", uid=VIP))
    dispatch(msg_update("/batch", uid=FREE))
    dispatch(msg_update("https://gplinks.co/vip-only", uid=VIP))
    check(len(B._STATE[VIP]["data"]["urls"]) == 1, "VIP buffer has 1")
    check(len(B._STATE.get(FREE, {}).get("data", {}).get("urls", [])) == 0,
          "the other user's buffer is untouched (no cross-talk)")
    B._STATE.clear()


def t_error_handler_does_not_crash():
    print("\n— error handler —")
    reset()

    async def boom(update, context):
        raise RuntimeError("synthetic failure")

    async def go():
        # Its own Application, so the shared one is not polluted for later
        # tests. build_application() is not used because we want ONLY the
        # exploding handler to be reachable.
        app = Application.builder().bot(BOT).concurrent_updates(True).build()
        app.add_handler(MessageHandler(B.filters.TEXT, boom))
        app.add_error_handler(B.on_error)
        app._initialized = True
        try:
            await app.process_update(msg_update("anything at all"))
        except Exception as exc:                      # noqa: BLE001
            return exc
        return None

    exc = run(go())
    check(exc is None, "a raising handler does not escape process_update",
          f"{exc!r}")
    check(any("synthetic failure" in str(c.get("text", "")) for c in REC.calls)
          is False, "error handler logs instead of messaging the user")


def main() -> int:
    print("=" * 70)
    print("SHADOWLINKS · python-telegram-bot dispatch tests")
    print("=" * 70)
    tests = [v for k, v in sorted(globals().items()) if k.startswith("t_")]
    for t in tests:
        try:
            t()
        except Exception:
            global FAIL
            FAIL += 1
            FAILED.append(f"{t.__name__} (exception)")
            print(f"  \033[31mERROR\033[0m in {t.__name__}")
            traceback.print_exc()
    print("\n" + "=" * 70)
    if FAILED:
        print("failed:", ", ".join(FAILED))
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
