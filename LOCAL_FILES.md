# Local music playback

Soloist handles Spotify audio. A separate MPD user service on the same playback device handles uploaded music. MPD stays on loopback port 6601 and shares the user's PulseAudio output.

The host desk tests the connection and indexes the library. Guests can search Spotify and uploaded local tracks together, or select either source. Local results are marked **Local file** and use the same request, vote and fair-queue rules as Spotify results. Installing the connection does not start music or change Soloist.

First follow [the playback-device setup guide](SOLOIST_DEVICE_SETUP.md) to install MPD and create `unicorntunes-local.service`. Upload music under `$HOME/Music/UnicornTunes` on the playback device.

## 1. Create a dedicated key on the Docker server

From the directory containing your Compose file:

```sh
docker compose exec foxtunes ssh-keygen -t ed25519 -N '' -f /data/local-player.key
docker compose exec foxtunes cat /data/local-player.key.pub
```

Do not overwrite an existing key. Copy only the displayed **public** key to a file named `unicorntunes-local.pub` on the playback device. The private key stays in the Docker data volume.

## 2. Authorize the key on the playback device

Run as the same audio user that runs Soloist and MPD:

```sh
curl -fL https://github.com/natsteff/unicorntunes/archive/refs/heads/main.tar.gz -o /tmp/unicorntunes-source.tar.gz
tar -xzf /tmp/unicorntunes-source.tar.gz -C /tmp
python3 /tmp/unicorntunes-main/deploy/authorize-local-player.py < unicorntunes-local.pub
```

This installs a small agent under `$HOME/.local/lib/unicorntunes/`. Its SSH key can only invoke the agent; SSH shell, port forwarding and PTY are disabled. The agent accepts library/status/scan and bounded player commands, connecting only to local MPD. Do not open MPD's port to the LAN.

## 3. Pin the playback device's SSH host key

On the playback device, display its host fingerprint:

```sh
ssh-keygen -lf /etc/ssh/ssh_host_ed25519_key.pub
```

On the Docker server, replace `PLAYER_ADDRESS` with the playback device's IP or resolvable hostname:

```sh
docker compose exec foxtunes sh -c 'ssh-keyscan -t ed25519 PLAYER_ADDRESS > /data/local-player.candidate'
docker compose exec foxtunes ssh-keygen -lf /data/local-player.candidate
```

Compare the fingerprints. Only if they match:

```sh
docker compose exec foxtunes mv /data/local-player.candidate /data/local-player.known_hosts
```

For a nonstandard SSH port add `-p PORT` to `ssh-keyscan`. A changed address may require pinning its host key under the new address again; verify the fingerprint each time.

## 4. Test from the host desk

Open `/host`, sign in, and expand **Local music player**. Enter the playback device's address, audio username and SSH port. Select the matching Spotify playback device, enable the connection, and save. Press **Test connection**, then **Scan library changes**. Successful results show MPD's state and an indexed track count.

Prefer a stable hostname or DHCP reservation. Connection settings and keys persist in `/data`; the Mac is not required for normal operation. Back up that volume securely.

The default local volume of 50 is a trial setting. Without automatic levels, ReplayGain can use existing track tags. Use cached loudness analysis below for per-track automatic volume. Avoid treating a percentage as normalized loudness.

## Mixed playback

Start a session with the matching Spotify playback device selected. Local files are bound to that device, so selecting another location does not route its requests to the wrong speakers. A session can begin with a local request while Spotify is idle, provided the selected Spotify device is available.

When the next request is local, UnicornTunes waits for the current Spotify track to end. It confirms Spotify is paused before asking the dedicated MPD queue to play one file. Brief gaps or a short start of Spotify's next background track can occur while the end is observed. MPD has single-track mode enabled, so the application chooses the next request across both sources. After a local track ends, the next Spotify request starts directly on the selected Spotify device. With no requests left, playback remains idle; a newly requested track starts automatically on its own source. Background Spotify contexts are not automatically resumed.

**Pause playback** pauses the current source and disables automatic handoff. **Skip to next request** stops the owned local item before starting a replacement. **Stop session** disables further handoff while allowing the current track to continue. Restarting a session during a local track stops that track before activating Spotify. A server restart preserves local ownership but leaves automatic handoff disabled.

Unconfirmed starts, unexpected MPD item changes, early stops and Spotify device changes hold further dispatch for host review. A lost start response is not automatically retried. Reconciliation of an unresolved local request stops the owned MPD track before releasing it. If MPD is playing a different item, check it and stop it manually first. A local track too short to be observed, or missing duration metadata, may need host review at its end.

Local files use their available tags, with the filename as the title fallback. Spotify's playlist-local entries remain inspection-only: these are separate from the uploaded MPD library and are not matched by title. Content rules apply to both sources; when explicit content is blocked, local tracks with unknown content ratings are blocked as well.

### First playback test

1. Keep the local trial volume conservative and verify **FoxTunes** is the linked Spotify device.
2. Search for a Spotify track, request it, then request one short uploaded file using **Local files only**.
3. Request another Spotify track after the local one. Listen for Spotify → local → Spotify, with no simultaneous audio.
4. Let the requests run out, then add another local or Spotify request and verify it starts.

The mixed source state machine is covered by automated tests. Audible transitions and volume balance still need validation on your playback hardware.


## Embedded artwork and local volume (v0.4.1)

Local covers are read from embedded MP3/other supported file tags via MPD's `readpicture` command. The web app serves them from its own origin and caches up to 64 MiB. JPEG, PNG, GIF and WebP pictures up to 16 MiB and 24 megapixels are supported and converted to 320-pixel JPEG thumbnails; missing or unsupported pictures use a music-note placeholder. Re-scan the library after replacing embedded artwork to clear the server cache; a browser image may remain cached for up to five minutes.

