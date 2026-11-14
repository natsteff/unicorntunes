#!/usr/bin/env python3
"""Local/LAN UnicornTunes browser feasibility prototype."""
import argparse
import base64
import hashlib
from collections import defaultdict, deque
from http.cookies import SimpleCookie
import http.server
import json
import os
from pathlib import Path
import secrets
import sqlite3
import threading
import time
from urllib.parse import parse_qs, urlsplit, urlencode

import foxtunes as f
from room import RoomFeatures
from local_player import LocalPlayback


def track(item):
    if item.get('source') == 'local':
        return dict(item)
    return {'uri': item.get('uri'), 'name': item.get('name') or 'Unknown track',
            'artists': ', '.join(a.get('name') or 'Unknown artist' for a in (item.get('artists') or [])),
            'art': next((i['url'] for i in ((item.get('album') or {}).get('images') or [])[-1:]), ''),
            'url': (item.get('external_urls') or {}).get('spotify', ''),
            'duration_ms': item.get('duration_ms') or 0,
            'album': (item.get('album') or {}).get('name', ''),
            'year': ((item.get('album') or {}).get('release_date') or '')[:4],
            'explicit': item.get('explicit'),
            'artist_names': [a.get('name') or 'Unknown artist' for a in (item.get('artists') or [])]}


def search_options(query):
    text = query.get('q', [''])[0].strip()
    if len(text) > 150:
        raise ValueError('Search text must be at most 150 characters.')
    parts = [text] if text else []
    for field in ('track', 'artist', 'album'):
        value = query.get(field, [''])[0].strip()
        if len(value) > 150 or any(c in value for c in ('"', '\\')):
            raise ValueError('Search fields must be at most 150 characters without quotes or backslashes.')
        if value:
            parts.append(field + ':"' + value + '"')
    year = query.get('year', [''])[0].strip()
    if year:
        bounds = year.split('-')
        if len(bounds) > 2 or any(len(y) != 4 or not y.isascii() or not y.isdigit() for y in bounds) or bounds[0] > bounds[-1]:
            raise ValueError('Use a year like 2005 or a range like 2000-2009.')
        parts.append('year:' + year)
    exact = query.get('exact', ['0'])[0] == '1'
    title = query.get('track', [''])[0].strip()
    if exact and not title:
        raise ValueError('Enter a song title to use exact title matching.')
    if not parts:
        raise ValueError('Enter search text or at least one search option.')
    return ' '.join(parts), title.casefold() if exact else None


