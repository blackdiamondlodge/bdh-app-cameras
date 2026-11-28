import base64
import json
import logging
import re
import threading
import time
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import wraps

from bdh_sdk import push
from bdh_sdk.rpc import BrokerError
from django.core.cache import cache
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from . import access_api, clips, config, events, frames, policy, popups
from .models import ProtectEvent
from .protect_api import (
    get_protect_cameras, ptz_goto_preset, refresh_talkback, talk_stream_name, talkback_source,
)

logger = logging.getLogger(__name__)

# go2rtc's player websocket, relayed by cameras.routing.watch.
WATCH_PATH = '/cameras/api/ws'

CLIP_DEFAULT = timedelta(seconds=30)
CLIP_MAX = timedelta(seconds=120)


@push.authorize
def push_topics(request):
    """Only viewers who see the events rail hear its updates."""
    if policy.is_admin(request):
        return ["cameras/#"]
    return [events.CHANGED_TOPIC] if policy.can_see_events(request) else []


def get_go2rtc_streams(go2rtc_url):
    """Fetch the list of configured streams from go2rtc API.

    Used as a fallback when UniFi Protect is not configured — shows
    whatever streams are already registered in go2rtc (e.g. manually
    configured in go2rtc.yaml).
    """
    try:
        req = urllib.request.Request(f'{go2rtc_url}/api/streams')
        with urllib.request.urlopen(req, timeout=5) as response:
            data = json.loads(response.read())
            return [
                {
                    'name': name,
                    'display_name': name.replace('_', ' ').replace('-', ' ').title(),
                }
                for name in sorted(data.keys())
            ]
    except (urllib.error.URLError, json.JSONDecodeError, OSError) as e:
        logger.warning("Failed to fetch streams from go2rtc at %s: %s", go2rtc_url, e)
        return []


def _register_streams_with_go2rtc(go2rtc_url, cameras):
    """Register camera RTSP streams with go2rtc via its API.

    Registers both high and low quality streams for each camera:
    - {stream_name} — high quality (used for fullscreen)
    - {stream_name}_low — low quality (used in grid view)

    Registers streams that are missing from go2rtc, and re-registers ones
    whose source URL has changed (e.g. after the RTSPS token was rotated).
    The streams are also remembered so the keeper can restore them if
    go2rtc restarts (it has no config file, so a restart forgets them).
    """
    wanted = _remember_streams(go2rtc_url, cameras)
    _sync_streams(go2rtc_url, wanted)


def _remember_streams(go2rtc_url, cameras):
    """Add the cameras' high/low streams to the keeper's set; returns them."""
    wanted = {}
    for camera in cameras:
        low_url = camera.get('rtsp_url_low', '')
        if camera.get('has_mic'):
            # Protect sends AAC, which WebRTC can't play; go2rtc only runs
            # this ffmpeg opus track while a viewer asks for audio.
            audio_src = low_url or camera['rtsp_url']
            wanted[camera['stream_name']] = (
                camera['rtsp_url'], f"ffmpeg:{audio_src}#audio=opus",
            )
        else:
            wanted[camera['stream_name']] = camera['rtsp_url']
        if low_url:
            wanted[f"{camera['stream_name']}_low"] = low_url
        if camera.get('talkback'):
            wanted[talk_stream_name(camera['stream_name'])] = talkback_source(camera['talkback'])
        if listen_stream_name(camera) != camera['stream_name']:
            # Video from Protect, sound from the echo canceller (which reads
            # the mic from the _low stream and the talk it proxies).
            wanted[listen_stream_name(camera)] = (
                f"{camera['rtsp_url']}#media=video",
                f"{config.aec_url()}/aec.ts?cam={urllib.parse.quote(camera['stream_name'], safe='')}",
            )
    with _keeper_lock:
        _wanted.setdefault(go2rtc_url, {}).update(wanted)
    return wanted


def listen_stream_name(camera):
    """The stream to watch and hear a camera on full screen.

    Two-way cameras go through the echo canceller when it's set up, so you
    don't hear yourself back from the doorbell.
    """
    name = camera['stream_name']
    if (config.aec_url() and camera.get('talkback') and camera.get('has_mic')
            and camera.get('rtsp_url_low')):
        return f"{name}_aec"
    return name


def _seed_from_protect():
    """Remember every Protect camera's streams without waiting for a page load.

    After an app update the process (and go2rtc) restart with nothing
    registered, while an already-open wall display never reloads the page,
    so page loads alone would leave go2rtc empty indefinitely.
    """
    if not config.protect_sites():
        return
    cameras = [cam for site in get_protect_cameras() for cam in site.get('cameras', [])]
    if cameras:
        _remember_streams(config.go2rtc_url(), cameras)


# go2rtc url -> {stream name: source url} registered by page loads.
_wanted = {}
_keeper_lock = threading.Lock()
_keeper_started = False
KEEPER_INTERVAL = 10


def _sync_streams(go2rtc_url, wanted, require_listing=False):
    """PUT every wanted stream that go2rtc is missing or has a stale source for.

    Returns the number of streams registered, or None when go2rtc couldn't be
    listed and ``require_listing`` is set.
    """
    try:
        req = urllib.request.Request(f'{go2rtc_url}/api/streams')
        with urllib.request.urlopen(req, timeout=5) as response:
            existing = _stream_sources(json.loads(response.read()))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        if require_listing:
            return None
        existing = {}

    to_register = [
        (name, srcs) for name, srcs in wanted.items()
        if name not in existing or not set(_as_sources(srcs)) <= existing[name]
    ]
    if to_register:
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(lambda args: _put_stream(go2rtc_url, *args), to_register))
    return len(to_register)


def keep_streams_once():
    """Restore any remembered streams that go2rtc has lost (e.g. after a restart)."""
    try:
        _seed_from_protect()
    except Exception:
        logger.exception("Couldn't list Protect cameras for the stream keeper")
    with _keeper_lock:
        snapshot = {url: dict(streams) for url, streams in _wanted.items()}
    for go2rtc_url, wanted in snapshot.items():
        n = _sync_streams(go2rtc_url, wanted, require_listing=True)
        if n:
            logger.warning("go2rtc at %s was missing %d stream(s); re-registered", go2rtc_url, n)


def start_stream_keeper():
    """Start the background thread that keeps go2rtc's streams registered (idempotent)."""
    global _keeper_started
    with _keeper_lock:
        if _keeper_started:
            return
        _keeper_started = True

    def loop():
        while True:
            time.sleep(KEEPER_INTERVAL)
            try:
                keep_streams_once()
            except Exception:
                logger.exception("go2rtc stream keeper failed")

    threading.Thread(target=loop, name="go2rtc-stream-keeper", daemon=True).start()


def _stream_sources(data):
    """Map each go2rtc stream name to the set of its source URLs.

    go2rtc returns ``{name: {"producers": [{"url": ...}], ...}}``.
    """
    sources = {}
    for name, info in (data or {}).items():
        producers = info.get('producers') if isinstance(info, dict) else info
        sources[name] = {
            p.get('url') for p in (producers or []) if isinstance(p, dict)
        }
    return sources


def _as_sources(srcs):
    """A wanted entry is one source URL or a tuple of them."""
    return (srcs,) if isinstance(srcs, str) else tuple(srcs)


