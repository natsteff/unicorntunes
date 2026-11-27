'use strict';
let csrf='',state={},offset=0,searchParams=null,searchGeneration=0,libraryPlayer='',devicesLoaded=false,settingsDirty=false;
const $=id=>document.getElementById(id);
const submitted=new Set();let toastTimer;
function notify(text){$('toast').textContent=text;$('toast').hidden=false;clearTimeout(toastTimer);toastTimer=setTimeout(()=>{$('toast').hidden=true;},6000);}
function updateRequestButtons(){for(const b of document.querySelectorAll('[data-request-uri]')){const added=submitted.has(b.dataset.requestUri)||(state.queue||[]).some(s=>s.uri===b.dataset.requestUri)||state.now?.uri===b.dataset.requestUri;if(b.dataset.sending!=='true'){b.disabled=added;b.textContent=added?'✓ Requested':'+ Request';}}}
function requestButton(song){const b=el('button','+ Request','secondary');b.dataset.requestUri=song.uri;b.onclick=async()=>{if(typeof prepareRequest==='function'&&!await prepareRequest())return;b.dataset.sending='true';b.disabled=true;b.textContent='Adding…';try{await api('/api/request',{uri:song.uri});submitted.add(song.uri);b.textContent='✓ Requested';notify(song.name+' added to the UnicornTunes queue.');await refresh();}catch(e){message(e.message);notify(e.message);await refresh();}finally{delete b.dataset.sending;updateRequestButtons();}};return b;}
function message(text){$('message').textContent=text||'';}
window.kioskPending=0;
async function api(path,body){
const kiosk=window.kioskSession;
if(body&&kiosk&&path!=='/api/guest/switch'&&!kiosk.isReady())throw Error('Preparing the kiosk for the next guest. Please wait.');
window.kioskPending++;
try{const response=await fetch(path,{method:body?'POST':'GET',headers:body?{'Content-Type':'application/json','X-FoxTunes-CSRF':csrf}:{},body:body?JSON.stringify(body):undefined});const data=await response.json();if(!response.ok)throw Error(data.error||'Request failed');if(body&&kiosk)kiosk.activity();return data;}
finally{window.kioskPending--;}}

