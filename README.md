# bdh-app-cameras

Black Diamond Hub app: live camera feeds via go2rtc and UniFi Protect. Moved out of
[BlackDiamondHub](https://github.com/blackdiamondlodge/BlackDiamondHub) and
installed into the hub through the `blackdiamondhub.plugins` entry point
(see [bdh-sdk](https://github.com/blackdiamondlodge/bdh-sdk)).

Page: `/cameras/`.

## Configuration

Each Protect site `n` (1..9) is configured with environment variables:

| Variable | Purpose |
|---|---|
| `BDH_CAMERAS_PROTECT_HOST_n`, `BDH_CAMERAS_PROTECT_API_KEY_n` | Required. Protect integration API key for live streams and PTZ. |
| `BDH_CAMERAS_PROTECT_NAME_n` | Site name shown on the tabs. |
| `BDH_CAMERAS_PROTECT_USERNAME_n`, `BDH_CAMERAS_PROTECT_PASSWORD_n` | Optional local, view-only Protect account. Turns on events and recordings. |
| `BDH_CAMERAS_EVENTS=off` | Don't start the events worker. |

## Events and recordings

When a site has a Protect account, the hub's server process keeps the last 24 hours of
detections, using the private Protect API via [uiprotect](https://github.com/uilibs/uiprotect).
The detections are Doorbell, Person, Vehicle, Package and Animal, plus plain Motion if you turn
it on. Choose which ones show up in the app's Settings.

- Signed-in staff get a "Last 24 hours" rail, plus a time scrubber on a full-screen camera
  for snapshots and replay. Guests only see live video.
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

A master switch turns all pop-ups off. Changes apply at once. A pop-up shares one alert per camera,
so another trigger while it is showing just restarts its timer (`popup_seconds`) without chiming again.

Until the page is first saved, the old settings still apply (and pre-fill the page): a ring pops up
its doorbell when `doorbell_popup` is on, and when `event_popup` is on each topic in the site's
`event_popups` (`site.toml` `[apps.cameras]` or JSON in `BDH_CAMERAS_EVENT_POPUPS`) pops up the
camera it names. Once saved, `event_popups` is ignored and can be removed.

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

To release, bump `version` in `pyproject.toml`. When that lands on
`main`, `release.yml` tags it and publishes the wheel, and the release
notes include the hashed line for the hub's `sites/<site>/apps.txt`.