def _put_stream(go2rtc_url, name, srcs):
    """Register a single stream (with one or more sources) with go2rtc."""
    try:
        query = '&'.join(
            'src=' + urllib.parse.quote(s, safe='') for s in _as_sources(srcs)
        )
        url = f"{go2rtc_url}/api/streams?name={name}&{query}"
        req = urllib.request.Request(url, method='PUT', data=b'')
        with urllib.request.urlopen(req, timeout=5):
            pass
    except urllib.error.HTTPError as e:
        # go2rtc creates the stream in memory, then fails to persist it
        # because the companion runs without a config file.
        body = e.read().decode('utf-8', 'replace')
        if e.code == 400 and 'config file disabled' in body:
            return
        logger.warning(
            "Failed to register stream %s with go2rtc: %s %s",
            name, e, body.strip(),
        )
    except (urllib.error.URLError, OSError) as e:
        logger.warning(
            "Failed to register stream %s with go2rtc: %s", name, e,
        )


# Sites with at least this many cameras get a large "hero" tile.
HERO_MIN_CAMERAS = 5


def _with_hero(streams):
    """Move the doorbell (else the first camera) to the front for the hero tile.

    Returns ``(streams, has_hero)``; small sites keep a plain grid.
    """
    if len(streams) < HERO_MIN_CAMERAS:
        return streams, False
    idx = next(
        (i for i, s in enumerate(streams)
         if s.get('is_doorbell') or 'doorbell' in s['display_name'].lower()),
        0,
    )
    return [streams[idx]] + streams[:idx] + streams[idx + 1:], True


POSTER_W = 640


def _inline_posters(streams):
    """Embed cached posters so they paint with the page.

    A separate request for each still lands a few hundred ms after the
    page, often after the stream's first frame is due, and the card is
    black until then. Uncached stills fall back to the snapshot URL.
    """
    for stream in streams:
        if stream.get('camera_id'):
            data = frames.live_peek((stream['camera_id'], POSTER_W))
            if data:
                stream['poster'] = 'data:image/jpeg;base64,' + base64.b64encode(data).decode()


def _cached_poster(camera_id):
    """The cached live still as a data URI, else None (and warm it for next time).

    Never fetches inline: a doorbell pop-up mustn't wait on Protect.
    """
    key = (camera_id, POSTER_W)
    data = frames.live_peek(key)
    if data:
        return 'data:image/jpeg;base64,' + base64.b64encode(data).decode()

    def warm():
        try:
            frames.live_get(key, _fetch_live)
        except Exception as err:
            logger.debug("Poster warm failed for %s: %s", camera_id, err)
    threading.Thread(target=warm, daemon=True).start()
    return None


def _requested_site(value, sites):
    """Index of the tab to open for ?site=, by name ("mercer_island") or index.

    Unknown sites (or ones with no cameras guests may see) open the first tab.
    """
    value = (value or '').strip().lower().replace(' ', '_').replace('-', '_')
    if value.isdigit():
        return int(value) if int(value) < len(sites) else 0
    for i, site in enumerate(sites):
        if site['name'].lower().replace(' ', '_').replace('-', '_') == value:
            return i
    return 0


def camera_feed_view(request):
    """Display camera feeds via go2rtc, with tabs for multiple sites.

    If UniFi Protect sites are configured, cameras are discovered dynamically
    from each Protect API and registered with go2rtc on the fly.

    Falls back to showing whatever streams are already in go2rtc if
    no Protect sites are configured.
    """
    go2rtc_url = config.go2rtc_url()
    protect_sites = config.protect_sites()
    admin = policy.is_admin(request)

    if protect_sites:
        # Multi-site discovery via Protect API
        site_data = get_protect_cameras()
        if not admin:
            site_data = policy.guest_sites(site_data)
        talk, ptz = policy.can_talk(request), policy.can_ptz(request)
        doors = _door_names() if admin else {}

        sites = []
        for site in site_data:
            all_cameras = site.get('cameras', [])
            _register_streams_with_go2rtc(go2rtc_url, all_cameras)
            streams = [
                {
                    'name': cam['stream_name'],
                    'display_name': cam['name'],
                    'camera_id': cam.get('camera_id', ''),
                    'is_ptz': ptz and cam.get('is_ptz', False),
                    'ptz_presets': cam.get('ptz_presets', 0) if ptz else 0,
                    'preset_range': list(range(cam.get('ptz_presets', 0) if ptz else 0)),
                    'has_low': bool(cam.get('rtsp_url_low', '')),
                    'is_doorbell': cam.get('is_doorbell', False),
                    'has_mic': cam.get('has_mic', False),
                    'has_speaker': cam.get('has_speaker', False),
                    'talk_stream': (talk_stream_name(cam['stream_name'])
                                    if talk and cam.get('talkback') else ''),
                    'hi_stream': listen_stream_name(cam),
                    'door_name': doors.get(cam.get('camera_id', ''), ''),
                }
                for cam in all_cameras
            ]
            streams, hero = _with_hero(streams)
            sites.append({
                'name': site['name'],
                'streams': streams,
                'hero': hero,
            })
        if not sites:
            # Nothing guests can see (or no cameras): one empty panel says so.
            streams, hero = _with_hero([])
            sites = [{'name': 'Cameras', 'streams': streams, 'hero': hero}]
    else:
        # Fallback: show streams already configured in go2rtc
        streams, hero = _with_hero(get_go2rtc_streams(go2rtc_url))
        sites = [{'name': 'Cameras', 'streams': streams, 'hero': hero}]

    active_site = _requested_site(request.GET.get('site'), sites)

    show_events = policy.can_see_events(request)
    window = policy.event_window(request)
    if show_events and sites:
        _inline_posters(sites[active_site]['streams'])

    return render(request, 'cameras/camera_feeds.html', {
        'sites': sites,
        'active_site': active_site,
        'watch_path': WATCH_PATH,
        'is_admin': admin,
        'show_events': show_events,
        # Earliest moment the events rail and scrubber may reach (ms), 0 for no limit.
        'events_from': int(window.timestamp() * 1000) if window else 0,
        'event_kinds': [(k, config.KIND_SHORT[k]) for k in config.enabled_event_kinds()],
        'kind_labels': json.dumps(config.KIND_SHORT),
        'keep_open': config.keep_open(),
        'video_mode': config.video_mode(),
    })


def _forbidden():
    return JsonResponse({'error': 'forbidden'}, status=403)


def _unavailable(err=None):
    if err is not None:
        logger.warning("Protect request failed: %s", err)
    return JsonResponse({'error': 'camera history unavailable'}, status=503)


def _parse_ts(value):
    """Unix seconds or ms (or ISO 8601) -> aware UTC datetime, else None."""
    if not value:
        return None
    try:
        num = float(value)
    except ValueError:
        try:
            dt = datetime.fromisoformat(value)
        except ValueError:
            return None
        return dt if dt.tzinfo else dt.replace(tzinfo=dt_timezone.utc)
    if num > 1e11:
        num /= 1000
    return datetime.fromtimestamp(num, tz=dt_timezone.utc)


