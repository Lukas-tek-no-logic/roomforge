#!/usr/bin/env bash
# RoomForge — Proxmox LXC Installer
#
# Run on the Proxmox HOST shell:
#   bash -c "$(curl -fsSL https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main/install-proxmox.sh)"
#
# Creates a Debian 12 LXC, installs RoomForge orchestrator inside.
# Supports arm64 (aarch64) and amd64 (x86_64). No Docker needed.

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
BLUE='\033[0;34m'; BOLD='\033[1m'; NC='\033[0m'
CYAN='\033[0;36m'

ok()   { echo -e "  ${GREEN}✓${NC} $*"; }
warn() { echo -e "  ${YELLOW}⚠${NC}  $*"; }
err()  { echo -e "  ${RED}✗${NC} $*"; }
info() { echo -e "  ${BLUE}→${NC} $*"; }
hdr()  { echo -e "\n${CYAN}${BOLD}$*${NC}"; }

ask() {
    local reply
    printf "%b" "$1" > /dev/tty
    read -r reply < /dev/tty
    echo "${reply:-$2}"
}

ask_yn() {
    local reply
    printf "%b" "$1" > /dev/tty
    read -r reply < /dev/tty
    reply="${reply:-$2}"
    [[ "$reply" =~ ^[Yy] ]]
}

REPO_RAW="https://raw.githubusercontent.com/Lukas-tek-no-logic/roomforge/main"

# ── banner ────────────────────────────────────────────────────────────────────
echo -e "${BLUE}${BOLD}"
echo "╔═══════════════════════════════════════════════════╗"
echo "║   RoomForge — AI Interior Designer               ║"
echo "║   Proxmox LXC Installer  (arm64 / amd64)         ║"
echo "╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo "  Lightweight orchestrator for DGX Spark GPU services"
echo "  Python + FastAPI (no Docker inside LXC)"
echo ""

# ── 1. verify Proxmox host ───────────────────────────────────────────────────
hdr "[1/6] Checking Proxmox host..."
if ! command -v pct &>/dev/null; then
    err "pct not found — run this script on the Proxmox VE host shell."
    exit 1
fi
ok "Proxmox VE detected"

HOST_ARCH=$(uname -m)
case "$HOST_ARCH" in
    aarch64) ARCH="arm64" ;;
    x86_64)  ARCH="amd64" ;;
    *) err "Unsupported architecture: $HOST_ARCH"; exit 1 ;;
esac
ok "Host architecture: $ARCH"

# ── 2. find / download Debian 12 template ────────────────────────────────────
hdr "[2/6] Finding Debian 12 LXC template..."

TEMPLATE_FILE=$(find /var/lib/vz/template/cache/ -name "debian-12-*${ARCH}*.tar.*" 2>/dev/null | sort -V | tail -1 || true)

if [[ -z "$TEMPLATE_FILE" ]]; then
    info "Not found locally — checking Proxmox mirrors..."
    pveam update 2>/dev/null || true
    TEMPLATE_NAME=$(pveam available --section system 2>/dev/null \
        | awk '{print $2}' | grep -E "debian-12.*${ARCH}" | sort -V | tail -1 || true)

    if [[ -n "$TEMPLATE_NAME" ]]; then
        pveam download local "$TEMPLATE_NAME"
        TEMPLATE_FILE="/var/lib/vz/template/cache/$TEMPLATE_NAME"
    else
        warn "No ${ARCH} template in Proxmox mirrors. Downloading from linuxcontainers.org..."
        LC_BASE="https://images.linuxcontainers.org/images/debian/bookworm/${ARCH}/default"
        LC_VER=$(curl -s "${LC_BASE}/" | grep -oP '\d{8}_\d+:\d+' | tail -1)
        if [[ -z "$LC_VER" ]]; then
            err "Could not fetch template list from linuxcontainers.org"
            exit 1
        fi
        TEMPLATE_FILE="/var/lib/vz/template/cache/debian-12-standard_${ARCH}.tar.xz"
        info "Downloading rootfs (~100 MB)..."
        wget -q --show-progress "${LC_BASE}/${LC_VER}/rootfs.tar.xz" -O "$TEMPLATE_FILE"
    fi
fi

ok "Template: $(basename "$TEMPLATE_FILE")"
TEMPLATE_STOR="local:vztmpl/$(basename "$TEMPLATE_FILE")"

# ── 3. LXC configuration ─────────────────────────────────────────────────────
hdr "[3/6] Configure LXC..."
echo ""

echo "  Available storage:"
pvesm status --content rootdir 2>/dev/null \
    | awk 'NR>1 {printf "    %-20s %s GiB free\n", $1, int($5/1024/1024)}' || true
echo ""

