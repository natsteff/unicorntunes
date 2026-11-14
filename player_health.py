"""Narrow playback-device diagnostics and explicit, fixed Soloist recovery."""
import os
import subprocess


def command(args):
    environment=dict(os.environ)
    runtime='/run/user/'+str(os.getuid())
    if os.path.isdir(runtime):
        environment.setdefault('XDG_RUNTIME_DIR',runtime)
        environment.setdefault('DBUS_SESSION_BUS_ADDRESS','unix:path='+runtime+'/bus')
    result=subprocess.run(args,capture_output=True,text=True,timeout=20,env=environment)
    return result


def soloist_status():
    service=command(['systemctl','--user','show','soloist.service','--property=ActiveState,SubState,NRestarts,ActiveEnterTimestamp,Result'])
    fields={}
    allowed={'ActiveState','SubState','NRestarts','ActiveEnterTimestamp','Result'}
    for line in service.stdout.splitlines():
        key,separator,value=line.partition('=')
        if separator and key in allowed:fields[key]=value[:160]
    journal=command(['journalctl','--user','-u','soloist.service','--since','15 minutes ago','-n','100','--no-pager','-o','cat'])
    categories={'authentication':('login failed','authentication failed','unauthorized'),
                'network':('connection failed','connection refused','timed out','network unreachable','disconnected'),
                'discovery':('mdns','zeroconf','discovery failed'),
                'audio':('audio error','failed to open audio','pulse error','alsa error')}
    counts={key:sum(any(term in line.casefold() for term in terms) for line in journal.stdout.splitlines()) for key,terms in categories.items()}
    # Never return journal text, command lines, environment or account tokens.
    discovery={'listeners':None,'soloist_socket_identified':None,'avahi':'unknown'}
    try:
        sockets=command(['ss','-H','-ulnp','sport = :5353'])
        if sockets.returncode==0:
            discovery['listeners']=len(sockets.stdout.splitlines())
            discovery['soloist_socket_identified']='soloist' in sockets.stdout.casefold()
        avahi=command(['systemctl','is-active','avahi-daemon.service'])
        if avahi.stdout.strip() in ('active','inactive','failed'):discovery['avahi']=avahi.stdout.strip()
    except (OSError,subprocess.TimeoutExpired):pass
    return {'discovery':discovery,'service':fields,'recent_log_counts':counts,'diagnostics_available':service.returncode==0,
            'note':'A running service does not prove a healthy Spotify connection. Log counts are hints, not a root-cause diagnosis.'}


def restart_soloist():
    result=command(['systemctl','--user','restart','soloist.service'])
    if result.returncode:raise ValueError('Soloist restart failed. Check its user service on the playback device.')
    return {'restarted':True,'service':soloist_status()['service']}
