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

Open `/host`, sign in, and expand **Local music player**. Enter the playback device's address, audio username and SSH port. Select the matching Spotify playback device, enable the connection, and save. Press **Test connection**, then **Scan local library**. Successful results show MPD's state and an indexed track count.

Prefer a stable hostname or DHCP reservation. Connection settings and keys persist in `/data`; the Mac is not required for normal operation. Back up that volume securely.

The default local volume of 50 is a trial setting. ReplayGain can use existing track tags, but loudness analysis and matching Spotify volume remain pending. Avoid treating a percentage as normalized loudness.

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


## Loudness matching (v0.5.1)

Soloist advertises [loudness normalization with peak limiting](https://github.com/spotify/soloist). [Spotify's normal reference is −14 LUFS](https://support.spotify.com/no-en/artists/article/track-not-as-loud-as-others/). UnicornTunes starts local preparation at that reference, using [FFmpeg's measured two-pass loudnorm filter](https://ffmpeg.org/ffmpeg-filters.html#loudnorm). The actual Soloist output level still needs a listening comparison; this application does not change Spotify's volume or claim to read Soloist's normalization preferences.

### One-time playback-device update

As the audio user on the playback device:

```sh
sudo apt-get update
sudo apt-get install -y ffmpeg
curl -fL https://raw.githubusercontent.com/natsteff/unicorntunes/v0.5.1/deploy/update-local-agent.sh -o /tmp/update-unicorntunes-agent.sh
bash /tmp/update-unicorntunes-agent.sh
```

The update includes `local_loudness.py` beside the existing restricted agent. No SSH reauthorization or Soloist restart is required. New installs using the authorization helper include both files. Update Docker normally afterward.

### Prepare and enable

1. In `/host` → **Local music player** → **Match local loudness to Spotify**, leave the target at **Normal reference (−14 LUFS)** and click **Prepare local library**.
2. Wait for all tracks to be prepared. Progress survives closing the browser. **Refresh progress** reads status; **Stop preparation** stops after the current processing step. Preparation runs at reduced CPU priority with one FFmpeg processing thread. Existing originals remain usable while preparing.
3. Check **Use level-matched playback copies**, review **Matched local output**, and click **Save loudness matching**. All indexed originals must have valid copies at this target. A new or changed file requires preparation again before it can play in matched mode.
4. Compare representative prepared local tracks with Spotify at a fixed amplifier/sink setting. The matched output starts at 100, removing the old trial-volume reduction. Lower **Local playback volume** and use **Apply local volume** if local audio is louder; while matched mode is active, that adjustment changes the matched output level. If 100 is still too quiet, disable matched mode, select a louder target, and prepare again. Do not assume equal source percentages mean equal loudness.

Enabling matching affects future local starts; finish or stop the active local track before changing modes. Disabling restores playback from originals at the saved original-mode volume. Neither mode selection nor preparation automatically starts music.

### File handling, limits and validation

Preparation analyzes each source, renders a stereo 48 kHz FLAC playback copy, then independently re-measures the copy. The default target is −14 LUFS with a −1 dB true-peak ceiling. Copies are accepted only within 0.7 LUFS of the target and at or below −0.8 dBTP (allowing measurement rounding). Quiet sources can be raised and peak limiting can be used where needed. ReplayGain is disabled only for matched copies to avoid a second loudness adjustment. Originals, original tags and embedded covers are never rewritten.

Generated copies live under the reserved `__unicorntunes_normalized` directory inside the music directory; that folder is omitted from guest/library search. Names, artwork, original track IDs, request attribution, voting and queue ownership continue to use the original track. Deleting a generated copy invalidates its readiness and preparation rebuilds it. Do not upload personal tracks into this reserved folder.

The managed copy budget is 4 GiB, with a 512 MiB free-space reserve. A file needs a known duration no longer than 30 minutes. Silence, unreadable sources, insufficient space, changed files or failed output checks are reported by track and do not alter originals. Preparation is resumable: unchanged verified copies at the selected target are reused. A failed or interrupted job must be resumed successfully before matching is enabled.

Automated tests use real FFmpeg audio to verify matching across quiet/loud inputs, true-peak bounds, original-file hashes and reuse of prepared copies. Physical output matching, preparation time and resource use on a particular playback board still require deployment testing.


## Player recovery (v0.6.0)

Update the restricted agent with the v0.6.0 helper to add `player_health.py`; the helper itself does not restart anything. **Check player health** reads fixed service fields, Spotify availability and categorized journal hints. It does not expose raw logs or credentials. **Restart Soloist** is a deliberate host action that can interrupt Spotify audio; the UI asks for confirmation, leaves handoff stopped and requires a fresh session start after device recovery. Local playback and uncertain/handed-off requests must be finished or reconciled first. The restricted command can restart only `soloist.service`, not arbitrary services or shell commands.

The reason for the intermittent device disappearance remains unconfirmed until diagnostic evidence is collected on the affected device. This provides a recovery path without requiring SSH for each occurrence.
