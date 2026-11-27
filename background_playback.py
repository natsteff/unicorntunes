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
    def background_state(self):
        saved = self.meta('background_context', {})
        return dict(saved, armed=bool(self.background_armed), message=self.background_message)

    def capture_background(self, state):
        """Only remember a real context on our selected player, never queue guesses."""
        if not state.get('is_playing') or (state.get('device') or {}).get('id') != self.device:
            return
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('sending','accepted','queued','uncertain','playing')").fetchone():
            return
        uri = (state.get('context') or {}).get('uri', '')
        if not CONTEXT.fullmatch(uri):
            return
        saved = self.meta('background_context', {})
        # An explicit host choice remains authoritative for this destination.
        if saved.get('device') == self.device and saved.get('explicit'):
            return
        self.set_meta('background_context', {'uri': uri, 'device': self.device, 'explicit': False})
        self.background_message = ''

    def arm_background(self):
        self.background_armed = True
        self.background_message = ''

    def restore_background(self, state):
        if not self.enabled or not self.idle_ready or not self.background_armed or self.local_active:
            return
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('waiting','sending','accepted','queued','uncertain','playing')").fetchone():
            return
        actual = (state.get('device') or {}).get('id')
        if state.get('is_playing') or (actual and actual != self.device):
            return
        saved = self.meta('background_context', {})
        self.background_armed = False  # One attempt, including timeout/403; never retry blindly.
        if saved.get('device') != self.device or not CONTEXT.fullmatch(saved.get('uri', '')):
            self.background_message = 'Choose background music in Playback to continue after requests. Player remains ready for requests.'
            return
        if self.local_bound() and self.local.call('status').get('state') in ('play', 'pause'):
            raise f.Failure('MPD is still active; background music was not started.')
        self.background_message = 'Background music requested; checking playback.'
        self.api.call('/me/player/play', 'PUT', {'context_uri': saved['uri']}, {'device_id': self.device})
        self.background_pending = {'uri': saved['uri'], 'device': self.device, 'deadline': time.monotonic()+8}
        self.idle_ready = False
        self.next_poll = 0
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
        if (actual == pending['device'] and state.get('is_playing')
                and (state.get('context') or {}).get('uri') == pending['uri']):
            self.background_pending = None
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
