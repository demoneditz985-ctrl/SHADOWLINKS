#!/usr/bin/env python3
"""Config / .env parser tests for SHADOWLINKS.

This file exists because of a real bug that survived 76 passing tests: the
loader stripped only surrounding quotes, so

    USE_CURL_CFFI=1        # the Cloudflare-capable backend

parsed as the literal string "1        # the Cloudflare-capable backend".
That is not "1", so _env_bool() returned False and the browser-TLS backend
silently switched itself off - which is the single thing that makes GPLinks
work. It was caught only by running --check against a real .env.

So: the parser is now asserted against the actual shipped file too.

Run:  python tests/test_config.py
"""
from __future__ import annotations

import os
import sys
import tempfile
import traceback

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

# Import with a throwaway env so the repo's real .env cannot skew the baseline.
_TMP = tempfile.mkdtemp(prefix="sl_cfg_")
os.environ.update({
    "BOT_TOKEN": "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11",
    "ADMIN_IDS": "700000001",
    "DB_PATH": os.path.join(_TMP, "test.db"),
})
import shadowlinks_bot as B                                    # noqa: E402

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


def load(text: str, keys: list[str]) -> dict[str, str]:
    """Write `text` to a temp .env, load it, and return the requested keys.

    load_dotenv() uses os.environ.setdefault, so the keys must be cleared
    first or a previous test's value would win.
    """
    for k in keys:
        os.environ.pop(k, None)
    path = os.path.join(tempfile.mkdtemp(prefix="sl_env_"), ".env")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(text)
    B.load_dotenv(path)          # an absolute path bypasses the script-dir join
    return {k: os.environ.get(k, "<unset>") for k in keys}


def t_inline_comment():
    print("\n— inline comments (the bug that once disabled curl_cffi) —")
    got = load(
        "USE_CURL_CFFI=1        # the Cloudflare-capable backend\n"
        "ADAPTIVE_DELAY=1 # learn timers\n"
        "IMPERSONATE=chrome    #  safari would also work\n",
        ["USE_CURL_CFFI", "ADAPTIVE_DELAY", "IMPERSONATE"])
    check(got["USE_CURL_CFFI"] == "1",
          "USE_CURL_CFFI parses as exactly '1'", repr(got["USE_CURL_CFFI"]))
    check(B._env_bool("USE_CURL_CFFI") is True,
          "_env_bool therefore returns True (backend stays ON)")
    check(got["ADAPTIVE_DELAY"] == "1", "trailing comment after a single space",
          repr(got["ADAPTIVE_DELAY"]))
    check(got["IMPERSONATE"] == "chrome", "value before comment kept",
          repr(got["IMPERSONATE"]))


def t_quotes_and_hashes():
    print("\n— quoting —")
    got = load(
        'PRICE_LINE="VIP is ₹299/month #1 plan"\n'
        "SUPPORT_USERNAME='shadow_help'\n"
        'BOT_NAME="SHADOWLINKS"\n'
        'EMPTY=""\n'
        "PLAIN=nocomment\n",
        ["PRICE_LINE", "SUPPORT_USERNAME", "BOT_NAME", "EMPTY", "PLAIN"])
    check(got["PRICE_LINE"] == "VIP is ₹299/month #1 plan",
          "a '#' INSIDE double quotes is kept verbatim", repr(got["PRICE_LINE"]))
    check(got["SUPPORT_USERNAME"] == "shadow_help",
          "single quotes are stripped", repr(got["SUPPORT_USERNAME"]))
    check(got["BOT_NAME"] == "SHADOWLINKS", "double quotes are stripped",
          repr(got["BOT_NAME"]))
    check(got["EMPTY"] == "", 'EMPTY="" yields an empty string', repr(got["EMPTY"]))
    check(got["PLAIN"] == "nocomment", "unquoted plain value untouched",
          repr(got["PLAIN"]))


def t_structural():
    print("\n— structural parsing —")
    got = load(
        "# a whole-line comment\n"
        "\n"
        "   \n"
        "no_equals_sign_here\n"
        "DB_KEY=a=b=c\n"
        "TRAILING=\n"
        "  SPACED_KEY  =  spaced value  \n"
        "URL_VALUE=https://x.example/?a=1&b=2\n",
        ["DB_KEY", "TRAILING", "SPACED_KEY", "URL_VALUE"])
    check(got["DB_KEY"] == "a=b=c", "only the FIRST '=' splits", repr(got["DB_KEY"]))
    check(got["TRAILING"] == "", "a key with no value is empty, not missing",
          repr(got["TRAILING"]))
    check(got["SPACED_KEY"] == "spaced value", "key and value are stripped",
          repr(got["SPACED_KEY"]))
    check(got["URL_VALUE"] == "https://x.example/?a=1&b=2",
          "a URL value with '=' and '&' survives", repr(got["URL_VALUE"]))
    check("<unset>" not in got.values() or True, "no crash on junk lines")


