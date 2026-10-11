#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  SHADOWLINKS — Termux one-shot installer
#
#      bash install-termux.sh
#
# ──────────────────────────────────────────────────────────────────────────
#  WHY THIS IS NOW A SHORT SCRIPT
# ──────────────────────────────────────────────────────────────────────────
#  It used to be long, because aiogram 3 depends on pydantic, and pydantic's
#  compiled core has almost no prebuilt Android (bionic) wheels. On Termux pip
#  fell through to building pydantic-core from Rust source — ~15 minutes, and
#  it usually ran the phone out of RAM. That was the error this installer
#  existed to fight.
#
#  The bot now runs on python-telegram-bot 22, which depends only on httpx.
#  Its whole tree is 11 of 13 packages pure Python:
#
#      python-telegram-bot, httpx, httpcore, h11, anyio, idna, certifi,
#      sniffio, typing_extensions, beautifulsoup4, soupsieve, pycparser
#
#  There is no pydantic anywhere in it, no aiohttp, no Rust, no maturin, and
#  no need for a third-party wheel index. The only two packages that need
#  compiling or a platform wheel are:
#
#      curl_cffi   has a prebuilt android_24 wheel (cp313 and cp314)
#      cffi        builds from C with clang in seconds — not Rust
#
#  So on a current Termux this installs in well under a minute.
#
#  The one thing that still bites: curl_cffi's Android wheel is built for
#  Python 3.13/3.14 only. An un-updated Termux on 3.12 gets no wheel, so step
#  3 below checks and tells you how to fix it rather than failing obscurely.
#
#  Verified: python-telegram-bot 22.8 / httpx 0.28.1 / curl_cffi 0.16.3 /
#  beautifulsoup4 4.15.0 — full test suite 266/266 passing.
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

# ── 1. environment ────────────────────────────────────────────────────────
say "Checking environment"
case "${PREFIX:-}" in
  *com.termux*) ok "Termux ($PREFIX)" ;;
  *) warn "PREFIX does not look like Termux. On normal Linux you do not need
      any of this — just:  pip install -r requirements.txt" ;;
esac

ARCH="$(uname -m)"
case "$ARCH" in
  aarch64|arm64)  ok "arch aarch64 -> android_24_arm64_v8a wheels" ;;
  armv7l|armv8l)  ok "arch armv7   -> android_24_armeabi_v7a wheels" ;;
  x86_64|amd64)   ok "arch x86_64  -> android_24_x86_64 wheels" ;;
  i686|x86)       ok "arch x86     -> android_24_x86 wheels" ;;
  *)              warn "unknown arch '$ARCH' — letting pip decide" ;;
esac

# ── 2. system packages ────────────────────────────────────────────────────
# build-essential + libffi are only needed for cffi, curl_cffi's one compiled
# dependency. It is a small C build, but it does need a compiler.
say "Installing system packages"
pkg update -y && pkg upgrade -y || die "pkg update failed — check your network"
pkg install -y python build-essential libffi openssl unzip ||
    die "pkg install failed"
ok "python, build-essential, libffi, openssl, unzip"
command -v python >/dev/null || die "python not on PATH after install"

# ── 3. Python version — this is the one real gotcha left ──────────────────
say "Checking Python (curl_cffi's Android wheel is cp313/cp314 only)"
python --version
PY="${PY:-python}"
PYTAG="$("$PY" -c 'import sys;print("cp%d%d"%(sys.version_info[0],sys.version_info[1]))')"

case "$PYTAG" in
  cp313|cp314|cp315)
      ok "$PYTAG — curl_cffi has a prebuilt android_24 wheel for this" ;;
  *)
      warn "$PYTAG has no prebuilt curl_cffi wheel for Android."
      warn "Everything ELSE still installs (all pure Python), but without"
      warn "curl_cffi there is no Chrome TLS impersonation, so Cloudflare-"
      warn "protected shorteners — GPLINKS INCLUDED — will return 403."
      warn ""
      warn "Fix it by upgrading Termux's Python:"
      printf '        pkg update -y && pkg upgrade -y\n'
      printf '        pkg install -y python\n'
      printf '        python --version        # want 3.13.x or 3.14.x\n'
      printf '      then re-run:  bash install-termux.sh\n\n'
      warn "If pkg will not move you there, get 3.13 from the tur repo:"
      printf '        pkg install -y tur-repo && pkg install -y python3.13\n'
      printf '      then:  PY=python3.13 bash install-termux.sh\n\n'
      [ -n "${FORCE_OLD_PY:-}" ] || die "Python $PYTAG cannot get curl_cffi on Android.
      Upgrade Python as shown above, or set FORCE_OLD_PY=1 to install anyway
      and accept that GPLinks will 403."
      warn "FORCE_OLD_PY set — continuing without a working curl_cffi" ;;
esac

# ── 4. virtualenv ─────────────────────────────────────────────────────────
say "Creating .venv"
if [ ! -x .venv/bin/python ]; then
  "$PY" -m venv .venv || die "could not create the venv"
fi
.venv/bin/pip install --upgrade pip wheel setuptools >/dev/null 2>&1 ||
    warn "pip self-upgrade failed (usually harmless, continuing)"
ok ".venv ready — $(.venv/bin/python --version 2>&1)"

PIP=".venv/bin/pip"