@require_GET
def events_json(request):
    """Last 24 hours of Protect events, filtered to the enabled kinds.

    Guests only get the cameras they see, from the Guests page's window on.
    """
    if not policy.can_see_events(request):
        return _forbidden()
    enabled = config.enabled_event_kinds()
    since = timezone.now() - events.HISTORY
    found = ProtectEvent.objects.filter(start__gte=since)
    if not policy.is_admin(request):
        window = policy.event_window(request)
        if window is not None:
            found = found.filter(start__gte=window)
        found = found.filter(camera_id__in=policy.visible_camera_ids(sites=get_protect_cameras))
    out = []
    for ev in found.defer('thumbnail'):
        kinds = [k for k in ev.kinds if k in enabled]
        if not kinds:
            continue
        out.append({
            'id': ev.event_id,
            'site': ev.site,
            'camera_id': ev.camera_id,
            'camera_name': ev.camera_name,
            'kinds': kinds,
            'start': int(ev.start.timestamp() * 1000),
            'end': int(ev.end.timestamp() * 1000) if ev.end else None,
            'score': ev.score,
        })
    return JsonResponse({'events': out, 'kinds': enabled, 'live': events.available()})


@require_GET
def event_thumb(request, event_id):
    if not policy.can_see_events(request):
        return _forbidden()
    ev = ProtectEvent.objects.filter(event_id=event_id).first()
    if ev is None or not policy.can_see_moment(request, ev.camera_id, ev.start, get_protect_cameras):
        raise Http404
    data = bytes(ev.thumbnail) if ev.thumbnail else None
    if data is None:
        try:
            data = events.thumbnail(ev.event_id, ev.host, ev.camera_id)
        except events.WorkerUnavailable:
            return _unavailable()
        except Exception as err:
            return _unavailable(err)
        if not data:
            raise Http404
    resp = HttpResponse(data, content_type='image/jpeg')
    resp['Cache-Control'] = 'private, max-age=86400'
    return resp


def _fetch_live(key):
    camera_id, width = key
    return events.run(camera_id, lambda client: client.get_camera_snapshot(camera_id, width=width, dt=None))


@require_GET
def snapshot(request, camera_id):
    """A still from the recording at ``ts`` (or now).

    Guests only get these with events on (they're the rail's and scrubber's
    stills), for cameras they see and inside their window.
    """
    dt = _parse_ts(request.GET.get('ts'))
    if not policy.can_see_events(request) or not policy.can_view(request, camera_id, get_protect_cameras):
        return _forbidden()
    if not policy.can_see_moment(request, camera_id, dt or timezone.now(), get_protect_cameras):
        return _forbidden()
    try:
        width = min(max(int(request.GET.get('w', 960)), 160), 1920)
    except ValueError:
        width = 960
    key = None
    if dt is not None:
        dt = frames.round_ts(dt)
        key = (camera_id, int(dt.timestamp()), width)
        data = frames.get(key)
        if data is not None:
            resp = HttpResponse(data, content_type='image/jpeg')
            resp['Cache-Control'] = 'private, max-age=3600'
            return resp
    try:
        if dt is None:
            data = frames.live_get((camera_id, width), _fetch_live)
        else:
            # Warm widths share one full-size fetch (Protect's cost is ~the same at any width).
            fetch_w = max(frames.WARM_WIDTHS) if width in frames.WARM_WIDTHS else width
            data = events.run(
                camera_id,
                lambda client: client.get_camera_snapshot(camera_id, width=fetch_w, dt=dt),
            )
    except events.WorkerUnavailable:
        return _unavailable()
    except Exception as err:
        return _unavailable(err)
    if not data:
        raise Http404
    if key is not None:
        data = frames.render(camera_id, key[1], data, width)
    resp = HttpResponse(data, content_type='image/jpeg')
    resp['Cache-Control'] = 'private, max-age=3600' if dt else 'no-store'
    return resp


@require_GET
def clip(request, camera_id):
    """An MP4 export of a recording window (at most CLIP_MAX seconds)."""
    if not policy.can_see_events(request) or not policy.can_view(request, camera_id, get_protect_cameras):
        return _forbidden()
    start = _parse_ts(request.GET.get('start'))
    end = _parse_ts(request.GET.get('end'))
    if start is None:
        return JsonResponse({'error': 'start required'}, status=400)
    if not policy.can_see_moment(request, camera_id, start, get_protect_cameras):
        return _forbidden()
    if end is None or end <= start:
        end = start + CLIP_DEFAULT
    end = min(end, start + CLIP_MAX, timezone.now())
    if end <= start:
        return JsonResponse({'error': 'start is in the future'}, status=400)

    def export(path):
        events.run(
            camera_id,
            lambda client: client.get_camera_video(camera_id, start, end, output_file=path),
            timeout=90,
        )

    try:
        path = clips.get(camera_id, start, end, export, timeout=90)
    except events.WorkerUnavailable:
        return _unavailable()
    except Exception as err:
        return _unavailable(err)
    if path is None:
        raise Http404
    return clips.response(request, path, 'private, max-age=3600')


@xframe_options_sameorigin
@require_GET
def popup(request, camera_id):
    """Chrome-less live view for the hub's doorbell pop-up (live only, no history)."""
    if not policy.can_view(request, camera_id, get_protect_cameras):
        raise Http404
    camera = None
    for site in get_protect_cameras():
        for cam in site.get('cameras', []):
            if cam.get('camera_id') == camera_id:
                camera = cam
                break
        if camera:
            break
    if camera is None:
        raise Http404
    _register_streams_with_go2rtc(config.go2rtc_url(), [camera])
    return render(request, 'cameras/popup.html', {
        'camera_id': camera_id,
        'stream': listen_stream_name(camera),
        'name': camera['name'],
        'has_mic': camera.get('has_mic', False),
        'talk_stream': (talk_stream_name(camera['stream_name'])
                        if camera.get('talkback') and policy.can_talk(request) else ''),
        # Inline only: guests without events can't use the snapshot view.
        'poster': _cached_poster(camera_id),
        'watch_path': WATCH_PATH,
        'video_mode': config.video_mode(),
        'door_name': _door_names().get(camera_id, '') if policy.is_admin(request) else '',
    })


@require_POST
def talk_prepare(request):
    """Refresh a speaker camera's talkback target before the kiosk starts talking.

    Re-registers ``<stream>_talk`` if Protect moved it. Returns
    ``{"stream": ..., "port": ...}`` for ``BDHDisplay.talkStart``: the kiosk
    connects to ``/api/talk?src=<stream>`` on this page's port, and ``stream``
    is a single-use token for the camera (see :mod:`cameras.routing`).
    """
    camera_id = request.GET.get('camera', '')
    if camera_id and not (policy.can_view(request, camera_id, get_protect_cameras) and policy.can_talk(request)):
        camera_id = ''
    cam = refresh_talkback(camera_id) if camera_id else None
    if cam is None:
        return JsonResponse({'error': 'talkback unavailable'}, status=404)
    go2rtc_url = config.go2rtc_url()
    name = talk_stream_name(cam['stream_name'])
    source = talkback_source(cam['talkback'])
    with _keeper_lock:
        _wanted.setdefault(go2rtc_url, {})[name] = source
    _sync_streams(go2rtc_url, {name: source})
    from .routing import issue_talk
    return JsonResponse({'stream': issue_talk(name), 'port': _page_port(request)})


def _page_port(request):
    """The port the page was loaded from (the app's port on the hub)."""
    host = request.get_host()
    _, sep, port = host.rpartition(':')
    if sep and port.isdigit() and not host.endswith(']'):
        return int(port)
    return 443 if request.is_secure() else 80


