import json
import logging
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone as dt_timezone
from functools import wraps

from bdh_sdk import is_admin
from django.http import Http404, HttpResponse, JsonResponse
from django.shortcuts import render
from django.urls import reverse
from django.utils import timezone
from django.views.decorators.clickjacking import xframe_options_sameorigin
from django.views.decorators.http import require_GET, require_POST

from . import config, events, frames, popups
from .models import ProtectEvent
from .protect_api import get_protect_cameras, ptz_goto_preset

logger = logging.getLogger(__name__)

CLIP_DEFAULT = timedelta(seconds=30)
CLIP_MAX = timedelta(seconds=120)


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
    """
    # Fetch existing streams once
    try:
        req = urllib.request.Request(f'{go2rtc_url}/api/streams')
        with urllib.request.urlopen(req, timeout=5) as response:
            existing = _stream_sources(json.loads(response.read()))
    except (urllib.error.URLError, json.JSONDecodeError, OSError):
        existing = {}

    # Collect streams that need registration
    to_register = []
    for camera in cameras:
        candidates = [(camera['stream_name'], camera['rtsp_url'])]
        low_url = camera.get('rtsp_url_low', '')
        if low_url:
            candidates.append((f"{camera['stream_name']}_low", low_url))
        for name, url in candidates:
            if name not in existing or url not in existing[name]:
                to_register.append((name, url))

    # Register all new streams in parallel
    if to_register:
        with ThreadPoolExecutor(max_workers=8) as pool:
            pool.map(
                lambda args: _put_stream(go2rtc_url, *args),
                to_register,
            )


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


def _put_stream(go2rtc_url, name, rtsp_url):
    """Register a single stream with go2rtc."""
    try:
        src = urllib.parse.quote(rtsp_url, safe='')
        url = f"{go2rtc_url}/api/streams?name={name}&src={src}"
        req = urllib.request.Request(url, method='PUT', data=b'')
        with urllib.request.urlopen(req, timeout=5):
            pass
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


def camera_feed_view(request):
    """Display camera feeds via go2rtc, with tabs for multiple sites.

    If UniFi Protect sites are configured, cameras are discovered dynamically
    from each Protect API and registered with go2rtc on the fly.

    Falls back to showing whatever streams are already in go2rtc if
    no Protect sites are configured.
    """
    go2rtc_url = config.go2rtc_url()
    protect_sites = config.protect_sites()

    if protect_sites:
        # Multi-site discovery via Protect API
        site_data = get_protect_cameras()

        # Only show the first (primary) site to unauthenticated users
        if not request.user.is_authenticated:
            site_data = site_data[:1]

        sites = []
        for site in site_data:
            all_cameras = site.get('cameras', [])
            _register_streams_with_go2rtc(go2rtc_url, all_cameras)
            streams = [
                {
                    'name': cam['stream_name'],
                    'display_name': cam['name'],
                    'camera_id': cam.get('camera_id', ''),
                    'is_ptz': cam.get('is_ptz', False),
                    'ptz_presets': cam.get('ptz_presets', 0),
                    'preset_range': list(range(cam.get('ptz_presets', 0))),
                    'has_low': bool(cam.get('rtsp_url_low', '')),
                    'is_doorbell': cam.get('is_doorbell', False),
                }
                for cam in all_cameras
            ]
            streams, hero = _with_hero(streams)
            sites.append({
                'name': site['name'],
                'streams': streams,
                'hero': hero,
            })
    else:
        # Fallback: show streams already configured in go2rtc
        streams, hero = _with_hero(get_go2rtc_streams(go2rtc_url))
        sites = [{'name': 'Cameras', 'streams': streams, 'hero': hero}]

    # Extract port from GO2RTC_URL for browser-side URL construction
    from urllib.parse import urlparse
    go2rtc_port = urlparse(go2rtc_url).port or 1984

    return render(request, 'cameras/camera_feeds.html', {
        'sites': sites,
        'go2rtc_url': go2rtc_url,
        'go2rtc_port': go2rtc_port,
        'is_admin': is_admin(request),
        'event_kinds': config.enabled_event_kinds(),
        'keep_open': config.keep_open(),
    })


def admin_only(view):
    """Recordings and history are for signed-in staff only; guests get live view."""
    @wraps(view)
    def wrapped(request, *args, **kwargs):
        if not is_admin(request):
            return JsonResponse({'error': 'forbidden'}, status=403)
        return view(request, *args, **kwargs)
    return wrapped


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


@admin_only
@require_GET
def events_json(request):
    """Last 24 hours of Protect events, filtered to the enabled kinds."""
    enabled = config.enabled_event_kinds()
    since = timezone.now() - events.HISTORY
    out = []
    for ev in ProtectEvent.objects.filter(start__gte=since).defer('thumbnail'):
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


@admin_only
@require_GET
def event_thumb(request, event_id):
    ev = ProtectEvent.objects.filter(event_id=event_id).first()
    if ev is None:
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


@admin_only
@require_GET
def snapshot(request, camera_id):
    """A still from the recording at ``ts`` (or now)."""
    dt = _parse_ts(request.GET.get('ts'))
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
        data = events.run(
            camera_id,
            lambda client: client.get_camera_snapshot(camera_id, width=width, dt=dt),
        )
    except events.WorkerUnavailable:
        return _unavailable()
    except Exception as err:
        return _unavailable(err)
    if not data:
        raise Http404
    if key is not None:
        data = frames.resize(data, width)
        frames.put(key, data)
    resp = HttpResponse(data, content_type='image/jpeg')
    resp['Cache-Control'] = 'private, max-age=3600' if dt else 'no-store'
    return resp


@admin_only
@require_GET
def clip(request, camera_id):
    """An MP4 export of a recording window (at most CLIP_MAX seconds)."""
    start = _parse_ts(request.GET.get('start'))
    end = _parse_ts(request.GET.get('end'))
    if start is None:
        return JsonResponse({'error': 'start required'}, status=400)
    if end is None or end <= start:
        end = start + CLIP_DEFAULT
    end = min(end, start + CLIP_MAX, timezone.now())
    if end <= start:
        return JsonResponse({'error': 'start is in the future'}, status=400)
    try:
        data = events.run(
            camera_id,
            lambda client: client.get_camera_video(camera_id, start, end),
            timeout=90,
        )
    except events.WorkerUnavailable:
        return _unavailable()
    except Exception as err:
        return _unavailable(err)
    if not data:
        raise Http404
    resp = HttpResponse(data, content_type='video/mp4')
    resp['Cache-Control'] = 'private, max-age=3600'
    return resp


@xframe_options_sameorigin
@require_GET
def popup(request, camera_id):
    """Chrome-less live view for the hub's doorbell pop-up (live only, no history)."""
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
    from urllib.parse import urlparse
    return render(request, 'cameras/popup.html', {
        'stream': camera['stream_name'],
        'name': camera['name'],
        'go2rtc_port': urlparse(config.go2rtc_url()).port or 1984,
    })


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

        success = ptz_goto_preset(camera_id, int(slot))
        return JsonResponse({'success': success})

    except (json.JSONDecodeError, ValueError, TypeError) as e:
        return JsonResponse(
            {'success': False, 'error': str(e)},
            status=400,
        )


