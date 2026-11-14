"""Persistent guest, queue, curation and observed-play features for UnicornTunes."""
import copy
import hashlib
import json
import secrets
import sqlite3
import os
import time
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import foxtunes as f
from request_history import RequestHistory

SHY = '<Shy guy - name not provided>'
COPY = {
    'tagline': "THE ROOM'S SOUNDTRACK", 'eyebrow': 'GOOD COMPANY. GREAT MUSIC.',
    'headline': 'What should we play next?',
    'description': 'Find your favorite. Make a request. Give a good song a little lift.',
    'footer': 'One room. Everyone gets a say.'}
PRESETS = {
    'tagline': ["THE ROOM'S SOUNDTRACK", 'YOUR PEOPLE. YOUR SOUNDTRACK.', 'POWERED BY GOOD TASTE. MOSTLY.', 'THE SOUND OF HANGING OUT.'],
    'eyebrow': ['GOOD COMPANY. GREAT MUSIC.', 'GOOD FRIENDS. LOUD OPINIONS.', 'BRING YOUR PEOPLE. PICK YOUR MUSIC.', 'EVERY GATHERING NEEDS A SOUNDTRACK.'],
    'headline': ['What should we play next?', 'What’s your pick?', 'Your turn. What’s next?', 'Got a song in mind?'],
    'description': [COPY['description'], 'Request a song. Vote for your favorites. Shape the soundtrack.', 'Pick a track. Back a favorite. Keep the music going.', 'Find your song. Get it in the queue.'],
    'footer': [COPY['footer'], 'A little your taste. A little everyone else’s.', 'Everyone brings something to the mix.', 'The best nights have a little of everyone in them.']}
DEFAULTS = {'copy': COPY, 'hide_eyebrow': False, 'fair_queue': True, 'theme': '', 'theme_searches': [],
            'block_explicit': False, 'max_minutes': 0, 'blocked_tracks': [], 'blocked_artists': [],
            'repeat_minutes': 0, 'locations': {}, 'timezone': 'America/Chicago'}