# go2rtc's player. video-stream.js imports ./video-rtc.js, so both are served
# from one directory.
GO2RTC_SCRIPTS = ('video-stream.js', 'video-rtc.js')
_scripts = {}


@require_GET
def go2rtc_script(request, name):
    """go2rtc's player scripts, fetched once from go2rtc (browsers can't reach it)."""
    if name not in GO2RTC_SCRIPTS:
        raise Http404
    body = _scripts.get(name)
    if body is None:
        try:
            with urllib.request.urlopen(f'{config.go2rtc_url()}/{name}', timeout=5) as resp:
                body = resp.read()
        except (urllib.error.URLError, OSError) as err:
            logger.warning("Failed to fetch %s from go2rtc: %s", name, err)
            return HttpResponse(status=502)
        _scripts[name] = body
    resp = HttpResponse(body, content_type='application/javascript')
    resp['Cache-Control'] = 'public, max-age=3600'
    return resp


@require_POST
def ptz_goto(request):
    """AJAX endpoint to move a PTZ camera to a preset.

    Expects JSON body: {"camera_id": "...", "slot": 0}
    Returns JSON: {"success": true/false}
    """
    try:
        data = json.loads(request.body)
        camera_id = data.get('camera_id')
        slot = data.get('slot')

        if not camera_id or slot is None:
            return JsonResponse(
                {'success': False, 'error': 'Missing camera_id or slot'},
                status=400,
            )

        if not (policy.can_view(request, camera_id, get_protect_cameras) and policy.can_ptz(request)):
            return JsonResponse({'success': False, 'error': 'forbidden'}, status=403)
        success = ptz_goto_preset(camera_id, int(slot))
        return JsonResponse({'success': success})

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse(
            {'success': False, 'error': str(e)},
            status=400,
        )


def _door_for(camera_id):
    """``(site, door)`` for a camera mapped to a door, from the site whose map has it.

    Only sites with Access set up (host and token) count; never a fallback site.
    """
    for site in config.protect_sites():
        if not (site.get('access_host') and site.get('access_token')):
            continue
        door = (site.get('door_map') or {}).get(camera_id)
        if door:
            return site, door
    return None, None


def _door_names():
    """``{camera_id: door name}`` for cameras with a door to unlock (admins only)."""
    names = {}
    for site in config.protect_sites():
        if site.get('access_host') and site.get('access_token'):
            for cam, door in (site.get('door_map') or {}).items():
                names.setdefault(cam, door['name'])
    return names


def _hub_label():
    try:
        from bdh_sdk.location import location
        loc = location()
        if loc and getattr(loc, 'name', ''):
            return loc.name
    except Exception:
        pass
    try:
        from bdh_sdk.branding import brand
        return brand().get('name') or ''
    except Exception:
        return ''


DOOR_TOPIC = 'cameras/door/unlocked'
DOOR_REPEAT_SECONDS = 3


def _notify_unlock(text, payload):
    """Tell admins about an unlock (admin-only push topic); never fails the unlock."""
    try:
        from bdh_sdk import events as bus
        bus.publish(DOOR_TOPIC, {**payload, 'message': text})
    except Exception as err:
        logger.warning("Door unlock notification failed: %s", err)


@require_POST
def door_unlock(request):
    """Momentarily unlock the door mapped to a camera (admins only).

    JSON body ``{"camera_id": "..."}``; the door, console and token come from
    the NVRs settings, never the page. Returns ``{ok, door, error?, maybe_unlocked?}``.
    """
    if not policy.is_admin(request):
        return JsonResponse({'ok': False, 'error': 'forbidden'}, status=403)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'ok': False, 'error': 'bad json'}, status=400)
    camera_id = data.get('camera_id') if isinstance(data, dict) else None
    if not isinstance(camera_id, str) or not camera_id:
        return JsonResponse({'ok': False, 'error': 'missing camera_id'}, status=400)
    site, door = _door_for(camera_id)
    if door is None:
        return JsonResponse({'ok': False, 'error': 'no door'}, status=404)

    user = getattr(getattr(request, 'user', None), 'username', '') or 'admin'
    site_name = site.get('name') or site.get('host', '')
    label = _hub_label() or site_name
    actor_name = f"BlackDiamondHub {label} \u2013 {user}"
    where = f"{door['name']} ({site_name})"

    # One unlock per door per few seconds: a repeat is a duplicate while the
    # first is in flight or after it worked, but a failure doesn't count.
    key = f"cameras:door:{site['id']}:{door['id']}"
    if not cache.add(key, 'pending', DOOR_REPEAT_SECONDS):
        state = cache.get(key)
        if state in ('pending', 'ok'):
            logger.warning("Door unlock %s by %s via camera %s: duplicate (%s)", where, user, camera_id, state)
            return JsonResponse({'ok': True, 'door': door['name'], 'duplicate': True})
        cache.set(key, 'pending', DOOR_REPEAT_SECONDS)

    payload = {'door': door['name'], 'site': site_name, 'user': user, 'camera_id': camera_id}
    try:
        access_api.unlock(site['access_host'], site['access_token'], door['id'],
                          f"bdh:{user}", actor_name, site.get('access_pin', ''))
    except access_api.AccessError as err:
        cache.delete(key)
        logger.warning("Door unlock %s by %s via camera %s: %s", where, user, camera_id, err.kind)
        if err.kind == 'timeout':
            _notify_unlock(f"{where} may have unlocked (no reply) \u2013 {user}", {**payload, 'result': 'timeout'})
            return JsonResponse({'ok': False, 'door': door['name'], 'maybe_unlocked': True,
                                 'error': 'No reply \u2014 the door may have unlocked'}, status=504)
        errors = {
            'unauthorized': ('Access turned down the token', 502),
            'not_found': ('Door not found in Access', 502),
            'pin_mismatch': ("Access's certificate changed", 502),
            'unreachable': ("Couldn't reach Access", 504),
        }
        msg, status = errors.get(err.kind, ("Access didn't unlock the door", 502))
        return JsonResponse({'ok': False, 'door': door['name'], 'error': msg}, status=status)
    except Exception:
        cache.delete(key)
        logger.exception("Door unlock %s by %s via camera %s failed", where, user, camera_id)
        return JsonResponse({'ok': False, 'door': door['name'], 'error': "Access didn't unlock the door"}, status=502)
    cache.set(key, 'ok', DOOR_REPEAT_SECONDS)
    logger.warning("Door unlock %s by %s via camera %s: unlocked", where, user, camera_id)
    _notify_unlock(f"{where} unlocked by {user}", {**payload, 'result': 'unlocked'})
    return JsonResponse({'ok': True, 'door': door['name']})


RTC_DIAG_MAX_BYTES = 16 * 1024


def _log_diag(request, label):
    """Log a browser diagnostics JSON object as one line under ``label``."""
    if len(request.body) > RTC_DIAG_MAX_BYTES:
        return JsonResponse({'error': 'too large'}, status=413)
    try:
        data = json.loads(request.body)
    except (json.JSONDecodeError, UnicodeDecodeError):
        return JsonResponse({'error': 'bad json'}, status=400)
    if not isinstance(data, dict):
        return JsonResponse({'error': 'bad json'}, status=400)
    logger.warning(
        "%s from %s: %s",
        label,
        request.META.get('REMOTE_ADDR'),
        json.dumps(data, separators=(',', ':'), sort_keys=True),
    )
    return HttpResponse(status=204)


