/* Host-only navigation. Existing controls and handlers are retained when regrouped. */
(function () {
    'use strict';
    if (location.pathname !== '/host') return;
    document.body.classList.add('host-mode');
    const $ = id => document.getElementById(id);
    const controls = $('controls');
    const saved = {};
    for (const id of ['room-host-tools','local-player-panel','host-playlist-tools','connection','devices','accepting','save','stop','refreshdevices','pause','skip','logout']) saved[id] = $(id);
    const spotifyLink = controls.querySelector('a[href="/api/host/spotify-login"]').parentElement;
    const volume = $('local-volume').closest('.local-field');
    const loudness = controls.querySelector('.local-loudness');
    const recovery = controls.querySelector('.player-recovery');
    const shell = document.createElement('div');
    shell.innerHTML = `
      <div class="host-overview">
        <div><span class="host-eyebrow">SESSION STATUS</span><h3 id="host-state">Checking session…</h3><p id="host-now"></p><p id="host-next-step" role="status"></p></div>
        <div class="host-destination"><span class="host-eyebrow">CURRENT PLAYER</span><strong id="host-current-player">No player selected</strong><span id="host-request-state"></span></div>
      </div>
      <div class="host-navigation" role="tablist" aria-label="Host sections">
        <button id="host-tab-playback" type="button" role="tab" aria-controls="host-panel-playback" aria-selected="true">Playback</button>
        <button id="host-tab-music" type="button" role="tab" aria-controls="host-panel-music" aria-selected="false" tabindex="-1">Music library</button>
        <button id="host-tab-room" type="button" role="tab" aria-controls="host-panel-room" aria-selected="false" tabindex="-1">Room &amp; gathering</button>
        <button id="host-tab-setup" type="button" role="tab" aria-controls="host-panel-setup" aria-selected="false" tabindex="-1">Setup &amp; troubleshooting</button>
      </div>
      <section id="host-panel-playback" class="host-panel" role="tabpanel" aria-labelledby="host-tab-playback" tabindex="0">
        <div class="host-panel-heading"><h3>Everyday playback</h3><p>Start requests, pause the music or skip a track. Setup is only needed when connecting or fixing a player.</p></div>
        <div class="host-playback-grid">
          <div class="host-card"><h4>Player &amp; session</h4><label for="devices">Use this playback device</label><div class="host-device-row"><div id="host-devices-slot"></div><div id="host-refresh-slot"></div></div>
            <label class="host-checkbox" for="accepting"><span id="host-accepting-slot"></span>Accept guest requests</label>
            <p class="muted">Device and request-setting changes apply when you start, move or restart the session.</p>
            <p id="host-pending" role="status" hidden></p><div id="host-start-slot"></div><p id="host-start-help" class="muted"></p>
          </div>
          <div class="host-card"><h4>During playback</h4>
            <div class="host-action-row"><div id="host-pause-slot"></div><p id="host-pause-help">Pause audio and stop automatic request handoff.</p></div>
            <div class="host-action-row"><div id="host-skip-slot"></div><p>Play the next request; if none is waiting, advance Spotify.</p></div>
            <div class="host-action-row"><div id="host-stop-slot"></div><p>Stop automatic handoff. Music already playing continues.</p></div>
          </div>
        </div>
        <div class="host-card"><h4>Background music after requests</h4><p>Choose a Spotify playlist, album or artist. A playing background context is also remembered automatically for this player. After the last request finishes, it starts again from the context's beginning.</p><form id="host-background-form"><label for="host-background-uri">Spotify link or URI</label><input id="host-background-uri" type="text" maxlength="200" placeholder="https://open.spotify.com/playlist/…"><button type="submit" class="secondary">Save background music</button></form><p id="host-background-status" role="status"></p></div>
        <div class="host-help-links"><button type="button" class="secondary" data-host-go="music">Local-file volume &amp; library</button><button type="button" class="secondary" data-host-go="setup">Player setup &amp; connection help</button></div>
      </section>
      <section id="host-panel-music" class="host-panel" role="tabpanel" aria-labelledby="host-tab-music" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Music library</h3><p>Choose playlists for guests, adjust local-file volume and maintain the local library.</p></div>
        <div class="host-card" id="host-volume-slot"><h4>Local-file volume</h4><p class="muted">This controls MPD local files only. It does not change Spotify, the device's overall output or the speaker's own volume.</p></div>
        <div id="host-playlists-slot"></div>
        <details class="host-card host-maintenance"><summary>Local library scan &amp; loudness matching</summary><p class="muted">Use when adding files or calibrating local music. Not needed for everyday Spotify playback.</p><div id="host-loudness-slot"></div></details>
      </section>
      <section id="host-panel-room" class="host-panel" role="tabpanel" aria-labelledby="host-tab-room" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Room &amp; gathering</h3><p>Configure queue rules and guest wording. Start a new gathering only when you want a new event, not to resume music.</p></div><div id="host-room-slot"></div>
      </section>
      <section id="host-panel-setup" class="host-panel" role="tabpanel" aria-labelledby="host-tab-setup" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Setup &amp; troubleshooting</h3><p>Connect accounts and players, manage locations or diagnose a connection. You do not need to revisit this for each listening session.</p></div>
        <div class="host-card" id="host-spotify-slot"><h4>Spotify authorization</h4><p class="muted">Use the same Spotify account as the playback device. Reconnect only when changing accounts, granting permissions or fixing authorization.</p></div>
        <div id="host-local-slot"></div><div class="host-card" id="host-recovery-slot"></div><div id="host-locations-slot"></div>
      </section>
      <div class="host-account-row"><a href="/">Open guest page ↗</a><div id="host-logout-slot"></div></div>`;
    controls.replaceChildren(shell);
    const put = (id, slot) => $(slot).append(saved[id]);
    put('devices','host-devices-slot');put('refreshdevices','host-refresh-slot');
    put('accepting','host-accepting-slot');put('save','host-start-slot');
    put('pause','host-pause-slot');put('skip','host-skip-slot');put('stop','host-stop-slot');
    put('logout','host-logout-slot');put('room-host-tools','host-room-slot');
    put('local-player-panel','host-local-slot');put('host-playlist-tools','host-playlists-slot');
    $('host-volume-slot').append(volume);$('host-loudness-slot').append(loudness);
    $('host-recovery-slot').append(recovery);$('host-spotify-slot').append(spotifyLink,saved.connection);
    $('local-player-panel').querySelector('summary').textContent='Local-file player connection';
    $('host-playlist-tools').querySelector('summary').textContent='Playlists available to guests';
    $('local-config').querySelector('.local-binding-note').textContent='Linked to the player selected in Playback. Save this connection after changing its settings.';
    recovery.querySelector('h3').textContent='Player health & recovery';
    recovery.querySelector('p').textContent='Check health if the player disappears or cannot be controlled. Restarting Soloist may interrupt audio and leaves request handoff stopped.';
    $('refreshdevices').textContent='Refresh players';$('stop').textContent='Stop handoff';
    $('pause').textContent='Pause music';$('skip').textContent='Next track';
    $('logout').textContent='Sign out of host controls';
    const guestHeading = document.createElement('div');guestHeading.className='host-guest-heading';
    guestHeading.innerHTML='<h2>Find music &amp; manage the queue</h2><p>Requests and queue controls are below. Adding a song is separate from starting the session.</p>';
    $('room-toolbar').before(guestHeading);
    // Keep guest tools immediately above search instead of above the host workspace.
    $('host').after(guestHeading,$('room-toolbar'));
    const tabs = Array.from(controls.querySelectorAll('[role="tab"]'));
    function select(name, focus=false) {
        for (const tab of tabs) {
            const selected=tab.id==='host-tab-'+name;
            tab.setAttribute('aria-selected',String(selected));tab.tabIndex=selected?0:-1;
            $(tab.getAttribute('aria-controls')).hidden=!selected;
            if(selected&&focus)tab.focus();
        }
    }
    tabs.forEach((tab,index)=>{
        tab.onclick=()=>select(tab.id.slice('host-tab-'.length));
        tab.onkeydown=event=>{
            let next;
            if(event.key==='ArrowRight')next=(index+1)%tabs.length;
            if(event.key==='ArrowLeft')next=(index+tabs.length-1)%tabs.length;
            if(event.key==='Home')next=0;if(event.key==='End')next=tabs.length-1;
            if(next!==undefined){event.preventDefault();select(tabs[next].id.slice('host-tab-'.length),true);}
        };
    });
    controls.querySelectorAll('[data-host-go]').forEach(button=>{button.onclick=()=>select(button.dataset.hostGo,true);});
    window.clearHostSections=()=>{$('host-locations-panel')?.remove();};
    window.mountHostSections=()=>{
        if($('host-locations-panel'))$('host-locations-slot').append($('host-locations-panel'));
    };
    window.showHostSection=select;
    $('save').setAttribute('aria-describedby','host-start-help');
    window.renderHostDesk=(current,dirty)=>{
        if(!current.host){select('playback');return;}
        const health=current.handoff||{}, running=!!current.enabled;
        $('host-state').textContent=health.status|| (running?'Running':'Stopped');
        const warning=health.status==='Needs attention'||!!current.error||!!health.stale;
        $('host-state').classList.toggle('host-needs-attention',warning);
        $('host-state').classList.toggle('is-running',running&&!warning);
        $('host-state').closest('.host-overview').classList.toggle('has-warning',warning);
        $('host-now').textContent=current.now?(current.now.playing?'Playing: ':'Paused: ')+current.now.name+(current.now.artists?' · '+current.now.artists:''): 'No current track reported.';
        const options=Array.from($('devices').options);
        $('host-current-player').textContent=options.find(option=>option.value===current.device)?.textContent|| (current.device?'Saved player · refresh to check availability':'No player selected');
        $('host-request-state').textContent=current.accepting?'Guest requests open':'Guest requests paused';
        const next=current.error||health.reason||(health.stale?'Playback status is stale. Open Setup & troubleshooting to check player health.':!current.spotify_connected?'Connect Spotify in Setup & troubleshooting.':running?(health.idle?'Ready for the next request.':'Use Pause, Next track or Stop handoff below. No restart needed for each song.'):'Choose a player, then Start session. Any music already playing may continue.');
        $('host-next-step').textContent=next;
        const moving=!!current.device&&!!$('devices').value&&$('devices').value!==current.device;
        $('stop').disabled=!running;
        $('pause').textContent=current.now&&!current.now.playing?'Resume playback':'Pause playback';
        $('pause').disabled=!current.now||health.stale;
        $('host-pause-help').textContent=current.now&&!current.now.playing?'Resume audio only. Start session separately to enable requests.':'Pause audio and stop automatic request handoff.';
        const background=current.background_music||{};
        const backgroundInput=$('host-background-uri');
        if(!backgroundInput.dataset.dirty&&document.activeElement!==backgroundInput)backgroundInput.value=background.device===current.device?background.uri||'':'';
        $('host-background-status').textContent=background.message||health.background_message||(background.uri&&background.device===current.device?'Saved for this player. Host Pause/Stop and server restarts never automatically resume music.':'No background context saved for this player. Choose one to continue music after requests.');
        $('save').textContent=!running?'Start session':moving?'Move session here':dirty?'Apply & restart session':'Restart session';
        $('save').classList.toggle('secondary',running&&!dirty&&!moving);
        $('save').hidden=running&&!dirty&&!moving;
        $('host-pending').hidden=!dirty;
        $('host-pending').textContent='Changes not yet applied. '+(running?'Moving or restarting can affect playback.':'Start the session to apply them.');
        $('host-start-help').textContent=running?(moving?'Move playback and future requests to the selected player.':'Session running. Change the player or request setting above to move or apply changes.'):'Activate the selected player and enable automatic requests. Shuffle and repeat are turned off.';
    };
    $('host-background-uri').addEventListener('input',()=>{$('host-background-uri').dataset.dirty='true';});
    $('host-background-form').onsubmit=async event=>{event.preventDefault();const b=event.currentTarget.querySelector('button');b.disabled=true;try{await api('/api/host/background',{uri:$('host-background-uri').value,device:window.hostDeskState.device});delete $('host-background-uri').dataset.dirty;await refresh();}catch(error){$('host-background-status').textContent=error.message;}finally{b.disabled=false;}};
    $('devices').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
    $('accepting').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
}());
