#!/usr/bin/env python3
"""Non-destructive, finite background preparation of level-matched MPD copies."""
import fcntl
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import sys
import time

CACHE_FOLDER = '__unicorntunes_normalized'
RECIPE = 1
PEAK = -1.0
CACHE_LIMIT = 4 * 1024**3
FREE_RESERVE = 512 * 1024**2


class Cancelled(Exception):
    pass


def paths():
    return Path.home()/'Music/UnicornTunes', Path.home()/'.local/share/unicorntunes-local/loudness'


def read_json(path, default):
    try: return json.loads(path.read_text())
    except FileNotFoundError: return default


def write_json(path, data):
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
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
    if path.is_absolute() or '..' in path.parts or not path.parts or path.parts[0]==CACHE_FOLDER:
        raise ValueError('Invalid source file.')
    result=(root/relative).resolve()
    if not result.is_relative_to(root.resolve()) or not result.is_file():
        raise ValueError('Source file is unavailable or outside the music directory.')
    return result


def fingerprint(path):
    stat=path.stat()
    return [stat.st_size,stat.st_mtime_ns]


def output_path(root, uri):
    if not re.fullmatch(r'local:[a-f0-9]{64}',str(uri)):raise ValueError('Invalid local track ID.')
    return root/CACHE_FOLDER/(uri[6:]+'.flac')


def prepared(root, state, song, target):
    entry=read_json(state/'manifest.json',{}).get(song['uri'])
    if not entry or entry.get('recipe')!=RECIPE or entry.get('target')!=target:return None
    try:
        source=source_path(root,song['_file']);output=output_path(root,song['uri'])
        if entry.get('source')!=fingerprint(source) or entry.get('output')!=fingerprint(output):return None
        return str(output.relative_to(root))
    except (OSError,ValueError):return None


def status(songs, root=None, state=None):
    if root is None:root,state=paths()
    job=read_json(state/'job.json',{})
    if job.get('running'):
        try:
            pid=int(job['pid']);os.kill(pid,0)
            command=Path(f'/proc/{pid}/cmdline').read_bytes().split(b'\0')
            if str(Path(__file__).resolve()).encode() not in command or b'--run' not in command:
                raise ValueError('Preparation process is no longer running.')
        except (OSError,ValueError,KeyError):
            job=dict(job,running=False,phase='interrupted',error='Preparation stopped. Start again to resume cached work.')
    target=job.get('target',-14.0)
    ready=sum(bool(prepared(root,state,song,target)) for song in songs)
    disk=shutil.disk_usage(root)
    return dict(job,ready=ready,total=len(songs),target=target,ffmpeg=bool(shutil.which('ffmpeg')),
                free_mb=disk.free//1024**2,cache_mb=sum(p.stat().st_size for p in (root/CACHE_FOLDER).glob('*.flac'))//1024**2)


def start(songs, target):
    target=validate_target(target);root,state=paths()
    if not shutil.which('ffmpeg'):raise ValueError('Install ffmpeg on the playback device first: sudo apt install ffmpeg')
    if not songs:raise ValueError('No local songs are indexed.')
    state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(state/'job.lock','a') as lock:
        try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        except BlockingIOError:raise ValueError('Loudness preparation is already running.')
        if status(songs,root,state).get('running'):raise ValueError('Loudness preparation is already running.')
        (state/'cancel').unlink(missing_ok=True)
        write_json(state/'input.json',{'songs':songs,'target':target})
        write_json(state/'job.json',{'running':True,'target':target,'phase':'starting','done':0,'errors':[]})
        process=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--run'],stdin=subprocess.DEVNULL,
                                 stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL,start_new_session=True)
        write_json(state/'job.json',{'running':True,'pid':process.pid,'target':target,'phase':'starting','done':0,'errors':[]})
    return {'started':True,'target':target}


def cancel():
    _,state=paths();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    (state/'cancel').touch(mode=0o600)
    return {'cancel_requested':True}


def run_ffmpeg(args, state):
    if (state/'cancel').exists():raise Cancelled()
    result=subprocess.run(['ffmpeg','-nostdin','-hide_banner','-nostats','-threads','1','-filter_threads','1']+args,
                          capture_output=True,text=True,timeout=1800)
    if (state/'cancel').exists():raise Cancelled()
    if result.returncode:raise ValueError('FFmpeg could not process this audio file.')
    try:
        report=json.loads(result.stderr[result.stderr.rfind('{'):])
        for key in ('input_i','input_tp','input_lra','input_thresh','target_offset'):
            if not math.isfinite(float(report[key])):raise ValueError()
        return report
    except (ValueError,KeyError):raise ValueError('No finite loudness measurement; the file may be silent or unreadable.')


