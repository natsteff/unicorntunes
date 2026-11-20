"""The isolated kiosk uses existing guest APIs and authorization boundaries."""
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import unittest
import urllib.error
import urllib.request

import web_server as w
from test_web import FakeSpotify, A


class LegacyTests(unittest.TestCase):
    def test_kiosk_routes_shared_queue_guest_switch_and_host_boundary(self):
        with tempfile.TemporaryDirectory() as directory:
            app = w.App(Path(directory), FakeSpotify())
            server = w.make_server(app, port=0)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = 'http://127.0.0.1:' + str(server.server_address[1])
            kiosk = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
            phone = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))

            def api(client, path, body=None, csrf=''):
                headers = {'Content-Type': 'application/json', 'Origin': base, 'X-FoxTunes-CSRF': csrf}
                request = urllib.request.Request(base + path, headers=headers,
                    data=json.dumps(body).encode() if body is not None else None)
                with client.open(request) as response:
                    return json.load(response)

            try:
                for path, mime in (('/legacy', 'text/html'), ('/legacy/', 'text/html'),
                                   ('/legacy.js', 'text/javascript'), ('/legacy.css', 'text/css'), ('/kiosk.js', 'text/javascript')):
                    with kiosk.open(base + path) as response:
                        self.assertEqual(response.status, 200)
                        self.assertIn(mime, response.headers['Content-Type'])
                        raw = response.read().decode()
                        if mime == 'text/html':
                            self.assertIn('apple-mobile-web-app-capable', raw)
                            self.assertNotIn('/app.js', raw)
                            self.assertNotIn('<dialog', raw)
                with phone.open(base + '/') as response:
                    self.assertIn('/app.js', response.read().decode())
                state = api(kiosk, '/api/state')
                csrf = state['csrf']
                api(kiosk, '/api/guest/switch', {'kiosk': True}, csrf)
                self.assertEqual(api(kiosk, '/api/state')['profile'], {'name': 'Kiosk User', 'named': False})
                api(kiosk, '/api/profile', {'name': 'Kiosk guest'}, csrf)
                found = api(kiosk, '/api/search?q=test&source=spotify&offset=0')
                self.assertEqual(found['items'][0]['uri'], A)
                api(kiosk, '/api/request', {'uri': A}, csrf)
                visible = api(phone, '/api/state')
                self.assertEqual(visible['queue'][0]['requested_by'], 'Kiosk guest')
                api(phone, '/api/vote', {'id': visible['queue'][0]['id']}, visible['csrf'])
                self.assertEqual(api(kiosk, '/api/state')['queue'][0]['votes'], 2)
                api(kiosk, '/api/guest/switch', {}, csrf)
                switched = api(kiosk, '/api/state')
                self.assertFalse(switched['profile']['named'])
                self.assertFalse(switched['queue'][0]['mine'])
                self.assertFalse(switched['queue'][0]['voted'])
                with self.assertRaises(urllib.error.HTTPError) as forbidden:
                    api(kiosk, '/api/host/start', {'device': 'speaker'}, csrf)
                self.assertEqual(forbidden.exception.code, 403)
                # Expiring a browser session preserves queued requests and music state.
                sid = next(key for key, session in app.sessions.items() if session['csrf'] == csrf)
                app.sessions[sid]['expires'] = 0
                app.enabled = True
                recovered = api(kiosk, '/api/state')
                self.assertNotEqual(recovered['csrf'], csrf)
                self.assertEqual(len(recovered['queue']), 1)
                self.assertTrue(app.enabled)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()
                app.db.close()