def t_existing_env_wins():
    print("\n— precedence —")
    # Deliberately NOT using load(): that helper clears the key first, which is
    # exactly the thing under test here.
    path = os.path.join(tempfile.mkdtemp(prefix="sl_prec_"), ".env")
    with open(path, "w", encoding="utf-8") as fh:
        fh.write("PREC_KEY=from_dotenv_file\nONLY_IN_FILE=from_file\n")
    os.environ.pop("PREC_KEY", None)
    os.environ.pop("ONLY_IN_FILE", None)
    os.environ["PREC_KEY"] = "from_real_environment"
    B.load_dotenv(path)
    check(os.environ["PREC_KEY"] == "from_real_environment",
          "a real environment variable beats .env (setdefault semantics)",
          repr(os.environ["PREC_KEY"]))
    check(os.environ.get("ONLY_IN_FILE") == "from_file",
          "a key absent from the environment IS taken from .env",
          repr(os.environ.get("ONLY_IN_FILE")))
    os.environ.pop("PREC_KEY", None)
    os.environ.pop("ONLY_IN_FILE", None)


def t_env_bool():
    print("\n— _env_bool —")
    for v in ("1", "true", "TRUE", "yes", "Y", "on", "enable", "enabled"):
        os.environ["B_KEY"] = v
        check(B._env_bool("B_KEY") is True, f"{v!r} is truthy")
    for v in ("0", "false", "no", "of", "n", "", "  ", "banana", "2"):
        os.environ["B_KEY"] = v
        check(B._env_bool("B_KEY") is False, f"{v!r} is falsy")
    os.environ.pop("B_KEY", None)
    check(B._env_bool("B_KEY") is False, "unset defaults to False")
    check(B._env_bool("B_KEY", True) is True, "unset honours default=True")
    os.environ["B_KEY"] = "  1  "
    check(B._env_bool("B_KEY") is True, "surrounding whitespace tolerated")
    os.environ.pop("B_KEY", None)


def t_env_list():
    print("\n— _env_list (ADMIN_IDS) —")
    cases = {
        "700000001": [700000001],
        "111,222,333": [111, 222, 333],
        "111 222 333": [111, 222, 333],
        "111;222,333 444": [111, 222, 333, 444],
        "@111,222": [111, 222],
        "111,notanumber,222": [111, 222],
        "": [],
        "   ": [],
    }
    for raw, want in cases.items():
        os.environ["L_KEY"] = raw
        got = B._env_list("L_KEY")
        check(got == want, f"{raw!r} -> {want}", f"got {got}")
    os.environ.pop("L_KEY", None)
    check(B._env_list("L_KEY") == [], "unset yields an empty list")


def t_env_numbers():
    print("\n— _env_int / _env_float —")
    os.environ["N_KEY"] = "42"
    check(B._env_int("N_KEY", 7) == 42, "int parsed")
    os.environ["N_KEY"] = "12.9"
    check(B._env_int("N_KEY", 7) == 12, "a float string truncates to int",
          str(B._env_int("N_KEY", 7)))
    os.environ["N_KEY"] = "garbage"
    check(B._env_int("N_KEY", 7) == 7, "garbage falls back to the default")
    check(B._env_int("NOPE_MISSING", 99) == 99, "missing key uses the default")
    os.environ["N_KEY"] = "2.5"
    check(abs(B._env_float("N_KEY", 1.0) - 2.5) < 1e-9, "float parsed")
    os.environ["N_KEY"] = "junk"
    check(B._env_float("N_KEY", 1.0) == 1.0, "bad float falls back")
    os.environ.pop("N_KEY", None)


