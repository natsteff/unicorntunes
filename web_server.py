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
from background_playback import BackgroundPlayback
from diagnostics import Diagnostics, ObservedSpotify, DiagnosticFeatures, category


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


class App(RoomFeatures, LocalPlayback, BackgroundPlayback, DiagnosticFeatures):
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
        self.background_armed, self.background_message, self.quick_poll_until = False, "", 0
        self.background_pending = None
        self.startup_pending = None
        self.startup_settle_until = 0
        self.background_shuffle_command = None
        self.shuffle_disable_retry = None
        self.verified_background_context = None
        self.last_poll, self.next_poll = 0, 0
        hostfile = data / 'web-host.json'
        if not hostfile.exists():
            f.private_write(hostfile, {'password': secrets.token_urlsafe(24)})
        self.password = f.load(hostfile)['password']
        self.room_init()
        self.idle_ready = False
        self.local_init()
        self.diagnostics = Diagnostics()
        self.account_cache = {'status':'unverified','display_name':None,'verified_at':None}
        self.account_due, self.account_fingerprint = 0, None
        self.api = ObservedSpotify(self.api, self)
        self.last_dispatch_reason = None
        self.diagnostics.emit('server.started', snapshot=self.diagnostic_snapshot())

    def session(self, sid, create=True):
        now = time.time()
        def discard(key):
            del self.sessions[key]
            # Session rate limits must not outlive the sessions they protect.
            for bucket in list(self.limits):
                if isinstance(bucket, tuple) and key in bucket[1:]:
                    del self.limits[bucket]
        for key, value in list(self.sessions.items()):
            if value['expires'] <= now or (not value['host'] and
                    value.get('seen', now) <= now - 30 * 60):
                discard(key)
        if sid not in self.sessions:
            if not create:
                raise PermissionError('Browser session expired. Refresh the page, then try again. Host controls may require signing in again.')
            if len(self.sessions) >= 500:
                # Under pressure, reclaim inactive guests, never hosts or recently active guests.
                for key, value in list(self.sessions.items()):
                    if not value['host'] and value.get('seen', now) <= now - 5 * 60:
                        discard(key)
            if len(self.sessions) >= 500:
                raise f.Failure('Browser session capacity reached. Please try again after inactive sessions expire.')
            sid = secrets.token_urlsafe(24)
            self.sessions[sid] = {'csrf': secrets.token_urlsafe(24), 'host': False, 'expires': now + 8 * 3600}
        self.sessions[sid]['seen'] = now
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
        owned_previous = self.db.execute("SELECT 1 FROM songs s JOIN request_attempts a ON a.song=s.id WHERE s.uri=? AND s.state='playing' AND a.confirmed=1 AND a.device=?", (previous.get('uri'), self.device)).fetchone()
        # A standalone request list can expose its original seed again after
        # the last queued track ends. Accept only a fresh observed owned end.
        same_track = not item or item.get('uri') == previous.get('uri') or bool(owned_previous and progress == 0)
        if (self.enabled and previous.get('playing') and duration and 0 <= remaining <= 15000
                and getattr(self, 'observed_device', None) == self.device and self.device
                and 0 <= elapsed <= 30 and elapsed + 1 >= remaining / 1000
                and ended_position and same_track):
            self.idle_ready = True
            self.db.execute("UPDATE songs SET state='past',detail='Observed end; ready for next request' WHERE state='playing'")
            self.db.execute("UPDATE plays SET outcome='unknown' WHERE outcome='observing'")
            self.db.commit()

    def sync_background_shuffle(self, state, enabled):
        if (state.get('device') or {}).get('id') != self.device:
            return not enabled
        observed = state.get('shuffle_state')
        sent = self.background_shuffle_command
        enabling = not enabled and sent and sent[:2] == (self.device, True)
        retry = self.shuffle_disable_retry if not enabled else None
        if retry and retry['device'] != self.device:
            retry = self.shuffle_disable_retry = None
        if not enabling and (observed is enabled or (not enabled and observed is None and not (sent or retry))):
            if sent and sent[:2] == (self.device, enabled):
                self.diagnostics.emit('background.shuffle_confirmed', enabled=enabled)
            self.background_shuffle_command = None
            if not enabled:
                self.shuffle_disable_retry = None
            return True
        if retry:
            if time.monotonic() >= retry['deadline']:
                raise f.Failure('Spotify did not confirm shuffle was off. Request order is on hold.')
            if observed is not True or time.monotonic() < retry['next_attempt']:
                return False
        key = (self.device, enabled)
        if sent and sent[:2] == key:
            if not enabled and time.monotonic()-sent[2] >= 12:
                raise f.Failure('Spotify did not confirm shuffle was off. Request order is on hold.')
            return False
        try:
            self.api.call('/me/player/shuffle', 'PUT', params={'state':'true' if enabled else 'false','device_id':self.device})
        except f.Failure as error:
            if enabled or error.status not in (502, 503, 504) or retry:
                raise
            now = time.monotonic()
            self.background_shuffle_command = None
            self.shuffle_disable_retry = {'device':self.device, 'next_attempt':now+2, 'deadline':now+12}
            self.diagnostics.emit('background.shuffle_rechecking', status=error.status)
            self.request_playback_refresh()
            return False
        self.background_shuffle_command = (*key, time.monotonic())
        self.diagnostics.emit('background.shuffle_requested', enabled=enabled)
        self.request_playback_refresh()
        return False

    def start_idle_request(self, row):
        if row['uri'].startswith('local:'):
            return self.start_local_request(row, self.api.call('/me/player'))
        self.check_rules(row)
        self.arm_background()
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
        with self.diagnostics.operation():
            before = self.diagnostic_snapshot()
            old_failure = (self.error, self.reason)
            try:
                return self._poll_state(force)
            except Exception as error:
                self.diagnostics.failure('poll.failure', before, category(error))
                raise
            finally:
                if (self.error, self.reason) != old_failure and self.error:
                    self.diagnostics.failure('playback.failure', before, 'handoff_stopped')
                if not before['idle'] and self.idle_ready:
                    self.diagnostics.emit('playback.end_observed', snapshot=before)
                self.diagnostics.observe(self.diagnostic_snapshot())
                reason = self.dispatch_reason()
                if reason != self.last_dispatch_reason:
                    self.last_dispatch_reason = reason
                    self.diagnostics.emit('dispatch.state', reason=reason)

    def _poll_state(self, force=False):
        if not force and time.monotonic() < self.next_poll:
            return
        self.next_poll = time.monotonic() + (1 if time.monotonic() < self.quick_poll_until else 5 if self.enabled else 20)
        try:
            if self.local_active:
                self.poll_local()
                return
            previous = self.now or {}
            previous_updated = self.updated
            previous_device = getattr(self, 'observed_device', None)
            try:
                state = self.api.call('/me/player')
            except f.Failure as error:
                if ((self.startup_pending and time.monotonic() < self.startup_pending['deadline']) or
                        (self.enabled and time.monotonic() < self.startup_settle_until and error.status == 404)):
                    self.next_poll = time.monotonic() + 1
                    return
                raise
            if (self.enabled and time.monotonic() < self.startup_settle_until and
                    not (state.get('device') or {}).get('id') and not self.idle_ready):
                self.next_poll = time.monotonic() + 1
                return
            self.update_idle_state(state)
            self.updated = time.time()
            self.error = ''
            item = state.get('item') or {}
            actual = (state.get('device') or {}).get('id')
            self.observed_device = actual
            self.now = dict(track(item), playing=bool(state.get('is_playing')), progress_ms=state.get('progress_ms', 0)) if item else None
            if self.startup_pending and not self.confirm_background_start(state):
                return
            if not self.verify_background(state):
                return
            self.capture_background(state)
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
            row=next((r for r in self.queue('') if r['state']=='waiting'),None)
            owned = self.db.execute("SELECT 1 FROM songs WHERE state='playing' AND uri=?", (item.get('uri'),)).fetchone()
            if not (row or owned):
                self.shuffle_disable_retry = None
            if (row or owned) and actual == self.device:
                try:
                    if not self.sync_background_shuffle(state, False):
                        return
                except f.Failure as error:
                    if error.status != 404 or time.monotonic() >= self.startup_settle_until:
                        raise
                    self.next_poll = time.monotonic() + 2
                    return
            configured = self.effective_background(self.device).get('uri') or ''
            context = self.normalized_context(state)
            fallback = self.meta('house_background', {}).get('uri')
            if fallback and (context == fallback or (not context and self.verified_background_context == (self.device, fallback))):
                configured = fallback
            if row or owned or (context and context != configured):
                self.verified_background_context = None
            verified = context == configured or (not context and self.verified_background_context == (self.device, configured))
            if not row and not owned and state.get('is_playing') and configured.startswith('spotify:playlist:') and verified:
                wanted = self.meta('player_background_shuffle', {}).get(self.device, True)
                try:
                    self.sync_background_shuffle(state, wanted)
                except f.Failure as error:
                    self.background_shuffle_command = (self.device, wanted, time.monotonic())
                    self.background_message = 'Could not change background shuffle: ' + str(error)
            # Decide from this poll's verified playback, not a submission-time
            # empty-queue snapshot: stale playing rows may have just been retired.
            if row and item.get('uri') and state.get('is_playing'):
                protected=self.db.execute("SELECT 1 FROM songs WHERE state='playing' AND uri=?",(item.get('uri'),)).fetchone()
                if not protected:
                    self.start_idle_request(row)
                    return
            if self.idle_ready:
                row = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
                if row:
                    self.start_idle_request(row)
                else:
                    self.restore_background(state)
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
            self.arm_background()
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
            self.startup_pending = None
            self.enabled = False
            self.next_poll = time.monotonic() + 60

    def health(self):
        status = 'Starting' if self.startup_pending else 'Running' if self.enabled else ('Needs attention' if self.reason and self.reason not in (
            'Session stopped after startup. Verify device and enable handoff.', 'Handoff stopped by host.',
            'Handoff stopped after host reconciliation.', 'Handoff stopped after host playback control.') else 'Stopped')
        return {'status': status, 'reason': self.reason, 'idle': self.enabled and self.idle_ready, 'stale': not self.updated or time.time()-self.updated>45,
                'accepting': self.accepting, 'error': self.error,
                'player_active': bool(self.now),
                'background_message': self.background_message,
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
        self.artist_catalog_cache.clear()
        self.next_poll = 0

    def finish_session_start(self, device, accepting, observed, seed=None, idle=False):
        f.private_write(self.data / 'settings.json', {'device': device, 'accepting': accepting})
        self.device, self.accepting = device, accepting
        self.check_soloist_on_session_start()
        self.enabled, self.reason, self.error = True, '', ''
        self.idle_ready = idle
        self.startup_settle_until = time.monotonic() + 25
        self.upcoming_due = 0
        item = observed.get('item')
        self.now = dict(track(item), playing=True, progress_ms=observed.get('progress_ms', 0)) if item else None
        self.updated = time.time()
        self.observed_device = (observed.get('device') or {}).get('id')
        if seed and (item or {}).get('uri') == seed['uri']:
            self.confirm_attempt(observed,dict(seed,state='accepted'))
        self.observe_play(observed)
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('playing','accepted','queued')").fetchone():
            self.arm_background()
        self.request_playback_refresh()

    @staticmethod
    def spotify_track_playing(state):
        item = state.get('item') or {}
        duration = item.get('duration_ms') or 0
        return bool(state.get('is_playing') and item.get('uri') and
                    (not duration or (state.get('progress_ms') or 0) < duration))

    def confirm_background_start(self, state):
        pending = self.startup_pending
        device = pending['device']
        actual = (state.get('device') or {}).get('id')
        context = self.normalized_context(state)
        playing = actual == device and self.spotify_track_playing(state)
        now = time.monotonic()
        if actual == device:
            pending['seen_target'] = True
        elif actual and (pending.get('seen_target') or actual != pending.get('previous_device')):
            raise f.Failure('Spotify moved to another player during startup. Handoff stayed stopped.')
        if playing and not context:
            sample = ((state.get('item') or {}).get('uri'), state.get('progress_ms') or 0)
            earlier = pending.setdefault('sample', sample)
            fresh_item = sample[0] != pending.get('previous_track') or earlier[1] < pending.get('previous_progress', 0)
            progressed = fresh_item and sample[0] == earlier[0] and sample[1] > earlier[1]
        else:
            pending.pop('sample', None)
            progressed = False
        expected = pending['uri']
        seed = pending.get('seed')
        confirmed = playing and (not expected or context == expected or
                                 (not context and progressed and now >= pending['grace']))
        if confirmed:
            allow_shuffle = (bool(expected and expected.startswith('spotify:playlist:')) and
                             self.meta('player_background_shuffle', {}).get(device, True))
            needs_shuffle_off = state.get('shuffle_state') is True and not allow_shuffle
            if not needs_shuffle_off and state.get('repeat_state') in (None, 'off'):
                try:
                    self.finish_session_start(device, pending['accepting'], state, seed)
                except OSError as error:
                    raise f.Failure('Could not save the player configuration.') from error
                if expected:
                    self.verified_background_context = (device, expected)
                self.startup_pending = None
                self.diagnostics.emit('background.start_confirmed', context_reported=bool(context))
                return True
            settings = (('shuffle', needs_shuffle_off, 'false'),
                        ('repeat', state.get('repeat_state') not in (None, 'off'), 'off'))
            for name, needs_change, value in settings:
                if not needs_change or now < pending.get(name + '_retry_at', 0) or pending.get(name + '_sent'):
                    continue
                try:
                    self.api.call('/me/player/' + name, 'PUT', params={'state':value, 'device_id':device})
                except f.Failure as error:
                    if error.status not in (404, 502, 503, 504) or error.uncertain:
                        raise
                    pending[name + '_retry_at'] = now + 2
                else:
                    pending[name + '_sent'] = True
        # After a confirmed API read with no playback, give Connect time to
        # settle before replaying a saved context once on the same target.
        if expected and not state.get('is_playing') and actual is None:
            missing_since = pending.setdefault('missing_since', now)
            if not pending.get('recovery_checked') and 5 <= now - missing_since and now < pending['deadline']:
                pending['recovery_checked'] = True
                devices = self.api.call('/me/player/devices').get('devices', [])
                available = any(d.get('id') == device and not d.get('is_restricted') for d in devices)
                elsewhere = any(d.get('is_active') and d.get('id') != device for d in devices)
                if available and not elsewhere:
                    self.diagnostics.emit('background.start_recovering')
                    self.api.call('/me/player/play', 'PUT', {'context_uri':expected}, {'device_id':device})
                    pending.update(grace=now+4, deadline=now+18)
        else:
            pending.pop('missing_since', None)
        if now >= pending['deadline']:
            self.diagnostics.emit('background.start_unconfirmed', target_observed=actual==device,
                                  context_state='matched' if context==pending['uri'] else 'different' if context else 'missing',
                                  playing=bool(playing))
            if confirmed:
                raise f.Failure('Playback is active, but Spotify shuffle or repeat could not be turned off. Handoff stayed stopped.')
            raise f.Failure('Playback could not be confirmed on this player. Check Spotify playback before starting again.')
        self.next_poll = now + 1
        return False

    def start_session(self, device, accepting, *, local_status=None):
        if self.startup_pending:
            raise f.Failure('Wait for the current startup check or stop it before starting another session.')
        self.enabled = False
        self.shuffle_disable_retry = None
        self.background_armed = False
        self.background_pending = None
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain handoff before starting a session.')
        devices = self.api.call('/me/player/devices').get('devices', [])
        selected = next((item for item in devices if item.get('id') == device and not item.get('is_restricted')), None)
        if not selected:
            raise f.Failure('Player unavailable. Refresh devices and check that Soloist is running.')
        names = self.meta('player_names', {})
        if selected.get('name') and names.get(device) != selected['name']:
            self.set_meta('player_names', dict(names, **{device:selected['name']}))
        if self.local_active:
            self.stop_local_owned()
        elif self.local_config.get('enabled') and self.local_config.get('device') == device:
            status = local_status if local_status is not None else self.local.call('status')
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
            self.check_soloist_on_session_start()
            self.enabled, self.error, self.reason, self.next_poll = True, '', '', 0
            return
        locked = self.db.execute("SELECT 1 FROM songs WHERE state IN ('accepted','queued','playing')").fetchone()
        startup_context = None if seed_local or locked else self.effective_background(device).get('uri')
        start_waiting_spotify = bool(seed_local and not locked and not seed_local['uri'].startswith('local:'))
        current = self.api.call('/me/player')
        already_selected = (current.get('device') or {}).get('id') == device
        playing_on_target = already_selected and self.spotify_track_playing(current)
        keep_background = bool(playing_on_target and startup_context and self.normalized_context(current) == startup_context)
        if playing_on_target and not keep_background:
            startup_context = None
        # A remote mutation may change playback even if its response is lost.
        # Stop dispatch until the selected target and playing state are observed.
        self.enabled = False
        self.reason = 'Starting selected player.'
        try:
            if startup_context:
                if not keep_background:
                    startup_context = self.play_background({'uri': startup_context}, device)
                started = time.monotonic()
                self.startup_pending = {'uri':startup_context, 'device':device, 'accepting':accepting,
                                        'previous_device':(current.get('device') or {}).get('id'),
                                        'previous_track':(current.get('item') or {}).get('uri'),
                                        'previous_progress':current.get('progress_ms') or 0,
                                        'grace':started + 4, 'deadline':started + 18}
                self.error = ''
                try:
                    state = self.api.call('/me/player')
                except f.Failure:
                    self.request_playback_refresh()
                else:
                    if not self.confirm_background_start(state):
                        self.request_playback_refresh()
                return
            observed = current
            if not already_selected:
                self.api.call('/me/player', 'PUT', {'device_ids': [device], 'play': not start_waiting_spotify})
                for _ in range(4):
                    observed = self.api.call('/me/player')
                    if (observed.get('device') or {}).get('id') == device:
                        break
                    time.sleep(0.5)
            if (observed.get('device') or {}).get('id') != device:
                raise f.Failure('Spotify accepted the transfer but the player has not become active. Handoff remains stopped; try Start again after checking player status.')
            previous_device = self.device
            self.device = device
            self.capture_background(observed)
            self.device = previous_device
            seed = None
            if not observed.get('is_playing'):
                if observed.get('item') and not start_waiting_spotify:
                    self.api.call('/me/player/play', 'PUT', {}, {'device_id': device})
                else:
                    seed = next((r for r in self.queue('') if r['state'] == 'waiting'), None)
                    if self.db.execute("SELECT id FROM songs WHERE state IN ('accepted','queued')").fetchone():
                        raise f.Failure('An earlier handoff remains unresolved. Reconcile it before starting a new track.')
                    if not seed:
                        self.finish_session_start(device, accepting, observed, idle=True)
                        self.background_message = 'Choose background music or request a song. The player is ready.'
                        return
                    if seed['uri'].startswith('local:'):
                        raise f.Failure('Spotify state changed during startup. Start again to begin the local request safely.')
                    self.check_rules(seed)
                    self.commit_selection(seed['id'])
                    self.arm_background()
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
            started = time.monotonic()
            self.startup_pending = {'uri':None, 'device':device, 'accepting':accepting,
                                    'seed':seed, 'previous_device':device,
                                    'grace':started, 'deadline':started + 18}
            try:
                latest = self.api.call('/me/player')
            except f.Failure:
                self.request_playback_refresh()
            else:
                if not self.confirm_background_start(latest):
                    self.request_playback_refresh()
        except (f.Failure, OSError) as error:
            self.startup_pending = None
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
            owned = self.db.execute(
                "SELECT 1 FROM songs s JOIN request_attempts a ON a.song=s.id "
                "WHERE s.state='playing' AND s.uri=? AND a.device=? AND a.confirmed=1",
                (before_uri, self.device),
            ).fetchone()
            if not chosen and owned and resume:
                self.finish_last_request_skip(before)
                return
            if chosen and not pending:
                if before.get('shuffle_state') is True and not self.sync_background_shuffle(before, False):
                    for _ in range(4):
                        observed = self.api.call('/me/player')
                        if self.sync_background_shuffle(observed, False):
                            break
                        time.sleep(.5)
                    else:
                        raise f.Failure('Spotify has not confirmed shuffle is off. Wait and try Next track again.')
                self.check_rules(chosen)
                self.commit_selection(chosen['id'])
                self.capture_background(before)
                self.arm_background()
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
        observed = route in ('/api/host/start','/api/host/stop','/api/host/pause',
                            '/api/host/resume','/api/host/skip','/api/host/background',
                            '/api/host/reconcile','/api/request','/api/vote')
        with self.diagnostics.operation():
            before = self.diagnostic_snapshot()
            if observed:self.diagnostics.emit('action.started', action=route, snapshot=before)
            try:
                result = self._action(sid, route, body)
            except Exception as error:
                if observed or (route.startswith('/api/host/') and self.sessions.get(sid,{}).get('host')):
                    self.diagnostics.failure('action.failed', before, category(error))
                raise
            if observed:
                self.diagnostics.emit('action.completed', action=route, snapshot=self.diagnostic_snapshot())
            return result

    def _action(self, sid, route, body):
        session = self.sessions[sid]
        if route == '/api/host/login':
            self.limit('login', 10, 300)
            if not secrets.compare_digest(str(body.get('password', '')), self.password):
                raise PermissionError('Host password is incorrect.')
            session['host'] = True
            return {'ok': True}
        if route.startswith('/api/host/') and not session['host']:
            raise PermissionError('Host access required.')
        if route == '/api/host/spotify-volume':
            device, volume = body.get('device'), body.get('volume')
            if not device or type(volume) is not int or not 0 <= volume <= 100:
                raise f.Failure('Select a player and enter a whole-number volume from 0 to 100.')
            devices = self.api.call('/me/player/devices').get('devices', [])
            selected = next((item for item in devices if item.get('id') == device), None)
            if not selected or selected.get('is_restricted') or selected.get('supports_volume') is not True:
                raise f.Failure('Spotify volume control is unavailable for this player.')
            self.api.call('/me/player/volume', 'PUT', params={'device_id':device, 'volume_percent':volume})
            return {'accepted':True}
        if route == '/api/host/diagnostics':
            return self.diagnostic_report()
        if route == '/api/host/account':
            return self.account_info()
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
        elif route == '/api/host/accepting':
            if type(body.get('accepting')) is not bool:
                raise f.Failure('Choose whether to accept guest requests.')
            accepting = body['accepting']
            f.private_write(self.data / 'settings.json', {'device':self.device, 'accepting':accepting})
            self.accepting = accepting
            if self.startup_pending:
                self.startup_pending['accepting'] = accepting
        elif route == '/api/host/end-session':
            if body.get('confirm') is not True:
                raise f.Failure('Confirm before shutting down the player and archiving the queue.')
            return self.end_linked_session(body.get('device'))
        elif route == '/api/host/start':
            self.start_session(str(body.get('device', '')), bool(body.get('accepting', True)))
        elif route == '/api/host/stop':
            self.mark_manual_playback_stop()
            self.background_armed = False
            self.background_pending = None
            self.enabled = False
            self.idle_ready = False
            self.reason = 'Handoff stopped by host.'
            self.error = ''
            self.request_playback_refresh()
        elif route == '/api/host/settings':
            if self.startup_pending:
                raise f.Failure('Wait for startup confirmation or stop the session first.')
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
            if self.startup_pending:
                raise f.Failure('Wait for startup confirmation or stop the session first.')
            self.skip_request()
            self.request_playback_refresh()
        elif route in ('/api/host/pause', '/api/host/resume'):
            self.mark_manual_playback_stop()
            self.playback_control(resume=route.endswith('/resume'))
        elif route == '/api/host/background':
            from background_playback import context_uri
            device = str(body.get('device', ''))
            uri = context_uri(body.get('uri'))
            scope = body.get('scope', 'player')
            name = str(body.get('name') or '').strip()[:200]
            if scope == 'gathering':
                if 'gathering' in body and body['gathering'] != self.meta('gathering'):
                    raise f.Failure('The gathering changed. Recheck the active settings and try again.')
                self.set_meta('background_override', {'uri': uri, 'name': name, 'gathering': self.meta('gathering')} if uri else {})
            elif scope == 'house':
                self.set_meta('house_background', {'uri': uri, 'name': name} if uri else {})
            elif scope == 'player':
                if not device or (device != self.device and not any(
                    row.get('id') == device and not row.get('is_restricted')
                    for row in self.api.call('/me/player/devices').get('devices', [])
                )):
                    raise f.Failure('Choose an available player before saving its background music.')
                self.save_background_default(device, uri, True, name)
            else:
                raise ValueError('Invalid background scope.')
            self.background_message = ''
        elif route == '/api/host/logout':
            session['host'] = False
        else:
            raise LookupError('Unknown action.')
        return {'ok': True}


STATIC_FILES = {'/host.webmanifest': 'host.webmanifest', '/legacy': 'legacy.html', '/legacy/': 'legacy.html', '/legacy.js': 'legacy.js', '/legacy.css': 'legacy.css', '/site.webmanifest': 'site.webmanifest', '/favicon.png': 'favicon.png', '/playlists.js': 'playlists.js', '/room.js': 'room.js', '/room.css': 'room.css', '/display': 'index.html', '/': 'index.html', '/host': 'index.html', '/app.js': 'app.js', '/style.css': 'style.css', '/foxtunes-logo.png': 'foxtunes-logo.png', '/vendor/qrcode.min.js': 'vendor/qrcode.min.js', '/kiosk.js': 'kiosk.js', '/host.js': 'host.js', '/host.css': 'host.css'}
STATIC_TYPES = {'host.webmanifest': 'application/manifest+json', 'legacy.html': 'text/html; charset=utf-8', 'legacy.js': 'text/javascript', 'legacy.css': 'text/css', 'site.webmanifest': 'application/manifest+json', 'favicon.png': 'image/png', 'playlists.js': 'text/javascript', 'room.js': 'text/javascript', 'room.css': 'text/css', 'index.html': 'text/html; charset=utf-8', 'app.js': 'text/javascript', 'style.css': 'text/css', 'foxtunes-logo.png': 'image/png', 'vendor/qrcode.min.js': 'text/javascript', 'kiosk.js': 'text/javascript', 'host.js': 'text/javascript', 'host.css': 'text/css'}


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
                    parsed = urlsplit(self.path)
                    if not post:
                        if parsed.path == '/healthz':
                            self.respond(200, {'ok': True})
                            return
                        if parsed.path in STATIC_FILES:
                            name = STATIC_FILES[parsed.path]
                            raw = (Path(__file__).parent / 'web' / name).read_bytes()
                            if parsed.path == '/host':
                                raw = raw.replace(b'content="UnicornTunes"', b'content="Host Desk"', 2)
                                raw = raw.replace(b'<title>UnicornTunes</title>', b'<title>Host Desk</title>', 1)
                                raw = raw.replace(b'href="/site.webmanifest"', b'href="/host.webmanifest"', 1)
                            self.respond(200, raw, STATIC_TYPES[name])
                            return
                        if parsed.path == '/api/share':
                            self.respond(200, {'url': (app.public_url or 'http://' + host) + '/'})
                            return
                        if parsed.path == '/api/favorites':
                            self.respond(200, {'items': app.favorites()})
                            return
                        if parsed.path == '/api/playlists':
                            self.respond(200, {'items': [json.loads(r[0]) for r in app.db.execute('SELECT metadata FROM shared_playlists ORDER BY created,id')]})
                            return
                        if parsed.path not in ('/api/state', '/api/search', '/api/stats', '/api/playlist-items',
                                '/api/host/playlists', '/api/host/devices', '/api/host/spotify-login', '/callback'):
                            raise LookupError('Not found.')
                    elif self.headers.get('Origin') != (app.public_url or 'http://' + host):
                        raise PermissionError('Open Host Desk at the configured address: ' + (app.public_url or 'http://' + host) + '/host')
                    cookies = SimpleCookie(self.headers.get('Cookie', ''))
                    old = cookies.get('ft_session')
                    sid, session = app.session(old.value if old else '', create=not post)
                    guest_cookie = cookies.get('ft_guest')
                    self.guest_token = app.attach_guest(sid, guest_cookie.value if guest_cookie else '')
                    if post:
                        if self.headers.get('X-FoxTunes-CSRF') != session['csrf']:
                            raise PermissionError('Your session changed. Reload Host Desk and try again.')
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
                        if parsed.path == '/api/host/playlists':
                            app.limit(('playlists', sid), 30)
                            payload = app.list_playlists(sid, query)
                        elif parsed.path == '/api/playlist-items':
                            app.limit(('playlist-items', sid), 30)
                            payload = app.playlist_tracks(sid, query.get('id', [''])[0], int(query.get('offset', ['0'])[0]))
                        elif parsed.path == '/api/stats':
                            payload = app.stats(query.get('period', ['tonight'])[0], sid)
                        elif parsed.path == '/api/state':
                            payload = {'csrf': session['csrf'], 'host': session['host'], 'now': app.now,
                                       'queue': app.queue(sid), 'accepting': app.accepting, 'enabled': app.enabled,
                                       'error': app.error, 'updated': app.updated, 'handoff': app.health(), **app.room_state(sid)}
                            if not session['host']:
                                payload['error'] = ''
                                for field in ('reason','error','recovery','background_message'):
                                    payload['handoff'][field] = ''
                            if session['host']:
                                payload['device'] = app.device
                                payload['spotify_connected'] = (app.data / 'tokens.json').exists()
                        elif parsed.path == '/api/search':
                            app.limit(('search', sid), 15)
                            app.limit(('search-ip', self.client_address[0]), 60)
                            payload = app.mixed_search(query, host=session['host'])
                        elif parsed.path == '/api/host/devices':
                            if not session['host']:
                                raise PermissionError('Host access required.')
                            devices = app.api.call('/me/player/devices').get('devices', [])
                            names = app.meta('player_names', {})
                            updated_names = dict(names, **{item['id']: item['name'] for item in devices
                                              if item.get('id') and item.get('name') and not item.get('is_restricted')})
                            if updated_names != names:
                                app.set_meta('player_names', updated_names)
                            payload = {'devices': devices}
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
        maintenance_check = 0
        while not stop.wait(1):
            with app.lock:
                app.poll()
                app.check_linked_player_boot()
                app.collect_soloist_session_status()
                now = time.monotonic()
                if now >= maintenance_check:
                    maintenance_check = now + 300
                    app.scheduled_soloist_update()
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
