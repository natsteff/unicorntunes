# Deploy UnicornTunes with Docker Compose

UnicornTunes runs on the Docker host. Spotify audio plays on a separate Connect device, such as a PINE A64+ running Soloist. Install and pair that player separately. This package does not include a Soloist binary.

## Requirements

- Docker Engine and Docker Compose v2 on Linux (amd64 or arm64).
- A Spotify Premium account and a dedicated Web API app in the Spotify Developer Dashboard.
- Nginx Proxy Manager or another reverse proxy, plus local DNS for `unicorntunes.nate`.
- A Connect player associated with the same Spotify account.

Use one UnicornTunes container per data volume. Separate installations need separate data and configuration. The host's Start session activates the selected Connect player through Spotify's Web API. No other Spotify client needs to be running for this control flow; the player itself must be paired, online and discoverable. Saved named locations are not included yet.

## Install

Download `compose.yml` and `.env.example` from [natsteff/unicorntunes](https://github.com/natsteff/unicorntunes) into a server directory such as `/opt/unicorntunes`. Copy `.env.example` to `.env` and edit:

```dotenv
SPOTIFY_CLIENT_ID=your_32_character_client_id
FOXTUNES_IMAGE=ghcr.io/natsteff/unicorntunes:latest
FOXTUNES_PUBLIC_URL=http://unicorntunes.nate
FOXTUNES_BIND_IP=192.168.1.50
FOXTUNES_PORT=8863
```

Replace `192.168.1.50` with the Docker host's LAN IP. If Nginx runs directly on that host, use `127.0.0.1` instead. A separate Nginx Proxy Manager container cannot reach the host's loopback address as its upstream. Restrict the backend port to your trusted LAN/proxy; do not expose it on your router.

GHCR package visibility is separate from the public source repository. If the image is private, authenticate the Docker server to GHCR using a GitHub personal access token (classic) with `read:packages` and package access. Enter it at the password prompt; do not put it in Compose or `.env`:

```sh
docker login ghcr.io -u YOUR_GITHUB_USERNAME
docker compose pull
docker compose up -d
docker compose ps
```

GitHub's [registry authentication guide](https://docs.github.com/en/packages/working-with-a-github-packages-registry/working-with-the-container-registry) covers private-package access. If downloading the source instead, build locally:

```sh
docker compose -f compose.yml -f compose.build.yml up -d --build
```

The source repository is public; this local-build path requires no GitHub login or GHCR image access. For subsequent local-build operations, continue using both Compose files.

## Nginx Proxy Manager

Create a Proxy Host:

- Domain: `unicorntunes.nate`
- Forward scheme: `http`
- Forward hostname/IP: Docker host's LAN IP
- Forward port: `8863`
- Preserve the original Host header (the normal proxy behavior).
- WebSockets are not required; this version uses HTTP polling.

Local DNS must resolve `unicorntunes.nate` to Nginx Proxy Manager from guest phones/tablets too. Add SSL only with a certificate covering this name, whose issuer your devices trust. An internal wildcard certificate may require installing the internal CA on devices. Do not bypass certificate warnings.

To serve both HTTP and HTTPS, leave **Force SSL** off and configure a trusted certificate. To redirect all browser traffic to HTTPS, enable **Force SSL**. Set `FOXTUNES_PUBLIC_URL` to the preferred public address and recreate the container after changing it. For the configured hostname, the proxy must overwrite X-Forwarded-Proto with its own request scheme. UnicornTunes checks each browser POST against that scheme and the configured authority, and uses separate cookies when serving both schemes. Keep the configured public URL for shared links and the Spotify callback. Keep the backend private so clients access it through Nginx.

See `deploy/nginx.conf.example` for a plain Nginx equivalent. Proxy/certificate/DNS setup is performed on your infrastructure, not by the image.

## Host login

Visit `http://unicorntunes.nate/host` (or HTTPS if configured). A random password is generated on first startup and retained in the data volume. Read it on the server:

```sh
docker compose exec foxtunes python -c 'import json; print(json.load(open("/data/web-host.json"))["password"])'
```

Keep it private. The host password is separate from Spotify credentials. On plain HTTP, host passwords and session cookies are unencrypted; use only a trusted LAN for testing. To replace it, stop the container, update `web-host.json` securely in the volume, and restart; do not publish that file. Guest browsers never receive Spotify tokens.

## Spotify login: trusted HTTPS

In the Spotify app settings register exactly:

```text
https://unicorntunes.nate/callback
```

Sign in to the UnicornTunes host desk and click **Connect / reconnect Spotify**. Authorize the same account used by the player. Authorization uses PKCE and a short-lived, single-use state, not a client secret. Tokens remain on the server in the volume. Host and guest sessions expire on container restart; Spotify authorization remains.

If Spotify rejects an internal hostname/callback or certificate trust is unavailable, use the loopback flow below. Browser OAuth over the actual private reverse proxy has to be verified in that environment.

## Spotify login: HTTP / loopback fallback

Spotify permits HTTP callbacks on explicit loopback IPs. Register this additional callback in the Spotify app:

```text
http://127.0.0.1:8765/callback
```

The Compose package publishes port 8765 only on the Docker host's loopback interface. From your laptop/desktop, keep an SSH tunnel open (replace the destination):

```sh
ssh -N -L 8765:127.0.0.1:8765 user@DOCKER_HOST
```

In another SSH terminal on the Docker server:

```sh
docker compose exec foxtunes python foxtunes.py login --no-browser
```

Open the printed authorization URL in the laptop/desktop browser while the tunnel is open. Complete Spotify login within three minutes. The browser callback travels through the tunnel to the container. Do not share the temporary URL. This writes the same server-side token file used by the web worker. Do not run separate sign-ins/refresh tools concurrently. Close the tunnel after success.

The callback listener binds all container interfaces only while this CLI login is running; Docker exposes it only through host loopback. Do not publish callback port 8765 to the LAN. No existing Mac token file needs to be copied.

## Start a listening test

1. Ensure the dedicated player is running and paired with the same Spotify account as UnicornTunes.
2. Request a song in UnicornTunes if the player has no existing playback context.
3. In the host desk, refresh devices, select the player and click **Start session**. This transfers playback, resumes the existing context or starts the top request if there is no context, and turns shuffle/repeat off.
4. UnicornTunes enables automatic handoff only after it observes the selected device playing. Confirm **Running**. No handoff checkbox or manual Spotify app activation is needed.
5. **Move / restart session here** applies the same flow to the selected target. **Stop session** stops dispatch while current music continues. Host Pause stops dispatch. Skip to next request immediately starts the next UnicornTunes request and preserves a running session; when no requests are pending, it advances Spotify normally. A handed-off request is advanced to without adding its URI again. An unconfirmed skip stops dispatch for reconciliation.

If Spotify refuses transfer/control or does not report playing state, the session stays stopped with an actionable error. Refresh discovery if a device disappears or its ID changes. An accepted command is not playback proof. No local Soloist WebSocket or PINE service configuration is changed by this release.

Failed settings validation preserves the last valid configuration. Device takeover/API failures stop handoff with a persistent **Needs attention** reason; a later successful status read does not hide that stop. Settings controls reflect server state after a failed save. Existing Spotify queued items can still precede a new request. A request disappearing from current playback is not proof of full playback or audible sound.

## Persistence, updates and rollback

The `foxtunes_data` named volume contains requests/votes, host password, Spotify tokens, selected device and request acceptance setting. Handoff deliberately starts stopped after container restart; verify the device and re-enable it. This avoids unexpected playback changes after maintenance. Browser sessions are not persisted; clearing cookies/restarting creates a new anonymous identity.

For updates, keep `FOXTUNES_IMAGE=ghcr.io/natsteff/unicorntunes:latest`, then:

```sh
docker compose pull
docker compose up -d
```

Do not use `down -v` unless you intentionally want to delete all data. Before updates, stop the container and take a protected snapshot/backup of the volume using your Docker host's backup tools. Keep credentials and listening activity private. Restart after the backup. SQLite must be backed up consistently; do not copy a changing database file by itself.

To roll back, stop the service and restore the previous image tag and its matching volume backup if a newer release changed the data schema. Version 0.1.0 adds settings storage without changing the earlier web database schema. There is no automatic updater or public internet exposure.

### Playback-device updates

- **Docker app:** An app release publishes a tagged image for the server to pull with `dcupd`. Check the app version under the logo after updating. The v0.8.2 browse and MPD handoff fixes are in this image.
- **Restricted MPD agent:** Update on the Pine64 when release notes specify a minimum agent version for a feature. Run the versioned helper as the existing audio user and verify the reported agent version and the host connection. [Agent update procedure](LOCAL_FILES.md#update-the-playback-device)
- **Spotify Soloist:** Track each board’s build timestamp and plan replacement before Spotify’s 90-day build expiry. Keep Soloist maintenance separate from pulling the Docker image. [Pine64 update procedure](SOLOIST_DEVICE_SETUP.md#update-spotify-soloist)
- **Local library and system packages:** Scan library changes after music files change. Maintain Armbian, MPD and PulseAudio on the board separately and verify audio after a reboot.

## Publishing a version

The repository's GitHub Actions workflow runs tests on main/PRs. A version tag publishes both its numbered image and `ghcr.io/natsteff/unicorntunes:latest` for amd64 and arm64 using the repository's scoped `GITHUB_TOKEN`. The first GHCR publication defaults private; verify package visibility/access after publication. Grant server users read access as needed. No personal GHCR write credential is stored in the repository. Tests, private records and runtime data are excluded from the image by an allowlist build context.

## Existing installation: repository / directory rename

The repository is now `https://github.com/natsteff/unicorntunes`. A directory rename must retain the existing Compose project identity. The supplied Compose file explicitly sets `name: foxtunes`, preserving the original `foxtunes-foxtunes-1` container and `foxtunes_foxtunes_data` volume regardless of the checkout directory name. Service/volume keys and FOXTUNES_* settings are retained for compatibility; the public URL is configurable, with unicorntunes.nate used in new examples. Changing repository/image names does not migrate or erase runtime data.

After moving the checkout to `/opt/unicorntunes` yourself, run:

```sh
cd /opt/unicorntunes
git remote set-url origin https://github.com/natsteff/unicorntunes.git
git pull --ff-only
sed -i 's|^FOXTUNES_IMAGE=.*|FOXTUNES_IMAGE=ghcr.io/natsteff/unicorntunes:latest|' .env
docker compose -f compose.yml -f compose.build.yml config --volumes
docker compose -f compose.yml -f compose.build.yml up -d --build
```

Keep the existing `.env` and volume. Do not run `down -v`. If a shell or `.env` already sets `COMPOSE_PROJECT_NAME`, it overrides the file: ensure it is `foxtunes` for this existing installation. Do not use `-p unicorntunes` with the existing data. A separate future installation can intentionally use a different `-p` project with its own data. The old GHCR package is not renamed or removed; this release publishes to the new `unicorntunes` image path. Existing `.env` files must update FOXTUNES_IMAGE explicitly, because they override the Compose default.


## Playlist browsing permissions (v0.3.0+)

After upgrading, reconnect Spotify once to grant `playlist-read-private` and `playlist-read-collaborative`. Use the trusted HTTPS flow or the HTTP/loopback fallback above. Refreshing a previously granted token does not expand its permissions. In the host desk, use Guest playlist selection to enable playlists after contents access is verified. Enabling a private playlist shares its returned metadata/tracks with guest browsers. Spotify may restrict item access to owned/collaborative playlists. Local-file rows are informational; no local-file sync/download is performed.
