#!/usr/bin/env bash
# One-time setup for a fresh Ubuntu 22.04/24.04 VM (x86 or Arm, e.g. Oracle
# Always Free). Run as a sudo-capable user:
#
#   curl -fsSL https://raw.githubusercontent.com/enzocampomanesv/s1drops/v2/deploy/bootstrap-vm.sh | bash
#   # or, after cloning:  bash deploy/bootstrap-vm.sh
#
# Installs Docker and Tailscale, opens only SSH, creates the data directories,
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

# Publishing is outbound-only (Funnel or cloudflared), so nothing but SSH needs
# to be reachable. Oracle's images already ship iptables rules that do exactly
# that, and layering ufw on top of them is a known way to lose SSH - skip it there.
if [ ! -f /etc/iptables/rules.v4 ]; then
  sudo ufw allow OpenSSH
  sudo ufw --force enable
fi

if ! command -v tailscale >/dev/null; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

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
  1. Set the admin password in $APP_DIR/deploy/.env.
  2. cd $APP_DIR/deploy && docker compose up -d --build
  3. sudo tailscale up --hostname s1drops      # open the printed login link
     sudo tailscale funnel --bg 8765          # prints the public https URL
  4. (optional) scp lcz_cog.tif to $DATA_DIR/lcz/ and set S1DROPS_LCZ_PATH in .env.
EOF