function el(tag,text,cls){const n=document.createElement(tag);if(text!==undefined)n.textContent=text;if(cls)n.className=cls;return n;}
function safeURL(value,art=false){if(art&&/^\/api\/local-art\/[a-f0-9]{64}(\?v=2)?$/.test(value))return value;try{const url=new URL(value);return url.protocol==='https:'&&(art?['i.scdn.co','mosaic.scdn.co','image-cdn-ak.spotifycdn.com'].includes(url.hostname):url.hostname==='open.spotify.com')?url.href:'';}catch{return '';}}
function cover(song){const placeholder=()=>{const n=el('div','♫','cover cover-placeholder');n.setAttribute('aria-hidden','true');return n;};const src=safeURL(song.source==='local'?'/api/local-art/'+song.uri.slice(6)+'?v=2':song.art,true);if(!src)return placeholder();const img=el('img',undefined,'cover');img.alt='';img.src=src;img.onerror=async()=>{img.onerror=null;const fallback=placeholder();fallback.title='Cover unavailable';img.replaceWith(fallback);if(state.host&&song.source==='local'){try{const response=await fetch(src);if(!response.ok){const error=await response.json();fallback.title=error.error||'Cover unavailable';}}catch{}}};return img;}
function info(song,metadata=false){const wrap=el('div',undefined,'songinfo'),link=el(song.source==='local'?'span':'a',song.name);link.href=safeURL(song.url)||'#';link.target='_blank';link.rel='noopener noreferrer';wrap.append(link,el('p',song.artists));if(song.source==='local')wrap.append(el('span','Local file','source-badge'));if(metadata){const duration=song.duration_ms?Math.floor(song.duration_ms/60000)+':'+String(Math.floor(song.duration_ms/1000)%60).padStart(2,'0'):'';const details=[song.album,song.year,duration].filter(Boolean).join(' · ');if(details)wrap.append(el('p',details,'songmeta'));}return wrap;}
function button(text,fn,cls){const b=el('button',text,cls);b.onclick=async()=>{b.disabled=true;try{await fn();message('');await refresh();}catch(e){message(e.message);}finally{b.disabled=false;}};return b;}
let refreshInFlight;
async function refresh(){if(refreshInFlight)return refreshInFlight;refreshInFlight=refreshState();try{return await refreshInFlight;}finally{refreshInFlight=null;}}
async function refreshState(){try{state=await api('/api/state');csrf=state.csrf;if(window.kioskSession&&!window.kioskSession.ensure(csrf))return;render();if(window.unlockKiosk)window.unlockKiosk();}catch(e){message(e.message);}}
let actionRefreshTimer;
async function refreshAfterPlaybackAction(){clearTimeout(actionRefreshTimer);await refresh();let remaining=6;const tick=async()=>{if(!state.host||document.hidden)return;await refresh();if(--remaining>0)actionRefreshTimer=setTimeout(tick,1000);};actionRefreshTimer=setTimeout(tick,1000);}
function render(){for(const uri of submitted)if(!state.queue.some(s=>s.uri===uri)&&state.now?.uri!==uri)submitted.delete(uri);updateRequestButtons();const now=$('now');now.replaceChildren();if(state.now){const song=state.now;const title=el('h2'),link=el(song.source==='local'?'span':'a',song.name);link.href=safeURL(song.url)||'#';link.target='_blank';link.rel='noopener noreferrer';title.append(link);if(song.source==='local')now.append(el('span','Local file','source-badge'));now.append(cover(song),title,el('p',song.artists),el('p',song.playing?'Now playing':(state.handoff?.idle?'Waiting for the next request':'Playback paused')));const bar=el('div',undefined,'nowprogress'),fill=el('div');fill.style.width=Math.min(100,Math.max(0,100*(song.progress_ms||0)/(song.duration_ms||1)))+'%';bar.append(fill);now.append(bar);}else now.append(el('h2','Waiting for the music'),el('p','The host will get things started.'));
const queue=$('queue');queue.replaceChildren();$('count').textContent=state.queue.length+' REQUEST'+(state.queue.length===1?'':'S');for(const [index,song]of state.queue.entries()){const row=el('div',undefined,'song'+(song.mine?' my-request':''));row.append(el('span',String(index+1).padStart(2,'0'),'rank'),cover(song));const details=info(song);details.append(el('small','Requested by '+song.requested_by));if(song.mine)details.append(el('small','Your request','ownership'));if(song.upvoted_by?.length)details.append(el('small','Upvoted by '+song.upvoted_by.join(', ')));if(song.protected)details.append(el('small','Next request · protected','protected-label'));else if(song.guaranteed&&state.room?.fair_queue)details.append(el('small','Protected first turn'));if(song.state!=='waiting')details.append(el('small',{'accepted':song.source==='local'?'Starting local playback · locked':'Handed to Spotify · locked','queued':'In Spotify queue · locked','playing':'Playing','uncertain':'Awaiting host review','sending':'Sending · locked'}[song.state]||song.state));row.append(details);const vote=button((song.voted?'✓ ':'↑ ')+song.votes,()=>api('/api/vote',{id:song.id}),song.voted?'voted':'secondary');vote.setAttribute('aria-label',(song.voted?'Voted for ':'Vote for ')+song.name);vote.disabled=song.voted||song.state!=='waiting';row.append(vote);if(state.host&&song.state==='waiting')row.append(button('Remove',()=>api('/api/host/remove',{id:song.id}),'secondary'));if(state.host&&['uncertain','accepted','queued'].includes(song.state))row.append(button('Reconcile',async()=>{const note=prompt('Check the actual player and queue first. Describe what you found. A local track will be stopped before releasing its request; Spotify entries must be checked separately.');if(note)await api('/api/host/reconcile',{id:song.id,note});},'secondary'));queue.append(row);}if(!state.queue.length)queue.append(el('p',state.now?.playing?'Background music is playing. Request something you love.':(state.handoff?.idle?'Player ready. Request a song to start the music.':'An open queue. A world of possibilities.'),'empty'));
if(state.handoff)$('handoffstatus').textContent=state.handoff.status+(state.handoff.reason?' — '+state.handoff.reason:'');if(state.host){$('save').textContent=state.enabled?'Move / restart session here':'Start session';$('stop').disabled=!state.enabled;$('pause').textContent=state.now&&!state.now.playing?'Resume playback':'Pause playback';$('pause').disabled=!state.now||state.handoff?.stale;$('connection').textContent=state.spotify_connected?'Spotify authorization saved.':'Connect Spotify before selecting a playback device.';if(!$('accepting').disabled)$('accepting').checked=state.accepting;if(!settingsDirty&&state.device&&Array.from($('devices').options).some(option=>option.value===state.device))$('devices').value=state.device;}if(location.pathname==='/host'){$('browse-local').hidden=!state.host;invalidateLocalBrowse();$('host').hidden=false;$('login').hidden=state.host;$('controls').hidden=!state.host;if(state.host&&!devicesLoaded)loadDevices();if(state.host&&typeof renderLocalPlayer==='function')renderLocalPlayer();}if(state.error&&state.host&&location.pathname==='/host')message(state.error);if(typeof renderRoom==='function')renderRoom();if(typeof renderHostDesk==='function'){window.hostDeskState=state;renderHostDesk(state,settingsDirty);}}
async function loadDevices(){devicesLoaded=true;try{const data=await api('/api/host/devices');const previous=$('devices').value;$('devices').replaceChildren();for(const d of data.devices){if(d.is_restricted)continue;const o=el('option',d.name+(d.is_active?' · active':''));o.value=d.id;o.dataset.name=d.name;o.dataset.volume=d.volume_percent??'';o.dataset.volumeSupported=String(d.supports_volume===true);$('devices').append(o);if(d.id===state.device||(!state.device&&d.is_active))o.selected=true;}if(settingsDirty&&Array.from($('devices').options).some(option=>option.value===previous))$('devices').value=previous;invalidateLocalBrowse();if(!$('accepting').disabled)$('accepting').checked=state.accepting;if(typeof renderHostDesk==='function')renderHostDesk(state,settingsDirty);}catch(e){devicesLoaded=false;message(e.message);}}
function resetSearchResults(){
    searchGeneration++;
    $('results').replaceChildren();
    $('pages').hidden=true;
    $('searchinfo').textContent='The whole catalog, one good idea at a time.';
}
async function search(){
    const generation=++searchGeneration;
    const limit=Number($('search-limit').value);
    const params=new URLSearchParams(searchParams);
    params.set('offset',offset);
    params.set('limit',limit);
    const player=libraryPlayer;
    const label=player?'Local files — '+($('devices').selectedOptions[0]?.dataset.name||'Selected player')+' · ':'';
    if(player)params.set('player',player);
    $('searchinfo').textContent=label+'Searching…';
    $('previous').disabled=$('next').disabled=true;
    try{
        const data=await api('/api/search?'+params);
        if(generation!==searchGeneration)return;
        $('results').replaceChildren();
        for(const song of data.items){
            const row=el('div',undefined,'song');
            row.append(cover(song),info(song,true),requestButton(song));
            if(state.host&&location.pathname==='/host'&&typeof favoriteButton==='function')row.append(favoriteButton(song));
            $('results').append(row);
        }
        updateRequestButtons();
        const count=data.total==null?'Total unavailable':`${data.total} result${data.total===1?'':'s'}`;
        const empty=data.items.length?'':data.more?' · No matches on this page':' · No tracks found on this page';
        $('searchinfo').textContent=label+`${count} · Page ${Math.floor(offset/limit)+1} (${limit} per page)`+empty;
        $('pages').hidden=false;
        $('previous').disabled=offset===0;
        $('next').disabled=!data.more;
        message(data.warning||'');
    }catch(error){
        if(generation!==searchGeneration)return;
        $('results').replaceChildren();
        $('pages').hidden=true;
        $('searchinfo').textContent=label+error.message;
    }
}
function submitSearch(player=''){
    libraryPlayer=player;
    searchParams=new URLSearchParams({
        q:$('query').value.trim(),source:$('search-source').value,track:$('search-title').value.trim(),
        artist:$('search-artist').value.trim(),album:$('search-album').value.trim(),
        year:$('search-year').value.trim(),exact:$('search-exact').checked?'1':'0'
    });
    offset=0;
    resetSearchResults();
    search();
}
$('search').onsubmit=event=>{
    event.preventDefault();
    const selectedLocal=location.pathname==='/host'&&state.host&&$('search-source').value==='local';
    if(selectedLocal&&!$('devices').value){message('Choose a playback device first.');return;}
    submitSearch(selectedLocal?$('devices').value:'');
};
$('search-limit').onchange=()=>{if(searchParams){offset=0;search();}};
$('previous').onclick=()=>{offset=Math.max(0,offset-Number($('search-limit').value));search();};
$('next').onclick=()=>{offset+=Number($('search-limit').value);search();};
function invalidateLocalBrowse(){
    if(libraryPlayer&&(!state.host||libraryPlayer!==$('devices').value)){
        libraryPlayer='';
        searchParams=null;
        resetSearchResults();
    }
}
$('browse-local').onclick=()=>{
    if(!state.host||!$('devices').value){message('Choose a playback device first.');return;}
    $('search').reset();
    $('search-source').value='local';
    submitSearch($('devices').value);
};
$('devices').addEventListener('change',invalidateLocalBrowse);
$('login').onsubmit=async e=>{e.preventDefault();try{await api('/api/host/login',{password:$('password').value});$('password').value='';devicesLoaded=false;message('');await refresh();}catch(e){message(e.message);}};
$('devices').onchange=()=>{settingsDirty=$('devices').value!==state.device;};
$('accepting').onchange=async event=>{
    const checkbox=event.currentTarget;
    checkbox.disabled=true;
    try{
        await api('/api/host/accepting',{accepting:checkbox.checked});
        await refresh();
        notify('Guest request setting saved.');
    }catch(error){notify(error.message);}
    finally{checkbox.checked=state.accepting;checkbox.disabled=false;}
};
$('save').onclick=async()=>{let error='';$('save').disabled=true;message('Starting player…');try{await api('/api/host/start',{device:$('devices').value,accepting:$('accepting').checked});}catch(e){error=e.message;}settingsDirty=false;await refreshAfterPlaybackAction();message(error||(state.enabled?'Session running. Requests will play through the selected player.':''));$('save').disabled=state.handoff?.status==='Starting';};
$('stop').onclick=async()=>{try{await api('/api/host/stop',{});await refreshAfterPlaybackAction();message('Session stopped. No new requests will be sent; current music continues.');}catch(e){message(e.message);}};
$('refreshdevices').onclick=()=>loadDevices();
for(const action of ['pause','skip'])$(action).onclick=async()=>{const command=action==='pause'&&state.now&&!state.now.playing?'resume':action;const prompt=command==='resume'?'Resume audio on the selected player? Request handoff stays stopped.':command==='pause'?'Pause audio and stop request handoff?':'Play the next request or continue with background music?';if(!confirm(prompt))return;$(action).disabled=true;try{await api('/api/host/'+command,{});message(command==='resume'?'Resume requested. Checking playback; request handoff remains stopped.':command==='skip'?'Next track requested. Checking playback.':'Pause requested. Checking playback; request handoff stopped.');}catch(e){message(e.message);}finally{await refreshAfterPlaybackAction();}};
$('logout').onclick=async()=>{await api('/api/host/logout',{});devicesLoaded=false;await refresh();};
refresh();setInterval(refresh,5000);

