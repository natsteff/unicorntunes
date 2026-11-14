import json
from pathlib import Path
import tempfile
import time
import unittest
from unittest.mock import patch

import foxtunes as f
import web_server as w

LOCAL = 'local:' + 'a'*64
SPOTIFY = 'spotify:track:'+'b'*22
SONG = dict(uri=LOCAL,source='local',name='Local Song',artists='Local Artist',artist_names=['Local Artist'],album='Local Album',year='2020',duration_ms=60000,explicit=None,url='',art='')


class Spotify:
    def __init__(self, events):
        self.events=events
        self.state={'item':{'uri':SPOTIFY,'name':'Spotify Song','duration_ms':60000,'artists':[]},'device':{'id':'speaker'},'is_playing':True,'progress_ms':59500}
        self.fail_pause=False
        self.fail_search=False
    def call(self,path,method='GET',payload=None,params=None):
        self.events.append(('spotify',path,method,payload))
        if path=='/search':
            if self.fail_search:raise f.Failure('Spotify unavailable')
            return {'tracks':{'items':[dict(self.state['item'])],'next':None}}
        if path=='/me/player/devices':return {'devices':[{'id':'speaker'}]}
        if path.endswith('/pause'):
            if not self.fail_pause:self.state['is_playing']=False
            return {}
        if path.endswith('/play') and method=='PUT':
            uri=(payload or {}).get('uris',[SPOTIFY])[0]
            if uri.startswith('local:'):raise AssertionError('Local URI sent to Spotify')
            self.state.update(item={'uri':uri,'name':'Spotify Song','duration_ms':60000,'artists':[]},is_playing=True,progress_ms=0,device={'id':'speaker'})
            return {}
        if path=='/me/player' and method=='GET':return dict(self.state)
        return {}


class MPD:
    def __init__(self,events,spotify):
        self.events=events;self.spotify=spotify
        self.state={'state':'stop','uri':None,'song_id':None,'progress_ms':0}
        self.fail_play=False
    def call(self,action,**args):
        self.events.append(('mpd',action))
        if action=='status':return dict(self.state)
        if action=='play':
            if self.spotify.state['is_playing']:raise AssertionError('Both players active')
            self.state.update(state='play',uri=args['uri'],song_id='7',progress_ms=0)
            if self.fail_play:raise f.Failure('Reply lost',uncertain=True)
            return {'song_id':'7'}
        if action=='stop':self.state['state']='stop'
        if action=='pause':self.state['state']='pause'
        return {'ok':True}


class MixedPlaybackTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.events=[]
        self.spotify=Spotify(self.events)
        self.app=w.App(Path(self.temp.name),self.spotify)
        self.app.local_config.update(enabled=True,device='speaker')
        self.app.device='speaker';self.app.enabled=True
        self.mpd=MPD(self.events,self.spotify);self.app.local=self.mpd
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(LOCAL,json.dumps(SONG)));self.app.db.commit()
        self.sid,self.session=self.app.session('');self.session['host']=True
        self.app.catalog[SPOTIFY]=w.track(self.spotify.state['item'])
    def tearDown(self):self.app.db.close();self.temp.cleanup()
    def request(self,uri=LOCAL):
        self.app.submit(self.sid,uri)
        return next(r for r in self.app.queue(self.sid) if r['uri']==uri)
    def row(self,uri=LOCAL):return self.app.db.execute('SELECT * FROM songs WHERE uri=?',(uri,)).fetchone()
    def start(self):
        row=self.request();self.app.start_local_request(row,self.spotify.state);return row
    def finish(self):
        self.mpd.state['progress_ms']=57000;self.app.poll(force=True)
        self.mpd.state['state']='stop';self.app.poll(force=True)

    def test_search_both_and_local_only_browse(self):
        result=self.app.mixed_search({'q':['Song']})
        self.assertEqual([s['uri'] for s in result['items']],[LOCAL,SPOTIFY])
        self.events.clear()
        result=self.app.mixed_search({'source':['local']})
        self.assertEqual(result['items'][0]['source'],'local')
        self.assertFalse(any(e[0]=='spotify' for e in self.events))

    def test_filters_and_spotify_search_failure(self):
        self.assertEqual(self.app.mixed_search({'q':['Song'],'source':['local'],'year':['2021']})['items'],[])
        self.spotify.fail_search=True
        result=self.app.mixed_search({'q':['Song']})
        self.assertTrue(result['warning']);self.assertEqual(len(result['items']),1)
        with self.assertRaises(ValueError):self.app.mixed_search({'year':['bad']})

    def test_requests_start_with_vote_and_no_second_vote(self):
        row=self.request();self.app.vote(self.sid,row['id'])
        self.assertEqual(self.app.queue(self.sid)[0]['votes'],1)
        self.app.local_config['enabled']=False
        with self.assertRaises(f.Failure):self.app.submit(self.sid,'local:'+'c'*64)

    def test_spotify_paused_before_local_start(self):
        self.start()
        pause=next(i for i,e in enumerate(self.events) if e[:3]==('spotify','/me/player/pause','PUT'))
        play=self.events.index(('mpd','play'))
        self.assertLess(pause,play)
        self.app.poll(force=True)
        self.assertEqual(self.row()['state'],'playing')
        self.assertEqual(self.app.now['source'],'local')
        self.assertTrue(self.app.enabled)

    def test_unconfirmed_spotify_pause_prevents_local_start(self):
        row=self.request();self.spotify.fail_pause=True
        with patch('local_player.time.sleep'):
            with self.assertRaises(f.Failure):self.app.start_local_request(row,self.spotify.state)
        self.assertNotIn(('mpd','play'),self.events)
        self.assertEqual(self.row()['state'],'uncertain')

    def test_lost_play_response_is_held_without_replay(self):
        self.mpd.fail_play=True
        row=self.request()
        with self.assertRaises(f.Failure):self.app.start_local_request(row,self.spotify.state)
        self.app.poll(force=True);self.app.poll(force=True)
        self.assertEqual(self.events.count(('mpd','play')),1)
        self.assertEqual(self.row()['state'],'uncertain');self.assertFalse(self.app.enabled)

    def test_local_end_starts_next_spotify(self):
        self.start();self.request(SPOTIFY);self.finish()
        self.assertEqual(self.row()['state'],'past')
        self.assertEqual(self.row(SPOTIFY)['state'],'accepted')
        self.assertIsNone(self.app.local_active)
        self.assertTrue(self.spotify.state['is_playing'])
        self.app.poll(force=True)
        self.assertEqual(self.row(SPOTIFY)['state'],'playing')

    def test_empty_queue_then_new_local_request_starts(self):
        self.start();self.finish();self.assertTrue(self.app.idle_ready)
        self.app.poll(force=True);self.assertTrue(self.app.idle_ready)
        self.request();self.app.poll(force=True)
        self.assertEqual(self.events.count(('mpd','play')),2)

    def test_other_spotify_device_pauses_owned_mpd(self):
        self.start();self.spotify.state.update(device={'id':'other'},is_playing=True)
        self.app.poll(force=True)
        self.assertEqual(self.mpd.state['state'],'pause');self.assertFalse(self.app.enabled)

    def test_mpd_external_item_is_not_stopped(self):
        self.start();self.mpd.state.update(uri='local:'+'c'*64,song_id='8')
        self.app.poll(force=True)
        self.assertNotIn(('mpd','pause'),self.events);self.assertFalse(self.app.enabled)

    def test_manual_early_stop_does_not_advance(self):
        self.start();self.app.poll(force=True);self.request(SPOTIFY)
        self.mpd.state['state']='stop';self.app.poll(force=True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.row(SPOTIFY)['state'],'waiting')

    def test_pause_and_skip_control_correct_source(self):
        self.start();self.app.poll(force=True)
        self.app.action(self.sid,'/api/host/pause',{})
        self.assertEqual(self.mpd.state['state'],'pause');self.assertFalse(self.app.enabled)
        self.request(SPOTIFY);self.app.skip_request()
        self.assertEqual(self.mpd.state['state'],'stop');self.assertTrue(self.spotify.state['is_playing'])

    def test_next_local_waits_until_spotify_end(self):
        current=self.request(SPOTIFY)
        self.app.record_attempt(current,'direct')
        self.app.confirm_attempt(dict(self.spotify.state,progress_ms=0),dict(current,state='accepted'))
        self.request();self.spotify.state['progress_ms']=30000
        self.app.poll(force=True);self.assertNotIn(('mpd','play'),self.events)
        self.spotify.state['progress_ms']=59500;self.app.poll(force=True)
        self.assertNotIn(('mpd','play'),self.events)
        self.app.now=dict(w.track(self.spotify.state['item']),playing=True,progress_ms=59000)
        self.app.updated=time.time()-2
        self.spotify.state.update(is_playing=False,progress_ms=0)
        self.app.poll(force=True)
        self.assertIn(('mpd','play'),self.events)

    def test_spotify_natural_boundary_starts_local(self):
        self.request();self.app.now=dict(w.track(self.spotify.state['item']),playing=True,progress_ms=57000)
        self.app.updated=time.time()-4;self.app.observed_device='speaker'
        self.spotify.state['item']=dict(self.spotify.state['item'],uri='spotify:track:'+'d'*22)
        self.spotify.state['progress_ms']=1000
        self.app.poll(force=True);self.assertIn(('mpd','play'),self.events)

    def test_local_metadata_in_history_and_device_binding(self):
        self.start();self.app.poll(force=True)
        play=self.app.db.execute('SELECT metadata FROM plays WHERE uri=?',(LOCAL,)).fetchone()
        self.assertEqual(json.loads(play[0])['source'],'local')
        self.app.stop_local_owned();self.app.device='other'
        with self.assertRaises(f.Failure):self.app.submit(self.sid,LOCAL)

    def test_consecutive_local_requests_then_spotify(self):
        other=dict(SONG,uri='local:'+'c'*64,name='Second local')
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(other['uri'],json.dumps(other)));self.app.db.commit()
        self.start();self.request(other['uri']);self.finish()
        self.assertEqual(self.app.local_active['uri'],other['uri'])
        self.assertFalse(self.spotify.state['is_playing'])
        self.request(SPOTIFY);self.finish()
        self.assertTrue(self.spotify.state['is_playing'])

    def test_restart_preserves_local_ownership_but_disables_dispatch(self):
        self.start();f.private_write(Path(self.temp.name)/'settings.json',{'device':'speaker','accepting':True});self.app.db.close()
        self.app=w.App(Path(self.temp.name),self.spotify);self.app.local=self.mpd
        self.assertIsNotNone(self.app.local_active);self.assertFalse(self.app.enabled)
        self.app.poll(force=True)
        self.assertEqual(self.app.now['uri'],LOCAL);self.assertFalse(self.app.enabled)

    def test_uncertain_local_cannot_be_restarted_without_review(self):
        self.mpd.fail_play=True
        with self.assertRaises(f.Failure):self.start()
        with self.assertRaises(f.Failure):self.app.start_session('speaker',True)
        self.assertEqual(self.row()['state'],'uncertain')

    def test_reconcile_stops_local_before_releasing_request(self):
        self.mpd.fail_play=True
        with self.assertRaises(f.Failure):self.start()
        self.app.action(self.sid,'/api/host/reconcile',{'id':self.row()['id'],'note':'Checked the actual MPD player and stopped the uncertain track.'})
        self.assertEqual(self.mpd.state['state'],'stop');self.assertIsNone(self.app.local_active)
        self.assertEqual(self.row()['state'],'past')

    def test_idle_start_with_local_seed_never_transfers_spotify_play(self):
        self.spotify.state.update(is_playing=False,item=None)
        self.request();self.app.start_session('speaker',True)
        self.assertEqual(self.app.local_active['uri'],LOCAL);self.assertTrue(self.app.enabled)
        self.assertFalse(any(e[:3]==('spotify','/me/player','PUT') for e in self.events))

    def test_http_local_search_request_and_private_config_boundary(self):
        import http.cookiejar
        import threading
        import urllib.error
        import urllib.request
        server=w.make_server(self.app,port=0)
        thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
        base='http://127.0.0.1:'+str(server.server_address[1])
        client=urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
        def request(path,body=None,csrf=''):
            headers={'Content-Type':'application/json','Origin':base,'X-FoxTunes-CSRF':csrf}
            with client.open(urllib.request.Request(base+path,data=json.dumps(body).encode() if body is not None else None,headers=headers)) as response:return json.load(response)
        try:
            state=request('/api/state');self.assertIsNone(state['local_player'])
            result=request('/api/search?source=local')
            self.assertEqual(result['items'][0]['uri'],LOCAL)
            request('/api/request',{'uri':LOCAL},state['csrf'])
            queue=request('/api/state')['queue'];self.assertEqual(queue[0]['source'],'local');self.assertEqual(queue[0]['votes'],1)
            with self.assertRaises(urllib.error.HTTPError) as denied:request('/api/host/local-test',{},state['csrf'])
            self.assertEqual(denied.exception.code,403)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_external_stop_can_be_recovered_by_host_restart(self):
        self.start();self.app.poll(force=True);self.mpd.state['state']='stop';self.app.poll(force=True)
        self.app.stop_local_owned()
        self.assertIsNone(self.app.local_active)

    def test_bound_device_change_is_blocked_while_local_active(self):
        self.start()
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/local-config',{'enabled':False})

    def test_failed_spotify_poll_pauses_owned_local(self):
        self.start()
        original=self.spotify.call
        def failed(path,*args,**kwargs):
            if path=='/me/player':raise f.Failure('Connection lost')
            return original(path,*args,**kwargs)
        self.spotify.call=failed;self.app.poll(force=True)
        self.assertEqual(self.mpd.state['state'],'pause');self.assertFalse(self.app.enabled)

    def test_spotify_idle_start_refuses_external_mpd_audio(self):
        row=self.request(SPOTIFY);self.mpd.state['state']='play'
        with self.assertRaises(f.Failure):self.app.start_idle_request(row)
        self.assertFalse(any(e[:3]==('spotify','/me/player/play','PUT') for e in self.events))

    def test_wrong_device_start_does_not_stop_owned_local(self):
        self.start()
        with self.assertRaises(f.Failure):self.app.start_session('missing-device',True)
        self.assertEqual(self.mpd.state['state'],'play')