def measure(source, target, state):
    return run_ffmpeg(['-i',str(source),'-map','0:a:0','-vn','-sn','-dn','-af',
                      f'aformat=channel_layouts=stereo,loudnorm=I={target}:TP={PEAK}:LRA=50:print_format=json','-f','null','-'],state)


def prepare(song, target, root, state):
    existing=prepared(root,state,song,target)
    if existing:return 'cached'
    source=source_path(root,song['_file']);before=fingerprint(source)
    duration=song.get('duration_ms',0)/1000
    if not 0<duration<=1800:raise ValueError('Preparation requires a known duration of at most 30 minutes.')
    output=output_path(root,song['uri']);output.parent.mkdir(parents=True,exist_ok=True,mode=0o700)
    if output.parent.is_symlink():raise ValueError('The playback-copy directory must not be a symlink.')
    estimated=int(duration*48000*3*2)+1024**2
    used=sum(p.stat().st_size for p in output.parent.glob('*.flac'))
    if used+estimated>CACHE_LIMIT or shutil.disk_usage(root).free<estimated+FREE_RESERVE:
        raise ValueError('Not enough cache space: 4 GiB copy budget and 512 MiB free reserve required.')
    temporary=output.with_name('.'+output.stem+'.part.flac')
    try:
        first=measure(source,target,state)
        fields=':'.join(f'{key}={first[value]}' for key,value in
                        [('measured_I','input_i'),('measured_TP','input_tp'),('measured_LRA','input_lra'),
                         ('measured_thresh','input_thresh'),('offset','target_offset')])
        report=run_ffmpeg(['-y','-i',str(source),'-map','0:a:0','-vn','-sn','-dn','-map_metadata','-1',
                           '-af',f'aformat=channel_layouts=stereo,loudnorm=I={target}:TP={PEAK}:LRA=50:{fields}:linear=true:print_format=json',
                           '-ar','48000','-ac','2','-c:a','flac','-sample_fmt','s32',str(temporary)],state)
        verified=measure(temporary,target,state)
        output_i=float(verified['input_i']);output_tp=float(verified['input_tp'])
        if abs(output_i-target)>.7 or output_tp>PEAK+.2:
            raise ValueError('The prepared copy did not meet loudness/peak limits; original remains unchanged.')
        if fingerprint(source)!=before:raise ValueError('Source changed during preparation; try again.')
        temporary.chmod(0o600);os.replace(temporary,output)
        manifest=read_json(state/'manifest.json',{})
        manifest[song['uri']]={'recipe':RECIPE,'target':target,'source':before,'output':fingerprint(output),
                               'input_lufs':float(first['input_i']),'output_lufs':output_i,'true_peak':output_tp}
        write_json(state/'manifest.json',manifest)
        return 'prepared'
    finally:temporary.unlink(missing_ok=True)


def mpd_protocol():
    try:
        from mpd_agent import MPD
        return MPD
    except ModuleNotFoundError:
        # The forced SSH command is installed with a hyphenated filename.
        import importlib.util
        spec=importlib.util.spec_from_file_location('unicorntunes_mpd_protocol',Path(__file__).with_name('mpd-agent.py'))
        module=importlib.util.module_from_spec(spec);spec.loader.exec_module(module)
        return module.MPD


def run():
    root,state=paths();state.mkdir(parents=True,exist_ok=True,mode=0o700)
    with open(state/'job.lock','a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX)
        data=read_json(state/'input.json',{});target=validate_target(data['target'])
        songs=data['songs'];job={'running':True,'pid':os.getpid(),'target':target,'phase':'preparing','done':0,'errors':[]}
        try:
            os.nice(10)
            for song in songs:
                if (state/'cancel').exists():raise Cancelled()
                job.update(current=song['name']);write_json(state/'job.json',job)
                try:prepare(song,target,root,state)
                except (ValueError,OSError,subprocess.TimeoutExpired) as error:
                    job['errors'].append({'name':song['name'],'error':str(error)[:200]})
                job['done']+=1;write_json(state/'job.json',job)
            # Index copies before declaring the batch ready for playback.
            with mpd_protocol()() as mpd:
                mpd.command('update',CACHE_FOLDER)
                deadline=time.monotonic()+60
                while dict(mpd.command('status')).get('updating_db'):
                    if time.monotonic()>deadline:raise ValueError('MPD is still indexing copies. Refresh its library before enabling matching.')
                    time.sleep(.25)
            job.update(phase='complete',current='')
        except Cancelled:job.update(phase='cancelled',current='')
        except Exception as error:job.update(phase='failed',error=str(error)[:200])
        finally:job['running']=False;write_json(state/'job.json',job)


if __name__=='__main__' and sys.argv[1:]==['--run']:run()
