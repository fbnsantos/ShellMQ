#!/usr/bin/env bash
# ─────────────────────────────────────────────────────────────────────────────
# mole — installer
# Installs the mole server as a systemd service that starts on boot.
#
# Usage:
#   sudo bash install.sh [OPTIONS]
#
# Options:
#   --broker      MQTT broker address        (default: localhost)
#   --port        MQTT broker port           (default: 1883)
#   --device-id   Device identifier          (default: hostname)
#   --username    MQTT username              (optional)
#   --password    MQTT password              (optional)
#   --shell       Shell to expose            (default: /bin/bash)
#   --tls         Enable TLS
#   --user        System user to run as      (default: current user)
#   --install-dir Installation directory     (default: /opt/mole)
#   --uninstall   Remove mole service
# ─────────────────────────────────────────────────────────────────────────────

set -euo pipefail

# ── colours ──────────────────────────────────────────────────────────────────
RED='\033[0;31m'; GREEN='\033[0;32m'; YELLOW='\033[1;33m'
CYAN='\033[0;36m'; BOLD='\033[1m'; RESET='\033[0m'

info()    { echo -e "${CYAN}[mole]${RESET} $*"; }
success() { echo -e "${GREEN}[mole]${RESET} $*"; }
warn()    { echo -e "${YELLOW}[mole]${RESET} $*"; }
error()   { echo -e "${RED}[mole] ERROR:${RESET} $*" >&2; exit 1; }

# ── defaults ─────────────────────────────────────────────────────────────────
BROKER="localhost"
PORT="1883"
DEVICE_ID="$(hostname -s)"
USERNAME=""
PASSWORD=""
SHELL_BIN="/bin/bash"
TLS=""
RUN_AS="${SUDO_USER:-$USER}"
INSTALL_DIR="/opt/mole"
UNINSTALL=0
SERVICE_NAME="mole"

# ── parse arguments ───────────────────────────────────────────────────────────
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

# ── check root ────────────────────────────────────────────────────────────────
if [[ $EUID -ne 0 ]]; then
    error "Please run with sudo: sudo bash install.sh"
fi

# ── uninstall ─────────────────────────────────────────────────────────────────
if [[ $UNINSTALL -eq 1 ]]; then
    info "Removing mole service..."
    systemctl stop  "$SERVICE_NAME" 2>/dev/null || true
    systemctl disable "$SERVICE_NAME" 2>/dev/null || true
    rm -f "/etc/systemd/system/${SERVICE_NAME}.service"
    systemctl daemon-reload
    success "Service removed. Files in ${INSTALL_DIR} were kept."
    exit 0
fi

# ── checks ────────────────────────────────────────────────────────────────────
command -v python3 >/dev/null || error "python3 not found"
command -v pip3    >/dev/null || error "pip3 not found"
id "$RUN_AS" >/dev/null 2>&1 || error "User '$RUN_AS' not found"

PYTHON="$(command -v python3)"
info "Using Python: $PYTHON"
info "Running as user: $RUN_AS"

# ── install directory ─────────────────────────────────────────────────────────
info "Installing to ${INSTALL_DIR} ..."
mkdir -p "$INSTALL_DIR"

# copy server script
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cp "${SCRIPT_DIR}/server.py" "${INSTALL_DIR}/server.py"
chown -R "$RUN_AS:$RUN_AS" "$INSTALL_DIR"
success "Copied server.py to ${INSTALL_DIR}"

# ── virtual environment ───────────────────────────────────────────────────────
VENV_DIR="${INSTALL_DIR}/.venv"
if [[ ! -d "$VENV_DIR" ]]; then
    info "Creating virtual environment..."
    sudo -u "$RUN_AS" python3 -m venv "$VENV_DIR"
fi

info "Installing paho-mqtt..."
sudo -u "$RUN_AS" "${VENV_DIR}/bin/pip" install --quiet --upgrade paho-mqtt
success "paho-mqtt installed"

VENV_PYTHON="${VENV_DIR}/bin/python3"

# ── build ExecStart command ───────────────────────────────────────────────────
EXEC_CMD="${VENV_PYTHON} ${INSTALL_DIR}/server.py"
EXEC_CMD+=" --broker ${BROKER}"
EXEC_CMD+=" --port ${PORT}"
EXEC_CMD+=" --device-id ${DEVICE_ID}"
EXEC_CMD+=" --shell ${SHELL_BIN}"
[[ -n "$USERNAME" ]] && EXEC_CMD+=" --username ${USERNAME}"
[[ -n "$PASSWORD" ]] && EXEC_CMD+=" --password ${PASSWORD}"
[[ -n "$TLS"      ]] && EXEC_CMD+=" ${TLS}"

# ── write config file ─────────────────────────────────────────────────────────
CONFIG_FILE="${INSTALL_DIR}/mole.conf"
cat > "$CONFIG_FILE" << EOF
# mole configuration — edit and run install.sh again to apply changes
BROKER=${BROKER}
PORT=${PORT}
DEVICE_ID=${DEVICE_ID}
USERNAME=${USERNAME}
PASSWORD=${PASSWORD}
SHELL=${SHELL_BIN}
TLS=${TLS}
EOF
chown "$RUN_AS:$RUN_AS" "$CONFIG_FILE"
chmod 600 "$CONFIG_FILE"
success "Config saved to ${CONFIG_FILE}"

# ── write systemd unit ────────────────────────────────────────────────────────
UNIT_FILE="/etc/systemd/system/${SERVICE_NAME}.service"
cat > "$UNIT_FILE" << EOF
[Unit]
Description=mole — remote bash shell over MQTT
Documentation=https://github.com/yourusername/mole
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${RUN_AS}
WorkingDirectory=${INSTALL_DIR}
ExecStart=${EXEC_CMD}
Restart=always
RestartSec=10
StandardOutput=journal
StandardError=journal
SyslogIdentifier=mole

# Restart on failure but not on clean exit
SuccessExitStatus=0
RestartPreventExitStatus=0

[Install]
WantedBy=multi-user.target
EOF

success "Systemd unit written to ${UNIT_FILE}"

# ── enable and start ──────────────────────────────────────────────────────────
systemctl daemon-reload
systemctl enable "$SERVICE_NAME"
systemctl restart "$SERVICE_NAME"

sleep 2
STATUS="$(systemctl is-active "$SERVICE_NAME" 2>/dev/null || true)"

echo ""
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
if [[ "$STATUS" == "active" ]]; then
    success "mole is running! (${STATUS})"
else
    warn "Service status: ${STATUS}"
    warn "Check logs with: journalctl -u mole -f"
fi
echo ""
echo -e "  ${CYAN}Device ID:${RESET}  ${DEVICE_ID}"
echo -e "  ${CYAN}Broker:${RESET}     ${BROKER}:${PORT}"
echo -e "  ${CYAN}Shell:${RESET}      ${SHELL_BIN}"
echo -e "  ${CYAN}Install dir:${RESET} ${INSTALL_DIR}"
echo ""
echo -e "  ${BOLD}Useful commands:${RESET}"
echo -e "    systemctl status mole"
echo -e "    journalctl -u mole -f"
echo -e "    systemctl restart mole"
echo -e "    sudo bash install.sh --uninstall"
echo -e "${BOLD}─────────────────────────────────────────────${RESET}"
echo ""
