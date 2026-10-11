#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  SHADOWLINKS — Termux one-shot installer
#
#      bash install-termux.sh
#
#  This script exists to fix the three things that actually go wrong on a
#  phone, in the order people hit them:
#
#  1. WRONG PACKAGE.  `pip install telegram` installs an unrelated 2013
#     name-squat whose entire source is `print("coming soon...")`. It shadows
#     the real library, so the bot dies with
#         ImportError: cannot import name 'Bot' from 'telegram'
#     The framework is `python-telegram-bot`. Step 5 detects the squat,
#     removes it, and installs the right thing.
#
#  2. SHARED STORAGE.  Unzipping into /sdcard/Download or
#     /storage/emulated/0 puts the project on a FUSE filesystem with no
#     symlink or execute-bit support. `python -m venv` needs both, so venv
#     creation fails there — and the SQLite database is unreliable too.
#     Step 1 detects this and tells you to move the folder into ~.
#
#  3. VENV BOOTSTRAP.  Even in the right place, Termux's ensurepip sometimes
#     fails. Step 4 tries venv, then venv-without-pip, then falls back to
#     Termux's own Python — which is already an isolated environment, so
#     installing into it is normal and safe there.
#
#  Nothing else here is exotic: python-telegram-bot's dependency tree is 11 of
#  13 packages pure Python, with no pydantic, no aiohttp, no Rust and no
#  third-party wheel index. Only curl_cffi needs a platform wheel (android_24
#  cp313/cp314) and cffi compiles from C in seconds.
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$HERE" || exit 1

C_R=$'\033[1;31m'; C_G=$'\033[1;32m'; C_Y=$'\033[1;33m'
C_C=$'\033[1;36m'; C_B=$'\033[1m';  C_0=$'\033[0m'
say()  { printf '\n%s==>%s %s\n' "$C_C" "$C_0" "$*"; }
ok()   { printf '    %s✓%s %s\n' "$C_G" "$C_0" "$*"; }
warn() { printf '    %s!%s %s\n' "$C_Y" "$C_0" "$*"; }
die()  { printf '\n%s✗ %s%s\n\n' "$C_R" "$*" "$C_0" >&2; exit 1; }

printf '%s🕶  SHADOWLINKS — Termux one-shot installer%s\n' "$C_B" "$C_0"