class RoomFeatures(RequestHistory):
    def room_init(self):
        self.db.executescript('''
          CREATE TABLE IF NOT EXISTS guests(id TEXT PRIMARY KEY,name TEXT NOT NULL,named INTEGER DEFAULT 0);
          CREATE TABLE IF NOT EXISTS room_meta(key TEXT PRIMARY KEY,value TEXT NOT NULL);
          CREATE TABLE IF NOT EXISTS shared_playlists(id TEXT PRIMARY KEY,metadata TEXT NOT NULL,created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS favorites(uri TEXT PRIMARY KEY,metadata TEXT NOT NULL,created REAL NOT NULL);
          CREATE TABLE IF NOT EXISTS plays(id INTEGER PRIMARY KEY,song INTEGER,uri TEXT NOT NULL,metadata TEXT NOT NULL,
            requester TEXT,guest TEXT,gathering INTEGER,started REAL NOT NULL,listened REAL DEFAULT 0,
            last_progress INTEGER DEFAULT 0,last_seen REAL NOT NULL,qualified INTEGER DEFAULT 0,outcome TEXT DEFAULT 'observing');
          CREATE INDEX IF NOT EXISTS plays_started ON plays(started);
          CREATE INDEX IF NOT EXISTS plays_uri ON plays(uri,qualified);
          CREATE INDEX IF NOT EXISTS songs_guest ON songs(guest,state);
          CREATE TABLE IF NOT EXISTS reactions(play INTEGER,guest TEXT,reaction TEXT,PRIMARY KEY(play,guest));
        ''')
        columns = {r['name'] for r in self.db.execute('PRAGMA table_info(songs)')}
        migrating = 'requested_by' not in columns
        if migrating and self.db.execute('SELECT 1 FROM songs LIMIT 1').fetchone():
            backup_path = self.data / 'web-pre-0.2.0.sqlite3'
            if not backup_path.exists():
                backup = sqlite3.connect(str(backup_path))
                try:
                    self.db.backup(backup)
                finally:
                    backup.close()
                os.chmod(backup_path, 0o600)
        for name, definition in [('requested_by', "TEXT DEFAULT ''"), ('gathering', 'INTEGER DEFAULT 1'), ('guaranteed', 'INTEGER DEFAULT 0')]:
            if name not in columns:
                self.db.execute('ALTER TABLE songs ADD COLUMN ' + name + ' ' + definition)
        if migrating:
            self.db.execute("UPDATE songs SET guaranteed=1 WHERE id IN (SELECT min(id) FROM songs WHERE state='waiting' GROUP BY guest)")
        if 'voter_name' not in {r['name'] for r in self.db.execute('PRAGMA table_info(votes)')}:
            self.db.execute("ALTER TABLE votes ADD COLUMN voter_name TEXT DEFAULT ''")
        if 'created' not in {r['name'] for r in self.db.execute('PRAGMA table_info(votes)')}:
            self.db.execute('ALTER TABLE votes ADD COLUMN created REAL DEFAULT 0')
        self.db.execute("INSERT OR IGNORE INTO votes(song,guest,voter_name) SELECT id,guest,requested_by FROM songs")
        self.db.commit()
        self.history_init()
        path = self.data / 'room.json'
        saved = f.load(path) if path.exists() else {}
        self.room = copy.deepcopy(DEFAULTS)
        self.room.update({k: v for k, v in saved.items() if k in DEFAULTS})
        self.room['copy'] = dict(COPY, **saved.get('copy', {}))
        if not self.meta('gathering'):
            self.set_meta('gathering', 1)
            self.set_meta('gathering_started', time.time())
        self.spotify_upcoming = {'items': [], 'updated': 0, 'error': ''}
        self.upcoming_due = 0
        self.playlist_cache = {}
        # Across restarts, never infer elapsed listening from wall-clock downtime.
        self.db.execute("UPDATE plays SET outcome='unknown' WHERE outcome='observing'")
        self.db.commit()

    def meta(self, key, default=None):
        row = self.db.execute('SELECT value FROM room_meta WHERE key=?', (key,)).fetchone()
        return json.loads(row[0]) if row else default

    def set_meta(self, key, value):
        self.db.execute('INSERT OR REPLACE INTO room_meta VALUES(?,?)', (key, json.dumps(value)))
        self.db.commit()

    def guest_id(self, sid):
        return self.sessions.get(sid, {}).get('guest', sid)

    def guest_name(self, guest):
        row = self.db.execute('SELECT name FROM guests WHERE id=?', (guest,)).fetchone()
        return row[0] if row else SHY

    def attach_guest(self, sid, token):
        guest = hashlib.sha256(token.encode()).hexdigest() if token else ''
        if not guest or not self.db.execute('SELECT id FROM guests WHERE id=?', (guest,)).fetchone():
            token = secrets.token_urlsafe(32)
            guest = hashlib.sha256(token.encode()).hexdigest()
            self.db.execute('INSERT INTO guests VALUES(?,?,0)', (guest, SHY))
            self.db.commit()
        self.sessions[sid]['guest'] = guest
        return token

    def queue_rows(self, sid):
        guest = self.guest_id(sid)
        rows = self.db.execute('''SELECT s.*,count(v.guest) AS votes,
          max(CASE WHEN v.guest=? THEN 1 ELSE 0 END) AS voted FROM songs s
          LEFT JOIN votes v ON v.song=s.id WHERE s.state NOT IN ('removed','past','playing')
          GROUP BY s.id ORDER BY s.created,s.id''', (guest,)).fetchall()
        result = []
        for r in rows:
            names = [v[0] or SHY for v in self.db.execute('SELECT voter_name FROM votes WHERE song=? AND guest!=? ORDER BY rowid', (r['id'], r['guest']))]
            result.append(dict(json.loads(r['metadata']), id=r['id'], state=r['state'], votes=r['votes'],
                               voted=bool(r['voted']), mine=r['guest'] == guest, requested_by=r['requested_by'] or SHY,
                               upvoted_by=names, guaranteed=bool(r['guaranteed']), created=r['created']))
        return result

    def queue(self, sid):
        rows = self.queue_rows(sid)
        handed = sorted([r for r in rows if r['state'] != 'waiting'], key=lambda r: r['id'])
        waiting = [r for r in rows if r['state'] == 'waiting']
        reserved = self.meta('next_request')
        locked = next((r for r in waiting if r['id'] == reserved), None)
        ordered = []
        count = self.meta('selection_count', 0)
        if locked:
            locked['protected'] = True
            ordered.append(locked)
            waiting.remove(locked)
            count += 1
        while waiting:
            if self.room['fair_queue'] and (count + 1) % 3 == 0:
                chosen = min(waiting, key=lambda r: (r['created'], r['id']))
            elif self.room['fair_queue'] and any(r['guaranteed'] for r in waiting):
                chosen = next(r for r in waiting if r['guaranteed'])
            else:
                chosen = min(waiting, key=lambda r: (-r['votes'], r['created'], r['id']))
            ordered.append(chosen)
            waiting.remove(chosen)
            count += 1
        return handed + ordered

    def reserve_next(self):
        if self.db.execute("SELECT 1 FROM songs WHERE state IN ('sending','accepted','queued','uncertain')").fetchone():
            return
        current = self.meta('next_request')
        if current and self.db.execute("SELECT 1 FROM songs WHERE id=? AND state='waiting'", (current,)).fetchone():
            return
        rows = self.queue('')
        self.set_meta('next_request', rows[0]['id'] if rows else None)

    def commit_selection(self, song):
        # Commit the scheduling slot and sending state together before any remote mutation.
        # A restart must hold the same request for reconciliation, never select a replacement.
        with self.db:
            if self.meta('last_selection') != song:
                for key, value in [('selection_count', self.meta('selection_count', 0) + 1), ('last_selection', song)]:
                    self.db.execute('INSERT OR REPLACE INTO room_meta VALUES(?,?)', (key, json.dumps(value)))
            if self.meta('next_request') == song:
                self.db.execute("INSERT OR REPLACE INTO room_meta VALUES('next_request','null')")
            self.db.execute("UPDATE songs SET state='sending' WHERE id=? AND state='waiting'", (song,))

    def check_rules(self, song):
        if self.room['block_explicit'] and song.get('explicit') is not False:
            raise f.Failure('Explicit tracks or tracks with unknown content rating are not allowed by the host.')
        limit = self.room['max_minutes']
        if limit and (not song.get('duration_ms') or song['duration_ms'] > limit * 60000):
            raise f.Failure('This track exceeds the host track-length limit or has no verified duration.')
        blocked = {x.casefold() for x in self.room['blocked_tracks']}
        if song['uri'].casefold() in blocked or song.get('name', '').casefold() in blocked:
            raise f.Failure('This track is blocked by the host.')
        artists = song.get('artist_names') or [song.get('artists', '')]
        if any(a.casefold() in {x.casefold() for x in self.room['blocked_artists']} for a in artists):
            raise f.Failure('This artist is blocked by the host.')
        if self.room['repeat_minutes']:
            if self.db.execute('SELECT 1 FROM requested_plays WHERE uri=? AND started>?', (song['uri'], time.time() - self.room['repeat_minutes'] * 60)).fetchone():
                raise f.Failure('This song was played recently. Try another favorite.')

    def submit(self, sid, uri):
        from local_player import LOCAL
        local = bool(LOCAL.fullmatch(str(uri)))
        if not f.TRACK.fullmatch(str(uri)) and not local:
            raise f.Failure('This track cannot be requested.')
        if local and not self.local_bound():
            raise f.Failure('The host must enable local playback for this room first.')
        if not self.accepting:
            raise f.Failure('The host has paused new requests.')
        favorite = self.db.execute('SELECT metadata FROM favorites WHERE uri=?', (uri,)).fetchone()
        song = self.catalog.get(uri) or (json.loads(favorite[0]) if favorite else None)
        if local:
            indexed = self.db.execute('SELECT metadata FROM local_tracks WHERE uri=?', (uri,)).fetchone()
            song = json.loads(indexed[0]) if indexed else None
        if not song:
            raise f.Failure('Search for this track first.')
        self.check_rules(song)
        if self.db.execute("SELECT id FROM songs WHERE uri=? AND state NOT IN ('past','removed')", (uri,)).fetchone():
            raise f.Failure('Already requested. Give it a vote instead.')
        guest = self.guest_id(sid)
        if self.db.execute("SELECT count(*) FROM songs WHERE guest=? AND state='waiting'", (guest,)).fetchone()[0] >= 3:
            raise f.Failure('You can have up to three waiting requests.')
        was_empty = not self.db.execute("SELECT 1 FROM songs WHERE state IN ('waiting','sending','accepted','queued','uncertain','playing')").fetchone()
        gathering = self.meta('gathering')
        first = not self.db.execute("SELECT 1 FROM songs WHERE guest=? AND gathering=? AND state!='removed'", (guest, gathering)).fetchone()
        name = self.guest_name(guest)
        with self.db:
            row = self.db.execute('INSERT INTO songs(guest,uri,metadata,created,requested_by,gathering,guaranteed) VALUES(?,?,?,?,?,?,?)',
                                  (guest, uri, json.dumps(song), time.time(), name, gathering, first))
            self.db.execute('INSERT INTO votes(song,guest,voter_name,created) VALUES(?,?,?,?)', (row.lastrowid, guest, name, time.time()))
        self.reserve_next()
        if was_empty:
            self.set_meta('immediate_request',row.lastrowid)
            self.next_poll=0

    def vote(self, sid, song):
        row = self.db.execute('SELECT state FROM songs WHERE id=?', (song,)).fetchone()
        if not row or row['state'] != 'waiting':
            raise f.Failure('This track is locked for playback.')
        guest = self.guest_id(sid)
        self.db.execute('INSERT OR IGNORE INTO votes(song,guest,voter_name,created) VALUES(?,?,?,?)', (song, guest, self.guest_name(guest), time.time()))
        self.db.commit()

    def observe_play(self, state, skipped=False):
        return self.observe_requested_play(state,skipped)

    def refresh_upcoming(self):
        if time.monotonic() < self.upcoming_due:
            return
        self.upcoming_due = time.monotonic() + 30
        from web_server import track
        try:
            data = self.api.call('/me/player/queue')
            self.spotify_upcoming = {'items': [track(x) for x in data.get('queue', [])[:20]], 'updated': time.time(), 'error': ''}
        except f.Failure as error:
            self.spotify_upcoming['error'] = str(error)

    @staticmethod
    def playlist_summary(item):
        return {'id': item.get('id'), 'name': item.get('name') or 'Untitled playlist',
                'artists': (item.get('owner') or {}).get('display_name') or (item.get('owner') or {}).get('id', ''),
                'art': next((i.get('url', '') for i in item.get('images') or []), ''),
                'url': (item.get('external_urls') or {}).get('spotify', ''),
                'total': (item.get('items') or item.get('tracks') or {}).get('total'),
                'public': item.get('public')}

    def playlist_call(self, path, params=None):
        try:
            return self.api.call(path, params=params)
        except f.Failure as error:
            if '403' in str(error) or '401' in str(error):
                raise f.Failure('Playlist access denied. Reconnect Spotify from the host desk to grant playlist read access. Spotify may restrict track browsing to playlists this account owns or collaborates on.')
            raise

    def list_playlists(self, sid, query):
        if not self.sessions[sid]['host']:
            raise PermissionError('Host access required.')
        offset = int(query.get('offset', ['0'])[0])
        if not 0 <= offset <= 100000:
            raise f.Failure('Invalid playlist page.')
        text = query.get('q', [''])[0].strip()
        if len(text) > 150:
            raise f.Failure('Playlist search must be at most 150 characters.')
        if text:
            if offset > 990:
                raise f.Failure('No more search pages.')
            result = self.playlist_call('/search', {'q': text, 'type': 'playlist', 'limit': 10, 'offset': offset}).get('playlists', {})
            step = 10
        else:
            result = self.playlist_call('/me/playlists', {'limit': 50, 'offset': offset})
            step = 50
        enabled = {r[0] for r in self.db.execute('SELECT id FROM shared_playlists')}
        return {'items': [dict(self.playlist_summary(x), enabled=x.get('id') in enabled) for x in result.get('items', []) if x and x.get('id')],
                'more': bool(result.get('next')) and offset + step <= (990 if text else 100000), 'next_offset': offset + step}

    def playlist_tracks(self, sid, playlist_id, offset=0):
        if not f.re.fullmatch(r'[A-Za-z0-9]{22}', playlist_id) or not 0 <= offset <= 100000:
            raise f.Failure('Invalid playlist or page.')
        shared = self.db.execute('SELECT metadata FROM shared_playlists WHERE id=?', (playlist_id,)).fetchone()
        host = self.sessions[sid]['host']
        if not host and not shared:
            raise PermissionError('This playlist is not enabled for guests.')
        key = (playlist_id, offset)
        cached = self.playlist_cache.get(key)
        if cached and time.monotonic() < cached[0]:
            result = copy.deepcopy(cached[1])
        else:
            from web_server import track
            summary = json.loads(shared[0]) if shared else self.playlist_summary(self.playlist_call('/playlists/' + playlist_id))
            page = self.playlist_call('/playlists/' + playlist_id + '/items', {'limit': 50, 'offset': offset})
            items = []
            for entry in page.get('items', []):
                raw = (entry or {}).get('item') or (entry or {}).get('track')
                if not raw:
                    items.append({'name': 'Unavailable track', 'artists': '', 'requestable': False, 'reason': 'Spotify did not return this item.'})
                    continue
                local = bool(entry.get('is_local') or raw.get('is_local') or str(raw.get('uri', '')).startswith('spotify:local:'))
                song = track(raw)
                requestable = not local and bool(f.TRACK.fullmatch(str(song['uri']))) and raw.get('is_playable') is not False
                items.append(dict(song, is_local=local, requestable=requestable,
                                  reason='Local file · Soloist playback not verified' if local else ('' if requestable else 'Unavailable or not a supported music track')))
            result = {'playlist': summary, 'items': items, 'more': bool(page.get('next')) and offset + 50 <= 100000,
                      'next_offset': offset + 50}
            if len(self.playlist_cache) >= 100:
                self.playlist_cache.clear()
            self.playlist_cache[key] = (time.monotonic() + 30, copy.deepcopy(result))
        if len(self.catalog) > 2000:
            self.catalog.clear()
        for song in result['items']:
            if song.get('requestable'):
                song['house_favorite'] = bool(self.db.execute('SELECT 1 FROM favorites WHERE uri=?', (song['uri'],)).fetchone())
                self.catalog[song['uri']] = song
        return result

    def favorites(self):
        return [dict(json.loads(r['metadata']), plays=r['plays']) for r in self.db.execute('''
          SELECT f.*, (SELECT count(*) FROM requested_plays p WHERE p.uri=f.uri AND p.qualified=1) AS plays
          FROM favorites f ORDER BY plays DESC, f.created,f.uri''')]

    def stats(self, period, sid):
        zone = ZoneInfo(self.room['timezone'])
        now = datetime.now(zone)
        cutoffs = {'tonight': self.meta('gathering_started'), 'week': (now - timedelta(days=7)).timestamp(),
                   'month': now.replace(day=1,hour=0,minute=0,second=0,microsecond=0).timestamp(), 'all': 0}
        if period not in cutoffs:
            raise f.Failure('Unknown stats period.')
        since = cutoffs[period]
        rows = self.db.execute('SELECT * FROM requested_plays WHERE started>=? ORDER BY started DESC LIMIT 100', (since,)).fetchall()
        recent = [dict(json.loads(r['metadata']), requested_by=r['requester'] or 'Background music', started=r['started'],
                       outcome=r['outcome'], qualified=bool(r['qualified'])) for r in rows]
        top = [dict(json.loads(r['metadata']), plays=r['plays']) for r in self.db.execute('''
          SELECT uri,metadata,count(*) AS plays FROM requested_plays WHERE started>=? AND qualified=1
          GROUP BY uri ORDER BY plays DESC,uri LIMIT 40''', (since,))]
        users = {r['requester']: {'name':r['requester'], 'plays':r['plays'], 'votes':0} for r in self.db.execute('''
          SELECT requester,count(*) AS plays FROM requested_plays WHERE started>=? AND qualified=1 AND requester!='' AND requester!=?
          GROUP BY requester''', (since, SHY))}
        favorites = [dict(json.loads(r['metadata']), votes=r['votes']) for r in self.db.execute('''
          SELECT s.uri,s.metadata,count(*) AS votes FROM songs s JOIN votes v ON v.song=s.id
          WHERE v.created>=? AND v.guest!=s.guest GROUP BY s.uri ORDER BY votes DESC,s.uri LIMIT 40''', (since,))]
        for r in self.db.execute('''
          SELECT s.requested_by,count(*) AS votes FROM songs s JOIN votes v ON v.song=s.id
          WHERE v.created>=? AND v.guest!=s.guest AND s.requested_by!='' AND s.requested_by!=? GROUP BY s.requested_by''', (since, SHY)):
            users.setdefault(r['requested_by'], {'name': r['requested_by'], 'plays': 0, 'votes': 0})['votes'] = r['votes']
        crowd = [dict(json.loads(r['metadata']), supporters=r['supporters']) for r in self.db.execute('''
          SELECT s.uri,s.metadata,count(DISTINCT v.guest) AS supporters FROM songs s JOIN votes v ON v.song=s.id
          WHERE v.created>=? AND v.guest!=s.guest GROUP BY s.uri ORDER BY supporters DESC,s.uri LIMIT 40''', (since,))]
        discovery = [dict(json.loads(r['metadata']), requested_by=r['requested_by'] or SHY, votes=r['votes']) for r in self.db.execute('''
          SELECT s.*,count(*) AS votes FROM songs s JOIN votes v ON v.song=s.id
          WHERE v.created>=? AND v.guest!=s.guest
            AND EXISTS(SELECT 1 FROM requested_plays p WHERE p.song=s.id AND p.qualified=1)
            AND NOT EXISTS(SELECT 1 FROM requested_plays p WHERE p.uri=s.uri AND p.started<s.created AND p.qualified=1)
          GROUP BY s.id ORDER BY votes DESC,s.created LIMIT 1''', (since,))]
        mine = [dict(json.loads(r['metadata']), state=r['state'], requested=r['created'], votes=r['votes']) for r in self.db.execute('''
          SELECT s.*,count(v.guest) AS votes FROM songs s LEFT JOIN votes v ON v.song=s.id
          WHERE s.guest=? AND s.created>=? GROUP BY s.id ORDER BY s.created DESC LIMIT 100''', (self.guest_id(sid), since))]
        reactions = self.db.execute('SELECT count(*) FROM reactions r JOIN requested_plays p ON p.id=r.play WHERE p.started>=?', (since,)).fetchone()[0]
        return {'recent': recent[:100], 'top': top, 'votes': favorites, 'crowd': crowd, 'discovery': discovery,
                'users': sorted(users.values(), key=lambda x: (-x['plays'], -x['votes'], x['name'])),
                'mine': mine, 'recap': {'plays': self.db.execute('SELECT count(*) FROM requested_plays WHERE started>=? AND qualified=1', (since,)).fetchone()[0],
                                      'guests': self.db.execute('SELECT count(DISTINCT guest) FROM songs WHERE created>=?', (since,)).fetchone()[0],
                                      'reactions': reactions}, 'truncated': False}

    def room_state(self, sid):
        guest = self.guest_id(sid)
        profile = self.db.execute('SELECT * FROM guests WHERE id=?', (guest,)).fetchone()
        current = self.db.execute("SELECT id FROM requested_plays WHERE outcome='observing' ORDER BY id DESC LIMIT 1").fetchone()
        reactions = []
        if current:
            reactions = [dict(r) for r in self.db.execute('SELECT reaction,count(*) AS count FROM reactions WHERE play=? GROUP BY reaction', (current['id'],))]
        now = dict(self.now) if self.now else None
        if now:
            owner = self.db.execute("SELECT s.requested_by FROM songs s JOIN request_attempts a ON a.song=s.id WHERE s.uri=? AND s.state='playing' AND a.confirmed=1 ORDER BY s.id DESC LIMIT 1", (now['uri'],)).fetchone()
            if owner:
                now['requested_by'] = owner[0] or SHY
        return {'now': now, 'profile': {'name': profile['name'] if profile else SHY, 'named': bool(profile and profile['named'])},
                'local_player': dict(self.local_config, count=self.db.execute('SELECT count(*) FROM local_tracks').fetchone()[0]) if self.sessions.get(sid, {}).get('host') else None,
                'room': self.room, 'version': os.environ.get('UNICORNTUNES_VERSION', '0.3.0-dev'),
                'spotify_upcoming': self.spotify_upcoming, 'play_id': current['id'] if current else None,
                'reactions': reactions, 'liked': bool(current and self.db.execute('SELECT 1 FROM reactions WHERE play=? AND guest=?',(current['id'],guest)).fetchone()), 'history_cleanup': self.meta('history_cleanup',{}) if self.sessions.get(sid,{}).get('host') else None, 'background': bool(self.now and not self.db.execute("SELECT 1 FROM songs WHERE state='playing'").fetchone()),
                'gathering': self.meta('gathering'), 'presets': PRESETS if self.sessions.get(sid, {}).get('host') else {}}

    def room_action(self, sid, route, body):
        guest = self.guest_id(sid)
        if route == '/api/profile':
            name = str(body.get('name', '')).strip()[:48]
            self.db.execute('INSERT OR REPLACE INTO guests VALUES(?,?,1)', (guest, name or SHY))
            self.db.commit()
        elif route == '/api/guest/switch':
            # Returning a new token makes switching on a shared tablet distinct from renaming a person.
            token = self.attach_guest(sid, '')
            return {'ok': True, '_guest_token': token}
        elif route == '/api/react':
            play = self.db.execute("SELECT id FROM requested_plays WHERE outcome='observing' ORDER BY id DESC LIMIT 1").fetchone()
            reaction = body.get('reaction')
            if play and 'play' in body and body['play']!=play['id']:
                raise f.Failure('The song changed. Refresh before liking the next one.')
            if not play or reaction not in ('like','love','fire','great-pick'):
                raise f.Failure('Choose a reaction to the current song.')
            self.db.execute('INSERT OR REPLACE INTO reactions VALUES(?,?,?)', (play['id'], guest, 'like'))
            self.db.commit()
        elif route == '/api/host/history-reset':
            return self.reset_play_history()
        elif route == '/api/host/playlist':
            playlist_id = str(body.get('id', ''))
            if not f.re.fullmatch(r'[A-Za-z0-9]{22}', playlist_id):
                raise f.Failure('Invalid playlist ID.')
            if body.get('remove'):
                self.db.execute('DELETE FROM shared_playlists WHERE id=?', (playlist_id,))
            else:
                # Verify contents access before exposing private playlist details to guests.
                data = self.playlist_tracks(sid, playlist_id)
                if self.db.execute('SELECT count(*) FROM shared_playlists').fetchone()[0] >= 100 and not self.db.execute('SELECT 1 FROM shared_playlists WHERE id=?', (playlist_id,)).fetchone():
                    raise f.Failure('At most 100 guest playlists can be enabled.')
                self.db.execute('INSERT OR REPLACE INTO shared_playlists VALUES(?,?,?)',
                                (playlist_id, json.dumps(data['playlist']), time.time()))
            self.db.commit()
        elif route == '/api/host/favorite':
            uri = body.get('uri')
            if body.get('remove'):
                self.db.execute('DELETE FROM favorites WHERE uri=?', (uri,))
            else:
                song = self.catalog.get(uri)
                if not song:
                    raise f.Failure('Search for this song first.')
                self.db.execute('INSERT OR IGNORE INTO favorites VALUES(?,?,?)', (uri, json.dumps(song), time.time()))
            self.db.commit()
        elif route == '/api/host/config':
            proposal = copy.deepcopy(self.room)
            if body.get('defaults'):
                proposal['copy'] = dict(COPY)
                proposal['hide_eyebrow'] = False
            for key in ('hide_eyebrow', 'fair_queue', 'block_explicit'):
                if key in body:
                    if not isinstance(body[key], bool):
                        raise f.Failure('Invalid switch setting.')
                    proposal[key] = body[key]
            for key in ('max_minutes', 'repeat_minutes'):
                if key in body:
                    value = int(body[key])
                    if not 0 <= value <= 1440:
                        raise f.Failure('Limits must be between 0 and 1440 minutes.')
                    proposal[key] = value
            for key in ('blocked_tracks', 'blocked_artists', 'theme_searches'):
                if key in body:
                    if not isinstance(body[key], list) or len(body[key]) > 100:
                        raise f.Failure('Use at most 100 entries.')
                    proposal[key] = [str(v).strip()[:150] for v in body[key] if str(v).strip()]
            if 'theme' in body:
                proposal['theme'] = str(body['theme']).strip()[:100]
            if 'timezone' in body:
                try:
                    ZoneInfo(str(body['timezone']))
                except (KeyError, ValueError):
                    raise f.Failure('Unknown timezone.')
                proposal['timezone'] = str(body['timezone'])
            if 'copy' in body:
                if not isinstance(body['copy'], dict):
                    raise f.Failure('Invalid copy settings.')
                for key in COPY:
                    if key in body['copy']:
                        proposal['copy'][key] = str(body['copy'][key])[:300]
            # Rules affect future selections too. Host can remove newly blocked waiting tracks explicitly.
            f.private_write(self.data / 'room.json', proposal)
            self.room = proposal
        elif route == '/api/host/location':
            name, device = str(body.get('name', '')).strip()[:60], str(body.get('device', ''))
            if not name:
                raise f.Failure('Enter a location name.')
            proposal = copy.deepcopy(self.room)
            if body.get('remove'):
                proposal['locations'].pop(name, None)
            else:
                devices = self.api.call('/me/player/devices').get('devices', [])
                if not any(d.get('id') == device and not d.get('is_restricted') for d in devices):
                    raise f.Failure('Choose an available player.')
                if len(proposal['locations']) >= 20 and name not in proposal['locations']:
                    raise f.Failure('At most 20 saved locations.')
                proposal['locations'][name] = device
            f.private_write(self.data / 'room.json', proposal)
            self.room = proposal
        elif route == '/api/host/gathering':
            if self.db.execute("SELECT 1 FROM songs WHERE state IN ('sending','accepted','queued','uncertain')").fetchone():
                raise f.Failure('Resolve handed-off tracks before starting a new gathering.')
            self.enabled = False
            self.reason = 'Handoff stopped by host.'
            self.db.execute("UPDATE songs SET state='removed',detail='Archived by new gathering' WHERE state='waiting'")
            self.db.commit()
            self.set_meta('gathering', self.meta('gathering') + 1)
            self.set_meta('gathering_started', time.time())
            self.set_meta('selection_count', 0)
            self.set_meta('next_request', None)
        else:
            return None
        return {'ok': True}