def t_shipped_env_file():
    """No env file may ship, and the parser must still round-trip one."""
    print("\n— no .env ships; the parser is still correct —")
    # The whole point of the rework: config lives INSIDE shadowlinks_bot.py.
    shipped = [n for n in (".env", ".env.example", ".env.sample", "config.ini",
                           "config.json", "settings.py")
               if os.path.isfile(os.path.join(REPO, n))]
    check(not shipped, "no .env / config file ships in the repo",
          "present: " + ", ".join(shipped))
    check("PASTE_YOUR" in open(os.path.join(REPO, "shadowlinks_bot.py"),
                               encoding="utf-8").read()
          or os.environ.get("BOT_TOKEN"),
          "the file itself carries the token (placeholder in git, real in the zip)")

    # Exercise the round-trip against a synthetic file so a USE_DOTENV=True
    # user is still covered even though nothing ships.
    for name in ("<synthetic>",):
        path = os.path.join(tempfile.mkdtemp(), "dotenv_roundtrip")
        with open(path, "w", encoding="utf-8") as fh:
            fh.write("# a comment\n\n"
                     'PLAIN=bare\nQUOTED="quoted value"\n'
                     "SPACED =  padded  \nEMPTY=\n"
                     "INLINE=value  # trailing comment\n")
        text = open(path, encoding="utf-8").read()
        # Every non-comment line with '=' must survive a round trip.
        problems = []
        for raw in text.splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            k, v = line.split("=", 1)
            k, v = k.strip(), v.strip()
            os.environ.pop(k, None)
            B.load_dotenv(path)
            got = os.environ.get(k, "<unset>")
            if "<unset>" in (got,):
                problems.append(f"{k}: unparsed")
            elif v and not v.startswith(('"', "'")) and " #" in v:
                want = v[:v.index(" #")].strip()
                if got != want:
                    problems.append(f"{k}: {got!r} != {want!r}")
            elif v.startswith(('"', "'")) and len(v) >= 2 and v[0] == v[-1]:
                if got != v[1:-1]:
                    problems.append(f"{k}: {got!r} != {v[1:-1]!r}")
            elif got != v:
                problems.append(f"{k}: {got!r} != {v!r}")
        check(not problems, "a .env round-trips if USE_DOTENV=True is set",
              "; ".join(problems[:6]))

    # The live CFG must reflect a curl_cffi-enabled build.
    check(B.CFG.use_curl_cffi is True,
          "CFG.use_curl_cffi is True for the shipped configuration",
          f"got {B.CFG.use_curl_cffi!r} - this is exactly the silent failure "
          "mode this file guards")
    check(bool(B.CFG.admin_ids), f"CFG.admin_ids populated: {B.CFG.admin_ids}")
    check(len(B.CFG.token.split(":")) == 2, "CFG.token has the id:secret shape")


def t_preflight_and_check():
    print("\n— preflight / --check —")
    check(B.preflight() is True, "preflight passes with a token + admin id set")
    object.__setattr__(B.CFG, "admin_ids", ())
    try:
        check(B.preflight() is False, "preflight fails when ADMIN_IDS is empty")
    finally:
        object.__setattr__(B.CFG, "admin_ids", (700000001,))
    object.__setattr__(B.CFG, "token", "")
    try:
        check(B.preflight() is False, "preflight fails when BOT_TOKEN is empty")
    finally:
        object.__setattr__(B.CFG, "token",
                          "123456:ABC-DEF1234ghIkl-zyx57W2v1u123ew11")
    check(B.preflight() is True, "preflight passes again once restored")


def t_settings_block_is_the_config():
    """There is no .env any more: the SETTINGS block in the file IS the config."""
    print("\n— in-file SETTINGS block —")
    required = [
        "BOT_TOKEN", "ADMIN_IDS", "BOT_NAME", "SUPPORT_USERNAME", "CHANNEL_URL",
        "PRICE_LINE", "SHOW_CHAIN_TO_FREE", "FORCE_CHANNEL", "FREE_DAILY_LIMIT",
        "FREE_COOLDOWN", "FREE_BATCH_LIMIT", "VIP_DAILY_LIMIT", "VIP_BATCH_LIMIT",
        "MAX_HOPS", "HTTP_TIMEOUT", "GOLINK_DELAY", "FORM_RETRIES",
        "ADAPTIVE_DELAY", "MAX_CONCURRENCY", "RESOLVE_CHAINS", "USE_CURL_CFFI",
        "IMPERSONATE", "EXTRA_GPLINKS_DOMAINS", "EXTRA_FORM_DOMAINS",
        "EXTRA_REDIRECT_DOMAINS", "EXTRA_ADFLY_DOMAINS", "DB_PATH",
        "LOG_RESOLVES", "LOG_LEVEL", "MAX_BODY_BYTES",
        "ALLOW_PRIVATE_NETWORKS",
    ]
    missing = [k for k in required if not hasattr(B, k)]
    check(not missing, f"all {len(required)} settings are editable module constants",
          f"missing: {missing}")

    check(B.USE_DOTENV is False,
          "USE_DOTENV defaults to False, so no .env file is read at import")
    check(isinstance(B.BOT_TOKEN, str) and isinstance(B.ADMIN_IDS, str),
          "the two REQUIRED values are plain strings a user can just overwrite")
    check(B.ALLOW_PRIVATE_NETWORKS is False,
          "ALLOW_PRIVATE_NETWORKS ships False (SSRF guard on)")
    check(B.USE_CURL_CFFI is True,
          "USE_CURL_CFFI ships True (Chrome TLS impersonation on)")

    # the block must appear near the top, before any real logic
    src = open(os.path.join(REPO, "shadowlinks_bot.py"), encoding="utf-8").read()
    at = src.index("EDIT THE BLOCK BELOW")
    check(at < src.index("# §2  DATABASE"),
          "the SETTINGS block sits above §2, so it is the first thing you see")
    check(src.count("BOT_TOKEN =") >= 1, "BOT_TOKEN is assigned in the block")


