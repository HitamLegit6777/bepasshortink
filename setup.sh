#!/usr/bin/env bash
# setup.sh — install everything inshorturl_http.py needs.
#
#   1. cfast challenge binary + browser profiles  (GitHub release)
#   2. cfast solver API on :8810                  (Turnstile for /links/go)
#   3. python deps for the resolver               (httpx[http2])
#
# Usage:
#   bash setup.sh              # install / start service / health-check
#   bash setup.sh --check      # also resolve one live shortlink end to end
#   bash setup.sh --with-browser  # add Camoufox (fallback bypass, ~300 MB)
#   bash setup.sh --uninstall
#
# Env overrides:
#   CF_REPO     (default HitamLegit6777/cfast)
#   CF_TAG      (default v1.1.0-managed)
#   CF_PROFILE  (default profile_safari.json)
#   PREFIX      (default /opt/cfast)
#   PORT        (default 8810)
#   NO_SYSTEMD=1  run the API under nohup instead of systemd (containers)
set -euo pipefail

CF_REPO="${CF_REPO:-HitamLegit6777/cfast}"
CF_TAG="${CF_TAG:-v1.1.0-managed}"
CF_PROFILE="${CF_PROFILE:-profile_safari.json}"
PREFIX="${PREFIX:-/opt/cfast}"
PORT="${PORT:-8810}"
UNIT=/etc/systemd/system/cfast.service

CHECK=0
WITH_BROWSER=0
UNINSTALL=0
for arg in "$@"; do
    case "$arg" in
        --check) CHECK=1 ;;
        --with-browser) WITH_BROWSER=1 ;;
        --uninstall) UNINSTALL=1 ;;
        -h|--help) sed -n '2,20p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
        *) echo "unknown flag: $arg" >&2; exit 2 ;;
    esac
done

say() { printf '[*] %s\n' "$*"; }
die() { printf '[!] %s\n' "$*" >&2; exit 1; }

SUDO=""
[ "$(id -u)" -eq 0 ] || SUDO="sudo"

if [ "$UNINSTALL" = 1 ]; then
    say "stopping cfast"
    if [ -f "$UNIT" ]; then
        $SUDO systemctl disable --now cfast 2>/dev/null || true
        $SUDO rm -f "$UNIT"
        $SUDO systemctl daemon-reload 2>/dev/null || true
    elif [ -f "$PREFIX/api.pid" ]; then
        kill "$(cat "$PREFIX/api.pid")" 2>/dev/null || true
    fi
    $SUDO rm -rf "$PREFIX"
    say "removed $PREFIX"
    exit 0
fi

# ── preflight ────────────────────────────────────────────────────────
arch=$(uname -m)
[ "$arch" = "x86_64" ] || die "release binary is linux-x86_64, this host is $arch.
Build from source instead:
  git clone https://github.com/$CF_REPO && cd cfast/source
  cargo build --release --bin challenge"

command -v python3 >/dev/null || die "python3 not found"
command -v curl >/dev/null || die "curl not found"

say "installing cfast $CF_TAG into $PREFIX"
$SUDO mkdir -p "$PREFIX"

# ── solver binary ────────────────────────────────────────────────────
$SUDO curl -fsSL --retry 3 \
    "https://github.com/$CF_REPO/releases/download/$CF_TAG/challenge" \
    -o "$PREFIX/challenge"
$SUDO chmod +x "$PREFIX/challenge"
"$PREFIX/challenge" --help >/dev/null 2>&1 \
    || die "$PREFIX/challenge does not run on this host"

# ── profiles (json + its answers dir, both profiles) ─────────────────
raw="https://raw.githubusercontent.com/$CF_REPO/main"
for p in profile_safari profile_fast; do
    $SUDO mkdir -p "$PREFIX/$p"
    for f in "$p.json" "$p/answers.json" "$p/challenge-page-answers.json"; do
        $SUDO curl -fsSL --retry 3 "$raw/$f" -o "$PREFIX/$f"
    done
done
[ -f "$PREFIX/$CF_PROFILE" ] || die "profile $CF_PROFILE missing after download"

# ── solver API ───────────────────────────────────────────────────────
$SUDO curl -fsSL --retry 3 "$raw/solver_api.py" -o "$PREFIX/solver_api.py"

