# bdh-app-cameras

Black Diamond Hub app: live camera feeds via go2rtc and UniFi Protect. Moved out of
[BlackDiamondHub](https://github.com/blackdiamondhub/BlackDiamondHub) and
installed into the hub through the `blackdiamondhub.plugins` entry point
(see [bdh-sdk](https://github.com/blackdiamondhub/bdh-sdk)).

Page: `/cameras/`.

Since 2.0.0 (`hub_api` 2) the app runs in its own container under
[bdh-supervisor](https://github.com/blackdiamondhub/bdh-supervisor), with its
own SQLite database at `/data`. It reaches the hub only through the broker RPC
(settings, alerts, Home Assistant state, events). Manifest (`cameras/plugin.py`):

| Field | Value | Why |
|---|---|---|
| `permissions` | `alerts`, `ha.read:*`, `events.subscribe:+/state/#` | Pop-ups, and Home Assistant triggers on any entity in any Home Assistant instance. |
| `network` | `lan` | UniFi Protect (API and websocket) is on the LAN. |
| `lifecycle` | `always` | The events worker keeps the last 24 hours of detections and fires pop-ups. |
| `memory_mb` | 128 | About 63 MB in use with the events worker running. Event clips are exported to a disk cache (`$BDH_DATA_DIR/clips`, at most 2 exports at a time, pruned after 1 hour or past 512 MB) and streamed from there, never buffered in memory. |
| `companions` | `go2rtc` | Live video. The supervisor passes its URL in `BDH_GO2RTC_URL`. |

The go2rtc companion's WebRTC port (8555) is bound on the host, because WebRTC over UDP
can't go through Docker's port proxy. Only one go2rtc per host can use it, so the hub's own
go2rtc must be removed when cameras moves to a container.

Browsers and kiosks never talk to go2rtc or the echo canceller (aec) directly: since 2.7.0b2
(with bdh-supervisor binding both to `127.0.0.1`) the app relays them, checking the guest
rules (`cameras/policy.py`) first:

| Route | Relayed to | Check |
|---|---|---|
| `/cameras/api/ws?src=<stream>` (websocket) | go2rtc `/api/ws` (WebRTC signalling, MSE) | The viewer may see the stream's camera (`can_view`); never a `_talk` stream. |
| `/api/talk?src=<token>` (websocket, the kiosk's `BDHDisplay.talkStart`) | aec `/api/talk`, or go2rtc's without aec | A single-use token from `/cameras/api/talk/prepare/`, which checks `can_view` and `can_talk`; it expires after 30 s. |
| `/cameras/go2rtc/video-stream.js`, `video-rtc.js` | go2rtc's player scripts (cached) | None (static code). |

WebRTC media still flows directly between the browser and go2rtc on port 8555; it's only
reachable with the session set up over the relayed signalling.

## Configuration

Admins add the UniFi Protect consoles (NVRs) on **Settings → NVRs** (`/cameras/settings/nvrs/`):
name, host, integration API key (live streams and PTZ) and an optional local, view-only Protect
account (events and recordings). Keys and passwords are stored as hub secrets.

| Setting (env var) | Purpose |
|---|---|
| `BDH_CAMERAS_EVENTS=off` | Don't start the events worker. |

### Moving to a container

1. On the hub, export the app's rows: `python -m bdh_sdk.appdata export cameras -o cameras.json`.
2. Copy `cameras.json` into the app's data volume, then in the container run
   `python -m bdh_sdk.runtime.manage import_app_data /data/cameras.json --replace`.
3. Add the NVRs on **Settings → NVRs**. The app doesn't read `UNIFI_PROTECT_*`.
4. Remove the hub's go2rtc and set `BDH_GO2RTC_CANDIDATES` (the hub's LAN `ip:8555`) on the
   supervisor.

## Events and recordings

When a site has a Protect account, the app's container keeps the last 24 hours of
detections, using the private Protect API via [uiprotect](https://github.com/uilibs/uiprotect).
The detections are Doorbell, Person, Vehicle, Package and Animal, plus plain Motion if you turn
it on. Choose which ones show up in the app's Settings.

- Admins get a "Last 24 hours" rail, plus a time scrubber on a full-screen camera for
  snapshots and replay, with everything recorded. Guests get them only if the Guests settings
  page allows it, and only from its window on (see [Guests](#guests)).
- A pop-up is a hub alert that embeds `/cameras/popup/<camera_id>/`, a live-only page. Choose
  what pops up each camera on the Pop-ups settings page (below); set how long it stays open in
  Settings.

## Pop-ups

Admins choose what pops up each camera on **Settings â†’ Pop-ups** (`/cameras/settings/popups/`):

- **Detections:** Ring (doorbells only), Person, Vehicle, Package or Animal. A detection pops up
  once per Protect event, and only within 30 seconds of the event starting.
- **Also pop up when:** any Home Assistant entity. Pick a device (grouped by room, rooms named like
  the camera first), then one of its entities, then what it does ("opens", "turns on", "unlocks"…),
  or search devices and entities by name, room or ID. Devices with no trigger-like entities
  (binary sensors, locks, covers, switches, `event.*`…) and diagnostic/config entities are hidden
  until you ask for them. By default a binary sensor fires on `on`, a lock on `unlocked` and an
  `event.*` entity on each new event; other entities fire on any state change, or on the state
  you choose. Tap a trigger to change or remove it.
- **Sound:** each detection and trigger plays a hub chime when it pops up (Ring: Ding-dong,
  Person: Notify, other detections: Single ding, triggers: Knock-knock), or pick another chime or
  Silent; ▶ previews it. Pop-ups are silent in Night mode and the volume is under Settings → Display
  (needs a hub with chimes, BlackDiamondHub #258).
- **Who sees it:** Everyone, or Only when signed in (the hub shows it only on screens with an
  admin signed in). Cameras hidden from guests are always Only when signed in.

A master switch turns all pop-ups off. Changes apply at once. A pop-up shares one alert per camera,
so another trigger while it is showing just restarts its timer (`popup_seconds`) without chiming again.

Until the page is first saved, a ring pops up its doorbell camera.

## Guests

A guest is anyone who isn't signed in as an admin (an active staff user). Admins see and do
everything; **Settings → Guests** (`/cameras/settings/guests/`) sets what guests get:

- **Cameras:** each NVR is Visible or Hidden to guests, and any camera can be set the other way.
  NVRs added later are hidden until you choose. Until the page is first saved, guests see the
  first NVR only.
- **Talk** and **PTZ presets:** on or off for guests (both on by default).
- **Events:** Off (default, live video only), Last 24 hours, or Since a date and time (the hub's
  local time). Guests then get the events rail, snapshots and replay, for the cameras they see
  and only from then on.

The rules are enforced on the server for every page and API serving camera data (the cameras
page, the home page tile, pop-ups, talk, PTZ, events, thumbnails, snapshots and clips): a camera
hidden from guests is a 403/404 for them everywhere. The settings pages are admin-only.

## Door unlock (UniFi Access)

Optional, admin-only: a camera can be mapped to a UniFi Access door (an electric strike), and
admins then get a round lock button on that camera in the full-screen player (bottom left), on
the hero tile and in its doorbell pop-up. Guests never see it, and the server refuses unlocks
from anyone who isn't an admin. An NVR without Access settings shows nothing door-related.

**Token:** in the UniFi Access app, Settings → API Token → create one with **Locations: Edit**
and every other permission set to None.

**Settings → NVRs → (NVR) → Door unlock:** enter the Access console's IP address (it need not be
the NVR's — e.g. a separate Cloud Gateway) and the token, then **Find doors** to list the doors and
pick one per camera (exact or prefix name matches, like "Front Door - Entry" → "Front Door", are
preselected). The token is kept in the hub's vault (`access_token_<nvr id>`); leave it blank to
keep the saved one. **Turn off door unlock** forgets the console, token and mapping. Every
NVR on every hub can be set up, so one house's hub can unlock the other's door over the VPN.

**Hold to unlock:** press and hold the button for 3 seconds while the ring fills; letting go,
sliding off, switching camera or replay cancels it. Space/Enter held does the same. The hub then
asks Access for a momentary unlock (Access relocks the door itself), shown in the Access log as
"BlackDiamondHub <hub> – <admin>". No reply within the timeout shows "No reply — the door may
have unlocked"; nothing is retried, and a second press within 3 s of a success isn't sent again.

**Notifications:** every unlock (and every "may have unlocked") is logged and published on the
admin-only `cameras/door/unlocked` event topic, e.g. "Front Door (Sun Peaks) unlocked by stefan".

**Certificate pinning:** the Access console's self-signed certificate is pinned (SHA-256) when
the settings are tested or saved; if the console's certificate changes, unlocks fail until you
**Find doors** and save again.

## Screen saver

While the cameras page is open the hub screen saver stays off, so the cameras stay on screen.
Turn this off with the "Leave cameras open when idle" setting (`keep_open`).

## Live video transport

"Advanced: live video transport" (`video_transport`) applies to every stream on every site,
including pop-ups:

- **TCP** (default, go2rtc mode `webrtc/tcp`): lost packets are resent by TCP, so a lossy Wi-Fi
  or busy link slows video down instead of freezing it.
- **UDP** (`webrtc`): lower latency, falls back to TCP if UDP can't connect. go2rtc 1.9.15 never
  answers the browser's NACKs (it doesn't read RTCP from its senders), so every lost UDP packet
  breaks a frame until the next keyframe.

## Stats overlay

Tap the cameras page with three fingers to show or hide WebRTC stats on every camera: stream
(low or high res), transport (UDP/TCP), resolution, fps, codec, bitrate, RTT, jitter, packets lost,
NACKs, PLIs, freezes, dropped frames and decoder. In full screen it shows the stream on screen.
It updates once a second and draws plain text (no blur), so it doesn't slow the display.

## Display performance

The hubs run on UniFi Connect displays: Chrome on a weak GPU, decoding up to 9 live WebRTC
streams at once. Keep the camera pages cheap to draw:

- Animate only `opacity` or `transform`, on as few elements as possible. Animations that repaint
  (`background-position`, gradients, `box-shadow`, `filter`, size changes) starve the GPU, and
  Chrome drops incoming video frames until they stop.
- In 1.7.3 a `background-position` shimmer on the tiles held back first video by 3–5 s and
  sometimes tripped the 6 s WebRTC→MSE fallback. The opacity pulse that replaced it (1.7.4)
  plays all tiles in about 2–3 s.
- Never use `backdrop-filter` on overlays that sit over live video (labels, status pills, PTZ
  and fullscreen buttons). The blur is recomputed on every video frame. Up to 1.7.4 the blurred
  pills on each tile made the display drop ~85% of frames on the 9-camera Sun Peaks tab. A solid
  70% dark background (1.7.5) looks almost the same and plays at a full 30 fps.
- A desktop browser will not show the problem. Test UI changes on a real display.
- In the full-screen player the hidden live stream is paused while you scrub or replay, and
  dropped after 3 s (a paused WebRTC video still decodes); it reconnects when you go back to live.

## Scrubber

Hovering or dragging along the scrubber shows a frame card above the pointer: the frame at that
time, or the event's thumbnail near an event mark. While dragging, the card and the big image use
320 px frames; the full 1280 px frame loads on release. A cold Protect fetch at either size fills the
server cache for both, so release usually hits the cache. Frame times snap to 5 s so nearby
positions share one browser-cached image.

## Development

CI (`.github/workflows/ci.yml`) uses the reusable `app-ci.yml` from
bdh-sdk. It builds the wheel, installs it next to the hub, then runs
`hub_smoke`, the app tests and the selenium tests.

Its `image` job locks the dependencies, builds the app's container image the way the platform
does (bdh-sdk `builder/`, FROM `bdh-app-runtime:hub-api-2`, without PyAV via `[tool.bdh]
pip_exclude`), starts it hardened like the supervisor with a 96 MB limit and an unreachable
Protect account, waits for `/_bdh/health` and reports RSS/PSS in the job summary.

To release, bump `version` in `pyproject.toml`. When that lands on `main`, `release.yml` tags it
and publishes the wheel, `bdh-lock.txt` and `bdh-app.json`. The catalog's builder
(`blackdiamondhub/bdh-app-catalog`, `build.yml`) then builds, attests and pushes
`ghcr.io/blackdiamondhub/bdh-app-cameras` and adds the version to the catalog.
