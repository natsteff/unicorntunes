'use strict';
let hostPlaylistRows=[],hostPlaylistOffset=0,hostPlaylistMore=false,hostPlaylistQuery='',hostPlaylistSource='library',hostPlaylistBusy=false;
let browsePlaylistId='',browsePlaylistOffset=0,browsePlaylistBusy=false,browsePlaylistHost=false;
const sharedPlaylistIds=new Set();
function playlistRow(playlist){const row=el('div',undefined,'song');const details=info(playlist);if(playlist.total!==null&&playlist.total!==undefined)details.append(el('small',playlist.total+' items'));if(playlist.public===false)details.append(el('small','Private playlist'));row.append(cover(playlist),details);return row;}
function playlistError(error){message(error.message);notify(error.message);}
async function refreshEnabledPlaylists(){const data=await api('/api/playlists');sharedPlaylistIds.clear();$('host-playlist-enabled').replaceChildren();for(const p of data.items){sharedPlaylistIds.add(p.id);const row=playlistRow(p);const view=el('button','Browse','secondary');view.onclick=()=>openPlaylist(p.id,true);const remove=el('button','Disable for guests','secondary');remove.onclick=protectedAction(async()=>{await api('/api/host/playlist',{id:p.id,remove:true});await refreshEnabledPlaylists();renderHostPlaylists();});row.append(view,remove);$('host-playlist-enabled').append(row);}if(!data.items.length)$('host-playlist-enabled').append(el('p','No playlists enabled yet.','muted'));return data.items;}
function playlistEnableButton(p){const enabled=sharedPlaylistIds.has(p.id)||p.enabled;const b=el('button',enabled?'✓ Enabled for guests':'Enable guest browsing',enabled?'secondary':'');b.disabled=Boolean(enabled);b.onclick=protectedAction(async()=>{b.disabled=true;try{await api('/api/host/playlist',{id:p.id});p.enabled=true;b.textContent='✓ Enabled for guests';await refreshEnabledPlaylists();renderHostPlaylists();notify('Playlist enabled for guests.');}catch(e){b.disabled=false;throw e;}});return b;}
function renderHostPlaylists(){$('host-playlist-results').replaceChildren();const query=hostPlaylistSource==='library'?$('playlist-query').value.trim().toLocaleLowerCase():'';for(const p of hostPlaylistRows){if(query&&!p.name.toLocaleLowerCase().includes(query))continue;const row=playlistRow(p);const preview=el('button','Preview','secondary');preview.onclick=()=>openPlaylist(p.id,true);p.enabled=sharedPlaylistIds.has(p.id);row.append(preview,backgroundPlaylistButton(p),playlistEnableButton(p));$('host-playlist-results').append(row);}$('host-playlist-more').hidden=!hostPlaylistMore;}
async function loadHostPlaylists(reset=false){if(hostPlaylistBusy)return;hostPlaylistBusy=true;$('host-playlist-status').textContent='Loading playlists…';try{if(reset){hostPlaylistRows=[];hostPlaylistOffset=0;hostPlaylistSource=$('playlist-source').value;hostPlaylistQuery=hostPlaylistSource==='spotify'?$('playlist-query').value.trim():'';if(hostPlaylistSource==='spotify'&&!hostPlaylistQuery)throw Error('Enter a playlist name to search Spotify.');}const data=await api('/api/host/playlists?'+new URLSearchParams({q:hostPlaylistQuery,offset:String(hostPlaylistOffset)}));for(const p of data.items)if(!hostPlaylistRows.some(x=>x.id===p.id))hostPlaylistRows.push(p);hostPlaylistOffset=data.next_offset;hostPlaylistMore=data.more;await refreshEnabledPlaylists();renderHostPlaylists();$('host-playlist-status').textContent=hostPlaylistRows.length+' playlists loaded'+(hostPlaylistMore?' · more available':'');}catch(e){$('host-playlist-status').textContent=e.message;playlistError(e);}finally{hostPlaylistBusy=false;}}
$('host-playlist-search').onsubmit=e=>{e.preventDefault();loadHostPlaylists(true);};$('host-playlist-more').onclick=()=>loadHostPlaylists();$('host-playlist-refresh').onclick=protectedAction(async()=>{await refreshEnabledPlaylists();renderHostPlaylists();});$('playlist-query').oninput=()=>{if(hostPlaylistSource==='library')renderHostPlaylists();};
$('host-playlist-tools').addEventListener('toggle',()=>{if($('host-playlist-tools').open&&state.host)loadHostPlaylists(true);});
$('host-playlist-open').onsubmit=e=>{e.preventDefault();try{const value=$('playlist-link').value.trim();let id=value;if(value.startsWith('spotify:playlist:'))id=value.slice(17);else if(value.startsWith('https://')){const url=new URL(value);if(url.hostname!=='open.spotify.com')throw Error('Use a Spotify playlist link.');const match=url.pathname.match(/\/(?:playlist)\/([A-Za-z0-9]{22})\/?$/);if(!match)throw Error('Use a Spotify playlist link.');id=match[1];}if(!/^[A-Za-z0-9]{22}$/.test(id))throw Error('Enter a valid playlist link or ID.');openPlaylist(id,true);}catch(e){playlistError(e);}};
async function showGuestPlaylists(){browsePlaylistId='';$('playlist-dialog-title').textContent='Playlists';$('playlist-content').replaceChildren();$('playlist-status').textContent='Loading enabled playlists…';$('playlist-back').hidden=true;$('playlist-more').hidden=true;try{const data=await api('/api/playlists');for(const p of data.items){const row=playlistRow(p),b=el('button','Browse tracks');b.onclick=()=>openPlaylist(p.id,false);row.append(b);$('playlist-content').append(row);}$('playlist-status').textContent=data.items.length?'Choose a playlist, then request individual tracks.':'The host has not enabled any playlists yet.';}catch(e){$('playlist-status').textContent=e.message;}}
$('open-playlists').onclick=()=>{if(!$('playlist-dialog').open)$('playlist-dialog').showModal();showGuestPlaylists();};$('close-playlists').onclick=()=>$('playlist-dialog').close();$('playlist-back').onclick=showGuestPlaylists;
async function openPlaylist(id,host=false){if(browsePlaylistBusy)return;browsePlaylistId=id;browsePlaylistOffset=0;browsePlaylistHost=host;$('playlist-content').replaceChildren();if(!$('playlist-dialog').open)$('playlist-dialog').showModal();$('playlist-back').hidden=host;await loadPlaylistTracks(true);}
async function loadPlaylistTracks(first=false){if(browsePlaylistBusy)return;browsePlaylistBusy=true;$('playlist-more').disabled=true;$('playlist-status').textContent='Loading tracks…';try{if(first&&browsePlaylistHost)await refreshEnabledPlaylists();const data=await api('/api/playlist-items?'+new URLSearchParams({id:browsePlaylistId,offset:String(browsePlaylistOffset)}));$('playlist-dialog-title').textContent=data.playlist.name;if(first&&browsePlaylistHost){const box=el('div',undefined,'buttons');box.append(backgroundPlaylistButton(data.playlist),playlistEnableButton(data.playlist));$('playlist-content').append(box);}for(const song of data.items){const row=el('div',undefined,'song'),details=info(song,true);row.append(cover(song),details);if(song.requestable){row.append(requestButton(song));if(state.host&&location.pathname==='/host')row.append(favoriteButton(song));}else{details.append(el('small',song.reason));const unavailable=el('button',song.is_local?'Local file · not verified':'Unavailable','secondary');unavailable.disabled=true;row.append(unavailable);}$('playlist-content').append(row);}browsePlaylistOffset=data.next_offset;$('playlist-more').hidden=!data.more;$('playlist-status').textContent=data.items.length?'Request individual tracks to add them to the shared queue. Local files are shown for inspection; Soloist playback is not verified.':'No more playlist items on this page.';updateRequestButtons();}catch(e){$('playlist-status').textContent=e.message;$('playlist-more').hidden=true;}finally{browsePlaylistBusy=false;$('playlist-more').disabled=false;}}
$('playlist-more').onclick=()=>loadPlaylistTracks();

