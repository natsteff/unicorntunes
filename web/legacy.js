/* Deliberately ES5: this page must parse on iOS 9 Safari. */
(function () {
    'use strict';
    var state = null, csrf = '', online = false, polling = false, changing = false;
    var searching = false, results = [], offset = 0, searchText = '', searchSource = 'all';
    var more = false, searchGeneration = 0;
    function $(id) { return document.getElementById(id); }
    function node(tag, text) { var n = document.createElement(tag); if (text !== undefined) { n.textContent = text; } return n; }
    function clear(n) { while (n.firstChild) { n.removeChild(n.firstChild); } }
    function notice(text) { $('notice').textContent = text || ''; }
    function api(path, body, done) {
        var xhr = new XMLHttpRequest(), finished = false;
        function finish(error, data) { if (!finished) { finished = true; done(error, data); } }
        xhr.open(body === null ? 'GET' : 'POST', path, true);
        xhr.timeout = 15000;
        if (body !== null) {
            xhr.setRequestHeader('Content-Type', 'application/json');
            xhr.setRequestHeader('X-FoxTunes-CSRF', csrf);
        }
        xhr.onload = function () {
            var data;
            try { data = JSON.parse(xhr.responseText); } catch (e) { finish('Unexpected server response. Please try again.'); return; }
            if (xhr.status < 200 || xhr.status >= 300) {
                if (xhr.status === 403) { online = false; syncControls(); refresh(); }
                finish(data.error || 'Request failed.'); return;
            }
            finish(null, data);
        };
        xhr.onerror = function () { finish('Connection lost. Check the queue before retrying an action.'); };
        xhr.ontimeout = function () { finish('Connection timed out. Check the queue before retrying an action.'); };
        xhr.send(body === null ? null : JSON.stringify(body));
    }
    function syncControls() {
        var disabled = !online || changing;
        $('save-name').disabled = disabled;
        $('switch-guest').disabled = disabled;
        $('search-button').disabled = disabled || searching;
        $('previous').disabled = disabled || searching || offset === 0;
        $('next').disabled = disabled || searching || !more || offset >= 990;
        var buttons = document.querySelectorAll('button[data-action]'), i;
        for (i = 0; i < buttons.length; i += 1) {
            buttons[i].disabled = disabled || buttons[i].getAttribute('data-blocked') === 'yes';
        }
    }
    function mutation(path, body, after) {
        if (!online || changing) { return; }
        changing = true; syncControls(); notice('Saving…');
        api(path, body, function (error) {
            changing = false;
            if (error) { notice(error); } else { notice('Saved.'); if (after) { after(); } }
            /* Never replay a mutation: a lost response may have succeeded. */
            online = false; syncControls(); refresh();
        });
    }
    function action(label, blocked, fn) {
        var b = node('button', label); b.type = 'button';
        b.setAttribute('data-action', 'yes'); b.setAttribute('data-blocked', blocked ? 'yes' : 'no');
        b.onclick = fn; return b;
    }
    function details(song) {
        var row = node('div'); row.className = 'song' + (song.mine ? ' mine' : '');
        row.appendChild(node('strong', song.name || 'Untitled track'));
        row.appendChild(node('p', (song.artists || '') + (song.source === 'local' ? ' · Local file' : '')));
        if (song.album) { row.appendChild(node('p', song.album)); } return row;
    }
    function requested(uri) {
        var i; if (!state) { return false; }
        if (state.now && state.now.uri === uri) { return true; }
        for (i = 0; i < state.queue.length; i += 1) { if (state.queue[i].uri === uri) { return true; } }
        return false;
    }
    function renderResults() {
        clear($('results'));
        var i;
        for (i = 0; i < results.length; i += 1) {
            (function (song) {
                var row = details(song), added = requested(song.uri);
                row.appendChild(action(added ? 'Already requested' : 'Request', added || !state || !state.accepting, function () {
                    mutation('/api/request', {uri: song.uri});
                })); $('results').appendChild(row);
            }(results[i]));
        }
        syncControls();
    }
    function render() {
        $('version').textContent = state.version ? 'v' + state.version : '';
        $('guest').textContent = 'Current guest: ' + (state.profile ? state.profile.name : 'Guest');
        $('queue-status').textContent = state.accepting ? 'Requests open · ' + state.queue.length + ' in queue' : 'Requests paused by host';
        var now = $('now'), song = state.now, i;
        clear(now);
        if (song) {
            var row = details(song), src = song.art || '';
            if (song.source === 'local' && /^local:[a-f0-9]{64}$/.test(song.uri || '')) { src = '/api/local-art/' + song.uri.slice(6) + '?v=2'; }
            if (/^https:\/\/(i\.scdn\.co|mosaic\.scdn\.co|image-cdn-ak\.spotifycdn\.com)\//.test(src) || /^\/api\/local-art\/[a-f0-9]{64}\?v=2$/.test(src)) {
                var img = node('img'); img.className = 'art'; img.alt = ''; img.src = src;
                img.onerror = function () { this.style.display = 'none'; }; row.insertBefore(img, row.firstChild);
            }
            row.appendChild(node('p', song.playing ? 'Playing' : 'Paused / waiting'));
            if (song.requested_by) { row.appendChild(node('p', 'Requested by ' + song.requested_by)); }
            row.appendChild(node('div', '')); row.lastChild.className = 'clear'; now.appendChild(row);
        } else { now.appendChild(node('p', 'Waiting for music.')); }
        clear($('queue'));
        for (i = 0; i < state.queue.length; i += 1) {
            (function (item, index) {
                var row = details(item);
                row.appendChild(node('p', (index + 1) + '. Requested by ' + (item.requested_by || 'Guest') + (item.mine ? ' · Your request' : '')));
                if (item.protected) { row.appendChild(node('p', 'Next request · protected')); }
                if (item.state !== 'waiting') { row.appendChild(node('p', 'Locked · ' + item.state)); }
                row.appendChild(action((item.voted ? 'Voted · ' : 'Vote · ') + item.votes, item.voted || item.state !== 'waiting', function () {
                    mutation('/api/vote', {id: item.id});
                })); $('queue').appendChild(row);
            }(state.queue[i], i));
        }
        if (!state.queue.length) { $('queue').appendChild(node('p', 'No waiting requests. Find a song to add.')); }
        renderResults();
    }
    function refresh() {
        if (polling || changing) { return; } polling = true;
        api('/api/state', null, function (error, data) {
            polling = false;
            if (error) {
                online = false; $('connection').textContent = 'Disconnected · displayed information may be stale. Reconnecting automatically…';
            } else {
                state = data; csrf = data.csrf; online = true;
                $('connection').textContent = 'Connected · live updates' + (data.handoff && data.handoff.stale ? ' · player status is stale' : '') + (data.error ? ' · ' + data.error : '');
                render();
            }
            syncControls();
        });
    }
    function search() {
        if (searching || !online || changing) { return; }
        searching = true; syncControls(); $('search-status').textContent = 'Searching…';
        var generation = searchGeneration;
        api('/api/search?q=' + encodeURIComponent(searchText) + '&source=' + encodeURIComponent(searchSource) + '&offset=' + offset, null, function (error, data) {
            searching = false;
            if (generation !== searchGeneration) { syncControls(); return; }
            if (error) { $('search-status').textContent = error; }
            else {
                results = data.items || []; more = !!data.more;
                $('search-status').textContent = (results.length ? 'Results ' + (offset + 1) + '–' + (offset + results.length) : 'No matches on this page.') + (data.warning ? ' · ' + data.warning : '');
                $('pages').hidden = !more && offset === 0; renderResults();
            }
            syncControls();
        });
    }
    $('search-form').onsubmit = function (event) {
        event.preventDefault(); if (searching || changing) { return; }
        searchText = $('query').value.replace(/^\s+|\s+$/g, ''); searchSource = $('source').value; offset = 0;
        if (!searchText && searchSource !== 'local') { $('search-status').textContent = 'Enter a song or artist.'; return; }
        search();
    };
    $('previous').onclick = function () { if (!searching && offset > 0) { offset = Math.max(0, offset - 10); search(); } };
    $('next').onclick = function () { if (!searching && more && offset < 990) { offset += 10; search(); } };
    $('name-form').onsubmit = function (event) {
        event.preventDefault(); mutation('/api/profile', {name: $('guest-input').value}, function () { $('guest-input').value = ''; });
    };
    $('switch-guest').onclick = function () {
        mutation('/api/guest/switch', {}, function () {
            searchGeneration += 1; results = []; more = false; offset = 0;
            $('guest-input').value = ''; $('query').value = ''; $('search-status').textContent = ''; $('pages').hidden = true;
            notice('Ready for the next guest. Enter a name, or request anonymously.');
            clear($('results'));
        });
    };
    window.addEventListener('pageshow', refresh, false);
    document.addEventListener('visibilitychange', function () { if (!document.hidden) { refresh(); } }, false);
    window.setInterval(function () { if (!document.hidden) { refresh(); } }, 5000);
    refresh();
}());
