#!/usr/bin/env bash
# Provision the one exact command used by the restricted playback helper.
set -euo pipefail
if [[ $EUID != 0 || $# != 1 || ! $1 =~ ^[a-z_][a-z0-9_-]*$ ]] || ! id "$1" >/dev/null 2>&1; then
  echo 'Run with sudo and pass the existing Pine64 audio username.' >&2
  exit 1
fi
if [[ $(id -u "$1") == 0 ]]; then exit 1; fi
temp=$(mktemp)
trap 'rm -f "$temp"' EXIT
printf '%s ALL=(root) NOPASSWD: /usr/bin/systemd-run --on-active=5s --unit=unicorntunes-poweroff /usr/bin/systemctl poweroff --no-wall\n' "$1" > "$temp"
visudo -cf "$temp"
install -o root -g root -m 0440 "$temp" /etc/sudoers.d/unicorntunes-player-shutdown
