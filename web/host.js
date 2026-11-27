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
      <div class="host-help-links"><button id="host-help" type="button" class="secondary" aria-controls="host-start-guide">Help</button><p id="host-start-reminder" class="muted"></p></div>
      <details id="host-start-guide" class="host-card"><summary>How to Start a Session</summary>
        <ol><li>Sign into the Host desk and turn on the playback player and speakers.</li>
        <li>Refresh players and select the room’s player. For the garage, choose UnicornPlayer - Garage.</li>
        <li>Check the Spotify account. Save background music in Player settings or add a song to the queue.</li>
        <li>Choose Start session, then check that it says Running and you can hear music. If it is already running, leave it running.</li></ol>
        <p>If the player is missing or there is no sound, check power, speaker volume and the connection.</p>
        <button type="button" class="secondary" data-host-go="setup">Open Setup &amp; troubleshooting</button>
      </details>
      <div class="host-overview">
        <div><span class="host-eyebrow">SESSION STATUS</span><h3 id="host-state">Checking session…</h3><p id="host-now"></p><p id="host-next-step" role="status"></p></div>
        <div class="host-destination"><span class="host-eyebrow">CURRENT PLAYER</span><strong id="host-current-player">No player selected</strong><span id="host-request-state"></span><p id="host-account-summary" class="muted">Spotify account: checking…</p></div>
      </div>
      <div class="host-navigation" role="tablist" aria-label="Host sections">
        <button id="host-tab-playback" type="button" role="tab" aria-controls="host-panel-playback" aria-selected="true">Playback</button>
        <button id="host-tab-music" type="button" role="tab" aria-controls="host-panel-music" aria-selected="false" tabindex="-1">Music library</button>
        <button id="host-tab-room" type="button" role="tab" aria-controls="host-panel-room" aria-selected="false" tabindex="-1">Room &amp; gathering</button>
        <button id="host-tab-player" type="button" role="tab" aria-controls="host-panel-player" aria-selected="false" tabindex="-1">Player settings</button>
        <button id="host-tab-application" type="button" role="tab" aria-controls="host-panel-application" aria-selected="false" tabindex="-1">UnicornTunes settings</button>
        <button id="host-tab-setup" type="button" role="tab" aria-controls="host-panel-setup" aria-selected="false" tabindex="-1">Setup &amp; troubleshooting</button>
      </div>
      <section id="host-panel-playback" class="host-panel" role="tabpanel" aria-labelledby="host-tab-playback" tabindex="0">
        <div class="host-panel-heading"><h3>Everyday playback</h3><p>Start requests, pause the music or skip a track. Setup is only needed when connecting or fixing a player.</p></div>
        <div class="host-playback-grid">
          <div class="host-card"><h4>Player &amp; session</h4><label for="devices">Use this playback device</label><div class="host-device-row"><div id="host-devices-slot"></div><div id="host-refresh-slot"></div></div>
            <p id="host-playback-account" class="muted"></p><label class="host-checkbox" for="accepting"><span id="host-accepting-slot"></span>Accept guest requests</label>
            <p class="muted">Device and request-setting changes apply when you start, move or restart the session.</p>
            <p id="host-pending" role="status" hidden></p><div id="host-start-slot"></div><p id="host-start-help" class="muted"></p>
          </div>
          <div class="host-card"><h4>During playback</h4>
            <div class="host-action-row"><div id="host-pause-slot"></div><p id="host-pause-help">Pause audio and stop automatic request handoff.</p></div>
            <div class="host-action-row"><div id="host-skip-slot"></div><p>Play the next request; if none is waiting, advance Spotify.</p></div>
            <div class="host-action-row"><div id="host-stop-slot"></div><p>Stop automatic handoff. Music already playing continues.</p></div>
          </div>
        </div>
        <p id="host-background-status" role="status"></p>
        <div class="host-help-links"><button type="button" class="secondary" data-host-go="player">Player settings</button><button type="button" class="secondary" data-host-go="setup">Player setup &amp; connection help</button></div>
      </section>
      <section id="host-panel-music" class="host-panel" role="tabpanel" aria-labelledby="host-tab-music" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Music library</h3><p>Choose playlists for guests and maintain the local library.</p></div>

        <div id="host-playlists-slot"></div>
        <details class="host-card host-maintenance"><summary>Local library scan &amp; loudness matching</summary><p class="muted">Use when adding files or calibrating local music. Not needed for everyday Spotify playback.</p><div id="host-loudness-slot"></div></details>
      </section>
      <section id="host-panel-room" class="host-panel" role="tabpanel" aria-labelledby="host-tab-room" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Room &amp; gathering</h3><p>Configure queue rules, themes and this gathering’s background music.</p></div><div class="host-card"><h4>Background music for this gathering</h4><p>Overrides the selected player’s default until you restore it or start a new gathering.</p>
        <form id="host-gathering-background"><label for="host-override-uri">Spotify link or URI</label><input id="host-override-uri" maxlength="200"><button type="submit" class="secondary">Save gathering override</button> <button id="host-restore-background" type="button" class="secondary">Restore player default</button></form><p id="host-gathering-background-status" role="status"></p></div>
        <div id="host-room-slot"></div>
      </section>
      <section id="host-panel-player" class="host-panel" role="tabpanel" aria-labelledby="host-tab-player" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Player settings</h3><p>Choose the player you want to configure in Playback.</p></div>
        <div class="host-card"><h4>Default background music</h4><p id="host-default-player"></p>
        <form id="host-background-form"><label for="host-background-uri">Spotify playlist, album or artist link or URI</label><input id="host-background-uri" maxlength="200"><button type="submit" class="secondary">Save player default</button></form><p id="host-default-status" role="status"></p></div>
        <div class="host-card"><h4>Guest welcome heading</h4><p>Leave blank to use the UnicornTunes default heading.</p>
        <form id="host-player-headline-form"><label for="host-player-headline">Heading for this player</label><input id="host-player-headline" maxlength="300"><button type="submit" class="secondary">Save welcome heading</button></form><p id="host-player-headline-status" role="status"></p></div>
        <div class="host-card" id="host-volume-slot"><h4>Local-file volume</h4><p class="muted">Controls audio played through MPD.</p></div>
        <div id="host-local-slot"></div><div id="host-locations-slot"></div>
      </section>
      <section id="host-panel-application" class="host-panel" role="tabpanel" aria-labelledby="host-tab-application" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>UnicornTunes settings</h3><p>Set the guest page wording and stats timezone.</p></div>
        <div id="host-app-settings-slot"></div>
      </section>
      <section id="host-panel-setup" class="host-panel" role="tabpanel" aria-labelledby="host-tab-setup" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Setup &amp; troubleshooting</h3><p>Check Spotify authorization and diagnose playback connections.</p></div>
        <div class="host-card" id="host-spotify-slot"><h4>Spotify authorization</h4><p class="muted">Use the same Spotify account as the playback device. Reconnect only when changing accounts, granting permissions or fixing authorization.</p></div>
        <div class="host-card"><h4>Connected Spotify account</h4><p id="host-account-details" role="status">Checking authorization…</p><p class="muted">This is UnicornTunes' authorized Spotify account. It does not independently verify Soloist pairing. Account checks are cached for up to five minutes.</p><button id="host-account-refresh" type="button" class="secondary">Check account</button></div><div class="host-card"><h4>Playback diagnostics</h4><p>Download recent actions, transitions and failures for troubleshooting. Credentials, account names and raw Spotify responses are omitted. Track/player identifiers are hashed. Reports cover recent activity since this container started.</p><button id="host-diagnostics-download" type="button" class="secondary">Download diagnostic report</button><p id="host-diagnostics-status" role="status"></p></div><div class="host-card" id="host-recovery-slot"></div><button type="button" class="secondary" data-host-go="player">Open player settings</button>
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
    window.clearHostSections=()=>{$('host-locations-panel')?.remove();$('host-app-config')?.remove();};
    window.mountHostSections=()=>{
        if($('host-locations-panel'))$('host-locations-slot').append($('host-locations-panel'));
        if($('host-app-config'))$('host-app-settings-slot').append($('host-app-config'));
    };
    $('host-help').onclick=()=>{
        const guide=$('host-start-guide');
        guide.open=true;
        guide.querySelector('summary').focus();
    };
    window.showHostSection=select;
    $('save').setAttribute('aria-describedby','host-start-help');
    window.renderHostDesk=(current,dirty)=>{
        $('host-signed-in').hidden=!current.host;
        if(!current.host){select('playback');accountLoaded=false;clearTimeout(accountTimer);showAccount({status:'unavailable'});return;}
        if(!accountLoaded)checkAccount();
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
        $('host-start-reminder').textContent=running?'Session running.':($('devices').value?'Check speakers and select Start session when ready.':'Power on the player and refresh the list.');
        const background=current.background_music||{};
        const selected=$('devices').value||current.device;
        const backgroundInput=$('host-background-uri');
        if(backgroundInput.dataset.device!==selected){
            delete backgroundInput.dataset.dirty;
            backgroundInput.dataset.device=selected||'';
        }
        if(!backgroundInput.dataset.dirty)backgroundInput.value=background.defaults?.[selected]?.uri||'';
        const headline=$('host-player-headline');
        if(headline.dataset.device!==selected){
            delete headline.dataset.dirty;
            headline.dataset.device=selected||'';
        }
        if(!headline.dataset.dirty)headline.value=current.player_headlines?.[selected]||'';
        const override=$('host-override-uri');
        if(!override.dataset.dirty)override.value=background.source==='gathering'?background.uri:'';
        $('host-default-player').textContent=$('devices').selectedOptions[0]?.dataset.name||'Select a player in Playback.';
        $('host-background-status').textContent=background.message||health.background_message||
            (background.uri?(background.source==='gathering'?'Background music uses this gathering’s override.':'Background music uses the player’s default.'):'Choose background music in Player settings.');
        $('save').textContent=!running?'Start session':moving?'Move session here':dirty?'Apply & restart session':'Restart session';
        $('save').classList.toggle('secondary',running&&!dirty&&!moving);
        $('save').hidden=running&&!dirty&&!moving;
        $('host-pending').hidden=!dirty;
        $('host-pending').textContent='Changes not yet applied. '+(running?'Moving or restarting can affect playback.':'Start the session to apply them.');
        $('host-start-help').textContent=running?(moving?'Move playback and future requests to the selected player.':'Session running. Change the player or request setting above to move or apply changes.'):'Activate the selected player and enable automatic requests. Shuffle and repeat are turned off.';
    };
    let accountTimer, accountLoading=false, accountLoaded=false;
    function showAccount(value){const label=value.status==='verified'?value.display_name:value.status==='not_connected'?'Not connected':'Unable to verify';$('host-account-summary').textContent='Spotify account: '+label;$('host-playback-account').textContent=value.status==='verified'?value.display_name:'';$('host-account-details').textContent=label+(value.verified_at?' · Verified '+new Date(value.verified_at*1000).toLocaleString():'');}
    async function checkAccount(){if(accountLoading)return;accountLoading=true;accountLoaded=true;clearTimeout(accountTimer);$('host-account-refresh').disabled=true;try{const value=await api('/api/host/account',{});if(window.hostDeskState?.host)showAccount(value);}catch(error){showAccount({status:'unavailable'});}finally{accountLoading=false;$('host-account-refresh').disabled=false;if(window.hostDeskState?.host)accountTimer=setTimeout(checkAccount,300000);}}
    $('host-account-refresh').onclick=checkAccount;
    $('host-diagnostics-download').onclick=async()=>{const b=$('host-diagnostics-download');b.disabled=true;try{const report=await api('/api/host/diagnostics',{});const url=URL.createObjectURL(new Blob([JSON.stringify(report,null,2)],{type:'application/json'}));const a=document.createElement('a');a.href=url;a.download='unicorntunes-diagnostics.json';a.click();setTimeout(()=>URL.revokeObjectURL(url),1000);$('host-diagnostics-status').textContent='Report downloaded. It contains recent diagnostic events; review before sharing.';}catch(error){$('host-diagnostics-status').textContent='Unable to download diagnostics. Sign in and try again.';}finally{b.disabled=false;}};
    async function saveBackground(input, scope, status, clear=false){
        const statusElement=$(status);
        try{
            await api('/api/host/background',{uri:clear?'':input.value,device:$('devices').value,scope});
            delete input.dataset.dirty;
            await refresh();
            statusElement.textContent=clear?'Player default restored.':'Saved.';
        }catch(error){statusElement.textContent=error.message;}
    }
    for(const [form,id,scope,status] of [
        ['host-background-form','host-background-uri','player','host-default-status'],
        ['host-gathering-background','host-override-uri','gathering','host-gathering-background-status']
    ]){
        const input=$(id);
        input.addEventListener('input',()=>{input.dataset.dirty='true';});
        $(form).onsubmit=async event=>{
            event.preventDefault();
            const submit=event.submitter;
            if(submit)submit.disabled=true;
            try{await saveBackground(input,scope,status);}finally{if(submit)submit.disabled=false;}
        };
    }
    $('host-player-headline').oninput=event=>{event.target.dataset.dirty='true';};
    $('host-player-headline-form').onsubmit=async event=>{
        event.preventDefault();
        const input=$('host-player-headline');
        const status=$('host-player-headline-status');
        try{
            await api('/api/host/player-headline',{device:$('devices').value,headline:input.value});
            delete input.dataset.dirty;
            await refresh();
            status.textContent='Welcome heading saved.';
        }catch(error){status.textContent=error.message;}
    };
    $('host-restore-background').onclick=()=>saveBackground($('host-override-uri'),'gathering','host-gathering-background-status',true);
    $('devices').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
    $('accepting').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
}());
