#!/usr/bin/env bash
# Pull the latest code and redeploy. Run on the VM from anywhere.
#
# The bake queue lives in memory, so restarting the app drops queued and running
# jobs (finished cubes on disk are safe). Check the Admin tab is idle first.
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/s1drops}"
cd "$APP_DIR"

git pull --ff-only
cd deploy
docker compose up -d --build
docker image prune -f
docker compose ps
