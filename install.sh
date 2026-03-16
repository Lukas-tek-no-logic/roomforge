#!/bin/bash
# RoomForge — AI Interior Designer - Installer v1.0
# Usage: curl -fsSL https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main/install.sh | bash
#
# Lightweight orchestrator connecting to GPU services on DGX Spark.
# No GPU needed on this machine — all rendering happens on DGX.
VERSION="1.0"

set -e

# Colors
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
BLUE='\033[0;34m'
NC='\033[0m'

REPO_RAW="https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main"
TEMP_DIR=$(mktemp -d)
trap "rm -rf $TEMP_DIR" EXIT

# Function to read input — works even when script is piped
ask() {
    local prompt="$1"
    local default="$2"
    local reply

    printf "%s" "$prompt" > /dev/tty
    read -r reply < /dev/tty

    if [ -z "$reply" ]; then
        echo "$default"
    else
        echo "$reply"
    fi
}

echo -e "${BLUE}"
echo "=============================================="
echo "  RoomForge — AI Interior Designer v${VERSION}"
echo "  Blender + Flux + Claude on DGX Spark        "
echo "=============================================="
echo -e "${NC}"

# ============================================
# Step 1: Check Prerequisites
# ============================================
echo -e "${YELLOW}[1/5] Checking prerequisites...${NC}"

# Python 3.11+
if command -v python3 &>/dev/null; then
    PY=$(command -v python3)
    PY_VER=$($PY --version 2>&1 | grep -oP '\d+\.\d+')
    PY_MAJOR=$(echo "$PY_VER" | cut -d. -f1)
    PY_MINOR=$(echo "$PY_VER" | cut -d. -f2)
    if [ "$PY_MAJOR" -lt 3 ] || { [ "$PY_MAJOR" -eq 3 ] && [ "$PY_MINOR" -lt 11 ]; }; then
        echo -e "  ${RED}ERROR: Python 3.11+ required (found $PY_VER)${NC}"
        exit 1
    fi
    echo -e "  ${GREEN}✓${NC} Python $PY_VER"
else
    echo -e "  ${RED}ERROR: Python3 not found${NC}"
    echo "  Install: apt install python3 python3-venv"
    exit 1
fi

if ! $PY -m venv --help &>/dev/null; then
    echo -e "  ${YELLOW}Installing python3-venv...${NC}"
    sudo apt-get install -y -qq python3-venv 2>/dev/null || {
        echo -e "  ${RED}ERROR: python3-venv not available. Install manually.${NC}"
        exit 1
    }
fi
echo -e "  ${GREEN}✓${NC} python3-venv"

if ! command -v curl &>/dev/null; then
    echo -e "  ${YELLOW}Installing curl...${NC}"
    sudo apt-get install -y -qq curl 2>/dev/null || true
fi
echo -e "  ${GREEN}✓${NC} curl"

# ============================================
# Step 2: Configure Services
# ============================================
echo ""
echo -e "${YELLOW}[2/5] Configure services${NC}"
echo ""
echo "  RoomForge connects to GPU services on DGX Spark"
echo "  and uses Claude API for AI chat."
echo ""

DEFAULT_DGX="192.168.0.200"
echo -e "  DGX Spark IP (default: ${BLUE}$DEFAULT_DGX${NC})"
DGX_HOST=$(ask "  DGX Host [$DEFAULT_DGX]: " "$DEFAULT_DGX")

DEFAULT_KEY=""
echo ""
echo -e "  Anthropic API key (for Claude AI chat)"
ANTHROPIC_KEY=$(ask "  API Key [skip to set later]: " "$DEFAULT_KEY")

# Verify DGX connectivity
echo ""
echo -e "  ${BLUE}Checking DGX services...${NC}"

