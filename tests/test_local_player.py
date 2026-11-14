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
        self.app.api.call.assert_not_called()
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

    def test_protocol_quotes_filename_and_rejects_newlines(self):
        mpd=mpd_agent.MPD();mpd.file=io.BytesIO()
        # Validate before any command reaches the socket.
        with self.assertRaises(ValueError):mpd.command('addid','file\nstop')
        self.assertEqual(mpd.file.getvalue(),b'')
