#!/bin/bash
# Deploy the stream service + built UI to the compute VM.
set -euo pipefail
HOST="ubuntu@hazshield-compute"; DEST="/opt/hazshield-stream"
SRC="$(cd "$(dirname "$0")" && pwd)"
UI="$SRC/../hazshield-ui"

command -v git >/dev/null && [ -n "$(git -C "$SRC" status --porcelain 2>/dev/null)" ] && echo "WARN: deploying dirty tree"

echo "-> building UI"
(cd "$UI" && npm run build --silent)
rm -rf "$SRC/dist" && cp -r "$UI/dist" "$SRC/dist"

echo "-> shipping"
ssh "$HOST" "sudo mkdir -p $DEST && sudo chown -R \$(whoami) $DEST"
scp -qr "$SRC/app.py" "$SRC/requirements.txt" "$SRC/dist" "$HOST:$DEST/"
ssh "$HOST" bash -s << 'REMOTE'
set -e
cd /opt/hazshield-stream
[ -d venv ] || python3 -m venv venv
./venv/bin/pip install -q -r requirements.txt
id hazstream &>/dev/null || sudo useradd -r -s /usr/sbin/nologin hazstream
sudo chown -R hazstream:hazstream /opt/hazshield-stream
sudo systemctl restart hazshield-stream
sleep 2
if ! systemctl is-active --quiet hazshield-stream; then
  echo "x service failed to start:"; journalctl -u hazshield-stream -n 15 --no-pager; exit 1
fi
curl -sf http://127.0.0.1:8010/api/stats > /dev/null && echo "OK deploy verified: /api/stats answers"
REMOTE