DGX_SERVICES=("Blender:8005" "Flux:8001" "CHORD:8002" "TRELLIS:8003" "DN-Splatter:8004")
DGX_OK=0
for svc in "${DGX_SERVICES[@]}"; do
    NAME="${svc%%:*}"
    PORT="${svc##*:}"
    if curl -sf --max-time 3 "http://${DGX_HOST}:${PORT}/health" > /dev/null 2>&1; then
        echo -e "  ${GREEN}✓${NC} $NAME (:$PORT)"
        DGX_OK=$((DGX_OK + 1))
    else
        echo -e "  ${YELLOW}✗${NC} $NAME (:$PORT) — not reachable"
    fi
done
echo -e "  ${DGX_OK}/${#DGX_SERVICES[@]} services online"

# ============================================
# Step 3: Choose Install Directory
# ============================================
echo ""
echo -e "${YELLOW}[3/5] Configure installation${NC}"
echo ""

DEFAULT_INSTALL_DIR="$HOME/roomforge"
echo -e "  Install directory (default: ${BLUE}$DEFAULT_INSTALL_DIR${NC})"
INSTALL_DIR=$(ask "  Directory [$DEFAULT_INSTALL_DIR]: " "$DEFAULT_INSTALL_DIR")
INSTALL_DIR="${INSTALL_DIR/#\~/$HOME}"

mkdir -p "$INSTALL_DIR"
echo -e "  ${GREEN}✓${NC} Using: $INSTALL_DIR"

DEFAULT_PORT="8000"
echo ""
echo -e "  Web UI port (default: ${BLUE}$DEFAULT_PORT${NC})"
PORT=$(ask "  Port [$DEFAULT_PORT]: " "$DEFAULT_PORT")

# ============================================
# Step 4: Download & Install
# ============================================
echo ""
echo -e "${YELLOW}[4/5] Downloading and installing...${NC}"

echo -e "  ${BLUE}Downloading project files...${NC}"

# Backend modules
mkdir -p "$INSTALL_DIR/backend"
for f in __init__ main render ai_render session modify analyze blender_scene \
         claude_api dgx_manager furniture_3d furniture_search material_extract \
         proposals reconstruct; do
    curl -fsSL "$REPO_RAW/backend/${f}.py" -o "$INSTALL_DIR/backend/${f}.py"
done
echo -e "  ${GREEN}✓${NC} Backend (14 modules)"

# Frontend
mkdir -p "$INSTALL_DIR/frontend"
for f in index.html style.css app.js manifest.json; do
    curl -fsSL "$REPO_RAW/frontend/${f}" -o "$INSTALL_DIR/frontend/${f}"
done
echo -e "  ${GREEN}✓${NC} Frontend (4 files)"

# Room templates
mkdir -p "$INSTALL_DIR/sessions/room_templates"
for f in salon sypialnia lazienka_master pokoj_dziecka; do
    curl -fsSL "$REPO_RAW/sessions/room_templates/${f}.json" -o "$INSTALL_DIR/sessions/room_templates/${f}.json"
done
curl -fsSL "$REPO_RAW/sessions/house_style.json" -o "$INSTALL_DIR/sessions/house_style.json" 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} Room templates (4)"

# pyproject.toml
curl -fsSL "$REPO_RAW/pyproject.toml" -o "$INSTALL_DIR/pyproject.toml"

# DGX deploy scripts (optional — for managing DGX services)
mkdir -p "$INSTALL_DIR/dgx"
curl -fsSL "$REPO_RAW/dgx/deploy.sh" -o "$INSTALL_DIR/dgx/deploy.sh" 2>/dev/null || true
chmod +x "$INSTALL_DIR/dgx/deploy.sh" 2>/dev/null || true
echo -e "  ${GREEN}✓${NC} DGX deploy scripts"

# Create Python venv
echo -e "  ${BLUE}Setting up Python environment...${NC}"
$PY -m venv "$INSTALL_DIR/.venv"
"$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"$INSTALL_DIR/.venv/bin/pip" install --quiet \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "python-multipart>=0.0.12" \
    "aiofiles>=24.1.0" \
    "httpx>=0.27.0" \
    "anthropic>=0.45.0"