class App(RoomFeatures, LocalPlayback):
    def __init__(self, data=f.DATA, api=None):
        self.data, self.api = data, api or f.Spotify(data)
        data.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.db = sqlite3.connect(str(data / 'web.sqlite3'), check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        os.chmod(data / 'web.sqlite3', 0o600)
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS songs(id INTEGER PRIMARY KEY, guest TEXT, uri TEXT,
            metadata TEXT, state TEXT DEFAULT 'waiting', created REAL, detail TEXT DEFAULT '');
          CREATE TABLE IF NOT EXISTS votes(song INTEGER,guest TEXT,PRIMARY KEY(song,guest));
        ''')
        self.db.execute("UPDATE songs SET state='uncertain', detail='Server restarted during handoff; host reconciliation required' WHERE state='sending'")
        self.db.commit()
        self.lock = threading.RLock()
        self.sessions, self.catalog = {}, {}
        self.limits = defaultdict(deque)
        self.now, self.error, self.updated = None, '', 0
        settings_file = data / 'settings.json'
        saved = f.load(settings_file) if settings_file.exists() else {}
        self.enabled, self.device, self.accepting = False, saved.get('device', ''), saved.get('accepting', True)
        self.reason = 'Session stopped after startup. Verify device and enable handoff.'
        self.public_url = os.environ.get('FOXTUNES_PUBLIC_URL', '').rstrip('/')
        if self.public_url:
            parsed = urlsplit(self.public_url)
            if parsed.scheme not in ('http', 'https') or not parsed.netloc or parsed.path or parsed.query or parsed.fragment or parsed.username:
                raise ValueError('FOXTUNES_PUBLIC_URL must be an HTTP(S) origin without path.')
        client = os.environ.get('SPOTIFY_CLIENT_ID', '').strip()
        if client:
            if not f.re.fullmatch(r'[a-fA-F0-9]{32}', client):
                raise ValueError('Invalid SPOTIFY_CLIENT_ID.')
            existing = f.load(data / 'config.json') if (data / 'config.json').exists() else {}
            if existing.get('client_id') not in (None, client) and (data / 'tokens.json').exists():
                raise ValueError('Disconnect Spotify before changing its client ID.')
            f.private_write(data / 'config.json', {'client_id': client})
        self.last_poll, self.next_poll = 0, 0
        hostfile = data / 'web-host.json'
        if not hostfile.exists():
            f.private_write(hostfile, {'password': secrets.token_urlsafe(24)})
        self.password = f.load(hostfile)['password']
        self.room_init()
        self.idle_ready = False
        self.local_init()

    def session(self, sid):
        now = time.time()
        for key in list(self.sessions):
            if self.sessions[key]['expires'] < now:
                del self.sessions[key]
        if sid not in self.sessions:
            if len(self.sessions) >= 500:
                raise f.Failure('Too many sessions; try later.')
            sid = secrets.token_urlsafe(24)
            self.sessions[sid] = {'csrf': secrets.token_urlsafe(24), 'host': False, 'expires': now + 8 * 3600}
        return sid, self.sessions[sid]

    def limit(self, key, count, seconds=60):
        now = time.monotonic()
        q = self.limits[key]
        while q and q[0] < now - seconds:
            q.popleft()
        if len(q) >= count:
            raise f.Failure('Please wait before trying again.')
        q.append(now)

    def update_idle_state(self, state):
        """Recognize a fresh observed end, not an arbitrary pause or inactive player."""
        item = state.get('item') or {}
        actual = (state.get('device') or {}).get('id')
        previous = self.now or {}
        if state.get('is_playing') or (actual and actual != self.device):
            self.idle_ready = False
            return
        if self.idle_ready:
            if previous.get('source') != 'local' and item.get('uri') and item.get('uri') != previous.get('uri'):
                self.idle_ready = False
            return
        duration = previous.get('duration_ms') or 0
        remaining = max(0, duration - (previous.get('progress_ms') or 0))
        elapsed = time.time() - self.updated if self.updated else float('inf')
        progress = state.get('progress_ms') or 0
        ended_position = not item or progress == 0 or (duration and progress >= duration - 1000)
        same_track = not item or item.get('uri') == previous.get('uri')
        if (self.enabled and previous.get('playing') and duration and 0 <= remaining <= 15000
                and getattr(self, 'observed_device', None) == self.device and self.device
                and 0 <= elapsed <= 30 and elapsed + 1 >= remaining / 1000
                and ended_position and same_track):
            self.idle_ready = True
            self.db.execute("UPDATE songs SET state='past',detail='Observed end; ready for next request' WHERE state='playing'")
            self.db.execute("UPDATE plays SET outcome='unknown' WHERE outcome='observing'")
            self.db.commit()

    def start_idle_request(self, row):
        if row['uri'].startswith('local:'):
            return self.start_local_request(row, self.api.call('/me/player'))
        self.check_rules(row)
        if self.local_bound() and self.local.call('status').get('state') in ('play', 'pause'):
            raise f.Failure('MPD is still active. Spotify was not started; check the local player.')
        self.commit_selection(row['id'])
        self.record_attempt(row,'direct')
        self.idle_ready = False
        try:
            # Queue insertion alone cannot start an exhausted Spotify player.
            self.api.call('/me/player/play', 'PUT', {'uris': [row['uri']]}, {'device_id': self.device})
        except f.Failure as error:
            self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error), row['id']))
            self.db.commit()
            raise
        self.db.execute("UPDATE songs SET state='accepted',detail='Request start accepted; awaiting observed playback' WHERE id=?", (row['id'],))
        self.db.commit()

    def poll(self, force=False):
        if not force and time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + (5 if self.enabled else 20)
        try:
            if self.local_active:
                self.poll_local()
                return
            previous = self.now or {}
            previous_updated = self.updated
            previous_device = getattr(self, 'observed_device', None)
            state = self.api.call('/me/player')
            self.update_idle_state(state)
            self.updated = time.time()
            self.error = ''
            item = state.get('item') or {}
            actual = (state.get('device') or {}).get('id')
            self.observed_device = actual
            self.now = dict(track(item), playing=bool(state.get('is_playing')), progress_ms=state.get('progress_ms', 0)) if item else None
            pending = self.db.execute("SELECT * FROM songs WHERE state IN ('sending','accepted','queued','uncertain') ORDER BY id LIMIT 1").fetchone()
            if pending:self.confirm_attempt(state,pending)
            self.db.execute("UPDATE songs SET state='past', detail='No longer current; completion unverified' WHERE state='playing' AND uri!=?", (item.get('uri', ''),))
            self.db.commit()
            self.observe_play(state)
            self.refresh_upcoming()
            if not self.enabled:
                return
            if actual != self.device and not (self.idle_ready and not actual):
                raise f.Failure('Selected Spotify player is unavailable. Check player health, then refresh devices and start the session.' if not actual else 'Spotify playback moved to another device. Handoff stopped; choose a player and start the session again.')
            if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','accepted','queued','uncertain')").fetchone():
                return
            immediate=self.meta('immediate_request')
            row=next((r for r in self.queue('') if r['state']=='waiting'),None)
            if immediate and row and row['id']==immediate and state.get('is_playing'):
                protected=self.db.execute("SELECT 1 FROM songs WHERE state='playing' AND uri=?",(item.get('uri'),)).fetchone()
                if not protected:
                    self.set_meta('immediate_request',None)
                    self.start_idle_request(row)
                    return
            if self.idle_ready:
                row = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
                if row:
                    self.start_idle_request(row)
                return
            next_row = next((r for r in self.queue('') if r['state']=='waiting'), None)
            remaining = (previous.get('duration_ms') or 0) - (previous.get('progress_ms') or 0)
            elapsed = time.time() - previous_updated if previous_updated else float('inf')
            boundary = (previous.get('playing') and previous.get('uri') != item.get('uri')
                        and previous.get('duration_ms') and 0 <= remaining <= 10000
                        and 0 <= elapsed <= 20 and elapsed + 1 >= remaining / 1000
                        and previous_device == self.device)
            if next_row and next_row['uri'].startswith('local:') and boundary:
                self.start_local_request(next_row, state)
                return
            if not state.get('is_playing') or not item.get('duration_ms') or item['duration_ms'] - (state.get('progress_ms') or 0) > 15000:
                return
            rows = self.queue('')
            row = next((r for r in rows if r['state'] == 'waiting'), None)
            if not row:
                return
            if row['uri'].startswith('local:'):
                # Never cut off the Spotify track's tail. Observe its end/boundary first.
                self.next_poll = time.monotonic() + 1
                return
            self.check_rules(row)
            self.commit_selection(row['id'])
            self.record_attempt(row,'queued')
            self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (row['id'],))
            self.db.commit()
            try:
                self.api.call('/me/player/queue', 'POST', params={'uri': row['uri'], 'device_id': self.device})
            except f.Failure as error:
                self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error), row['id']))
                self.db.commit()
                raise
            self.db.execute("UPDATE songs SET state='accepted' WHERE id=?", (row['id'],))
            self.db.commit()
        except f.Failure as error:
            self.error = str(error)
            self.reason = str(error)
            self.enabled = False
            self.next_poll = time.monotonic() + 60

    def health(self):
        status = 'Running' if self.enabled else ('Needs attention' if self.reason and self.reason not in (
            'Session stopped after startup. Verify device and enable handoff.', 'Handoff stopped by host.',
            'Handoff stopped after host reconciliation.', 'Handoff stopped after host playback control.') else 'Stopped')
        return {'status': status, 'reason': self.reason, 'idle': self.enabled and self.idle_ready, 'stale': not self.updated or time.time()-self.updated>45,
                'accepting': self.accepting, 'error': self.error,
                'player_active': bool(self.now),
                'recovery': ('Reconnect Spotify from the host desk.' if any(x in self.error.lower() for x in ('token','authoriz','401','login')) else
                             'Refresh devices and start the session on an available player.') if self.error or (not self.now and not (self.enabled and self.idle_ready)) else ''}

    def oauth_start(self, session):
        if not session['host']:
            raise PermissionError('Host access required.')
        parsed = urlsplit(self.public_url)
        if parsed.scheme != 'https' and parsed.hostname not in ('127.0.0.1', '::1'):
            raise f.Failure('Spotify hostname login requires HTTPS. Use the documented loopback CLI sign-in instead.')
        config = f.load(self.data / 'config.json')
        verifier, state = secrets.token_urlsafe(64), secrets.token_urlsafe(32)
        session['oauth'] = {'verifier': verifier, 'state': state, 'expires': time.time() + 180}
        challenge = base64.urlsafe_b64encode(hashlib.sha256(verifier.encode()).digest()).rstrip(b'=').decode()
        return 'https://accounts.spotify.com/authorize?' + urlencode({
            'client_id': config['client_id'], 'response_type': 'code', 'redirect_uri': self.public_url + '/callback',
            'scope': f.SCOPES, 'state': state, 'code_challenge_method': 'S256', 'code_challenge': challenge})

    def oauth_finish(self, session, query):
        flow = session.pop('oauth', None)
        if not session['host'] or not flow or flow['expires'] < time.time() or not secrets.compare_digest(flow['state'], query.get('state', [''])[0]):
            raise PermissionError('Expired or invalid Spotify login. Start again from the host desk.')
        if not query.get('code'):
            raise f.Failure('Spotify authorization was declined.')
        config = f.load(self.data / 'config.json')
        tokens = f.request_json('https://accounts.spotify.com/api/token', 'POST', urlencode({
            'client_id': config['client_id'], 'grant_type': 'authorization_code', 'code': query['code'][0],
            'redirect_uri': self.public_url + '/callback', 'code_verifier': flow['verifier']}).encode(),
            {'Content-Type': 'application/x-www-form-urlencoded'})
        if not tokens.get('access_token') or not tokens.get('refresh_token'):
            raise f.Failure('Spotify did not return the required credentials.')
        tokens['expires_at'] = time.time() + tokens.get('expires_in', 3600)
        f.private_write(self.data / 'tokens.json', tokens)
        self.next_poll = 0

    def start_session(self, device, accepting):
        self.enabled = False
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain handoff before starting a session.')
        devices = self.api.call('/me/player/devices').get('devices', [])
        if not any(d.get('id') == device and not d.get('is_restricted') for d in devices):
            raise f.Failure('Player unavailable. Refresh devices and check that Soloist is running.')
        if self.local_active:
            self.stop_local_owned()
        elif self.local_config.get('enabled') and self.local_config.get('device') == device:
            status = self.local.call('status')
            if status.get('state') in ('play', 'pause'):
                raise f.Failure('MPD is active outside this session. Stop it before starting Spotify.')
        seed_local = next((r for r in self.queue('') if r['state']=='waiting'), None)
        current = self.api.call('/me/player') if seed_local and seed_local['uri'].startswith('local:') else None
        if current is not None and not current.get('is_playing'):
            if self.local_config.get('device') != device:
                raise f.Failure('Choose the Spotify device linked to the local player.')
            self.device, self.accepting = device, accepting
            self.start_local_request(seed_local, current)
            f.private_write(self.data / 'settings.json', {'device':device,'accepting':accepting})
            self.enabled, self.error, self.reason, self.next_poll = True, '', '', 0
            return
        # A remote mutation may change playback even if its response is lost.
        # Stop dispatch until the selected target and playing state are observed.
        self.enabled = False
        self.reason = 'Starting selected player.'
        try:
            self.api.call('/me/player', 'PUT', {'device_ids': [device], 'play': True})
            observed = {}
            for attempt in range(4):
                observed = self.api.call('/me/player')
                if (observed.get('device') or {}).get('id') == device:
                    break
                time.sleep(0.5)
            if (observed.get('device') or {}).get('id') != device:
                raise f.Failure('Spotify accepted the transfer but the player has not become active. Handoff remains stopped; try Start again after checking player status.')
            seed = None
            if not observed.get('is_playing'):
                if observed.get('item'):
                    self.api.call('/me/player/play', 'PUT', {}, {'device_id': device})
                else:
                    seed = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
                    if not seed:
                        raise f.Failure('Player activated. Request a song in UnicornTunes, then press Start session to begin playback.')
                    if self.db.execute("SELECT id FROM songs WHERE state IN ('accepted','queued')").fetchone():
                        raise f.Failure('An earlier handoff remains unresolved. Reconcile it before starting a new track.')
                    if seed['uri'].startswith('local:'):
                        raise f.Failure('Spotify state changed during startup. Start again to begin the local request safely.')
                    self.check_rules(seed)
                    self.commit_selection(seed['id'])
                    self.record_attempt(seed,'direct',device)
                    self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (seed['id'],))
                    self.db.commit()
                    try:
                        self.api.call('/me/player/play', 'PUT', {'uris': [seed['uri']]}, {'device_id': device})
                    except f.Failure:
                        self.db.execute("UPDATE songs SET state='uncertain',detail='Start-track result uncertain; reconcile before retry' WHERE id=?", (seed['id'],))
                        self.db.commit()
                        raise
                    self.db.execute("UPDATE songs SET state='accepted' WHERE id=?", (seed['id'],))
                    self.db.commit()
            self.api.call('/me/player/shuffle', 'PUT', params={'state': 'false', 'device_id': device})
            self.api.call('/me/player/repeat', 'PUT', params={'state': 'off', 'device_id': device})
            for attempt in range(4):
                observed = self.api.call('/me/player')
                valid = ((observed.get('device') or {}).get('id') == device and observed.get('is_playing')
                         and not observed.get('shuffle_state') and observed.get('repeat_state') in (None, 'off'))
                if valid:
                    break
                time.sleep(0.5)
            if not valid:
                raise f.Failure('Player playback/settings could not be confirmed. Handoff remains stopped; check Soloist connectivity and Spotify permissions.')
            f.private_write(self.data / 'settings.json', {'device': device, 'accepting': accepting})
            self.device, self.accepting = device, accepting
            self.enabled, self.reason, self.error = True, '', ''
            self.idle_ready = False
            self.next_poll = 0
            self.now = dict(track(observed.get('item') or {}), playing=True, progress_ms=observed.get('progress_ms', 0))
            self.updated = time.time()
            if seed and (observed.get('item') or {}).get('uri') == seed['uri']:
                self.confirm_attempt(observed,dict(seed,state='accepted'))
            self.observe_play(observed)
        except (f.Failure, OSError) as error:
            self.reason = 'Session start needs attention: ' + (str(error) if isinstance(error, f.Failure) else 'Could not save configuration.')
            self.error = self.reason
            self.enabled = False
            self.next_poll = time.monotonic() + 30
            raise f.Failure(self.reason)

    def skip_request(self):
        next_local = next((r for r in self.queue('') if r['state']=='waiting'), None)
        if self.local_active or (next_local and next_local['uri'].startswith('local:')):
            return self.skip_mixed_request()
        if not self.device:
            raise f.Failure('Start a session on the selected player first.')
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain handoff before skipping.')
        resume = self.enabled
        chosen = None
        mutated = False
        self.enabled = False
        try:
            before = self.api.call('/me/player')
            before_uri = (before.get('item') or {}).get('uri')
            if (before.get('device') or {}).get('id') != self.device:
                raise f.Failure('Selected player is no longer active. Start the session again before skipping.')
            # A handed-off request is locked ahead of still-votable requests.
            pending = self.db.execute("SELECT * FROM songs WHERE state IN ('accepted','queued') ORDER BY id LIMIT 1").fetchone()
            if pending:
                chosen = dict(pending)
                # Already playing but not yet reconciled: do not replay it.
                if (before.get('item') or {}).get('uri') == chosen['uri'] and before.get('is_playing'):
                    self.db.execute("UPDATE songs SET state='playing' WHERE id=?", (chosen['id'],))
                    self.db.commit()
                    pending = chosen = None
            if not pending:
                chosen = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
            if chosen and not pending:
                self.check_rules(chosen)
                self.commit_selection(chosen['id'])
                self.record_attempt(chosen,'direct')
                self.db.execute("UPDATE songs SET state='sending' WHERE id=?", (chosen['id'],))
                self.db.commit()
                mutated = True
                self.api.call('/me/player/play', 'PUT', {'uris': [chosen['uri']]}, {'device_id': self.device})
            else:
                # Do not queue or directly play an already-handed-off URI twice.
                mutated = True
                self.api.call('/me/player/next', 'POST', params={'device_id': self.device})
            for attempt in range(4):
                observed = self.api.call('/me/player')
                item = observed.get('item') or {}
                target = chosen['uri'] if chosen else None
                changed = item.get('uri') == target if target else bool(item.get('uri') and item.get('uri') != before_uri)
                if (observed.get('device') or {}).get('id') == self.device and observed.get('is_playing') and changed:
                    break
                time.sleep(0.5)
            else:
                raise f.Failure('Next track could not be confirmed. Handoff stopped; reconcile before trying again.')
            self.db.execute("UPDATE songs SET state='past',detail='Host skipped; full playback not claimed' WHERE state='playing'")
            if chosen:
                self.confirm_attempt(observed,dict(chosen,state='accepted'))
            self.db.commit()
            self.now = dict(track(observed.get('item') or {}), playing=True, progress_ms=observed.get('progress_ms', 0))
            self.updated = time.time()
            self.observe_play(observed, skipped=True)
            self.enabled = resume
            self.reason = '' if resume else 'Handoff stopped by host.'
            self.error = ''
            self.next_poll = time.monotonic() + 5
        except f.Failure as error:
            if chosen and mutated:
                self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error), chosen['id']))
                self.db.commit()
            self.reason = 'Skip needs attention: ' + str(error)
            self.error = self.reason
            self.next_poll = time.monotonic() + 30
            raise f.Failure(self.reason)

    def action(self, sid, route, body):
        session = self.sessions[sid]
        if route == '/api/host/login':
            self.limit('login', 10, 300)
            if not secrets.compare_digest(str(body.get('password', '')), self.password):
                raise PermissionError('Host password is incorrect.')
            session['host'] = True
            return {'ok': True}
        if route.startswith('/api/host/') and not session['host']:
            raise PermissionError('Host access required.')
        handled = self.local_action(sid, route, body)
        if handled is not None:
            return handled
        handled = self.room_action(sid, route, body)
        if handled is not None:
            return handled
        if route == '/api/request':
            self.limit(('submit', sid), 6)
            self.submit(sid, body.get('uri'))
        elif route == '/api/vote':
            self.limit(('vote', sid), 30)
            self.vote(sid, int(body['id']))
        elif route == '/api/host/start':
            self.start_session(str(body.get('device', '')), bool(body.get('accepting', True)))
        elif route == '/api/host/stop':
            self.enabled = False
            self.idle_ready = False
            self.reason = 'Handoff stopped by host.'
            self.error = ''
        elif route == '/api/host/settings':
            accepting = bool(body.get('accepting', True))
            enabled = bool(body.get('enabled'))
            device = str(body.get('device', ''))
            if self.local_active and device != self.device:
                raise f.Failure('Stop local playback before changing the playback device.')
            devices = self.api.call('/me/player/devices').get('devices', [])
            if not any(d.get('id') == device and not d.get('is_restricted') for d in devices):
                raise f.Failure('Choose an available unrestricted playback device.')
            if enabled:
                state = self.api.call('/me/player')
                if (state.get('device') or {}).get('id') != device:
                    raise f.Failure('Activate that device in Spotify first, then enable handoff.')
                if state.get('shuffle_state') or (state.get('repeat_state') not in (None, 'off')):
                    raise f.Failure('Turn off Spotify shuffle/repeat before enabling handoff.')
            # Commit only after the whole proposal validates, including persistence.
            f.private_write(self.data / 'settings.json', {'device': device, 'accepting': accepting})
            self.device, self.accepting, self.enabled = device, accepting, enabled
            self.reason = '' if enabled else 'Handoff stopped by host.'
            self.error = ''
            self.next_poll = 0
        elif route == '/api/host/remove':
            row = self.db.execute('SELECT state FROM songs WHERE id=?', (int(body['id']),)).fetchone()
            if not row or row['state'] != 'waiting':
                raise f.Failure('Only waiting requests can be removed. Reconcile handed-off tracks in Spotify.')
            self.db.execute("UPDATE songs SET state='removed' WHERE id=?", (int(body['id']),))
            self.db.commit()
            self.reserve_next()
        elif route == '/api/host/reconcile':
            note = str(body.get('note', '')).strip()
            if len(note) < 10:
                raise f.Failure('Describe what you checked in Spotify before releasing this request.')
            row = self.db.execute('SELECT state FROM songs WHERE id=?', (int(body['id']),)).fetchone()
            if not row or row['state'] not in ('uncertain', 'accepted', 'queued'):
                raise f.Failure('Only an unresolved handoff can be reconciled.')
            if self.local_active and self.local_active['id'] == int(body['id']):
                self.stop_local_owned()
            self.enabled = False
            self.reason = 'Handoff stopped after host reconciliation.'
            self.db.execute("UPDATE songs SET state='past', detail=? WHERE id=?",
                            ('Host reconciled; no completion claim: ' + note[:300], int(body['id'])))
            self.db.commit()
        elif route == '/api/host/skip':
            self.skip_request()
        elif route == '/api/host/pause':
            self.idle_ready = False
            if not self.device:
                raise f.Failure('Select the playback device first.')
            self.enabled = False
            self.reason = 'Handoff stopped after host playback control.'
            if route.endswith('pause'):
                if self.local_active:
                    self.owned_local_status()
                    self.local.call('pause')
                else:
                    self.api.call('/me/player/pause', 'PUT', params={'device_id': self.device})
            else:
                self.api.call('/me/player/next', 'POST', params={'device_id': self.device})
        elif route == '/api/host/logout':
            session['host'] = False
        else:
            raise LookupError('Unknown action.')
        return {'ok': True}


def handler(app):
    class Handler(http.server.BaseHTTPRequestHandler):
        def log_message(self, *args):
            pass

        def respond(self, code, payload, content='application/json', cookie=None, cache='no-store'):
            raw = payload if isinstance(payload, bytes) else json.dumps(payload).encode()
            self.send_response(code)
            self.send_header('Content-Type', content)
            self.send_header('Content-Length', str(len(raw)))
            self.send_header('Cache-Control', cache)
            self.send_header('X-Content-Type-Options', 'nosniff')
            self.send_header('Referrer-Policy', 'no-referrer')
            self.send_header('Content-Security-Policy', "default-src 'self'; img-src 'self' https://i.scdn.co https://mosaic.scdn.co https://image-cdn-ak.spotifycdn.com data:; style-src 'self'; script-src 'self'; frame-ancestors 'none'; base-uri 'none'")
            if cookie:
                self.send_header('Set-Cookie', 'ft_session=' + cookie + '; HttpOnly; SameSite=Lax; Path=/; Max-Age=28800' + ('; Secure' if app.public_url.startswith('https://') else ''))
            if getattr(self, 'guest_token', None):
                self.send_header('Set-Cookie', 'ft_guest=' + self.guest_token + '; HttpOnly; SameSite=Lax; Path=/; Max-Age=31536000' + ('; Secure' if app.public_url.startswith('https://') else ''))
            self.end_headers()
            self.wfile.write(raw)

        def handle_route(self, post=False):
            try:
                parsed_art = urlsplit(self.path)
                if not post and parsed_art.path.startswith('/api/local-art/'):
                    with app.lock:
                        if self.headers.get('Host', '') not in self.server.allowed_hosts:
                            raise PermissionError('Unknown host.')
                        app.limit(('local-art', self.client_address[0]), 180)
                    mime, raw = app.local_artwork(parsed_art.path[len('/api/local-art/'):])
                    self.respond(200 if raw else 204, raw, mime or 'application/octet-stream', cache='private, max-age=300')
                    return
                with app.lock:
                    host = self.headers.get('Host', '')
                    # Explicit allowed authorities prevent DNS-rebinding requests to arbitrary hosts.
                    if host not in self.server.allowed_hosts:
                        raise PermissionError('Unknown host.')
                    cookies = SimpleCookie(self.headers.get('Cookie', ''))
                    old = cookies.get('ft_session')
                    sid, session = app.session(old.value if old else '')
                    guest_cookie = cookies.get('ft_guest')
                    self.guest_token = app.attach_guest(sid, guest_cookie.value if guest_cookie else '')
                    if post:
                        if self.headers.get('Origin') != (app.public_url or 'http://' + host) or self.headers.get('X-FoxTunes-CSRF') != session['csrf']:
                            raise PermissionError('Invalid request origin or session.')
                        if self.headers.get('Content-Type', '').split(';')[0] != 'application/json':
                            raise ValueError('JSON required.')
                        length = int(self.headers.get('Content-Length', 0))
                        if not 0 < length <= 32768:
                            raise ValueError('Invalid request size.')
                        app.limit(('ip', self.client_address[0]), 100)
                        body = json.loads(self.rfile.read(length))
                        if not isinstance(body, dict):
                            raise ValueError('JSON object required.')
                        payload = app.action(sid, urlsplit(self.path).path, body)
                        if '_guest_token' in payload:
                            self.guest_token = payload.pop('_guest_token')
                    else:
                        parsed = urlsplit(self.path)
                        query = parse_qs(parsed.query)
                        if parsed.path == '/healthz':
                            self.respond(200, {'ok': True})
                            return
                        if parsed.path == '/api/host/spotify-login':
                            url = app.oauth_start(session)
                            self.send_response(303)
                            self.send_header('Location', url)
                            self.send_header('Cache-Control', 'no-store')
                            self.send_header('Content-Length', '0')
                            self.end_headers()
                            return
                        if parsed.path == '/callback':
                            app.oauth_finish(session, query)
                            self.respond(200, b'<!doctype html><title>UnicornTunes connected</title><p>Spotify connected. <a href="/host">Return to host desk</a></p>', 'text/html')
                            return
                        if parsed.path in ('/', '/host', '/app.js', '/style.css', '/foxtunes-logo.png', '/vendor/qrcode.min.js', '/room.js', '/room.css', '/display', '/playlists.js'):
                            name = {'/playlists.js': 'playlists.js', '/room.js': 'room.js', '/room.css': 'room.css', '/display': 'index.html', '/': 'index.html', '/host': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/foxtunes-logo.png': 'foxtunes-logo.png', '/vendor/qrcode.min.js': 'vendor/qrcode.min.js'}[parsed.path]
                            content = {'playlists.js': 'text/javascript', 'room.js': 'text/javascript', 'room.css': 'text/css', 'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css', 'foxtunes-logo.png': 'image/png', 'vendor/qrcode.min.js': 'text/javascript'}[name]
                            self.respond(200, (Path(__file__).parent / 'web' / name).read_bytes(), content, sid)
                            return
                        if parsed.path == '/api/host/playlists':
                            app.limit(('playlists', sid), 30)
                            payload = app.list_playlists(sid, query)
                        elif parsed.path == '/api/playlists':
                            payload = {'items': [json.loads(r[0]) for r in app.db.execute('SELECT metadata FROM shared_playlists ORDER BY created,id')]}
                        elif parsed.path == '/api/playlist-items':
                            app.limit(('playlist-items', sid), 30)
                            payload = app.playlist_tracks(sid, query.get('id', [''])[0], int(query.get('offset', ['0'])[0]))
                        elif parsed.path == '/api/favorites':
                            payload = {'items': app.favorites()}
                        elif parsed.path == '/api/stats':
                            payload = app.stats(query.get('period', ['tonight'])[0], sid)
                        elif parsed.path == '/api/share':
                            payload = {'url': (app.public_url or 'http://' + host) + '/'}
                        elif parsed.path == '/api/state':
                            payload = {'csrf': session['csrf'], 'host': session['host'], 'now': app.now,
                                       'queue': app.queue(sid), 'accepting': app.accepting, 'enabled': app.enabled,
                                       'error': app.error, 'updated': app.updated, 'handoff': app.health(), **app.room_state(sid)}
                            if session['host']:
                                payload['device'] = app.device
                                payload['spotify_connected'] = (app.data / 'tokens.json').exists()
                        elif parsed.path == '/api/search':
                            app.limit(('search', sid), 15)
                            app.limit(('search-ip', self.client_address[0]), 60)
                            payload = app.mixed_search(query)
                        elif parsed.path == '/api/host/devices':
                            if not session['host']:
                                raise PermissionError('Host access required.')
                            payload = {'devices': app.api.call('/me/player/devices').get('devices', [])}
                        else:
                            raise LookupError('Not found.')
                    self.respond(200, payload, cookie=sid)
            except PermissionError as error:
                self.respond(403, {'error': str(error)})
            except LookupError as error:
                self.respond(404, {'error': str(error)})
            except (ValueError, TypeError, json.JSONDecodeError):
                self.respond(400, {'error': 'Invalid request.'})
            except f.Failure as error:
                self.respond(409, {'error': str(error)})
            except Exception:
                self.respond(500, {'error': 'Local operation failed. Check host setup.'})

        def do_GET(self):
            self.handle_route()

        def do_POST(self):
            self.handle_route(True)
    return Handler


def make_server(app, bind='127.0.0.1', port=8863, allowed=None):
    server = http.server.ThreadingHTTPServer((bind, port), handler(app))
    server.daemon_threads = True
    actual = server.server_address[1]
    server.allowed_hosts = set(allowed or ['127.0.0.1:' + str(actual), 'localhost:' + str(actual)])
    return server


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--bind', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8863)
    parser.add_argument('--lan-host', help='LAN IP used in browser URLs, required for LAN binding')
    args = parser.parse_args()
    if args.bind != '127.0.0.1' and not args.lan_host and not os.environ.get('FOXTUNES_PUBLIC_URL'):
        parser.error('LAN binding requires --lan-host with this computer\'s LAN IP.')
    os.umask(0o077)
    app = App()
    allowed = ['127.0.0.1:%s' % args.port, 'localhost:%s' % args.port]
    if args.lan_host:
        allowed.append('%s:%s' % (args.lan_host, args.port))
    if app.public_url:
        allowed.append(urlsplit(app.public_url).netloc)
    server = make_server(app, args.bind, args.port, allowed)
    stop = threading.Event()

    def worker():
        while not stop.wait(1):
            with app.lock:
                app.poll()
    thread = threading.Thread(target=worker, daemon=True)
    thread.start()
    print('UnicornTunes: http://127.0.0.1:%s' % args.port, flush=True)
    print('Host page: /host. Password is in private .local-work/runtime/web-host.json', flush=True)
    print('Automatic handoff starts OFF; enable it only for a deliberate host test.', flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        stop.set()
        server.server_close()
        thread.join(timeout=20)
        app.db.close()


if __name__ == '__main__':
    main()
