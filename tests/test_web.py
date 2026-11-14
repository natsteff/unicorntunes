import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import foxtunes as f
import web_server as w

A, B, C = ['spotify:track:' + c * 22 for c in 'abc']


class FakeSpotify:
    def __init__(self):
        self.calls = []
        self.state = {'item': {'uri': 'spotify:track:'+'x'*22, 'duration_ms': 100000},
                      'device': {'id': 'speaker'}, 'progress_ms': 90000, 'is_playing': True}
        self.error = None

    def call(self, path, method='GET', payload=None, params=None):
        self.calls.append((path, method, params))
        if method != 'GET':
            if self.error:
                raise self.error
            return {}
        if path == '/me/player':
            return self.state
        if path == '/me/player/devices':
            return {'devices': [{'id': 'speaker', 'name': 'Test speaker', 'is_active': True}]}
        if path == '/search':
            return {'tracks': {'items': [{'uri': A, 'name': '<script>safe text</script>', 'artists': []}], 'next': 'next'}}
        return {}


class WebTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.api = FakeSpotify()
        self.app = w.App(Path(self.temp.name), self.api)
        self.sessions = [self.app.session('')[0] for _ in range(3)]
        for uri in (A, B, C):
            self.app.catalog[uri] = {'uri': uri, 'name': uri, 'artists': '', 'url': '', 'art': ''}

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def test_vote_order_ties_duplicate_vote_and_lock(self):
        for sid, uri in zip(self.sessions, (A, B, C)):
            self.app.submit(sid, uri)
        self.assertEqual([r['uri'] for r in self.app.queue('')], [A, B, C])
        self.app.vote(self.sessions[0], 3)
        self.app.vote(self.sessions[0], 3)
        self.assertEqual(self.app.queue('')[0]['uri'], A)
        self.assertEqual(next(r for r in self.app.queue('') if r['uri']==C)['votes'], 2)
        self.assertEqual(self.app.queue('')[0]['votes'], 1)
        self.app.vote(self.sessions[1], 2)
        self.assertEqual([r['uri'] for r in self.app.queue('')], [A, B, C])
        self.app.db.execute("UPDATE songs SET state='accepted' WHERE id=2")
        with self.assertRaises(f.Failure):
            self.app.vote(self.sessions[2], 2)

    def test_duplicate_and_three_request_cap(self):
        for uri in (A, B, C):
            self.app.submit(self.sessions[0], uri)
        with self.assertRaises(f.Failure):
            self.app.submit(self.sessions[1], A)
        extra = 'spotify:track:'+'d'*22
        self.app.catalog[extra] = {'uri': extra}
        with self.assertRaises(f.Failure):
            self.app.submit(self.sessions[0], extra)

    def test_guests_cannot_call_any_host_action(self):
        for route in ('devices', 'settings', 'pause', 'skip', 'remove', 'logout'):
            with self.assertRaises(PermissionError):
                self.app.action(self.sessions[0], '/api/host/'+route, {})
        self.assertFalse(self.api.calls)

    def test_scheduler_off_then_protected_next_only_and_no_retry(self):
        for sid, uri in zip(self.sessions, (A, B, C)):
            self.app.submit(sid, uri)
        self.app.vote(self.sessions[1], 3)
        self.app.set_meta('immediate_request',None)  # This case exercises near-end queue insertion.
        self.app.poll(True)
        self.assertFalse(any(c[1] != 'GET' for c in self.api.calls))
        self.app.device, self.app.enabled = 'speaker', True
        self.app.poll(True)
        writes = [c for c in self.api.calls if c[1] != 'GET']
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0][2]['uri'], A)
        self.app.poll(True)
        self.assertEqual(len([c for c in self.api.calls if c[1] != 'GET']), 1)
        self.assertEqual(self.app.queue('')[0]['state'], 'accepted')

    def test_uncertain_and_takeover_stop_worker(self):
        self.app.submit(self.sessions[0], A)
        self.app.enabled, self.app.device = True, 'speaker'
        self.api.error = f.Failure('timeout', uncertain=True)
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.queue('')[0]['state'], 'uncertain')
        self.app.enabled = True
        self.api.state['device']['id'] = 'other'
        self.app.poll(True)
        self.assertFalse(self.app.enabled)

    def test_paused_and_not_near_end_do_not_dispatch(self):
        self.app.submit(self.sessions[0], A)
        self.app.set_meta('immediate_request',None)  # Exercise an existing waiting queue, not first background arrival.
        self.app.enabled, self.app.device = True, 'speaker'
        self.api.state['progress_ms'] = 1000
        self.app.poll(True)
        self.api.state['is_playing'] = False
        self.api.state['progress_ms'] = 99000
        self.app.poll(True)
        self.assertFalse(any(c[1] != 'GET' for c in self.api.calls))

    def test_http_three_sessions_csrf_host_boundary_and_search_pages(self):
        server = w.make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        clients = [urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar())) for _ in range(3)]
        def request(client, path, body=None, csrf=None, origin=None):
            headers = {}
            if body is not None:
                headers = {'Content-Type': 'application/json', 'Origin': origin or base, 'X-FoxTunes-CSRF': csrf or ''}
            req = urllib.request.Request(base+path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
            with client.open(req) as response:
                return json.load(response)
        try:
            states = [request(c, '/api/state') for c in clients]
            self.assertEqual(len({s['csrf'] for s in states}), 3)
            with self.assertRaises(urllib.error.HTTPError) as bad:
                request(clients[0], '/api/request', {'uri': A}, states[0]['csrf'], 'http://evil.example')
            self.assertEqual(bad.exception.code, 403)
            for c, state, uri in zip(clients, states, (A, B, C)):
                request(c, '/api/request', {'uri': uri}, state['csrf'])
            request(clients[2], '/api/vote', {'id': 1}, states[2]['csrf'])
            with self.assertRaises(urllib.error.HTTPError) as forbidden:
                request(clients[0], '/api/host/skip', {}, states[0]['csrf'])
            self.assertEqual(forbidden.exception.code, 403)
            request(clients[0], '/api/search?q=test&offset=10')
            self.assertEqual(self.api.calls[-1][2]['offset'], 10)
            from urllib.parse import urlencode
            options = urlencode({'track': '<SCRIPT>safe text</SCRIPT>', 'artist': 'Example', 'year': '2000-2009', 'exact': '1'})
            exact = request(clients[0], '/api/search?' + options)
            self.assertEqual(len(exact['items']), 1)
            self.assertIn('year:2000-2009', self.api.calls[-1][2]['q'])
            no_match = request(clients[0], '/api/search?track=Different&exact=1')
            self.assertEqual(no_match['items'], [])
            self.assertTrue(no_match['more'])
            visible = request(clients[1], '/api/state')
            self.assertEqual(len(visible['queue']), 3)
            self.assertEqual(visible['queue'][0]['votes'], 2)
            encoded = json.dumps(visible)
            self.assertNotIn(self.app.password, encoded)
            self.assertNotIn('access_token', encoded)
            request(clients[0], '/api/host/login', {'password': self.app.password}, states[0]['csrf'])
            self.assertTrue(request(clients[0], '/api/state')['host'])
            self.assertFalse(request(clients[0], '/api/search?q=test')['items'][0]['house_favorite'])
            request(clients[0], '/api/host/favorite', {'uri': A}, states[0]['csrf'])
            self.assertTrue(request(clients[0], '/api/search?q=test')['items'][0]['house_favorite'])
            request(clients[0], '/api/host/favorite', {'uri': A, 'remove': True}, states[0]['csrf'])
            self.assertFalse(request(clients[0], '/api/search?q=test')['items'][0]['house_favorite'])
            self.assertFalse(request(clients[1], '/api/state')['host'])
        finally:
            server.shutdown()
            server.server_close()
            thread.join()


if __name__ == '__main__':
    unittest.main()

class PackagingTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name)
        self.api = FakeSpotify()
        self.app = w.App(self.path, self.api)
        self.sid, session = self.app.session('')
        session['host'] = True

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def test_failed_settings_preserve_running_configuration(self):
        self.app.device, self.app.enabled, self.app.accepting = 'speaker', True, True
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, '/api/host/settings', {'device': 'missing', 'enabled': True, 'accepting': False})
        self.assertEqual((self.app.device, self.app.enabled, self.app.accepting), ('speaker', True, True))
        self.api.state['device']['id'] = 'other'
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, '/api/host/settings', {'device': 'speaker', 'enabled': True, 'accepting': False})
        self.assertEqual((self.app.device, self.app.enabled, self.app.accepting), ('speaker', True, True))

    def test_success_persists_settings_but_restart_never_dispatches(self):
        self.app.action(self.sid, '/api/host/settings', {'device': 'speaker', 'enabled': True, 'accepting': False})
        self.assertTrue(self.app.enabled)
        other = w.App(self.path, self.api)
        self.assertEqual(other.device, 'speaker')
        self.assertFalse(other.accepting)
        self.assertFalse(other.enabled)
        other.db.close()

    def test_health_reason_survives_successful_reads(self):
        self.app.device, self.app.enabled = 'missing', True
        self.app.poll(True)
        reason = self.app.reason
        self.assertEqual(self.app.health()['status'], 'Needs attention')
        self.app.poll(True)
        self.assertEqual(self.app.reason, reason)
        self.assertEqual(self.app.health()['status'], 'Needs attention')

    def test_https_pkce_state_expiry_and_no_token_disclosure(self):
        from unittest.mock import patch
        import time
        from urllib.parse import urlsplit, parse_qs
        f.private_write(self.path / 'config.json', {'client_id': 'a'*32})
        self.app.public_url = 'https://foxtunes.example'
        session = self.app.sessions[self.sid]
        url = self.app.oauth_start(session)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query['redirect_uri'], ['https://foxtunes.example/callback'])
        with self.assertRaises(PermissionError):
            self.app.oauth_finish(session, {'state': ['bad'], 'code': ['code']})
        self.app.oauth_start(session)
        state = session['oauth']['state']
        with patch.object(f, 'request_json', return_value={'access_token': 'secret', 'refresh_token': 'refresh', 'expires_in': 3600}):
            self.app.oauth_finish(session, {'state': [state], 'code': ['code']})
        self.assertEqual(f.load(self.path / 'tokens.json')['access_token'], 'secret')
        with self.assertRaises(PermissionError):
            self.app.oauth_finish(session, {'state': [state], 'code': ['code']})
        self.app.oauth_start(session)
        session['oauth']['expires'] = time.time() - 1
        with self.assertRaises(PermissionError):
            self.app.oauth_finish(session, {'state': [session['oauth']['state']], 'code': ['code']})

    def test_http_hostname_oauth_requires_loopback_fallback(self):
        self.app.public_url = 'http://foxtunes.nate'
        with self.assertRaises(f.Failure):
            self.app.oauth_start(self.app.sessions[self.sid])

    def test_proxy_https_origin_and_cookie(self):
        self.app.public_url = 'https://foxtunes.example'
        server = w.make_server(self.app, port=0, allowed=['foxtunes.example'])
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:'+str(server.server_address[1])
        try:
            req = urllib.request.Request(base+'/api/state', headers={'Host':'foxtunes.example'})
            with urllib.request.urlopen(req) as response:
                state = json.load(response)
                cookie = response.headers['Set-Cookie']
                self.assertIn('Secure', cookie)
                self.assertIn('HttpOnly', cookie)
                self.assertIn('SameSite=Lax', cookie)
            headers={'Host':'foxtunes.example', 'Origin':'https://foxtunes.example',
                     'Cookie':cookie.split(';')[0], 'Content-Type':'application/json',
                     'X-FoxTunes-CSRF':state['csrf']}
            req=urllib.request.Request(base+'/api/host/login',data=json.dumps({'password':self.app.password}).encode(),headers=headers)
            with urllib.request.urlopen(req) as response:
                self.assertEqual(response.status,200)
            with urllib.request.urlopen(urllib.request.Request(base+'/healthz',headers={'Host':'foxtunes.example'})) as response:
                self.assertEqual(json.load(response),{'ok':True})
        finally:
            server.shutdown(); server.server_close(); thread.join()

