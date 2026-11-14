import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import Mock, patch

import foxtunes as f
import player_health
import web_server as w
import test_mixed_playback as fixtures

A='spotify:track:'+'c'*22


class QueueCompletionTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.path=Path(self.temp.name);self.events=[]
        self.spotify=fixtures.Spotify(self.events);self.app=w.App(self.path,self.spotify)
        self.app.local=fixtures.MPD(self.events,self.spotify)
        self.app.device='speaker';self.app.enabled=True
        self.sid,self.session=self.app.session('');self.session['host']=True
        self.app.catalog[fixtures.SPOTIFY]=w.track(self.spotify.state['item'])
        self.app.catalog[A]=dict(w.track(self.spotify.state['item']),uri=A,name='Requested song')
        self.spotify.state['progress_ms']=1000
    def tearDown(self):self.app.db.close();self.temp.cleanup()
    def owned(self,uri=fixtures.SPOTIFY):
        self.app.submit(self.sid,uri);row=next(r for r in self.app.queue('') if r['uri']==uri)
        self.app.record_attempt(row,'direct')
        self.app.db.execute("UPDATE songs SET state='accepted' WHERE id=?",(row['id'],));self.app.db.commit()
        self.spotify.state['item']=dict(self.spotify.state['item'],uri=uri)
        self.spotify.state['progress_ms']=0
        self.app.confirm_attempt(self.spotify.state,dict(row,state='accepted'))
        self.app.observe_play(self.spotify.state)
        return row

    def test_first_spotify_request_interrupts_background_immediately(self):
        self.app.submit(self.sid,A);self.app.poll(True)
        self.assertTrue(any(e[:3]==('spotify','/me/player/play','PUT') for e in self.events))
        self.assertFalse(any(e[:3]==('spotify','/me/player/queue','POST') for e in self.events))
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],0)
        self.app.poll(True)
        self.assertEqual(self.app.db.execute('SELECT song FROM plays').fetchone()[0],1)

    def test_first_local_request_interrupts_background_without_overlap(self):
        self.app.local_config.update(enabled=True,device='speaker')
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(fixtures.LOCAL,json.dumps(fixtures.SONG)));self.app.db.commit()
        self.app.submit(self.sid,fixtures.LOCAL);self.app.poll(True)
        self.assertIn(('mpd','play'),self.events)
        self.assertFalse(self.spotify.state['is_playing'])

    def test_actual_requested_track_is_not_interrupted(self):
        self.owned();self.events.clear();self.spotify.state['progress_ms']=1000
        self.app.submit(self.sid,A);self.app.poll(True)
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))

    def test_stopped_paused_and_other_devices_are_not_taken_over(self):
        self.app.submit(self.sid,A);self.app.enabled=False;self.app.poll(True)
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))
        self.app.enabled=True;self.spotify.state['is_playing']=False;self.app.poll(True)
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))
        self.app.enabled=True;self.spotify.state.update(is_playing=True,device={'id':'other'});self.app.poll(True)
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))

    def test_matching_background_uri_without_dispatch_does_not_count(self):
        self.app.submit(self.sid,fixtures.SPOTIFY)
        self.app.observe_play(self.spotify.state)
        self.app.db.execute("UPDATE songs SET state='playing'");self.app.db.commit()
        self.app.observe_play(self.spotify.state)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],0)

    def test_uncertain_and_old_background_replays_never_count(self):
        row=self.owned();self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],1)
        self.spotify.state['item']['uri']=A;self.app.poll(True)
        self.spotify.state['item']['uri']=fixtures.SPOTIFY;self.app.poll(True)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],1)
        self.app.submit(self.sid,A);new=next(r for r in self.app.queue('') if r['uri']==A)
        self.app.record_attempt(new,'direct');self.app.db.execute("UPDATE songs SET state='uncertain' WHERE id=?",(new['id'],));self.app.db.commit()
        self.spotify.state['item']['uri']=A;self.app.poll(True)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],1)

    def test_likes_are_one_per_guest_and_separate_from_votes(self):
        row=self.owned();votes=self.app.db.execute('SELECT count(*) FROM votes').fetchone()[0]
        for value in ['like','like','fire','great-pick']:self.app.action(self.sid,'/api/react',{'reaction':value})
        self.assertEqual(self.app.db.execute('SELECT reaction FROM reactions').fetchone()[0],'like')
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM reactions').fetchone()[0],1)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM votes').fetchone()[0],votes)
        self.assertTrue(self.app.room_state(self.sid)['liked'])

    def test_reset_backs_up_history_and_preserves_other_data(self):
        self.owned();self.app.action(self.sid,'/api/react',{'reaction':'like'})
        self.app.action(self.sid,'/api/host/favorite',{'uri':fixtures.SPOTIFY})
        self.app.action(self.sid,'/api/profile',{'name':'Nate'})
        self.app.submit(self.sid,A)
        tables=['songs','votes','favorites','guests','request_attempts']
        before={t:self.app.db.execute('SELECT count(*) FROM '+t).fetchone()[0] for t in tables}
        result=self.app.action(self.sid,'/api/host/history-reset',{})
        backup=self.path/result['backup'];self.assertTrue(backup.exists());self.assertEqual(backup.stat().st_mode&0o777,0o600)
        with sqlite3.connect(backup) as db:self.assertEqual(db.execute('SELECT count(*) FROM plays').fetchone()[0],1)
        for table,count in before.items():self.assertEqual(self.app.db.execute('SELECT count(*) FROM '+table).fetchone()[0],count)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],0)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM reactions').fetchone()[0],0)

    def test_midtrack_seek_does_not_retire_requested_track(self):
        row=self.owned()
        self.spotify.state['progress_ms']=30000;self.app.observe_play(self.spotify.state)
        self.spotify.state['progress_ms']=5000;self.app.observe_play(self.spotify.state)
        self.assertEqual(self.app.db.execute('SELECT state FROM songs WHERE id=?',(row['id'],)).fetchone()[0],'playing')
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],1)

    def test_restart_resumes_owned_observation_without_duplicate(self):
        self.owned();f.private_write(self.path/'settings.json',{'device':'speaker','accepting':True})
        self.app.db.close();self.app=w.App(self.path,self.spotify)
        self.app.poll(True)
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],1)
        self.assertFalse(self.app.enabled)

    def test_unattributed_rows_do_not_reach_any_play_statistics(self):
        import time
        now=time.time()
        self.app.db.execute("INSERT INTO plays(uri,metadata,started,last_seen,qualified,outcome) VALUES(?,?,?,?,1,'unknown')",(fixtures.SPOTIFY,json.dumps(self.app.catalog[fixtures.SPOTIFY]),now,now));self.app.db.commit()
        self.app.action(self.sid,'/api/host/favorite',{'uri':fixtures.SPOTIFY})
        stats=self.app.stats('all',self.sid)
        self.assertEqual(stats['top'],[]);self.assertEqual(stats['recent'],[]);self.assertEqual(stats['recap']['plays'],0)
        self.assertEqual(self.app.favorites()[0]['plays'],0)

    def test_upgrade_cleanup_is_once_and_holds_unverifiable_handoffs(self):
        import time
        self.app.submit(self.sid,A)
        self.app.db.execute("UPDATE songs SET state='accepted'")
        self.app.db.execute("INSERT INTO plays(uri,metadata,started,last_seen) VALUES(?,?,?,?)",(A,json.dumps(self.app.catalog[A]),time.time(),time.time()))
        self.app.db.execute("DELETE FROM room_meta WHERE key='requested_history_v1'");self.app.db.commit();self.app.db.close()
        self.app=w.App(self.path,self.spotify)
        self.assertEqual(self.app.db.execute('SELECT state FROM songs').fetchone()[0],'uncertain')
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM plays').fetchone()[0],0)
        self.assertEqual(len(list(self.path.glob('web-history-backup-*.sqlite3'))),1)
        self.app.db.close();self.app=w.App(self.path,self.spotify)
        self.assertEqual(len(list(self.path.glob('web-history-backup-*.sqlite3'))),1)


