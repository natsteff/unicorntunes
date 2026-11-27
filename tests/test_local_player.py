import io
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch

import foxtunes as f
import local_player
import mpd_agent
import web_server


class LocalPlayerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.app = web_server.App(Path(self.temp.name), Mock())
        self.sid, session = self.app.session('')
        session['host'] = True

    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def test_config_persists_and_is_private(self):
        config = dict(enabled=True, host='player.example', user='audio', port=22, device='speaker', volume=50)
        self.app.action(self.sid, '/api/host/local-config', config)
        self.assertEqual(self.app.room_state(self.sid)['local_player']['host'], 'player.example')
        guest, _ = self.app.session('')
        self.assertIsNone(self.app.room_state(guest)['local_player'])
        with self.assertRaises(PermissionError):
            self.app.action(guest, '/api/host/local-test', {})
        self.assertEqual(json.loads((Path(self.temp.name)/'local-player.json').read_text()), dict(config,loudness_method='cached-volume-v1',normalization_enabled=False))

    def test_rejects_shell_arguments(self):
        for host in ['-oProxyCommand=bad', 'host;bad', 'host\nbad']:
            with self.assertRaises(f.Failure):
                self.app.action(self.sid, '/api/host/local-config', dict(enabled=True, host=host,user='audio',device='speaker'))

    def test_scan_does_not_play_or_call_spotify(self):
        self.app.local_config['enabled'] = True
        song = dict(uri='local:'+'a'*64,name='Test', artists='Artist',album='',year='')
        self.app.local = Mock()
        self.app.local.call.side_effect = [{'job':'1'}, {'updating':False,'agent_version':6}, {'items':[song]}, {'started':True}]
        self.assertEqual(self.app.scan_local(), {'count':1,'loudness':{'started':True}})
        self.assertEqual([c.args[0] for c in self.app.local.call.call_args_list], ['scan','status','library','loudness_start'])
        self.app.api.api.call.assert_not_called()
        self.assertEqual(self.app.db.execute('SELECT count(*) FROM local_tracks').fetchone()[0], 1)

    def test_old_agent_indexes_without_starting_obsolete_copy_job(self):
        self.app.local_config['enabled']=True;self.app.local=Mock()
        self.app.local.call.side_effect=[{}, {'updating':False,'agent_version':5}, {'items':[]}]
        result=self.app.scan_local()
        self.assertIn('v0.7.0',result['loudness_error'])
        self.assertEqual([c.args[0] for c in self.app.local.call.call_args_list],['scan','status','library'])

    def test_remote_requires_dedicated_keys(self):
        with self.assertRaisesRegex(f.Failure, 'dedicated'):
            local_player.RemoteMPD(Path(self.temp.name),dict(host='player.example',user='audio')).call('status')

    def test_agent_library_has_stable_ids_and_no_paths(self):
        mpd=Mock()
        mpd.command.return_value=[('file','Artist/song.mp3'),('Title','Title'),('Artist','Artist'),('duration','123.5')]
        result=mpd_agent.execute(mpd,{'action':'library'})['items'][0]
        self.assertRegex(result['uri'],r'^local:[a-f0-9]{64}$')
        self.assertNotIn('_file',result)
        self.assertEqual(result['duration_ms'],123500)
        self.assertEqual(result['uri'],mpd_agent.execute(mpd,{'action':'library'})['items'][0]['uri'])

    def test_agent_rejects_arbitrary_actions(self):
        mpd=Mock()
        with self.assertRaises(ValueError):mpd_agent.execute(mpd,{'action':'shell'})
        mpd.command.assert_not_called()

    def test_output_volume_targets_linked_device_and_verified_sink(self):
        self.app.local_config.update(enabled=True, device='speaker')
        self.app.local = Mock()
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, '/api/host/output-volume', {'device':'other','sink':'analog','volume':40})
        self.app.local.call.assert_not_called()
        self.app.action(self.sid, '/api/host/output-volume', {'device':'speaker','sink':'analog','volume':40})
        self.app.local.call.assert_called_once_with('output_volume_set', sink='analog', volume=40)

        def command(args):
            output = {'get-default-sink':'analog', 'get-sink-volume':'front-left: 25000 / 40% / -2 dB',
                      'get-sink-mute':'Mute: yes', 'set-sink-volume':''}
            return Mock(returncode=0, stdout=output[args[1]])

        with patch('player_health.command', side_effect=command) as pulse:
            with self.assertRaisesRegex(ValueError, 'default output changed'):
                mpd_agent.execute(None, {'action':'output_volume_set','sink':'old','volume':40})
            pulse.assert_called_once_with(['pactl','get-default-sink'])
            result = mpd_agent.execute(None, {'action':'output_volume_set','sink':'analog','volume':40})
            self.assertEqual(result, {'sink':'analog','volume':40,'muted':True})
            self.assertEqual([args[0] for args, _ in pulse.call_args_list if args[0][1].startswith('set')],
                             [['pactl','set-sink-volume','analog','40%']])

    def test_spotify_volume_requires_host_and_explicit_supported_device(self):
        guest, _ = self.app.session('')
        route = '/api/host/spotify-volume'
        body = {'device':'speaker','volume':0}
        with self.assertRaises(PermissionError):
            self.app.action(guest, route, body)
        self.app.api.api.call.return_value = {'devices':[{'id':'speaker','supports_volume':False}]}
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, route, body)
        self.app.api.api.call.return_value = {'devices':[{'id':'speaker','supports_volume':True}]}
        self.app.action(self.sid, route, body)
        self.app.api.api.call.assert_called_with('/me/player/volume', 'PUT', None, {'device_id':'speaker','volume_percent':0})

    def test_soloist_update_rejects_active_playback_before_remote_apply(self):
        self.app.local_config.update(enabled=True, device='speaker')
        self.app.device = 'speaker'
        self.app.local = Mock()
        self.app.api.api.call.return_value = {'is_playing':True}
        route = '/api/host/soloist-update'
        with self.assertRaisesRegex(f.Failure, 'Spotify is playing'):
            self.app.action(self.sid, route, {'action':'apply'})
        self.app.local.call.assert_not_called()
        mpd = Mock()
        mpd.command.return_value = [('state','play')]
        with patch('soloist_update.run') as update:
            with self.assertRaisesRegex(ValueError, 'Local playback is active'):
                mpd_agent.execute(mpd, {'action':'soloist_update_apply'})
            update.assert_not_called()

    def test_power_on_starts_only_linked_player_once_and_respects_off(self):
        self.app.local_config.update(enabled=True, device='speaker')
        self.app.api.api.call.side_effect = lambda path, *_: (
            {'devices':[{'id':'speaker'}]} if path.endswith('/devices') else {'is_playing':False}
        )
        self.app.local = Mock()
        status = {'state':'stop'}
        self.app.local.call.side_effect = [f.Failure('Connection refused'), status]
        first = {'boot_id':'a'*36,'uptime':30}
        self.app.set_meta('shutdown_progress', {'speaker':{
            'phase':'offline', 'accepted_at':local_player.time.time()-60, 'boot_id':'z'*36}})
        with patch.object(self.app, 'start_session') as start:
            self.app.handle_player_boot('speaker', first)
            start.assert_not_called()
            self.assertFalse(self.app.meta('boot_start_status')['speaker']['consumed'])
            self.assertNotIn('speaker', self.app.meta('shutdown_progress'))
            self.app.handle_player_boot('speaker', first)
            self.app.handle_player_boot('speaker', first)
            start.assert_called_once_with('speaker', True, local_status=status)
            self.app.set_meta('player_auto_start', {'speaker':False})
            self.app.handle_player_boot('speaker', {'boot_id':'b'*36,'uptime':60})
            self.assertEqual(start.call_count, 1)
            self.app.set_meta('player_auto_start', {'speaker':True})
            self.app.enabled, self.app.device = True, 'other'
            self.app.handle_player_boot('speaker', {'boot_id':'c'*36,'uptime':60})
            self.assertEqual(start.call_count, 1)
            self.assertIn('Another player', self.app.meta('boot_start_status')['speaker']['message'])

    def test_end_session_archives_only_after_linked_board_acknowledges(self):
        self.app.device = 'speaker'
        self.app.local_config.update(enabled=True, device='speaker')
        self.app.db.execute("INSERT INTO songs(guest,uri,metadata,created,state) VALUES('guest','test','{}',0,'waiting')")
        self.app.db.commit()
        self.app.local = Mock()
        route = '/api/host/end-session'
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, route, {'device':'other','confirm':True})
        self.app.local.call.assert_not_called()
        self.app.local.call.return_value = {}
        with self.assertRaises(f.Failure):
            self.app.action(self.sid, route, {'device':'speaker','confirm':True})
        self.assertEqual(self.app.db.execute('SELECT state FROM songs').fetchone()[0], 'waiting')
        self.app.local.call.return_value = {'shutdown_accepted':True}
        self.app.action(self.sid, route, {'device':'speaker','confirm':True})
        self.assertEqual(self.app.db.execute('SELECT state FROM songs').fetchone()[0], 'removed')
        self.assertEqual(self.app.meta('gathering'), 2)
        self.assertFalse(self.app.enabled)

    def test_shutdown_offline_requires_consecutive_probe_failures(self):
        self.app.set_meta('shutdown_progress', {'speaker':{'phase':'waiting','accepted_at':0,'failures':0}})
        with patch('local_player.time.time', return_value=8):
            self.app.update_shutdown_probe('speaker', {'offline':True})
            self.assertEqual(self.app.meta('shutdown_progress')['speaker']['phase'], 'waiting')
            self.app.update_shutdown_probe('speaker', {'boot_id':'present'})
            self.app.update_shutdown_probe('speaker', {'offline':True})
            self.assertEqual(self.app.meta('shutdown_progress')['speaker']['phase'], 'waiting')
            self.app.update_shutdown_probe('speaker', {'offline':True})
            result = self.app.meta('shutdown_progress')['speaker']
            self.assertEqual(result['phase'], 'offline')
            self.assertIn('Confirm it has finished', result['message'])

    def test_power_off_uses_only_fixed_graceful_shutdown_command(self):
        with patch('mpd_agent.subprocess.run', return_value=Mock(returncode=0)) as run:
            self.assertTrue(mpd_agent.execute(None, {'action':'power_off','command':'ignored'})['shutdown_accepted'])
            run.assert_called_once_with(['/usr/bin/sudo','-n','/usr/bin/systemd-run','--on-active=5s',
                '--unit=unicorntunes-poweroff','/usr/bin/systemctl','poweroff','--no-wall'],capture_output=True,timeout=5)

    def test_protocol_quotes_filename_and_rejects_newlines(self):
        mpd=mpd_agent.MPD();mpd.file=io.BytesIO()
        # Validate before any command reaches the socket.
        with self.assertRaises(ValueError):mpd.command('addid','file\nstop')
        self.assertEqual(mpd.file.getvalue(),b'')