# ---- Pop-ups settings page ---------------------------------------------------

POPUP_STATES_TTL = 60
POPUP_REGISTRY_TTL = 600
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
        if not is_admin(request):
            raise Http404()
        return view(request, *args, **kwargs)
    return wrapped


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
        for site in get_protect_cameras():
            cams = [
                {"id": c["camera_id"], "name": c.get("name") or c["camera_id"],
                 "doorbell": bool(c.get("is_doorbell"))}
                for c in site.get("cameras", []) if c.get("camera_id")
            ]
            if cams:
                sites.append({"name": site.get("name") or "", "cameras": cams})
    except Exception:
        logger.exception("Could not list cameras for the pop-ups page")
    try:
        cfg = popups.current()
    except Exception:
        logger.exception("Could not read the pop-up settings")
        cfg = popups.clean({})
    return render(request, "cameras/settings_popups.html", {
        "active": "cameras",
        "page": {
            "sites": sites, "config": cfg, "kinds": list(popups.POPUP_KINDS),
            "chimes": list(popups.CHIMES), "silent": popups.SILENT,
            "kind_sounds": {k: popups.kind_sound(k) for k in popups.POPUP_KINDS},
            "trigger_sound": popups.TRIGGER_SOUND,
            "save_url": request.path,
            "entities_url": reverse("cameras_popup_entities"),
        },
    })


def _ha_registry():
    """HA's entity/device/area registries, cached: ``{"entities": {entity_id: {area, device,
    category}}, "devices": {device_id: {name, maker, area}}}``."""
    from django.core.cache import cache

    reg = cache.get("cameras:popup_registry")
    if reg is None:
        from bdh_sdk import ha

        entities, area_list, devices = ha.ws_commands(
            {"type": "config/entity_registry/list"},
            {"type": "config/area_registry/list"},
            {"type": "config/device_registry/list"},
        )
        names = {a["area_id"]: a.get("name") or "" for a in area_list or []}
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
        cache.set("cameras:popup_registry", reg, POPUP_REGISTRY_TTL)
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
    """HA's entities and devices, for the trigger picker.

    ``entities``: ``[{id, name, short, area, state, domain, device_class, device, category,
    useful, suggest}]``; ``devices``: ``[{id, name, maker, area}]`` (only ones with entities).
    """
    from django.core.cache import cache

    data = cache.get("cameras:popup_entities2")
    if data is None:
        from bdh_sdk import ha

        try:
            states = ha.get_all_states()
        except ha.HAError as err:
            logger.warning("Pop-ups: HA states unavailable: %s", err)
            return JsonResponse({"error": "Home Assistant unavailable"}, status=503)
        try:
            reg = _ha_registry()
        except ha.HAError as err:
            logger.warning("Pop-ups: HA registries unavailable: %s", err)
            reg = {"entities": {}, "devices": {}}
        devices = reg["devices"]
        entities, used = [], set()
        for st in states:
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
        data = {
            "entities": entities,
            "devices": sorted(
                ({"id": d, **devices[d]} for d in used if d),
                key=lambda d: (d["name"].lower(), d["id"])),
        }
        cache.set("cameras:popup_entities2", data, POPUP_STATES_TTL)
    return JsonResponse(data)