DEFAULT_STORAGE=$(pvesm status --content rootdir 2>/dev/null \
    | awk 'NR>1 {print $1; exit}')
DEFAULT_STORAGE=${DEFAULT_STORAGE:-local}

CTID=$(pvesh get /cluster/nextid 2>/dev/null || echo "200")
CTID=$(ask     "  Container ID [${CTID}]: "        "$CTID")
STORAGE=$(ask  "  Storage [${DEFAULT_STORAGE}]: "  "$DEFAULT_STORAGE")
HOSTNAME=$(ask "  Hostname [roomforge]: "           "roomforge")
RAM=$(ask      "  RAM MB [512]: "                   "512")
DISK=$(ask     "  Disk GB [4]: "                    "4")
CORES=$(ask    "  CPU cores [1]: "                  "1")

echo ""
LAST_IP=$(grep -h '^net0:' /etc/pve/lxc/*.conf 2>/dev/null \
    | grep -oE 'ip=[0-9]+\.[0-9]+\.[0-9]+\.[0-9]+' \
    | grep -oE '[0-9]+$' | sort -n | tail -1 || echo "")
SUGGEST_LAST=$(( ${LAST_IP:-199} + 1 ))
DEFAULT_IP="192.168.0.${SUGGEST_LAST}/24"

CT_IP=$(ask  "  Container IP [${DEFAULT_IP}]: "  "$DEFAULT_IP")
GW=$(ask     "  Gateway [192.168.0.1]: "          "192.168.0.1")

# ── 4. RoomForge configuration ───────────────────────────────────────────────
hdr "[4/6] Configure RoomForge..."
echo ""

DEFAULT_DGX="192.168.0.200"
DGX_HOST=$(ask "  DGX Spark IP [${DEFAULT_DGX}]: " "$DEFAULT_DGX")

DEFAULT_PORT="8000"
RF_PORT=$(ask  "  Web UI port [${DEFAULT_PORT}]: "  "$DEFAULT_PORT")

# ── summary ───────────────────────────────────────────────────────────────────
CT_BARE_IP=$(echo "$CT_IP" | cut -d/ -f1)
echo ""
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo "  Summary:"
echo "    LXC ID:       $CTID"
echo "    Hostname:     $HOSTNAME"
echo "    IP:           $CT_IP"
echo "    RAM/Disk:     ${RAM} MB / ${DISK} GB"
echo "    Arch:         $ARCH"
echo "    DGX Spark:    $DGX_HOST"
echo "    Web UI:       http://${CT_BARE_IP}:${RF_PORT}"
echo -e "${YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━${NC}"
echo ""
ask_yn "  Proceed? [Y/n]: " "y" || { echo "Aborted."; exit 0; }

# ── 5. create LXC ────────────────────────────────────────────────────────────
hdr "[5/6] Creating LXC ${CTID}..."

BRIDGE=$(grep -h 'bridge=' /etc/pve/lxc/*.conf 2>/dev/null \
    | grep -oE 'bridge=[^,]+' | cut -d= -f2 \
    | sort | uniq -c | sort -rn | awk 'NR==1{print $2}' || echo "vmbr0")
BRIDGE=${BRIDGE:-vmbr0}
info "Network bridge: $BRIDGE"

pct create "$CTID" "$TEMPLATE_STOR" \
    --hostname "$HOSTNAME" \
    --memory   "$RAM" \
    --cores    "$CORES" \
    --rootfs   "${STORAGE}:${DISK}" \
    --net0     "name=eth0,bridge=${BRIDGE},ip=${CT_IP},gw=${GW}" \
    --unprivileged 1 \
    --ostype   debian \
    --features "nesting=1" \
    --start    1 \
    --onboot   1

ok "Container $CTID created and started"
info "Waiting for boot..."
sleep 4

# ── 6. install RoomForge inside LXC ──────────────────────────────────────────
hdr "[6/6] Installing RoomForge inside LXC..."

pct exec "$CTID" -- bash -euo pipefail << INSTALL_INNER
export DEBIAN_FRONTEND=noninteractive

# System packages
apt-get update -qq
apt-get install -y -qq python3 python3-venv curl ffmpeg >/dev/null 2>&1

# Detect Python version for venv package
PY_VER=\$(python3 --version 2>&1 | grep -oP '\d+\.\d+')
apt-get install -y -qq "python\${PY_VER}-venv" 2>/dev/null \
    || apt-get install -y -qq python3-venv 2>/dev/null || true

INSTALL_DIR="/opt/roomforge"
mkdir -p "\$INSTALL_DIR/backend" "\$INSTALL_DIR/frontend" "\$INSTALL_DIR/sessions/room_templates" "\$INSTALL_DIR/dgx"

# Download backend
for f in __init__ main render ai_render session modify analyze blender_scene \
         claude_api dgx_manager furniture_3d furniture_search material_extract \
         proposals reconstruct; do
    curl -fsSL "${REPO_RAW}/backend/\${f}.py" -o "\$INSTALL_DIR/backend/\${f}.py"
done

# Download frontend
for f in index.html style.css app.js manifest.json; do
    curl -fsSL "${REPO_RAW}/frontend/\${f}" -o "\$INSTALL_DIR/frontend/\${f}"
done

# Room templates
for f in salon sypialnia lazienka_master pokoj_dziecka; do
    curl -fsSL "${REPO_RAW}/sessions/room_templates/\${f}.json" -o "\$INSTALL_DIR/sessions/room_templates/\${f}.json"
done
curl -fsSL "${REPO_RAW}/sessions/house_style.json" -o "\$INSTALL_DIR/sessions/house_style.json" 2>/dev/null || true

# pyproject.toml + dgx deploy script
curl -fsSL "${REPO_RAW}/pyproject.toml" -o "\$INSTALL_DIR/pyproject.toml"
curl -fsSL "${REPO_RAW}/dgx/deploy.sh" -o "\$INSTALL_DIR/dgx/deploy.sh" 2>/dev/null || true
chmod +x "\$INSTALL_DIR/dgx/deploy.sh" 2>/dev/null || true

# Python venv
python3 -m venv "\$INSTALL_DIR/.venv"
"\$INSTALL_DIR/.venv/bin/pip" install --quiet --upgrade pip
"\$INSTALL_DIR/.venv/bin/pip" install --quiet \
    "fastapi>=0.115.0" \
    "uvicorn[standard]>=0.32.0" \
    "python-multipart>=0.0.12" \
    "aiofiles>=24.1.0" \
    "httpx>=0.27.0" \
    "anthropic>=0.45.0"

# .env
cat > "\$INSTALL_DIR/.env" << ENVEOF
DGX_HOST=${DGX_HOST}
HOST=0.0.0.0
PORT=${RF_PORT}
ENVEOF

# systemd service
cat > /etc/systemd/system/roomforge.service << SVCEOF
[Unit]
Description=RoomForge — AI Interior Designer
After=network.target

[Service]
Type=simple
WorkingDirectory=\$INSTALL_DIR
EnvironmentFile=\$INSTALL_DIR/.env
ExecStart=\$INSTALL_DIR/.venv/bin/uvicorn backend.main:app --host 0.0.0.0 --port ${RF_PORT}
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
SVCEOF

systemctl daemon-reload
systemctl enable roomforge
systemctl start roomforge

echo "RoomForge installed and running"
INSTALL_INNER

ok "RoomForge installed and running"

# ── verify ────────────────────────────────────────────────────────────────────
info "Waiting for startup..."
sleep 3
if curl -sf --max-time 5 "http://${CT_BARE_IP}:${RF_PORT}/" > /dev/null 2>&1; then
    ok "Web UI responding"
else
    warn "Web UI not responding yet — may need a few more seconds"
fi

# ── Check DGX connectivity from inside LXC ────────────────────────────────────
info "Checking DGX services from LXC..."
for svc in "Blender:8005" "Flux:8001" "CHORD:8002" "TRELLIS:8003" "DN-Splatter:8004"; do
    NAME="${svc%%:*}"; PORT="${svc##*:}"
    if pct exec "$CTID" -- curl -sf --max-time 3 "http://${DGX_HOST}:${PORT}/health" > /dev/null 2>&1; then
        ok "$NAME (:$PORT)"
    else
        warn "$NAME (:$PORT) — not reachable"
    fi
done

# ── done ──────────────────────────────────────────────────────────────────────
echo ""
echo -e "${GREEN}${BOLD}"
echo "╔═══════════════════════════════════════════════════╗"
echo "║   Installation complete!                          ║"
echo "╚═══════════════════════════════════════════════════╝"
echo -e "${NC}"
echo -e "  Web UI:     ${BOLD}http://${CT_BARE_IP}:${RF_PORT}${NC}"
echo -e "  DGX Spark:  ${DGX_HOST}"
echo ""
echo "  Management (from Proxmox host):"
echo "    pct exec ${CTID} -- systemctl status roomforge"
echo "    pct exec ${CTID} -- journalctl -u roomforge -f"
echo "    pct exec ${CTID} -- systemctl restart roomforge"
echo ""
echo "  Update:"
echo "    pct exec ${CTID} -- bash -c 'cd /opt/roomforge && curl -fsSL ${REPO_RAW}/install.sh | bash'"
echo ""
echo "  Config:"
echo "    pct exec ${CTID} -- nano /opt/roomforge/.env"
echo ""