# A port already in use would leave the resolver talking to whatever else is
# listening there, so refuse rather than start a second bind attempt.
if command -v ss >/dev/null && ss -ltn 2>/dev/null | grep -q ":$PORT "; then
    if ss -ltn 2>/dev/null | grep -q ":$PORT " && curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        say "port $PORT already serves a solver API — reusing it"
        PORT_BUSY=0
    else
        die "port $PORT is taken by another process (not a cfast API).
Pick a free one:  PORT=8899 bash $0"
    fi
else
    PORT_BUSY=1
fi

if [ "$PORT_BUSY" != 1 ]; then
    say "reusing the solver API already answering on :$PORT"
elif [ "${NO_SYSTEMD:-0}" != 1 ] && command -v systemctl >/dev/null && [ -d /run/systemd/system ]; then
    $SUDO tee "$UNIT" >/dev/null <<EOF
[Unit]
Description=cfast pure-protocol solver API
After=network.target

[Service]
Type=simple
WorkingDirectory=$PREFIX
ExecStart=/usr/bin/env python3 $PREFIX/solver_api.py
Environment=TS_BIN=$PREFIX/challenge
Environment=TS_PROFILE=$PREFIX/$CF_PROFILE
Environment=TS_PORT=$PORT
Restart=always
RestartSec=3
User=root

[Install]
WantedBy=multi-user.target
EOF
    $SUDO systemctl daemon-reload
    $SUDO systemctl enable cfast >/dev/null 2>&1 || true
    $SUDO systemctl restart cfast
else
    say "no systemd — starting the API with nohup"
    if [ -f "$PREFIX/api.pid" ]; then $SUDO kill "$(cat "$PREFIX/api.pid")" 2>/dev/null || true; fi
    cd "$PREFIX"
    $SUDO env TS_BIN="$PREFIX/challenge" TS_PROFILE="$PREFIX/$CF_PROFILE" \
        TS_PORT="$PORT" nohup python3 "$PREFIX/solver_api.py" \
        >>"$PREFIX/api.log" 2>&1 &
    echo $! | $SUDO tee "$PREFIX/api.pid" >/dev/null
fi

# ── python deps ──────────────────────────────────────────────────────
have_httpx() { python3 -c 'import httpx, h2' >/dev/null 2>&1; }
if ! have_httpx; then
    say "installing httpx[http2]"
    if ! python3 -m pip install --quiet 'httpx[http2]' 2>/dev/null; then
        say "system pip refused (PEP 668) — using a venv"
        $SUDO python3 -m venv "$PREFIX/venv"
        $SUDO "$PREFIX/venv/bin/pip" install --quiet 'httpx[http2]'
        say "run the resolver with $PREFIX/venv/bin/python"
    fi
fi

if [ "$WITH_BROWSER" = 1 ]; then
    say "installing Camoufox (fallback browser bypass)"
    python3 -m pip install --quiet camoufox[geoip] 2>/dev/null \
        || $SUDO "$PREFIX/venv/bin/pip" install --quiet camoufox[geoip] 2>/dev/null \
        || die "pip install camoufox failed"
    python3 -m camoufox fetch 2>/dev/null \
        || $SUDO "$PREFIX/venv/bin/python" -m camoufox fetch \
        || die "camoufox fetch failed"
fi

# ── health ───────────────────────────────────────────────────────────
say "waiting for the solver API on :$PORT"
for _ in $(seq 1 20); do
    if curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1; then
        say "solver API live"
        break
    fi
    sleep 1
done
curl -fsS "http://127.0.0.1:$PORT/health" >/dev/null 2>&1 \
    || die "solver API did not come up on :$PORT.
  systemd: journalctl -u cfast -n 50
  nohup:   tail $PREFIX/api.log"

export CFast_BIN="$PREFIX/challenge"
export CFast_PROFILE="$PREFIX/$CF_PROFILE"
export CFast_API="http://127.0.0.1:$PORT"

if [ "$CHECK" = 1 ]; then
    say "resolving one live shortlink"
    python3 "$(dirname "$0")/inshorturl_http.py" Tddfjl8 --verbose \
        || $SUDO "$PREFIX/venv/bin/python" "$(dirname "$0")/inshorturl_http.py" Tddfjl8 --verbose \
        || die "end-to-end check failed"
fi

say "done"
cat <<EOF

  export CFast_BIN=$CFast_BIN
  export CFast_PROFILE=$CFast_PROFILE
  export CFast_API=$CFast_API

  python3 inshorturl_http.py https://inshorturl.in/<alias>
  python3 inshorturl_bypass.py <alias>      # browser fallback (needs camoufox)

EOF
