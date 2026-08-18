# Spotify Connect on Mars — deferred plan

Status: **parked**. This branch (`feat/spotify`) holds no implementation — only this document.

Music shipped first as Bluetooth A2DP instead (branch `feat/bluetooth-speaker`), because
go-librespot is an unofficial reverse-engineered Spotify client: it cannot go on customer
robots while the Commercial Hardware eSDK application is open, and Spotify can disable it
fleet-wide at any time. Revisit this document when the eSDK moves, or for internal-only builds.

Most of the work below is already done by the Bluetooth branch. Spotify Connect adds exactly
one capability the Bluetooth path cannot have: **play by name**.

## Approach

`go-librespot` (`devgianlu/go-librespot`) as a systemd unit. The user picks "Mars" once in
their phone's Spotify app; ZeroConf hands credentials device-to-device, and go-librespot
outputs to the `music` ALSA PCM the Bluetooth branch already defines.

```
phone Spotify app ──ZeroConf──►┌───────────────────────────────┐
                               │ go-librespot (127.0.0.1:3678) │
agent skills ─────HTTP────────►│ /player/*   transport         │
                               │ /status /events  state        │
webapp proxy ─────HTTP────────►│ /web-api/*  Spotify Web API   │
                               └───────────────┬───────────────┘
                                               │ audio_backend: alsa
                                 ┌─────────────▼──────────────┐
                                 │ pcm.music (softvol "Music")│ ← existing ducking node
                                 │   └─ softvol "Master"      │ ← existing SetVolume
                                 │        └─ dmix ─ hw:APE,0  │ ← shared with TTS
                                 └────────────────────────────┘
```

## The finding that makes this small

go-librespot's [api-spec.yml](https://github.com/devgianlu/go-librespot/blob/master/api-spec.yml)
exposes two endpoints beyond transport:

- `POST /token` — "Returns a Spotify access token for the active session"
- `GET|POST|PUT|DELETE /web-api/{path}` — "Proxy to the Spotify Web API", query forwarded as-is

The active session is the ZeroConf login. So once the user has picked Mars — which they must do
anyway for playback to work — the daemon already holds user-level credentials and proxies the
whole Web API as that user: search, playlists, liked songs, recently played.

That means **none of the following is needed**: a Spotify Developer app, `client_id`/`secret`,
client-credentials token minting, an OAuth PKCE flow, a registered redirect URI, a relay route
on `link.innate.bot`, or any token storage on the robot. An earlier draft of this plan built
all of it. It is unnecessary.

Caveats: the token comes from librespot's own `client_id`, not a registered Innate app — the
same unofficial-client exposure that deferred this work. And set `X-Spotify-Scope` explicitly
per call; [requesting every scope at once breaks some
endpoints](https://github.com/librespot-org/librespot-java/issues/401).

## Endpoints

Verified against the spec at the time of writing; re-check against the pinned release.

| Method | Path | Notes |
|---|---|---|
| GET | `/status` | `paused`, `stopped`, `volume`, `volume_steps`, `track` |
| GET | `/events` | websocket event stream |
| POST | `/player/play` | `{uri, skip_to_uri?, paused?, position?}` |
| POST | `/player/pause` \| `/resume` \| `/stop` | no body |
| POST | `/player/next` | `{uri?}` |
| POST | `/player/prev` | **`prev`**, not `previous` |
| POST | `/player/volume` | `{volume}`, scaled against `volume_steps` from `/status` |
| POST | `/token` | access token for the active session |
| ANY | `/web-api/{path}` | Spotify Web API proxy |

## What to build

Everything here is additive to the Bluetooth branch. The ducking node, the `music` ALSA PCM,
the webapp mini-player, `whats_playing` and `control_music` are all shared and need no second
implementation — point them at a second backend rather than duplicating them.

1. **Install** — a checksum-pinned tarball helper next to `fetch_asset`
   (`scripts/update/post_update.sh:136-150`), which already does verified, self-healing,
   non-fatal downloads. Nothing in the repo untars or `chmod +x`es a download yet, so that
   helper is the one new primitive. Prefer this over the `innate-packages` apt repo: that is a
   signed channel the robot boots from, and an unofficial Spotify client does not belong in it.

2. **Gating** — keyed on `MUSIC_ENABLED=1` in `.env`. **The unit must not live in
   `config/systemd/`**: that directory is installed wholesale at `post_update.sh:476-494` and
   enabled at :990-1015, so anything placed there ships on. Put the template in
   `config/go-librespot/` and install it from the opt-in block. The `else` branch must
   `systemctl disable --now` and remove the unit, so turning the flag off actually turns it off.

3. **Config** (`config/go-librespot/config.yml.template`, seeded copy-if-missing like
   `settings.yaml` at `post_update.sh:316-325` — a copy, not a symlink, since the daemon writes
   `state.json` and a lockfile into that directory):

   ```yaml
   device_name: Mars
   device_type: speaker
   audio_backend: alsa
   audio_device: music
   mixer_device: ""            # internal volume; never touches Master
   zeroconf_enabled: true
   credentials: { type: zeroconf, zeroconf: { persist_credentials: true } }
   server: { enabled: true, address: 127.0.0.1, port: 3678 }
   ```

   **`address: 127.0.0.1` is non-negotiable.** `/token` and `/web-api/*` are an unauthenticated
   full-account Spotify proxy; on `0.0.0.0` they would be "delete all my playlists" on the LAN.

4. **The one new skill** — `play_music.py` in `workspace/innate_skills/music/`:
   `execute(query: str, kind: Literal["track","album","artist","playlist"] = "track")`. Resolve
   a pasted link locally, otherwise `GET /web-api/v1/search`, then `POST /player/play {uri}`.
   `set_music_volume.py` also becomes meaningful here.

5. **Webapp** — if the proxy exposes search or library, allowlist read-only shapes server-side
   (`webapp/proxy/`, registered in `build_app()` before the `/{tail:.*}` catch-all). **Never
   pass `/web-api/*` through raw.** The SPA owns `/music`, so an API route needs the
   `/settings` → `/settings.json` treatment.

6. **Sim** — the container has no audio device and bridge networking kills ZeroConf. Run
   go-librespot control-only with `credentials.type: interactive` and let the browser play via
   the Spotify Web Playback SDK. That needs Premium, EME, and a secure context
   (`http://localhost` qualifies; `http://<lan-ip>` does not). Payoff: `/token`, `/web-api/*`
   and `/player/*` then exist in both environments, so skills and webapp code are identical and
   the only branch is whether local audio output exists.

## Risks

- **mDNS 5353 contention.** avahi already runs (the robot is `<host>.local`). go-librespot's
  builtin ZeroConf backend may fail to bind or silently not advertise; its avahi backend may
  need client libs added to `ros2_ws/apt-dependencies.hardware.txt`. Verify on hardware first —
  this is the biggest schedule risk.
- **Same-L2 requirement.** If Mars is on its own AP or a separate VLAN from the phone, it never
  appears in the device picker. No software fix.
- **Premium required**, and `persist_credentials` must survive a reboot or the demo story
  changes materially — verify.
- **Credentials at rest** in `~/.config/go-librespot/state.json`, plus a loopback API handing
  out access tokens to any local process. Keep 0600 modes and check no support-bundle path
  sweeps `~/.config`.
- **Licensing.** The reason this is parked. Keep "PROTOTYPE" wording in the unit `Description=`
  and in `.env.template` if it is ever built.
