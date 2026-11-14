# Deployment acceptance and rebuild checks

These checks require the actual playback device, speakers and guest devices. Automated tests cannot establish audible playback, board resource use or LAN discovery reliability.

## Playback and guests

- Start a session on the linked Spotify device, then let the application run for several tracks.
- While background music is playing with no requested tracks, add a Spotify request and confirm it starts immediately. Repeat with a local request. An actual requested song must not be interrupted by the next arrival.
- Request Spotify → local → Spotify, then two local files consecutively. Listen for overlap, unexpected volume jumps, and source-order problems.
- Let the queue empty, then request a new song from each source and confirm playback restarts.
- Compare several quiet/loud local files after loudness preparation with Spotify at a fixed speaker/amplifier setting. Calibrate matched local output; preparation's digital loudness check is not a speaker measurement.
- Verify pause, skip, stop, uncertain-request reconciliation and the selected playback-location binding.
- Use two or more real phones to confirm names, one initial request vote, one additional vote per other guest, protected next selection, every-third-oldest scheduling and one like per guest per requested play.
- Confirm background tracks never enter recent history, played rankings or favorite play counts. Existing user-request/vote statistics are intentionally preserved by history cleanup.
- Reboot the playback device and Docker host deliberately when playback interruption is acceptable. Verify Soloist and MPD services return, the local library/keys/settings persist, and automatic handoff remains stopped until the host starts it.

## If Soloist disappears

From the host's Local music player section, use **Check player health** and note the service state, categorized hints and Spotify device availability. A running process alone is not proof of registration. Finish/reconcile any outstanding handoff before a deliberate **Restart Soloist**, then refresh devices and start again after it returns. Do not publish raw unit command lines or unredacted journal logs, since they may contain credentials. Root-cause diagnosis remains open until evidence is captured during the failure.

## Conditional Spotify guest-account migration

Keep the personal account during validation, as agreed. Switch only after playback and local-file behavior are accepted:

1. Stop handoff and finish/reconcile outstanding requests. Securely back up the web data volume and playback-device configuration.
2. Verify the intended guest account's Premium status and developer-app access. See Spotify's current application requirements rather than assuming the existing account's authorization transfers.
3. Pair Soloist with that account through Spotify's supported flow, and reconnect the web application's Spotify authorization using the same account. Keys and refresh tokens stay private; never add them to GitHub.
4. Refresh available devices, bind the local player to the new matching Spotify device ID, and reselect playlists/favorites as needed for that account's accessible catalog and library.
5. Repeat source-transition, idle-restart, loudness and multi-phone checks before normal use. The Mac is not a runtime dependency; deployment-side authorization alternatives are documented in DEPLOY.md.

No account migration or physical acceptance is implied by publishing the software. These remain explicit installation steps.


### Discovery evidence

The health report also checks UDP 5353 listener presence, whether a Soloist socket can be identified, and Avahi's service state. These are diagnostic hints, not a definitive conflict test. A [reported Soloist issue](https://github.com/spotify/soloist/issues/1) describes silent discovery failure with a UDP 5353 conflict on a different version/setup. Compare actual failure-time observations before attributing the current installation's problem to that report. Do not disable Avahi or change networking merely because both services are present.
