#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VM. Run as a sudo-capable user:
#
#   curl -fsSL https://raw.githubusercontent.com/enzocampomanesv/s1drops/v2/deploy/bootstrap-vm.sh | bash
#   # or, after cloning:  bash deploy/bootstrap-vm.sh
#
# Installs Docker, opens only SSH in the firewall, creates the data directories,
# and clones the repo to /opt/s1drops. Safe to re-run.
set -euo pipefail

REPO_URL="${REPO_URL:-https://github.com/enzocampomanesv/s1drops.git}"
BRANCH="${BRANCH:-v2}"
APP_DIR="${APP_DIR:-/opt/s1drops}"
DATA_DIR="${DATA_DIR:-/srv/s1drops}"

if ! command -v docker >/dev/null; then
  curl -fsSL https://get.docker.com | sudo sh
  sudo usermod -aG docker "$USER"
  echo "Added $USER to the docker group - log out and back in before using docker without sudo."
fi

# The tunnel is outbound-only, so nothing but SSH needs to be reachable.
sudo ufw allow OpenSSH
sudo ufw --force enable

sudo mkdir -p "$DATA_DIR/cache" "$DATA_DIR/lcz"
sudo chown -R "$USER":"$USER" "$DATA_DIR"

if [ ! -d "$APP_DIR/.git" ]; then
  sudo mkdir -p "$APP_DIR"
  sudo chown "$USER":"$USER" "$APP_DIR"
  git clone --branch "$BRANCH" "$REPO_URL" "$APP_DIR"
fi

if [ ! -f "$APP_DIR/deploy/.env" ]; then
  cp "$APP_DIR/deploy/.env.example" "$APP_DIR/deploy/.env"
  chmod 600 "$APP_DIR/deploy/.env"
fi

cat <<EOF

Done. Next:
  1. Fill in $APP_DIR/deploy/.env (admin password + tunnel token).
  2. (optional) scp lcz_cog.tif to $DATA_DIR/lcz/ and set S1DROPS_LCZ_PATH in .env.
  3. cd $APP_DIR/deploy && docker compose up -d --build
EOF
