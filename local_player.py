"""Optional local playback provider. No hardcoded player address or shared shell access."""
import base64
import io
import json
import re
import subprocess
import time
import threading

import foxtunes as f

LOCAL=re.compile(r'^local:[a-f0-9]{64}$')


def artwork_thumbnail(raw):
    from PIL import Image, ImageOps, UnidentifiedImageError
    import warnings
    try:
        with warnings.catch_warnings():
            warnings.simplefilter('error', Image.DecompressionBombWarning)
            with Image.open(io.BytesIO(raw)) as image:
                if image.width * image.height > 24_000_000:
                    raise f.Failure('Cover dimensions exceed the 24 megapixel limit.')
                image = ImageOps.exif_transpose(image)
                image.thumbnail((320,320), Image.Resampling.LANCZOS)
                if image.mode in ('RGBA','LA') or (image.mode=='P' and 'transparency' in image.info):
                    rgba=image.convert('RGBA')
                    background=Image.new('RGB',rgba.size,(18,18,18));background.paste(rgba,mask=rgba.getchannel('A'))
                    image=background
                else:image=image.convert('RGB')
                output=io.BytesIO();image.save(output,format='JPEG',quality=85,optimize=True)
                return 'image/jpeg', output.getvalue()
    except (UnidentifiedImageError, OSError, ValueError, Image.DecompressionBombError, Image.DecompressionBombWarning) as error:
        raise f.Failure('Embedded cover image could not be decoded safely.') from error


class RemoteMPD:
    def __init__(self,data,config):
        self.data,self.config=data,config

    def call(self,action,**args):
        host=self.config.get('host','');user=self.config.get('user','')
        if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}',host) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',user):
            raise f.Failure('Configure the local-player hostname and SSH user in the host desk.')
        port=int(self.config.get('port',22))
        if not 1<=port<=65535:raise f.Failure('Invalid local-player SSH port.')
        key=self.data/'local-player.key';known=self.data/'local-player.known_hosts'
        if not key.exists() or not known.exists():raise f.Failure('Install the dedicated player key and verified host key using the local-player setup guide.')
        command=['ssh','-T','-o','BatchMode=yes','-o','ConnectTimeout=5','-o','StrictHostKeyChecking=yes',
                 '-o','IdentitiesOnly=yes','-o','GlobalKnownHostsFile=/dev/null','-o','UserKnownHostsFile='+str(known),
                 '-o','ControlMaster=auto','-o','ControlPersist=30','-o','ControlPath=/tmp/unicorntunes-mpd-%C',
                 '-i',str(key),'-p',str(port),user+'@'+host,'unicorntunes-mpd']
        try:
            result=subprocess.run(command,input=json.dumps(dict(action=action,**args))+'\n',text=True,capture_output=True,timeout=30 if action=='picture' else 12)
            if len(result.stdout)>24_000_000:raise f.Failure('Local library response is too large.')
            if not result.stdout.strip():raise f.Failure('Local-player SSH connection failed. Check its address, dedicated key and pinned host key.')
            payload=json.loads(result.stdout)
            if result.returncode or payload.get('error'):raise f.Failure('Local player: '+str(payload.get('error','command failed'))[:300],uncertain=action in ('play','pause','stop'))
            return payload
        except (OSError,subprocess.TimeoutExpired,ValueError) as error:
            raise f.Failure('Local-player connection/response failed; check the player before retrying playback.',uncertain=action in ('play','pause','stop')) from error


