"""Bounded, allowlisted playback diagnostics. Never accepts request bodies or raw errors."""
from collections import deque
from contextlib import contextmanager
from datetime import datetime, timezone
import hashlib
import json
import re
import secrets
import threading
import time

import foxtunes as f


class Diagnostics:
    def __init__(self, sink=None, capacity=200):
        self.events = deque(maxlen=capacity)
        self.failures = deque(maxlen=5)
        self.salt = secrets.token_bytes(32)
        self.sink = sink or (lambda event: print(json.dumps(event, separators=(',', ':')), flush=True))
        self.local = threading.local()
        self.previous = None
        self.lock = threading.RLock()
        self.sequence = 0
        self.slow_reads = {}
        self.queue_signature = None

    def opaque(self, value):
        return hashlib.sha256(self.salt + str(value).encode()).hexdigest()[:12] if value else None

    @contextmanager
    def operation(self):
        previous = getattr(self.local, 'correlation', None)
        if previous is None:
            self.local.correlation = secrets.token_hex(6)
        try:
            yield
        finally:
            self.local.correlation = previous

    def emit(self, kind, **fields):
        # Callers supply only the fixed, sanitized schemas below. No raw request/error data.
        with self.lock:
            self.sequence += 1
            event = dict(at=datetime.now(timezone.utc).isoformat(), sequence=self.sequence,
                         event=kind, correlation=getattr(self.local, 'correlation', None), **fields)
            event = json.loads(json.dumps(event))
            self.events.append(event)
            try:
                self.sink(event)
            except Exception:
                pass  # Observability must never turn a successful mutation into a failure.

    def failure(self, kind, snapshot, category):
        with self.lock:
            self.failures.append(dict(at=datetime.now(timezone.utc).isoformat(), category=category,
                                      correlation=getattr(self.local, 'correlation', None), snapshot=snapshot))
        self.emit(kind, category=category, snapshot=snapshot)

    def observe(self, snapshot):
        with self.lock:
            # Position changes alone are not events. Track, device and state changes are.
            signature = dict(snapshot)
            signature['playback'] = {k:v for k,v in snapshot['playback'].items() if k!='progress_ms'}
            if signature == self.previous:
                return
            previous = self.previous
            self.previous = signature
        self.emit('state.changed', previous=previous, current=snapshot)

    def report(self):
        with self.lock:
            return json.loads(json.dumps(dict(events=list(self.events), failures=list(self.failures))))


def category(error):
    status = getattr(error, 'status', None)
    if isinstance(status, int):
        return 'http_' + str(status)
    return 'uncertain' if getattr(error, 'uncertain', False) else 'operation_failed' if isinstance(error, f.Failure) else 'internal_error'


def endpoint(path):
    if path in ('/me', '/search', '/me/playlists') or re.fullmatch(r'/me/player(?:/(?:play|pause|next|previous|shuffle|repeat|queue|devices|volume|seek|currently-playing))?', path):
        return path
    return '/other'  # No dynamic playlist/account IDs, queries or external URLs.


class ObservedSpotify:
    def __init__(self, api, app):
        self.api, self.app = api, app

    def __getattr__(self, name):
        return getattr(self.api, name)

    def call(self, path, method='GET', payload=None, params=None):
        diag = self.app.diagnostics
        with diag.operation():
            started = time.monotonic()
            before = self.app.diagnostic_snapshot()
            try:
                result = self.api.call(path, method, payload, params)
            except Exception as error:
                diag.emit('spotify.operation', method=method if method in ('GET','PUT','POST','DELETE') else 'OTHER',
                          endpoint=endpoint(path), outcome='failed', category=category(error),
                          status=getattr(error, 'status', None) if isinstance(getattr(error, 'status', None),int) else None,
                          duration_ms=round((time.monotonic()-started)*1000))
                diag.failure('spotify.failure', before, category(error))
                raise
            if path == '/me/player' and method == 'GET' and isinstance(result,dict):
                item, device, context = result.get('item') or {}, result.get('device') or {}, result.get('context') or {}
                restrictions = (result.get('actions') or {}).get('disallows') or {}
                allowed = ('pausing','resuming','seeking','skipping_next','skipping_prev','transferring_playback',
                           'toggling_repeat_context','toggling_repeat_track','toggling_shuffle','interrupting_playback')
                self.app.provider_snapshot = {'device':diag.opaque(device.get('id')), 'track':diag.opaque(item.get('uri')),
                    'context':diag.opaque(context.get('uri')), 'context_type':context.get('type') if context.get('type') in ('album','playlist','artist') else 'other' if context else None,
                    'playing':result.get('is_playing') is True, 'shuffle':result.get('shuffle_state') if isinstance(result.get('shuffle_state'),bool) else None,
                    'repeat':result.get('repeat_state') if result.get('repeat_state') in ('off','track','context') else None,
                    'disallows':[key for key in allowed if restrictions.get(key) is True]}
                self.app.provider_observed_at = time.time()
            if path == '/me/player/queue' and method == 'GET' and isinstance(result,dict):
                items = (result.get('queue') or [])[:20]
                refs = {diag.opaque(x.get('uri')) for x in items if isinstance(x,dict)}
                signature = (len(items)>1 and len(refs)==1,len(items),len(refs))
                if signature != diag.queue_signature and (signature[0] or diag.queue_signature and diag.queue_signature[0]):
                    diag.emit('spotify.queue_snapshot', repeated=signature[0], reported_count=signature[1], distinct_count=signature[2])
                diag.queue_signature = signature
            duration_ms = round((time.monotonic()-started)*1000)
            safe_endpoint = endpoint(path)
            if method == 'GET' and duration_ms >= 2000 and time.monotonic()-diag.slow_reads.get(safe_endpoint,-1000)>=60:
                diag.slow_reads[safe_endpoint]=time.monotonic()
                diag.emit('spotify.slow_read', endpoint=safe_endpoint, duration_ms=duration_ms)
            if method != 'GET':
                status = getattr(self.api, 'last_status', None)
                diag.emit('spotify.operation', method=method if method in ('PUT','POST','DELETE') else 'OTHER',
                          endpoint=endpoint(path), outcome='accepted', status=status if isinstance(status,int) else None,
                          duration_ms=round((time.monotonic()-started)*1000))
            return result