let backgroundRows=[], backgroundOffset=0, backgroundMore=false, backgroundPickerGeneration=0, backgroundPreset=false, backgroundPickerDevice='';
function backgroundPlaylistButton(playlist){
    const action=el('button','Use for background…','secondary');
    action.onclick=()=>openBackgroundPlaylistPicker('player',playlist);
    return action;
}
function openBackgroundPlaylistPicker(scope, playlist){
    if(location.pathname!=='/host'||!state.host)return;
    backgroundPickerGeneration++;
    backgroundRows=playlist?[playlist]:[];
    backgroundPreset=!!playlist;
    backgroundOffset=0;
    backgroundMore=false;
    backgroundPickerDevice=$('devices').value;
    const dialog=$('background-playlist-dialog');
    $('background-playlist-scope').value=scope;
    $('background-playlist-filter').hidden=backgroundPreset;
    document.querySelector('label[for="background-playlist-filter"]').hidden=backgroundPreset;
    $('background-playlist-filter').value='';
    $('background-playlist-status').textContent='';
    if(!dialog.open)dialog.showModal();
    renderBackgroundPlaylists();
    if(!playlist)loadBackgroundPlaylists();
}
function renderBackgroundPlaylists(){
    const results=$('background-playlist-results');
    results.replaceChildren();
    const filter=$('background-playlist-filter').value.trim().toLocaleLowerCase();
    for(const playlist of backgroundRows){
        if(filter&&!playlist.name.toLocaleLowerCase().includes(filter))continue;
        const row=playlistRow(playlist), save=el('button','Save as background','secondary');
        save.onclick=async()=>{
            save.disabled=true;
            const scope=$('background-playlist-scope').value;
            const device=backgroundPickerDevice;
            const player=$('devices').selectedOptions[0]?.dataset.name;
            const generation=backgroundPickerGeneration;
            try{
                if(scope==='player'&&(!device||device!==$('devices').value))throw Error('Choose the player again before saving.');
                await api('/api/host/background',{uri:'spotify:playlist:'+playlist.id,name:playlist.name,scope,device});
                const field=$(scope==='player'?'host-background-uri':scope==='gathering'?'host-override-uri':'host-house-uri');
                delete field.dataset.dirty;
                await refresh();
                if(generation!==backgroundPickerGeneration)return;
                $('background-playlist-status').textContent='Saved “'+playlist.name+'” as '+(scope==='player'?(player||'the player')+'’s default':scope==='gathering'?'the gathering override':'the house fallback')+'.';
            }catch(error){
                if(generation===backgroundPickerGeneration)$('background-playlist-status').textContent=error.message;
            }finally{save.disabled=false;}
        };
        row.append(save);results.append(row);
    }
    if(!results.children.length&&backgroundRows.length)results.append(el('p','No matches among loaded playlists.','muted'));
    $('background-playlist-more').hidden=!backgroundMore||backgroundPreset;
}
async function loadBackgroundPlaylists(){
    const generation=backgroundPickerGeneration;
    const more=$('background-playlist-more');
    more.disabled=true;
    $('background-playlist-status').textContent='Loading Spotify playlists…';
    try{
        const data=await api('/api/host/playlists?offset='+backgroundOffset);
        if(generation!==backgroundPickerGeneration)return;
        backgroundRows.push(...data.items.filter(item=>!backgroundRows.some(row=>row.id===item.id)));
        backgroundOffset=data.next_offset;
        backgroundMore=data.more;
        renderBackgroundPlaylists();
        $('background-playlist-status').textContent=backgroundRows.length?backgroundRows.length+' playlists loaded.':'No playlists found in this account.';
    }catch(error){
        if(generation===backgroundPickerGeneration)$('background-playlist-status').textContent=error.message;
    }finally{if(generation===backgroundPickerGeneration)more.disabled=false;}
}
$('background-playlist-filter').oninput=renderBackgroundPlaylists;
$('background-playlist-more').onclick=loadBackgroundPlaylists;
