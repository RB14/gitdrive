#!/usr/bin/env bash
set -euo pipefail

# ---------------------------------------------------------------------------
# install.sh — Global installer for GitDrive
#
# Creates a virtualenv at ~/.gitdrive/venv/, installs the package in editable
# mode, and drops thin wrapper scripts into ~/.local/bin/ so that `gitdrive`
# and `git-remote-gdrive` are available system-wide.
#
# Safe to re-run at any time (idempotent).
# ---------------------------------------------------------------------------

# ── Colours & helpers ──────────────────────────────────────────────────────
if [[ -t 1 ]]; then
    GREEN=$'\033[0;32m'
    YELLOW=$'\033[1;33m'
    RED=$'\033[0;31m'
    BLUE=$'\033[0;34m'
    RESET=$'\033[0m'
else
    GREEN="" YELLOW="" RED="" BLUE="" RESET=""
fi

info()    { printf '%s[info]%s    %s\n' "$BLUE"   "$RESET" "$1"; }
success() { printf '%s[ok]%s      %s\n' "$GREEN"  "$RESET" "$1"; }
warn()    { printf '%s[warn]%s    %s\n' "$YELLOW" "$RESET" "$1"; }
error()   { printf '%s[error]%s   %s\n' "$RED"    "$RESET" "$1" >&2; }

# ── Resolve source directory (follows symlinks) ───────────────────────────
SOURCE="${BASH_SOURCE[0]}"
while [[ -L "$SOURCE" ]]; do
    DIR="$(cd -P "$(dirname "$SOURCE")" && pwd)"
    SOURCE="$(readlink "$SOURCE")"
    [[ "$SOURCE" != /* ]] && SOURCE="$DIR/$SOURCE"
done
GITDRIVE_SRC="$(cd -P "$(dirname "$SOURCE")" && pwd)"

PYPROJECT="$GITDRIVE_SRC/pyproject.toml"
INSTALL_DIR="$HOME/.gitdrive"
VENV_DIR="$INSTALL_DIR/venv"
HASH_FILE="$INSTALL_DIR/.pyproject_hash"
BIN_DIR="$HOME/.local/bin"

# ── Find best Python ──────────────────────────────────────────────────────
find_python() {
    local candidates=("python3.12" "python3.11" "python3.10" "python3")
    for cmd in "${candidates[@]}"; do
        if command -v "$cmd" &>/dev/null; then
            # Validate version >= 3.10
            local ver
            ver="$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')"
            local major minor
            major="${ver%%.*}"
            minor="${ver##*.}"
            if (( major == 3 && minor >= 10 )); then
                echo "$cmd"
                return 0
            fi
        fi
    done
    return 1
}

PYTHON="$(find_python)" || {
    error "Python 3.10+ is required but was not found."
    error "Please install Python 3.10 or newer and try again."
    exit 1
}

PYTHON_VER="$("$PYTHON" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}")')"
info "Using $PYTHON (Python $PYTHON_VER)"

# ── Create installation directory ─────────────────────────────────────────
mkdir -p "$INSTALL_DIR"

# ── Create virtual environment if missing ─────────────────────────────────
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment at $VENV_DIR ..."
    "$PYTHON" -m venv "$VENV_DIR"
    success "Virtual environment created."
else
    info "Virtual environment already exists."
fi

# ── Change detection via pyproject.toml hash ──────────────────────────────
CURRENT_HASH="$(sha256sum "$PYPROJECT" | cut -d' ' -f1)"

NEEDS_INSTALL=false
if [[ ! -f "$HASH_FILE" ]]; then
    NEEDS_INSTALL=true
elif [[ "$(cat "$HASH_FILE")" != "$CURRENT_HASH" ]]; then
    info "pyproject.toml has changed since last install."
    NEEDS_INSTALL=true
fi

if [[ "$NEEDS_INSTALL" == "true" ]]; then
    info "Installing gitdrive in editable mode ..."
    "$VENV_DIR/bin/pip" install -q -e "$GITDRIVE_SRC" 2>&1 | \
        while IFS= read -r line; do
            # Only show errors/warnings, suppress normal output
            case "$line" in
                *ERROR*|*error*|*Warning*|*warning*) echo "  $line" ;;
            esac
        done || true
    # Verify the install actually worked
    if [[ ! -x "$VENV_DIR/bin/gitdrive" ]]; then
        error "Installation failed — gitdrive entry point not found."
        exit 1
    fi
    printf '%s' "$CURRENT_HASH" > "$HASH_FILE"
    success "Package installed."
else
    info "Package is up to date (pyproject.toml unchanged)."
fi

# ── Create wrapper scripts in ~/.local/bin/ ────────────────────────────────
mkdir -p "$BIN_DIR"

create_wrapper() {
    local name="$1"
    local target="$VENV_DIR/bin/$name"
    local wrapper="$BIN_DIR/$name"

    cat > "$wrapper" <<WRAPPER
#!/usr/bin/env bash
exec "${target}" "\$@"
WRAPPER
    chmod +x "$wrapper"
}

info "Creating wrapper scripts in $BIN_DIR ..."
create_wrapper "gitdrive"
create_wrapper "git-remote-gdrive"
success "Wrappers created: gitdrive, git-remote-gdrive"

# ── Check PATH ─────────────────────────────────────────────────────────────
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    echo ""
    warn "$BIN_DIR is not on your PATH."
    warn "Add the following to your shell profile (~/.bashrc, ~/.zshrc, etc.):"
    echo ""
    echo "    export PATH=\"$BIN_DIR:\$PATH\""
    echo ""
fi

# ── Verify installation ───────────────────────────────────────────────────
echo ""
info "Verifying installation ..."
VERSION="$("$BIN_DIR/gitdrive" --version 2>&1)" || {
    error "Verification failed. Try running: $BIN_DIR/gitdrive --version"
    exit 1
}
success "$VERSION"
echo ""
success "GitDrive installed successfully!"
warn "Installed in editable mode — do NOT move, rename, or delete this directory:"
warn "  $GITDRIVE_SRC"
warn "To update: cd $GITDRIVE_SRC && git pull && ./install.sh"
echo ""
info "Run 'gitdrive --help' to get started."