class DiagnosticFeatures:
    def diagnostic_snapshot(self):
        now = self.now or {}
        rows = self.db.execute("SELECT id,state,uri FROM songs WHERE state NOT IN ('past','removed') ORDER BY id LIMIT 100").fetchall()
        states = {'waiting','sending','accepted','queued','uncertain','playing'}
        return {'enabled':bool(self.enabled), 'accepting':bool(self.accepting), 'idle':bool(self.idle_ready),
                'selected_device':self.diagnostics.opaque(self.device),
                'observed_device':self.diagnostics.opaque(getattr(self,'observed_device',None)),
                'playback':{'track':self.diagnostics.opaque(now.get('uri')), 'playing':bool(now.get('playing')),
                            'duration_ms':max(0,min(86400000,now.get('duration_ms',0))) if isinstance(now.get('duration_ms',0),(int,float)) else 0, 'progress_ms':max(0,min(86400000,now.get('progress_ms',0))) if isinstance(now.get('progress_ms',0),(int,float)) else 0, 'source':'local' if now.get('source')=='local' else 'spotify'},
                'requests':[{'id':r['id'],'state':r['state'] if r['state'] in states else 'other',
                             'track':self.diagnostics.opaque(r['uri'])} for r in rows],
                'spotify':getattr(self,'provider_snapshot',{}), 'local_active':bool(self.local_active), 'background_armed':bool(self.background_armed),
                'background_pending':bool(self.background_pending)}

    def dispatch_reason(self):
        if not self.enabled:return 'session_stopped'
        if self.background_pending:return 'background_confirmation'
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('sending','accepted','queued','uncertain')").fetchone():return 'handoff_unresolved'
        if self.local_active:return 'local_request_active'
        if self.db.execute("SELECT 1 FROM songs WHERE state='playing'").fetchone():return 'requested_track_active'
        if self.db.execute("SELECT 1 FROM songs WHERE state='waiting'").fetchone():
            return 'player_paused_or_unavailable' if not (self.now or {}).get('playing') and not self.idle_ready else 'waiting_for_dispatch'
        return 'queue_empty'

    def diagnostic_report(self):
        return dict(schema=1, version=__import__('os').environ.get('UNICORNTUNES_VERSION','development'),
                    generated_at=datetime.now(timezone.utc).isoformat(),
                    note='Track/device IDs are per-process hashes. No account names, credentials, raw errors or provider bodies. State observations do not establish who issued an external pause. Recent memory resets on restart; Docker logs retain emitted events within configured limits.',
                    provider_observed_at=getattr(self,'provider_observed_at',None),
                    queue_snapshot={'repeated':self.diagnostics.queue_signature[0], 'reported_count':self.diagnostics.queue_signature[1], 'distinct_count':self.diagnostics.queue_signature[2]} if self.diagnostics.queue_signature else None,
                    snapshot=self.diagnostic_snapshot(), **self.diagnostics.report())

    def account_info(self):
        fingerprint = tuple((p.stat().st_mtime_ns, p.stat().st_size) if p.exists() else None
                            for p in (self.data/'tokens.json', self.data/'config.json'))
        if fingerprint != self.account_fingerprint:
            self.account_cache = {'status':'unverified', 'display_name':None, 'verified_at':None}
            self.account_due = 0
            self.account_fingerprint = fingerprint
        if not (self.data/'tokens.json').exists():
            self.account_cache = {'status':'not_connected', 'display_name':None, 'verified_at':None}
            return self.account_cache
        if time.monotonic() < self.account_due:
            return self.account_cache
        # A failed lookup is cached briefly; successful names last five minutes.
        self.account_due = time.monotonic()+60
        try:
            result = self.api.call('/me')
            if not isinstance(result,dict) or not result.get('id'):
                raise f.Failure('Account identity could not be verified.')
            name = result.get('display_name')
            self.account_cache = {'status':'verified', 'display_name':str(name).strip()[:128] if name else 'Connected account (no display name)',
                                  'verified_at':time.time()}
            self.account_due = time.monotonic()+300
            # Refreshing the access token may have changed file metadata during /me.
            self.account_fingerprint = tuple((p.stat().st_mtime_ns,p.stat().st_size) if p.exists() else None
                                             for p in (self.data/'tokens.json', self.data/'config.json'))
        except Exception:
            self.account_cache = {'status':'unavailable', 'display_name':None, 'verified_at':None}
        return self.account_cache
