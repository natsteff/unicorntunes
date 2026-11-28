"""Bounded background-context restoration and explicit playback controls."""
import re
import time
from urllib.parse import urlsplit

import foxtunes as f

CONTEXT = re.compile(r'^spotify:(playlist|album|artist):[A-Za-z0-9]{22}$')


def context_uri(value):
    value = str(value or '').strip()
    if value.startswith('https://open.spotify.com/'):
        parts = urlsplit(value)
        fields = parts.path.strip('/').split('/')
        if len(fields) == 2:
            value = 'spotify:' + ':'.join(fields)
    if value and not CONTEXT.fullmatch(value):
        raise f.Failure('Use a Spotify playlist, album or artist link/URI.')
    return value


class BackgroundPlayback:
    def background_default(self, device):
        defaults = self.meta('background_defaults', {})
        if device in defaults:
            return defaults[device]
        legacy = self.meta('background_context', {})
        return legacy if legacy.get('device') == device else {}

    def effective_background(self, device):
        override = self.meta('background_override', {})
        if override.get('gathering') == self.meta('gathering') and override.get('uri'):
            return dict(override, device=device, source='gathering')
        saved = self.background_default(device)
        if saved.get('uri'):
            return dict(saved, source='player')
        return dict(self.meta('house_background', {}), device=device, source='house')

    @staticmethod
    def normalized_context(state):
        context = (state.get('context') or {}).get('uri') or ''
        if context.startswith('spotify:user:') and ':playlist:' in context:
            return 'spotify:playlist:' + context.rsplit(':playlist:', 1)[-1]
        return context

    def play_background(self, selected, device):
        uri = selected['uri']
        try:
            self.api.call('/me/player/play', 'PUT', {'context_uri': uri}, {'device_id': device})
        except f.Failure as error:
            fallback = self.meta('house_background', {}).get('uri')
            if error.status != 404 or error.uncertain or not fallback or fallback == uri:
                raise
            # A 404 can also mean the device disappeared. Verify it is still idle and selected.
            state = self.api.call('/me/player')
            if state.get('is_playing') or (state.get('device') or {}).get('id') != device:
                raise
            self.api.call('/me/player/play', 'PUT', {'context_uri': fallback}, {'device_id': device})
            self.background_message = 'Preferred background was unavailable. Using the house background playlist.'
            return fallback
        return uri

    def background_state(self):
        defaults = dict(self.meta('background_defaults', {}))
        legacy = self.meta('background_context', {})
        if legacy.get('device'):
            defaults.setdefault(legacy['device'], legacy)
        effective = self.effective_background(self.device)
        return dict(effective, defaults=defaults, override=self.meta('background_override', {}),
                    house=self.meta('house_background', {}), armed=bool(self.background_armed), message=self.background_message)

    def save_background_default(self, device, uri, explicit, name=''):
        defaults = self.meta('background_defaults', {})
        legacy = self.meta('background_context', {})
        if legacy.get('device'):
            defaults.setdefault(legacy['device'], legacy)
        defaults[device] = {'uri': uri, 'device': device, 'explicit': explicit}
        if name:
            defaults[device]['name'] = name
        self.set_meta('background_defaults', defaults)
        self.set_meta('background_context', defaults[device])

    def capture_background(self, state):
        """Only remember a real context on our selected player, never queue guesses."""
        if not state.get('is_playing') or (state.get('device') or {}).get('id') != self.device:
            return
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('sending','accepted','queued','uncertain','playing')").fetchone():
            return
        uri = (state.get('context') or {}).get('uri', '')
        if not CONTEXT.fullmatch(uri):
            return
        saved = self.background_default(self.device)
        # An explicit host choice remains authoritative for this destination.
        effective = self.effective_background(self.device)
        if saved.get('explicit') or (effective.get('uri') and effective['source'] in ('gathering', 'house')):
            return
        self.save_background_default(self.device, uri, False)
        self.background_message = ''

    def arm_background(self):
        self.background_armed = True
        self.background_message = ''

    def restore_background(self, state, *, skipping=False):
        if not self.enabled or not self.idle_ready or not self.background_armed or self.local_active:
            return
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('waiting','sending','accepted','queued','uncertain','playing')").fetchone():
            return
        actual = (state.get('device') or {}).get('id')
        if (state.get('is_playing') and not skipping) or (actual and actual != self.device):
            return
        saved = self.effective_background(self.device)
        self.background_armed = False  # One attempt, including timeout/403; never retry blindly.
        if saved.get('device') != self.device or not CONTEXT.fullmatch(saved.get('uri', '')):
            self.background_message = 'Choose background music in Player settings to continue after requests. Player remains ready for requests.'
            return
        if self.local_bound() and self.local.call('status').get('state') in ('play', 'pause'):
            raise f.Failure('MPD is still active; background music was not started.')
        self.background_message = 'Background music requested; checking playback.'
        started_uri = self.play_background(saved, self.device)
        self.background_pending = {'uri': started_uri, 'device': self.device, 'deadline': time.monotonic()+18, 'grace':time.monotonic()+4,
                                   'previous_track':(state.get('item') or {}).get('uri'),
                                   'previous_progress':state.get('progress_ms') or 0,
                                   'fallback': started_uri != saved['uri']}
        self.idle_ready = False
        self.next_poll = 0
        self.request_playback_refresh()

    def finish_last_request_skip(self, state):
        """End an owned request, then use the normal verified background path."""
        saved = self.effective_background(self.device)
        if state.get('is_playing') and not saved.get('uri'):
            self.api.call('/me/player/pause', 'PUT', params={'device_id':self.device})
            for _ in range(4):
                observed = self.api.call('/me/player')
                if (observed.get('device') or {}).get('id') not in (None, self.device):
                    raise f.Failure('Spotify moved to another player while ending the request.')
                if not observed.get('is_playing'):
                    state = observed
                    break
                time.sleep(0.5)
            else:
                raise f.Failure('Could not confirm that the last request stopped.')
        self.db.execute("UPDATE songs SET state='past',detail='Host skipped final request' WHERE state='playing'")
        self.db.execute("UPDATE plays SET outcome='skipped' WHERE outcome='observing'")
        self.db.commit()
        self.enabled, self.idle_ready, self.now = True, True, None
        self.reason = self.error = ''
        self.arm_background()
        try:
            self.restore_background(state, skipping=True)
        except f.Failure:
            self.enabled = False
            raise
        self.request_playback_refresh()

    def verify_background(self, state):
        pending = self.background_pending
        if not pending:
            return True
        if not self.enabled:
            self.background_pending = None
            return True
        actual = (state.get('device') or {}).get('id')
        if actual and actual != pending['device']:
            self.background_pending = None
            raise f.Failure('Player changed while restoring background music. Handoff stopped.')
        context = self.normalized_context(state)
        item = (state.get('item') or {}).get('uri')
        playing = actual == pending['device'] and state.get('is_playing') and item
        progressed = False
        if playing and not context:
            sample = (item, state.get('progress_ms') or 0)
            first = pending.setdefault('sample', sample)
            fresh = first[0] != pending.get('previous_track') or first[1] < pending.get('previous_progress', 0)
            progressed = fresh and sample[0] == first[0] and sample[1] > first[1]
        else:
            pending.pop('sample', None)
        if playing and (context == pending['uri'] or (not context and progressed and time.monotonic() >= pending.get('grace', pending['deadline']))):
            self.background_pending = None
            self.verified_background_context = (pending['device'], pending['uri'])
            self.upcoming_due = 0
            self.diagnostics.emit('background.resume_confirmed', context_reported=bool(context))
            if not pending.get('fallback'):
                self.background_message = ''
            return True
        if time.monotonic() >= pending['deadline']:
            self.background_pending = None
            self.background_message = 'Background playback was not confirmed. Check the player and saved context; no automatic retry.'
            raise f.Failure(self.background_message)
        return False

    def request_playback_refresh(self):
        # The worker owns Spotify polling; browsers cannot force API reads.
        self.quick_poll_until = time.monotonic() + 8
        self.next_poll = 0

    def playback_control(self, resume=False):
        if not self.device:
            raise f.Failure('Select the playback device first.')
        self.enabled = False
        self.idle_ready = False
        self.background_armed = False
        self.background_pending = None
        self.reason = 'Handoff stopped after host playback control.'
        if self.local_active:
            status = self.owned_local_status()
            if resume:
                if status.get('state') != 'pause':
                    raise f.Failure('Local playback is not paused; check the player before resuming.')
                if status.get('agent_version', 0) < 7:
                    raise f.Failure('Update the restricted local agent to v0.8.0 (agent 7) to resume local playback.')
                spotify = self.api.call('/me/player')
                if spotify.get('is_playing') or ((spotify.get('device') or {}).get('id') not in (None, self.device)):
                    raise f.Failure('Spotify is active elsewhere; local playback was not resumed.')
            self.local.call('resume' if resume else 'pause')
        else:
            state = self.api.call('/me/player')
            if (state.get('device') or {}).get('id') != self.device:
                raise f.Failure('Selected player is not active; refresh devices before controlling playback.')
            if resume and (not state.get('item') or state.get('is_playing')):
                raise f.Failure('Selected player has no paused track to resume.')
            self.api.call('/me/player/play' if resume else '/me/player/pause', 'PUT',
                          {} if resume else None, {'device_id': self.device})
        self.error = ''
        self.request_playback_refresh()