class SessionStartTests(unittest.TestCase):
    setUp = WebTests.setUp
    tearDown = WebTests.tearDown

    def test_start_activates_without_other_spotify_app(self):
        from unittest.mock import patch
        session=self.app.sessions[self.sessions[0]]
        session['host']=True
        self.api.state['device']['id']='other'
        self.api.state['is_playing']=False
        original=self.api.call
        def call(path,method='GET',payload=None,params=None):
            result=original(path,method,payload,params)
            if path=='/me/player' and method=='PUT':
                self.api.state['device']['id']=payload['device_ids'][0]
            if path=='/me/player/play':
                self.api.state['is_playing']=True
            return result
        with patch.object(self.api,'call',side_effect=call):
            self.app.action(self.sessions[0],'/api/host/start',{'device':'speaker'})
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.device,'speaker')
        self.app.action(self.sessions[0],'/api/host/stop',{})
        self.assertFalse(self.app.enabled)

    def test_start_no_context_seeds_local_request(self):
        from unittest.mock import patch
        self.app.submit(self.sessions[0],A)
        self.api.state['item']=None
        self.api.state['is_playing']=False
        original=self.api.call
        def call(path,method='GET',payload=None,params=None):
            result=original(path,method,payload,params)
            if path=='/me/player/play':
                self.api.state['item']={'uri':payload['uris'][0],'duration_ms':100000}
                self.api.state['progress_ms']=0
                self.api.state['is_playing']=True
            return result
        with patch.object(self.api,'call',side_effect=call):
            self.app.start_session('speaker',True)
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.db.execute('SELECT state FROM songs').fetchone()[0],'playing')

    def test_transfer_not_observed_never_enables(self):
        from unittest.mock import patch
        self.api.state['device']['id']='other'
        with patch.object(w.time,'sleep'),self.assertRaises(f.Failure):
            self.app.start_session('speaker',True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.health()['status'],'Needs attention')

    def test_guest_start_and_stop_forbidden(self):
        for route in ('start','stop'):
            with self.assertRaises(PermissionError):
                self.app.action(self.sessions[0],'/api/host/'+route,{'device':'speaker'})
        self.assertFalse(self.api.calls)

    def test_mutation_failure_stops_without_retry(self):
        self.app.enabled=True
        self.api.error=f.Failure('timeout',uncertain=True)
        with self.assertRaises(f.Failure):
            self.app.start_session('speaker',True)
        self.assertFalse(self.app.enabled)
        self.assertEqual(len([c for c in self.api.calls if c[1]!='GET']),1)

