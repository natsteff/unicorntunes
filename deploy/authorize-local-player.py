#!/usr/bin/env python3
"""Install the restricted MPD agent and authorize one supplied PUBLIC SSH key."""
import base64
from pathlib import Path
import re
import shutil
import sys


def main():
    key = sys.stdin.readline(8193).strip()
    parts = key.split()
    if len(key) > 8192 or len(parts) < 2 or parts[0] != 'ssh-ed25519':
        raise SystemExit('Supply one ssh-ed25519 public key on stdin.')
    try:
        base64.b64decode(parts[1], validate=True)
    except ValueError:
        raise SystemExit('Invalid public key.')
    home = Path.home()
    agent = home / '.local/lib/unicorntunes/mpd-agent.py'
    source = Path(__file__).resolve().parent.parent / 'mpd_agent.py'
    if not source.is_file():
        raise SystemExit('Run this helper from a downloaded UnicornTunes checkout.')
    if not re.fullmatch(r'/[A-Za-z0-9_./ -]+', str(agent)):
        raise SystemExit('Unsupported home directory characters.')
    agent.parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(source, agent)
    agent.chmod(0o600)
    loudness=source.parent/'local_loudness.py'
    shutil.copyfile(loudness,agent.parent/'local_loudness.py')
    (agent.parent/'local_loudness.py').chmod(0o600)
    shutil.copyfile(source.parent/'player_health.py',agent.parent/'player_health.py')
    (agent.parent/'player_health.py').chmod(0o600)
    ssh = home / '.ssh'
    ssh.mkdir(mode=0o700, exist_ok=True)
    ssh.chmod(0o700)
    authorized = ssh / 'authorized_keys'
    existing = authorized.read_text() if authorized.exists() else ''
    # The forced command ignores any command requested by the SSH client.
    entry = f'restrict,command="/usr/bin/python3 \'{agent}\'" {parts[0]} {parts[1]} unicorntunes-local-player'
    lines = existing.splitlines()
    if any(parts[1] in line.split() for line in lines):
        raise SystemExit('This public key already exists. Inspect its restrictions before replacing it.')
    authorized.write_text(existing.rstrip('\n') + ('\n' if existing else '') + entry + '\n')
    authorized.chmod(0o600)
    print('Restricted local-player key installed. No playback was started.')


if __name__ == '__main__':
    main()