# ── 1. shared storage: the most common reason venv cannot be created ──────
say "Checking where this folder lives"
case "$HERE" in
  /storage/*|/sdcard/*|*/storage/emulated/*)
      printf '%s\n' "
    This folder is on ANDROID SHARED STORAGE:
        $HERE

    That filesystem (FUSE/sdcardfs) does not support symlinks or execute
    bits, which is why 'python -m venv' fails here — and SQLite is
    unreliable on it too. This is not a Termux bug and no pip flag fixes it.

    Move the project into Termux's own home directory:

        mkdir -p ~/SHADOWLINKS
        cp -r \"$HERE\"/. ~/SHADOWLINKS/
        cd ~/SHADOWLINKS
        bash install-termux.sh

    (If cp complains about permissions, run 'termux-setup-storage' once,
     grant the prompt, and try again.)
" >&2
      die "refusing to install on shared storage - move the folder to ~ first"
      ;;
  *)
      ok "$HERE"
      ;;
esac

case "${PREFIX:-}" in
  *com.termux*) ok "Termux detected ($PREFIX)" ;;
  *) warn "PREFIX does not look like Termux. On normal Linux you do not need
      any of this - just:  pip install -r requirements.txt" ;;
esac

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64)  ok "arch aarch64 -> android_24_arm64_v8a wheels" ;;
  armv7l|armv8l)  ok "arch armv7   -> android_24_armeabi_v7a wheels" ;;
  x86_64|amd64)   ok "arch x86_64  -> android_24_x86_64 wheels" ;;
  i686|x86)       ok "arch x86     -> android_24_x86 wheels" ;;
  *)              warn "unknown arch '$ARCH' - letting pip decide" ;;
esac

# ── 2. system packages ────────────────────────────────────────────────────
# build-essential + libffi are only for cffi, curl_cffi's one compiled
# dependency: a small C build, but it does need a compiler.
say "Installing system packages"
pkg update -y && pkg upgrade -y || die "pkg update failed - check your network"
pkg install -y python build-essential libffi openssl unzip ||
    die "pkg install failed"
command -v python >/dev/null || die "python not on PATH after install"

PY="${PY:-python}"
ok "python, build-essential, libffi, openssl, unzip  ($("$PY" --version 2>&1))"

# ── 3. Python version - the one remaining real gotcha ─────────────────────
say "Checking Python (curl_cffi's Android wheel is cp313/cp314 only)"
"$PY" --version
PYTAG="$("$PY" -c 'import sys;print("cp%d%d"%(sys.version_info[0],sys.version_info[1]))')"
case "$PYTAG" in
  cp313|cp314|cp315)
      ok "$PYTAG - curl_cffi has a prebuilt android_24 wheel for this" ;;
  *)
      warn "$PYTAG has no prebuilt curl_cffi wheel for Android."
      warn "Everything else installs (all pure Python), but without curl_cffi"
      warn "there is no Chrome TLS impersonation, so Cloudflare-protected"
      warn "shorteners - GPLINKS INCLUDED - will return 403."
      warn ""
      warn "Fix it by upgrading Termux's Python:"
      printf '        pkg update -y && pkg upgrade -y && pkg install -y python\n'
      printf '        python --version        # want 3.13.x or 3.14.x\n'
      printf '      then re-run:  bash install-termux.sh\n\n'
      warn "If pkg will not move you there, get 3.13 from the tur repo:"
      printf '        pkg install -y tur-repo && pkg install -y python3.13\n'
      printf '      then:  PY=python3.13 bash install-termux.sh\n\n'
      [ -n "${FORCE_OLD_PY:-}" ] || die "Python $PYTAG cannot get curl_cffi on Android.
      Upgrade Python as shown above, or set FORCE_OLD_PY=1 to install anyway
      and accept that GPLinks will 403."
      warn "FORCE_OLD_PY set - continuing without a working curl_cffi" ;;
esac

# ── 4. virtualenv, with fallbacks ─────────────────────────────────────────
say "Setting up an environment"
PYBIN=""; PIPCMD=""

use_system_python() {
  # Termux is ALREADY an isolated environment (its own $PREFIX, its own
  # filesystem), so installing into its Python is normal and safe there -
  # this is what pkg/pip are for. Not the advice you would give on a desktop.
  PYBIN="$PY"
  warn "using Termux's own Python instead of a venv"
  warn "(on Termux that is fine - it is already an isolated environment)"
}

if [ -x .venv/bin/python ]; then
  PYBIN=".venv/bin/python"
  ok "reusing the existing .venv"
else
  if "$PY" -m venv .venv >/tmp/sl_venv.log 2>&1 && [ -x .venv/bin/python ]; then
    PYBIN=".venv/bin/python"
    ok ".venv created"
  else
    warn "'python -m venv' failed:"
    sed 's/^/        /' /tmp/sl_venv.log 2>/dev/null | tail -8
    rm -rf .venv
    warn "retrying with --without-pip, then bootstrapping pip"
    if "$PY" -m venv --without-pip .venv >/tmp/sl_venv2.log 2>&1 \
       && [ -x .venv/bin/python ]; then
      PYBIN=".venv/bin/python"
      if "$PYBIN" -m ensurepip --upgrade >/dev/null 2>&1 \
         || "$PYBIN" -c "import pip" >/dev/null 2>&1; then
        ok ".venv created (--without-pip), pip bootstrapped"
      else
        warn "no pip inside the venv and ensurepip is unavailable"
        rm -rf .venv; PYBIN=""
      fi
    else
      sed 's/^/        /' /tmp/sl_venv2.log 2>/dev/null | tail -6
      rm -rf .venv; PYBIN=""
    fi
    [ -n "$PYBIN" ] || use_system_python
  fi
fi

ensure_pip() {
  "$PYBIN" -m pip --version >/dev/null 2>&1 && return 0
  warn "pip is not available for $PYBIN - bootstrapping"
  "$PYBIN" -m ensurepip --upgrade >/dev/null 2>&1 && return 0
  die "could not get pip working for $PYBIN.
      Try:  pkg install -y python-pip"
}
ensure_pip
"$PYBIN" -m pip install -q --upgrade pip wheel setuptools >/dev/null 2>&1 ||
    warn "pip self-upgrade failed (usually harmless, continuing)"
ok "pip: $("$PYBIN" -m pip --version 2>&1 | cut -d' ' -f1-2)"

# pip needs either a venv or, on an externally-managed system Python, the
# override flag. Work it out once.
PIPX=()
# A venv reports the BASE interpreter's stdlib, which carries an
# EXTERNALLY-MANAGED marker it does not itself enforce - so only check the
# marker when we are NOT in a venv (sys.prefix == sys.base_prefix).
PEP668="$("$PYBIN" -c '
import sys, sysconfig, os
if sys.prefix != getattr(sys, "base_prefix", sys.prefix):
    print("venv")
else:
    p = os.path.join(sysconfig.get_path("stdlib"), "EXTERNALLY-MANAGED")
    print("managed" if os.path.isfile(p) else "plain")
' 2>/dev/null)"
if [ "$PEP668" = "managed" ]; then
  PIPX=(--break-system-packages)
  warn "this Python is PEP 668 'externally managed' - adding --break-system-packages"
else
  ok "pip target: ${PEP668:-unknown} (no override flag needed)"
fi

# ── 5. the wrong 'telegram' package ───────────────────────────────────────
say "Checking for the name-squatted 'telegram' package"
WRONG="$("$PYBIN" - <<'PYEOF'
import importlib.util as u
spec = u.find_spec("telegram")
if spec is None:
    print("absent")
else:
    try:
        import telegram
        print("real" if hasattr(telegram, "Bot") else "SQUATTED:" + str(getattr(telegram, "__file__", "?")))
    except Exception as e:
        print("SQUATTED:" + str(e))
PYEOF
)"
case "$WRONG" in
  absent)   ok "nothing installed yet - good" ;;
  real)     ok "python-telegram-bot is already present" ;;
  SQUATTED*)
      warn "found the WRONG package: ${WRONG#SQUATTED:}"
      warn "this is what causes: ImportError: cannot import name 'Bot'"
      "$PYBIN" -m pip uninstall -y telegram ${PIPX[@]+${PIPX[@]+"${PIPX[@]}"}} >/dev/null 2>&1
      # pip's metadata can survive the package (legacy setup.py install), and
      # the leftover telegram/ directory is what actually shadows the real
      # library. Delete it only when the resolved path is a real site-packages.
      SP="$("$PYBIN" -c 'import site;print(site.getsitepackages()[0])' 2>/dev/null)"
      case "$SP" in
        */site-packages|*/dist-packages)
            if [ -d "$SP/telegram" ]; then
              rm -rf "$SP/telegram" "$SP"/telegram-*.egg-info
              ok "removed the leftover $SP/telegram directory"
            fi ;;
        *)  warn "could not resolve site-packages; relying on pip uninstall" ;;
      esac
      "$PYBIN" -m pip uninstall -y telegram ${PIPX[@]+${PIPX[@]+"${PIPX[@]}"}} >/dev/null 2>&1
      # Confirm the shadow is really gone before moving on.
      if "$PYBIN" -c 'import telegram' 2>/dev/null; then
        warn "'telegram' still imports - check: $PYBIN -c \"import telegram;print(telegram.__file__)\""
      else
        ok "removed the squatted 'telegram'"
      fi
      ;;