@require_POST
def rtc_diag(request):
    """Log a browser's WebRTC stats when a tile gives up and falls back to MSE.

    The fallback is otherwise silent server-side; these stats (frames
    received vs decoded, decoder, ICE state) show *why* WebRTC didn't play.
    """
    return _log_diag(request, "WebRTC fallback")


@require_POST
def rtc_events(request):
    """Log a browser's batched stream events (frame gaps, resolution changes).

    Sent every couple of seconds, so the last events before a display crash
    reach the log even though the page dies.
    """
    return _log_diag(request, "Stream events")


# ---- Pop-ups settings page ---------------------------------------------------

POPUP_STATES_TTL = 60
POPUP_REGISTRY_TTL = 600
POPUP_ENTITIES_KEY = "cameras:popup_entities3"
# Domains whose entities are worth suggesting before the admin searches.
_SUGGEST_DOMAINS = ("event", "lock")
_SUGGEST_CLASSES = {"door", "garage_door", "opening", "window", "motion", "occupancy", "presence"}
# Domains whose state changes make sense as a pop-up trigger; a device's other
# entities (sensors, numbers, updates...) sit behind "Show more" in the picker.
_TRIGGER_DOMAINS = {
    "alarm_control_panel", "binary_sensor", "button", "cover", "device_tracker", "event", "fan",
    "input_boolean", "input_button", "input_select", "light", "lock", "media_player", "person",
    "select", "siren", "switch", "valve",
}


def _settings_admin_only(view):
    """Settings pages don't exist for anyone but admins."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not policy.is_admin(request):
            raise Http404()
        return view(request, *args, **kwargs)
    return wrapped


def _camera_rows(cams):
    return [
        {"id": c["camera_id"], "name": c.get("name") or c["camera_id"],
         "doorbell": bool(c.get("is_doorbell")),
         "enabled": list(c["detections"].get("enabled", [])),
         "supported": list(c["detections"].get("supported", []))}
        for c in cams if c.get("camera_id") and isinstance(c.get("detections"), dict)
    ]


def _detection_sites():
    """Each NVR's cameras and the detections they have on in Protect.

    ``[{id, name, ok, stale, cameras: [{id, name, doorbell, enabled, supported}]}]``.
    When an NVR can't be reached, its cameras are the last ones seen (``stale``).
    """
    known = config.known_detections()
    out = []
    for site in get_protect_cameras():
        live = site.get("cameras") or []
        rows = _camera_rows(live)
        stale = not site.get("ok", True) or len(rows) < len([c for c in live if c.get("camera_id")])
        if stale:
            rows = known.get(site.get("id")) or [
                {"id": c["camera_id"], "name": c.get("name") or c["camera_id"],
                 "doorbell": bool(c.get("is_doorbell")),
                 "enabled": (["doorbell"] if c.get("is_doorbell") else []) + ["motion"],
                 "supported": []}
                for c in live if c.get("camera_id")
            ]
        out.append({"id": site.get("id"), "name": site.get("name") or "",
                    "ok": site.get("ok", True), "stale": stale, "cameras": rows})
    return out


@_settings_admin_only
def popup_settings(request):
    if request.method == "POST":
        try:
            raw = json.loads(request.body or b"{}")
        except ValueError:
            return JsonResponse({"error": "bad JSON"}, status=400)
        return JsonResponse(popups.save(raw))
    sites = []
    try:
        guests = policy.current()
        for i, site in enumerate(_detection_sites()):
            if site["cameras"]:
                cams = [{**c, "guest_hidden": not policy.camera_visible(guests, site, i, c["id"])}
                        for c in site["cameras"]]
                sites.append({"name": site["name"], "stale": site["stale"], "cameras": cams})
    except Exception:
        logger.exception("Could not list cameras for the pop-ups page")
    try:
        cfg = popups.current()
    except Exception:
        logger.exception("Could not read the pop-up settings")
        cfg = popups.clean({})
    cfg.setdefault("seconds", config.popup_seconds())
    return render(request, "cameras/settings_popups.html", {
        "active": "cameras",
        "page": {
            "sites": sites, "config": cfg, "kinds": list(popups.POPUP_KINDS),
            "labels": {k: config.KIND_LABELS[k] for k in popups.POPUP_KINDS},
            "seconds_min": config.POPUP_SECONDS_MIN, "seconds_max": config.POPUP_SECONDS_MAX,
            "chimes": list(popups.CHIMES), "silent": popups.SILENT,
            "kind_sounds": {k: popups.kind_sound(k) for k in popups.POPUP_KINDS},
            "trigger_sound": popups.TRIGGER_SOUND,
            "save_url": request.path,
            "entities_url": reverse("cameras_popup_entities"),
        },
    })


@_settings_admin_only
def main_settings(request):
    if request.method == "POST":
        raw = _json_body(request)
        if raw is None:
            return JsonResponse({"error": "bad JSON"}, status=400)
        changes = {}
        if isinstance(raw.get("events"), list):
            # Only the kinds on the page change; picks for kinds no camera has now are kept.
            shown = raw.get("shown") if isinstance(raw.get("shown"), list) else config.EVENT_KINDS
            current = config.enabled_event_kinds()
            changes["events"] = [
                k for k in config.EVENT_KINDS
                if k in raw["events"] or (k in current and k not in shown)
            ]
        if isinstance(raw.get("keep_open"), bool):
            changes["keep_open"] = raw["keep_open"]
        if raw.get("video_transport") in ("tcp", "udp"):
            changes["video_transport"] = raw["video_transport"]
        if not changes:
            return JsonResponse({"error": "nothing to save"}, status=400)
        config.save_settings(**changes)
        return JsonResponse({"ok": True, **changes})

    try:
        nvrs, error = _nvr_rows(), ""
    except Exception:
        logger.exception("Could not read the NVRs")
        nvrs, error = [], "Couldn't read the NVRs. Try again in a minute."
    sites = []
    if nvrs:
        try:
            sites = _detection_sites()
        except Exception:
            logger.exception("Could not list cameras for the settings page")
    by_id = {s["id"]: s for s in sites}
    for n in nvrs:
        site = by_id.get(n["id"])
        n["cameras"] = len(site["cameras"]) if site else 0
        n["ok"] = bool(site and site["ok"])
    counts = {}
    for site in sites:
        for cam in site["cameras"]:
            for k in cam["enabled"]:
                counts[k] = counts.get(k, 0) + 1
    kinds = [
        {"id": k, "label": label, "group": group, "cameras": counts[k]}
        for k, (label, group, _) in config.KINDS.items() if counts.get(k)
    ]
    try:
        pcfg = popups.current()
    except Exception:
        logger.exception("Could not read the pop-up settings")
        pcfg = popups.clean({})
    gcfg = policy.current()
    cams = [(i, site, c["id"]) for i, site in enumerate(sites) for c in site["cameras"]]
    guests = {
        "visible": sum(policy.camera_visible(gcfg, site, i, cid) for i, site, cid in cams),
        "cameras": len(cams), "talk": gcfg["talk"], "ptz": gcfg["ptz"], "events": gcfg["events"],
        "since": _local_since(gcfg["events_since"]).replace("T", " "),
    }
    return render(request, "cameras/settings_main.html", {
        "active": "cameras",
        "page": {
            "nvrs": nvrs, "error": error,
            "unreachable": [s["name"] for s in sites if not s["ok"]],
            "kinds": kinds, "events": config.enabled_event_kinds(),
            "popups": {"enabled": pcfg["enabled"], "cameras": len(pcfg["cameras"]),
                       "seconds": config.popup_seconds()},
            "keep_open": config.keep_open(), "video_transport": config.video_transport(),
            "guests": guests,
            "save_url": request.path,
            "nvrs_url": reverse("cameras_nvrs"), "popups_url": reverse("cameras_popups"),
            "guests_url": reverse("cameras_guests"),
        },
    })


def _local_since(value):
    """A stored ``events_since`` as the hub's local time for a datetime-local input."""
    if not value:
        return ""
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return ""
    return timezone.localtime(dt).strftime("%Y-%m-%dT%H:%M")


