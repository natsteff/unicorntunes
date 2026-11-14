"""Playback ownership and requested-only history, with recoverable cleanup."""
import json
import os
import secrets
import sqlite3
import time


class RequestHistory:
    def history_init(self):
        self.db.execute('''CREATE TABLE IF NOT EXISTS request_attempts(
            song INTEGER PRIMARY KEY,device TEXT,kind TEXT,before_uri TEXT,before_progress INTEGER,
            before_playing INTEGER,confirmed INTEGER DEFAULT 0,seen_other INTEGER DEFAULT 0,created REAL)''')
        self.db.commit()
        if not self.meta('requested_history_v1'):
            self.reset_play_history(migration=True)
            self.db.execute("UPDATE songs SET state='uncertain',detail='Upgrade cannot verify this earlier handoff; check the player and reconcile' WHERE state IN ('accepted','queued') AND id NOT IN (SELECT song FROM request_attempts)")
            self.db.commit()
        self.db.execute("CREATE VIEW IF NOT EXISTS requested_plays AS SELECT p.* FROM plays p JOIN songs s ON s.id=p.song AND s.uri=p.uri JOIN request_attempts a ON a.song=s.id AND a.confirmed=1")
        self.db.execute('CREATE UNIQUE INDEX IF NOT EXISTS plays_request_once ON plays(song) WHERE song IS NOT NULL')
        self.db.execute("UPDATE reactions SET reaction='like'")
        self.db.commit()

    def reset_play_history(self, migration=False):
        count=self.db.execute('SELECT count(*) FROM plays').fetchone()[0]
        reactions=self.db.execute('SELECT count(*) FROM reactions').fetchone()[0]
        backup_name=''
        if count or reactions:
            self.db.commit()
            backup_name='web-history-backup-'+str(time.time_ns())+'-'+secrets.token_hex(4)+'.sqlite3'
            path=self.data/backup_name
            descriptor=os.open(path,os.O_CREAT|os.O_EXCL|os.O_WRONLY,0o600);os.close(descriptor)
            backup=sqlite3.connect(str(path))
            try:self.db.backup(backup)
            finally:backup.close()
        with self.db:
            self.db.execute('DELETE FROM reactions')
            self.db.execute('DELETE FROM plays')
            info={'removed_plays':count,'removed_reactions':reactions,'backup':backup_name,'at':time.time()}
            self.db.execute('INSERT OR REPLACE INTO room_meta VALUES(?,?)',('history_cleanup',json.dumps(info)))
            if migration:self.db.execute("INSERT OR REPLACE INTO room_meta VALUES('requested_history_v1','1')")
        return info

    def record_attempt(self, row, kind, device=None, before=None):
        before=(self.now or {}) if before is None else before
        self.db.execute('''INSERT OR REPLACE INTO request_attempts
            (song,device,kind,before_uri,before_progress,before_playing,confirmed,seen_other,created)
            VALUES(?,?,?,?,?,?,0,0,?)''',
            (row['id'],self.device if device is None else device,kind,before.get('uri'),
             before.get('progress_ms') or 0,int(bool(before.get('playing'))),time.time()))
        self.db.commit()

    def confirm_attempt(self, state, row, local=False):
        if row['state'] not in ('accepted','queued','playing'):return False
        attempt=self.db.execute('SELECT * FROM request_attempts WHERE song=?',(row['id'],)).fetchone()
        if not attempt:return False
        uri=(state.get('item') or {}).get('uri');device=(state.get('device') or {}).get('id')
        if device!=attempt['device'] or device!=self.device or not state.get('is_playing'):return False
        if uri!=row['uri']:
            self.db.execute('UPDATE request_attempts SET seen_other=1 WHERE song=?',(row['id'],));self.db.commit()
            return False
        progress=state.get('progress_ms') or 0
        fresh=0<=progress<=15000
        evidence=(attempt['confirmed'] or local or (fresh and (attempt['kind']=='direct' or
                  attempt['before_uri']!=uri or not attempt['before_playing'] or attempt['seen_other'] or
                  progress+1000<attempt['before_progress'])))
        if not evidence:return False
        with self.db:
            self.db.execute("UPDATE songs SET state='past',detail='A new requested playback was observed' WHERE state='playing' AND id!=?",(row['id'],))
            self.db.execute('UPDATE request_attempts SET confirmed=1 WHERE song=?',(row['id'],))
            self.db.execute("UPDATE songs SET state='playing' WHERE id=?",(row['id'],))
        return True

    def observe_requested_play(self, state, skipped=False):
        from web_server import track
        now=time.time();item=state.get('item') or {};uri=item.get('uri')
        progress=state.get('progress_ms') or 0;device=(state.get('device') or {}).get('id')
        active=self.db.execute("SELECT * FROM plays WHERE outcome='observing' ORDER BY id DESC LIMIT 1").fetchone()
        if not self.device or device!=self.device:
            if active:self.db.execute("UPDATE plays SET outcome='unknown' WHERE id=?",(active['id'],));self.db.commit()
            return
        duration=json.loads(active['metadata']).get('duration_ms',0) if active else 0
        gap=now-active['last_seen'] if active else 0
        restarted=bool(active and active['uri']==uri and progress+10000<active['last_progress'] and duration
                       and active['last_progress']>=duration-5000 and 0<=gap<=30
                       and gap+1 >= max(0,duration-active['last_progress'])/1000)
        if active and (active['uri']!=uri or restarted):
            duration=json.loads(active['metadata']).get('duration_ms',0)
            near=duration>0 and active['last_progress']>=duration-5000
            outcome='skipped' if skipped else ('ended-near-finish' if near and now-active['last_seen']<15 else 'unknown')
            self.db.execute('UPDATE plays SET outcome=? WHERE id=?',(outcome,active['id']))
            if restarted:self.db.execute("UPDATE songs SET state='past' WHERE id=? AND state='playing'",(active['song'],))
            active=None
        owner=self.db.execute('''SELECT s.* FROM songs s JOIN request_attempts a ON a.song=s.id
            WHERE s.uri=? AND s.state='playing' AND a.confirmed=1 AND a.device=? ORDER BY s.id DESC LIMIT 1''',(uri,self.device)).fetchone()
        if not owner:
            if active:self.db.execute("UPDATE plays SET outcome='unknown' WHERE id=?",(active['id'],))
            self.db.commit();self.reserve_next();return
        if active and active['song']!=owner['id']:
            self.db.execute("UPDATE plays SET outcome='unknown' WHERE id=?",(active['id'],));active=None
        if state.get('is_playing'):
            if not active:
                prior=self.db.execute('SELECT * FROM plays WHERE song=?',(owner['id'],)).fetchone()
                if prior:
                    gap=now-prior['last_seen'];delta=(progress-prior['last_progress'])/1000
                    local_owner=getattr(self,'local_active',None) or {}
                    verified_local=item.get('source')=='local' and local_owner.get('id')==owner['id']
                    if not verified_local and not (0<=gap<30 and 0<=delta<=gap+3):
                        self.db.commit();return
                    # Resume an owned request after restart without counting downtime or a duplicate play.
                    self.db.execute("UPDATE plays SET outcome='observing',last_seen=?,last_progress=? WHERE id=?",(now,progress,prior['id']))
                else:
                    self.db.execute('''INSERT INTO plays(song,uri,metadata,requester,guest,gathering,started,last_seen,last_progress)
                        VALUES(?,?,?,?,?,?,?,?,?)''',(owner['id'],uri,json.dumps(track(item)),owner['requested_by'],owner['guest'],owner['gathering'],now,now,progress))
            else:
                delta=max(0,progress-active['last_progress'])/1000;elapsed=now-active['last_seen']
                listened=active['listened']+(min(delta,elapsed) if elapsed<30 and delta<=elapsed+3 else 0)
                duration=item.get('duration_ms') or 0;threshold=min(30,duration/2000) if duration else 30
                self.db.execute('UPDATE plays SET listened=?,qualified=?,last_progress=?,last_seen=? WHERE id=?',
                    (listened,int(listened>=threshold),progress,now,active['id']))
        elif active:self.db.execute('UPDATE plays SET last_seen=?,last_progress=? WHERE id=?',(now,progress,active['id']))
        self.db.commit();self.reserve_next()
