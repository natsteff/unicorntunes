/* Shared kiosk lifecycle, intentionally ES5 for iOS 9 Safari. */
(function () {
    'use strict';
    window.createKioskSession = function (options) {
        var idleMs = 2 * 60 * 1000, lastActivity = Date.now(), pending = false;
        var ready = false, token = '', retryAfter = 0, started = false, nextRequested = false, used = false;
        function activity() { lastActivity = Date.now(); used = true; }
        function reset() {
            if (pending) { return; }
            if (options.isBusy()) { nextRequested = true; return; }
            nextRequested = false;
            ready = false; pending = true; started = true;
            options.onReset();
            options.reset(function (error) {
                pending = false;
                if (error) {
                    retryAfter = Date.now() + 5000;
                    options.onError(error);
                } else {
                    ready = true; retryAfter = 0; lastActivity = Date.now(); used = false; options.onReady();
                }
            });
        }
        function tick() {
            if (!token || pending || options.isBusy()) { return; }
            if (nextRequested || (!ready && Date.now() >= retryAfter) || (ready && used && Date.now() - lastActivity >= idleMs)) { reset(); }
        }
        var events = ['touchstart', 'mousedown', 'keydown', 'input', 'focusin', 'click'], i;
        for (i = 0; i < events.length; i += 1) { document.addEventListener(events[i], activity, true); }
        document.addEventListener('visibilitychange', function () { if (!document.hidden) { tick(); } }, false);
        window.addEventListener('pageshow', tick, false);
        window.setInterval(tick, 1000);
        return {
            ensure: function (csrf) {
                if (token !== csrf) { token = csrf; ready = false; retryAfter = 0; }
                if (!started || !ready) { tick(); }
                return ready;
            },
            isReady: function () { return ready; },
            next: reset,
            activity: activity
        };
    };
}());
