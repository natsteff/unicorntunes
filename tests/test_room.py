import copy
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.request
from unittest.mock import patch

import foxtunes as f
import web_server as w
from room import COPY, SHY
from test_web import FakeSpotify, A, B, C


class RoomTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.api = FakeSpotify()
        self.app = w.App(self.path, self.api)
        self.sids = [self.app.session('')[0] for _ in range(4)]
        self.host = self.sids[0]
        self.app.sessions[self.host]['host'] = True
        for index, uri in enumerate((A, B, C, 'spotify:track:'+'d'*22, 'spotify:track:'+'e'*22, 'spotify:track:'+'f'*22)):
            self.app.catalog[uri] = {'uri': uri, 'name': 'Song '+str(index), 'artists': 'Artist', 'artist_names': ['Artist'], 'duration_ms': 100000, 'explicit': False}

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def owned_state(self, uri=A):
        self.app.device='speaker'
        self.app.submit(self.sids[1],uri)
        row=next(r for r in self.app.queue('') if r['uri']==uri)
        self.app.record_attempt(row,'direct')
        self.app.db.execute("UPDATE songs SET state='accepted' WHERE id=?",(row['id'],));self.app.db.commit()
        state=copy.deepcopy(self.api.state)
        state['item']=dict(self.app.catalog[uri],artists=[{'name':'Artist'}]);state['progress_ms']=0
        self.app.confirm_attempt(state,dict(row,state='accepted'))
        return state

    def play_selection(self):
        chosen = next(x for x in self.app.queue('') if x['state']=='waiting')
        self.app.commit_selection(chosen['id'])
        self.app.db.execute("UPDATE songs SET state='past' WHERE id=?", (chosen['id'],))
        self.app.db.commit()
        self.app.reserve_next()
        return chosen

    def test_initial_vote_ownership_and_distinct_additional_names(self):
        self.app.room_action(self.sids[1], '/api/profile', {'name': 'Nate'})
        self.app.submit(self.sids[1], A)
        self.app.vote(self.sids[1], 1)
        self.assertEqual(self.app.queue(self.sids[1])[0]['votes'], 1)
        self.assertTrue(self.app.queue(self.sids[1])[0]['voted'])
        self.assertTrue(self.app.queue(self.sids[1])[0]['mine'])
        self.app.room_action(self.sids[2], '/api/profile', {'name': 'Sam'})
        self.app.vote(self.sids[2], 1)
        song = self.app.queue(self.sids[2])[0]
        self.assertEqual((song['requested_by'], song['upvoted_by'], song['votes']), ('Nate', ['Sam'], 2))
        self.assertFalse(song['mine'])

    def test_guest_identity_survives_app_restart_without_host_auth(self):
        token = self.app.attach_guest(self.host, '')
        self.app.room_action(self.host, '/api/profile', {'name': 'Nate'})
        self.app.submit(self.host, A)
        other = w.App(self.path, self.api)
        try:
            sid, session = other.session('')
            self.assertEqual(other.attach_guest(sid, token), token)
            self.assertFalse(session['host'])
            self.assertEqual(other.room_state(sid)['profile']['name'], 'Nate')
            other.vote(sid, 1)
            self.assertEqual(other.queue(sid)[0]['votes'], 1)
            self.assertTrue(other.queue(sid)[0]['mine'])
            self.assertFalse(other.enabled)
        finally:
            other.db.close()

    def test_shared_tablet_switch_is_distinct_guest(self):
        self.app.attach_guest(self.host, '')
        original = self.app.guest_id(self.host)
        self.app.submit(self.host, A)
        result = self.app.room_action(self.host, '/api/guest/switch', {})
        self.assertIn('_guest_token', result)
        self.assertNotEqual(original, self.app.guest_id(self.host))
        self.assertFalse(self.app.queue(self.host)[0]['mine'])
        self.assertFalse(self.app.room_state(self.host)['profile']['named'])

    def test_guaranteed_first_turn_and_every_third_oldest(self):
        uris = list(self.app.catalog)
        self.app.submit(self.sids[1], uris[0])
        self.app.submit(self.sids[1], uris[1])  # oldest second request
        self.app.submit(self.sids[2], uris[2])  # another guest's protected turn
        self.app.submit(self.sids[2], uris[3])
        self.app.vote(self.sids[3], 4)
        self.app.vote(self.host, 4)
        # Votes cannot bump A or the next guest's protected first turn.
        self.assertEqual(self.play_selection()['uri'], uris[0])
        self.assertEqual(self.play_selection()['uri'], uris[2])
        self.assertEqual(self.play_selection()['uri'], uris[1])  # third = oldest
        self.assertEqual(self.play_selection()['uri'], uris[3])
        self.assertEqual(self.app.meta('selection_count'), 4)

    def test_reservation_survives_restart_and_host_removal(self):
        self.app.submit(self.sids[1], A)
        self.app.submit(self.sids[2], B)
        self.app.vote(self.host, 2)
        self.assertEqual(self.app.queue('')[0]['uri'], A)
        other = w.App(self.path, self.api)
        try:
            self.assertEqual(other.queue('')[0]['uri'], A)
        finally:
            other.db.close()
        self.app.action(self.host, '/api/host/remove', {'id': 1})
        self.assertEqual(self.app.queue('')[0]['uri'], B)
        self.assertTrue(self.app.queue('')[0]['protected'])

    def test_favorite_sort_and_requests_enforce_host_rules(self):
        self.app.action(self.host, '/api/host/favorite', {'uri': A})
        self.app.action(self.host, '/api/host/favorite', {'uri': A})
        self.assertEqual(len(self.app.favorites()), 1)
        self.assertFalse(self.app.queue(''))
        self.app.action(self.host, '/api/host/config', {'blocked_artists': ['Artist']})
        with self.assertRaises(f.Failure):
            self.app.submit(self.sids[1], A)
        self.app.action(self.host, '/api/host/config', {'blocked_artists': [], 'max_minutes': 1})
        with self.assertRaises(f.Failure):
            self.app.submit(self.sids[1], A)
        self.app.action(self.host, '/api/host/config', {'max_minutes': 0, 'block_explicit': True})
        self.app.catalog[A]['explicit'] = None
        with self.assertRaises(f.Failure):
            self.app.submit(self.sids[1], A)

    def test_guests_cannot_change_any_host_features(self):
        for route in ('config', 'favorite', 'gathering', 'location'):
            with self.assertRaises(PermissionError):
                self.app.action(self.sids[1], '/api/host/'+route, {'uri': A})
        self.assertEqual(self.app.room['copy'], COPY)

    def test_config_persists_defaults_and_validates_atomically(self):
        self.app.action(self.host, '/api/host/config', {'copy': {'headline': '<script>plain text</script>'}, 'theme': 'Metal night'})
        other = w.App(self.path, self.api)
        try:
            self.assertEqual(other.room['copy']['headline'], '<script>plain text</script>')
        finally:
            other.db.close()
        before = copy.deepcopy(self.app.room)
        with self.assertRaises(f.Failure):
            self.app.action(self.host, '/api/host/config', {'theme': 'Lost', 'max_minutes': -1})
        self.assertEqual(self.app.room, before)
        self.app.action(self.host, '/api/host/config', {'defaults': True})
        self.assertEqual(self.app.room['copy'], COPY)
        self.assertEqual(self.app.room['theme'], 'Metal night')

    def test_new_gathering_resets_guarantee_retains_history_and_holds_uncertain(self):
        self.app.submit(self.sids[1], A)
        self.app.action(self.host, '/api/host/gathering', {})
        self.assertEqual(self.app.meta('gathering'), 2)
        self.assertFalse(self.app.queue(''))
        self.app.submit(self.sids[1], B)
        self.assertTrue(self.app.queue('')[0]['guaranteed'])
        self.app.db.execute("UPDATE songs SET state='uncertain' WHERE id=2")
        with self.assertRaises(f.Failure):
            self.app.action(self.host, '/api/host/gathering', {})

    def test_play_counts_require_observed_progress_and_ignore_seeks(self):
        self.app.device = 'speaker'
        state = self.owned_state(A)
        state['progress_ms'] = 0
        with patch('room.time.time', return_value=1000): self.app.observe_play(state)
        state['progress_ms'] = 90000
        with patch('room.time.time', return_value=1005): self.app.observe_play(state)
        self.assertEqual(self.app.db.execute('SELECT qualified FROM plays').fetchone()[0], 0)
        state['progress_ms'] = 95000
        with patch('room.time.time', return_value=1010): self.app.observe_play(state)
        self.assertEqual(self.app.db.execute('SELECT listened FROM plays').fetchone()[0], 5)
        state = self.owned_state(B)
        with patch('room.time.time', return_value=1015): self.app.observe_play(state, skipped=True)
        self.assertEqual(self.app.db.execute('SELECT outcome FROM plays WHERE id=1').fetchone()[0], 'skipped')
        for i in range(1, 7):
            state['progress_ms'] = i*5000
            with patch('room.time.time', return_value=1015+i*5): self.app.observe_play(state)
        self.assertEqual(self.app.db.execute('SELECT qualified FROM plays WHERE id=2').fetchone()[0], 1)
        stats = self.app.stats('all', self.sids[1])
        self.assertEqual(stats['top'][0]['uri'], B)
        self.assertEqual(stats['recap']['plays'], 1)

    def test_observation_gap_and_other_device_do_not_count(self):
        self.app.device = 'speaker'
        state = self.owned_state(A)
        with patch('room.time.time', return_value=100): self.app.observe_play(state)
        state['progress_ms']=60000
        with patch('room.time.time', return_value=160): self.app.observe_play(state)
        self.assertEqual(self.app.db.execute('SELECT listened FROM plays').fetchone()[0], 0)
        state['device']['id']='elsewhere'
        self.app.observe_play(state)
        self.assertEqual(self.app.db.execute('SELECT outcome FROM plays').fetchone()[0], 'unknown')

    def test_upcoming_shared_cache_not_per_guest(self):
        self.app.refresh_upcoming()
        self.app.refresh_upcoming()
        self.assertEqual(len([c for c in self.api.calls if c[0]=='/me/player/queue']), 1)
        self.assertEqual(self.app.spotify_upcoming['items'], [])

    def test_http_guest_cookie_profile_and_endpoints(self):
        server = w.make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True);thread.start()
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        jar = http.cookiejar.CookieJar()
        client = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
        try:
            def get(path):
                with client.open(base+path) as response:return json.load(response)
            state = get('/api/state')
            self.assertFalse(state['profile']['named'])
            req = urllib.request.Request(base+'/api/profile', data=json.dumps({'name':'<b>Nate</b>'}).encode(), headers={'Content-Type':'application/json','Origin':base,'X-FoxTunes-CSRF':state['csrf']})
            with client.open(req) as response:response.read()
            self.app.sessions.clear()
            state = get('/api/state')
            self.assertEqual(state['profile']['name'], '<b>Nate</b>')
            self.assertFalse(state['host'])
            self.assertEqual(get('/api/favorites'), {'items':[]})
            self.assertEqual(get('/api/stats?period=all')['top'], [])
            with client.open(base+'/display') as response:self.assertEqual(response.status,200)
        finally:
            server.shutdown();server.server_close();thread.join()

    def test_migration_backs_up_old_database_and_is_idempotent(self):
        import sqlite3
        with tempfile.TemporaryDirectory() as temp:
            path=Path(temp)
            db=sqlite3.connect(str(path/'web.sqlite3'))
            db.executescript("CREATE TABLE songs(id INTEGER PRIMARY KEY,guest TEXT,uri TEXT,metadata TEXT,state TEXT DEFAULT 'waiting',created REAL,detail TEXT DEFAULT ''); CREATE TABLE votes(song INTEGER,guest TEXT,PRIMARY KEY(song,guest));")
            db.execute('INSERT INTO songs(guest,uri,metadata,created) VALUES(?,?,?,?)', ('old', A,json.dumps(self.app.catalog[A]),time.time()))
            db.commit();db.close()
            app=w.App(path,self.api)
            self.assertTrue((path/'web-pre-0.2.0.sqlite3').exists())
            self.assertEqual(app.queue('old')[0]['votes'],1)
            app.db.close()
            app=w.App(path,self.api)
            self.assertEqual(app.queue('old')[0]['votes'],1)
            app.db.close()

    def test_rule_change_blocks_dispatch_without_losing_protected_track(self):
        self.app.submit(self.sids[1],A)
        self.app.action(self.host,'/api/host/config',{'blocked_tracks':['Song 0']})
        self.app.device,self.app.enabled='speaker',True
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.meta('selection_count',0),0)
        self.assertEqual(self.app.queue('')[0]['uri'],A)
        self.assertFalse([x for x in self.api.calls if x[1]!='GET'])

    def test_duplicate_display_names_combine_and_unnamed_excluded(self):
        for sid,uri in zip(self.sids[1:3],(A,B)):
            self.app.room_action(sid,'/api/profile',{'name':'Nate'})
            self.app.submit(sid,uri)
        self.app.submit(self.sids[3],C)
        for song in (1,2,3):self.app.vote(self.host,song)
        stats=self.app.stats('all',self.host)
        self.assertEqual(stats['users'],[{'name':'Nate','plays':0,'votes':2}])
        self.assertEqual(len(stats['votes']),3)
        self.assertEqual(len(stats['crowd']),3)

    def test_counted_favorites_sort_and_repeat_cooldown(self):
        for uri in (A,B):self.app.action(self.host,'/api/host/favorite',{'uri':uri})
        state=self.owned_state(B)
        self.app.observe_play(state)
        self.app.db.execute("UPDATE plays SET qualified=1,outcome='unknown' WHERE uri=?",(B,))
        self.app.db.commit()
        self.assertEqual(self.app.favorites()[0]['uri'],B)
        self.assertEqual(self.app.favorites()[0]['plays'],1)
        self.app.action(self.host,'/api/host/config',{'repeat_minutes':60})
        with self.assertRaises(f.Failure):self.app.submit(self.sids[1],B)

    def test_reaction_is_one_per_guest_and_active_play_only(self):
        self.app.observe_play(self.owned_state(A))
        self.app.room_action(self.host,'/api/react',{'reaction':'love'})
        self.app.room_action(self.host,'/api/react',{'reaction':'fire'})
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM reactions').fetchone()[0],1)
        with self.assertRaises(f.Failure):self.app.room_action(self.host,'/api/react',{'reaction':'invalid'})

    def test_saved_location_requires_available_device(self):
        self.app.action(self.host,'/api/host/location',{'name':'Kitchen','device':'speaker'})
        self.assertEqual(self.app.room['locations']['Kitchen'],'speaker')
        with self.assertRaises(f.Failure):self.app.action(self.host,'/api/host/location',{'name':'Garage','device':'offline'})
        self.app.action(self.host,'/api/host/location',{'name':'Kitchen','remove':True})
        self.assertFalse(self.app.room['locations'])
