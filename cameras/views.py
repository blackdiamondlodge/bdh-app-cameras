import json
import logging
import urllib.request
import urllib.error
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

from django.http import JsonResponse
from django.shortcuts import render
from django.views.decorators.http import require_POST

from . import config
from .protect_api import get_protect_cameras, ptz_goto_preset

logger = logging.getLogger(__name__)


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
