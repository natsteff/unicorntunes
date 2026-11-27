'use strict';
const displayMode=location.pathname==='/display';
if(displayMode)document.body.classList.add('display-mode');
let nameResolve=null,configBuilt=false,statsData=null,displayQR=false;
const announcedRequests=new Set();
if(displayMode){
    let blocked=false;
    const block=()=>{
        if(!blocked){
            for(const control of document.querySelectorAll('main button,main input,main select')){
                control.dataset.kioskDisabled=String(control.disabled);control.disabled=true;
            }
            blocked=true;
        }
    };
    window.unlockKiosk=()=>{
        if(!blocked)return;
        for(const control of document.querySelectorAll('[data-kiosk-disabled]')){
            control.disabled=control.dataset.kioskDisabled==='true';delete control.dataset.kioskDisabled;
        }
        blocked=false;updateRequestButtons();
    };
    window.kioskSession=createKioskSession({
        isBusy:()=>busy||window.kioskPending>0,
        reset:done=>api('/api/guest/switch',{kiosk:true}).then(()=>done()).catch(error=>done(error.message)),
        onReset:()=>{
            block();submitted.clear();announcedRequests.clear();offset=0;query='';searchParams='';
            for(const id of ['query','search-title','search-artist','search-album','search-year','guest-input'])$(id).value='';
            $('search-source').value='all';$('search-exact').checked=false;
            for(const id of ['results','queue','now','stats-content','playlist-content','favorite-tracks'])$(id).replaceChildren();
            $('pages').hidden=true;$('favorites-panel').hidden=true;$('searchinfo').textContent='The whole catalog, one good idea at a time.';
            for(const dialog of document.querySelectorAll('dialog[open]'))dialog.close();
            if(nameResolve){nameResolve(false);nameResolve=null;}
            document.activeElement?.blur();$('kiosk-current').textContent='Preparing Kiosk User…';
            message('Preparing the kiosk for the next guest…');
        },
        onReady:()=>{message('Ready. No name needed. The kiosk resets after two minutes without activity.');refresh();},
        onError:error=>{message(error+' Reconnecting before accepting another request.');refresh();}
    });
    $('kiosk-next').hidden=false;$('kiosk-current').hidden=false;
    $('kiosk-next').onclick=()=>window.kioskSession.next();
    $('name-dialog').querySelector('.muted').textContent='Skip to continue as Kiosk User.';
    block();
}

const protectedAction=fn=>async()=>{try{await fn();}catch(e){message(e.message);notify(e.message);}};
let hostInstallOffered=false;
function offerHomeScreenInstall(host=false, manual=false) {
    const dialog = $('install-dialog');
    if (location.pathname !== (host?'/host':'/') || typeof dialog.showModal !== 'function') return;
    const ios = /iPhone|iPad|iPod/i.test(navigator.userAgent) ||
        (navigator.platform === 'MacIntel' && navigator.maxTouchPoints > 1);
    const android = /Android/i.test(navigator.userAgent);
    if ((!ios&&!android) || navigator.standalone ||
        matchMedia('(display-mode: standalone), (display-mode: fullscreen), (display-mode: minimal-ui)').matches) return;

    if(host)$('host-install').hidden=false;
    if(!manual&&((!ios&&!android)||(host&&hostInstallOffered)))return;
    if(dialog.open||document.querySelector('dialog[open]'))return;
    if(host)hostInstallOffered=true;
    const storageKey = host?'unicorntunes-host-install-tip-seen':'unicorntunes-install-tip-seen';
    try {
        if (!manual&&sessionStorage.getItem(storageKey)) return;
        sessionStorage.setItem(storageKey, '1');
    } catch {
        // The instructions still work when browser storage is unavailable.
    }
    if(host){
        $('install-title').textContent='Add Host Desk to your Home Screen';
        $('install-description').textContent='Open the Host Desk directly from your phone. Host sign-in still applies.';
    }
    $('install-ios').hidden=!(ios||(!ios&&!android));
    $('install-android').hidden=!(android||(!ios&&!android));
    dialog.showModal();
}

