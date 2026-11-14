#!/usr/bin/env python3
"""Incremental loudness measurements and MPD software-volume planning; no audio copies."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import shutil
import subprocess
import sys
import time

# Reserved only to hide legacy generated files. This module never creates audio files.
CACHE_FOLDER='__unicorntunes_normalized'
METHOD='cached-volume-v1'
ANALYSIS_VERSION=1
PEAK=-1.0


class Cancelled(Exception):pass


def paths():return Path.home()/'Music/UnicornTunes',Path.home()/'.local/share/unicorntunes-local/loudness'


def read_json(path, default):
    try:return json.loads(path.read_text())
    except FileNotFoundError:return default


def write_json(path, data):
    path.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    temporary=path.with_name(path.name+'.tmp')
    with open(temporary,'w',encoding='utf-8') as output:
        os.chmod(temporary,0o600);json.dump(data,output);output.flush();os.fsync(output.fileno())
    os.replace(temporary,path)


def validate_target(value):
    if type(value) not in (int,float) or not math.isfinite(value) or not -20<=value<=-11:
        raise ValueError('Choose a loudness target between -20 and -11 LUFS.')
    return float(value)


def source_path(root, relative):
    path=PurePosixPath(relative)
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]==CACHE_FOLDER:raise ValueError('Invalid source file.')
    result=(root/relative).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():raise ValueError('Source is missing or outside the music directory.')
    return result


def fingerprint(path):
    stat=path.stat();return [stat.st_size,stat.st_mtime_ns]


def cache(state):return read_json(state/'measurements.json',{})


def measurement(root,state,song):
    entry=cache(state).get(song['uri'])
    if not entry or entry.get('version')!=ANALYSIS_VERSION:return None
    try:
        if entry.get('path')!=song['_file'] or entry.get('source')!=fingerprint(source_path(root,song['_file'])):return None
        if not all(math.isfinite(float(entry[key])) for key in ('input_lufs','true_peak')):return None
        return entry
    except (OSError,ValueError,KeyError):return None


def volume_gain(volume):
    if type(volume) is not int or not 0<=volume<=100:raise ValueError('Volume must be an integer from 0 to 100.')
    if volume==0:return float('-inf')
    return 20*math.log10(math.expm1(volume/25)/math.expm1(4))


def gain_volume(gain):
    if gain==float('-inf'):return 0
    if not math.isfinite(gain):raise ValueError('Invalid playback gain.')
    # Floor to a supported MPD percent: never overshoot the requested gain/peak ceiling.
    return max(0,min(100,math.floor(25*math.log1p(10**(min(0,gain)/20)*math.expm1(4))+1e-9)))


def playback_plan(entry,target=-14,reference=100):
    target=validate_target(target);reference_gain=volume_gain(reference)
    loudness=float(entry['input_lufs']);peak=float(entry['true_peak'])
    if not math.isfinite(loudness) or not math.isfinite(peak):raise ValueError('Invalid cached loudness measurement.')
    effective_target=target+reference_gain
    requested=effective_target-loudness
    safe=min(requested,0,PEAK-peak)
    volume=gain_volume(safe);actual_gain=volume_gain(volume)
    reason=''
    if reference and safe<requested-.01:reason='peak limit' if PEAK-peak<0 and PEAK-peak<requested else 'cannot boost above 100'
    return {'volume':volume,'reference':reference,'gain_db':round(actual_gain,3) if volume else None,
            'target':target,'effective_target':round(effective_target,2) if reference else None,
            'estimated_lufs':round(loudness+actual_gain,2) if volume else None,
            'estimated_true_peak':round(peak+actual_gain,2) if volume else None,
            'limited':bool(reason),'reason':reason,'method':METHOD}


def cached_plan(song,target=-14,reference=100):
    root,state=paths();entry=measurement(root,state,song)
    if not entry:raise ValueError('This file needs loudness analysis. Scan the local library.')
    return playback_plan(entry,target,reference)


def remember_playback(uri,song_id,plan):
    _,state=paths();write_json(state/'playback-volume.json',dict(plan,uri=uri,song_id=str(song_id)))


def playback_record(uri,song_id):
    _,state=paths();record=read_json(state/'playback-volume.json',{})
    return record if record.get('method')==METHOD and record.get('uri')==uri and record.get('song_id')==str(song_id) else None


def active_job(state):
    job=read_json(state/'job.json',{})
    if job.get('running'):
        try:
            pid=int(job['pid']);os.kill(pid,0)
            command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(Path(__file__).resolve()).encode() not in command or b'--run' not in command:raise ValueError()
        except (OSError,ValueError,KeyError):job=dict(job,running=False,phase='interrupted')
    return job


def status(songs,root=None,state=None,target=None,reference=100):
    if root is None:root,state=paths()
    job=active_job(state);target=validate_target(job.get('target',-14) if target is None else target)
    ready=0;limited=[]
    for song in songs:
        entry=measurement(root,state,song)
        if entry:
            ready+=1;plan=playback_plan(entry,target,reference)
            if plan['limited']:limited.append(dict(name=song['name'],reason=plan['reason'],estimated_lufs=plan['estimated_lufs'],volume=plan['volume']))
    return dict(job,method=METHOD,analysis_version=ANALYSIS_VERSION,legacy_job=bool(job.get('running') and job.get('method')!=METHOD),
                ready=ready,total=len(songs),target=target,ffmpeg=bool(shutil.which('ffmpeg')),limited=limited,
                rescan_queued=(state/'pending.json').exists(),cache_bytes=(state/'measurements.json').stat().st_size if (state/'measurements.json').exists() else 0)


def start(songs,target=-14):
    target=validate_target(target);root,state=paths();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    if any(not measurement(root,state,s) for s in songs) and not shutil.which('ffmpeg'):
        raise ValueError('Install ffmpeg on the playback device first: sudo apt install ffmpeg')
    with open(state/'submit.lock','a') as submission:
        fcntl.flock(submission,fcntl.LOCK_EX)
        with open(state/'job.lock','a') as lock:
            locked=True
            try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except BlockingIOError:locked=False
            running=active_job(state)
            if not locked or running.get('running'):
                if (state/'cancel').exists():raise ValueError('The previous scan is stopping. Wait for its current step, then scan again.')
                if running.get('method')!=METHOD:
                    (state/'cancel').touch(mode=0o600)
                    raise ValueError('Old audio-copy preparation is stopping. Wait for its current step, then scan again.')
                write_json(state/'pending.json',{'songs':songs,'target':target})
                return {'started':False,'queued':True,'target':target}
            (state/'cancel').unlink(missing_ok=True)
            (state/'pending.json').unlink(missing_ok=True)
            write_json(state/'input.json',{'songs':songs,'target':target})
            process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--run'],stdin=subprocess.DEVNULL,
                                     stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
            write_json(state/'job.json',{'running':True,'pid':process.pid,'method':METHOD,'target':target,'phase':'starting','done':0,'errors':[]})
    return {'started':True,'target':target}


def cancel():
    _,state=paths();state.mkdir(parents=True,exist_ok=True,mode=0o700);(state/'cancel').touch(mode=0o600)
    return {'cancel_requested':True}


def run_ffmpeg(args,state):
    if (state/'cancel').exists():raise Cancelled()
    result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-nostats','-threads','1','-filter_threads','1']+args,
                          capture_output=True,text=True,timeout=1800)
    if (state/'cancel').exists():raise Cancelled()
    if result.returncode:raise ValueError('FFmpeg could not analyze this file.')
    try:
        report,_=json.JSONDecoder().raw_decode(result.stderr[result.stderr.rfind('{'):])
        if not all(math.isfinite(float(report[k])) for k in ('input_i','input_tp')):raise ValueError()
        return report
    except (ValueError,KeyError):raise ValueError('No finite measurement; the file may be silent or unreadable.')


def measure(source,state):
    # One read/analysis pass, discarded output only. No source/target audio is written.
    return run_ffmpeg(['-i',str(source),'-map','0:a:0','-vn','-sn','-dn','-af',
                      'aformat=channel_layouts=stereo,loudnorm=I=-14:TP=-1:LRA=50:print_format=json','-f','null','-'],state)


def analyze(song,root,state):
    if measurement(root,state,song):return 'skipped'
    source=source_path(root,song['_file']);before=fingerprint(source)
    if not 0<song.get('duration_ms',0)<=1800000:raise ValueError('Analysis requires a known duration of at most 30 minutes.')
    digest=hashlib.sha256()
    with source.open('rb') as stream:
        for chunk in iter(lambda:stream.read(1024*1024),b''):digest.update(chunk)
    checksum=digest.hexdigest();entries=cache(state)
    matched=next((entry for entry in entries.values() if entry.get('sha256')==checksum and entry.get('version')==ANALYSIS_VERSION),None)
    if matched:entry=dict(matched)
    else:
        result=measure(source,state)
        entry={'version':ANALYSIS_VERSION,'input_lufs':float(result['input_i']),'true_peak':float(result['input_tp']),'sha256':checksum}
    if fingerprint(source)!=before:raise ValueError('Source changed during analysis; scan again.')
    entry.update(path=song['_file'],source=before)
    entries=cache(state);entries[song['uri']]=entry;write_json(state/'measurements.json',entries)
    return 'reused' if matched else 'analyzed'


def reconcile(songs,state):
    entries=cache(state);ids={s['uri'] for s in songs};removed=[uri for uri in entries if uri not in ids]
    for uri in removed:del entries[uri]
    if removed:write_json(state/'measurements.json',entries)
    return len(removed)


def run():
    root,state=paths();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(state/'job.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX);data=read_json(state/'input.json',{})
        job={'running':True,'pid':os.getpid(),'method':METHOD,'phase':'scanning','done':0,'errors':[],
             'analyzed':0,'skipped':0,'reused':0,'removed':0,'target':validate_target(data.get('target',-14))}
        finalized=False
        try:
            os.nice(10)
            while True:
                songs=data['songs'];job.update(done=0,total=len(songs),current='',target=validate_target(data.get('target',-14)))
                for song in songs:
                    if (state/'cancel').exists():raise Cancelled()
                    job.update(current=song['name']);write_json(state/'job.json',job)
                    try:
                        outcome=analyze(song,root,state);job[outcome]+=1
                    except (ValueError,OSError,subprocess.TimeoutExpired) as error:
                        job['errors'].append({'name':song['name'],'error':str(error)[:200]})
                    job['done']+=1;write_json(state/'job.json',job)
                # Prune only after additions/renames have had a chance to reuse cached content.
                job['removed']+=reconcile(songs,state)
                with open(state/'submit.lock','a') as submission:
                    fcntl.flock(submission,fcntl.LOCK_EX)
                    pending=read_json(state/'pending.json',None)
                    if pending:
                        (state/'pending.json').unlink();data=pending;job['errors']=[];continue
                    job.update(running=False,phase='complete',current='');write_json(state/'job.json',job)
                    fcntl.flock(lock,fcntl.LOCK_UN)
                    finalized=True
                    return
        except Cancelled:job.update(running=False,phase='cancelled',current='')
        except Exception as error:job.update(running=False,phase='failed',error=str(error)[:200])
        finally:
            if not finalized:
                with open(state/'submit.lock','a') as submission:
                    fcntl.flock(submission,fcntl.LOCK_EX)
                    job['running']=False;write_json(state/'job.json',job)
                    fcntl.flock(lock,fcntl.LOCK_UN)


if __name__=='__main__' and sys.argv[1:]==['--run']:run()
