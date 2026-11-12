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
- On a doorbell ring the app pushes a hub alert that embeds `/cameras/popup/<camera_id>/`,
  a live-only page. You can turn the pop-up off and set how long it stays open in Settings.

## Event pop-ups

The app can also pop up a camera when a hub event fires (`bdh_sdk.events`), for example a door
opening in Home Assistant. Map event topics to Protect camera names (case-insensitive) in the
site's `site.toml`:

```toml
[apps.cameras]
event_popups = { "ha/state/binary_sensor.front_door" = "Front Door" }
```

or as JSON in `BDH_CAMERAS_EVENT_POPUPS`. A state change pops up only on `off` → `on`; any other
event (e.g. a webhook) always pops up. The pop-up shares the doorbell's alert, so a ring or another
event while it is showing just restarts its timer (`popup_seconds`). Turn it off with the
"Event pop-ups" setting. Changes to `event_popups` need a hub restart.

## Screen saver

While the cameras page is open the hub screen saver stays off, so the cameras stay on screen.
Turn this off with the "Leave cameras open when idle" setting (`keep_open`).

## Development

CI (`.github/workflows/ci.yml`) uses the reusable `app-ci.yml` from
bdh-sdk. It builds the wheel, installs it next to the hub, then runs
`hub_smoke`, the app tests and the selenium tests.

To release, bump `version` in `pyproject.toml`. When that lands on
`main`, `release.yml` tags it and publishes the wheel, and the release
notes include the hashed line for the hub's `sites/<site>/apps.txt`.