echo -e "  ${GREEN}✓${NC} Python venv + dependencies"

# Write .env
cat > "$INSTALL_DIR/.env" << ENVEOF
DGX_HOST=$DGX_HOST
ANTHROPIC_API_KEY=$ANTHROPIC_KEY
HOST=0.0.0.0
PORT=$PORT
ENVEOF
echo -e "  ${GREEN}✓${NC} Configuration saved to .env"

# ============================================
# Step 5: Install CLI + systemd
# ============================================
echo ""
echo -e "${YELLOW}[5/5] Installing 'roomforge' command...${NC}"

BIN_DIR="$HOME/.local/bin"
mkdir -p "$BIN_DIR"

cat > "$BIN_DIR/roomforge" << 'WRAPPER'
#!/bin/bash
# RoomForge v__VERSION__
INSTALL_DIR="__INSTALL_DIR__"
ENV_FILE="$INSTALL_DIR/.env"

# Load config
[ -f "$ENV_FILE" ] && set -a && source "$ENV_FILE" && set +a

case "${1:-help}" in
    start)
        echo "Starting RoomForge on port ${PORT:-8000}..."
        cd "$INSTALL_DIR"
        nohup "$INSTALL_DIR/.venv/bin/uvicorn" backend.main:app \
            --host "${HOST:-0.0.0.0}" --port "${PORT:-8000}" \
            > "$INSTALL_DIR/roomforge.log" 2>&1 &
        echo $! > "$INSTALL_DIR/roomforge.pid"
        sleep 2
        if kill -0 "$(cat "$INSTALL_DIR/roomforge.pid")" 2>/dev/null; then
            IP=$(hostname -I | awk '{print $1}')
            echo "RoomForge running: http://${IP}:${PORT:-8000}"
        else
            echo "Failed to start — check $INSTALL_DIR/roomforge.log"
        fi
        ;;
    stop)
        if [ -f "$INSTALL_DIR/roomforge.pid" ]; then
            kill "$(cat "$INSTALL_DIR/roomforge.pid")" 2>/dev/null && echo "Stopped." || echo "Not running."
            rm -f "$INSTALL_DIR/roomforge.pid"
        else
            echo "Not running."
        fi
        ;;
    restart)
        $0 stop
        sleep 1
        $0 start
        ;;
    status)
        if [ -f "$INSTALL_DIR/roomforge.pid" ] && kill -0 "$(cat "$INSTALL_DIR/roomforge.pid")" 2>/dev/null; then
            PID=$(cat "$INSTALL_DIR/roomforge.pid")
            echo "RoomForge running (PID $PID)"
            echo "  http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
        else
            echo "RoomForge not running."
        fi
        # DGX services
        echo ""
        echo "DGX Services (${DGX_HOST:-192.168.0.200}):"
        for svc in "Blender:8005" "Flux:8001" "CHORD:8002" "TRELLIS:8003" "DN-Splatter:8004"; do
            NAME="${svc%%:*}"; PORT_N="${svc##*:}"
            if curl -sf --max-time 2 "http://${DGX_HOST:-192.168.0.200}:${PORT_N}/health" > /dev/null 2>&1; then
                echo "  ✓ $NAME (:$PORT_N)"
            else
                echo "  ✗ $NAME (:$PORT_N)"
            fi
        done
        ;;
    logs)
        tail -f "$INSTALL_DIR/roomforge.log"
        ;;
    config)
        ${EDITOR:-nano} "$INSTALL_DIR/.env"
        echo "Config saved. Run: roomforge restart"
        ;;
    dgx)
        shift
        cd "$INSTALL_DIR" && bash dgx/deploy.sh "$@"
        ;;
    update)
        echo "Updating RoomForge..."
        curl -fsSL https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main/install.sh | bash
        ;;
    *)
        echo "RoomForge — AI Interior Designer v__VERSION__"
        echo ""
        echo "Usage: roomforge <command>"
        echo ""
        echo "Commands:"
        echo "  start          Start web server"
        echo "  stop           Stop web server"
        echo "  restart        Restart web server"
        echo "  status         Show status + DGX services"
        echo "  logs           Follow server logs"
        echo "  config         Edit .env configuration"
        echo "  dgx <cmd>      Manage DGX services (deploy.sh passthrough)"
        echo "  update         Re-run installer to update"
        echo ""
        echo "Web UI: http://$(hostname -I | awk '{print $1}'):${PORT:-8000}"
        echo "Install: $INSTALL_DIR"
        ;;