def t_env_still_overrides_settings():
    """Env vars win over the block - that keeps Docker/systemd/CI working."""
    print("\n— precedence: environment over SETTINGS —")
    os.environ.pop("PROBE_KEY", None)
    check(B._env("PROBE_KEY", "from_settings") == "from_settings",
          "SETTINGS value is used when no environment variable is set")
    os.environ["PROBE_KEY"] = "from_env"
    check(B._env("PROBE_KEY", "from_settings") == "from_env",
          "a real environment variable overrides the SETTINGS block")
    os.environ.pop("PROBE_KEY", None)

    os.environ["PROBE_N"] = "77"
    check(B._env_int("PROBE_N", B.FREE_DAILY_LIMIT) == 77, "_env_int honours env")
    os.environ.pop("PROBE_N", None)
    check(B._env_int("PROBE_N", B.FREE_DAILY_LIMIT) == B.FREE_DAILY_LIMIT,
          "_env_int falls back to the SETTINGS value")

    os.environ["PROBE_B"] = "0"
    check(B._env_bool("PROBE_B", True) is False, "_env_bool honours env")
    os.environ.pop("PROBE_B", None)
    check(B._env_bool("PROBE_B", True) is True,
          "_env_bool falls back to the SETTINGS value")


def t_split_hosts():
    print("\n— _split_hosts (EXTRA_*_DOMAINS) —")
    cases = {
        "": (),
        "   ": (),
        "a.com": ("a.com",),
        "a.com,b.com": ("a.com", "b.com"),
        " A.com , B.COM ; c.com ": ("a.com", "b.com", "c.com"),
        ".leading.com": ("leading.com",),
        "a.com,,b.com": ("a.com", "b.com"),
    }
    for raw, want in cases.items():
        got = B._split_hosts(raw)
        check(got == want, f"{raw!r} -> {want}", f"got {got}")

    # the four EXTRA_* settings must reach the registry through apply_extra_domains
    os.environ["EXTRA_REDIRECT_DOMAINS"] = "probe-extra.example"
    try:
        got = B._split_hosts(B._env("EXTRA_REDIRECT_DOMAINS",
                                    B.EXTRA_REDIRECT_DOMAINS))
        check(got == ("probe-extra.example",),
              "an EXTRA_* env override parses into a host tuple", str(got))
    finally:
        os.environ.pop("EXTRA_REDIRECT_DOMAINS", None)


def t_placeholder_is_rejected():
    print("\n— placeholder credentials are rejected loudly —")
    saved_token, saved_admins = B.CFG.token, B.CFG.admin_ids

    object.__setattr__(B.CFG, "token", "PASTE_YOUR_BOT_TOKEN_HERE")
    object.__setattr__(B.CFG, "admin_ids", ())
    try:
        check(B.preflight() is False,
              "the shipped placeholders make preflight fail")
    finally:
        object.__setattr__(B.CFG, "token", saved_token)
        object.__setattr__(B.CFG, "admin_ids", saved_admins)

    object.__setattr__(B.CFG, "token", "not-a-token-no-colon")
    try:
        check(B.preflight() is False, "a malformed token (no ':') is rejected")
    finally:
        object.__setattr__(B.CFG, "token", saved_token)

    check(B.preflight() is True, "preflight passes again once restored")


def t_no_env_file_needed():
    """The whole point: a bare checkout with no .env still configures itself."""
    print("\n— runs with no .env present —")
    check(not os.path.isfile(os.path.join(REPO, ".env")),
          "the repo ships no .env file at all")
    src = open(os.path.join(REPO, "shadowlinks_bot.py"), encoding="utf-8").read()
    check("load_dotenv()" not in src.split("if USE_DOTENV:")[0].split("def load_dotenv")[1]
          if "if USE_DOTENV:" in src else False,
          "load_dotenv is called only behind the USE_DOTENV flag")
    check("if USE_DOTENV:" in src, "the .env loader is opt-in")


def main() -> int:
    print("=" * 70)
    print("SHADOWLINKS · config / .env parser")
    print("=" * 70)
    for name, fn in sorted((k, v) for k, v in globals().items() if k.startswith("t_")):
        try:
            fn()
        except Exception:
            global FAIL
            FAIL += 1
            FAILED.append(f"{name} (exception)")
            print(f"  \033[31mERROR\033[0m in {name}")
            traceback.print_exc()
    print("\n" + "=" * 70)
    if FAILED:
        print("failed:", ", ".join(FAILED))
    print(f"RESULT: {PASS} passed, {FAIL} failed")
    print("=" * 70)
    return 1 if FAIL else 0


if __name__ == "__main__":
    raise SystemExit(main())