offerHomeScreenInstall();
if($('host-install'))$('host-install').onclick=()=>offerHomeScreenInstall(true,true);
$('install-dialog').addEventListener('close', () => {
    if(location.pathname==='/'&&state.profile&&!state.profile.named)askName();
});

let editingGuestName=false;
function realGuestName(){
    const profile=state.profile;
    return profile&&(profile.has_name??(profile.named&&profile.name!=='<Shy guy - name not provided>'&&profile.name!=='Kiosk User'))?profile.name:'';
}
function askName(){
    if(!$('name-dialog').open){
        const input=$('guest-input'), name=realGuestName();
        editingGuestName=!!name;
        input.value=name;
        $('skip-name').textContent=editingGuestName?'Cancel':'Skip';
        $('name-dialog').querySelector('.muted').hidden=editingGuestName;
        $('name-dialog').showModal();
        input.focus();input.select();
    }
    return new Promise(resolve=>{nameResolve=resolve;});
}
function cancelName(){
    $('name-dialog').close();
    if(nameResolve){nameResolve(false);nameResolve=null;}
}
async function saveName(value){
    try{
        await api('/api/profile',{name:value});
        $('name-dialog').close();
        if(nameResolve){nameResolve(true);nameResolve=null;}
        await refresh();
    }catch(e){notify(e.message);}
}
$('name-form').onsubmit=e=>{e.preventDefault();saveName($('guest-input').value);};
$('skip-name').onclick=()=>editingGuestName?cancelName():saveName('');
$('name-dialog').addEventListener('cancel',e=>{e.preventDefault();editingGuestName?cancelName():saveName('');});
$('name-dialog').addEventListener('close',()=>{if(nameResolve){nameResolve(false);nameResolve=null;}});
$('guest-name').onclick=()=>askName();
$('edit-guest-name').onclick=()=>askName();
function renderGuestGreeting(){
    const guest=location.pathname==='/';
    const name=realGuestName();
    $('guest-greeting').hidden=!guest;
    $('guest-name').hidden=guest;
    $('guest-search-jump').hidden=!guest;
    $('guest-greeting-name').textContent=name?'Hi, '+name+' ·':'';
    $('edit-guest-name').textContent=name?'Edit name':'Add your name';
    if(guest){
        $('copy-headline').closest('.intro').hidden=false;
        document.body.classList.add('guest-page');
    }
}
async function prepareRequest(){if(displayMode)return window.kioskSession.isReady();if(!state.profile?.named)return askName();return true;}
function setFavoriteButtons(uri,saved){for(const b of document.querySelectorAll('[data-favorite-uri]')){if(b.dataset.favoriteUri===uri){b.disabled=saved;b.textContent=saved?'✓ Already in House Favorites':'Add to House Favorites';}}}
function favoriteButton(song){const b=el('button',song.house_favorite?'✓ Already in House Favorites':'Add to House Favorites','secondary favorite-action');b.dataset.favoriteUri=song.uri;b.disabled=Boolean(song.house_favorite);b.onclick=protectedAction(async()=>{b.disabled=true;try{await api('/api/host/favorite',{uri:song.uri});setFavoriteButtons(song.uri,true);notify('Added to House Favorites.');}catch(e){setFavoriteButtons(song.uri,false);throw e;}});return b;}
async function loadFavorites(){const data=await api('/api/favorites');$('favorite-tracks').replaceChildren();for(const song of data.items){const row=el('div',undefined,'song');row.append(cover(song),info(song,true),el('span',song.plays+' plays','stats-count'),requestButton(song));if(state.host){const b=el('button','Remove favorite','secondary');b.onclick=protectedAction(async()=>{await api('/api/host/favorite',{uri:song.uri,remove:true});setFavoriteButtons(song.uri,false);await loadFavorites();});row.append(b);}$('favorite-tracks').append(row);}if(!data.items.length)$('favorite-tracks').append(el('p','No House Favorites yet. The host can add them from search.','muted'));updateRequestButtons();}
$('open-favorites').onclick=protectedAction(async()=>{$('favorites-panel').hidden=false;await loadFavorites();$('favorites-panel').scrollIntoView({behavior:'smooth',block:'start'});});
$('close-favorites').onclick=()=>$('favorites-panel').hidden=true;
function renderHeadline(node, text, accent, stock=false){
    node.replaceChildren();
    if(stock){
        node.append('What should',document.createElement('br'),'we play ',el('em','next?'));
        return;
    }
    const start=accent?text.indexOf(accent):-1;
    if(start<0){node.textContent=text;return;}
    node.append(text.slice(0,start),el('em',accent),text.slice(start+accent.length));
}
function addHeadlineAccent(grid,input,id,value){
    const accent=field(grid,'Text to highlight in red',id,'text',value||'');
    accent.maxLength=300;
    const preview=el('p',undefined,'headline-preview');
    preview.setAttribute('aria-label','Headline preview');
    const update=()=>renderHeadline(preview,input.value,accent.value);
    input.addEventListener('input',update);accent.addEventListener('input',update);
    grid.append(preview);update();
    return accent;
}
function renderCopy(){
    const c=state.room.copy;
    const text={tagline:c.tagline,headline:state.welcome_headline??c.headline,
        description:c.description,footer:c.footer};
    for(const [key,value] of Object.entries(text)){
        const node=$('copy-'+key);
        const shown=(value||'').trim();
        node.hidden=!shown;
        if(key==='headline')renderHeadline(node,shown,state.headline_accent,
            state.welcome_headline==null&&shown==='What should we play next?'&&!state.headline_accent);
        else node.textContent=shown;
    }
    $('copy-headline').closest('.intro').hidden=['headline','description'].every(key=>!text[key]?.trim());
}
function renderRoom(){if(!state.room)return;if(displayMode)$('kiosk-current').textContent='Current guest: '+state.profile.name;$('queue-policy').textContent=state.room.fair_queue?'Your first request each gathering gets a protected turn. Every third pick goes to the oldest waiting request. The next request is locked.':'Votes order waiting requests. The next selected request and songs handed to Spotify stay locked.';for(const song of state.queue){if(song.mine&&song.protected&&!announcedRequests.has(song.id)&&!displayMode){announcedRequests.add(song.id);notify(song.name+' is the next protected request. Spotify tracks may still play ahead of it.');}}renderCopy();renderGuestGreeting();$('version').textContent='v'+state.version;$('guest-name').textContent=displayMode?(state.profile.named?'Change your name':'Add your name (optional)'):(state.profile.named?state.profile.name:'Set your name');if(!displayMode&&location.pathname!='/host'&&!state.profile.named&&!$('install-dialog').open&&!$('name-dialog').open)askName();$('theme-banner').hidden=!state.room.theme;$('theme-name').textContent=state.room.theme;$('theme-searches').replaceChildren();for(const text of state.room.theme_searches){const b=el('button',text,'secondary');b.onclick=()=>{$('query').value=text;for(const id of ['search-title','search-artist','search-album','search-year'])$(id).value='';$('search-exact').checked=false;$('search').requestSubmit();};$('theme-searches').append(b);}const health=state.handoff;const parts=[health.status,state.accepting?'Requests open':'Requests paused'];if(health.idle)parts.push('Player idle · ready for the next request');else if(!health.player_active)parts.push('No active Spotify player');if(state.host&&health.recovery)parts.push(health.recovery);if(health.stale)parts.push('Playback status is stale');if(health.status==='Needs attention')parts.push(state.host&&location.pathname==='/host'?health.reason:'Requests are waiting for the host to resume playback.');if(state.now&&!state.now.playing&&!health.idle)parts.push('Playback paused');$('playback-health').textContent=parts.join(' · ');$('playback-health').classList.toggle('health-alert',health.stale||health.status==='Needs attention');const upcoming=state.spotify_upcoming;$('spotify-age').textContent=upcoming.error?('Spotify queue unavailable: '+upcoming.error):(upcoming.updated?'Updated '+new Date(upcoming.updated*1000).toLocaleTimeString()+' · refreshes about every 30 seconds':'Waiting for Spotify queue snapshot.');if(upcoming.warning)$('spotify-age').textContent+=' · '+upcoming.warning;$('spotify-tracks').replaceChildren();for(const song of upcoming.items){const r=el('div',undefined,'song');r.append(cover(song),info(song));$('spotify-tracks').append(r);}if(upcoming.updated&&!upcoming.items.length&&!upcoming.error)$('spotify-tracks').append(el('p','Spotify reports no upcoming tracks.','muted'));if(state.now?.requested_by)$('now').append(el('p','Requested by '+state.now.requested_by));if(state.now?.playing&&state.background)$('now').append(el('p','Background music · make a request to join the mix.'));else if(health.idle&&state.now)$('now').append(el('p','Playback finished. The next request will start the music.'));if(state.play_id&&state.now?.playing){const reactions=el('div',undefined,'reactions');const count=state.reactions.reduce((sum,x)=>sum+x.count,0);const playId=state.play_id;const b=el('button',(state.liked?'♥ Liked':'♥ Like this song')+' · '+count,state.liked?'voted':'secondary');b.setAttribute('aria-pressed',String(!!state.liked));b.disabled=!!state.liked;b.onclick=protectedAction(async()=>{await api('/api/react',{reaction:'like',play:playId});await refresh();});reactions.append(b,el('small','Likes appreciate this song; queue upvotes choose what plays next.','muted'));$('now').append(reactions);}if(state.host&&location.pathname==='/host'&&!configBuilt){buildHostConfig();configBuilt=true;}if(!state.host){configBuilt=false;$('room-host-tools').replaceChildren();}if(displayMode&&!displayQR){displayQR=true;api('/api/share').then(share=>{const box=el('div');box.id='display-qr';$('now').parentElement.append(box,el('p','Scan to request from your phone.','muted'));new QRCode(box,{text:share.url,width:120,height:120,correctLevel:QRCode.CorrectLevel.M});}).catch(()=>{displayQR=false;});}}
function field(grid,label,id,type='text',value=''){const wrap=el('label',label);const input=el(type==='textarea'?'textarea':'input');input.id=id;input.name=id;input.autocomplete='off';if(type!=='textarea')input.type=type;if(type==='number'){input.min='0';input.max='1440';input.step='1';}if(type==='checkbox')input.checked=Boolean(value);else input.value=value;wrap.append(input);grid.append(wrap);return input;}
function buildHostConfig(){if(typeof clearHostSections==='function')clearHostSections();const root=$('room-host-tools');root.replaceChildren();const details=el('details');details.id='host-room-config-panel';details.append(el('summary','Queue rules, theme & guest wording'));const form=el('form'),grid=el('div',undefined,'config-grid');form.id='host-room-config';form.name='host-room-config';form.autocomplete='off';const r=state.room;
field(grid,'Fair queue enabled','cfg-fair','checkbox',r.fair_queue);field(grid,'Block explicit / unknown ratings','cfg-explicit','checkbox',r.block_explicit);
field(grid,'Maximum track length (minutes; 0 = unlimited)','cfg-length','number',r.max_minutes);field(grid,'Repeat cooldown (minutes; 0 = off)','cfg-repeat','number',r.repeat_minutes);
for(const id of ['cfg-length','cfg-repeat']){const input=grid.querySelector('#'+id);input.min=0;input.required=true;}
field(grid,'Blocked track names or Spotify URIs (one per line)','cfg-tracks','textarea',r.blocked_tracks.join('\n'));field(grid,'Blocked artist names (one per line)','cfg-artists','textarea',r.blocked_artists.join('\n'));
field(grid,'Theme name','cfg-theme','text',r.theme);field(grid,'Suggested searches (one per line)','cfg-searches','textarea',r.theme_searches.join('\n'));
const appForm=el('form',undefined,'host-card');appForm.id='host-app-config';const appGrid=el('div',undefined,'config-grid');
field(appGrid,'Stats timezone','cfg-timezone','text',r.timezone);
appForm.append(el('p','Leave a field blank to hide it on the guest page.','muted'));
for(const [key,label]of Object.entries({tagline:'Header tagline',headline:'Main headline',description:'Intro description',footer:'Footer tagline'})){const input=field(appGrid,label,'cfg-copy-'+key,'text',r.copy[key]);input.maxLength=300;const list=el('datalist');list.id='copy-options-'+key;for(const text of state.presets[key]||[]){const option=el('option');option.value=text;list.append(option);}input.setAttribute('list',list.id);appGrid.append(list);}
addHeadlineAccent(appGrid,$('cfg-copy-headline')||appGrid.querySelector('#cfg-copy-headline'),'cfg-headline-accent',r.headline_accent);
const roomStatus=el('p',undefined,'muted');
roomStatus.setAttribute('role','status');
form.append(grid,el('p','Rules apply when requesting and when selecting a waiting track. Already handed-off tracks stay protected.','muted'),roomStatus);
bindSettingsAutosave(form,{status:roomStatus,read:()=>{
    const lines=id=>$(id).value.split('\n').map(x=>x.trim()).filter(Boolean);
    return {fair_queue:$('cfg-fair').checked,block_explicit:$('cfg-explicit').checked,max_minutes:$('cfg-length').valueAsNumber,
        repeat_minutes:$('cfg-repeat').valueAsNumber,blocked_tracks:lines('cfg-tracks'),blocked_artists:lines('cfg-artists'),
        theme:$('cfg-theme').value,theme_searches:lines('cfg-searches')};
},apply:value=>api('/api/host/config',value)});
details.append(form);root.append(details);
const appActions=el('div',undefined,'buttons'), appStatus=el('p',undefined,'muted');
appStatus.setAttribute('role','status');
const reset=el('button','Restore default wording','secondary');
reset.type='button';appActions.append(reset);appForm.append(appGrid,appActions,appStatus);
const appAutosave=bindSettingsAutosave(appForm,{status:appStatus,read:()=>{
    const copy={};
    for(const key of ['tagline','headline','description','footer'])copy[key]=$('cfg-copy-'+key).value;
    return {copy,headline_accent:$('cfg-headline-accent').value,timezone:$('cfg-timezone').value};
},apply:value=>api('/api/host/config',value),saved:(_result,value)=>{
    if(value.defaults){configBuilt=false;refresh();}
}});
reset.onclick=()=>appAutosave.submit({defaults:true});
root.append(appForm);
const locations=el('details');locations.id='host-locations-panel';locations.append(el('summary','Saved playback locations'));const locGrid=el('div',undefined,'config-grid');field(locGrid,'Location name','location-name');const saveLoc=el('button','Save selected player here','secondary');saveLoc.onclick=protectedAction(async()=>{await api('/api/host/location',{name:$('location-name').value,device:$('devices').value});configBuilt=false;await refresh();});locGrid.append(saveLoc);for(const [name,device]of Object.entries(r.locations)){const b=el('button','Start / move to '+name,'secondary');b.onclick=protectedAction(async()=>{await api('/api/host/start',{device,accepting:$('accepting').checked});settingsDirty=false;await refresh();});const remove=el('button','Remove '+name,'secondary');remove.onclick=protectedAction(async()=>{await api('/api/host/location',{name,remove:true});configBuilt=false;await refresh();});locGrid.append(b,remove);}locations.append(locGrid);root.append(locations);const fresh=el('button','New gathering','secondary');fresh.onclick=protectedAction(async()=>{if(!confirm('Archive waiting requests, reset protected first turns, and start new Tonight stats? Playback continues; request handoff stops.'))return;await api('/api/host/gathering',{});submitted.clear();await refresh();notify('New gathering started. Start session when ready.');});const gathering=el('section',undefined,'host-card host-gathering');gathering.append(el('h3','New gathering'),el('p','Start a new event: archive waiting requests, reset protected turns and Tonight stats. Music continues, but automatic handoff stops. To resume music, use Start session in Playback.','muted'),fresh);root.append(gathering);const history=el('details');history.id='host-history-panel';history.append(el('summary','Playback history & reset'));history.append(el('p','Only observed UnicornTunes requests count. Resetting clears play history and likes after saving a private database backup; requests, votes, favorites and settings stay intact.','muted'));if(state.history_cleanup?.removed_plays)history.append(el('p','Last reset removed '+state.history_cleanup.removed_plays+' playback records. Backup: '+state.history_cleanup.backup,'muted'));const historyReset=el('button','Back up and reset play history','secondary');historyReset.onclick=protectedAction(async()=>{if(!confirm('Back up the database, then clear played history and likes? Requests, votes, profiles, favorites, playlists and settings will stay intact.'))return;const result=await api('/api/host/history-reset',{});configBuilt=false;await refresh();notify('Play history reset.'+(result.backup?' Backup saved: '+result.backup:''));});history.append(historyReset);root.append(history);if(typeof mountHostSections==='function')mountHostSections();}
$('open-stats').onclick=protectedAction(async()=>{$('stats-dialog').showModal();await loadStats();});$('close-stats').onclick=()=>$('stats-dialog').close();$('stats-period').onchange=protectedAction(loadStats);$('stats-view').onchange=protectedAction(loadStats);
async function loadStats(){let period=$('stats-period').value;const view=$('stats-view').value;if(view==='trending')period='week';if(view==='anthem')period='month';statsData=await api('/api/stats?period='+period);const root=$('stats-content');root.replaceChildren();if(view==='recap'){root.append(el('h2',statsData.recap.plays+' counted plays'),el('p',statsData.recap.guests+' requesting guests · '+statsData.recap.reactions+' likes'));if(statsData.top[0])root.append(el('p','Most played: '+statsData.top[0].name));if(statsData.votes[0])root.append(el('p','Crowd favorite: '+statsData.votes[0].name));}else if(view==='users'){const sort=el('select');sort.setAttribute('aria-label','Rank top users by');for(const [value,label]of [['plays','Songs played'],['votes','Additional votes received']]){const o=el('option',label);o.value=value;sort.append(o);}root.append(sort);const list=el('div');root.append(list);const show=()=>{list.replaceChildren();for(const user of [...statsData.users].sort((a,b)=>b[sort.value]-a[sort.value]||a.name.localeCompare(b.name)).slice(0,40))list.append(el('p',user.name+' · '+user.plays+' plays · '+user.votes+' additional votes'));};sort.onchange=show;show();root.append(el('p','Same provided names are combined. Unnamed guests are excluded.','muted'));}else{let items=statsData[view]||[];if(view==='trending')items=statsData.votes;if(view==='anthem')items=statsData.top.slice(0,1);if(view==='discovery')root.append(el('p','Most additional support for a played request with no earlier counted play in recorded history.','muted'));if(view==='trending')root.append(el('p','Most supported tracks in the last 7 days, ranked by additional votes.','muted'));for(const song of items){const row=el('div',undefined,'song');const text=info(song,true);if(song.requested_by)text.append(el('small','Requested by '+song.requested_by));if(song.started)text.append(el('small',new Date(song.started*1000).toLocaleString()+' · '+song.outcome+(song.qualified?' · counted play':' · not yet counted')));if(song.requested)text.append(el('small',new Date(song.requested*1000).toLocaleString()+' · '+song.state));row.append(cover(song),text);if(song.supporters!==undefined)row.append(el('span',song.supporters+' different supporters','stats-count'));if(song.plays!==undefined)row.append(el('span',song.plays+' plays','stats-count'));if(song.votes!==undefined)row.append(el('span',song.votes+(view==='mine'?' total votes':' additional votes'),'stats-count'));root.append(row);}if(!items.length)root.append(el('p','No recorded activity for this view yet.','muted'));}if(statsData.truncated)root.append(el('p','This view is limited to the latest 5,000 playback observations.','muted'));}
$('export-history').onclick=()=>{if(!statsData)return;const text=statsData.recent.map(s=>new Date(s.started*1000).toISOString()+' | '+s.name+' | '+s.artists+' | '+s.requested_by+' | '+s.outcome).join('\n');const url=URL.createObjectURL(new Blob([text],{type:'text/plain'}));const a=el('a');a.href=url;a.download='unicorntunes-play-history.txt';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);};

$('copy-recap').onclick=protectedAction(async()=>{if(!statsData)return;const d=statsData;await navigator.clipboard.writeText('UnicornTunes · '+$('stats-period').selectedOptions[0].textContent+'\n'+d.recap.plays+' counted plays · '+d.recap.guests+' requesting guests\n'+(d.top[0]?'Most played: '+d.top[0].name+' — '+d.top[0].artists+'\n':'')+(d.votes[0]?'Crowd favorite: '+d.votes[0].name+'\n':''));notify('Recap copied.');});
