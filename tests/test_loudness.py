import hashlib
import json
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import Mock, patch

import foxtunes as f
import local_loudness as loud
import mpd_agent
import web_server as w

URI='local:'+'a'*64
SONG={'uri':URI,'name':'Quiet song','_file':'quiet.wav','duration_ms':8000}


class LoudnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'music';self.root.mkdir()
        self.state=Path(self.temp.name)/'state';self.state.mkdir()
    def tearDown(self):self.temp.cleanup()

    def test_targets_and_source_paths_are_bounded(self):
        for value in [-21,-10,float('nan'),True,'-14']:
            with self.assertRaises(ValueError):loud.validate_target(value)
        for value in ['../outside.wav','/etc/passwd',loud.CACHE_FOLDER+'/x.flac']:
            with self.assertRaises(ValueError):loud.source_path(self.root,value)
        outside=Path(self.temp.name)/'outside.wav';outside.write_bytes(b'123')
        (self.root/'link.wav').symlink_to(outside)
        with self.assertRaises(ValueError):loud.source_path(self.root,'link.wav')

    def test_original_fingerprint_controls_prepared_copy(self):
        source=self.root/'quiet.wav';source.write_bytes(b'original')
        output=loud.output_path(self.root,URI);output.parent.mkdir();output.write_bytes(b'copy')
        loud.write_json(self.state/'manifest.json',{URI:{'recipe':loud.RECIPE,'target':-14.0,'source':loud.fingerprint(source),'output':loud.fingerprint(output)}})
        self.assertTrue(loud.prepared(self.root,self.state,SONG,-14))
        self.assertIsNone(loud.prepared(self.root,self.state,SONG,-12))
        source.write_bytes(b'changed original');self.assertIsNone(loud.prepared(self.root,self.state,SONG,-14))

    def test_derived_copies_do_not_duplicate_library_or_change_identity(self):
        mpd=Mock();mpd.command.return_value=[('file',loud.CACHE_FOLDER+'/'+'a'*64+'.flac'),('Title','copy'),('file','quiet.wav'),('Time','8')]
        songs=mpd_agent.library(mpd);self.assertEqual(len(songs),1);self.assertEqual(songs[0]['name'],'quiet')
        mpd.command.side_effect=[[('state','play'),('songid','2')],[('file',loud.CACHE_FOLDER+'/'+'a'*64+'.flac')]]
        status=mpd_agent.execute(mpd,{'action':'status'})
        self.assertEqual(status['uri'],URI);self.assertTrue(status['normalized'])

    def test_prepared_playback_disables_replaygain(self):
        mpd=Mock()
        def command(name,*args):
            if name=='listallinfo':return [('file','quiet.wav'),('Time','8')]
            if name=='addid':return [('Id','7')]
            return []
        mpd.command.side_effect=command
        with patch('local_loudness.prepared',return_value=loud.CACHE_FOLDER+'/prepared.flac'):
            result=mpd_agent.execute(mpd,{'action':'play','uri':'local:'+hashlib.sha256(b'quiet.wav').hexdigest(),'normalize':True,'volume':100})
        self.assertEqual(result['song_id'],'7')
        self.assertTrue(any(c.args==('replay_gain_mode','off') for c in mpd.command.call_args_list))
        self.assertTrue(any(c.args==('addid',loud.CACHE_FOLDER+'/prepared.flac') for c in mpd.command.call_args_list))

    def test_missing_copy_never_stops_or_starts_mpd(self):
        mpd=Mock();mpd.command.return_value=[('file','quiet.wav'),('Time','8')]
        with patch('local_loudness.prepared',return_value=None):
            with self.assertRaises(ValueError):mpd_agent.execute(mpd,{'action':'play','uri':'local:'+hashlib.sha256(b'quiet.wav').hexdigest(),'normalize':True})
        self.assertFalse(any(c.args[0] in ('stop','playid') for c in mpd.command.call_args_list))

    def test_space_reserve_fails_before_processing(self):
        (self.root/'quiet.wav').write_bytes(b'audio')
        disk=shutil._ntuple_diskusage(1024,1023,1)
        with patch('local_loudness.shutil.disk_usage',return_value=disk),patch('local_loudness.run_ffmpeg') as runner:
            with self.assertRaisesRegex(ValueError,'space'):loud.prepare(SONG,-14,self.root,self.state)
        runner.assert_not_called()

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg is required for audio integration test')
    def test_quiet_and_loud_audio_match_without_modifying_originals(self):
        outputs=[]
        for name,volume in [('quiet','0.03'),('loud','0.9')]:
            source=self.root/(name+'.wav')
            subprocess.run(['ffmpeg','-nostdin','-v','error','-f','lavfi','-i','sine=frequency=440:duration=8',
                            '-af','volume='+volume,'-c:a','pcm_s16le',str(source)],check=True,capture_output=True)
            before=hashlib.sha256(source.read_bytes()).hexdigest()
            uri='local:'+hashlib.sha256(source.name.encode()).hexdigest()
            song=dict(SONG,uri=uri,_file=source.name)
            self.assertEqual(loud.prepare(song,-14,self.root,self.state),'prepared')
            self.assertEqual(hashlib.sha256(source.read_bytes()).hexdigest(),before)
            entry=loud.read_json(self.state/'manifest.json',{})[uri];outputs.append(entry['output_lufs'])
            self.assertLessEqual(entry['true_peak'],-.8)
            self.assertEqual(loud.prepare(song,-14,self.root,self.state),'cached')
        self.assertLess(abs(outputs[0]-outputs[1]),.5)
        self.assertTrue(all(abs(level+14)<.7 for level in outputs))


class LoudnessHostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=w.App(Path(self.temp.name),Mock())
        self.app.local=Mock();self.sid,self.session=self.app.session('');self.session['host']=True
    def tearDown(self):self.app.db.close();self.temp.cleanup()

    def test_host_only_and_incomplete_library_cannot_enable(self):
        guest,_=self.app.session('')
        with self.assertRaises(PermissionError):self.app.action(guest,'/api/host/loudness-start',{'target':-14})
        self.app.local.call.return_value={'phase':'complete','running':False,'ready':1,'total':2,'target':-14}
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True,'target':-14,'volume':100})
        self.assertFalse(self.app.local_config.get('normalization_enabled',False))

    def test_enable_preserves_original_volume_and_live_adjusts_active_mode(self):
        self.app.local.call.return_value={'phase':'complete','running':False,'ready':2,'total':2,'target':-14}
        self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True,'target':-14,'volume':100})
        self.assertEqual(self.app.local_config['volume'],50)
        self.app.apply_local_volume(85)
        self.assertEqual(self.app.local_config['normalized_volume'],85);self.assertEqual(self.app.local_config['volume'],50)

    def test_disable_does_not_start_or_stop_audio(self):
        self.app.action(self.sid,'/api/host/loudness-config',{'enabled':False})
        self.app.local.call.assert_not_called()

    def test_normalized_readiness_is_checked_before_pausing_spotify(self):
        self.app.device='speaker';self.app.local_config.update(enabled=True,device='speaker',normalization_enabled=True)
        song=dict(uri=URI,name='Track',artists='Artist',source='local',duration_ms=8000)
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(URI,json.dumps(song)));self.app.db.commit()
        self.app.submit(self.sid,URI);row=self.app.queue(self.sid)[0]
        self.app.local.call.side_effect=[{'state':'stop'},{'ready':False}]
        self.app.api.call.reset_mock()
        with self.assertRaises(f.Failure):self.app.start_local_request(row,{'device':{'id':'speaker'},'is_playing':True})
        self.app.api.call.assert_not_called()
        self.assertEqual(self.app.queue(self.sid)[0]['state'],'waiting')

    def test_matched_play_request_uses_prepared_mode_and_output(self):
        self.app.device='speaker';self.app.local_config.update(enabled=True,device='speaker',normalization_enabled=True,loudness_target=-14,normalized_volume=90)
        song=dict(uri=URI,name='Track',artists='Artist',source='local',duration_ms=8000)
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(URI,json.dumps(song)));self.app.db.commit()
        self.app.submit(self.sid,URI);row=self.app.queue(self.sid)[0]
        self.app.local.call.side_effect=[{'state':'stop'},{'ready':True},{'song_id':'7'}]
        self.app.start_local_request(row,{'device':{'id':'speaker'},'is_playing':False})
        self.assertEqual(self.app.local.call.call_args.kwargs,{'uri':URI,'volume':90,'normalize':True,'target':-14})


class LoudnessJobTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'music';self.root.mkdir()
        self.state=Path(self.temp.name)/'state';self.state.mkdir()
    def tearDown(self):self.temp.cleanup()

    def test_installed_hyphenated_agent_can_be_loaded_by_worker(self):
        import os
        import sys
        installed=Path(self.temp.name)/'agent';installed.mkdir()
        shutil.copyfile(Path(mpd_agent.__file__),installed/'mpd-agent.py')
        shutil.copyfile(Path(loud.__file__),installed/'local_loudness.py')
        result=subprocess.run([sys.executable,'-c','import local_loudness; print(local_loudness.mpd_protocol().__name__)'],
                              cwd=installed,env=dict(os.environ,PYTHONPATH=str(installed)),capture_output=True,text=True)
        self.assertEqual(result.returncode,0,result.stderr);self.assertEqual(result.stdout.strip(),'MPD')

    def test_start_is_detached_and_cannot_duplicate_active_job(self):
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.shutil.which',return_value='/usr/bin/ffmpeg'),patch('local_loudness.status',return_value={'running':False}),patch('local_loudness.subprocess.Popen') as spawn:
            spawn.return_value.pid=12345
            self.assertTrue(loud.start([SONG],-14)['started'])
            self.assertTrue(spawn.call_args.kwargs['start_new_session'])
            self.assertNotIn('shell',spawn.call_args.kwargs)
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.shutil.which',return_value='/usr/bin/ffmpeg'),patch('local_loudness.status',return_value={'running':True}),patch('local_loudness.subprocess.Popen') as spawn:
            with self.assertRaises(ValueError):loud.start([SONG],-14)
            spawn.assert_not_called()

    def test_cancelled_job_finishes_without_starting_audio(self):
        loud.write_json(self.state/'input.json',{'songs':[SONG],'target':-14})
        (self.state/'cancel').touch()
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.os.nice'),patch('local_loudness.run_ffmpeg') as process:
            loud.run()
            process.assert_not_called()
        result=loud.read_json(self.state/'job.json',{})
        self.assertFalse(result['running']);self.assertEqual(result['phase'],'cancelled')

    def test_reused_pid_is_not_reported_as_active_preparation(self):
        import os
        loud.write_json(self.state/'job.json',{'running':True,'pid':os.getpid(),'target':-14,'phase':'preparing'})
        result=loud.status([],self.root,self.state)
        self.assertFalse(result['running']);self.assertEqual(result['phase'],'interrupted')