esac
WRAPPER

sed -i "s|__INSTALL_DIR__|$INSTALL_DIR|g; s|__VERSION__|$VERSION|g" "$BIN_DIR/roomforge"
chmod +x "$BIN_DIR/roomforge"
echo -e "  ${GREEN}✓${NC} Installed to $BIN_DIR/roomforge"

# Add to PATH if needed
if [[ ":$PATH:" != *":$BIN_DIR:"* ]]; then
    SHELL_RC=""
    [ -f "$HOME/.zshrc" ] && SHELL_RC="$HOME/.zshrc"
    [ -f "$HOME/.bashrc" ] && SHELL_RC="${SHELL_RC:-$HOME/.bashrc}"

    if [ -n "$SHELL_RC" ] && ! grep -q ".local/bin" "$SHELL_RC" 2>/dev/null; then
        echo 'export PATH="$HOME/.local/bin:$PATH"' >> "$SHELL_RC"
        echo -e "  ${GREEN}✓${NC} Added ~/.local/bin to PATH in $SHELL_RC"
    fi
fi

# Systemd (if available and running as root or with lingering)
if [ -d /etc/systemd/system ] || [ -d "$HOME/.config/systemd/user" ]; then
    echo ""
    SYSTEMD=$(ask "  Install systemd service for auto-start? [Y/n]: " "Y")
    if [[ "$SYSTEMD" =~ ^[Yy] ]]; then
        if [ "$(id -u)" -eq 0 ]; then
            cat > /etc/systemd/system/roomforge.service << SVCEOF
[Unit]
Description=RoomForge — AI Interior Designer
After=network.target

[Service]
Type=simple
User=$(logname 2>/dev/null || echo root)
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF
            systemctl daemon-reload
            systemctl enable roomforge
            echo -e "  ${GREEN}✓${NC} systemd service installed (systemctl start roomforge)"
        else
            mkdir -p "$HOME/.config/systemd/user"
            cat > "$HOME/.config/systemd/user/roomforge.service" << SVCEOF
[Unit]
Description=RoomForge — AI Interior Designer
After=network.target

[Service]
Type=simple
WorkingDirectory=$INSTALL_DIR
EnvironmentFile=$INSTALL_DIR/.env
ExecStart=$INSTALL_DIR/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port $PORT
Restart=always
RestartSec=5

[Install]
WantedBy=default.target
SVCEOF
            systemctl --user daemon-reload
            systemctl --user enable roomforge
            echo -e "  ${GREEN}✓${NC} User systemd service (systemctl --user start roomforge)"
        fi
    fi
fi

# ============================================
# Done
# ============================================
echo ""
echo -e "${GREEN}=============================================="
echo "         Installation Complete!               "
echo "==============================================${NC}"
echo ""
echo "Quick start:"
echo "  roomforge start      # Start web server"
echo "  roomforge status     # Check DGX services"
echo "  roomforge config     # Edit API keys"
echo ""
IP=$(hostname -I 2>/dev/null | awk '{print $1}')
echo "Web UI: http://${IP:-localhost}:${PORT}"
echo "Install: $INSTALL_DIR"
echo ""
if [ -z "$ANTHROPIC_KEY" ]; then
    echo -e "${YELLOW}Don't forget to set ANTHROPIC_API_KEY:${NC}"
    echo "  roomforge config"
    echo ""
fi