$('show-qr').onclick=async()=>{try{const share=await api('/api/share');const url=new URL(share.url);if(!['http:','https:'].includes(url.protocol))throw Error('Invalid server URL.');$('share-url').textContent=url.href;$('share-url').href=url.href;$('qrcode').replaceChildren();new QRCode($('qrcode'),{text:url.href,width:240,height:240,correctLevel:QRCode.CorrectLevel.M});$('share-dialog').showModal();}catch(e){message(e.message);}};
$('close-share').onclick=()=>$('share-dialog').close();

// Search filters are transient. Do not restore positional form values from the host configuration.
window.addEventListener('pageshow',()=>{$('search').reset();});

let localLoaded=false;
function renderLocalPlayer(){if(localLoaded||!state.local_player)return;const c=state.local_player;$('local-enabled').checked=c.enabled;for(const key of ['host','user','port','volume'])$('local-'+key).value=(key==='volume'&&c.normalization_enabled?c.normalized_volume:c[key])??'';$('loudness-enabled').checked=!!c.normalization_enabled;$('loudness-target').value=String(c.loudness_target??-14);$('loudness-output').value=c.normalized_volume??100;$('local-status').textContent=c.count+' tracks indexed';localLoaded=true;}
const localFields=['enabled','host','user','port'].map(key=>$('local-'+key));
function localConnection(){
    const config=state.local_player||{};
    return {enabled:$('local-enabled').checked,host:$('local-host').value,user:$('local-user').value,
        port:$('local-port').valueAsNumber,volume:config.normalization_enabled?config.normalized_volume:config.volume,
        device:$('devices').value};
}
const localConfigAutosave=bindSettingsAutosave($('local-config'),{
    status:$('local-config-save-status'),controls:localFields,
    read:()=>state.local_player?.device===$('devices').value?localConnection():null,
    apply:value=>api('/api/host/local-config',value)
});
$('local-bind').onclick=()=>{
    const body=localConnection();
    if(!body.device||!confirm('Link the local connection to the selected playback player?'))return;
    localConfigAutosave.submit(body);
};
for(const action of ['test','scan'])$('local-'+action).onclick=async()=>{const b=$('local-'+action);b.disabled=true;$('local-status').textContent=action==='scan'?'Scanning…':'Connecting…';try{const result=await api('/api/host/local-'+action,{});$('local-status').textContent=action==='scan'?result.count+' tracks indexed'+(result.loudness_error?' · analysis: '+result.loudness_error:result.loudness?.queued?' · incremental analysis queued':' · incremental analysis started'):'Connected · MPD '+result.state+' · local volume '+result.volume+(result.agent_version>=6?'':' · update the Pine64 agent for cached-volume analysis');if(action==='scan')await refreshLoudness();}catch(error){$('local-status').textContent=error.message;}finally{b.disabled=false;}};