def _guest_page_config(cfg):
    return {**cfg, "events_since": _local_since(cfg["events_since"])}


@_settings_admin_only
def guests_settings(request):
    """What guests (anyone not signed in as an admin) see: cameras, talk, PTZ and events."""
    if request.method == "POST":
        raw = _json_body(request)
        if raw is None:
            return JsonResponse({"error": "bad JSON"}, status=400)
        return JsonResponse(_guest_page_config(policy.save(raw)))
    cfg = policy.current()
    sites, error = [], ""
    try:
        for i, site in enumerate(_detection_sites()):
            entry = cfg["nvrs"].get(str(site["id"])) if site["id"] is not None else None
            default = entry["default"] if entry else policy.HIDDEN
            if cfg.get(policy.FIRST_ONLY) and i == 0:
                default = policy.VISIBLE
            sites.append({
                "id": site["id"], "name": site["name"], "stale": site["stale"], "default": default,
                "cameras": [{"id": c["id"], "name": c["name"],
                             "visible": policy.camera_visible(cfg, site, i, c["id"])}
                            for c in site["cameras"]],
            })
    except Exception:
        logger.exception("Could not list cameras for the guests page")
        error = "Couldn't list the cameras. Try again in a minute."
    return render(request, "cameras/settings_guests.html", {
        "active": "cameras",
        "page": {"sites": sites, "config": _guest_page_config(cfg), "error": error,
                 "save_url": request.path},
    })


_HOST_RE = re.compile(r"^(?:[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?\.?)+(?::\d{1,5})?$|^\[[0-9A-Fa-f:.]+\](?::\d{1,5})?$")
NVR_NAME_MAX = 40
NVR_FIELD_MAX = 256


def _clean_host(raw):
    """A host name or IP (optionally with a port); a pasted URL is cut down to its host."""
    host = str(raw or "").strip()
    host = re.sub(r"^[a-z][a-z0-9+.-]*://", "", host, flags=re.I).split("/", 1)[0]
    if not host or len(host) > 253 or not _HOST_RE.match(host):
        return ""
    return host


def _text(raw, limit=NVR_FIELD_MAX):
    return str(raw or "").strip()[:limit]


def _nvr_rows():
    """The NVRs for the page: no secrets, only whether each one is set."""
    sites = {s["id"]: s for s in config.protect_sites()}
    rows = []
    for n in config.nvrs():
        site = sites.get(n["id"])
        rows.append({
            "id": n["id"], "name": n.get("name") or n["host"], "host": n["host"],
            "username": n.get("username", ""),
            "has_api_key": site is not None,
            "has_password": bool(site and site.get("password")),
            "access_host": n.get("access_host", ""),
            "has_access_token": bool(site and site.get("access_token")),
            "door_map": config.clean_door_map(n.get("door_map")) if n.get("access_host") else {},
        })
    return rows


def _nvr_cameras():
    """``{nvr_id: [{id, name}]}``: each NVR's cameras for the door mapping table."""
    try:
        return {s["id"]: [{"id": c["id"], "name": c["name"]} for c in s["cameras"]]
                for s in _detection_sites() if s.get("id")}
    except Exception:
        logger.exception("Could not list the cameras for door mapping")
        return {k: [{"id": c["id"], "name": c["name"]} for c in v if isinstance(c, dict) and c.get("id")]
                for k, v in config.known_detections().items() if isinstance(v, list)}


def _nvrs_changed():
    from . import protect_api

    config.forget_sites()
    protect_api.clear_cache()
    try:
        events.restart()
    except Exception:
        logger.exception("Could not restart the Protect events workers")


def _json_body(request):
    try:
        raw = json.loads(request.body or b"{}")
    except ValueError:
        return None
    return raw if isinstance(raw, dict) else None


@_settings_admin_only
def nvr_settings(request):
    try:
        rows = _nvr_rows()
        error = ""
    except Exception:
        logger.exception("Could not read the NVRs")
        rows, error = [], "Couldn't read the NVRs. Try again in a minute."
    return render(request, "cameras/settings_nvrs.html", {
        "active": "cameras",
        "page": {
            "nvrs": rows, "error": error,
            "cameras": _nvr_cameras() if any(r.get("access_host") for r in rows) else {},
            "save_url": reverse("cameras_nvr_save"),
            "remove_url": reverse("cameras_nvr_remove"),
            "test_url": reverse("cameras_nvr_test"),
            "access_test_url": reverse("cameras_nvr_access_test"),
        },
    })


@_settings_admin_only
@require_POST
def nvr_save(request):
    """Add (no ``id``) or edit an NVR. A blank API key or password keeps the saved one."""
    from bdh_sdk import secrets

    raw = _json_body(request)
    if raw is None:
        return JsonResponse({"error": "bad JSON"}, status=400)
    name = _text(raw.get("name"), NVR_NAME_MAX)
    host = _clean_host(raw.get("host"))
    username = _text(raw.get("username"))
    api_key = _text(raw.get("api_key"))
    password = str(raw.get("password") or "")[:NVR_FIELD_MAX]
    if not name:
        return JsonResponse({"error": "Give it a name.", "field": "name"}, status=400)
    if not host:
        return JsonResponse({"error": "Enter the console's IP address or host name.", "field": "host"}, status=400)

    items = config.nvrs()
    nvr_id = str(raw.get("id") or "")
    if nvr_id:
        existing = next((n for n in items if n["id"] == nvr_id), None)
        if existing is None:
            return JsonResponse({"error": "That NVR was removed."}, status=404)
    else:
        existing = None
        used = {int(n["id"]) for n in items if str(n["id"]).isdigit()}
        nvr_id = str(next(i for i in range(1, len(used) + 2) if i not in used))
    if any(n["host"].lower() == host.lower() and n["id"] != nvr_id for n in items):
        return JsonResponse({"error": "That console is already added.", "field": "host"}, status=400)
    if any((n.get("name") or "").lower() == name.lower() and n["id"] != nvr_id for n in items):
        return JsonResponse({"error": "Another NVR has that name.", "field": "name"}, status=400)

    has_key = bool(secrets.get(config.api_key_secret(nvr_id), "", app="cameras")) if existing else False
    if not api_key and not has_key:
        return JsonResponse({"error": "Enter the API key.", "field": "api_key"}, status=400)
    has_password = bool(secrets.get(config.password_secret(nvr_id), "", app="cameras")) if existing else False
    if username and not password and not (has_password and existing.get("username") == username):
        return JsonResponse({"error": "Enter the password for that account.", "field": "password"}, status=400)

    access, access_error = _access_settings(raw, existing, nvr_id)
    if access_error:
        return access_error

    if api_key:
        secrets.set(config.api_key_secret(nvr_id), api_key, app="cameras")
    if access.pop("token", None):
        secrets.set(config.access_token_secret(nvr_id), access["new_token"], app="cameras")
    elif access.pop("drop_token", False):
        try:
            secrets.delete(config.access_token_secret(nvr_id), app="cameras")
        except Exception:
            logger.exception("Could not delete the Access token")
    access.pop("new_token", None)
    if username and password:
        secrets.set(config.password_secret(nvr_id), password, app="cameras")
    elif not username and has_password:
        secrets.delete(config.password_secret(nvr_id), app="cameras")
    entry = {"id": nvr_id, "name": name, "host": host, "username": username, **access}
    if existing:
        items = [entry if n["id"] == nvr_id else n for n in items]
    else:
        items.append(entry)
    from . import protect_api

    protect_api.clear_cache()  # the old host's cache, before it leaves the list
    config.save_nvrs(items)
    _nvrs_changed()
    return JsonResponse({"ok": True, "nvrs": _nvr_rows()})


