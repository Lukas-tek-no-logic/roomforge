#!/bin/bash
# RoomForge — AI Interior Designer
# One-line install: curl -fsSL https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main/install.sh | bash
#
# Runs the orchestrator (FastAPI) that coordinates GPU services on DGX Spark.
# No GPU needed on this machine — all rendering happens on DGX.

set -euo pipefail

REPO="https://github.com/Lukas-tek-no-logic/roomforge.git"
INSTALL_DIR="/opt/roomforge"
SERVICE_USER="roomforge"
SERVICE_NAME="roomforge"

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
NC='\033[0m'

info()  { echo -e "${GREEN}[roomforge]${NC} $*"; }
warn()  { echo -e "${YELLOW}[roomforge]${NC} $*"; }
error() { echo -e "${RED}[roomforge]${NC} $*" >&2; }

# ── Check root ────────────────────────────────────────────────────────────
if [ "$(id -u)" -ne 0 ]; then
    error "Run as root: curl -fsSL ... | sudo bash"
    exit 1
fi

info "Installing RoomForge — AI Interior Designer"
echo ""

# ── Detect OS ─────────────────────────────────────────────────────────────
if [ -f /etc/os-release ]; then
    . /etc/os-release
    DISTRO="$ID"
else
    DISTRO="unknown"
fi
ARCH=$(uname -m)
info "OS: ${PRETTY_NAME:-$DISTRO} ($ARCH)"

# ── Install system dependencies ───────────────────────────────────────────
info "Installing system packages..."
case "$DISTRO" in
    ubuntu|debian)
        apt-get update -qq
        apt-get install -y -qq python3 python3-venv python3-pip git curl >/dev/null 2>&1
        ;;
    alpine)
        apk add --no-cache python3 py3-pip git curl
        ;;
    fedora|rhel|centos|rocky)
        dnf install -y python3 python3-pip git curl
        ;;
    arch|manjaro)
        pacman -Sy --noconfirm python python-pip git curl
        ;;
    *)
        warn "Unknown distro '$DISTRO' — assuming python3/git/curl are installed"
        ;;
esac

# Verify Python 3.12+
PY=$(command -v python3)
PY_VER=$($PY --version 2>&1 | grep -oP '\d+\.\d+')
PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
    error "Python 3.11+ required (found $PY_VER)"
    exit 1
fi
info "Python: $($PY --version)"

# ── Clone / update repo ──────────────────────────────────────────────────
if [ -d "$INSTALL_DIR/.git" ]; then
    info "Updating existing installation..."
    cd "$INSTALL_DIR"
    git pull --ff-only
else
    info "Cloning RoomForge..."
    git clone "$REPO" "$INSTALL_DIR"
    cd "$INSTALL_DIR"
fi

# ── Create venv and install deps ─────────────────────────────────────────
info "Setting up Python environment..."
$PY -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "python-multipart>=0.0.12" \
    "aiofiles>=24.1.0" \
    "httpx>=0.27.0" \
    "anthropic>=0.45.0"

# ── Configuration ─────────────────────────────────────────────────────────
ENV_FILE="$INSTALL_DIR/.env"
if [ ! -f "$ENV_FILE" ]; then
    info "Creating config..."
    cat > "$ENV_FILE" << 'EOF'
# RoomForge configuration
# DGX Spark host (all GPU services: Blender, Flux, CHORD, TRELLIS, DN-Splatter)
DGX_HOST=192.168.0.200

# Anthropic API key (required for AI chat/modify)
ANTHROPIC_API_KEY=

# Server
HOST=0.0.0.0
PORT=8000
EOF
    warn "Edit /opt/roomforge/.env and set ANTHROPIC_API_KEY"
else
    info "Config exists, keeping current .env"
fi

# ── Create sessions dir ──────────────────────────────────────────────────
mkdir -p "$INSTALL_DIR/sessions"

# ── Create service user ──────────────────────────────────────────────────
if ! id "$SERVICE_USER" &>/dev/null; then
    useradd --system --home "$INSTALL_DIR" --shell /usr/sbin/nologin "$SERVICE_USER" 2>/dev/null || true
fi
chown -R "$SERVICE_USER":"$SERVICE_USER" "$INSTALL_DIR" 2>/dev/null || chown -R root:root "$INSTALL_DIR"

# ── Systemd service ──────────────────────────────────────────────────────
if [ -d /etc/systemd/system ]; then
    info "Installing systemd service..."
    cat > /etc/systemd/system/${SERVICE_NAME}.service << EOF
[Unit]
Description=RoomForge — AI Interior Designer
After=network.target
Wants=network-online.target

[Service]
Type=simple
User=$SERVICE_USER
Group=$SERVICE_USER
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn backend.main:app --host \${HOST} --port \${PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF

    systemctl daemon-reload
    systemctl enable "$SERVICE_NAME"
    info "Service installed: systemctl start roomforge"
else
    info "No systemd — start manually:"
    info "  cd $INSTALL_DIR && .venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port 8000"
fi

# ── Done ──────────────────────────────────────────────────────────────────
echo ""
echo "=========================================="
echo "  RoomForge installed to $INSTALL_DIR"
echo "=========================================="
echo ""
echo "  1. Set your API key:"
echo "     nano /opt/roomforge/.env"
echo ""
echo "  2. Start:"
echo "     systemctl start roomforge"
echo ""
echo "  3. Open:"
echo "     http://$(hostname -I | awk '{print $1}'):8000"
echo ""
echo "  Update: cd /opt/roomforge && git pull && systemctl restart roomforge"
echo ""