$('local-volume-apply').onclick=async()=>{if(!$('local-volume').reportValidity())return;const b=$('local-volume-apply');b.disabled=true;try{const result=await api('/api/host/local-volume',{volume:Number($('local-volume').value)});if(state.local_player?.normalization_enabled)$('loudness-output').value=result.volume;$('local-status').textContent=(result.applied_volume!==undefined?'Local reference '+result.volume+' · MPD applied '+result.applied_volume:'Local volume saved: '+result.volume)+(result.live?' · applied to current local track.':' · applies to the next local track.');}catch(error){$('local-status').textContent=error.message;}finally{b.disabled=false;}};

let loudnessTimer;
async function refreshLoudness(){clearTimeout(loudnessTimer);try{const s=await api('/api/host/loudness-status',{});$('loudness-status').textContent=(s.ffmpeg?'':'Install ffmpeg on the playback device first. ')+(s.ready??0)+' / '+(s.total??0)+' files have valid analysis'+(s.running?' · Processing '+(s.current||'library'):(s.phase?' · '+s.phase:''))+(s.error?' · '+s.error:'')+' · '+(s.cache_bytes??0)+' bytes of cached measurements';$('loudness-errors').replaceChildren();$('loudness-status').textContent+=' · '+(s.analyzed??0)+' analyzed, '+(s.skipped??0)+' skipped, '+(s.reused??0)+' reused, '+(s.removed??0)+' removed';for(const item of (s.limited||[]).slice(0,8))$('loudness-errors').append(el('p',item.name+': '+item.reason+' · estimated '+item.estimated_lufs+' LUFS','muted'));for(const error of (s.errors||[]).slice(0,8))$('loudness-errors').append(el('p',error.name+': '+error.error));if(s.running)loudnessTimer=setTimeout(refreshLoudness,10000);}catch(error){$('loudness-status').textContent=error.message;}}
$('loudness-refresh').onclick=refreshLoudness;
$('loudness-cancel').onclick=async()=>{try{await api('/api/host/loudness-cancel',{});$('loudness-status').textContent='Stop requested. The current file-analysis pass will finish first.';}catch(error){$('loudness-status').textContent=error.message;}};
bindSettingsAutosave($('loudness-config'),{
    status:$('loudness-save-status'),
    read:()=>({enabled:$('loudness-enabled').checked,target:Number($('loudness-target').value),
        volume:$('loudness-output').valueAsNumber,device:$('devices').value}),
    apply:value=>api('/api/host/loudness-config',value),
    saved:result=>{
        if(result.enabled)$('local-volume').value=result.volume;
        else {localLoaded=false;renderLocalPlayer();}
        refreshLoudness();
    }
});

