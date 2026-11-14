# Playback-device setup and rebuild guide

UnicornTunes uses a Docker-hosted web application and a separate Linux playback device connected to the stereo. Spotify Soloist handles Spotify audio. The optional MPD local-file player is currently a trial; mixed-source search and playback integration are not implemented yet.

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

## Validate Spotify playback

1. Start the Soloist user service.
2. Open Spotify on a phone or computer with the intended account.
3. Select your Soloist device and play a normal catalog song. Verify audio through the stereo.
4. In UnicornTunes's host desk, refresh devices, choose that player and start the session.
5. Test one guest request and observe handoff.
6. Reboot and repeat the audio/session checks without first opening an SSH login.

A phone or Mac is not an ongoing playback dependency. Spotify authorization setup for HTTP deployments may use a temporary browser/SSH callback tunnel; see [DEPLOY.md](DEPLOY.md).

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


## Optional local loudness preparation

Install the `ffmpeg` package on the playback device and use the v0.5.0 restricted-agent update. Follow [Loudness matching](LOCAL_FILES.md#loudness-matching-v050) to prepare, verify and enable non-destructive FLAC playback copies through the host page. The helper does not change Soloist, SSH permissions or original media. Allow space for up to 4 GiB of copies plus the 512 MiB free reserve; the final source balance must be compared on the installed audio output.
