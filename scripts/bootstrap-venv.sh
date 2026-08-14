#!/usr/bin/env bash
# Bootstrap a self-contained .venv with Python 3.13.5 bundled inside it.
#
# The interpreter, libpython, and stdlib are copied into .venv/bundle/, so the
# venv has no external dependencies. This lets the same .venv be used from both
# the host and the agent-vm (which bind-mount the project at the same absolute
# path but have different $HOME).
#
# The bundle lives in .venv/bundle/ rather than .venv/python/ because uv treats
# .venv/python as its own symlink to the canonical install and recreates it
# during subsequent uv operations (e.g. uv pip install), which would undo a
# directory copy placed there.
#
# Idempotent: skips the interpreter bootstrap if .venv/bundle/bin/python3.13
# already exists; always (re)installs requirements with --link-mode=copy.

set -euo pipefail

PROJ="$(cd "$(dirname "$0")/.." && pwd)"
cd "$PROJ"

PY_VERSION_REQUIRED="3.13.5"

if ! command -v uv >/dev/null 2>&1; then
    curl -LsSf https://astral.sh/uv/install.sh | sh
    export PATH="$HOME/.local/bin:$PATH"
fi

if [ ! -x .venv/bundle/bin/python3.13 ] || [ -L .venv/bundle ]; then
    rm -rf .venv

    uv python install "$PY_VERSION_REQUIRED"
    # Resolve through any symlinks so PY_ROOT is the real install directory.
    # Otherwise `cp -a` preserves the source as a symlink instead of copying it.
    PY_BIN="$(readlink -f "$(uv python find "$PY_VERSION_REQUIRED")")"
    PY_ROOT="$(dirname "$(dirname "$PY_BIN")")"

    uv venv --python "$PY_BIN" --seed .venv

    cp -a "$PY_ROOT" .venv/bundle

    rm -f .venv/bin/python .venv/bin/python3 .venv/bin/python3.13
    ln -s ../bundle/bin/python3.13 .venv/bin/python
    ln -s python                   .venv/bin/python3
    ln -s python                   .venv/bin/python3.13

    PYVER="$("$PY_BIN" -c 'import sys; print("%d.%d.%d" % sys.version_info[:3])')"
    cat > .venv/pyvenv.cfg <<EOF
home = $PROJ/.venv/bundle/bin
include-system-site-packages = false
version = $PYVER
EOF
fi

# The block above runs only on a fresh bootstrap, so a venv created before the switch to an
# isolated one keeps seeing site-packages until this converges it.
sed -i 's/^include-system-site-packages = true$/include-system-site-packages = false/' .venv/pyvenv.cfg

UV_LINK_MODE=copy uv pip install \
    --python .venv/bin/python \
    -r requirements.txt -r requirements-dev.txt

# pyproject.toml/.coveragerc are the shared WB codestyle baseline, not committed
# here (see .gitignore) — pulled from the ../codestyle sibling checkout, per
# https://github.com/wirenboard/codestyle python/local/linux-devenv.sh.
CODESTYLE_DIR="$PROJ/../codestyle"
if [ ! -d "$CODESTYLE_DIR" ]; then
    git clone https://github.com/wirenboard/codestyle.git "$CODESTYLE_DIR"
fi
cp "$CODESTYLE_DIR/python/config/pyproject.toml" .
cp "$CODESTYLE_DIR/python/config/.coveragerc" .
