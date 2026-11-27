import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch
import urllib.error
import foxtunes as f
import web_server as w
from diagnostics import Diagnostics
from test_web import FakeSpotify, A


class DiagnosticTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory()
        self.api=FakeSpotify()
        self.app=w.App(Path(self.temp.name),self.api)
        self.output=[]
        self.app.diagnostics.sink=self.output.append
        self.sid,self.session=self.app.session('')
        self.session['host']=True
        self.app.device='speaker'
        self.app.catalog[A]={'uri':A,'name':'PRIVATE SONG','artists':'PRIVATE ARTIST'}
    def tearDown(self):
        self.app.db.close()
        self.temp.cleanup()

    def test_action_api_and_state_events_share_correlation_without_private_input(self):
        self.app.submit(self.sid,A)
        self.api.state['is_playing']=False
        self.app.action(self.sid,'/api/host/resume',{'password':'SECRET','search':'PRIVATE SEARCH'})
        events=self.app.diagnostics.report()['events']
        action=next(e for e in events if e['event']=='action.started')
        operation=next(e for e in events if e['event']=='spotify.operation')
        completed=next(e for e in events if e['event']=='action.completed')
        self.assertEqual(action['correlation'],operation['correlation'])
        self.assertEqual(action['correlation'],completed['correlation'])
        encoded=json.dumps(events)
        for private in ['SECRET','PRIVATE SONG','PRIVATE ARTIST','PRIVATE SEARCH',A,'speaker']:
            self.assertNotIn(private,encoded)
        self.assertEqual(operation['endpoint'],'/me/player/play')

    def test_failure_preserves_pre_recovery_snapshot_and_status(self):
        self.api.error=f.Failure('Bearer SECRET private-provider-body',status=403)
        with self.assertRaises(f.Failure):self.app.action(self.sid,'/api/host/pause',{})
        self.app.now=None
        report=self.app.diagnostic_report()
        self.assertTrue(report['failures'])
        self.assertTrue(any(e.get('status')==403 for e in report['events']))
        self.assertNotIn('SECRET',json.dumps(report))
        self.assertNotIn('private-provider-body',json.dumps(report))

    def test_successful_polls_do_not_log_every_time_or_every_position(self):
        self.app.poll(True)
        length=len(self.output)
        self.api.state['progress_ms']+=1000
        self.app.poll(True)
        self.assertEqual(len(self.output),length)
        self.api.state['is_playing']=False
        self.app.poll(True)
        self.assertTrue(any(e['event']=='state.changed' for e in self.output[length:]))

    def test_report_and_account_require_host(self):
        self.session['host']=False
        for route in ('diagnostics','account'):
            with self.assertRaises(PermissionError):self.app.action(self.sid,'/api/host/'+route,{})
        self.assertFalse(self.api.calls)

    def test_capacity_failure_retention_and_logging_failure_do_not_affect_playback(self):
        diag=Diagnostics(sink=lambda e:None,capacity=3)
        for n in range(10):diag.emit('example',number=n)
        self.assertEqual(len(diag.report()['events']),3)
        for n in range(10):diag.failure('failure',{'id':n},'uncertain')
        self.assertEqual(len(diag.report()['failures']),5)
        self.app.diagnostics.sink=Mock(side_effect=OSError('Disk full'))
        self.app.action(self.sid,'/api/host/stop',{})
        self.assertFalse(self.app.enabled)

    def test_request_state_changes_and_block_reason_are_observable(self):
        self.app.submit(self.sid,A)
        self.app.poll(True)
        self.app.enabled=True
        self.app.poll(True)
        report=self.app.diagnostic_report()
        self.assertTrue(any(e['event']=='dispatch.state' for e in report['events']))
        self.assertTrue(any(e['event']=='state.changed' and any(r['state']=='accepted' for r in e['current']['requests']) for e in report['events']))

    def test_http_status_is_retained_without_reading_error_body(self):
        error=urllib.error.HTTPError('https://api.spotify.com/v1/me/player',403,'Forbidden',{},io.BytesIO(b'SECRET'))
        with patch('urllib.request.urlopen',side_effect=error):
            with self.assertRaises(f.Failure) as caught:f.request_json('https://api.spotify.com/v1/me/player')
        self.assertEqual(caught.exception.status,403)
        self.assertNotIn('SECRET',str(caught.exception))

    def test_cache_account_privacy_failure_and_token_change(self):
        (self.app.data/'tokens.json').write_text('{"access_token":"SECRET"}')
        with patch.object(self.api,'call',return_value={'id':'PRIVATE-ID','email':'PRIVATE-EMAIL','display_name':'Guest Example'}) as lookup:
            first=self.app.account_info()
            self.assertEqual(first['display_name'],'Guest Example')
            self.assertEqual(self.app.account_info(),first)
            self.assertEqual(lookup.call_count,1)
            (self.app.data/'tokens.json').write_text('{"access_token":"DIFFERENT-SECRET"}')
            self.app.account_info()
            self.assertEqual(lookup.call_count,2)
        self.app.account_due=0
        with patch.object(self.api,'call',side_effect=f.Failure('SECRET')) as lookup:
            self.assertEqual(self.app.account_info()['status'],'unavailable')
            self.assertIsNone(self.app.account_info()['display_name'])
            self.assertEqual(lookup.call_count,1)
        report=json.dumps(self.app.diagnostic_report())
        for value in ['Guest Example','PRIVATE-ID','PRIVATE-EMAIL','SECRET']:
            self.assertNotIn(value,report)

    def test_no_token_does_not_fetch_or_show_old_name(self):
        self.app.account_cache={'status':'verified','display_name':'Old Guest','verified_at':1}
        self.assertEqual(self.app.account_info()['status'],'not_connected')
        self.assertFalse(self.api.calls)

    def test_report_is_independent_copy_and_salted_ids_change_between_processes(self):
        self.app.diagnostics.failure('test',{'enabled':True},'uncertain')
        result=self.app.diagnostics.report()
        result['failures'][0]['snapshot']['enabled']=False
        self.assertTrue(self.app.diagnostics.report()['failures'][0]['snapshot']['enabled'])
        self.assertNotEqual(Diagnostics().opaque('track'),Diagnostics().opaque('track'))

    def test_provider_snapshot_and_queue_anomalies_are_sanitized_and_deduplicated(self):
        state={'device':{'id':'PRIVATE_DEVICE','name':'PRIVATE_NAME'},
               'item':{'uri':A,'name':'PRIVATE_TITLE'},
               'context':{'uri':'PRIVATE_CONTEXT','type':'playlist'},
               'is_playing':False,'shuffle_state':False,'repeat_state':'off',
               'actions':{'disallows':{'resuming':True,'SECRET':True}}}
        with patch.object(self.api,'call',return_value=state):
            self.app.api.call('/me/player')
        report=self.app.diagnostic_report()
        self.assertEqual(report['snapshot']['spotify']['repeat'],'off')
        self.assertEqual(report['snapshot']['spotify']['disallows'],['resuming'])
        with patch.object(self.api,'call',return_value={'queue':[state['item']]*4}):
            self.app.api.call('/me/player/queue')
            count=len(self.output)
            self.app.api.call('/me/player/queue')
            self.assertEqual(len(self.output),count)
        encoded=json.dumps(self.app.diagnostic_report())
        for private in ['PRIVATE_DEVICE','PRIVATE_NAME','PRIVATE_CONTEXT','PRIVATE_TITLE','SECRET',A]:
            self.assertNotIn(private,encoded)
        self.assertEqual(self.app.diagnostic_report()['queue_snapshot']['reported_count'],4)
