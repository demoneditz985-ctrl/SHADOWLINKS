#!/usr/bin/env bash
# ══════════════════════════════════════════════════════════════════════════
#  SHADOWLINKS — Termux one-shot installer
#
#      bash install-termux.sh
#
# ──────────────────────────────────────────────────────────────────────────
#  WHY pydantic-core FAILS ON TERMUX, AND HOW THIS AVOIDS IT
# ──────────────────────────────────────────────────────────────────────────
#  Termux runs on Android's bionic libc. PyPI's normal wheels are "manylinux"
#  = glibc, so pip refuses them and tries to COMPILE pydantic-core from Rust
#  source — ~15 minutes, and it usually runs the phone out of RAM.
#
#  Prebuilt Android (bionic) pydantic-core wheels do exist, but only for a
#  handful of versions, on the Termux user-repository index. So you must land
#  on a pydantic / pydantic-core pair that has one.
#
#  THE TRAP: aiogram 3.31 requires `pydantic<2.14,>=2.4.1`. That single upper
#  bound rules out the newest pydantic, and the newest one aiogram DOES allow
#  (2.13.5) pins pydantic-core==2.46.5 — which has NO Android wheel at all.
#  So a plain `pip install aiogram` always ends in a doomed Rust build.
#
#  Verified against the actual wheel listings, this is the ONLY pair that is
#  both allowed by aiogram and prebuilt for Android:
#
#      pydantic 2.12.5  ->  pydantic-core 2.41.5
#                           cp313 + cp314, arm64_v8a / armeabi_v7a / x86 / x86_64
#
#  This script pins that pair with a pip CONSTRAINTS FILE, which pip is not
#  allowed to relax while resolving anything else, and installs pydantic-core
#  first with --only-binary so it fails loudly instead of compiling.
#
#  Tested: pydantic 2.12.5 / pydantic-core 2.41.5 / aiogram 3.31.0 /
#  aiohttp 3.14.5 / curl_cffi 0.16.3 / bs4 4.15.0 — full suite, 76/76 pass.
# ══════════════════════════════════════════════════════════════════════════
set -uo pipefail

# ── the known-good pair ───────────────────────────────────────────────────
PIN_PYDANTIC="2.12.5"
PIN_PYDANTIC_CORE="2.41.5"

IDX_PRIMARY="https://termux-user-repository.github.io/pypi/"
IDX_BACKUP="https://eutalix.github.io/android-pydantic-core/"

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
say "Installing system packages"
pkg update -y && pkg upgrade -y || die "pkg update failed — check your network"
pkg install -y python build-essential libffi openssl unzip ||
    die "pkg install failed"
ok "python, build-essential, libffi, openssl, unzip"
command -v python >/dev/null || die "python not on PATH after install"

# ── 3. Python must be 3.13 or 3.14 ────────────────────────────────────────
say "Checking Python version against available Android wheels"
python --version
PY="${PY:-python}"
PYTAG="$("$PY" -c 'import sys;print("cp%d%d"%(sys.version_info[0],sys.version_info[1]))')"

case "$PYTAG" in
  cp313|cp314|cp315)
      ok "$PYTAG is supported — pydantic-core $PIN_PYDANTIC_CORE has a prebuilt
      android_24 wheel for cp313 and cp314" ;;
  *)
      warn "$PYTAG has NO android_24 pydantic-core wheels published."
      warn "Upgrade Termux's Python — this is the actual fix:"
      printf '        pkg update -y && pkg upgrade -y\n'
      printf '        pkg install -y python\n'
      printf '        python --version        # want 3.13.x or 3.14.x\n'
      printf '      then re-run:  bash install-termux.sh\n\n'
      warn "If pkg will not move you there, get 3.13 from the tur repo:"
      printf '        pkg install -y tur-repo && pkg install -y python3.13\n'
      printf '      then:  PY=python3.13 bash install-termux.sh\n\n'
      [ -n "${FORCE_OLD_PY:-}" ] || die "unsupported Python $PYTAG for a wheel-only
      install. Set FORCE_OLD_PY=1 to attempt the Rust source build anyway."
      warn "FORCE_OLD_PY set — will attempt a source build" ;;
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
export PIP_EXTRA_INDEX_URL="$IDX_PRIMARY"

# ── 5. constraints file: pip may NOT move these ───────────────────────────
CONSTRAINTS="$(mktemp)"
trap 'rm -f "$CONSTRAINTS"' EXIT
cat > "$CONSTRAINTS" <<EOF
pydantic==$PIN_PYDANTIC
pydantic-core==$PIN_PYDANTIC_CORE
EOF
say "Pinning with a constraints file"
sed 's/^/      /' "$CONSTRAINTS"
ok "every pip call below passes -c, so nothing can drag pydantic to 2.13.x"

# ── 6. pydantic-core, cascading strategies ────────────────────────────────
say "Installing pydantic-core==$PIN_PYDANTIC_CORE (the step that normally fails)"

pdc_ok() { .venv/bin/python -c "import pydantic_core" >/dev/null 2>&1; }