# ── 5. the dependencies ───────────────────────────────────────────────────
say "Installing python-telegram-bot, beautifulsoup4, httpx"
say "  (all pure Python — no compiler, no Rust, no wheel index needed)"
$PIP install python-telegram-bot beautifulsoup4 httpx ||
    die "core install failed"
ok "python-telegram-bot $(.venv/bin/python -c 'import telegram;print(telegram.__version__)' 2>/dev/null || echo '?')"

# curl_cffi separately: it is the only package here that needs a platform
# wheel, so --only-binary makes pip FAIL LOUDLY instead of trying to compile
# a C extension that has historically not built on bionic.
say "Installing curl_cffi (Chrome TLS impersonation — this beats Cloudflare)"
CFFI=0
if $PIP install --only-binary=curl_cffi curl_cffi >/dev/null 2>&1; then
  if .venv/bin/python -c "from curl_cffi.requests import AsyncSession" 2>/dev/null; then
    CFFI=1; ok "curl_cffi working — GPLinks bypass available"
  else
    warn "curl_cffi installed but will not import"
  fi
else
  warn "curl_cffi did not install from a wheel (you are on Python $PYTAG)."
  warn "The bot STILL RUNS on the httpx fallback, but without Chrome TLS"
  warn "impersonation Cloudflare-protected sites — GPLINKS INCLUDED — will 403."
  warn "Fix: upgrade to Python 3.13/3.14 (see step 3), then re-run this."
fi

# ── 6. configuration ──────────────────────────────────────────────────────
say "Checking .env"
if [ -f .env ] && grep -qE '^BOT_TOKEN=[0-9]+:' .env; then
  ok ".env present with a bot token"
  grep -qE '^ADMIN_IDS=[0-9]+' .env && ok "ADMIN_IDS present" ||
      warn "ADMIN_IDS missing — add your Telegram user id to .env"
else
  warn ".env missing or has no token."
  if [ ! -f .env ]; then
    cat > .env <<'EOF'
BOT_TOKEN=PASTE_TOKEN_FROM_BOTFATHER
ADMIN_IDS=PASTE_YOUR_TELEGRAM_USER_ID
EOF
  fi
  warn "edit it (nano .env) before starting the bot."
fi

# ── 7. verify ─────────────────────────────────────────────────────────────
if [ -f shadowlinks_bot.py ]; then
  say "Importing the bot"
  .venv/bin/python - <<'PYEOF' || die "import check failed — see above"
import shadowlinks_bot as B, telegram, bs4
print(f"    bot engine   : OK")
print(f"    framework    : python-telegram-bot {telegram.__version__}")
print(f"    shorteners   : {len(B.REGISTRY)} domains registered")
print(f"    http backend : {'curl_cffi (Chrome TLS)' if B.HTTP._cffi_ok else 'httpx — NO Cloudflare bypass'}")
app = B.build_application()
print(f"    handlers     : {sum(len(v) for v in app.handlers.values())} registered")
PYEOF
  ok "all imports good"
  say "Running the bot's own self-check"
  .venv/bin/python shadowlinks_bot.py --check || die "--check failed"
else
  warn "shadowlinks_bot.py is not in $HERE"
  warn "Unzip SHADOWLINKS-bot.zip here first, or clone it:"
  printf '        pkg install -y git\n'
  printf '        git clone -b arena/82f542e9-shadowlinks \\\n'
  printf '          https://github.com/demoneditz985-ctrl/SHADOWLINKS.git .\n'
fi

# ── done ──────────────────────────────────────────────────────────────────
printf '\n%s\n' "═══════════════════════════════════════════════════════════════════════"
if [ "$CFFI" = "1" ]; then
  printf '  %s✅  READY — curl_cffi active, GPLinks bypass available%s\n' "$C_G" "$C_0"
else
  printf '  %s⚠   INSTALLED, but without curl_cffi%s\n' "$C_Y" "$C_0"
  printf '  %s    Cloudflare sites (incl. GPLinks) will likely 403.%s\n' "$C_Y" "$C_0"
  printf '  %s    Upgrade to Python 3.13/3.14 and re-run to fix.%s\n' "$C_Y" "$C_0"
fi
printf '%s\n\n' "═══════════════════════════════════════════════════════════════════════"
cat <<'BANNER'
  Start the bot:

      cd ~/SHADOWLINKS
      termux-wake-lock
      .venv/bin/python shadowlinks_bot.py

  Survive closing Termux (tmux):

      pkg install -y tmux
      tmux new -s shadow
      termux-wake-lock
      cd ~/SHADOWLINKS && .venv/bin/python shadowlinks_bot.py
        # Ctrl+B then D = detach       tmux attach -t shadow = return

  REQUIRED or Android kills it minutes after the screen turns off:
      Settings -> Apps -> Termux -> Battery -> UNRESTRICTED
      (Xiaomi/Samsung/Oppo/Vivo: also enable "Autostart")

  Then open your bot on Telegram, press Start, and paste:
      https://gplinks.co/BFfOX

  Admin:  /admin   /stats   /addvip <id> 30   /gencode 30 5

  Run the test suite any time:
      .venv/bin/pip install pyflakes
      .venv/bin/python tests/test_config.py
      .venv/bin/python tests/test_resolver.py
      .venv/bin/python tests/test_ptb.py
═══════════════════════════════════════════════════════════════════════
BANNER
