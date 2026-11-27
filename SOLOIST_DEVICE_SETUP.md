# Playback-device setup and rebuild guide

UnicornTunes uses a Docker-hosted web application and a separate Linux playback device connected to the stereo. Spotify Soloist handles Spotify audio. The optional MPD local-file player supports mixed-source search and coordinated playback; audible calibration and reboot behavior still require validation on each installation.

Machine-specific addresses, usernames, exact installed unit settings and validation observations are kept in the private installation record, outside version control. Substitute your own values in the commands below.

## Base system and audio

1. Install a Soloist-supported Linux distribution and architecture.
2. Create a dedicated non-root service user and configure networking.
3. Configure PulseAudio or another supported audio server in that user's session. Verify audio at the stereo before adding playback software.
4. Ensure the user services/audio session can start after reboot without an interactive SSH login. Record the distribution-specific user-service persistence setup used on your system.

Useful inspection commands:

```sh
uname -m
cat /etc/os-release
hostname -I
pactl info | grep -E 'Server Name|Default Sink'
```

Do not assume another board exposes the same audio sink. The current local-player trial targets PulseAudio; adapt and validate it before using a different audio stack.

## Install Spotify Soloist

Download the correct architecture directly from [Spotify](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates) and follow the [official setup](https://developer.spotify.com/documentation/soloist). The binary is not bundled with UnicornTunes.

Keep its executable, persistent device/account state and cache paths documented in your private installation record. From the extracted directory:

```sh
./soloist --version
./soloist --help
```

The reference deployment uses a systemd user service. Its exact installed unit is still to be collected and sanitized before this guide can provide a validated reusable Soloist service template. Do not guess its state paths or overwrite a working service during a rebuild rehearsal.

Configure a recognizable Spotify Connect device name and retain the paired state. The Soloist API key is separate from the Web API Client ID/OAuth authorization stored by the Docker app. Pair the player with the same Spotify account authorized in UnicornTunes.

Spotify's startup interface can put the API key in service/process arguments. Do not share unredacted unit contents, ExecStart output, process command lines or logs. Keep credentials out of Git. Builds require periodic vendor updates; record and check the installed version when rebuilding.

## Update Spotify Soloist

Spotify says Soloist builds expire after 90 days. Check the installed executable with `./soloist --version` from its directory and record the version and build timestamp for each board. Plan the next update from the build timestamp so a newly downloaded older build does not reset the maintenance clock. [Spotify Soloist overview](https://developer.spotify.com/documentation/soloist)

1. Download a newer Linux ARM64 archive for Garage from Spotify. Extract it separately and compare the new executable’s version and build timestamp with the installed one. [Official downloads](https://developer.spotify.com/documentation/soloist/reference/downloads-and-updates)
2. Schedule a brief playback interruption. Keep a private backup of the current executable, service configuration and stored pairing state. Preserve the audio user, the existing service options and the advertised name **UnicornPlayer - Garage**.
3. Replace the executable at the existing service path and restart `soloist.service` in the audio user’s systemd session. Check `systemctl --user is-active soloist.service` and record the new build information.
4. Refresh players in the Host desk and verify that **UnicornPlayer - Garage** appears and plays audibly. If the update fails, restore the previous executable if its build is still valid. Otherwise use a newer supported build and check the existing service and pairing state.

## Scheduled Soloist maintenance

Install restricted agent 8 on the linked Pine64 as the audio user. From the published v0.8.3 checkout, run `bash deploy/update-local-agent.sh` and `bash deploy/enable-soloist-maintenance.sh`. The user timer checks weekly and stages a newer official ARM64 build within 30 days of expiry. Ensure the audio user’s services run independently of SSH login. To check staging immediately, run `systemctl --user start unicorntunes-soloist-stage.service`.

In **Host desk → Setup & troubleshooting → Soloist updates**, select **Read update status** to see the build date, expiry and staged build. Enable scheduled application and choose the maintenance hour in the configured UnicornTunes timezone. The Docker worker checks within that hour while it is running. It applies only when handoff is stopped, requests are resolved, Spotify is idle and MPD reports stopped. A busy or unverifiable player defers the update. The host can also check, stage or apply manually.

The updater preserves the service and paired data, backs up the executable and verifies the restarted user service. The Host desk reports whether Spotify discovery was observed. Confirm audible playback after applying a build.

## Validate Spotify playback

1. Start the Soloist user service.
2. Open Spotify on a phone or computer with the intended account.
3. Select your Soloist device and play a normal catalog song. Verify audio through the stereo.
4. In UnicornTunes's host desk, refresh devices, choose that player and start the session.
5. Test one guest request and observe handoff.
6. Reboot and repeat the audio/session checks without first opening an SSH login.

A phone or Mac is not an ongoing playback dependency. Spotify authorization setup for HTTP deployments may use a temporary browser/SSH callback tunnel; see [DEPLOY.md](DEPLOY.md).

For volume controls, distortion checks, persistence limits and safe maintenance, see [the README](README.md#volume-adjustment-and-distortion).

## Service diagnostics

Use your own unit name in place of PLAYER_UNIT:

```sh
systemctl --user list-units --all '*soloist*'
systemctl --user show PLAYER_UNIT -p StandardOutput -p StandardError -p WorkingDirectory
./soloist ctl status
```

`ws: not available` means the optional WebSocket interface is disabled. UnicornTunes's current Spotify control path uses the Web API and does not require that socket. Do not expose Soloist's unauthenticated control socket to browsers or the LAN.

Inspect journal messages locally, then review/redact before sharing. Empty filtered logs do not establish successful authentication or absence of a problem. Restarting the player interrupts its audio; use deliberate restarts for diagnosis rather than a blind recovery loop.

## Optional local-file player: trial procedure

**Status: MPD installation, indexing and audible playback have been tested on the trial device. Reboot behavior and audible coordinated Spotify/local playback remain unverified; mixed-source coordination is implemented and covered by automated tests.**

Follow the [local playback guide](LOCAL_FILES.md) for host desk connection settings, the restricted SSH key and mixed playback testing.

For a small library, designate a folder under the service user's home:

```sh
mkdir -p "$HOME/Music/UnicornTunes"
```

Upload several test MP3s with useful title/artist/album tags. Confirm the files and audio server are available to the service user.

Check existing tools before installing anything:

```sh
command -v mpd
command -v mpc
```

On a new Debian-family installation without an existing MPD setup:

```sh
sudo apt-get update
sudo apt-get install --no-install-recommends mpd mpc
sudo systemctl disable --now mpd.service mpd.socket
mpd --version
```

The package's default system service is disabled because this trial uses a dedicated user service sharing the user's PulseAudio session. On a machine already using MPD, preserve its configuration and service rather than blindly disabling it.

Prepared helper: [setup-local-player.sh](deploy/setup-local-player.sh). Run it as the audio/soloist service user after reviewing it. It has been used on the trial playback device. It:

- Checks for MPD/mpc and the pulse output plugin.
- Uses the designated music folder and dedicated per-user configuration/state directories.
- Binds MPD only to loopback port 6601.
- Creates a dedicated user service without editing Soloist or starting music.
- Refuses to overwrite an existing trial configuration.

After setup, pause Soloist before playing one local file. Stop MPD before resuming Spotify. Verify both routes audibly and after reboot, with no overlap or unexpected autoplay.

Only after this validation should the Docker-to-player connection, combined search and mixed-source queue switching be added. Remote control must have an authenticated access design; the helper does not expose an unauthenticated LAN port.

## Local-file and playlist limitations

Spotify can return local-file playlist metadata without supplying playable audio to Soloist. The native iPhone-style transfer/download workflow has not been established for Soloist, and no playlist-download command is documented in its public controls. The current web app therefore displays local playlist entries as unverified and does not queue them.

The MPD trial plays files accessible on the playback device independently of Spotify. It is not an implementation of Spotify playlist downloads.

## Rebuild checklist

- [ ] OS, architecture, service user and network configuration recorded privately.
- [ ] Audio server and intended sink tested before configuring players.
- [ ] Soloist executable, version, private unit, state/cache paths and account pairing recorded.
- [ ] Normal catalog audio, web session start and request handoff verified.
- [ ] User-service/audio startup verified after reboot without interactive login.
- [ ] Optional MPD installation, configuration, audio and reboot checks verified before claiming support.
- [ ] Backups cover required configuration/state and local media; credentials excluded from public docs.


## Optional cached loudness analysis

Install `ffmpeg` on the playback device and use the v0.7.0 restricted-agent update. Follow [cached loudness and automatic volume](LOCAL_FILES.md#cached-loudness-and-automatic-volume-v070). Each library scan analyzes new/changed files, skips valid cached results and prunes removed entries. Playback adjusts MPD volume on original files; no audio copies are generated. The final source balance needs a listening comparison on the installed output.

## Enable graceful shutdown

On the linked Pine64, use the published v0.8.5 release files. Run `bash deploy/update-local-agent.sh` as the existing UnicornTunes audio user to update the playback helper. Then run the following as an administrator, substituting the actual audio user if it differs:

```sh
sudo bash deploy/enable-player-shutdown.sh foxtunes
```

The helper can schedule one fixed systemd power-off command. The configuration grants the audio user permission for that exact command. Existing forced-command SSH restrictions still apply. The shutdown is scheduled five seconds after acceptance so the Docker server can receive the acknowledgement before the Pine64 goes offline.

In **Host Desk → Playback**, select the linked session player and use **End Session**. Confirm the player name and queue reset. Wait for the board to power down before cutting power at the amp. If shutdown was not acknowledged, UnicornTunes keeps the queue and shows the result in the Host Desk.