@_settings_admin_only
@require_POST
def nvr_remove(request):
    from bdh_sdk import secrets
    from . import protect_api

    raw = _json_body(request)
    if raw is None:
        return JsonResponse({"error": "bad JSON"}, status=400)
    nvr_id = str(raw.get("id") or "")
    items = config.nvrs()
    if not any(n["id"] == nvr_id for n in items):
        return JsonResponse({"error": "That NVR was already removed."}, status=404)
    protect_api.clear_cache()
    config.save_nvrs([n for n in items if n["id"] != nvr_id])
    for name in (config.api_key_secret(nvr_id), config.password_secret(nvr_id),
                 config.access_token_secret(nvr_id)):
        try:
            secrets.delete(name, app="cameras")
        except Exception:
            logger.exception("Could not delete secret %s", name)
    _nvrs_changed()
    return JsonResponse({"ok": True, "nvrs": _nvr_rows()})


def _access_pin_key(host):
    return f"cameras:access_pin:{host.lower()}"


def _access_settings(raw, existing, nvr_id):
    """The door unlock part of an NVR save: ``(entry fields, error response)``.

    Door unlock is optional. ``access_remove`` turns it off (token, console,
    certificate and door map all go). Otherwise a token needs the Access
    console's host (never the NVR's), a blank token keeps the saved one, and
    the console's certificate is pinned: from the last test of that host,
    the saved pin if the host is unchanged, or fetched now.
    """
    from bdh_sdk import secrets

    existing = existing or {}
    old_host = existing.get("access_host", "")
    has_token = bool(old_host and secrets.get(config.access_token_secret(nvr_id), "", app="cameras")) \
        if existing else False
    off = {"drop_token": bool(old_host)}
    if raw.get("access_remove"):
        return off, None
    host_raw = _text(raw.get("access_host"))
    host = _clean_host(host_raw)
    token = _text(raw.get("access_token"), 1024)
    if host_raw and not host:
        return None, JsonResponse({"error": "Enter the Access console's IP address or host name.",
                                   "field": "access_host"}, status=400)
    if not host:
        if token or has_token:
            return None, JsonResponse({"error": "Enter the Access console's IP address, or remove door unlock.",
                                       "field": "access_host"}, status=400)
        return off, None
    if not token and not has_token:
        return None, JsonResponse({"error": "Enter the Access API token.", "field": "access_token"}, status=400)

    pin = access_api.clean_pin(cache.get(_access_pin_key(host)))
    if not pin and host.lower() == old_host.lower():
        pin = access_api.clean_pin(existing.get("access_pin"))
    if not pin:
        try:
            pin = access_api.fetch_pin(host)
        except access_api.AccessError:
            return None, JsonResponse({"error": f"Couldn't reach {host}.", "field": "access_host"}, status=400)
    door_map = raw.get("door_map")
    door_map = config.clean_door_map(door_map if isinstance(door_map, dict) else existing.get("door_map"))
    out = {"access_host": host, "access_pin": pin, "door_map": door_map}
    if token:
        out.update(token=True, new_token=token)
    return out, None


@_settings_admin_only
@require_POST
def nvr_access_test(request):
    """Check an Access console and token: lists its doors and pins its certificate.

    ``{ok, doors: [{id, name}], cameras: [{id, name}]}`` or ``{ok: false, error, field}``.
    A blank token for a saved NVR (``id``) uses the saved one.
    """
    from bdh_sdk import secrets

    raw = _json_body(request)
    if raw is None:
        return JsonResponse({"error": "bad JSON"}, status=400)
    host = _clean_host(raw.get("access_host"))
    if not host:
        return JsonResponse({"ok": False, "error": "Enter the Access console's IP address or host name.",
                             "field": "access_host"})
    nvr_id = str(raw.get("id") or "")
    saved = next((n for n in config.nvrs() if n["id"] == nvr_id), None) if nvr_id else None
    token = _text(raw.get("access_token"), 1024)
    if not token and saved and saved.get("access_host"):
        token = secrets.get(config.access_token_secret(nvr_id), "", app="cameras")
    if not token:
        return JsonResponse({"ok": False, "error": "Enter the Access API token.", "field": "access_token"})
    try:
        pin = access_api.fetch_pin(host)
        doors = access_api.list_doors(host, token, pin)
    except access_api.AccessError as err:
        if err.kind == "unauthorized":
            return JsonResponse({"ok": False, "error": "Access turned down that token.", "field": "access_token"})
        if err.kind in ("unreachable", "timeout", "pin_mismatch"):
            return JsonResponse({"ok": False, "error": f"Couldn't reach {host}.", "field": "access_host"})
        return JsonResponse({"ok": False, "error": "That doesn't look like a UniFi Access console.",
                             "field": "access_host"})
    cache.set(_access_pin_key(host), pin, 600)
    return JsonResponse({"ok": True, "doors": doors, "cameras": _nvr_cameras().get(nvr_id, []) if nvr_id else []})


def _test_login(host, username, password):
    """True/False for the local account, or None if the console couldn't be reached."""
    import requests

    try:
        resp = requests.post(
            f"https://{host}/api/auth/login",
            json={"username": username, "password": password, "rememberMe": False},
            verify=False, timeout=10,
        )
    except requests.RequestException:
        return None
    if resp.status_code in (400, 401, 403):
        return False
    return resp.ok or None