esac

# ── 6. the real dependencies ──────────────────────────────────────────────
say "Installing python-telegram-bot, beautifulsoup4, httpx"
say "  (all pure Python - no compiler, no Rust, no wheel index needed)"
"$PYBIN" -m pip install ${PIPX[@]+"${PIPX[@]}"} -U \
    "python-telegram-bot>=22,<23" beautifulsoup4 httpx \
    || die "core install failed - see the pip output above"
ok "python-telegram-bot $("$PYBIN" -c 'import telegram;print(telegram.__version__)' 2>/dev/null || echo '?')"

# The assertion that matters. If this fails, nothing downstream can work, and
# the message has to say which of the two packages is at fault.
say "Verifying the framework really imports"
"$PYBIN" - <<'PYEOF' || die "the framework still will not import.
  Run these two lines by hand and paste the output into the chat:
      $PYBIN -m pip uninstall -y telegram
      $PYBIN -m pip install -U --force-reinstall python-telegram-bot"
from telegram import Bot, Update, InlineKeyboardMarkup
from telegram.ext import Application, CommandHandler, filters
print("    from telegram import Bot  ✓")
PYEOF

# curl_cffi separately: the only package needing a platform wheel, so
# --only-binary makes pip FAIL LOUDLY rather than attempt a C build that has
# historically not worked against bionic.
say "Installing curl_cffi (Chrome TLS impersonation - this beats Cloudflare)"
CFFI=0
if "$PYBIN" -m pip install ${PIPX[@]+"${PIPX[@]}"} --only-binary=curl_cffi curl_cffi \
        >/tmp/sl_cffi.log 2>&1; then
  if "$PYBIN" -c "from curl_cffi.requests import AsyncSession" 2>/dev/null; then
    CFFI=1; ok "curl_cffi working - GPLinks bypass available"
  else
    warn "curl_cffi installed but will not import:"
    sed 's/^/        /' /tmp/sl_cffi.log | tail -5
  fi
