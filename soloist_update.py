"""Stage and apply officially distributed Soloist executables on the audio device."""
import fcntl
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import tarfile
import tempfile
import time

from player_health import command

ARCHIVE = 'https://soloist-builds.spotifycdn.com/soloist_release_arm64.tar.gz'
BUILD_LIFETIME = 90 * 86400
STAGE_WINDOW = 30 * 86400
ROOT = Path.home() / '.local/state/unicorntunes-soloist'
STAGED = ROOT / 'staged'


def version(executable):
    result = subprocess.run([str(executable), '--version'], capture_output=True, text=True, timeout=8, check=True)
    output = result.stdout + result.stderr
    match = re.search(r'\b(1[5-9]\d{8}|2\d{9})\b', output)
    if match:
        stamp = int(match[0])
    else:
        date = re.search(r'\b(20\d{2})[-/]?(0[1-9]|1[0-2])[-/]?([0-2]\d|3[01])\b', output)
        if not date:
            raise ValueError('Soloist did not report a recognizable build timestamp.')
        stamp = int(datetime(*map(int, date.groups()), tzinfo=timezone.utc).timestamp())
    label = re.search(r'\b\d+\.\d+\.\d+(?:\.\d+)?\b', output)
    if not label or not 1451606400 < stamp <= time.time() + 86400:
        raise ValueError('Soloist build version or timestamp is invalid.')
    return {'version': label[0], 'built_at': stamp, 'expires_at': stamp + BUILD_LIFETIME}


def installed(allow_inactive=False):
    result = command(['systemctl', '--user', 'show', 'soloist.service', '-p', 'MainPID', '--value'])
    pid = result.stdout.strip()
    if not result.returncode and pid.isdigit() and int(pid) > 1:
        binary = Path('/proc', pid, 'exe').resolve(strict=True)
    elif allow_inactive and metadata().get('installed_path'):
        binary = Path(metadata()['installed_path']).resolve(strict=True)
    else:
        raise ValueError('Start Soloist once before setting up managed updates.')
    if binary.name != 'soloist' or not binary.is_relative_to(Path.home()) or not binary.is_file():
        raise ValueError('The running Soloist executable is outside the audio user’s home directory.')
    return binary


def metadata():
    try:
        return json.loads((ROOT / 'result.json').read_text())
    except FileNotFoundError:
        return {}


def record(**fields):
    result = dict(metadata(), **fields)
    with tempfile.NamedTemporaryFile(mode='w', dir=ROOT, delete=False) as temp:
        json.dump(result, temp)
    os.replace(temp.name, ROOT / 'result.json')


def status(binary):
    current = version(binary)
    staged = version(STAGED) if STAGED.exists() else None
    if staged and (staged['built_at'] <= current['built_at'] or staged['expires_at'] <= time.time()):
        staged = None
    saved = {key:value for key,value in metadata().items() if key != 'installed_path'}
    return dict(saved, installed=current, staged=staged,
                expiry_warning=current['expires_at'] - time.time() <= STAGE_WINDOW)


def fetch(current, stage):
    with tempfile.TemporaryDirectory(dir=ROOT) as scratch:
        archive = Path(scratch) / 'soloist.tar.gz'
        subprocess.run(['curl', '--fail', '--silent', '--show-error', '--location', '--proto', '=https',
                        '--max-time', '90', '--max-filesize', str(150 * 1024**2), '--output', str(archive), ARCHIVE],
                       capture_output=True, timeout=95, check=True)
        candidate = Path(scratch) / 'soloist'
        with tarfile.open(archive, 'r:gz') as bundle:
            for entry in bundle:
                if Path(entry.name).name != 'soloist' or not entry.isfile():
                    continue
                if not 1 <= entry.size <= 150 * 1024**2:
                    raise ValueError('The Soloist archive has an invalid executable size.')
                with bundle.extractfile(entry) as source, candidate.open('wb') as target:
                    shutil.copyfileobj(source, target)
                break
            else:
                raise ValueError('The Spotify archive does not contain the Soloist executable.')
        candidate.chmod(0o700)
        available = version(candidate)
        newer = available['built_at'] > current['built_at'] and available['expires_at'] > time.time()
        if stage and newer:
            os.replace(candidate, STAGED)
        record(checked_at=int(time.time()), available=available,
               message='New build staged.' if stage and newer else 'New build available.' if newer else 'No newer build available.')


def restart_verified(binary, build):
    try:
        result = command(['systemctl', '--user', 'restart', 'soloist.service'])
    except (OSError, subprocess.SubprocessError):
        return False
    if result.returncode:
        return False
    for _ in range(4):
        time.sleep(1)
        try:
            if installed() == binary and version(binary)['built_at'] == build['built_at']:
                return True
        except (OSError, ValueError, subprocess.SubprocessError):
            continue
    return False


def replace_binary(source, destination):
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as temp:
        with source.open('rb') as stream:
            shutil.copyfileobj(stream, temp)
    candidate = Path(temp.name)
    try:
        candidate.chmod(destination.stat().st_mode & 0o777)
        os.replace(candidate, destination)
    finally:
        candidate.unlink(missing_ok=True)


def apply(binary, current, staged):
    if not staged:
        raise ValueError('Stage a newer Soloist build before applying it.')
    previous = ROOT / 'previous'
    shutil.copy2(binary, previous)
    replace_binary(STAGED, binary)
    if restart_verified(binary, staged):
        STAGED.unlink()
        record(applied_at=int(time.time()), message='Updated and restarted. Confirm discovery in the Host desk.')
    elif current['expires_at'] > time.time():
        replace_binary(previous, binary)
        restored = restart_verified(binary, current)
        record(message='Update failed; previous build restored.' if restored else 'Update and rollback restart failed.')
        raise ValueError(metadata()['message'])
    else:
        record(message='Update failed verification; the previous build is expired.')
        raise ValueError(metadata()['message'])


def run(action):
    ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
    with (ROOT / 'lock').open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError as error:
            raise ValueError('Soloist maintenance is already running.') from error
        binary = installed(allow_inactive=True)
        if metadata().get('installed_path') != str(binary):
            record(installed_path=str(binary))
        current = status(binary)
        try:
            if action in ('check', 'stage') or (action == 'scheduled-stage' and current['expiry_warning'] and not current['staged']):
                fetch(current['installed'], action != 'check')
            elif action == 'apply':
                apply(binary, current['installed'], current['staged'])
        except (OSError, subprocess.SubprocessError, tarfile.TarError) as error:
            record(checked_at=int(time.time()), message='Soloist maintenance could not finish. Check the network and service.')
            raise ValueError(metadata()['message']) from error
        return status(binary)


if __name__ == '__main__':
    import sys
    print(json.dumps(run(sys.argv[1])))
