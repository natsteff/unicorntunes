#!/usr/bin/env bash
# Run as the audio user. Replaces only the restricted agent; no service/audio changes.
set -euo pipefail
agent_dir="$HOME/.local/lib/unicorntunes"
if [ ! -f "$agent_dir/mpd-agent.py" ]; then
  echo 'Install the restricted local-player agent first; see LOCAL_FILES.md.' >&2
  exit 1
fi
agent_tmp="$(mktemp "$agent_dir/.mpd-agent.XXXXXX")"
loudness_tmp="$(mktemp "$agent_dir/.loudness.XXXXXX")"
health_tmp="$(mktemp "$agent_dir/.health.XXXXXX")"
trap 'rm -f "$agent_tmp" "$loudness_tmp" "$health_tmp"' EXIT
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.7.0/mpd_agent.py -o "$agent_tmp"
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.7.0/local_loudness.py -o "$loudness_tmp"
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.7.0/player_health.py -o "$health_tmp"
python3 -c 'import ast,sys; [ast.parse(open(p).read()) for p in sys.argv[1:]]' "$agent_tmp" "$loudness_tmp" "$health_tmp"
# Ask an old copy-rendering job to stop at its next safe checkpoint. Never stop audio services.
python3 - <<'PY_CANCEL'
import json
from pathlib import Path
state=Path.home()/'.local/share/unicorntunes-local/loudness'
try:job=json.loads((state/'job.json').read_text())
except FileNotFoundError:job={}
if job.get('running') and job.get('method')!='cached-volume-v1':
    (state/'cancel').touch(mode=0o600)
    print('Requested stop of obsolete copy preparation; its current step may finish first.')
PY_CANCEL
chmod 600 "$health_tmp"
mv "$health_tmp" "$agent_dir/player_health.py"
chmod 600 "$loudness_tmp"
mv "$loudness_tmp" "$agent_dir/local_loudness.py"
chmod 600 "$agent_tmp"
mv "$agent_tmp" "$agent_dir/mpd-agent.py"
printf '%s\n' 'Restricted agent updated. No playback or SSH permissions changed.'
printf '%s\n' '{"action":"status"}' | python3 "$agent_dir/mpd-agent.py"
