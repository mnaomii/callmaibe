#!/usr/bin/env bash
# callmaibe installer for Linux and macOS. No sudo. Safe to re-run.
#
#   ./scripts/install.sh            install (or upgrade) callmaibe with SIP + local speech
#   CALLMAIBE_SOURCE=/path/to/repo ./scripts/install.sh      install from a local checkout
#   ./scripts/install.sh --source URL_OR_PATH --yes
set -euo pipefail

# Where the package comes from. A GitHub archive, so no git is needed.
SOURCE="${CALLMAIBE_SOURCE:-https://github.com/mnaomii/callmaibe/archive/refs/heads/main.zip}"
ASSUME_YES=0

while [ $# -gt 0 ]; do
  case "$1" in
    --source) SOURCE="$2"; shift 2 ;;
    -y|--yes) ASSUME_YES=1; shift ;;
    -h|--help) sed -n '2,7p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

step() { printf '\n==> %s\n' "$*"; }
ask() { # ask "question" -> 0 for yes
  [ "$ASSUME_YES" = 1 ] && return 0
  [ -t 0 ] || { echo "(no terminal, answering no to: $1)"; return 1; }
  read -r -p "$1 [y/N] " reply
  case "$reply" in y|Y|yes|YES) return 0 ;; *) return 1 ;; esac
}

# A local path needs a file:// URL for uv's "name @ url" syntax.
if [ -d "$SOURCE" ]; then
  SOURCE="file://$(cd "$SOURCE" && pwd)"
elif [ -f "$SOURCE" ]; then
  SOURCE="file://$(cd "$(dirname "$SOURCE")" && pwd)/$(basename "$SOURCE")"
fi

step "1/4 Checking for uv"
if ! command -v uv >/dev/null 2>&1; then
  export PATH="$HOME/.local/bin:$PATH"
fi
if command -v uv >/dev/null 2>&1; then
  echo "found $(command -v uv)"
else
  echo "uv is not installed. It manages Python 3.12 and the callmaibe install."
  if ask "Run the official uv installer (curl -LsSf https://astral.sh/uv/install.sh | sh)?"; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
  else
    echo "Install uv yourself (https://docs.astral.sh/uv/) and re-run this script." >&2
    exit 1
  fi
fi

step "2/4 Installing callmaibe[sip,speech,local] from $SOURCE"
uv tool install --python 3.12 --force "callmaibe[sip,speech,local] @ $SOURCE"

step "3/4 Making sure uv's tool directory is on your PATH"
uv tool update-shell || echo "(update-shell failed; add $(uv tool dir --bin 2>/dev/null || echo '~/.local/bin') to PATH yourself)"

step "4/4 Setting up callmaibe (addresses, password, backend, clients)"
CM="$(uv tool dir --bin 2>/dev/null || echo "$HOME/.local/bin")/callmaibe"
[ -x "$CM" ] || CM=callmaibe
if [ "$ASSUME_YES" = 1 ]; then
  echo "Skipped (--yes). Open a new terminal and run:  callmaibe setup"
elif [ -t 0 ]; then
  "$CM" setup || echo "callmaibe setup did not finish; re-run it any time: callmaibe setup"
else
  echo "No terminal. Open one and run:  callmaibe setup"
fi
