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
SONG={'uri':URI,'name':'Song','_file':'song.wav','duration_ms':8000}

class LoudnessTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.root=Path(self.temp.name)/'music';self.root.mkdir()
        self.state=Path(self.temp.name)/'state';self.state.mkdir()
        (self.root/'song.wav').write_bytes(b'original')
    def tearDown(self):self.temp.cleanup()

    def test_incremental_add_change_delete_rename_and_target_changes(self):
        with patch('local_loudness.measure',return_value={'input_i':'-10','input_tp':'-2'}) as measure:
            self.assertEqual(loud.analyze(SONG,self.root,self.state),'analyzed')
            self.assertEqual(loud.analyze(SONG,self.root,self.state),'skipped')
            entry=loud.measurement(self.root,self.state,SONG)
            self.assertNotEqual(loud.playback_plan(entry,-14)['volume'],loud.playback_plan(entry,-18)['volume'])
            (self.root/'renamed.wav').write_bytes(b'original')
            renamed=dict(SONG,uri='local:'+'b'*64,_file='renamed.wav')
            self.assertEqual(loud.analyze(renamed,self.root,self.state),'reused')
            self.assertEqual(loud.reconcile([renamed],self.state),1)
            (self.root/'renamed.wav').write_bytes(b'changed')
            self.assertIsNone(loud.measurement(self.root,self.state,renamed))
            self.assertEqual(loud.analyze(renamed,self.root,self.state),'analyzed')
            self.assertEqual(measure.call_count,2)
            self.assertEqual(loud.reconcile([],self.state),1)
            self.assertEqual(loud.cache(self.state),{})
        self.assertEqual((self.root/'song.wav').read_bytes(),b'original')
        self.assertFalse((self.root/loud.CACHE_FOLDER).exists())

    def test_curve_peak_ceiling_and_no_amplification(self):
        self.assertAlmostEqual(loud.volume_gain(50),-18.473,places=2)
        for percent in range(1,101):self.assertEqual(loud.gain_volume(loud.volume_gain(percent)),percent)
        peak=loud.playback_plan({'input_lufs':-20,'true_peak':0})
        self.assertTrue(peak['limited']);self.assertLessEqual(peak['estimated_true_peak'],-1)
        quiet=loud.playback_plan({'input_lufs':-30,'true_peak':-10})
        self.assertEqual(quiet['volume'],100);self.assertTrue(quiet['limited'])
        self.assertEqual(loud.playback_plan({'input_lufs':-10,'true_peak':-2},reference=0)['volume'],0)

    def test_play_uses_original_and_bound_volume_record(self):
        mpd=Mock()
        def command(name,*args):
            if name=='listallinfo':return [('file','song.wav'),('Time','8')]
            if name=='addid':return [('Id','7')]
            return []
        mpd.command.side_effect=command
        plan=loud.playback_plan({'input_lufs':-10,'true_peak':-2})
        with patch('local_loudness.cached_plan',return_value=plan),patch('local_loudness.paths',return_value=(self.root,self.state)):
            uri='local:'+hashlib.sha256(b'song.wav').hexdigest()
            result=mpd_agent.execute(mpd,{'action':'play','uri':uri,'normalize':True,'volume':100})
            self.assertEqual(result['song_id'],'7')
            self.assertIsNotNone(loud.playback_record(uri,'7'));self.assertIsNone(loud.playback_record(uri,'8'))
        self.assertIn(unittest.mock.call('addid','song.wav'),mpd.command.call_args_list)
        self.assertIn(unittest.mock.call('setvol',plan['volume']),mpd.command.call_args_list)
        self.assertIn(unittest.mock.call('replay_gain_mode','off'),mpd.command.call_args_list)

    def test_missing_analysis_never_stops_playback(self):
        mpd=Mock();mpd.command.return_value=[('file','song.wav'),('Time','8')]
        with patch('local_loudness.cached_plan',side_effect=ValueError('needs analysis')):
            with self.assertRaises(ValueError):mpd_agent.execute(mpd,{'action':'play','uri':'local:'+hashlib.sha256(b'song.wav').hexdigest(),'normalize':True})
        self.assertFalse(any(c.args[0] in ('stop','playid') for c in mpd.command.call_args_list))

    def test_rescan_coalesces_and_worker_reconciles_latest_snapshot(self):
        job={'running':True,'method':loud.METHOD}
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.active_job',return_value=job),patch('local_loudness.shutil.which',return_value='ffmpeg'):
            self.assertTrue(loud.start([SONG])['queued'])
            loud.start([])
        self.assertEqual(loud.read_json(self.state/'pending.json',{})['songs'],[])
        loud.write_json(self.state/'input.json',{'songs':[SONG]})
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.os.nice'),patch('local_loudness.measure',return_value={'input_i':'-10','input_tp':'-2'}):loud.run()
        self.assertEqual(loud.cache(self.state),{})
        job=loud.read_json(self.state/'job.json',{})
        self.assertEqual(job['phase'],'complete');self.assertEqual(job['removed'],1)

    def test_legacy_job_is_stopped_without_launching_new_copy_job(self):
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.active_job',return_value={'running':True}),patch('local_loudness.shutil.which',return_value='ffmpeg'),patch('local_loudness.subprocess.Popen') as spawn:
            with self.assertRaisesRegex(ValueError,'Old audio-copy'):loud.start([SONG])
            self.assertTrue((self.state/'cancel').exists());spawn.assert_not_called()

    def test_cancel_and_empty_scan_do_not_run_ffmpeg(self):
        loud.write_json(self.state/'input.json',{'songs':[SONG]});(self.state/'cancel').touch()
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.os.nice'),patch('local_loudness.measure') as process:
            loud.run();process.assert_not_called()
        self.assertEqual(loud.read_json(self.state/'job.json',{})['phase'],'cancelled')
        with patch('local_loudness.paths',return_value=(self.root,self.state)),patch('local_loudness.shutil.which',return_value=None),patch('local_loudness.subprocess.Popen') as spawn:
            spawn.return_value.pid=12345
            self.assertTrue(loud.start([])['started']);self.assertTrue(spawn.call_args.kwargs['start_new_session'])

    def test_paths_are_bounded_and_nonfinite_reports_rejected(self):
        for value in ['../outside.wav','/etc/passwd',loud.CACHE_FOLDER+'/x.flac']:
            with self.assertRaises(ValueError):loud.source_path(self.root,value)
        for value in [float('nan'),True,'-14',-21]:
            with self.assertRaises(ValueError):loud.validate_target(value)
        report={'input_i':'-14','input_tp':'-1'}
        with patch('local_loudness.subprocess.run',return_value=Mock(returncode=0,stderr=json.dumps(report)+'\nlog trailer')):
            self.assertEqual(loud.run_ffmpeg([],self.state),report)

    @unittest.skipUnless(shutil.which('ffmpeg'),'FFmpeg required')
    def test_real_analysis_keeps_original_and_reuses_measurement(self):
        source=self.root/'song.wav';source.unlink()
        subprocess.run(['ffmpeg','-v','error','-f','lavfi','-i','sine=frequency=440:duration=8','-c:a','pcm_s16le',str(source)],check=True,capture_output=True)
        original=source.read_bytes()
        self.assertEqual(loud.analyze(SONG,self.root,self.state),'analyzed')
        entry=loud.measurement(self.root,self.state,SONG)
        self.assertTrue(-40<entry['input_lufs']<-10)
        self.assertLessEqual(loud.playback_plan(entry)['estimated_true_peak'],-1)
        with patch('local_loudness.measure') as measure:
            self.assertEqual(loud.analyze(SONG,self.root,self.state),'skipped');measure.assert_not_called()
        self.assertEqual(source.read_bytes(),original)
        self.assertEqual(list(self.root.iterdir()),[source])

class LoudnessHostTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.app=w.App(Path(self.temp.name),Mock())
        self.app.local=Mock();self.sid,self.session=self.app.session('');self.session['host']=True
    def tearDown(self):self.app.db.close();self.temp.cleanup()

    def test_host_only_and_incomplete_library_cannot_enable(self):
        guest,_=self.app.session('')
        with self.assertRaises(PermissionError):self.app.action(guest,'/api/host/loudness-start',{'target':-14})
        self.app.local.call.return_value={'method':loud.METHOD,'ready':0,'total':2}
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True,'target':-14,'volume':100})
        self.assertFalse(self.app.local_config.get('normalization_enabled',False))

    def test_enable_preserves_original_volume_and_live_adjusts_active_mode(self):
        self.app.local.call.return_value={'method':loud.METHOD,'ready':2,'total':2}
        self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True,'target':-14,'volume':100})
        self.assertEqual(self.app.local_config['volume'],50)
        self.app.apply_local_volume(85)
        self.assertEqual(self.app.local_config['normalized_volume'],85);self.assertEqual(self.app.local_config['volume'],50)

    def test_partial_analysis_can_enable_but_old_copy_readiness_cannot(self):
        self.app.local.call.return_value={'method':loud.METHOD,'ready':1,'total':2,'running':True}
        self.assertTrue(self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True})['enabled'])
        self.app.local.call.return_value={'ready':2,'total':2}
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/loudness-config',{'enabled':True})

    def test_disable_does_not_start_or_stop_audio(self):
        self.app.action(self.sid,'/api/host/loudness-config',{'enabled':False})
        self.app.local.call.assert_not_called()

    def test_normalized_readiness_is_checked_before_pausing_spotify(self):
        self.app.device='speaker';self.app.local_config.update(enabled=True,device='speaker',normalization_enabled=True)
        song=dict(uri=URI,name='Track',artists='Artist',source='local',duration_ms=8000)
        self.app.db.execute('INSERT INTO local_tracks VALUES(?,?)',(URI,json.dumps(song)));self.app.db.commit()
        self.app.local.call.return_value={'ready':True}
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
        self.app.local.call.return_value={'ready':True}
        self.app.submit(self.sid,URI);row=self.app.queue(self.sid)[0]
        self.app.local.call.side_effect=[{'state':'stop'},{'ready':True},{'song_id':'7'}]
        self.app.start_local_request(row,{'device':{'id':'speaker'},'is_playing':False})
        self.assertEqual(self.app.local.call.call_args.kwargs,{'uri':URI,'volume':90,'normalize':True,'target':-14})