class SkipRequestTests(unittest.TestCase):
    setUp = WebTests.setUp
    tearDown = WebTests.tearDown

    def prepare(self):
        self.app.device,self.app.enabled='speaker',True
        self.app.sessions[self.sessions[0]]['host']=True
        original=self.api.call
        def call(path,method='GET',payload=None,params=None):
            result=original(path,method,payload,params)
            if path=='/me/player/play':
                self.api.state['item']={'uri':payload['uris'][0],'duration_ms':100000}
                self.api.state['progress_ms']=0
            elif path=='/me/player/next':
                self.api.state['item']={'uri':getattr(self,'next_uri',C),'duration_ms':100000}
            return result
        return call

    def test_skip_respects_protected_next_and_keeps_running(self):
        from unittest.mock import patch
        for sid,uri in zip(self.sessions,(A,B,C)):self.app.submit(sid,uri)
        self.app.vote(self.sessions[1],2)
        with patch.object(self.api,'call',side_effect=self.prepare()):
            self.app.action(self.sessions[0],'/api/host/skip',{})
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.now['uri'],A)
        self.assertEqual(self.app.db.execute('SELECT state FROM songs WHERE id=1').fetchone()[0],'playing')
        writes=[c for c in self.api.calls if c[1]!='GET']
        self.assertEqual([c[0] for c in writes],['/me/player/play'])

    def test_already_handed_off_track_is_not_added_or_played_twice(self):
        from unittest.mock import patch
        self.app.submit(self.sessions[0],A)
        self.app.db.execute("UPDATE songs SET state='accepted'");self.app.db.commit()
        self.next_uri=A
        with patch.object(self.api,'call',side_effect=self.prepare()):self.app.skip_request()
        self.assertTrue(self.app.enabled)
        self.assertEqual([c[0] for c in self.api.calls if c[1]!='GET'],['/me/player/next'])

    def test_empty_queue_uses_normal_skip_and_keeps_running(self):
        from unittest.mock import patch
        with patch.object(self.api,'call',side_effect=self.prepare()):self.app.skip_request()
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.now['uri'],C)

    def test_timeout_marks_request_uncertain_and_does_not_retry(self):
        from unittest.mock import patch
        self.app.submit(self.sessions[0],A)
        call=self.prepare()
        self.api.error=f.Failure('timeout',uncertain=True)
        with patch.object(self.api,'call',side_effect=call),self.assertRaises(f.Failure):self.app.skip_request()
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.queue('')[0]['state'],'uncertain')
        writes=len([c for c in self.api.calls if c[1]!='GET'])
        with self.assertRaises(f.Failure):self.app.skip_request()
        self.assertEqual(len([c for c in self.api.calls if c[1]!='GET']),writes)

    def test_unconfirmed_track_change_stops(self):
        from unittest.mock import patch
        self.app.submit(self.sessions[0],A)
        self.app.device,self.app.enabled='speaker',True
        with patch.object(w.time,'sleep'),self.assertRaises(f.Failure):self.app.skip_request()
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.queue('')[0]['state'],'uncertain')