class LocalPlayback:
    def local_init(self):
        path=self.data/'local-player.json'
        self.local_config=f.load(path) if path.exists() else {'enabled':False,'host':'','user':'','port':22,'device':'','volume':50}
        self.local=RemoteMPD(self.data,self.local_config)
        self.db.execute('CREATE TABLE IF NOT EXISTS local_tracks(uri TEXT PRIMARY KEY,metadata TEXT NOT NULL)')
        self.db.execute('CREATE TABLE IF NOT EXISTS local_art(uri TEXT PRIMARY KEY, mime TEXT, data BLOB, updated REAL)')
        self.db.commit()
        if self.meta('local_art_cache_version') != 2:
            self.db.execute('DELETE FROM local_art')
            self.db.commit()
            self.set_meta('local_art_cache_version',2)
        self.art_lock=threading.Lock()
        self.art_errors={}
        self.local_active=self.meta('local_active')
        self.local_error=''
        self.player_missing=False

    def local_bound(self):
        return bool(self.local_config.get('enabled') and self.device and self.local_config.get('device')==self.device)

    def scan_local(self):
        if not self.local_config.get('enabled'):raise f.Failure('Enable and configure the local player first.')
        self.local.call('scan')
        for _ in range(20):
            if not self.local.call('status').get('updating'):break
            time.sleep(.25)
        else:raise f.Failure('Local library scan is still running; try again shortly.')
        result=self.local.call('library')
        songs=result.get('items',[])
        if not isinstance(songs,list) or len(songs)>1000:raise f.Failure('Local library must contain at most 1000 indexed tracks.')
        cleaned=[]
        for song in songs:
            if not isinstance(song,dict) or not LOCAL.fullmatch(str(song.get('uri',''))):raise f.Failure('Invalid local track returned by player.')
            cleaned.append(dict(song,source='local',url='',art='/api/local-art/'+song['uri'][6:]))
        with self.db:
            self.db.execute('DELETE FROM local_art')
            self.art_errors.clear()
            self.db.execute('DELETE FROM local_tracks')
            self.db.executemany('INSERT INTO local_tracks VALUES(?,?)',[(s['uri'],json.dumps(s)) for s in cleaned])
        self.local_error=''
        return {'count':len(cleaned)}

    def local_search(self,query,offset):
        terms=query.get('q',[''])[0].strip().casefold().split()
        title=query.get('track',[''])[0].strip().casefold();artist=query.get('artist',[''])[0].strip().casefold()
        album=query.get('album',[''])[0].strip().casefold();year=query.get('year',[''])[0].strip()
        exact=query.get('exact',['0'])[0]=='1'
        if exact and not title:raise f.Failure('Enter a title for exact matching.')
        matches=[]
        for row in self.db.execute('SELECT metadata FROM local_tracks ORDER BY uri'):
            song=json.loads(row[0]);name=song.get('name','').casefold()
            hay=' '.join(str(song.get(k,'')) for k in ('name','artists','album','year')).casefold()
            if any(t not in hay for t in terms):continue
            if title and (name!=title if exact else title not in name):continue
            if artist and artist not in song.get('artists','').casefold():continue
            if album and album not in song.get('album','').casefold():continue
            if year:
                bounds=year.split('-')
                if len(bounds)>2 or any(not re.fullmatch(r'\d{4}',v) for v in bounds) or bounds[0]>bounds[-1]:raise f.Failure('Use a four-digit year or year range.')
                if not bounds[0]<=song.get('year','')<=bounds[-1]:continue
            song['house_favorite']=bool(self.db.execute('SELECT 1 FROM favorites WHERE uri=?',(song['uri'],)).fetchone())
            matches.append(song)
        matches.sort(key=lambda s:(s['name'].casefold(),s['uri']))
        return matches[offset:offset+10],len(matches)>offset+10

    def local_artwork(self, digest):
        # Artwork SSH work must never hold the playback worker's global lock.
        with self.art_lock:
            return self.fetch_local_artwork(digest)

    def fetch_local_artwork(self, digest):
        from mpd_agent import image_type, MAX_ART
        uri = 'local:' + digest
        with self.lock:
            if not LOCAL.fullmatch(uri) or not self.local_config.get('enabled'):
                raise LookupError('Local artwork unavailable.')
            if not self.db.execute('SELECT 1 FROM local_tracks WHERE uri=?',(uri,)).fetchone():
                raise LookupError('Local file not indexed.')
            cached = self.db.execute('SELECT * FROM local_art WHERE uri=?',(uri,)).fetchone()
            if cached and (cached['data'] or time.time()-cached['updated'] < 300):
                return cached['mime'], bytes(cached['data'])
            provider = self.local
        failed = self.art_errors.get(uri)
        if failed and time.monotonic() < failed[0]:
            raise f.Failure(failed[1])
        try: result = provider.call('picture', uri=uri)
        except f.Failure as error:
            with self.lock:
                if len(self.art_errors)>=256:self.art_errors.pop(next(iter(self.art_errors)))
                self.art_errors[uri]=(time.monotonic()+30,str(error))
            raise
        encoded = result.get('data', '')
        if not isinstance(encoded, str) or len(encoded) > (MAX_ART+2)//3*4:
            raise f.Failure('Local artwork response is too large.')
        try: raw = base64.b64decode(encoded, validate=True)
        except ValueError as error: raise f.Failure('Invalid local artwork response.') from error
        mime = image_type(raw) if raw else ''
        if raw and (not mime or len(raw)>MAX_ART): raise f.Failure('Unsupported local artwork.')
        if raw:
            mime, raw = artwork_thumbnail(raw)
        self.art_errors.pop(uri, None)
        with self.lock, self.db:
            size = self.db.execute('SELECT coalesce(sum(length(data)),0) FROM local_art').fetchone()[0]
            while size + len(raw) > 64*1024*1024:
                old = self.db.execute('SELECT uri,length(data) FROM local_art ORDER BY updated LIMIT 1').fetchone()
                if not old: break
                self.db.execute('DELETE FROM local_art WHERE uri=?',(old[0],));size-=old[1]
            self.db.execute('INSERT OR REPLACE INTO local_art VALUES(?,?,?,?)',(uri,mime,raw,time.time()))
        return mime, raw

    def apply_local_volume(self, volume):
        if type(volume) is not int or not 0 <= volume <= 100:
            raise f.Failure('Local volume must be a whole number from 0 to 100.')
        if self.local_active:
            self.owned_local_status()
            result = self.local.call('volume', volume=volume)
            if result.get('volume') != volume: raise f.Failure('MPD did not confirm the requested volume.')
        key='normalized_volume' if self.local_config.get('normalization_enabled') else 'volume'
        config = dict(self.local_config, **{key:volume})
        f.private_write(self.data/'local-player.json', config)
        self.local_config=config
        self.local.config=config
        return {'volume':volume,'live':bool(self.local_active)}

    def local_action(self,sid,route,body):
        if route=='/api/host/loudness-status':return self.local.call('loudness_status')
        if route=='/api/host/loudness-start':
            if self.local_config.get('normalization_enabled'):
                raise f.Failure('Disable matched playback before preparing a new loudness target.')
            from local_loudness import validate_target
            return self.local.call('loudness_start',target=validate_target(body.get('target',-14)))
        if route=='/api/host/loudness-cancel':return self.local.call('loudness_cancel')
        if route=='/api/host/loudness-config':
            if self.local_active:raise f.Failure('Finish local playback before changing loudness mode.')
            from local_loudness import validate_target
            enabled=body.get('enabled') is True
            target=validate_target(body.get('target',-14))
            volume=body.get('volume',100)
            if type(volume) is not int or not 0<=volume<=100:raise f.Failure('Output level must be between 0 and 100.')
            if enabled:
                status=self.local.call('loudness_status')
                if status.get('running') or status.get('phase')!='complete' or not status.get('total') or status.get('ready')!=status['total'] or status.get('target')!=target:
                    raise f.Failure('Prepare every local track successfully at this target before enabling matching.')
            config=dict(self.local_config,normalization_enabled=enabled,loudness_target=target,normalized_volume=volume)
            f.private_write(self.data/'local-player.json',config)
            self.local_config=config;self.local.config=config
            return {'enabled':enabled,'volume':volume,'target':target}
        if route=='/api/host/local-volume':return self.apply_local_volume(body.get('volume'))
        if route=='/api/host/local-config':
            if self.local_active:raise f.Failure('Stop or finish local playback before changing its connection.')
            config=dict(self.local_config,**{'enabled':bool(body.get('enabled')),'host':str(body.get('host','')).strip(),
                    'user':str(body.get('user','')).strip(),'port':int(body.get('port',22)),
                    'device':str(body.get('device','')),'volume':int(body.get('volume',50))})
            if not 1<=config['port']<=65535 or not 0<=config['volume']<=100:raise f.Failure('Invalid port or local volume.')
            if config['enabled']:
                if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9.:-]{0,252}',config['host']) or not re.fullmatch(r'[a-z_][a-z0-9_-]{0,31}',config['user']):raise f.Failure('Enter a valid player hostname/IP and SSH username.')
                if not config['device']:raise f.Failure('Select the matching Spotify playback device before binding local playback.')
            if config.get('normalization_enabled'):
                config['normalized_volume']=int(body.get('volume',100))
                config['volume']=self.local_config.get('volume',50)
            f.private_write(self.data/'local-player.json',config)
            self.local_config=config;self.local=RemoteMPD(self.data,config)
            return {'ok':True}
        if route=='/api/host/local-scan':return self.scan_local()
        if route=='/api/host/local-test':return self.local.call('status')
        return None

    def mixed_search(self, query):
        from web_server import search_options, track
        offset = int(query.get('offset', ['0'])[0])
        source = query.get('source', ['all'])[0]
        if source not in ('all', 'spotify', 'local') or not 0 <= offset <= 990:
            raise ValueError('Invalid search source or page.')
        blank = not any(query.get(k, [''])[0].strip() for k in ('q', 'track', 'artist', 'album', 'year'))
        if blank and source != 'local':
            raise ValueError('Enter search text, or choose Local files to browse the library.')
        # Validate filters once for both providers, including exact-title and year syntax.
        text, exact = search_options(query) if not blank else ('', None)
        items, more, warning = [], False, ''
        if source != 'spotify' and self.local_config.get('enabled'):
            items, more = self.local_search(query, offset)
        if source != 'local':
            try:
                result = self.api.call('/search', params={'q':text,'type':'track','limit':10,'offset':offset}).get('tracks', {})
                items += [track(t) for t in result.get('items', []) if exact is None or t.get('name', '').strip().casefold() == exact]
                more = more or bool(result.get('next'))
            except f.Failure:
                if not items: raise
                warning = 'Spotify search is unavailable. Showing matching local files.'
        if len(self.catalog) > 2000: self.catalog.clear()
        for song in items:
            song['house_favorite'] = bool(self.db.execute('SELECT 1 FROM favorites WHERE uri=?', (song['uri'],)).fetchone())
            self.catalog[song['uri']] = song
        return {'items':items, 'more':more and offset < 990, 'offset':offset, 'warning':warning}

    def start_local_request(self, row, spotify):
        if not self.local_bound():
            raise f.Failure('Local playback is not linked to the selected Spotify device. Save its connection in the host desk.')
        status = self.local.call('status')
        if status.get('state') in ('play', 'pause'):
            raise f.Failure('MPD has another active track. Stop it before starting local playback.')
        if status.get('error'): raise f.Failure('MPD reports an error: ' + status['error'])
        self.check_rules(row)
        normalized=self.local_config.get('normalization_enabled',False)
        if normalized and not self.local.call('loudness_check',uri=row['uri'],target=self.local_config.get('loudness_target',-14)).get('ready'):
            raise f.Failure('This local track needs loudness preparation before playback.')
        actual = (spotify.get('device') or {}).get('id')
        if actual and actual != self.device: raise f.Failure('Another Spotify device is active; local playback was not started.')
        self.commit_selection(row['id'])
        self.local_active = {'id':row['id'], 'uri':row['uri'], 'song_id':None, 'starting':True}
        self.set_meta('local_active', self.local_active)
        self.set_meta('local_last_progress', 0)
        try:
            if spotify.get('is_playing'):
                self.api.call('/me/player/pause', 'PUT', params={'device_id':self.device})
                for _ in range(4):
                    observed = self.api.call('/me/player')
                    active_device = (observed.get('device') or {}).get('id')
                    if active_device and active_device != self.device:
                        raise f.Failure('Spotify device changed while pausing; local playback was not started.')
                    if not observed.get('is_playing'): break
                    time.sleep(.25)
                else: raise f.Failure('Spotify pause was not confirmed; local playback was not started.')
            options={'uri':row['uri'],'volume':self.local_config.get('normalized_volume',100) if normalized else self.local_config.get('volume',50)}
            if normalized:options.update(normalize=True,target=self.local_config.get('loudness_target',-14))
            result = self.local.call('play', **options)
            if result.get('song_id') is None: raise f.Failure('Local start response did not identify the MPD queue item.')
            self.local_active.update(song_id=str(result['song_id']), starting=False)
            self.set_meta('local_active', self.local_active)
            self.db.execute("UPDATE songs SET state='past',detail='Replaced at local-source transition' WHERE state='playing'")
            self.db.execute("UPDATE songs SET state='accepted',detail='Local start accepted; awaiting observed playback' WHERE id=?", (row['id'],))
            self.db.commit()
            self.idle_ready = False
        except f.Failure as error:
            self.db.execute("UPDATE songs SET state='uncertain',detail=? WHERE id=?", (str(error),row['id']))
            self.db.commit()
            raise

    def owned_local_status(self):
        status = self.local.call('status')
        active = self.local_active
        if active and (status.get('uri') != active['uri'] or
                       (active.get('song_id') is not None and str(status.get('song_id')) != active['song_id'])):
            raise f.Failure('MPD track ownership changed. Check the local player before reconciling.')
        return status

    def stop_local_owned(self):
        if not self.local_active: return
        status = self.local.call('status')
        if status.get('state') != 'stop':
            self.owned_local_status()
            self.local.call('stop')
        if self.local.call('status').get('state') != 'stop':
            raise f.Failure('MPD stop could not be confirmed. Spotify was not started.')
        self.db.execute("UPDATE songs SET state='past',detail='Local playback stopped by host' WHERE id=?", (self.local_active['id'],))
        self.db.commit()
        self.local_active = None
        self.set_meta('local_active', None)

    def poll_local(self):
        active = self.local_active
        status = self.owned_local_status()
        if active.get('starting'):
            raise f.Failure('Local start is unresolved. Check MPD and reconcile the request before restarting.')
        row = self.db.execute('SELECT * FROM songs WHERE id=?',(active['id'],)).fetchone()
        if not row or row['state'] == 'uncertain':
            raise f.Failure('Local playback requires host reconciliation.')
        try:
            spotify = self.api.call('/me/player')
        except f.Failure:
            if status.get('state') == 'play': self.local.call('pause')
            raise
        actual = (spotify.get('device') or {}).get('id')
        if spotify.get('is_playing') or (actual and actual != self.device):
            # Never stop someone else's MPD item; ownership was verified above.
            self.local.call('pause')
            raise f.Failure('Spotify playback resumed or moved during a local track. MPD paused; check both players before restarting.')
        if status.get('error'): raise f.Failure('MPD reports an error: ' + status['error'])
        song = json.loads(row['metadata'])
        self.now = dict(song, playing=status['state']=='play', progress_ms=status.get('progress_ms',0), normalized=bool(status.get('normalized')))
        self.updated = time.time()
        self.error = ''
        if status['state'] == 'play':
            self.db.execute("UPDATE songs SET state='playing' WHERE id=?", (row['id'],))
            self.db.commit()
            self.set_meta('local_last_progress', status.get('progress_ms',0))
            self.observe_play({'item':song,'device':{'id':self.device},'is_playing':True,'progress_ms':status.get('progress_ms',0)})
            return
        if status['state'] == 'pause': return
        duration = song.get('duration_ms') or 0
        previous = self.meta('local_last_progress', 0)
        # Only an observed near-end can advance automatically. An external stop is held.
        if row['state'] != 'playing' or not duration or previous < max(0, duration-10000):
            raise f.Failure('Local playback stopped before an observed end. Check MPD, then restart or reconcile.')
        self.db.execute("UPDATE songs SET state='past',detail='Local near-end followed by stop; full completion unverified' WHERE id=?", (row['id'],))
        self.db.execute("UPDATE plays SET outcome='unknown' WHERE outcome='observing'")
        self.db.commit()
        self.local_active = None
        self.set_meta('local_active', None)
        self.idle_ready = True
        if self.enabled:
            next_row = next((r for r in self.queue('') if r['state']=='waiting'),None)
            if next_row: self.start_idle_request(next_row)
        # With no requests, remain idle. The next request starts on its own source.
    def skip_mixed_request(self):
        pending = self.db.execute("SELECT id FROM songs WHERE state IN ('accepted','queued')").fetchall()
        if any(not self.local_active or r['id'] != self.local_active['id'] for r in pending):
            raise f.Failure('A Spotify request is already handed off. Wait for it or reconcile it before switching sources.')
        if self.db.execute("SELECT id FROM songs WHERE state IN ('sending','uncertain')").fetchone():
            raise f.Failure('Reconcile the uncertain request before skipping.')
        row = next((r for r in self.queue('') if r['state']=='waiting'),None)
        if row: self.check_rules(row)
        enabled = self.enabled
        self.enabled = False
        try:
            if self.local_active: self.stop_local_owned()
            state = self.api.call('/me/player')
            actual = (state.get('device') or {}).get('id')
            if actual and actual != self.device:
                raise f.Failure('Another Spotify device is active; restart on the selected player.')
            if row: self.start_idle_request(row)
            else:
                self.idle_ready = True
                self.now = None
            self.enabled = enabled
            self.reason, self.error, self.next_poll = '', '', 0
        except f.Failure as error:
            self.reason = self.error = 'Skip needs attention: ' + str(error)
            raise