This requires an updated restricted agent on the playback device. Run as the existing audio user:

```sh
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.4.2/deploy/update-local-agent.sh -o /tmp/update-unicorntunes-agent.sh
bash /tmp/update-unicorntunes-agent.sh
```

The helper validates and atomically replaces only the installed agent; it does not restart MPD/Soloist, change SSH permissions, or start music. Its status output includes `agent_version: 3`. Then update the Docker image and refresh the page. Existing indexed tracks and queued local items can show artwork without being re-requested.

**Apply local volume** saves the chosen MPD level and, when an owned local track is active, applies it immediately after verifying ownership. It does not change Spotify's volume or the amplifier/sink settings. The original trial setting was 50; if local tracks are too quiet, raise the value gradually while listening, for example to 75, then compare with Spotify. This is a manual level adjustment, not per-track loudness normalization. Existing levels are preserved during upgrades; the app never silently raises every local track to maximum.


### Artwork size and error isolation (v0.4.2)

The original 2 MiB input limit rejected some larger embedded covers. The agent now accepts up to 16 MiB, and Docker generates small thumbnails without modifying the MP3 or its tags. Resizing runs on Docker, so the Pine64 needs no imaging package. Failures are cached per track; one bad or oversized image no longer blocks other covers. Host users can hover a missing-cover placeholder to see the returned error.

Update both the restricted agent and Docker, then refresh the page. This upgrade clears the old server artwork cache and uses a new image URL version, so old missing-image responses are not reused. Re-scan the library if you later change its embedded covers. If artwork is still missing, inspect that specific track rather than assuming the rest of the library is broken.


## Cached loudness and automatic volume (v0.7.0)

The playback device analyzes original files with FFmpeg and stores only measurements in a small JSON cache. Playback uses the original file with a calculated MPD software volume. No MP3 rewriting, tag changes, or new audio copies are involved.

### Update the playback device

Run as the existing audio user:

```sh
sudo apt-get install -y ffmpeg
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.7.0/deploy/update-local-agent.sh -o /tmp/update-unicorntunes-agent.sh
bash /tmp/update-unicorntunes-agent.sh
```

The agent reports version 6. Update Docker afterward. SSH reauthorization and Soloist restarts are unnecessary. The update asks obsolete copy preparation to stop after its current step. Existing generated copies remain on disk, excluded from search; they are not used for new playback. The old copy mode is disabled on upgrade and must be explicitly replaced with automatic levels after analysis.

### Scan and enable

1. Open `/host` → **Local music player** → **Match local loudness to Spotify** and click **Scan library changes**.
2. This indexes additions/removals and starts incremental analysis. Unchanged size/mtime matches are skipped; changed or new files are analyzed, and removed entries are pruned. Identical content moved to a new filename can reuse its measured result. Repeated scans while busy queue the latest library snapshot. Progress continues after closing the browser.
3. Review progress and errors, then enable **Auto-adjust original-file volume from cached analysis** and **Save automatic levels**. At least one valid measurement is required. Files still awaiting analysis cannot be requested in this mode; valid cached files remain available.
4. Compare representative local and Spotify tracks at a fixed amplifier/sink setting. Start with the −14 LUFS target and adjust the calibration reference as needed. Changing the target/reference uses the existing measurements immediately; it does not require another analysis scan.

After adding, changing or removing files, use **Scan library changes** again. This is an ongoing incremental scan, not a scan limited to initial setup. **Stop analysis** takes effect after the current FFmpeg step; a later scan resumes with valid cache entries skipped. Finish local playback before switching modes. Analysis and mode selection do not start music.

### Gain calculation and limitations

Each file needs one FFmpeg analysis pass plus a checksum read. Measurements live under `$HOME/.local/share/unicorntunes-local/loudness/measurements.json`, keyed by track identity with source size, modification time and checksum. Processing uses one filter thread at reduced CPU priority. A known duration of up to 30 minutes is required. Silence and unreadable files produce per-track errors.

The calculation accounts for [MPD 0.24.4's exponential software mixer curve](https://github.com/MusicPlayerDaemon/MPD/blob/v0.24.4/src/mixer/plugins/SoftwareMixerPlugin.cxx), as configured by the setup helper. A reference of 100 represents unity gain; it is not a command to play every track at 100. Each track receives its own level before playback, with ReplayGain disabled to avoid double adjustment. Live reference adjustment verifies ownership of the current MPD item.

Volume alone cannot amplify beyond MPD 100 or limit individual peaks. The calculation only attenuates and respects an estimated −1 dB true-peak ceiling. Tracks that cannot reach the target are listed with their limiting reason. Very quiet files may therefore remain quieter; no lossy file changes or hidden transcoding are performed. Digital measurements and the −14 LUFS starting target do not guarantee the installed Soloist and MPD outputs sound identical; compare them on the actual speakers.

Automated tests cover incremental reuse, changes/removals, gain and peak limits, original-file playback, ownership and real FFmpeg analysis without modifying source bytes.

## Player recovery (v0.6.0)

Update the restricted agent with the v0.6.0 helper to add `player_health.py`; the helper itself does not restart anything. **Check player health** reads fixed service fields, Spotify availability and categorized journal hints. It does not expose raw logs or credentials. **Restart Soloist** is a deliberate host action that can interrupt Spotify audio; the UI asks for confirmation, leaves handoff stopped and requires a fresh session start after device recovery. Local playback and uncertain/handed-off requests must be finished or reconciled first. The restricted command can restart only `soloist.service`, not arbitrary services or shell commands.

The reason for the intermittent device disappearance remains unconfirmed until diagnostic evidence is collected on the affected device. This provides a recovery path without requiring SSH for each occurrence.