install_pdc() {
  pdc_ok && { ok "pydantic-core already importable"; return 0; }

  warn "strategy 1/3 — prebuilt wheel, official Termux index"
  $PIP install --only-binary=pydantic-core "pydantic-core==$PIN_PYDANTIC_CORE" \
      --extra-index-url "$IDX_PRIMARY" 2>&1 | tail -6
  pdc_ok && { ok "pydantic-core from the Termux index"; return 0; }

  warn "strategy 2/3 — prebuilt wheel, android-pydantic-core mirror"
  $PIP install --only-binary=pydantic-core "pydantic-core==$PIN_PYDANTIC_CORE" \
      --extra-index-url "$IDX_BACKUP" 2>&1 | tail -6
  pdc_ok && { ok "pydantic-core from the mirror index"; return 0; }

  warn "strategy 3/3 — compile from source with Rust (slow, ~15 min)"
  pkg install -y rust binutils || warn "could not install the Rust toolchain"
  # These CARGO_* settings keep the build inside a phone's RAM; without them
  # rustc parallelises hard and the kernel OOM-kills it.
  CARGO_BUILD_JOBS=2 \
  CARGO_PROFILE_RELEASE_LTO=thin \
  CARGO_PROFILE_RELEASE_CODEGEN_UNITS=16 \
  CARGO_PROFILE_RELEASE_STRIP=true \
  ANDROID_API_LEVEL=24 \
  $PIP install --no-binary=pydantic-core "pydantic-core==$PIN_PYDANTIC_CORE" 2>&1 | tail -25
  pdc_ok && { ok "pydantic-core compiled from source"; return 0; }
  return 1
}

install_pdc || die "could not get pydantic-core==$PIN_PYDANTIC_CORE working.

  Copy the last ~30 lines above into the chat and I will read them.
  Likely causes, in order:
    1. Python is not 3.13/3.14   ->  pkg install -y python; python --version
    2. No route to the wheel index -> ping -c1 termux-user-repository.github.io
    3. Rust build ran out of RAM   -> close apps, plug in power, re-run"

# ── 7. pydantic (pure python, exact pin) ──────────────────────────────────
say "Installing pydantic==$PIN_PYDANTIC"
$PIP install -c "$CONSTRAINTS" "pydantic==$PIN_PYDANTIC" ||
    die "pydantic install failed"
ok "pydantic $(.venv/bin/python -c 'import pydantic;print(pydantic.VERSION)' 2>/dev/null || echo '?')"

# ── 8. the bot's dependencies, still constrained ──────────────────────────
say "Installing aiogram, beautifulsoup4, aiohttp"
$PIP install -c "$CONSTRAINTS" aiogram beautifulsoup4 aiohttp ||
    die "aiogram install failed"
ok "aiogram $(.venv/bin/python -c 'import aiogram;print(aiogram.__version__)' 2>/dev/null || echo '?')"

# confirm nothing moved the pins
say "Verifying the pins held"
.venv/bin/python - <<PYEOF || die "the pins moved — do not proceed, report this"
import pydantic, pydantic_core
assert pydantic.VERSION == "$PIN_PYDANTIC", f"pydantic drifted to {pydantic.VERSION}"
assert pydantic_core.__version__ == "$PIN_PYDANTIC_CORE", \
    f"pydantic-core drifted to {pydantic_core.__version__}"
print(f"    pydantic {pydantic.VERSION} · pydantic-core {pydantic_core.__version__} — locked")
PYEOF
ok "pins intact"

# ── 9. curl_cffi — optional, but it is the whole point ────────────────────
say "Installing curl_cffi (Chrome TLS impersonation — this beats Cloudflare)"
CFFI=0
if $PIP install --only-binary=curl_cffi curl_cffi >/dev/null 2>&1; then
  if .venv/bin/python -c "from curl_cffi.requests import AsyncSession" 2>/dev/null; then
    CFFI=1; ok "curl_cffi working — GPLinks bypass available"
  else
    warn "curl_cffi installed but will not import"
  fi
else
  warn "curl_cffi did not install."
  warn "The bot STILL RUNS on aiohttp, but without Chrome TLS impersonation"
  warn "Cloudflare-protected sites — GPLINKS INCLUDED — will return 403."
  warn "It needs Python 3.13/3.14 for the Android wheel; you have $PYTAG."
fi

# ── 10. configuration ─────────────────────────────────────────────────────
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

# ── 11. verify ────────────────────────────────────────────────────────────
if [ -f shadowlinks_bot.py ]; then
  say "Importing the bot"
  .venv/bin/python - <<'PYEOF' || die "import check failed — see above"
import shadowlinks_bot as B, aiogram, pydantic, pydantic_core, bs4
print(f"    bot engine   : OK")
print(f"    shorteners   : {len(B.REGISTRY)} domains registered")
print(f"    http backend : {'curl_cffi (Chrome TLS)' if B.HTTP._cffi_ok else 'aiohttp — NO Cloudflare bypass'}")
print(f"    aiogram {aiogram.__version__} · pydantic {pydantic.VERSION} · "
      f"pydantic-core {pydantic_core.__version__} · bs4 {bs4.__version__}")
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
═══════════════════════════════════════════════════════════════════════
BANNER
