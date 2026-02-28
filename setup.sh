#!/usr/bin/env bash
# Bootstrap the networkInterface capture agent on Linux.
set -euo pipefail

PROJECT_ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
VENV_DIR="${PROJECT_ROOT}/.venv"
PYTHON=""

# ═══════════════════════════════════════════════════════════════════════
# 1. Python 3.11
# ═══════════════════════════════════════════════════════════════════════
for candidate in python3.11 python3 python; do
    if command -v "$candidate" &>/dev/null; then
        ver=$("$candidate" --version 2>&1)
        if [[ "$ver" == *"3.11"* ]]; then
            PYTHON="$candidate"
            break
        fi
    fi
done

if [[ -z "$PYTHON" ]]; then
    echo "[FATAL] Python 3.11 is required but not found in PATH."
    exit 1
fi
echo "[OK] $("$PYTHON" --version)"

# ═══════════════════════════════════════════════════════════════════════
# 2. AF_PACKET capability check
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "[*] Capture backend: AF_PACKET raw socket (kernel native, no libpcap needed)"

if [[ $EUID -eq 0 ]]; then
    echo "[OK] Running as root — AF_PACKET will work"
else
    echo "[INFO] capture_agent.py needs raw socket access. Two options:"
    echo "       Option A:  sudo python capture_agent.py"
    echo "       Option B:  sudo setcap cap_net_raw+ep \$(which python3.11)"
    echo "                  (allows running without sudo, persists across reboots)"
fi

# ═══════════════════════════════════════════════════════════════════════
# 3. Virtual environment
# ═══════════════════════════════════════════════════════════════════════
if [[ ! -d "$VENV_DIR" ]]; then
    echo ""
    echo "[*] Creating virtual environment..."
    "$PYTHON" -m venv --upgrade-deps --prompt="networkInterface" "$VENV_DIR"
else
    echo "[OK] Venv already exists at $VENV_DIR"
fi

# ═══════════════════════════════════════════════════════════════════════
# 4. Dependencies
# ═══════════════════════════════════════════════════════════════════════
# shellcheck disable=SC1091
source "${VENV_DIR}/bin/activate"

echo "[*] Installing dependencies..."
pip install --quiet --upgrade pip
pip install --quiet -r "${PROJECT_ROOT}/requirements.txt"

# ═══════════════════════════════════════════════════════════════════════
# Done
# ═══════════════════════════════════════════════════════════════════════
echo ""
echo "=== Setup complete ==="
echo ""
echo "  Activate:  source .venv/bin/activate"
echo "  Capture:   sudo python capture_agent.py"
echo "  Pipeline:  python databricks_pipeline.py --local"
