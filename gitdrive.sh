#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# gitdrive.sh — Local runner for GitDrive
#
# Manages a .venv/ inside the project directory and ensures the package is
# installed (editable mode) before delegating to the real CLI entry point.
# ---------------------------------------------------------------------------

# ── Colours ────────────────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[0;33m'
    RED=$'\033[0;31m'
    RESET=$'\033[0m'
else
    GREEN="" YELLOW="" RED="" RESET=""
fi

info()    { printf '%s[gitdrive]%s %s\n' "$GREEN"  "$RESET" "$1"; }
warn()    { printf '%s[gitdrive]%s %s\n' "$YELLOW" "$RESET" "$1"; }
error()   { printf '%s[gitdrive]%s %s\n' "$RED"    "$RESET" "$1" >&2; }

# ── Resolve script directory (follows symlinks) ───────────────────────────
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
SCRIPT_DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"

VENV_DIR="$SCRIPT_DIR/.venv"
PYPROJECT="$SCRIPT_DIR/pyproject.toml"
HASH_FILE="$VENV_DIR/.pyproject_hash"

# ── Ensure python3 is available ───────────────────────────────────────────
if ! command -v python3 &>/dev/null; then
    error "python3 not found. Please install Python 3.10+ and try again."
    exit 1
fi

# ── Create virtual environment if missing ─────────────────────────────────
FIRST_SETUP=false
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR ..."
    python3 -m venv "$VENV_DIR"
    FIRST_SETUP=true
fi

# ── Change detection via pyproject.toml hash ──────────────────────────────
CURRENT_HASH="$(sha256sum "$PYPROJECT" | cut -d' ' -f1)"

NEEDS_INSTALL=false
if [[ ! -f "$HASH_FILE" ]]; then
    NEEDS_INSTALL=true
elif [[ "$(cat "$HASH_FILE")" != "$CURRENT_HASH" ]]; then
    NEEDS_INSTALL=true
fi

if [[ "$NEEDS_INSTALL" == "true" ]]; then
    info "Installing gitdrive in editable mode ..."
    "$VENV_DIR/bin/pip" install -q -e "$SCRIPT_DIR"
    printf '%s' "$CURRENT_HASH" > "$HASH_FILE"
    info "Installation complete."
fi

# ── Prepend .venv/bin to PATH (so git finds git-remote-gdrive) ────────────
export PATH="$VENV_DIR/bin:$PATH"

# ── Local mode: ensure .gitdrive/ exists for auto-detection ──────────────
mkdir -p "$SCRIPT_DIR/.gitdrive"

# ── First-setup hint ──────────────────────────────────────────────────────
if [[ "$FIRST_SETUP" == "true" ]]; then
    warn "Hint: for git push/pull to work, add this to your shell (or ~/.bashrc):"
    warn "  export PATH=\"$VENV_DIR/bin:\$PATH\""
fi

# ── Hand off to the real CLI ──────────────────────────────────────────────
exec "$VENV_DIR/bin/gitdrive" "$@"
