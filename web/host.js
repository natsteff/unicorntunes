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
      <div class="host-help-links"><button id="host-help" type="button" class="secondary" aria-controls="host-start-guide">Help</button><p id="host-start-reminder" class="muted"></p><button type="button" class="secondary" id="host-install" hidden>Add Host Desk to Home Screen</button></div>
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
        <button id="host-tab-room" type="button" role="tab" aria-controls="host-panel-room" aria-selected="false" tabindex="-1">Gathering</button>
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
        <p id="host-background-status" role="status"></p><p id="host-soloist-expiry" role="status"></p>
        <div id="host-background-warning" class="host-card" hidden><p>No background music selected for this configuration. Choose a playlist to keep music playing after requests run out.</p><button type="button" class="secondary" data-background-pick="player">Choose a playlist</button></div>
        <div class="host-help-links"><button type="button" class="secondary" data-host-volume>Volume controls</button><button type="button" class="secondary" data-host-go="player">Player settings</button><button type="button" class="secondary" data-host-go="setup">Player setup &amp; connection help</button></div>
      </section>
      <section id="host-panel-music" class="host-panel" role="tabpanel" aria-labelledby="host-tab-music" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Music library</h3><p>Choose playlists for guests and maintain the local library.</p></div>

        <div id="host-playlists-slot"></div>
        <button type="button" class="secondary" data-host-volume>Local library scan &amp; loudness matching</button>
      </section>
      <section id="host-panel-room" class="host-panel" role="tabpanel" aria-labelledby="host-tab-room" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Gathering</h3><p>Configure queue rules, themes and this gathering’s background music.</p></div><div class="host-card"><h4>Background music for this gathering</h4><p>Overrides the selected player’s default until you restore it or start a new gathering.</p>
        <p id="host-gathering-background-name"></p><button type="button" class="secondary" data-background-pick="gathering">Browse Spotify playlists</button><form class="host-settings-form" id="host-gathering-background"><label for="host-override-uri">Spotify link or URI</label><input id="host-override-uri" type="text" maxlength="200"><button type="submit" class="secondary">Save gathering override</button> <button id="host-restore-background" type="button" class="secondary">Restore player default</button></form><p id="host-gathering-background-status" role="status"></p></div>
        <div id="host-room-slot"></div>
      </section>
      <section id="host-panel-player" class="host-panel" role="tabpanel" aria-labelledby="host-tab-player" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Player settings</h3><p>Choose the player you want to configure in Playback.</p></div>
        <div class="host-card"><h4>Default background music</h4><p id="host-default-player"></p>
        <p id="host-default-name"></p><button type="button" class="secondary" data-background-pick="player">Browse Spotify playlists</button><form class="host-settings-form" id="host-background-form"><label for="host-background-uri">Spotify playlist, album or artist link or URI</label><input id="host-background-uri" type="text" maxlength="200"><button type="submit" class="secondary">Save player default</button></form><p id="host-default-status" role="status"></p></div>
        <div class="host-card"><h4>Guest welcome line</h4><form id="host-player-welcome-form" class="host-settings-form">
          <label for="host-player-welcome">Welcome line for this player</label><input type="text" id="host-player-welcome" maxlength="300">
          <p class="muted">Leave blank to hide the welcome line on the guest page.</p><p id="host-player-welcome-current" class="muted"></p>
          <div class="buttons"><button type="submit" class="secondary">Save welcome line</button><button type="button" class="secondary" id="host-player-welcome-restore">Restore default</button></div>
        </form><p id="host-player-welcome-status" role="status"></p></div>
        <div class="host-card"><h4>Guest headline</h4><form id="host-player-headline-form" class="host-settings-form">
          <label for="host-player-headline">Heading for this player</label><input type="text" id="host-player-headline" maxlength="300">
          <p class="muted">Leave blank to hide the headline on the guest page.</p><p id="host-player-headline-current" class="muted"></p>
          <label for="host-player-accent">Text to highlight in red</label><input type="text" id="host-player-accent" maxlength="300">
          <p id="host-player-headline-preview" class="headline-preview" aria-label="Headline preview"></p>
          <div class="buttons"><button type="submit" class="secondary">Save headline</button><button type="button" class="secondary" id="host-player-headline-restore">Restore default</button></div>
        </form><p id="host-player-headline-status" role="status"></p></div>
        <section id="host-audio-levels" class="host-card" tabindex="-1" aria-labelledby="host-volume-title">
          <h4 id="host-volume-title">Volume controls</h4>
          <p id="host-volume-device"></p>
          <button id="host-refresh-volumes" type="button" class="secondary">Refresh volume levels</button>
          <div class="host-volume-grid">
            <form id="host-spotify-volume-form">
              <h5>Spotify volume</h5><p class="muted">Spotify playback on the selected Connect player.</p>
              <label for="host-spotify-volume">Spotify level (0–100)</label><input id="host-spotify-volume" type="number" min="0" max="100" step="1" required>
              <button type="submit" class="secondary">Apply Spotify volume</button><p id="host-spotify-volume-status" role="status"></p>
            </form>
            <div id="host-volume-slot"><h5>Local-file volume</h5><p class="muted">Local files through MPD.</p><p id="host-local-volume-binding" role="status"></p></div>
            <form id="host-output-volume-form">
              <h5>Player output volume</h5><p class="muted">Changes all audio routed through the player’s default PulseAudio output.</p>
              <label for="host-output-volume">Output level (0–100)</label><input id="host-output-volume" type="number" min="0" max="100" step="1" required>
              <button type="submit" class="secondary">Apply output volume</button><p id="host-output-volume-status" role="status"></p>
            </form>
          </div>
          <p class="muted">Use the stereo’s physical volume knob or remote for the final speaker level.</p>
          <details class="host-maintenance"><summary>Local library scan &amp; loudness matching</summary><div id="host-loudness-slot"></div></details>
        </section>
        <div id="host-local-slot"></div><div id="host-locations-slot"></div>
      </section>
      <section id="host-panel-application" class="host-panel" role="tabpanel" aria-labelledby="host-tab-application" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>UnicornTunes settings</h3><p>Set the guest page wording and stats timezone.</p></div>
        <div id="host-app-settings-slot"></div>
        <div class="host-card"><h4>House background playlist</h4><p>Used when the gathering and player have no background selection.</p>
        <p id="host-house-name"></p><button type="button" class="secondary" data-background-pick="house">Browse Spotify playlists</button>
        <form class="host-settings-form" id="host-house-form"><label for="host-house-uri">Spotify link or URI</label><input id="host-house-uri" type="text" maxlength="200"><button type="submit" class="secondary">Save house playlist</button></form><p id="host-house-status" role="status"></p></div>
      </section>
      <section id="host-panel-setup" class="host-panel" role="tabpanel" aria-labelledby="host-tab-setup" tabindex="0" hidden>
        <div class="host-panel-heading"><h3>Setup &amp; troubleshooting</h3><p>Check Spotify authorization and diagnose playback connections.</p></div>
        <div class="host-card"><h4>Soloist updates</h4>
          <p>Check and stage the official ARM64 build on the linked Pine64. Applying an update restarts Soloist.</p>
          <div class="buttons"><button type="button" class="secondary" data-soloist-action="status">Read update status</button><button type="button" class="secondary" data-soloist-action="check">Check for update</button><button type="button" class="secondary" data-soloist-action="stage">Download and stage</button><button type="button" class="secondary" data-soloist-action="apply">Apply and restart</button></div>
          <p id="host-soloist-update-status" role="status"></p>
          <form id="host-soloist-schedule" class="host-settings-form"><label><input type="checkbox" id="host-soloist-scheduled"> Apply staged updates when idle</label><label for="host-soloist-hour">Maintenance hour (0–23, UnicornTunes timezone)</label><input type="number" id="host-soloist-hour" min="0" max="23" value="3" required><button type="submit" class="secondary">Save maintenance window</button></form>
        </div>
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
    controls.querySelectorAll('[data-host-volume]').forEach(button=>{
        button.onclick=()=>{
            select('player');
            $('host-audio-levels').scrollIntoView({block:'start'});
            $('host-audio-levels').focus({preventScroll:true});
        };
    });
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
        if(!current.host){renderVolumeControls({});select('playback');accountLoaded=false;clearTimeout(accountTimer);showAccount({status:'unavailable'});return;}
        if(!accountLoaded)checkAccount();
        if(typeof offerHomeScreenInstall==='function')offerHomeScreenInstall(true);
        renderVolumeControls(current);
        $('host-soloist-expiry').textContent=current.soloist_expiry_warning?
            'Soloist expiry is approaching. Check Soloist updates in Setup & troubleshooting. '+(current.soloist_schedule_warning||''):'';
        const schedule=$('host-soloist-schedule');
        if(!schedule.dataset.dirty){
            $('host-soloist-hour').value=current.soloist_update_schedule?.hour??3;
            $('host-soloist-scheduled').checked=!!current.soloist_update_schedule?.enabled;
        }
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
        renderPlayerCopy(current, selected);
        const override=$('host-override-uri'), house=$('host-house-uri');
        const currentOverride=background.override||{};
        const currentHouse=background.house||{};
        if(!override.dataset.dirty)override.value=currentOverride.uri||'';
        if(!house.dataset.dirty)house.value=currentHouse.uri||'';
        const playerDefault=background.defaults?.[selected]||{};
        $('host-default-name').textContent=playerDefault.name||'';
        $('host-gathering-background-name').textContent=currentOverride.name||'';
        $('host-house-name').textContent=currentHouse.name||'';
        $('host-default-player').textContent=$('devices').selectedOptions[0]?.dataset.name||'Select a player in Playback.';
        const effective=currentOverride.uri?currentOverride:playerDefault.uri?playerDefault:currentHouse;
        $('host-background-warning').hidden=!!effective.uri;
        const source=effective===currentOverride?'gathering override':effective===playerDefault?'player default':'house background playlist';
        $('host-background-status').textContent=(current.device===selected&&(background.message||health.background_message))||
            (effective.uri?'Background music uses the '+source+(effective.name?' · '+effective.name:''):'');
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
        ['host-gathering-background','host-override-uri','gathering','host-gathering-background-status'],
        ['host-house-form','host-house-uri','house','host-house-status']
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
    function renderPlayerCopy(current, selected){
        const name=$('devices').selectedOptions[0]?.dataset.name;
        for(const [key, values, inherited] of [
            ['headline',current.player_headlines,current.room?.copy.headline],
            ['welcome',current.player_welcomes,name?'Welcome to '+name:'']
        ]){
            const input=$('host-player-'+key);
            const accent=$('host-player-accent');
            if(input.dataset.device!==selected){
                delete input.dataset.dirty;
                if(key==='headline')delete accent.dataset.dirty;
                input.dataset.device=selected||'';
            }
            const value=values?.[selected];
            if(!input.dataset.dirty)input.value=value??'';
            $('host-player-'+key+'-current').textContent='Default: '+(inherited||'Hidden')+' · Current: '+(value===undefined?'Default':value||'Hidden');
            if(key==='headline'){
                if(!accent.dataset.dirty)accent.value=current.player_accents?.[selected]||'';
                updatePlayerPreview();
            }
        }
    }
    function updatePlayerPreview(){
        if(typeof renderHeadline==='function')renderHeadline($('host-player-headline-preview'),$('host-player-headline').value,$('host-player-accent').value);
    }
    for(const key of ['headline','welcome']){
        const input=$('host-player-'+key);
        input.oninput=()=>{input.dataset.dirty='true';updatePlayerPreview();};
        const save=async restore=>{
            const status=$('host-player-'+key+'-status');
            try{
                await api('/api/host/player-'+key,{device:$('devices').value,[key]:input.value,restore,accent:$('host-player-accent').value});
                delete input.dataset.dirty;
                if(key==='headline')delete $('host-player-accent').dataset.dirty;
                await refresh();
                status.textContent=restore?'Default restored.':'Saved.';
            }catch(error){status.textContent=error.message;}
        };
        $('host-player-'+key+'-form').onsubmit=event=>{event.preventDefault();save(false);};
        $('host-player-'+key+'-restore').onclick=()=>save(true);
    }
    $('host-player-accent').oninput=event=>{event.target.dataset.dirty='true';updatePlayerPreview();};
    controls.querySelectorAll('[data-background-pick]').forEach(button=>{
        button.onclick=()=>openBackgroundPlaylistPicker(button.dataset.backgroundPick);
    });
    $('host-restore-background').onclick=()=>saveBackground($('host-override-uri'),'gathering','host-gathering-background-status',true);
    const soloistStatus=$('host-soloist-update-status');
    const soloistButtons=Array.from(controls.querySelectorAll('[data-soloist-action]'));
    for(const button of soloistButtons)button.onclick=async()=>{
        const action=button.dataset.soloistAction;
        if(action==='apply'&&!confirm('Apply the staged build and restart Soloist? The session must be stopped and audio idle.'))return;
        soloistButtons.forEach(button=>{button.disabled=true;});
        soloistStatus.textContent='Working on the linked Pine64…';
        try{
            let result=await api('/api/host/soloist-update',{action});
            while(result.pending){
                await new Promise(resolve=>setTimeout(resolve,1500));
                result=await api('/api/host/soloist-update',{action:'job'});
            }
            if(result.error)throw Error(result.error);
            const date=value=>new Date(value*1000).toLocaleDateString();
            soloistStatus.textContent=[
                'Installed '+result.installed.version+' · built '+date(result.installed.built_at)+' · expires '+date(result.installed.expires_at),
                result.expiry_warning?'Expiry is approaching.':'',
                result.staged?'Staged '+result.staged.version:'',
                result.checked_at?'Last checked '+date(result.checked_at):'',
                result.message,result.schedule_message,
                result.discovery_confirmed===true?'Player rediscovered.':result.discovery_confirmed===false?'Player discovery unconfirmed; refresh players.':''
            ].filter(Boolean).join(' · ');
        }catch(error){soloistStatus.textContent=error.message;}
        finally{soloistButtons.forEach(button=>{button.disabled=false;});}
    };
    $('host-soloist-schedule').oninput=event=>{event.currentTarget.dataset.dirty='true';};
    $('host-soloist-schedule').onsubmit=async event=>{
        event.preventDefault();
        const form=event.currentTarget;
        try{
            await api('/api/host/soloist-update',{action:'schedule',hour:$('host-soloist-hour').valueAsNumber,enabled:$('host-soloist-scheduled').checked});
            delete form.dataset.dirty;
            await refresh();
            soloistStatus.textContent='Maintenance window saved.';
        }catch(error){soloistStatus.textContent=error.message;}
    };
    const spotifyLevel=$('host-spotify-volume');
    const pulseLevel=$('host-output-volume');
    const spotifyStatus=$('host-spotify-volume-status');
    const pulseStatus=$('host-output-volume-status');
    const spotifySave=$('host-spotify-volume-form').querySelector('button');
    const pulseSave=$('host-output-volume-form').querySelector('button');
    let volumeDevice, outputSink, outputGeneration=0, volumeSelectionGeneration=0;
    let spotifyOption, outputAvailable, spotifyBusy=false, outputBusy=false;

    function renderVolumeControls(current){
        const option=current.host?$('devices').selectedOptions[0]:null;
        const device=option?.value||'';
        const localLinked=!!device&&current.local_player?.enabled&&current.local_player.device===device;
        const changed=volumeDevice!==device||outputAvailable!==localLinked;
        $('host-volume-device').textContent=option?.dataset.name||'Choose a player in Playback.';
        if(changed){
            outputGeneration++;
            volumeSelectionGeneration++;
            volumeDevice=device;
            outputAvailable=localLinked;
            outputSink=null;
            pulseLevel.value='';
            pulseStatus.textContent=localLinked?'Refresh volume levels to check the player’s output.':'Link a local player to control its output.';
        }
        if(changed||spotifyOption!==option){
            spotifyOption=option;
            spotifyLevel.value=option?.dataset.volume||'';
            spotifyStatus.textContent=option?.dataset.volumeSupported==='true'?'':'Spotify has not reported volume control for this player.';
        }
        spotifyLevel.disabled=option?.dataset.volumeSupported!=='true';
        spotifySave.disabled=spotifyLevel.disabled||spotifyBusy;
        pulseLevel.disabled=!outputSink;
        pulseSave.disabled=!outputSink||outputBusy;
        $('local-volume-apply').disabled=!localLinked;
        for(const id of ['local-scan','loudness-refresh','loudness-cancel','loudness-save']){
            if(changed)$(id).disabled=!localLinked;
        }
        $('host-local-volume-binding').textContent=localLinked?'':'Select the player linked to MPD to apply local levels.';
    }

    function reportOutput(result){
        outputSink=result.sink;
        pulseLevel.value=result.volume<=100?result.volume:'';
        pulseStatus.textContent='Output: '+result.sink+' · '+result.volume+'%'+(result.muted?' · Muted at the device':'');
        pulseLevel.disabled=pulseSave.disabled=false;
    }
    async function loadOutput(){
        if(!outputAvailable)return;
        const generation=++outputGeneration;
        const device=volumeDevice;
        outputSink=null;
        pulseLevel.disabled=pulseSave.disabled=true;
        pulseStatus.textContent='Checking player output…';
        try{
            const result=await api('/api/host/output-volume-status',{device});
            if(generation===outputGeneration)reportOutput(result);
        }catch(error){
            if(generation===outputGeneration)pulseStatus.textContent=error.message;
        }
    }
    $('host-refresh-volumes').onclick=async event=>{
        const button=event.currentTarget;
        button.disabled=true;
        try{await loadDevices();await loadOutput();}finally{button.disabled=false;}
    };
    $('host-spotify-volume-form').onsubmit=async event=>{
        event.preventDefault();
        const device=volumeDevice, generation=volumeSelectionGeneration;
        spotifyBusy=spotifySave.disabled=true;
        try{
            await api('/api/host/spotify-volume',{device,volume:spotifyLevel.valueAsNumber});
            if(generation===volumeSelectionGeneration)spotifyStatus.textContent='Spotify accepted the change. Refresh volume levels to confirm.';
        }catch(error){if(generation===volumeSelectionGeneration)spotifyStatus.textContent=error.message;}
        finally{spotifyBusy=false;spotifySave.disabled=spotifyLevel.disabled;}
    };
    $('host-output-volume-form').onsubmit=async event=>{
        event.preventDefault();
        const device=volumeDevice, generation=outputGeneration;
        outputBusy=pulseSave.disabled=true;
        try{
            const result=await api('/api/host/output-volume',{device,sink:outputSink,volume:pulseLevel.valueAsNumber});
            if(generation===outputGeneration)reportOutput(result);
        }catch(error){if(generation===outputGeneration)pulseStatus.textContent=error.message;}
        finally{outputBusy=false;pulseSave.disabled=!outputSink;}
    };
    $('devices').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
    $('accepting').addEventListener('change',()=>window.renderHostDesk(window.hostDeskState||{},true));
}());
