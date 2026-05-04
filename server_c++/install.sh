#!/usr/bin/env bash
# mole C++ server installer
# Builds from source and installs as a systemd service.
#
# Usage:
#   sudo bash install.sh [OPTIONS]
#
# Options: same as the Python installer

set -euo pipefail

RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'
info()    { echo -e "${CYAN}[mole]${RESET} $*"; }
success() { echo -e "${GREEN}[mole]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[mole]${RESET} $*"; }
error()   { echo -e "${RED}[mole] ERROR:${RESET} $*" >&2; exit 1; }

BROKER="localhost"; PORT=1883
DEVICE_ID="$(hostname -s)"; USERNAME=""; PASSWORD=""
SHELL_BIN="/bin/bash"; TLS=""
RUN_AS="${SUDO_USER:-$USER}"
INSTALL_DIR="/opt/mole"; BUILD_DIR="/tmp/mole-build"
SERVICE="mole"; UNINSTALL=0

while [[ $# -gt 0 ]]; do
    case "$1" in
        --broker)      BROKER="$2";     shift 2 ;;
        --port)        PORT="$2";       shift 2 ;;
        --device-id)   DEVICE_ID="$2";  shift 2 ;;
        --username)    USERNAME="$2";   shift 2 ;;
        --password)    PASSWORD="$2";   shift 2 ;;
        --shell)       SHELL_BIN="$2";  shift 2 ;;
        --tls)         TLS="--tls";     shift   ;;
        --user)        RUN_AS="$2";     shift 2 ;;
        --install-dir) INSTALL_DIR="$2";shift 2 ;;
        --uninstall)   UNINSTALL=1;     shift   ;;
        *) error "Unknown option: $1" ;;
    esac
done

[[ $EUID -ne 0 ]] && error "Run with sudo"

if [[ $UNINSTALL -eq 1 ]]; then
    info "Removing mole service..."
    systemctl stop    "$SERVICE" 2>/dev/null || true
    systemctl disable "$SERVICE" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE}.service"
    systemctl daemon-reload
    success "Service removed. Files in ${INSTALL_DIR} were kept."
    exit 0
fi

# ── install build dependencies ────────────────────────────────────────────────
info "Installing build dependencies..."
apt-get install -y --no-install-recommends \
    build-essential cmake \
    libssl-dev uuid-dev \
    libpaho-mqtt-dev libpaho-mqttpp-dev \
    nlohmann-json3-dev 2>/dev/null || {
    warn "Some packages may not be available — trying minimal set..."
    apt-get install -y build-essential cmake libssl-dev uuid-dev
    # paho-mqtt: build from source if package not available
    if ! dpkg -l libpaho-mqttpp-dev &>/dev/null; then
        info "Building paho-mqtt from source..."
        build_paho
    fi
}

# ── build ─────────────────────────────────────────────────────────────────────
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
info "Building mole-server..."
rm -rf "$BUILD_DIR" && mkdir -p "$BUILD_DIR"
cp "${SCRIPT_DIR}/server.cpp"      "$BUILD_DIR/"
cp "${SCRIPT_DIR}/CMakeLists.txt"  "$BUILD_DIR/"

pushd "$BUILD_DIR" > /dev/null
cmake -S . -B build -DCMAKE_BUILD_TYPE=Release -DCMAKE_INSTALL_PREFIX="$INSTALL_DIR" 2>&1
cmake --build build --parallel "$(nproc)"
cmake --install build
popd > /dev/null

success "Built and installed to ${INSTALL_DIR}/bin/mole-server"
chown root:root "${INSTALL_DIR}/bin/mole-server"
chmod 755 "${INSTALL_DIR}/bin/mole-server"

# ── environment file (credentials safe from $ and # chars) ───────────────────
mkdir -p "$INSTALL_DIR"
ENV_FILE="${INSTALL_DIR}/mole.env"
cat > "$ENV_FILE" << ENVEOF
MOLE_BROKER=${BROKER}
MOLE_PORT=${PORT}
MOLE_DEVICE_ID=${DEVICE_ID}
MOLE_USERNAME=${USERNAME}
MOLE_PASSWORD=${PASSWORD}
MOLE_SHELL=${SHELL_BIN}
ENVEOF
chmod 600 "$ENV_FILE"
chown "$RUN_AS:$RUN_AS" "$ENV_FILE" 2>/dev/null || true
success "Config saved to ${ENV_FILE}"

# ── build ExecStart ───────────────────────────────────────────────────────────
EXEC="${INSTALL_DIR}/bin/mole-server"
EXEC+=" --broker \${MOLE_BROKER} --port \${MOLE_PORT}"
EXEC+=" --device-id \${MOLE_DEVICE_ID}"
EXEC+=" --shell \${MOLE_SHELL}"
EXEC+=" --username \${MOLE_USERNAME} --password \${MOLE_PASSWORD}"
[[ -n "$TLS" ]] && EXEC+=" --tls"

# ── systemd unit ──────────────────────────────────────────────────────────────
UNIT="/etc/systemd/system/${SERVICE}.service"
cat > "$UNIT" << UNITEOF
[Unit]
Description=mole — remote bash shell over MQTT (C++ server)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_AS}
EnvironmentFile=${INSTALL_DIR}/mole.env
ExecStart=${EXEC}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mole

[Install]
WantedBy=multi-user.target
UNITEOF

systemctl daemon-reload
systemctl enable "$SERVICE"
systemctl restart "$SERVICE"
sleep 2

STATUS="$(systemctl is-active "$SERVICE" 2>/dev/null || true)"
echo ""
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
if [[ "$STATUS" == "active" ]]; then
    success "mole is running!"
else
    warn "Status: ${STATUS} — check: journalctl -u mole -f"
fi
echo -e "  ${CYAN}Device ID:${RESET}  ${DEVICE_ID}"
echo -e "  ${CYAN}Broker:${RESET}     ${BROKER}:${PORT}"
echo -e "  ${CYAN}Binary:${RESET}     ${INSTALL_DIR}/bin/mole-server"
echo ""
echo -e "  ${BOLD}Commands:${RESET}"
echo -e "    systemctl status mole"
echo -e "    journalctl -u mole -f"
echo -e "    sudo bash install.sh --uninstall"
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
