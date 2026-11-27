#!/usr/bin/env bash
# Install a user timer that stages newer builds without restarting playback.
set -euo pipefail
if [ "$(id -u)" -eq 0 ]; then
  echo 'Run as the existing Soloist audio user.' >&2
  exit 1
fi
agent_dir="$HOME/.local/lib/unicorntunes"
test -f "$agent_dir/soloist_update.py" || { echo 'Update the UnicornTunes player agent first.' >&2; exit 1; }
units="$HOME/.config/systemd/user"
mkdir -p "$units"
cat > "$units/unicorntunes-soloist-stage.service" <<'UNIT'
[Unit]
Description=Check and stage a newer Spotify Soloist build

[Service]
Type=oneshot
ExecStart=/usr/bin/python3 %h/.local/lib/unicorntunes/soloist_update.py scheduled-stage
UNIT
cat > "$units/unicorntunes-soloist-stage.timer" <<'UNIT'
[Unit]
Description=Weekly Spotify Soloist staging check

[Timer]
OnCalendar=weekly
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
UNIT
systemctl --user daemon-reload
systemctl --user enable --now unicorntunes-soloist-stage.timer