else
  warn "curl_cffi did not install from a wheel (you are on Python $PYTAG)."
  sed 's/^/        /' /tmp/sl_cffi.log 2>/dev/null | tail -6
  warn "The bot STILL RUNS on the httpx fallback, but without Chrome TLS"
  warn "impersonation Cloudflare sites - GPLINKS INCLUDED - will 403."
  warn "Fix: upgrade to Python 3.13/3.14 (step 3), then re-run this."
fi

# ── 7. configuration now lives INSIDE shadowlinks_bot.py ──────────────────
say "Checking the settings block in shadowlinks_bot.py"
if [ ! -f shadowlinks_bot.py ]; then
  warn "shadowlinks_bot.py is not in $HERE"
  warn "Copy it here first, or clone the repo:"
  printf '        pkg install -y git\n'
  printf '        git clone -b arena/82f542e9-shadowlinks \\\n'
  printf '          https://github.com/demoneditz985-ctrl/SHADOWLINKS.git .\n'
else
  # There is no .env any more - the token and admin id are edited in the file.
  if grep -qE '^BOT_TOKEN *= *"PASTE_YOUR_BOT_TOKEN_HERE"' shadowlinks_bot.py; then
      warn "BOT_TOKEN is still the placeholder."
      warn "Open shadowlinks_bot.py and set the two values near the top:"
      printf '        nano shadowlinks_bot.py\n'
      printf '      line ~100:  BOT_TOKEN = "123456789:AA..."\n'
      printf '      line ~105:  ADMIN_IDS = "your numeric user id"\n'
      warn "Get the token from @BotFather and your id from @userinfobot."
  elif grep -qE '^BOT_TOKEN *= *"[0-9]+:' shadowlinks_bot.py; then
      ok "BOT_TOKEN is set"
      grep -qE '^ADMIN_IDS *= *"[0-9]+' shadowlinks_bot.py \
        && ok "ADMIN_IDS is set" \
        || warn "ADMIN_IDS still looks like a placeholder - set it in the file"
  else
      warn "could not find a BOT_TOKEN line - check the top of the file"
  fi
fi

# ── 8. verify ─────────────────────────────────────────────────────────────
if [ -f shadowlinks_bot.py ]; then
  say "Importing the bot"
  "$PYBIN" - <<'PYEOF' || die "import check failed - see above"
import shadowlinks_bot as B, telegram, bs4
print(f"    framework    : python-telegram-bot {telegram.__version__}")
print(f"    shorteners   : {len(B.REGISTRY)} domains registered")
print(f"    http backend : {'curl_cffi (Chrome TLS)' if B.HTTP._cffi_ok else 'httpx - NO Cloudflare bypass'}")
app = B.build_application()
print(f"    handlers     : {sum(len(v) for v in app.handlers.values())} registered")
PYEOF
  say "Running the bot's own self-check"
  "$PYBIN" shadowlinks_bot.py --check || die "--check failed"
fi

# ── done ──────────────────────────────────────────────────────────────────
printf '\n%s\n' "═══════════════════════════════════════════════════════════════════════"
if [ "$CFFI" = "1" ]; then
  printf '  %s✅  READY - curl_cffi active, GPLinks bypass available%s\n' "$C_G" "$C_0"
else
  printf '  %s⚠   INSTALLED, but without curl_cffi%s\n' "$C_Y" "$C_0"
  printf '  %s    Cloudflare sites (incl. GPLinks) will likely 403.%s\n' "$C_Y" "$C_0"
  printf '  %s    Upgrade to Python 3.13/3.14 and re-run to fix.%s\n' "$C_Y" "$C_0"
fi
printf '%s\n\n' "═══════════════════════════════════════════════════════════════════════"

if [ "$PYBIN" = ".venv/bin/python" ]; then RUN=".venv/bin/python shadowlinks_bot.py";
else RUN="python shadowlinks_bot.py"; fi

cat <<BANNER
  Start the bot:

      cd $HERE
      termux-wake-lock
      $RUN

  Survive closing Termux (tmux):

      pkg install -y tmux
      tmux new -s shadow
      termux-wake-lock
      cd $HERE && $RUN
        # Ctrl+B then D = detach       tmux attach -t shadow = return

  REQUIRED or Android kills it minutes after the screen turns off:
      Settings -> Apps -> Termux -> Battery -> UNRESTRICTED
      (Xiaomi/Samsung/Oppo/Vivo: also enable "Autostart")

  Then open your bot on Telegram, press Start, and paste:
      https://gplinks.co/BFfOX

  Admin:  /admin   /stats   /addvip <id> 30   /gencode 30 5

  Run the test suite any time (266 checks, no network needed):
      ${PYBIN} tests/test_config.py
      ${PYBIN} tests/test_resolver.py
      ${PYBIN} tests/test_ptb.py
═══════════════════════════════════════════════════════════════════════
BANNER