class RecoveryTests(unittest.TestCase):
    def test_diagnostics_never_return_raw_logs_or_command_secrets(self):
        service=Mock(stdout='ActiveState=active\nSubState=running\nNRestarts=2\nExecStart=spak_secret\n',returncode=0)
        journal=Mock(stdout='network unreachable access_token=secret\nlogin failed spak_private\n',returncode=0)
        with patch('player_health.command',side_effect=[service,journal,Mock(stdout='UNCONN socket users:((\"soloist\",pid=123))\n',returncode=0),Mock(stdout='active\n',returncode=0)]):result=player_health.soloist_status()
        encoded=json.dumps(result);self.assertNotIn('secret',encoded);self.assertNotIn('spak_',encoded)
        self.assertEqual(result['recent_log_counts']['network'],1);self.assertEqual(result['service']['ActiveState'],'active')

    def test_restart_uses_only_fixed_service(self):
        with patch('player_health.command',return_value=Mock(returncode=0,stdout='')),patch('player_health.soloist_status',return_value={'service':{'ActiveState':'active'}}):
            self.assertTrue(player_health.restart_soloist()['restarted'])

    def test_host_recovery_stops_handoff_and_refuses_local_or_uncertain(self):
        with tempfile.TemporaryDirectory() as temp:
            app=w.App(Path(temp),Mock());sid,session=app.session('');session['host']=True
            try:
                app.device='speaker';app.local_config.update(enabled=True,device='speaker');app.local=Mock()
                app.local_active={'id':1}
                with self.assertRaises(f.Failure):app.action(sid,'/api/host/player-restart',{})
                app.local.call.assert_not_called();app.local_active=None
                app.enabled=True;app.local.call.return_value={'restarted':True}
                self.assertTrue(app.action(sid,'/api/host/player-restart',{})['restarted']);self.assertFalse(app.enabled)
                guest,_=app.session('')
                with self.assertRaises(PermissionError):app.action(guest,'/api/host/player-restart',{})
            finally:app.db.close()