$('player-diagnostics').onclick=async()=>{const b=$('player-diagnostics');b.disabled=true;try{const d=await api('/api/host/player-diagnostics',{});$('player-health-status').textContent='Soloist: '+(d.service?.ActiveState||'unknown')+' / '+(d.service?.SubState||'unknown')+' · Spotify device '+(d.spotify_device_available?'available':'missing')+' · service restarts: '+(d.service?.NRestarts??'unknown')+'. mDNS listeners: '+(d.discovery?.listeners??'unknown')+'; Soloist socket identified: '+(d.discovery?.soloist_socket_identified===null?'unknown':d.discovery?.soloist_socket_identified?'yes':'no')+'; Avahi: '+(d.discovery?.avahi||'unknown')+'. Recent log hints: '+Object.entries(d.recent_log_counts||{}).map(([k,v])=>k+' '+v).join(', ')+'. '+d.note;}catch(error){$('player-health-status').textContent=error.message;}finally{b.disabled=false;}};
$('player-restart').onclick=async()=>{if(!confirm('Restart Soloist on the linked playback device? Spotify audio may stop. The session will remain stopped until you refresh devices and start it again.'))return;const b=$('player-restart');b.disabled=true;try{await api('/api/host/player-restart',{});await refresh();await loadDevices();$('player-health-status').textContent='Restart requested. Give Spotify a moment, then refresh devices and start the session once the player is available.';}catch(error){$('player-health-status').textContent=error.message;}finally{b.disabled=false;}};
