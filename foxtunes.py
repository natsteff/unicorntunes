#!/usr/bin/env python3
"""Local-only UnicornTunes feasibility CLI. Python standard library only."""
import argparse
import base64
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import secrets
import sqlite3
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser

ROOT = Path(__file__).resolve().parent
DATA = Path(os.environ.get('FOXTUNES_DATA_DIR', ROOT / '.local-work' / 'runtime'))
REDIRECT = 'http://127.0.0.1:8765/callback'
SCOPES = 'user-read-playback-state user-read-currently-playing user-modify-playback-state playlist-read-private playlist-read-collaborative'
TRACK = re.compile(r'^spotify:track:[A-Za-z0-9]{22}$')


class Failure(Exception):
    def __init__(self, message, uncertain=False, status=None):
        super().__init__(message)
        self.uncertain = uncertain
        self.status = status


def private_write(path, value):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    os.chmod(path.parent, 0o700)
    temp = path.with_suffix('.tmp')
    fd = os.open(str(temp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, 'w') as handle:
        json.dump(value, handle)
    os.replace(temp, path)
    os.chmod(path, 0o600)


def load(path):
    try:
        return json.loads(path.read_text())
    except FileNotFoundError:
        raise Failure('Run setup and login first.')


def request_json(url, method='GET', body=None, headers=None, on_response=None):
    request = urllib.request.Request(url, data=body, method=method, headers=headers or {})
    try:
        with urllib.request.urlopen(request, timeout=15) as response:
            if on_response:on_response(response.status)
            # Player mutations have no response document; acceptance is not playback.
            if url.startswith('https://api.spotify.com/v1/') and method != 'GET':
                return {}
            raw = response.read()
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as error:
        # Never print remote response bodies, authorization codes or tokens.
        retry = error.headers.get('Retry-After', 'unspecified')
        if error.code == 429:
            raise Failure('Spotify returned 429; stop and retry after %s seconds. Quota may require a longer wait.' % retry, status=429)
        raise Failure('Spotify HTTP %s. Check account, scopes and device; no automatic retry.' % error.code, status=error.code)
    except (OSError, ValueError) as error:
        raise Failure('Network/response failure; reconcile before retrying a playback command.', uncertain=method != 'GET') from error


class Spotify:
    def __init__(self, data=DATA):
        self.data = data

    def token(self):
        config = load(self.data / 'config.json')
        tokens = load(self.data / 'tokens.json')
        if tokens.get('expires_at', 0) <= time.time() + 60:
            if not tokens.get('refresh_token'):
                raise Failure('Host login required.')
            fresh = request_json('https://accounts.spotify.com/api/token', 'POST',
                urllib.parse.urlencode({'grant_type': 'refresh_token', 'refresh_token': tokens['refresh_token'],
                                        'client_id': config['client_id']}).encode(),
                {'Content-Type': 'application/x-www-form-urlencoded'})
            tokens.update(fresh)
            tokens['expires_at'] = time.time() + fresh.get('expires_in', 3600)
            private_write(self.data / 'tokens.json', tokens)
        return tokens['access_token']

    def call(self, path, method='GET', payload=None, params=None):
        # Only fixed relative Spotify API paths are accepted by callers.
        if not path.startswith('/') or '://' in path or '?' in path:
            raise Failure('Invalid API path.')
        url = 'https://api.spotify.com/v1' + path
        if params:
            url += '?' + urllib.parse.urlencode(params)
        try:
            self.last_status = None
            return request_json(url, method, json.dumps(payload).encode() if payload is not None else None,
                                {'Authorization': 'Bearer ' + self.token(), 'Content-Type': 'application/json'},
                                on_response=lambda status: setattr(self, 'last_status', status))
        except Failure as error:
            # Fixed API path only: no query, account ID, token or response body.
            raise Failure(method + ' ' + path + ': ' + str(error), uncertain=error.uncertain, status=error.status) from error


class Ledger:
    def __init__(self, data=DATA):
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        os.chmod(data, 0o700)
        self.db = sqlite3.connect(str(data / 'requests.sqlite3'))
        self.db.row_factory = sqlite3.Row
        os.chmod(data / 'requests.sqlite3', 0o600)
        self.db.executescript('''
            CREATE TABLE IF NOT EXISTS requests (
                id INTEGER PRIMARY KEY, guest TEXT NOT NULL, uri TEXT NOT NULL,
                state TEXT NOT NULL, device TEXT, created REAL NOT NULL, detail TEXT);
            CREATE TABLE IF NOT EXISTS observations (
                id INTEGER PRIMARY KEY, observed REAL NOT NULL, device TEXT,
                uri TEXT, playing INTEGER, progress INTEGER);
        ''')

    def submit(self, guest, uri):
        if not TRACK.fullmatch(uri):
            raise Failure('Use a Spotify track URI with a 22-character ID.')
        if not re.fullmatch(r'[A-Za-z0-9_-]{1,32}', guest):
            raise Failure('Guest label must be 1–32 letters, digits, hyphens or underscores.')
        duplicate = self.db.execute("SELECT id FROM requests WHERE uri=? AND state NOT IN ('failed','closed')", (uri,)).fetchone()
        if duplicate:
            raise Failure('This track already has an open request.')
        count = self.db.execute("SELECT count(*) FROM requests WHERE guest=? AND state='requested'", (guest,)).fetchone()[0]
        if count >= 3:
            raise Failure('Guest has three pending requests.')
        row = self.db.execute("INSERT INTO requests(guest,uri,state,created) VALUES(?,?,'requested',?)", (guest, uri, time.time()))
        self.db.commit()
        return row.lastrowid

    def rows(self):
        return [dict(row) for row in self.db.execute('SELECT * FROM requests ORDER BY id')]

    def dispatch(self, api, device):
        # BEGIN IMMEDIATE serializes dispatch across CLI invocations. No mutation retries.
        self.db.execute('BEGIN IMMEDIATE')
        unresolved = self.db.execute("SELECT id FROM requests WHERE state IN ('dispatching','uncertain','command-accepted')").fetchone()
        if unresolved:
            self.db.rollback()
            raise Failure('Observe or reconcile the outstanding dispatch before sending another.')
        row = self.db.execute("SELECT * FROM requests WHERE state='requested' ORDER BY id LIMIT 1").fetchone()
        if not row:
            self.db.rollback()
            raise Failure('No pending requests.')
        self.db.execute("UPDATE requests SET state='dispatching', device=? WHERE id=?", (device, row['id']))
        self.db.commit()
        try:
            api.call('/me/player/queue', 'POST', params={'uri': row['uri'], 'device_id': device})
            state, detail = 'command-accepted', 'Spotify accepted command; playback not yet observed'
        except Failure as error:
            state, detail = ('uncertain' if error.uncertain else 'failed'), str(error)
        self.db.execute('UPDATE requests SET state=?, detail=? WHERE id=?', (state, detail, row['id']))
        self.db.commit()
        return {'id': row['id'], 'state': state, 'detail': detail}

    def observe_queue(self, queue, device):
        uris = [item.get('uri') for item in queue.get('queue', [])]
        for row in self.db.execute("SELECT id,uri FROM requests WHERE state='command-accepted' AND device=?", (device,)).fetchall():
            if uris.count(row['uri']) == 1:
                self.db.execute("UPDATE requests SET state='queued-observed', detail='Track appears once in Spotify queue on observed active device; playback unproven' WHERE id=?", (row['id'],))
        self.db.commit()
        return uris

    def observe(self, playback):
        item = playback.get('item') or {}
        device = (playback.get('device') or {}).get('id')
        uri = item.get('uri')
        playing = bool(playback.get('is_playing'))
        progress = playback.get('progress_ms')
        self.db.execute('INSERT INTO observations(observed,device,uri,playing,progress) VALUES(?,?,?,?,?)',
                        (time.time(), device, uri, int(playing), progress))
        if playing and uri and device:
            # Only a unique accepted request on its intended device can be attributed.
            rows = self.db.execute("SELECT id FROM requests WHERE uri=? AND device=? AND state IN ('command-accepted','queued-observed')", (uri, device)).fetchall()
            if len(rows) == 1:
                self.db.execute("UPDATE requests SET state='observed-playing', detail='Matching track/device observed playing; completion unproven' WHERE id=?", (rows[0]['id'],))
        self.db.commit()
        return {'device': device, 'uri': uri, 'is_playing': playing, 'progress_ms': progress,
                'note': 'Snapshot only; no audible output or completion claim.'}


def login(data=DATA, open_browser=True):
    config = load(data / 'config.json')
    state, verifier = secrets.token_urlsafe(32), secrets.token_urlsafe(64)
    challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
    result = {}

    class Callback(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass  # Callback URLs contain confidential authorization codes.

        def do_GET(self):
            parsed = urllib.parse.urlsplit(self.path)
            query = urllib.parse.parse_qs(parsed.query)
            valid = (self.headers.get('Host') == '127.0.0.1:8765' and parsed.path == '/callback'
                     and secrets.compare_digest(query.get('state', [''])[0], state))
            if not valid:
                self.send_error(400, 'Invalid OAuth callback')
                return
            result.update({'code': query.get('code', [''])[0], 'error': query.get('error', [''])[0]})
            self.send_response(200)
            self.send_header('Content-Type', 'text/plain')
            self.send_header('Cache-Control', 'no-store')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.end_headers()
            self.wfile.write(b'Return to the UnicornTunes terminal. This window can be closed.')

    with http.server.HTTPServer((os.environ.get('FOXTUNES_OAUTH_BIND', '127.0.0.1'), 8765), Callback) as server:
        server.timeout = 1
        url = 'https://accounts.spotify.com/authorize?' + urllib.parse.urlencode({
            'client_id': config['client_id'], 'response_type': 'code', 'redirect_uri': REDIRECT,
            'scope': SCOPES, 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})
        print('Opening Spotify host authorization. Waiting up to 3 minutes on loopback.')
        if open_browser:
            webbrowser.open(url)
        else:
            print(url)
        deadline = time.monotonic() + 180
        while not result and time.monotonic() < deadline:
            server.handle_request()
    if not result.get('code'):
        raise Failure('Login denied or timed out; no tokens stored.')
    tokens = request_json('https://accounts.spotify.com/api/token', 'POST', urllib.parse.urlencode({
        'client_id': config['client_id'], 'grant_type': 'authorization_code', 'code': result['code'],
        'redirect_uri': REDIRECT, 'code_verifier': verifier}).encode(),
        {'Content-Type': 'application/x-www-form-urlencoded'})
    if not tokens.get('access_token'):
        raise Failure('Token response missing access token.')
    tokens['expires_at'] = time.time() + tokens.get('expires_in', 3600)
    private_write(data / 'tokens.json', tokens)
    print('Host authorized. Tokens stored privately on this computer.')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('setup')
    sub.add_parser('login').add_argument('--no-browser', action='store_true')
    sub.add_parser('devices')
    sub.add_parser('search').add_argument('query')
    add = sub.add_parser('submit')
    add.add_argument('--guest', required=True)
    add.add_argument('uri')
    three = sub.add_parser('three-guests')
    three.add_argument('uris', nargs=3)
    sub.add_parser('status')
    sub.add_parser('observe')
    sub.add_parser('spotify-queue')
    for name in ['dispatch', 'transfer', 'skip']:
        action = sub.add_parser(name)
        action.add_argument('--device', required=True)
        if name in ('transfer', 'skip'):
            action.add_argument('--confirm', action='store_true', required=True, help='May interrupt current playback')
    close = sub.add_parser('resolve')
    close.add_argument('id', type=int)
    close.add_argument('--note', required=True, help='Manual reconciliation; does not assert played/completed')
    sub.add_parser('logout')
    args = parser.parse_args()
    os.umask(0o077)
    try:
        if args.command == 'setup':
            client = input('UnicornTunes Spotify client ID (not client secret): ').strip()
            if not re.fullmatch(r'[a-fA-F0-9]{32}', client):
                raise Failure('Expected a 32-character Spotify client ID.')
            if (DATA / 'tokens.json').exists():
                raise Failure('Log out before changing client ID.')
            private_write(DATA / 'config.json', {'client_id': client})
            print('Configured. Register callback exactly: ' + REDIRECT)
            return
        if args.command == 'login':
            login(DATA, open_browser=not args.no_browser)
            return
        if args.command == 'logout':
            (DATA / 'tokens.json').unlink(missing_ok=True)
            print('Local tokens removed. Revoke UnicornTunes access in Spotify account settings to disconnect fully.')
            return
        ledger, api = Ledger(DATA), Spotify(DATA)
        if args.command == 'submit':
            output = {'id': ledger.submit(args.guest, args.uri), 'state': 'requested'}
        elif args.command == 'three-guests':
            if len(set(args.uris)) != 3 or any(not TRACK.fullmatch(uri) for uri in args.uris):
                raise Failure('Provide three different valid track URIs.')
            # All-or-nothing simulation, including duplicate/limit validation.
            try:
                ledger.db.execute('SAVEPOINT guests')
                ids = []
                for index, uri in enumerate(args.uris, 1):
                    if ledger.db.execute("SELECT id FROM requests WHERE uri=? AND state NOT IN ('failed','closed')", (uri,)).fetchone():
                        raise Failure('One of these tracks already has an open request.')
                    if ledger.db.execute("SELECT count(*) FROM requests WHERE guest=? AND state='requested'", ('guest-%s' % index,)).fetchone()[0] >= 3:
                        raise Failure('A simulated guest already has three pending requests.')
                for index, uri in enumerate(args.uris, 1):
                    row = ledger.db.execute("INSERT INTO requests(guest,uri,state,created) VALUES(?,?,'requested',?)", ('guest-%s' % index, uri, time.time()))
                    ids.append(row.lastrowid)
                ledger.db.execute('RELEASE guests')
                ledger.db.commit()
            except Exception:
                ledger.db.rollback()
                raise
            output = {'ids': ids, 'state': 'requested', 'mode': 'local simulated guests; no Spotify login or playback'}
        elif args.command == 'status':
            output = ledger.rows()
        elif args.command == 'search':
            result = api.call('/search', params={'q': args.query, 'type': 'track', 'limit': 5})
            output = [{'uri': item.get('uri'), 'name': item.get('name'),
                       'artists': [a.get('name') for a in item.get('artists', [])],
                       'spotify_url': item.get('external_urls', {}).get('spotify')}
                      for item in result.get('tracks', {}).get('items', [])]
        elif args.command == 'devices':
            output = api.call('/me/player/devices').get('devices', [])
        elif args.command == 'observe':
            output = ledger.observe(api.call('/me/player'))
        elif args.command == 'spotify-queue':
            before = api.call('/me/player')
            result = api.call('/me/player/queue')
            after = api.call('/me/player')
            device = (before.get('device') or {}).get('id')
            if device and device == (after.get('device') or {}).get('id'):
                ledger.observe_queue(result, device)
            output = {'current': (result.get('currently_playing') or {}).get('uri'),
                      'upcoming': [item.get('uri') for item in result.get('queue', [])]}
        elif args.command in ('dispatch', 'transfer', 'skip'):
            devices = api.call('/me/player/devices').get('devices', [])
            if not any(d.get('id') == args.device and not d.get('is_restricted') for d in devices):
                raise Failure('Selected device is missing or restricted; rediscover devices.')
            if args.command == 'dispatch':
                output = ledger.dispatch(api, args.device)
            elif args.command == 'transfer':
                api.call('/me/player', 'PUT', {'device_ids': [args.device], 'play': True})
                output = {'state': 'transfer-command-accepted', 'next': 'Run observe to verify active device.'}
            else:
                api.call('/me/player/next', 'POST', params={'device_id': args.device})
                output = {'state': 'skip-command-accepted', 'next': 'Run observe; completion is not established.'}
        elif args.command == 'resolve':
            row = ledger.db.execute('SELECT state FROM requests WHERE id=?', (args.id,)).fetchone()
            if not row or row['state'] == 'requested':
                raise Failure('Resolve only an existing attempted request after manual reconciliation.')
            ledger.db.execute("UPDATE requests SET state='closed', detail=? WHERE id=?", ('Manually closed, playback/completion not asserted: ' + args.note[:300], args.id))
            ledger.db.commit()
            output = {'id': args.id, 'state': 'closed', 'note': 'No automatic retry; submit again only after checking Spotify.'}
        print(json.dumps(output, indent=2))
    except (Failure, OSError, sqlite3.Error) as error:
        # OS errors can contain paths but never OAuth query data.
        print('UnicornTunes: ' + (str(error) if isinstance(error, Failure) else 'Local operation failed; check permissions, storage and callback port.'))
        return 1
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