@_settings_admin_only
@require_POST
def nvr_test(request):
    """Check a console before saving: the API key lists cameras, the account logs in.

    Blank secrets for a saved NVR (``id``) use the saved ones.
    """
    import requests
    from bdh_sdk import secrets
    from . import protect_api

    raw = _json_body(request)
    if raw is None:
        return JsonResponse({"error": "bad JSON"}, status=400)
    host = _clean_host(raw.get("host"))
    if not host:
        return JsonResponse({"ok": False, "error": "Enter the console's IP address or host name.", "field": "host"})
    nvr_id = str(raw.get("id") or "")
    saved = next((n for n in config.nvrs() if n["id"] == nvr_id), None) if nvr_id else None
    api_key = _text(raw.get("api_key"))
    if not api_key and saved:
        api_key = secrets.get(config.api_key_secret(nvr_id), "", app="cameras")
    if not api_key:
        return JsonResponse({"ok": False, "error": "Enter the API key.", "field": "api_key"})
    try:
        resp = protect_api._request(
            "get", f"https://{host}{protect_api.API_BASE}/cameras",
            headers={"X-API-KEY": api_key}, verify=False, timeout=10,
        )
    except requests.RequestException:
        return JsonResponse({"ok": False, "error": f"Couldn't reach {host}.", "field": "host"})
    if resp.status_code in (401, 403):
        return JsonResponse({"ok": False, "error": "The console turned down that API key.", "field": "api_key"})
    if not resp.ok:
        return JsonResponse({"ok": False, "error": f"The console answered {resp.status_code}. Is it a UniFi Protect console?", "field": "host"})
    try:
        data = resp.json()
    except ValueError:
        data = None
    if not isinstance(data, list):
        return JsonResponse({"ok": False, "error": "That doesn't look like a UniFi Protect console.", "field": "host"})

    result = {"ok": True, "cameras": len(data)}
    username = _text(raw.get("username"))
    if username:
        password = str(raw.get("password") or "")
        if not password and saved and saved.get("username") == username:
            password = secrets.get(config.password_secret(nvr_id), "", app="cameras")
        if not password:
            result.update(ok=False, error="Enter the password for that account.", field="password")
        else:
            login = _test_login(host, username, password)
            if login is False:
                result.update(ok=False, error="The console turned down that username or password.", field="password")
            elif login is None:
                result.update(ok=False, error="Couldn't sign in to the console with that account.", field="password")
    return JsonResponse(result)


def _ha_registry(instance=None):
    """An HA instance's entity/device/area registries, cached: ``{"entities": {entity_id:
    {area, device, category}}, "devices": {device_id: {name, maker, area}}}``."""
    from django.core.cache import cache

    key = f"cameras:popup_registry:{instance or 'ha'}"
    reg = cache.get(key)
    if reg is None:
        from bdh_sdk import ha

        kw = {"instance": instance} if instance else {}
        # ha.registry needs only ha.read (it's trimmed to readable entities);
        # older hubs send no "areas".
        got = ha.get_registry(**kw) or {}
        entities, devices = got.get("entities"), got.get("devices")
        names = {a["area_id"]: a.get("name") or ""
                 for a in got.get("areas") or [] if a.get("area_id")}
        devs = {}
        for d in devices or []:
            if d.get("id") and not d.get("disabled_by"):
                devs[d["id"]] = {
                    "name": d.get("name_by_user") or d.get("name") or "",
                    "maker": d.get("manufacturer") or "",
                    "area": names.get(d.get("area_id"), ""),
                }
        ents = {}
        for e in entities or []:
            device_id = e.get("device_id") if e.get("device_id") in devs else ""
            area = names.get(e.get("area_id")) or (devs[device_id]["area"] if device_id else "")
            category = e.get("entity_category") or ("hidden" if e.get("hidden_by") else "")
            ents[e["entity_id"]] = {"area": area, "device": device_id, "category": category}
        reg = {"entities": ents, "devices": devs}
        cache.set(key, reg, POPUP_REGISTRY_TTL)
    return reg


def _short_name(name, device_name):
    """``name`` without its device's name in front ("Front Door Motion" -> "Motion")."""
    if device_name and name.lower().startswith(device_name.lower()):
        rest = name[len(device_name):].lstrip(" -:�")
        if rest:
            return rest
    return name


def _suggested(domain, device_class):
    return domain in _SUGGEST_DOMAINS or (
        domain == "binary_sensor" and device_class in _SUGGEST_CLASSES)


@_settings_admin_only
@require_GET
def popup_entities(request):
    """Every Home Assistant instance's entities and devices, for the trigger picker.

    ``instances``: ``[{id, name, site, default}]`` (ones that answered);
    ``entities``: ``[{id, instance, name, short, area, state, domain, device_class, device,
    category, useful, suggest}]``; ``devices``: ``[{id, instance, name, maker, area}]`` (only
    ones with entities).
    """
    from django.core.cache import cache

    data = cache.get(POPUP_ENTITIES_KEY)
    if data is None:
        from bdh_sdk import ha

        try:
            instances = ha.instances() or []
        except (ha.HAError, BrokerError) as err:
            logger.warning("Pop-ups: HA instances unavailable: %s", err)
            instances = []
        if not instances:
            instances = [{"id": "ha", "name": "Home Assistant", "site": "", "default": True}]
        data = {"instances": [], "entities": [], "devices": []}
        for inst in instances:
            part = _instance_entities(inst["id"], inst.get("default"))
            if part is None:
                continue
            data["instances"].append(
                {k: inst.get(k) for k in ("id", "name", "site", "default")})
            data["entities"] += part["entities"]
            data["devices"] += part["devices"]
        if not data["instances"]:
            return JsonResponse({"error": "Home Assistant unavailable"}, status=503)
        cache.set(POPUP_ENTITIES_KEY, data, POPUP_STATES_TTL)
    return JsonResponse(data)


def _instance_entities(instance, default):
    """``{entities, devices}`` for one HA instance, or None if it's unavailable."""
    from bdh_sdk import ha

    # The default instance is asked without ``instance`` so older hubs still answer.
    kw = {} if default else {"instance": instance}
    try:
        states = ha.get_all_states(**kw)
    except (ha.HAError, BrokerError) as err:
        logger.warning("Pop-ups: HA %s states unavailable: %s", instance, err)
        return None
    try:
        reg = _ha_registry(kw.get("instance"))
    except (ha.HAError, BrokerError) as err:
        # States only if the registry is unavailable.
        logger.warning("Pop-ups: HA %s registries unavailable: %s", instance, err)
        reg = {"entities": {}, "devices": {}}
    devices = reg["devices"]
    entities, used = [], set()
    for st in states or []:
        entity_id = st.get("entity_id") or ""
        if "." not in entity_id:
            continue
        attrs = st.get("attributes") or {}
        domain = entity_id.split(".", 1)[0]
        device_class = attrs.get("device_class") or ""
        info = reg["entities"].get(entity_id) or {}
        device_id = info.get("device") or ""
        name = attrs.get("friendly_name") or entity_id
        category = info.get("category") or ""
        used.add(device_id)
        entities.append({
            "id": entity_id,
            "instance": instance,
            "name": name,
            "short": _short_name(name, devices[device_id]["name"] if device_id else ""),
            "area": info.get("area") or "",
            "state": st.get("state") or "",
            "domain": domain,
            "device_class": device_class,
            "device": device_id,
            "category": category,
            "useful": domain in _TRIGGER_DOMAINS and not category,
            "suggest": _suggested(domain, device_class),
        })
    entities.sort(key=lambda e: (e["name"].lower(), e["id"]))
    return {
        "entities": entities,
        "devices": sorted(
            ({"id": d, "instance": instance, **devices[d]} for d in used if d),
            key=lambda d: (d["name"].lower(), d["id"])),
    }
