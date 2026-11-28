import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import foxtunes as f
import mpd_agent
import web_server as w
from background_playback import context_uri
import test_mixed_playback as fixtures
import test_queue_completion as completion
A = completion.A

CONTEXT = 'spotify:playlist:' + 'p'*22


class BackgroundTests(unittest.TestCase):
    setUp = completion.QueueCompletionTests.setUp
    tearDown = completion.QueueCompletionTests.tearDown
    owned = completion.QueueCompletionTests.owned

    def save_background(self):
        self.app.set_meta('background_context', {'uri':CONTEXT, 'device':'speaker', 'explicit':True})

    def finish_request(self):
        self.owned(A)
        self.app.arm_background()
        self.spotify.state['progress_ms'] = 59000
        self.app.poll(True)
        self.spotify.state.update(is_playing=False, progress_ms=0)
        # Spotify can retain the original seed rather than the last queued track.
        self.spotify.state['item'] = dict(self.spotify.state['item'], uri=fixtures.SPOTIFY)
        self.events.clear()
        with patch('web_server.time.time', return_value=self.app.updated+2):
            self.app.poll(True)

    def test_restores_context_once_after_last_observed_request_end(self):
        self.save_background()
        self.finish_request()
        writes = [e for e in self.events if e[0]=='spotify' and e[2]!='GET']
        self.assertEqual([e[3] for e in writes], [{'context_uri':CONTEXT}])
        self.assertFalse(self.app.background_armed)
        self.app.poll(True)
        self.assertEqual(len([e for e in self.events if e[0]=='spotify' and e[2]!='GET']), 1)

    def test_background_acceptance_requires_observed_context_and_times_out(self):
        self.save_background()
        self.finish_request()
        self.assertIsNotNone(self.app.background_pending)
        self.assertFalse(self.app.verify_background(self.spotify.state))
        self.spotify.state['context']={'uri':CONTEXT}
        self.assertTrue(self.app.verify_background(self.spotify.state))
        self.assertIsNone(self.app.background_pending)
        self.app.background_pending={'uri':CONTEXT,'device':'speaker','deadline':0}
        self.spotify.state['context']=None
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.assertIn('not confirmed',self.app.reason)

    def test_missing_context_explains_idle_without_replaying_seed(self):
        self.finish_request()
        self.assertIn('Choose background music', self.app.background_message)
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))
        self.assertTrue(self.app.idle_ready)

    def test_capture_real_context_before_first_request_changes_it(self):
        self.spotify.state['context'] = {'uri':CONTEXT}
        self.app.submit(self.sid,A)
        self.app.poll(True)
        self.assertEqual(self.app.meta('background_context')['uri'], CONTEXT)
        self.assertTrue(self.app.background_armed)

    def test_never_captures_request_context_or_other_device(self):
        self.owned(A)
        self.spotify.state['context'] = {'uri':CONTEXT}
        self.app.capture_background(self.spotify.state)
        self.assertEqual(self.app.meta('background_context',{}), {})
        self.app.db.execute("UPDATE songs SET state='past'")
        self.app.db.commit()
        self.spotify.state['device'] = {'id':'other'}
        self.app.capture_background(self.spotify.state)
        self.assertEqual(self.app.meta('background_context',{}), {})

    def test_restore_refuses_wrong_device_pending_requests_and_midtrack_pause(self):
        self.save_background()
        self.app.arm_background()
        self.app.idle_ready=True
        self.spotify.state.update(is_playing=False,device={'id':'other'})
        self.app.restore_background(self.spotify.state)
        self.assertFalse(self.events)
        self.spotify.state['device']={'id':'speaker'}
        self.app.submit(self.sid,A)
        self.app.restore_background(self.spotify.state)
        self.assertFalse(self.events)
        self.app.db.execute("UPDATE songs SET state='past'")
        self.app.db.commit()
        self.app.idle_ready=False
        self.app.restore_background(self.spotify.state)
        self.assertFalse(self.events)

    def test_host_stop_and_server_restart_disarm_automatic_restoration(self):
        self.save_background()
        self.app.arm_background()
        self.app.idle_ready=True
        self.app.action(self.sid,'/api/host/stop',{})
        self.assertFalse(self.app.background_armed)
        self.app.restore_background({})
        self.assertFalse(self.events)
        self.app.db.close()
        self.app=w.App(self.path,self.spotify)
        self.assertEqual(self.app.meta('background_context')['uri'],CONTEXT)
        self.assertFalse(self.app.enabled)
        self.assertFalse(self.app.background_armed)

    def test_restore_failure_is_not_retried(self):
        self.save_background()
        self.app.arm_background()
        self.app.idle_ready=True
        self.spotify.state.update(is_playing=False)
        original=self.spotify.call
        def call(path,method='GET',payload=None,params=None):
            if method=='PUT':raise f.Failure('Forbidden')
            return original(path,method,payload,params)
        with patch.object(self.spotify,'call',side_effect=call) as mocked:
            with self.assertRaises(f.Failure):self.app.restore_background(self.spotify.state)
            self.app.restore_background(self.spotify.state)
            self.assertEqual(sum(c.kwargs.get('method')=='PUT' or len(c.args)>1 and c.args[1]=='PUT' for c in mocked.call_args_list),1)

    def test_local_finish_restores_only_after_verified_mpd_stop(self):
        self.save_background()
        self.app.local_config.update(enabled=True,device='speaker')
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(fixtures.LOCAL,json.dumps(fixtures.SONG)))
        self.app.db.commit()
        self.app.submit(self.sid,fixtures.LOCAL)
        self.app.poll(True)
        self.app.local.state['progress_ms']=59000
        self.app.poll(True)
        self.app.local.state['state']='stop'
        self.events.clear()
        self.app.poll(True)
        self.assertTrue(any(e[0]=='spotify' and e[3]=={'context_uri':CONTEXT} for e in self.events if len(e)>3))
        self.assertIsNone(self.app.local_active)

    def test_resume_audio_does_not_enable_handoff_or_replace_track(self):
        self.spotify.state['is_playing']=False
        self.app.action(self.sid,'/api/host/resume',{})
        writes=[e for e in self.events if e[0]=='spotify' and e[2]!='GET']
        self.assertEqual([e[3] for e in writes],[{}])
        self.assertFalse(self.app.enabled)
        self.assertGreater(self.app.quick_poll_until,0)

    def test_resume_refuses_other_device_and_guest(self):
        self.spotify.state.update(is_playing=False,device={'id':'other'})
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/resume',{})
        self.assertFalse(any(e[0]=='spotify' and e[2]!='GET' for e in self.events))
        guest,_=self.app.session('')
        for route in ('resume','background'):
            with self.assertRaises(PermissionError):self.app.action(guest,'/api/host/'+route,{})

    def test_background_choice_validates_uri_and_does_not_start_audio(self):
        self.app.action(self.sid,'/api/host/background',{'uri':'https://open.spotify.com/playlist/'+'p'*22+'?si=x','device':'speaker'})
        self.assertEqual(self.app.meta('background_context')['uri'],CONTEXT)
        self.assertFalse(self.events)
        for value in ('https://example.com/playlist/x','spotify:track:'+'a'*22,'https://open.spotify.com/playlist/x'):
            with self.assertRaises(f.Failure):context_uri(value)

    def test_cold_start_uses_gathering_override_and_restore_preserves_player_default(self):
        self.save_background()
        override = 'spotify:playlist:' + 'g'*22
        self.app.action(self.sid, '/api/host/background', {'scope':'gathering', 'uri':override})
        self.assertFalse(self.events)
        self.spotify.state.update(is_playing=False, item=None, shuffle_state=False, repeat_state='off')
        original = self.spotify.call

        def call(path, method='GET', payload=None, params=None):
            if path == '/me/player/play' and method == 'PUT' and payload.get('context_uri'):
                self.events.append(('spotify', path, method, payload))
                self.spotify.state.update(is_playing=True, item={'uri': A}, context={'uri': payload['context_uri']})
                return {}
            return original(path, method, payload, params)

        with patch.object(self.spotify, 'call', side_effect=call):
            self.app.start_session('speaker', True)
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.spotify.state['context']['uri'], override)
        self.app.action(self.sid, '/api/host/background', {'scope':'gathering', 'uri':''})
        self.assertEqual(self.app.effective_background('speaker')['uri'], CONTEXT)

    def test_player_defaults_preserve_legacy_and_are_independent(self):
        self.save_background()
        other = 'spotify:playlist:' + 'o'*22
        self.app.save_background_default('other', other, True)
        self.assertEqual(self.app.effective_background('speaker')['uri'], CONTEXT)
        self.assertEqual(self.app.effective_background('other')['uri'], other)

    def test_delayed_contextless_start_releases_first_request_without_retrying_context(self):
        self.save_background()
        item = self.spotify.state['item']
        self.spotify.state.update(is_playing=False, item=None, shuffle_state=False, repeat_state='off')
        self.app.start_session('speaker', True)
        self.assertEqual(self.app.health()['status'], 'Starting')
        self.app.submit(self.sid, A)
        self.spotify.state.update(is_playing=True, item=item, context=None, progress_ms=200)
        self.app.poll(True)
        self.assertFalse(self.app.enabled)
        self.app.startup_pending['grace'] = 0
        self.spotify.state['progress_ms'] = 1200
        self.app.poll(True)
        self.assertTrue(self.app.enabled)
        writes = [event[3] for event in self.events if event[:3]==('spotify','/me/player/play','PUT')]
        self.assertEqual(writes, [{'context_uri':CONTEXT},{'uris':[A]}])

    def test_startup_recovers_after_terminal_track_and_missing_player(self):
        self.save_background()
        item = self.spotify.state['item']
        self.spotify.state.update(context={'uri':CONTEXT}, shuffle_state=True, repeat_state='off',
                                  progress_ms=item['duration_ms'])
        original = self.spotify.call

        def call(path, method='GET', payload=None, params=None):
            if path == '/me/player/play' and method == 'PUT' and 'context_uri' in payload:
                self.events.append(('spotify', path, method, payload))
                return {}
            return original(path, method, payload, params)

        self.enterContext(patch.object(self.spotify, 'call', side_effect=call))
        self.app.start_session('speaker', True)
        self.assertFalse(self.app.enabled)
        self.assertIsNotNone(self.app.startup_pending)
        self.spotify.state.clear()
        self.app.poll(True)
        self.app.startup_pending['missing_since'] -= 6
        self.app.poll(True)
        self.app.poll(True)
        writes = [event[3] for event in self.events if event[:3] == ('spotify','/me/player/play','PUT')]
        self.assertEqual(writes, [{'context_uri':CONTEXT}, {'context_uri':CONTEXT}])
        self.assertFalse(any(event[:3] == ('spotify','/me/player/shuffle','PUT') for event in self.events))
        self.spotify.state.update(device={'id':'speaker'}, item=item, progress_ms=2000,
                                  is_playing=True, context={'uri':CONTEXT}, shuffle_state=True, repeat_state='off')
        self.app.poll(True)
        self.assertTrue(self.app.enabled)
        self.assertFalse(any(event[:3] == ('spotify','/me/player/shuffle','PUT') for event in self.events))
        self.app.submit(self.sid, A)
        self.app.poll(True)
        self.assertTrue(any(event[:3] == ('spotify','/me/player/shuffle','PUT') for event in self.events))
        self.spotify.state['shuffle_state'] = False
        self.app.poll(True)
        writes = [event[3] for event in self.events if event[:3] == ('spotify','/me/player/play','PUT')]
        self.assertEqual(writes, [{'context_uri':CONTEXT}, {'context_uri':CONTEXT}, {'uris':[A]}])

    def test_nonmatching_context_cannot_confirm_start_and_host_stop_cancels_pending(self):
        self.save_background()
        self.spotify.state.update(is_playing=False, item=None)
        self.app.start_session('speaker', True)
        self.spotify.state.update(is_playing=True, item={'uri':A}, context={'uri':'spotify:playlist:'+'z'*22})
        self.app.poll(True)
        self.assertEqual(self.app.health()['status'], 'Starting')
        self.app.action(self.sid, '/api/host/stop', {})
        self.assertIsNone(self.app.startup_pending)
        self.app.poll(True)
        self.assertFalse(self.app.enabled)

    def test_background_shuffle_is_disabled_before_the_first_request(self):
        self.save_background()
        self.spotify.state.update(context={'uri':CONTEXT},shuffle_state=False)
        self.events.clear()
        self.app.poll(True)
        self.assertTrue(any(event[:3]==('spotify','/me/player/shuffle','PUT') for event in self.events))
        self.spotify.state['shuffle_state'] = True
        self.app.submit(self.sid,A)
        self.events.clear()
        self.app.poll(True)
        self.assertFalse(any(event[:3]==('spotify','/me/player/play','PUT') for event in self.events))
        self.spotify.state['shuffle_state'] = False
        self.app.poll(True)
        self.assertTrue(any(event[:3]==('spotify','/me/player/play','PUT') for event in self.events))

    def test_shuffle_502_rechecks_before_dispatching_first_request(self):
        self.save_background()
        self.spotify.state.update(context={'uri':CONTEXT}, shuffle_state=True)
        self.app.submit(self.sid, A)
        original = self.app.api.call

        def call(path, method='GET', payload=None, params=None):
            if path == '/me/player/shuffle' and method == 'PUT':
                raise f.Failure('Bad gateway', status=502)
            return original(path, method, payload, params)

        with patch.object(self.app.api, 'call', side_effect=call) as api:
            self.app.poll(True)
            self.assertTrue(self.app.enabled)
            self.assertEqual(self.app.queue(self.sid)[0]['state'], 'waiting')
            self.spotify.state['shuffle_state'] = False
            self.app.poll(True)
            writes = [request.args[:2] for request in api.call_args_list
                      if len(request.args) > 1 and request.args[1] == 'PUT']
            self.assertEqual(writes, [('/me/player/shuffle', 'PUT'), ('/me/player/play', 'PUT')])

    def test_skipping_last_spotify_request_returns_to_background(self):
        self.save_background()
        self.owned(A)
        self.events.clear()
        self.app.skip_request()
        writes = [event for event in self.events if event[0]=='spotify' and event[2]!='GET']
        self.assertEqual([event[3] for event in writes], [{'context_uri':CONTEXT}])
        self.assertTrue(self.app.enabled)
        self.assertEqual(self.app.background_pending['uri'], CONTEXT)
        self.assertEqual(self.app.db.execute('SELECT state FROM songs').fetchone()[0], 'past')

    def test_house_fallback_requires_definitive_failure_and_idle_target(self):
        house = 'spotify:playlist:' + 'h'*22
        self.app.action(self.sid, '/api/host/background', {'scope':'house','uri':house,'name':'House'})
        self.assertEqual(self.app.effective_background('speaker')['uri'], house)
        with patch.object(self.app.api,'call',side_effect=[
            f.Failure('Unavailable', status=404), {'device':{'id':'speaker'},'is_playing':False}, {}
        ]) as spotify:
            self.assertEqual(self.app.play_background({'uri':CONTEXT}, 'speaker'), house)
            self.assertEqual(spotify.call_args.args[2], {'context_uri':house})
        with patch.object(self.app.api,'call',side_effect=f.Failure('Timeout', uncertain=True)) as spotify:
            with self.assertRaises(f.Failure):
                self.app.play_background({'uri':CONTEXT}, 'speaker')
            spotify.assert_called_once()

    def test_repeated_snapshot_warns_without_inventing_or_removing_entries(self):
        with patch.object(self.spotify,'call',return_value={'queue':[self.spotify.state['item']]*4}):
            self.app.refresh_upcoming()
        self.assertEqual(len(self.app.spotify_upcoming['items']),4)
        self.assertIn('same track repeatedly',self.app.spotify_upcoming['warning'])

    def test_local_resume_checks_agent_and_preserves_owned_item(self):
        self.app.local_config.update(enabled=True,device='speaker')
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(fixtures.LOCAL,json.dumps(fixtures.SONG)))
        self.app.db.commit()
        self.app.submit(self.sid,fixtures.LOCAL)
        self.app.poll(True)
        self.app.local.state.update(state='pause',agent_version=6)
        with self.assertRaisesRegex(f.Failure,'agent 7'):
            self.app.action(self.sid,'/api/host/resume',{})
        self.assertNotIn(('mpd','resume'),self.events)
        self.app.local.state['agent_version']=7
        self.app.action(self.sid,'/api/host/resume',{})
        self.assertIn(('mpd','resume'),self.events)
        self.assertFalse(self.app.enabled)
        self.assertEqual(self.app.local_active['uri'],fixtures.LOCAL)

    def test_agent_resume_is_bounded_and_does_not_replace_the_queue(self):
        mpd=Mock()
        mpd_agent.execute(mpd,{'action':'resume'})
        mpd.command.assert_called_once_with('pause',0)

    def test_api_error_identifies_operation_without_query_or_token(self):
        with tempfile.TemporaryDirectory() as temp:
            api=f.Spotify(Path(temp))
            with patch.object(api,'token',return_value='secret-token'),patch('foxtunes.request_json',side_effect=f.Failure('Spotify HTTP 403.')):
                with self.assertRaisesRegex(f.Failure,'PUT /me/player/repeat: Spotify HTTP 403') as error:
                    api.call('/me/player/repeat','PUT',params={'device_id':'private-device'})
            self.assertNotIn('secret',str(error.exception))
            self.assertNotIn('private-device',str(error.exception))
