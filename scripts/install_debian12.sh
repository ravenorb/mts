#!/usr/bin/env bash
set -euo pipefail

if [[ "${EUID}" -ne 0 ]]; then
  echo "Run as root: sudo bash scripts/install_debian12.sh <repo_url> [branch]"
  exit 1
fi

REPO_URL="${1:-}"
BRANCH="${2:-main}"
INSTALL_DIR="/opt/mts"
APP_USER="mts"
DATA_ROOT="/var/lib/mts"
RUNTIME_SETTINGS_PATH="${DATA_ROOT}/config/runtime_settings.json"

if [[ -z "${REPO_URL}" ]]; then
  echo "Usage: sudo bash scripts/install_debian12.sh <repo_url> [branch]"
  echo "Example: sudo bash scripts/install_debian12.sh https://github.com/your-org/mts.git main"
  exit 1
fi

export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y --no-install-recommends \
  ca-certificates \
  curl \
  git \
  python3 \
  python3-venv \
  python3-pip \
  sudo

id -u "${APP_USER}" >/dev/null 2>&1 || useradd --system --create-home --shell /bin/bash "${APP_USER}"

mkdir -p "${INSTALL_DIR}" "${DATA_ROOT}"/{sql,drawings,pdfs,part_revision_files,config}
chown -R "${APP_USER}:${APP_USER}" "${DATA_ROOT}"

if [[ -d "${INSTALL_DIR}/.git" ]]; then
  sudo -u "${APP_USER}" git -C "${INSTALL_DIR}" fetch --all --prune
  sudo -u "${APP_USER}" git -C "${INSTALL_DIR}" checkout "${BRANCH}"
  sudo -u "${APP_USER}" git -C "${INSTALL_DIR}" pull --ff-only origin "${BRANCH}"
else
  rm -rf "${INSTALL_DIR}"
  sudo -u "${APP_USER}" git clone --branch "${BRANCH}" "${REPO_URL}" "${INSTALL_DIR}"
fi

sudo -u "${APP_USER}" python3 -m venv "${INSTALL_DIR}/.venv"
sudo -u "${APP_USER}" "${INSTALL_DIR}/.venv/bin/pip" install --upgrade pip
sudo -u "${APP_USER}" "${INSTALL_DIR}/.venv/bin/pip" install -r "${INSTALL_DIR}/requirements.txt"

cat > /etc/systemd/system/mts.service <<SERVICE
[Unit]
Description=Manufacturing Tracking System
After=network.target

[Service]
Type=simple
User=${APP_USER}
Group=${APP_USER}
WorkingDirectory=${INSTALL_DIR}
Environment=SQL_DATA_PATH=${DATA_ROOT}/sql/mts.db
Environment=DRAWING_DATA_PATH=${DATA_ROOT}/drawings
Environment=PDF_DATA_PATH=${DATA_ROOT}/pdfs
Environment=PART_FILE_DATA_PATH=${DATA_ROOT}/part_revision_files
Environment=MTS_RUNTIME_SETTINGS_PATH=${RUNTIME_SETTINGS_PATH}
Environment=MTS_PULL_APPLY_COMMAND=sudo -n /bin/systemctl restart mts.service
Environment=SECRET_KEY=change-me
ExecStart=${INSTALL_DIR}/.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 80
Restart=always
RestartSec=3
AmbientCapabilities=CAP_NET_BIND_SERVICE

[Install]
WantedBy=multi-user.target
SERVICE

cat > /etc/sudoers.d/mts-restart <<'SUDOERS'
mts ALL=(root) NOPASSWD: /bin/systemctl restart mts.service
SUDOERS
chmod 0440 /etc/sudoers.d/mts-restart

systemctl daemon-reload
systemctl enable --now mts.service

echo "Install complete."
echo "URL: http://<server-ip>/"
echo "Default login from seed data is admin/admin123 unless already changed."
echo "Persistent data root: ${DATA_ROOT}"
