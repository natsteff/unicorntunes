"""Browser capacity must reflect visitors, not Docker health checks or static assets."""
import http.cookiejar
import json
from pathlib import Path
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request
from unittest.mock import patch

import foxtunes as f
import web_server as w
from test_web import FakeSpotify, A, B


class SessionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = w.App(Path(self.temp.name), FakeSpotify())

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def test_idle_cleanup_preserves_host_music_queue_identity_and_limits(self):
        now = time.time()
        with patch('web_server.time.time', return_value=now):
            guest, session = self.app.session('')
            token = self.app.attach_guest(guest, '')
            self.app.action(guest, '/api/profile', {'name': 'Returning visitor'})
            self.app.catalog[A] = {'uri': A, 'name': 'Song'}
            self.app.submit(guest, A)
            self.app.limit(('search', guest), 15)
            host, host_session = self.app.session('')
            host_session['host'] = True
            host_csrf = host_session['csrf']
        self.app.enabled, self.app.device = True, 'speaker'
        with patch('web_server.time.time', return_value=now + 31 * 60):
            sid, recovered = self.app.session(guest)
            self.assertNotEqual(sid, guest)
            self.assertNotIn(('search', guest), self.app.limits)
            self.app.attach_guest(sid, token)
            self.assertEqual(self.app.room_state(sid)['profile']['name'], 'Returning visitor')
            self.assertTrue(self.app.queue(sid)[0]['mine'])
            self.assertEqual(self.app.queue(sid)[0]['votes'], 1)
            self.assertEqual(self.app.session(host)[1]['csrf'], host_csrf)
            self.assertTrue(self.app.session(host)[1]['host'])
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.device, 'speaker')
        self.assertFalse(self.app.api.calls)
        with patch('web_server.time.time', return_value=now + 8 * 3600):
            with self.assertRaisesRegex(PermissionError, 'expired'):
                self.app.session(host, create=False)
        self.assertTrue(self.app.enabled)

    def test_active_guests_keep_sessions_but_absolute_expiry_still_applies(self):
        start = time.time()
        with patch('web_server.time.time', return_value=start):
            sid, session = self.app.session('')
            csrf = session['csrf']
        for minute in range(20, 480, 20):
            with patch('web_server.time.time', return_value=start + minute * 60):
                self.assertEqual(self.app.session(sid)[1]['csrf'], csrf)
        with patch('web_server.time.time', return_value=start + 8 * 3600):
            with self.assertRaisesRegex(PermissionError, 'expired'):
                self.app.session(sid, create=False)
            self.assertNotEqual(self.app.session(sid)[0], sid)

    def test_capacity_reclaims_only_inactive_guests_and_keeps_existing_active_sessions(self):
        now = time.time()
        for n in range(500):
            self.app.sessions[str(n)] = {'csrf': str(n), 'host': n == 0,
                'expires': now + 8 * 3600, 'seen': now}
        self.app.sessions['0']['seen'] = now - 10 * 60  # inactive host is protected
        self.app.sessions['1']['seen'] = now - 6 * 60
        self.app.sessions['2']['seen'] = now - 4 * 60
        with patch('web_server.time.time', return_value=now):
            sid, _ = self.app.session('new')
            self.assertNotIn('1', self.app.sessions)
            self.assertIn('0', self.app.sessions)
            self.assertIn('2', self.app.sessions)
            self.assertEqual(len(self.app.sessions), 500)
            self.assertEqual(self.app.session('2')[1]['csrf'], '2')
            with self.assertRaisesRegex(f.Failure, 'capacity'):
                self.app.session('another')
            self.assertIn(sid, self.app.sessions)

    def test_cookie_free_health_static_public_unknown_and_post_do_not_allocate(self):
        server = w.make_server(self.app, port=0)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        base = 'http://127.0.0.1:' + str(server.server_address[1])
        try:
            paths = ['/healthz', '/', '/display', '/legacy', '/legacy/', '/legacy.js', '/kiosk.js',
                     '/legacy.css', '/api/share', '/api/playlists', '/api/favorites']
            for _ in range(50):
                for path in paths:
                    with urllib.request.urlopen(base + path) as response:
                        self.assertIsNone(response.headers.get('Set-Cookie'))
                        response.read()
            self.assertEqual(self.app.sessions, {})
            self.assertEqual(self.app.db.execute('SELECT count(*) FROM guests').fetchone()[0], 0)
            with self.assertRaises(urllib.error.HTTPError) as missing:
                urllib.request.urlopen(base + '/unknown')
            self.assertEqual(missing.exception.code, 404)
            request = urllib.request.Request(base + '/api/request', data=b'{"uri":"bad"}',
                headers={'Origin': base, 'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as expired:
                urllib.request.urlopen(request)
            self.assertEqual(expired.exception.code, 403)
            self.assertIn('expired', expired.exception.read().decode())
            self.assertEqual(self.app.sessions, {})
            # Health and UI assets stay reachable even when real browser capacity is full.
            now = time.time()
            self.app.sessions.update({str(n): {'host': True, 'csrf': str(n), 'seen': now,
                'expires': now + 3600} for n in range(500)})
            for path in ['/healthz', '/legacy']:
                with urllib.request.urlopen(base + path) as response:
                    self.assertEqual(response.status, 200)
            with self.assertRaises(urllib.error.HTTPError):
                urllib.request.urlopen(base + '/api/state')
            with self.assertRaises(urllib.error.HTTPError) as bad_host:
                urllib.request.urlopen(urllib.request.Request(base + '/healthz', headers={'Host': 'evil.example'}))
            self.assertEqual(bad_host.exception.code, 403)
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def test_kiosk_default_optional_name_and_switch_preserve_attribution_and_phone_behavior(self):
        sid, _ = self.app.session('')
        self.app.attach_guest(sid, '')
        result = self.app.action(sid, '/api/guest/switch', {'kiosk': True})
        self.assertIn('_guest_token', result)
        first = self.app.guest_id(sid)
        self.assertEqual(self.app.room_state(sid)['profile'], {'name': 'Kiosk User', 'named': False})
        for uri in [A, B]:
            self.app.catalog[uri] = {'uri': uri, 'name': uri}
        self.app.submit(sid, A)
        self.app.action(sid, '/api/profile', {'name': 'Pat'})
        self.app.submit(sid, B)
        self.app.action(sid, '/api/profile', {'name': ''})
        self.assertEqual(self.app.guest_name(first), 'Kiosk User')
        self.app.enabled = True
        self.app.action(sid, '/api/guest/switch', {'kiosk': True})
        self.assertNotEqual(self.app.guest_id(sid), first)
        queue = self.app.queue(sid)
        self.assertEqual([r['requested_by'] for r in queue], ['Kiosk User', 'Pat'])
        self.assertTrue(all(not r['mine'] and not r['voted'] for r in queue))
        self.assertTrue(self.app.enabled)
        self.app.vote(sid, queue[0]['id'])
        self.assertEqual(self.app.queue(sid)[0]['votes'], 2)
        phone, _ = self.app.session('')
        self.app.attach_guest(phone, '')
        self.assertEqual(self.app.room_state(phone)['profile']['name'], '<Shy guy - name not provided>')
        self.app.action(phone, '/api/profile', {'name': 'Phone visitor'})
        self.assertEqual(self.app.guest_name(self.app.guest_id(phone)), 'Phone visitor')