class ShareTests(unittest.TestCase):
    def test_share_uses_server_origin_without_host_path_or_credentials(self):
        with tempfile.TemporaryDirectory() as temp:
            app=w.App(Path(temp),FakeSpotify())
            app.public_url='http://foxtunes.nate'
            server=w.make_server(app,port=0)
            thread=threading.Thread(target=server.serve_forever,daemon=True);thread.start()
            base='http://127.0.0.1:'+str(server.server_address[1])
            try:
                with urllib.request.urlopen(base+'/api/share') as response:
                    payload=json.load(response)
                    self.assertEqual(payload,{'url':'http://foxtunes.nate/'})
                    self.assertNotIn(app.password,json.dumps(payload))
                with urllib.request.urlopen(base+'/vendor/qrcode.min.js') as response:
                    self.assertEqual(response.headers.get_content_type(),'text/javascript')
                    response.read()
                with urllib.request.urlopen(base+'/foxtunes-logo.png') as response:
                    self.assertEqual(response.headers.get_content_type(),'image/png')
                    response.read()
            finally:
                server.shutdown();server.server_close();thread.join();app.db.close()


class SearchOptionsTests(unittest.TestCase):
    def test_combined_filters_and_exact_title(self):
        query = {k: [v] for k, v in {'track': 'Through Struggle', 'artist': 'As I Lay Dying', 'album': 'Shadows Are Security', 'year': '2005', 'exact': '1'}.items()}
        text, exact = w.search_options(query)
        self.assertEqual(text, 'track:"Through Struggle" artist:"As I Lay Dying" album:"Shadows Are Security" year:2005')
        self.assertEqual(exact, 'through struggle')

    def test_invalid_filters(self):
        for query in ({}, {'year': ['2009-2000']}, {'year': ['20xx']}, {'exact': ['1'], 'q': ['song']}, {'artist': ['bad"field']}, {'q': ['x' * 151]}):
            with self.subTest(query=query), self.assertRaises(ValueError):
                w.search_options(query)

    def test_album_metadata_and_missing_album(self):
        song = w.track({'album': {'name': 'Example', 'release_date': '2005-06-14'}})
        self.assertEqual((song['album'], song['year']), ('Example', '2005'))
        self.assertEqual(w.track({})['year'], '')
