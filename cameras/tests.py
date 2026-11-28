import json
import unittest
import urllib.request
import urllib.parse
from django.test import SimpleTestCase, TestCase, Client, override_settings, tag
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import User
from django.utils.functional import classproperty
from unittest.mock import ANY, AsyncMock, patch, MagicMock, call
from .views import get_go2rtc_streams, _register_streams_with_go2rtc
from django.core.cache import cache as django_cache
from .protect_api import (
    get_protect_cameras, clear_cache, _camera_name_to_stream_name,
    _fetch_cameras_from_site, _is_ptz_camera, ptz_goto_preset,
    PTZ_DEFAULT_PRESETS, CACHE_KEY_PREFIX, INCOMPLETE_CACHE_TTL,
)


def protect_settings(sites):
    """``override_settings`` kwargs that configure ``sites`` like the numbered app settings."""
    out = {}
    for i in range(1, 10):
        site = sites[i - 1] if i <= len(sites) else {}
        for field in ("host", "api_key", "name", "username", "password"):
            default = f"Site {i}" if field == "name" else ""
            out[f"BDH_CAMERAS_PROTECT_{field.upper()}_{i}"] = site.get(field, default)
    return out


def _sites_from_test_settings():
    """Stand-in for ``config.protect_sites`` that reads :func:`protect_settings` overrides."""
    from django.conf import settings as dj

    out = []
    for i in range(1, 10):
        def get(field, i=i):
            return getattr(dj, f"BDH_CAMERAS_PROTECT_{field.upper()}_{i}", "")
        if get("host") and get("api_key"):
            out.append({"id": str(i), "host": get("host"), "api_key": get("api_key"),
                        "name": get("name") or f"Site {i}", "username": get("username"),
                        "password": get("password")})
    return out


from . import config as _config_mod  # noqa: E402

_real_protect_sites = _config_mod.protect_sites
_sites_patch = patch.object(_config_mod, "protect_sites", _sites_from_test_settings)


def setUpModule():
    # The real sites come from app state and the vault (see NvrSettingsTests);
    # the rest of this file configures sites with ``protect_settings``.
    _sites_patch.start()


def tearDownModule():
    _sites_patch.stop()


# --- Protect API helper tests ---

class CameraNameToStreamNameTests(TestCase):
    """Tests for the stream name conversion helper."""

    def test_spaces_become_underscores(self):
        self.assertEqual(_camera_name_to_stream_name("Front Door"), "front_door")

    def test_hyphens_become_underscores(self):
        self.assertEqual(_camera_name_to_stream_name("Back-Yard"), "back_yard")

    def test_mixed_formatting(self):
        self.assertEqual(
            _camera_name_to_stream_name("Front Door - Cam 1"),
            "front_door___cam_1",
        )

    def test_already_lowercase(self):
        self.assertEqual(_camera_name_to_stream_name("garage"), "garage")


# --- Protect API fetch tests ---

# Mock camera list from GET /v1/cameras
MOCK_CAMERAS_RESPONSE = [
    {'id': 'cam_front', 'name': 'Front Door', 'state': 'CONNECTED'},
    {'id': 'cam_back', 'name': 'Backyard', 'state': 'CONNECTED'},
]

# Mock RTSPS stream responses from GET/POST /v1/cameras/{id}/rtsps-stream
MOCK_RTSPS_FRONT = {
    'high': 'rtsps://192.168.10.1:7441/abc123high',
    'medium': None,
    'low': 'rtsps://192.168.10.1:7441/abc123low',
}
MOCK_RTSPS_BACK = {
    'high': 'rtsps://192.168.10.1:7441/xyz789high',
    'medium': None,
    'low': 'rtsps://192.168.10.1:7441/xyz789low',
}
MOCK_RTSPS_EMPTY = {'high': None, 'medium': None, 'low': None}


def _make_mock_response(json_data, status_code=200):
    """Create a mock response object."""
    resp = MagicMock()
    resp.json.return_value = json_data
    resp.status_code = status_code
    resp.raise_for_status = MagicMock()
    return resp


def _make_ptz_not_supported_response():
    """Create a 400 response for non-PTZ cameras."""
    return _make_mock_response(
        {'error': 'Camera is not a type of PTZ'}, status_code=400,
    )


def _mock_get_side_effect(url, **kwargs):
    """Route mocked GET requests to correct responses."""
    if url.endswith('/cameras'):
        return _make_mock_response(MOCK_CAMERAS_RESPONSE)
    elif 'cam_front/rtsps-stream' in url:
        return _make_mock_response(MOCK_RTSPS_FRONT)
    elif 'cam_back/rtsps-stream' in url:
        return _make_mock_response(MOCK_RTSPS_BACK)
    return _make_mock_response(MOCK_RTSPS_EMPTY)


def _mock_post_non_ptz(url, **kwargs):
    """POST mock: all cameras return 400 (not PTZ)."""
    if '/ptz/' in url:
        return _make_ptz_not_supported_response()
    return _make_mock_response({}, status_code=200)


@override_settings(
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class FetchCamerasFromSiteTests(TestCase):
    """Tests for _fetch_cameras_from_site."""

    def setUp(self):
        # clear_cache() only covers configured sites; these tests probe
        # unconfigured hosts, so wipe everything (incl. cached PTZ answers).
        django_cache.clear()

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_returns_cameras_sorted_by_name(self, mock_get, mock_post):
        """Cameras are returned sorted alphabetically."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(len(cameras), 2)
        self.assertEqual(cameras[0]['name'], 'Backyard')
        self.assertEqual(cameras[1]['name'], 'Front Door')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_returns_rtsps_urls(self, mock_get, mock_post):
        """RTSPS URLs are returned from the API."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        front_door = next(c for c in cameras if c['name'] == 'Front Door')
        self.assertEqual(front_door['rtsp_url'], 'rtsps://192.168.10.1:7441/abc123high')
        self.assertEqual(front_door['rtsp_url_low'], 'rtsps://192.168.10.1:7441/abc123low')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_audio_flags_and_talkback(self, mock_get, mock_post):
        """Mic/speaker flags come from Protect; only speaker cameras get a talkback target."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'id': 'door', 'name': 'Door', 'state': 'CONNECTED',
                     'featureFlags': {'hasMic': True, 'hasSpeaker': True}},
                    {'id': 'yard', 'name': 'Yard', 'state': 'CONNECTED',
                     'featureFlags': {'hasMic': True}},
                    {'id': 'muted', 'name': 'Muted', 'state': 'CONNECTED', 'isMicEnabled': False,
                     'featureFlags': {'hasMic': True}},
                ])
            return _make_mock_response(MOCK_RTSPS_FRONT)

        def post_side_effect(url, **kwargs):
            if '/ptz/' in url:
                return _make_ptz_not_supported_response()
            if url.endswith('/talkback-session'):
                self.assertIn('/cameras/door/', url)
                return _make_mock_response({'url': 'rtp://192.168.10.232:7004',
                                            'codec': 'opus', 'samplingRate': 24000})
            return _make_mock_response(MOCK_RTSPS_FRONT)

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = post_side_effect

        cams = {c['camera_id']: c for c in _fetch_cameras_from_site('192.168.10.1', 'k')}
        self.assertEqual({k: (c['has_mic'], c['has_speaker']) for k, c in cams.items()},
                         {'door': (True, True), 'yard': (True, False), 'muted': (False, False)})
        self.assertEqual(cams['door']['talkback'],
                         {'url': 'rtp://192.168.10.232:7004', 'codec': 'opus', 'rate': 24000})
        self.assertIsNone(cams['yard']['talkback'])

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_creates_stream_when_none_exists(self, mock_get, mock_post):
        """Creates RTSPS stream via POST when GET returns null URLs."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'id': 'cam1', 'name': 'New Cam', 'state': 'CONNECTED'},
                ])
            return _make_mock_response(MOCK_RTSPS_EMPTY)

        def post_side_effect(url, **kwargs):
            if '/ptz/' in url:
                return _make_ptz_not_supported_response()
            return _make_mock_response({
                'high': 'rtsps://192.168.10.1:7441/newstream',
                'medium': None, 'low': None,
            })

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = post_side_effect

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['rtsp_url'], 'rtsps://192.168.10.1:7441/newstream')
        # No low URL available in this case
        self.assertEqual(cameras[0]['rtsp_url_low'], '')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_skips_cameras_without_rtsps_url(self, mock_get, mock_post):
        """Cameras where RTSPS stream creation fails are excluded."""
        import requests as req_lib

        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'id': 'cam1', 'name': 'Broken Cam', 'state': 'CONNECTED'},
                ])
            return _make_mock_response(MOCK_RTSPS_EMPTY)

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = req_lib.RequestException("Failed")

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(cameras, [])

    @patch('cameras.protect_api.requests.get')
    def test_returns_none_on_request_failure(self, mock_get):
        """Returns None when the camera list request fails."""
        import requests
        mock_get.side_effect = requests.RequestException("Connection refused")

        result = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertIsNone(result)

    def test_returns_none_when_not_configured(self):
        """Returns None when host/key are empty."""
        result = _fetch_cameras_from_site('', '')

        self.assertIsNone(result)

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_generates_stream_name(self, mock_get, mock_post):
        """Stream names are generated from camera names."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        front_door = next(c for c in cameras if c['name'] == 'Front Door')
        self.assertEqual(front_door['stream_name'], 'front_door')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_stream_name_prefixed_with_site_name(self, mock_get, mock_post):
        """Stream names include site prefix to avoid cross-site collisions."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site(
            '192.168.10.1', 'test_api_key', site_name='Sun Peaks',
        )

        front_door = next(c for c in cameras if c['name'] == 'Front Door')
        self.assertEqual(front_door['stream_name'], 'sun_peaks_front_door')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_handles_dict_format_cameras(self, mock_get, mock_post):
        """Handles response where cameras is a dict keyed by ID."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response({
                    'cam1': {'id': 'cam1', 'name': 'Garage', 'state': 'CONNECTED'},
                })
            return _make_mock_response({
                'high': 'rtsps://192.168.10.1:7441/garageAlias',
                'medium': None, 'low': 'rtsps://192.168.10.1:7441/garageAliaslow',
            })

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(len(cameras), 1)
        self.assertEqual(cameras[0]['name'], 'Garage')
        self.assertEqual(cameras[0]['rtsp_url_low'], 'rtsps://192.168.10.1:7441/garageAliaslow')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_sends_api_key_header(self, mock_get, mock_post):
        """Verifies the API key is sent in the X-API-KEY header."""
        mock_get.side_effect = lambda url, **kwargs: _make_mock_response([])
        mock_get.return_value = _make_mock_response([])

        _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        first_call = mock_get.call_args_list[0]
        self.assertEqual(first_call.kwargs['headers']['X-API-KEY'], 'test_api_key')
        self.assertIn('/cameras', first_call.args[0])

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_strips_enable_srtp_from_url(self, mock_get, mock_post):
        """Strips ?enableSrtp from RTSPS URLs — go2rtc handles TLS natively."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'id': 'cam1', 'name': 'Cam', 'state': 'CONNECTED'},
                ])
            return _make_mock_response({
                'high': 'rtsps://192.168.10.1:7441/stream1?enableSrtp',
                'medium': None, 'low': 'rtsps://192.168.10.1:7441/stream1low?enableSrtp',
            })

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(cameras[0]['rtsp_url'], 'rtsps://192.168.10.1:7441/stream1')
        self.assertEqual(cameras[0]['rtsp_url_low'], 'rtsps://192.168.10.1:7441/stream1low')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_skips_cameras_without_id(self, mock_get, mock_post):
        """Cameras missing an ID field are skipped."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'name': 'No ID Cam', 'state': 'CONNECTED'},
                ])
            return _make_mock_response(MOCK_RTSPS_EMPTY)

        mock_get.side_effect = get_side_effect

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(cameras, [])


# --- Cache tests ---

@override_settings(
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class ProtectCachingTests(TestCase):
    """Tests for the camera list caching."""

    def setUp(self):
        clear_cache()

    @patch('cameras.protect_api._fetch_cameras_from_site_ex')
    def test_caches_result(self, mock_fetch):
        """Second call uses cached result, doesn't re-fetch."""
        mock_fetch.return_value = ([{'name': 'Cam1', 'stream_name': 'cam1', 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low'}], True)

        result1 = get_protect_cameras()
        result2 = get_protect_cameras()

        mock_fetch.assert_called_once()
        self.assertEqual(result1, result2)

    @patch('cameras.protect_api._fetch_cameras_from_site_ex')
    def test_returns_empty_list_on_failure(self, mock_fetch):
        """Returns site with empty cameras when fetch fails (returns None)."""
        mock_fetch.return_value = (None, False)

        result = get_protect_cameras()

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]['cameras'], [])

    def test_clear_cache(self):
        """clear_cache resets the internal cache."""
        cache_key = f'{CACHE_KEY_PREFIX}192.168.10.1'
        django_cache.set(cache_key, {'cameras': [{'name': 'test'}]}, 300)

        clear_cache()

        self.assertIsNone(django_cache.get(cache_key))

    @override_settings(**protect_settings([]))
    def test_returns_empty_list_when_no_sites(self):
        """Returns empty list when no sites are configured."""
        result = get_protect_cameras()

        self.assertEqual(result, [])


# --- go2rtc fallback stream tests ---

class GetGo2rtcStreamsTests(TestCase):
    """Tests for the go2rtc fallback stream fetcher."""

    @patch("cameras.views.urllib.request.urlopen")
    def test_returns_sorted_streams(self, mock_urlopen):
        """Streams are returned sorted alphabetically with display names."""
        mock_response = MagicMock()
        mock_response.read.return_value = json.dumps({
            "front_door": [{"url": "rtsp://..."}],
            "backyard": [{"url": "rtsp://..."}],
            "driveway": [{"url": "rtsp://..."}],
        }).encode()
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        streams = get_go2rtc_streams("http://localhost:1984")

        self.assertEqual(len(streams), 3)
        self.assertEqual(streams[0]["name"], "backyard")
        self.assertEqual(streams[0]["display_name"], "Backyard")
        self.assertEqual(streams[1]["name"], "driveway")
        self.assertEqual(streams[2]["name"], "front_door")
        self.assertEqual(streams[2]["display_name"], "Front Door")

    @patch("cameras.views.urllib.request.urlopen")
    def test_returns_empty_on_connection_error(self, mock_urlopen):
        """Returns empty list when go2rtc is unreachable."""
        mock_urlopen.side_effect = OSError("Connection refused")

        streams = get_go2rtc_streams("http://localhost:1984")

        self.assertEqual(streams, [])

    @patch("cameras.views.urllib.request.urlopen")
    def test_returns_empty_on_invalid_json(self, mock_urlopen):
        """Returns empty list when go2rtc returns invalid JSON."""
        mock_response = MagicMock()
        mock_response.read.return_value = b"not json"
        mock_response.__enter__ = lambda s: s
        mock_response.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = mock_response

        streams = get_go2rtc_streams("http://localhost:1984")

        self.assertEqual(streams, [])


# --- View tests ---

@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class CameraFeedViewWithProtectTests(TestCase):
    """Tests for camera_feed_view when Protect is configured."""

    def setUp(self):
        self.client = Client()
        clear_cache()

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_returns_200(self, mock_cameras, mock_register):
        """Camera page returns 200."""
        mock_cameras.return_value = [{'name': 'Test Site', 'host': '192.168.10.1', 'cameras': []}]

        response = self.client.get("/cameras/")

        self.assertEqual(response.status_code, 200)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_uses_protect_cameras(self, mock_cameras, mock_register):
        """Streams come from Protect API, not go2rtc fallback."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {'name': 'Front Door', 'stream_name': 'front_door',
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
            ],
        }]

        response = self.client.get("/cameras/")

        sites = response.context['sites']
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]['name'], 'Test Site')
        self.assertEqual(len(sites[0]['streams']), 1)
        self.assertEqual(sites[0]['streams'][0]['display_name'], 'Front Door')

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_registers_streams_with_go2rtc(self, mock_cameras, mock_register):
        """Discovered cameras are registered with go2rtc."""
        cameras = [
            {'name': 'Front Door', 'stream_name': 'front_door',
             'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
             'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
        ]
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1', 'cameras': cameras,
        }]

        self.client.get("/cameras/")

        mock_register.assert_called_once_with('http://localhost:1984', cameras)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_shows_no_cameras_message(self, mock_cameras, mock_register):
        """Shows 'No Cameras Available' when Protect returns no cameras."""
        mock_cameras.return_value = [{'name': 'Test Site', 'host': '192.168.10.1', 'cameras': []}]

        response = self.client.get("/cameras/")

        self.assertContains(response, "No Cameras Available")

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_renders_camera_streams(self, mock_cameras, mock_register):
        """Renders video-stream elements for each discovered camera."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {'name': 'Front Door', 'stream_name': 'front_door',
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
                {'name': 'Backyard', 'stream_name': 'backyard',
                 'rtsp_url': 'rtsps://y', 'rtsp_url_low': 'rtsps://y_low',
                 'camera_id': 'cam2', 'is_ptz': False, 'ptz_presets': 0},
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        # Camera names are displayed
        self.assertContains(response, "Front Door")
        self.assertContains(response, "Backyard")
        # Live cameras hold off the hub screensaver while open
        self.assertContains(response, '<meta name="bdh-screensaver" content="off">')
        # Uses the correct video-stream custom element (not video-rtc)
        self.assertIn("<video-stream", content)
        self.assertNotIn("<video-rtc", content)

        with patch("cameras.config.keep_open", return_value=False):
            response = self.client.get("/cameras/")
        self.assertNotContains(response, 'name="bdh-screensaver"')
        # Stream names stored as data attributes for dynamic URL construction
        self.assertIn('data-stream-name="front_door"', content)
        self.assertIn('data-stream-name="backyard"', content)
        # Low-quality flag is set
        self.assertIn('data-has-low="true"', content)
        # Properties set via JS using dynamic go2rtc base URL
        self.assertIn('el.dataset.streamName', content)
        # Loads the correct JS module (video-stream.js, not video-rtc.js)
        self.assertIn('video-stream.js', content)
        self.assertNotIn('video-rtc.js', content)
        # Each camera card has a touch overlay div for fullscreen
        self.assertEqual(content.count('class="cam-touch-overlay"'), 2)
        # WebRTC-only first (no MSE warm-up -> no mid-start source swap /
        # black flash), with MSE as the fallback mode.
        self.assertIn('data-mode="webrtc/tcp"', content)
        self.assertIn('data-fallback-mode="mse"', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_stream_mode_webrtc_with_mse_fallback(self, mock_cameras, mock_register):
        """Tiles start WebRTC-only and fall back to MSE.

        Running MSE and WebRTC in parallel ('webrtc,mse,mp4') made go2rtc
        show MSE frames first and then swap the <video> source to WebRTC,
        blanking every tile mid-start.
        """
        mock_cameras.return_value = [{
            'name': 'Test', 'host': '192.168.10.1',
            'cameras': [
                {'name': 'Cam', 'stream_name': 'cam',
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': 'c1', 'is_ptz': False, 'ptz_presets': 0},
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('data-mode="webrtc/tcp"', content)
        self.assertIn('data-fallback-mode="mse"', content)
        self.assertNotIn('data-mode="webrtc,mse', content)
        self.assertIn('function startStream(el, src)', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_video_transport_setting(self, mock_cameras, mock_register):
        """TCP by default; the advanced setting switches every stream to UDP-first."""
        mock_cameras.return_value = [{
            'name': 'Test', 'host': '192.168.10.1',
            'cameras': [
                {'name': 'Cam', 'stream_name': 'cam',
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': 'c1', 'is_ptz': False, 'ptz_presets': 0},
            ],
        }]
        from . import config
        real = config.app_setting
        with patch("cameras.config.app_setting", side_effect=lambda app, key, *a, **kw:
                   "udp" if key == "video_transport" else real(app, key, *a, **kw)):
            page = self.client.get("/cameras/").content.decode()
            popup = self.client.get("/cameras/popup/c1/").content.decode()
        for html in (page, popup):
            self.assertIn('data-mode="webrtc"', html)
            self.assertNotIn('webrtc/tcp', html)
        # Fullscreen high-res copies the tile's mode instead of hard-coding one
        self.assertIn('hi.dataset.mode = low.dataset.mode', page)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_stats_overlay_per_card(self, mock_cameras, mock_register):
        """Each card has its own stats box, toggled by a three-finger tap."""
        mock_cameras.return_value = [{
            'name': 'Test', 'host': '192.168.10.1',
            'cameras': [
                {'name': n, 'stream_name': n.lower(),
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': n, 'is_ptz': False, 'ptz_presets': 0}
                for n in ('A', 'B')
            ],
        }]
        html = self.client.get("/cameras/").content.decode()
        self.assertEqual(html.count('class="cam-stats"'), 2)
        self.assertIn('e.touches.length === 3', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_view_includes_fullscreen_overlay_and_js(self, mock_cameras, mock_register):
        """Touch overlay and fullscreen JS are present in the rendered page."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {'name': 'Front Door', 'stream_name': 'front_door',
                 'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                 'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        # Touch overlay div sits above video for tap handling
        self.assertIn('class="cam-touch-overlay"', content)
        # Fullscreen toggle JS wired to the overlay
        self.assertIn('requestFullscreen', content)
        self.assertIn('exitFullscreen', content)
        self.assertIn('.cam-touch-overlay', content)
        # Stream quality management JS
        self.assertIn('fullscreenchange', content)
        self.assertIn('stopStreams', content)


MULTI_SITE_CAMERAS = [
    {
        'name': 'Sun Peaks', 'host': '192.168.10.1',
        'cameras': [
            {'name': 'Front Door', 'stream_name': 'sp_front_door',
             'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
             'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
        ],
    },
    {
        'name': 'Mercer Island', 'host': '192.168.1.26',
        'cameras': [
            {'name': 'Driveway', 'stream_name': 'mi_driveway',
             'rtsp_url': 'rtsps://y', 'rtsp_url_low': 'rtsps://y_low',
             'camera_id': 'cam2', 'is_ptz': False, 'ptz_presets': 0},
        ],
    },
]


def _cam(name, cid, **extra):
    cam = {'name': name, 'stream_name': cid, 'rtsp_url': 'rtsps://x',
           'rtsp_url_low': 'rtsps://x_low', 'camera_id': cid,
           'is_ptz': False, 'ptz_presets': 0}
    cam.update(extra)
    return cam


@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'k', 'name': 'Sun Peaks'}]),
)
class HeroLayoutTests(TestCase):
    """Sites with enough cameras feature the doorbell as a large hero tile."""

    def setUp(self):
        self.client = Client()
        clear_cache()

    def _get(self, cameras):
        with patch("cameras.views._register_streams_with_go2rtc"), \
                patch("cameras.views.get_protect_cameras") as mock_cameras:
            mock_cameras.return_value = [
                {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': cameras},
            ]
            return self.client.get("/cameras/")

    def test_doorbell_moved_first_as_hero(self):
        cams = [_cam(n, f"c{i}") for i, n in enumerate(['Backyard', 'Garage', 'Hot Tub', 'Side Yard'])]
        cams.insert(2, _cam('Front Bell', 'bell', is_doorbell=True))
        response = self._get(cams)
        site = response.context['sites'][0]
        self.assertTrue(site['hero'])
        self.assertEqual(
            [s['display_name'] for s in site['streams']],
            ['Front Bell', 'Backyard', 'Garage', 'Hot Tub', 'Side Yard'],
        )
        content = response.content.decode()
        self.assertIn(' has-hero"', content)
        self.assertEqual(content.count('cam-card hero'), 1)

    def test_doorbell_matched_by_name(self):
        names = ['Backyard', 'Doorbell Camera', 'Garage', 'Hot Tub', 'Side Yard']
        response = self._get([_cam(n, f"c{i}") for i, n in enumerate(names)])
        self.assertEqual(response.context['sites'][0]['streams'][0]['display_name'], 'Doorbell Camera')

    def test_first_camera_is_hero_without_doorbell(self):
        names = ['Backyard', 'Garage', 'Hot Tub', 'Side Yard', 'Shed']
        response = self._get([_cam(n, f"c{i}") for i, n in enumerate(names)])
        site = response.context['sites'][0]
        self.assertTrue(site['hero'])
        self.assertEqual([s['display_name'] for s in site['streams']], names)

    def test_small_site_has_no_hero(self):
        names = ['Doorbell', 'Garage', 'Hot Tub']
        response = self._get([_cam(n, f"c{i}") for i, n in enumerate(names)])
        site = response.context['sites'][0]
        self.assertFalse(site['hero'])
        self.assertEqual([s['display_name'] for s in site['streams']], names)
        content = response.content.decode()
        self.assertNotIn(' has-hero"', content)
        self.assertNotIn('cam-card hero', content)


@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([
        {'host': '192.168.10.1', 'api_key': 'key1', 'name': 'Sun Peaks'},
        {'host': '192.168.1.26', 'api_key': 'key2', 'name': 'Mercer Island'},
    ]),
)
class CameraFeedViewAuthFilterTests(TestCase):
    """Until the Guests page is saved, guests see only the first site; admins see all."""

    def setUp(self):
        self.client = Client()
        clear_cache()
        self.user = User.objects.create_user(
            username='testuser', password='testpass', is_staff=True,
        )

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_anonymous_sees_only_first_site(self, mock_cameras, mock_register):
        """Unauthenticated users only see the primary (first) site."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS

        response = self.client.get("/cameras/")

        sites = response.context['sites']
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]['name'], 'Sun Peaks')

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_anonymous_does_not_see_second_site(self, mock_cameras, mock_register):
        """Unauthenticated users do not see secondary site cameras."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertNotContains(response, 'Mercer Island')
        self.assertNotIn('mi_driveway', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_anonymous_no_site_tabs(self, mock_cameras, mock_register):
        """With only one visible site, no tab bar is shown."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertNotIn('class="site-tabs"', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_authenticated_sees_all_sites(self, mock_cameras, mock_register):
        """Signed-in admins see all configured sites."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS
        self.client.login(username='testuser', password='testpass')

        response = self.client.get("/cameras/")

        sites = response.context['sites']
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0]['name'], 'Sun Peaks')
        self.assertEqual(sites[1]['name'], 'Mercer Island')

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_authenticated_sees_site_tabs(self, mock_cameras, mock_register):
        """Signed-in admins see the site tab bar with both sites."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS
        self.client.login(username='testuser', password='testpass')

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('class="site-tabs"', content)
        self.assertContains(response, 'Sun Peaks')
        self.assertContains(response, 'Mercer Island')

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_authenticated_only_registers_visible_streams(self, mock_cameras, mock_register):
        """Only streams for visible sites are registered with go2rtc."""
        mock_cameras.return_value = MULTI_SITE_CAMERAS

        # Anonymous: only first site's streams registered
        self.client.get("/cameras/")

        self.assertEqual(mock_register.call_count, 1)
        registered_cameras = mock_register.call_args[0][1]
        stream_names = [c['stream_name'] for c in registered_cameras]
        self.assertIn('sp_front_door', stream_names)
        self.assertNotIn('mi_driveway', stream_names)


@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([]),
)
class CameraFeedViewFallbackTests(TestCase):
    """Tests for camera_feed_view when Protect is NOT configured (fallback)."""

    def setUp(self):
        self.client = Client()

    @patch("cameras.views.get_go2rtc_streams")
    def test_falls_back_to_go2rtc(self, mock_streams):
        """Uses go2rtc stream list when Protect is not configured."""
        mock_streams.return_value = [
            {'name': 'manual_cam', 'display_name': 'Manual Cam'},
        ]

        response = self.client.get("/cameras/")

        self.assertEqual(response.status_code, 200)
        sites = response.context['sites']
        self.assertEqual(len(sites), 1)
        self.assertEqual(sites[0]['name'], 'Cameras')
        self.assertEqual(len(sites[0]['streams']), 1)
        self.assertEqual(sites[0]['streams'][0]['name'], 'manual_cam')


# --- Stream registration tests ---

class RegisterStreamsTests(TestCase):
    """Tests for _register_streams_with_go2rtc."""

    def setUp(self):
        from . import views
        views._wanted.clear()
        self.addCleanup(views._wanted.clear)

    @patch("cameras.views.urllib.request.urlopen")
    def test_registers_missing_streams(self, mock_urlopen):
        """Registers both high and low streams that don't exist in go2rtc."""
        # First call: GET existing streams (empty)
        get_resp = MagicMock()
        get_resp.read.return_value = json.dumps({}).encode()
        get_resp.__enter__ = lambda s: s
        get_resp.__exit__ = MagicMock(return_value=False)

        # PUT calls for high and low streams
        put_resp = MagicMock()
        put_resp.__enter__ = lambda s: s
        put_resp.__exit__ = MagicMock(return_value=False)

        put_resp2 = MagicMock()
        put_resp2.__enter__ = lambda s: s
        put_resp2.__exit__ = MagicMock(return_value=False)

        mock_urlopen.side_effect = [get_resp, put_resp, put_resp2]

        cameras = [
            {'name': 'Front Door', 'stream_name': 'front_door',
             'rtsp_url': 'rtsps://host:7441/abc',
             'rtsp_url_low': 'rtsps://host:7441/abc_low'},
        ]

        _register_streams_with_go2rtc('http://localhost:1984', cameras)

        # Three calls: GET streams + PUT high + PUT low
        self.assertEqual(mock_urlopen.call_count, 3)

    @patch("cameras.views.urllib.request.urlopen")
    def test_skips_existing_streams(self, mock_urlopen):
        """Does not re-register streams already in go2rtc."""
        get_resp = MagicMock()
        get_resp.read.return_value = json.dumps({
            "front_door": [{"url": "rtsps://host:7441/abc"}],
            "front_door_low": [{"url": "rtsps://host:7441/abc_low"}],
        }).encode()
        get_resp.__enter__ = lambda s: s
        get_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = get_resp

        cameras = [
            {'name': 'Front Door', 'stream_name': 'front_door',
             'rtsp_url': 'rtsps://host:7441/abc',
             'rtsp_url_low': 'rtsps://host:7441/abc_low'},
        ]

        _register_streams_with_go2rtc('http://localhost:1984', cameras)

        # Only one call: GET streams (no PUTs needed)
        self.assertEqual(mock_urlopen.call_count, 1)

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_reregisters_stream_when_url_changes(self, mock_urlopen, mock_put):
        """A rotated RTSPS token replaces the stale source in go2rtc."""
        get_resp = MagicMock()
        get_resp.read.return_value = json.dumps({
            "front_door": {"producers": [{"url": "rtsps://host:7441/old"}]},
            "front_door_low": {"producers": [{"url": "rtsps://host:7441/abc_low"}]},
        }).encode()
        get_resp.__enter__ = lambda s: s
        get_resp.__exit__ = MagicMock(return_value=False)
        mock_urlopen.return_value = get_resp

        cameras = [
            {'name': 'Front Door', 'stream_name': 'front_door',
             'rtsp_url': 'rtsps://host:7441/new',
             'rtsp_url_low': 'rtsps://host:7441/abc_low'},
        ]

        _register_streams_with_go2rtc('http://localhost:1984', cameras)

        mock_put.assert_called_once_with(
            'http://localhost:1984', 'front_door', 'rtsps://host:7441/new',
        )

    @patch("cameras.views.urllib.request.urlopen")
    def test_handles_go2rtc_unreachable(self, mock_urlopen):
        """Gracefully handles go2rtc being unreachable."""
        mock_urlopen.side_effect = OSError("Connection refused")

        cameras = [
            {'name': 'Front Door', 'stream_name': 'front_door',
             'rtsp_url': 'rtsps://host:7441/abc',
             'rtsp_url_low': 'rtsps://host:7441/abc_low'},
        ]

        # Should not raise
        _register_streams_with_go2rtc('http://localhost:1984', cameras)

    @patch("cameras.views.urllib.request.urlopen")
    def test_config_file_disabled_is_not_a_failure(self, mock_urlopen):
        """go2rtc's 'config file disabled' 400 means the stream was created."""
        import io
        from .views import _put_stream

        def http_400(body):
            def raise_(*_a, **_k):
                raise urllib.error.HTTPError(
                    'u', 400, 'Bad Request', {}, io.BytesIO(body))
            return raise_

        mock_urlopen.side_effect = http_400(b'config file disabled\n')
        with self.assertNoLogs('cameras.views', level='WARNING'):
            _put_stream('http://localhost:1984', 'front_door', 'rtsps://x')

        mock_urlopen.side_effect = http_400(b'invalid source\n')
        with self.assertLogs('cameras.views', level='WARNING'):
            _put_stream('http://localhost:1984', 'front_door', 'rtsps://x')


class StreamKeeperTests(TestCase):
    """go2rtc has no config file, so a restart forgets every stream; the
    keeper must put back the ones page loads registered."""

    CAMS = [
        {'name': 'Front Door', 'stream_name': 'front_door',
         'rtsp_url': 'rtsps://host:7441/abc',
         'rtsp_url_low': 'rtsps://host:7441/abc_low'},
        {'name': 'Hot Tub', 'stream_name': 'hot_tub',
         'rtsp_url': 'rtsps://host:7441/tub', 'rtsp_url_low': ''},
    ]
    URL = 'http://localhost:1984'

    def setUp(self):
        from . import views
        self.views = views
        views._wanted.clear()
        self.addCleanup(views._wanted.clear)
        sites = patch("cameras.views.config.protect_sites", return_value=[])
        self.protect_sites = sites.start()
        self.addCleanup(sites.stop)

    def _listing(self, data):
        resp = MagicMock()
        resp.read.return_value = json.dumps(data).encode()
        resp.__enter__ = lambda s: s
        resp.__exit__ = MagicMock(return_value=False)
        return resp

    def _all_present(self):
        return {
            'front_door': {'producers': [{'url': 'rtsps://host:7441/abc'}]},
            'front_door_low': {'producers': [{'url': 'rtsps://host:7441/abc_low'}]},
            'hot_tub': {'producers': [{'url': 'rtsps://host:7441/tub'}]},
        }

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_restores_streams_after_go2rtc_restart(self, mock_urlopen, mock_put):
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)
        mock_put.assert_not_called()

        # go2rtc restarted: its stream list is empty.
        mock_urlopen.return_value = self._listing({})
        with self.assertLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()

        self.assertCountEqual(
            [c.args for c in mock_put.call_args_list],
            [(self.URL, 'front_door', 'rtsps://host:7441/abc'),
             (self.URL, 'front_door_low', 'rtsps://host:7441/abc_low'),
             (self.URL, 'hot_tub', 'rtsps://host:7441/tub')],
        )

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_restores_only_the_missing_stream(self, mock_urlopen, mock_put):
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)

        partial = self._all_present()
        del partial['hot_tub']
        mock_urlopen.return_value = self._listing(partial)
        with self.assertLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()
        mock_put.assert_called_once_with(self.URL, 'hot_tub', 'rtsps://host:7441/tub')

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_no_puts_when_everything_present(self, mock_urlopen, mock_put):
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)
        with self.assertNoLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()
        mock_put.assert_not_called()

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_mic_camera_gets_opus_audio_source(self, mock_urlopen, mock_put):
        cam = dict(self.CAMS[0], has_mic=True)
        hi = {'url': 'rtsps://host:7441/abc'}
        opus = {'url': 'ffmpeg:rtsps://host:7441/abc_low#audio=opus'}
        low = {'producers': [{'url': 'rtsps://host:7441/abc_low'}]}

        # Only the video source is registered: re-PUT with both sources.
        mock_urlopen.return_value = self._listing(
            {'front_door': {'producers': [hi]}, 'front_door_low': low})
        _register_streams_with_go2rtc(self.URL, [cam])
        mock_put.assert_called_once_with(
            self.URL, 'front_door',
            ('rtsps://host:7441/abc', 'ffmpeg:rtsps://host:7441/abc_low#audio=opus'))

        # Both sources present: nothing to do.
        mock_put.reset_mock()
        mock_urlopen.return_value = self._listing(
            {'front_door': {'producers': [hi, opus]}, 'front_door_low': low})
        _register_streams_with_go2rtc(self.URL, [cam])
        mock_put.assert_not_called()

    @patch("cameras.views.urllib.request.urlopen")
    def test_put_stream_sends_every_source(self, mock_urlopen):
        from .views import _put_stream
        _put_stream(self.URL, 'cam', ('rtsps://h/a', 'ffmpeg:rtsps://h/b#audio=opus'))
        url = mock_urlopen.call_args.args[0].full_url
        self.assertEqual(
            url,
            f'{self.URL}/api/streams?name=cam&src=rtsps%3A%2F%2Fh%2Fa'
            '&src=ffmpeg%3Artsps%3A%2F%2Fh%2Fb%23audio%3Dopus')

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_waits_while_go2rtc_is_down(self, mock_urlopen, mock_put):
        """No PUT storm (and no warning per stream) while go2rtc is restarting."""
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)

        mock_urlopen.side_effect = OSError("Connection refused")
        self.views.keep_streams_once()
        mock_put.assert_not_called()

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_keeps_latest_source_after_token_rotation(self, mock_urlopen, mock_put):
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)
        rotated = [dict(self.CAMS[1], rtsp_url='rtsps://host:7441/tub2')]
        _register_streams_with_go2rtc(self.URL, rotated)
        mock_put.reset_mock()

        mock_urlopen.return_value = self._listing({})
        with self.assertLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()
        self.assertIn((self.URL, 'hot_tub', 'rtsps://host:7441/tub2'),
                      [c.args for c in mock_put.call_args_list])
        self.assertNotIn((self.URL, 'hot_tub', 'rtsps://host:7441/tub'),
                         [c.args for c in mock_put.call_args_list])

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_registers_protect_cameras_without_a_page_load(self, mock_urlopen, mock_put):
        """After an app update nothing is remembered and the open wall display
        never reloads; the keeper must still register every Protect camera."""
        self.protect_sites.return_value = [{'host': 'nvr'}]
        mock_urlopen.return_value = self._listing({})
        with patch("cameras.views.get_protect_cameras",
                   return_value=[{'cameras': self.CAMS}, {'cameras': [], 'ok': False}]), \
             patch("cameras.views.config.go2rtc_url", return_value=self.URL), \
             self.assertLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()

        self.assertCountEqual(
            [c.args for c in mock_put.call_args_list],
            [(self.URL, 'front_door', 'rtsps://host:7441/abc'),
             (self.URL, 'front_door_low', 'rtsps://host:7441/abc_low'),
             (self.URL, 'hot_tub', 'rtsps://host:7441/tub')],
        )

    @patch("cameras.views._put_stream")
    @patch("cameras.views.urllib.request.urlopen")
    def test_protect_failure_still_restores_remembered_streams(self, mock_urlopen, mock_put):
        mock_urlopen.return_value = self._listing(self._all_present())
        _register_streams_with_go2rtc(self.URL, self.CAMS)
        self.protect_sites.return_value = [{'host': 'nvr'}]
        mock_urlopen.return_value = self._listing({})
        with patch("cameras.views.get_protect_cameras", side_effect=RuntimeError("nvr down")), \
             self.assertLogs('cameras.views', level='WARNING'):
            self.views.keep_streams_once()
        self.assertEqual(mock_put.call_count, 3)

    def test_keeper_starts_once(self):
        self.views._keeper_started = False
        self.addCleanup(setattr, self.views, '_keeper_started', False)
        with patch("cameras.views.threading.Thread") as thread:
            self.views.start_stream_keeper()
            self.views.start_stream_keeper()
        thread.assert_called_once()
        self.assertTrue(thread.call_args.kwargs['daemon'])


# --- PTZ API tests ---

class PtzDetectionTests(TestCase):
    """Tests for PTZ camera detection via safe slot 1000 probe."""

    def setUp(self):
        # clear_cache() only covers configured sites; these tests probe
        # unconfigured hosts, so wipe everything (incl. cached PTZ answers).
        django_cache.clear()

    @patch('cameras.protect_api.requests.post')
    def test_probe_result_is_cached(self, mock_post):
        """A camera is probed once; later refreshes reuse the answer."""
        mock_post.return_value = _make_ptz_not_supported_response()

        self.assertFalse(_is_ptz_camera('192.168.10.1', 'key', 'cam_fixed'))
        self.assertFalse(_is_ptz_camera('192.168.10.1', 'key', 'cam_fixed'))

        self.assertEqual(mock_post.call_count, 1)

    @patch('cameras.protect_api.requests.post')
    def test_detects_ptz_camera(self, mock_post):
        """Returns True when camera responds 404 (preset not found = PTZ)."""
        mock_post.return_value = _make_mock_response(
            {'error': "Entity 'preset' not found"}, status_code=404,
        )

        result = _is_ptz_camera('192.168.10.1', 'key', 'cam_ptz')

        self.assertTrue(result)
        self.assertIn('/ptz/goto/1000', mock_post.call_args.args[0])

    @patch('cameras.protect_api.requests.post')
    def test_detects_non_ptz_camera(self, mock_post):
        """Returns False when camera responds 400 (not a PTZ camera)."""
        mock_post.return_value = _make_ptz_not_supported_response()

        result = _is_ptz_camera('192.168.10.1', 'key', 'cam_fixed')

        self.assertFalse(result)

    @patch('cameras.protect_api.requests.post')
    def test_returns_none_on_network_error(self, mock_post):
        """Returns None (unknown) when request fails."""
        import requests
        mock_post.side_effect = requests.RequestException("Timeout")

        result = _is_ptz_camera('192.168.10.1', 'key', 'cam1')

        self.assertIsNone(result)

    @patch('cameras.protect_api.requests.post')
    def test_detects_ptz_from_camera_type_without_probe(self, mock_post):
        """A camera type containing "PTZ" is PTZ without an API call."""
        result = _is_ptz_camera('192.168.10.1', 'key', 'cam1', 'UVC G5 PTZ')

        self.assertTrue(result)
        mock_post.assert_not_called()

    @patch('cameras.protect_api.time.sleep')
    @patch('cameras.protect_api.requests.post')
    def test_retries_when_rate_limited(self, mock_post, mock_sleep):
        """A 429 is retried rather than treated as "not PTZ"."""
        mock_post.side_effect = [
            _make_mock_response({}, status_code=429),
            _make_mock_response(
                {'error': "Entity 'preset' not found"}, status_code=404,
            ),
        ]

        result = _is_ptz_camera('192.168.10.1', 'key', 'cam_ptz')

        self.assertTrue(result)
        self.assertEqual(mock_post.call_count, 2)
        mock_sleep.assert_called_once()


@override_settings(
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class PtzGotoPresetTests(TestCase):
    """Tests for ptz_goto_preset."""

    def setUp(self):
        clear_cache()
        # Populate cache so _find_site_for_camera can find the host
        cache_key = f'{CACHE_KEY_PREFIX}192.168.10.1'
        django_cache.set(cache_key, [
            {'camera_id': 'cam_ptz', 'name': 'PTZ Cam'},
            {'camera_id': 'cam_fixed', 'name': 'Fixed Cam'},
        ], 300)

    @patch('cameras.protect_api.requests.post')
    def test_returns_true_on_success(self, mock_post):
        """Returns True when goto returns 204."""
        mock_post.return_value = _make_mock_response({}, status_code=204)

        result = ptz_goto_preset('cam_ptz', 2)

        self.assertTrue(result)

    @patch('cameras.protect_api.requests.post')
    def test_returns_false_on_failure(self, mock_post):
        """Returns False when goto returns non-204."""
        mock_post.return_value = _make_ptz_not_supported_response()

        result = ptz_goto_preset('cam_fixed', 0)

        self.assertFalse(result)

    @patch('cameras.protect_api.requests.post')
    def test_returns_false_on_network_error(self, mock_post):
        """Returns False on network error."""
        import requests
        mock_post.side_effect = requests.RequestException("Timeout")

        result = ptz_goto_preset('cam_ptz', 0)

        self.assertFalse(result)

    @override_settings(**protect_settings([]))
    def test_returns_false_when_not_configured(self):
        """Returns False when Protect is not configured."""
        result = ptz_goto_preset('cam1', 0)

        self.assertFalse(result)


class FetchCamerasWithPtzTests(TestCase):
    """Tests for auto-detected PTZ info in _fetch_cameras_from_site results."""

    def setUp(self):
        # clear_cache() only covers configured sites; these tests probe
        # unconfigured hosts, so wipe everything (incl. cached PTZ answers).
        django_cache.clear()

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_non_ptz_cameras_have_is_ptz_false(self, mock_get, mock_post):
        """Non-PTZ cameras (400 on probe) have is_ptz=False."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        for cam in cameras:
            self.assertFalse(cam['is_ptz'])
            self.assertEqual(cam['ptz_presets'], 0)

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_ptz_camera_auto_detected(self, mock_get, mock_post):
        """Camera responding 404 to slot 1000 probe is detected as PTZ."""
        def get_side_effect(url, **kwargs):
            if url.endswith('/cameras'):
                return _make_mock_response([
                    {'id': 'cam_ptz', 'name': 'Hot Tub', 'state': 'CONNECTED'},
                ])
            return _make_mock_response({
                'high': 'rtsps://192.168.10.1:7441/hottub',
                'medium': None, 'low': 'rtsps://192.168.10.1:7441/hottublow',
            })

        def post_side_effect(url, **kwargs):
            if '/ptz/goto/1000' in url:
                return _make_mock_response(
                    {'error': "Entity 'preset' not found"}, status_code=404,
                )
            return _make_mock_response({}, status_code=200)

        mock_get.side_effect = get_side_effect
        mock_post.side_effect = post_side_effect

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(len(cameras), 1)
        self.assertTrue(cameras[0]['is_ptz'])
        self.assertEqual(cameras[0]['ptz_presets'], PTZ_DEFAULT_PRESETS)
        self.assertEqual(cameras[0]['camera_id'], 'cam_ptz')

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_mixed_ptz_and_non_ptz(self, mock_get, mock_post):
        """Only cameras responding 404 to probe are marked PTZ."""
        mock_get.side_effect = _mock_get_side_effect

        def post_side_effect(url, **kwargs):
            # Front Door is PTZ, Backyard is not
            if 'cam_front/ptz/goto/1000' in url:
                return _make_mock_response(
                    {'error': "Entity 'preset' not found"}, status_code=404,
                )
            if '/ptz/' in url:
                return _make_ptz_not_supported_response()
            return _make_mock_response({}, status_code=200)

        mock_post.side_effect = post_side_effect

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        front = next(c for c in cameras if c['name'] == 'Front Door')
        back = next(c for c in cameras if c['name'] == 'Backyard')
        self.assertTrue(front['is_ptz'])
        self.assertEqual(front['ptz_presets'], PTZ_DEFAULT_PRESETS)
        self.assertFalse(back['is_ptz'])
        self.assertEqual(back['ptz_presets'], 0)

    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_cameras_include_camera_id(self, mock_get, mock_post):
        """Cameras include their Protect camera_id."""
        mock_get.side_effect = _mock_get_side_effect
        mock_post.side_effect = _mock_post_non_ptz

        cameras = _fetch_cameras_from_site('192.168.10.1', 'test_api_key')

        self.assertEqual(cameras[0]['camera_id'], 'cam_back')
        self.assertEqual(cameras[1]['camera_id'], 'cam_front')

    @override_settings(
        **protect_settings([{'host': '192.168.10.1', 'api_key': 'k', 'name': 'S'}]),
    )
    @patch('cameras.protect_api.time.sleep')
    @patch('cameras.protect_api.requests.post')
    @patch('cameras.protect_api.requests.get')
    def test_rate_limited_result_cached_briefly(self, mock_get, mock_post, _):
        """A partial (rate-limited) camera list is cached with the short TTL."""
        def get_side_effect(url, **kwargs):
            if 'cam_back/rtsps-stream' in url:
                return _make_mock_response({}, status_code=429)
            return _mock_get_side_effect(url, **kwargs)

        def post_side_effect(url, **kwargs):
            if 'cam_back/rtsps-stream' in url:
                return _make_mock_response({}, status_code=429)
            return _mock_post_non_ptz(url, **kwargs)

        def raise_on_error(resp):
            import requests
            if resp.status_code >= 400:
                resp.raise_for_status.side_effect = requests.HTTPError("429")
            return resp

        mock_get.side_effect = lambda url, **kw: raise_on_error(get_side_effect(url, **kw))
        mock_post.side_effect = lambda url, **kw: raise_on_error(post_side_effect(url, **kw))

        with patch('cameras.protect_api.django_cache') as mock_cache:
            mock_cache.get.return_value = None
            sites = get_protect_cameras()

        self.assertEqual([c['name'] for c in sites[0]['cameras']], ['Front Door'])
        ttl = mock_cache.set.call_args.args[2]
        self.assertEqual(ttl, INCOMPLETE_CACHE_TTL)


# --- PTZ View tests ---

@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class PtzGotoViewTests(TestCase):
    """Tests for the ptz_goto view endpoint."""

    def setUp(self):
        self.client = Client()
        p = patch('cameras.views.get_protect_cameras', return_value=[{'name': 'Home', 'cameras': [
            {'camera_id': 'cam_ptz'}, {'camera_id': 'cam_fixed'}]}])
        p.start()
        self.addCleanup(p.stop)

    @patch('cameras.views.ptz_goto_preset')
    def test_successful_goto(self, mock_goto):
        """Returns success JSON on valid request."""
        mock_goto.return_value = True

        response = self.client.post(
            '/cameras/ptz/goto/',
            data=json.dumps({'camera_id': 'cam_ptz', 'slot': 2}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()['success'])
        mock_goto.assert_called_once_with('cam_ptz', 2)

    @patch('cameras.views.ptz_goto_preset')
    def test_failed_goto(self, mock_goto):
        """Returns success=false when goto fails."""
        mock_goto.return_value = False

        response = self.client.post(
            '/cameras/ptz/goto/',
            data=json.dumps({'camera_id': 'cam_fixed', 'slot': 0}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 200)
        self.assertFalse(response.json()['success'])

    def test_rejects_get(self):
        """GET requests are rejected (POST only)."""
        response = self.client.get('/cameras/ptz/goto/')

        self.assertEqual(response.status_code, 405)

    def test_rejects_missing_fields(self):
        """Missing camera_id or slot returns 400."""
        response = self.client.post(
            '/cameras/ptz/goto/',
            data=json.dumps({'camera_id': 'cam1'}),
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)

    def test_rejects_invalid_json(self):
        """Invalid JSON returns 400."""
        response = self.client.post(
            '/cameras/ptz/goto/',
            data='not json',
            content_type='application/json',
        )

        self.assertEqual(response.status_code, 400)


class ParseTalkbackTests(SimpleTestCase):
    def parse(self, **over):
        from .protect_api import _parse_talkback
        return _parse_talkback({'url': 'rtp://192.168.1.135:7004', 'codec': 'opus',
                                'samplingRate': 24000, **over})

    def test_usable_session(self):
        self.assertEqual(self.parse(),
                         {'url': 'rtp://192.168.1.135:7004', 'codec': 'opus', 'rate': 24000})

    def test_rejects_what_go2rtc_would_refuse(self):
        for over in ({'codec': 'aac'}, {'samplingRate': 44100}, {'url': 'rtp://8.8.8.8:7004'},
                     {'url': 'rtp://192.168.1.135:0'}, {'url': 'rtp://192.168.1.999:7004'},
                     {'url': 'udp://192.168.1.135:7004'}, {'url': None}):
            with self.subTest(over=over):
                self.assertIsNone(self.parse(**over))

    def test_rejects_non_dict(self):
        from .protect_api import _parse_talkback
        self.assertIsNone(_parse_talkback(['x']))


class GetTalkbackTests(TestCase):
    SESSION = {'url': 'rtp://192.168.1.135:7004', 'codec': 'opus', 'samplingRate': 24000}

    def setUp(self):
        django_cache.clear()
        self.addCleanup(django_cache.clear)

    def _resp(self, data):
        resp = MagicMock()
        resp.json.return_value = data
        return resp

    @patch('cameras.protect_api._request')
    def test_cached_until_refresh(self, mock_request):
        from .protect_api import _get_talkback
        mock_request.return_value = self._resp(self.SESSION)
        first = _get_talkback('nvr', 'k', 'cam1')
        self.assertEqual(_get_talkback('nvr', 'k', 'cam1'), first)
        self.assertEqual(mock_request.call_count, 1)
        _get_talkback('nvr', 'k', 'cam1', refresh=True)
        self.assertEqual(mock_request.call_count, 2)
        self.assertEqual(mock_request.call_args.args[1],
                         'https://nvr/proxy/protect/integration/v1/cameras/cam1/talkback-session')

    @patch('cameras.protect_api._request')
    def test_failures_are_not_cached(self, mock_request):
        import requests
        from .protect_api import _get_talkback
        mock_request.side_effect = requests.ConnectionError('down')
        with self.assertLogs('cameras.protect_api', level='WARNING'):
            self.assertIsNone(_get_talkback('nvr', 'k', 'cam1'))
        mock_request.side_effect = None
        mock_request.return_value = self._resp({'codec': 'aac'})
        with self.assertLogs('cameras.protect_api', level='WARNING'):
            self.assertIsNone(_get_talkback('nvr', 'k', 'cam1'))
        mock_request.return_value = self._resp(self.SESSION)
        self.assertIsNotNone(_get_talkback('nvr', 'k', 'cam1'))


class TalkPrepareViewTests(TestCase):
    URL = '/cameras/api/talk/prepare/'
    TALKBACK = {'url': 'rtp://192.168.1.135:7004', 'codec': 'opus', 'rate': 24000}
    SITES = [
        {'name': 'Home', 'cameras': [{'camera_id': 'door', 'stream_name': 'front_door'}]},
        {'name': 'Cabin', 'cameras': [{'camera_id': 'cabin', 'stream_name': 'cabin_door'}]},
    ]

    def setUp(self):
        from . import views
        views._wanted.clear()
        self.addCleanup(views._wanted.clear)
        self.wanted = views._wanted
        for target, kw in (('cameras.views.get_protect_cameras', {'return_value': self.SITES}),
                           ('cameras.views._sync_streams', {}),
                           ('cameras.views.config.go2rtc_url', {'return_value': 'http://h:11900'})):
            p = patch(target, **kw)
            setattr(self, target.rsplit('.', 1)[1], p.start())
            self.addCleanup(p.stop)

    def _camera(self, camera_id):
        cam = next(c for s in self.SITES for c in s['cameras'] if c['camera_id'] == camera_id)
        return dict(cam, has_speaker=True, talkback=self.TALKBACK)

    def test_rejects_get(self):
        self.assertEqual(self.client.get(self.URL + '?camera=door').status_code, 405)

    @patch('cameras.views.refresh_talkback')
    def test_registers_talk_stream(self, mock_refresh):
        mock_refresh.side_effect = self._camera
        response = self.client.post(self.URL + '?camera=door')
        self.assertEqual(response.status_code, 200)
        body = response.json()
        self.assertEqual(body['port'], 80)
        from .routing import redeem_talk
        self.assertEqual(redeem_talk(body['stream']), 'front_door_talk')
        source = 'talkback:rtp://192.168.1.135:7004?codec=opus&rate=24000'
        self.assertEqual(self.wanted['http://h:11900']['front_door_talk'], source)
        self._sync_streams.assert_called_once_with('http://h:11900', {'front_door_talk': source})

    @patch('cameras.views.refresh_talkback', return_value=None)
    def test_unavailable_is_404(self, mock_refresh):
        for query in ('?camera=door', ''):
            with self.subTest(query=query):
                self.assertEqual(self.client.post(self.URL + query).status_code, 404)
        self._sync_streams.assert_not_called()

    @patch('cameras.views.refresh_talkback')
    def test_guests_only_talk_to_first_site(self, mock_refresh):
        mock_refresh.side_effect = self._camera
        self.assertEqual(self.client.post(self.URL + '?camera=cabin').status_code, 404)
        mock_refresh.assert_not_called()
        self.assertEqual(self.client.post(self.URL + '?camera=door').status_code, 200)

        user = User.objects.create_user('staff', password='pw', is_staff=True)
        self.client.force_login(user)
        self.assertEqual(self.client.post(self.URL + '?camera=cabin').status_code, 200)

    @patch('cameras.views.refresh_talkback')
    def test_port_is_the_pages(self, mock_refresh):
        """The kiosk talks to the app's own port, not go2rtc's or aec's."""
        mock_refresh.side_effect = self._camera
        for host, port in (('testserver:9105', 9105), ('testserver', 80)):
            with self.subTest(host=host):
                response = self.client.post(self.URL + '?camera=door', HTTP_HOST=host)
                self.assertEqual(response.json()['port'], port)


class Go2rtcScriptTests(TestCase):
    """go2rtc's player is served by the app (browsers can't reach go2rtc)."""

    def setUp(self):
        from . import views
        views._scripts.clear()
        self.addCleanup(views._scripts.clear)

    @patch('cameras.views.config.go2rtc_url', return_value='http://127.0.0.1:11900')
    @patch('cameras.views.urllib.request.urlopen')
    def test_serves_and_caches_the_player(self, mock_open, _url):
        mock_open.return_value.__enter__.return_value.read.return_value = b'export class VideoRTC {}'
        for _ in range(2):
            resp = self.client.get('/cameras/go2rtc/video-rtc.js')
            self.assertEqual((resp.status_code, resp.content), (200, b'export class VideoRTC {}'))
            self.assertEqual(resp['Content-Type'], 'application/javascript')
        mock_open.assert_called_once_with('http://127.0.0.1:11900/video-rtc.js', timeout=5)

    @patch('cameras.views.urllib.request.urlopen')
    def test_only_the_player(self, mock_open):
        for name in ('api', 'index.html', 'stream.html'):
            self.assertEqual(self.client.get(f'/cameras/go2rtc/{name}').status_code, 404)
        mock_open.side_effect = urllib.error.URLError('down')
        self.assertEqual(self.client.get('/cameras/go2rtc/video-stream.js').status_code, 502)
        mock_open.assert_called_once()


class EchoCancelStreamTests(SimpleTestCase):
    """With the bdh-aec sidecar set, two-way cameras are heard through <stream>_aec."""
    CAM = {'stream_name': 'front_door', 'rtsp_url': 'rtsps://nvr/hi', 'rtsp_url_low': 'rtsps://nvr/lo',
           'has_mic': True, 'has_speaker': True,
           'talkback': {'url': 'rtp://10.0.0.5:7004', 'codec': 'opus', 'rate': 24000}}

    def setUp(self):
        from . import views
        self.views = views
        views._wanted.clear()
        self.addCleanup(views._wanted.clear)

    def test_off_by_default(self):
        with patch('cameras.views.config.aec_url', return_value=''):
            wanted = self.views._remember_streams('http://g', [self.CAM])
            self.assertEqual(self.views.listen_stream_name(self.CAM), 'front_door')
        self.assertNotIn('front_door_aec', wanted)

    def test_two_way_cameras_get_an_aec_stream(self):
        mic_only = dict(self.CAM, stream_name='yard', talkback=None)
        with patch('cameras.views.config.aec_url', return_value='http://h:11911'):
            wanted = self.views._remember_streams('http://g', [self.CAM, mic_only])
            self.assertEqual(self.views.listen_stream_name(self.CAM), 'front_door_aec')
            self.assertEqual(self.views.listen_stream_name(mic_only), 'yard')
        self.assertEqual(wanted['front_door_aec'],
                         ('rtsps://nvr/hi#media=video', 'http://h:11911/aec.ts?cam=front_door'))
        self.assertNotIn('yard_aec', wanted)


class RtcDiagViewTests(TestCase):
    """The WebRTC-fallback diagnostics endpoint logs what the browser saw."""

    def test_logs_stats(self):
        body = {'src': 'driveway_low', 'reason': 'timeout', 'stats': [{'framesDecoded': 0}]}
        with self.assertLogs('cameras.views', 'WARNING') as logs:
            response = self.client.post('/cameras/diag/rtc/', data=json.dumps(body),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 204)
        self.assertIn('"framesDecoded":0', logs.output[0])
        self.assertIn('"src":"driveway_low"', logs.output[0])

    def test_rejects_oversized_body(self):
        body = json.dumps({'pad': 'x' * 20000})
        response = self.client.post('/cameras/diag/rtc/', data=body, content_type='application/json')
        self.assertEqual(response.status_code, 413)

    def test_rejects_non_object(self):
        for body in ('not json', '[1, 2]'):
            response = self.client.post('/cameras/diag/rtc/', data=body, content_type='application/json')
            self.assertEqual(response.status_code, 400)

    def test_logs_stream_events(self):
        body = {'maxStreams': 2, 'events': [{'k': 'gap', 's': 'driveway_low', 'ms': 900, 'pf': 1}]}
        with self.assertLogs('cameras.views', 'WARNING') as logs:
            response = self.client.post('/cameras/diag/events/', data=json.dumps(body),
                                        content_type='application/json')
        self.assertEqual(response.status_code, 204)
        self.assertIn('Stream events', logs.output[0])
        self.assertIn('"ms":900', logs.output[0])

# --- PTZ template tests ---

@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}]),
)
class PtzTemplateTests(TestCase):
    """Tests for PTZ controls in the camera template."""

    def setUp(self):
        self.client = Client()
        clear_cache()

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_ptz_buttons_shown_for_ptz_camera(self, mock_cameras, mock_register):
        """PTZ preset buttons appear for PTZ cameras."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {
                    'name': 'Hot Tub', 'stream_name': 'hot_tub',
                    'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                    'camera_id': 'cam_ptz', 'is_ptz': True, 'ptz_presets': 4,
                },
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('class="ptz-controls"', content)
        self.assertIn('class="ptz-btn"', content)
        # 4 preset buttons labeled 1-4
        self.assertEqual(content.count('class="ptz-btn"'), 4)
        self.assertIn('data-camera-id="cam_ptz"', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_no_ptz_buttons_for_non_ptz_camera(self, mock_cameras, mock_register):
        """Non-PTZ cameras do not get PTZ buttons."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {
                    'name': 'Front Door', 'stream_name': 'front_door',
                    'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                    'camera_id': 'cam_front', 'is_ptz': False, 'ptz_presets': 0,
                },
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertNotIn('class="ptz-controls"', content)
        self.assertNotIn('class="ptz-btn"', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_ptz_js_sends_to_correct_endpoint(self, mock_cameras, mock_register):
        """PTZ JavaScript posts to the ptz_goto URL."""
        mock_cameras.return_value = [{
            'name': 'Test Site', 'host': '192.168.10.1',
            'cameras': [
                {
                    'name': 'Hot Tub', 'stream_name': 'hot_tub',
                    'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                    'camera_id': 'cam_ptz', 'is_ptz': True, 'ptz_presets': 2,
                },
            ],
        }]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('/cameras/ptz/goto/', content)
        self.assertIn('X-CSRFToken', content)


# --- Multi-site tab tests ---

@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([
        {'host': '192.168.10.1', 'api_key': 'key1', 'name': 'Sun Peaks'},
        {'host': '192.168.1.26', 'api_key': 'key2', 'name': 'Mercer Island'},
    ]),
)
class MultiSiteTabTests(TestCase):
    """Tests for tabbed multi-site camera display.

    All tests use an authenticated user since only authenticated users
    can see multiple sites (anonymous users see only the first site).
    """

    def setUp(self):
        self.client = Client()
        clear_cache()
        self.user = User.objects.create_user(
            username='testuser', password='testpass', is_staff=True,
        )
        self.client.login(username='testuser', password='testpass')

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_tabs_shown_for_multiple_sites(self, mock_cameras, mock_register):
        """Tab buttons appear when multiple sites are configured."""
        mock_cameras.return_value = [
            {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': []},
            {'name': 'Mercer Island', 'host': '192.168.1.26', 'cameras': []},
        ]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('class="site-tab', content)
        self.assertIn('Sun Peaks', content)
        self.assertIn('Mercer Island', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_no_tabs_for_single_site(self, mock_cameras, mock_register):
        """Tab buttons are hidden when only one site is configured."""
        mock_cameras.return_value = [
            {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': []},
        ]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertNotIn('class="site-tab', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_each_site_has_own_panel(self, mock_cameras, mock_register):
        """Each site gets its own camera grid panel."""
        mock_cameras.return_value = [
            {
                'name': 'Sun Peaks', 'host': '192.168.10.1',
                'cameras': [
                    {'name': 'Front Door', 'stream_name': 'front_door',
                     'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                     'camera_id': 'c1', 'is_ptz': False, 'ptz_presets': 0},
                ],
            },
            {
                'name': 'Mercer Island', 'host': '192.168.1.26',
                'cameras': [
                    {'name': 'Garage', 'stream_name': 'garage',
                     'rtsp_url': 'rtsps://y', 'rtsp_url_low': 'rtsps://y_low',
                     'camera_id': 'c2', 'is_ptz': False, 'ptz_presets': 0},
                ],
            },
        ]

        response = self.client.get("/cameras/")
        content = response.content.decode()

        self.assertIn('data-site="0"', content)
        self.assertIn('data-site="1"', content)
        self.assertIn('Front Door', content)
        self.assertIn('Garage', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_site_param_opens_that_tab(self, mock_cameras, mock_register):
        """?site= opens a tab by name or index; unknown values open the first."""
        mock_cameras.return_value = [
            {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': []},
            {'name': 'Mercer Island', 'host': '192.168.1.26', 'cameras': []},
        ]
        for query, expected in [('', 0), ('?site=mercer_island', 1), ('?site=Mercer-Island', 1),
                                ('?site=1', 1), ('?site=5', 0), ('?site=nowhere', 0)]:
            response = self.client.get("/cameras/" + query)
            self.assertEqual(response.context['active_site'], expected, query)
            self.assertIn(f'class="site-tab active" data-site="{expected}"', response.content.decode())

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_reload_reopens_the_last_picked_tab(self, mock_cameras, mock_register):
        """The hub reloads idle pages after updates; the tab picked last (cookie) reopens,
        ?site= still wins, and a stale cookie falls back to the first tab."""
        mock_cameras.return_value = [
            {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': []},
            {'name': 'Mercer Island', 'host': '192.168.1.26', 'cameras': []},
        ]
        for cookie, query, expected in [('mercer_island', '', 1), ('mercer_island', '?site=sun_peaks', 0),
                                        ('gone', '', 0)]:
            self.client.cookies['cameras_site'] = cookie
            response = self.client.get("/cameras/" + query)
            self.assertEqual(response.context['active_site'], expected, (cookie, query))
        self.assertIn('data-site-name="Mercer Island"', response.content.decode())

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_sites_passed_in_context(self, mock_cameras, mock_register):
        """Context contains sites list with correct structure."""
        mock_cameras.return_value = [
            {
                'name': 'Sun Peaks', 'host': '192.168.10.1',
                'cameras': [
                    {'name': 'Cam1', 'stream_name': 'cam1',
                     'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
                     'camera_id': 'c1', 'is_ptz': False, 'ptz_presets': 0},
                ],
            },
            {
                'name': 'Mercer Island', 'host': '192.168.1.26',
                'cameras': [],
            },
        ]

        response = self.client.get("/cameras/")

        sites = response.context['sites']
        self.assertEqual(len(sites), 2)
        self.assertEqual(sites[0]['name'], 'Sun Peaks')
        self.assertEqual(len(sites[0]['streams']), 1)
        self.assertEqual(sites[1]['name'], 'Mercer Island')
        self.assertEqual(len(sites[1]['streams']), 0)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_registers_all_sites_with_go2rtc(self, mock_cameras, mock_register):
        """Cameras from all sites are registered with go2rtc."""
        cams1 = [
            {'name': 'Cam1', 'stream_name': 'cam1',
             'rtsp_url': 'rtsps://x', 'rtsp_url_low': 'rtsps://x_low',
             'camera_id': 'c1', 'is_ptz': False, 'ptz_presets': 0},
        ]
        cams2 = [
            {'name': 'Cam2', 'stream_name': 'cam2',
             'rtsp_url': 'rtsps://y', 'rtsp_url_low': 'rtsps://y_low',
             'camera_id': 'c2', 'is_ptz': False, 'ptz_presets': 0},
        ]
        mock_cameras.return_value = [
            {'name': 'Site 1', 'host': '192.168.10.1', 'cameras': cams1},
            {'name': 'Site 2', 'host': '192.168.1.26', 'cameras': cams2},
        ]

        self.client.get("/cameras/")

        self.assertEqual(mock_register.call_count, 2)
        mock_register.assert_any_call('http://localhost:1984', cams1)
        mock_register.assert_any_call('http://localhost:1984', cams2)


# --- Selenium smoke tests ---

GO2RTC_CI_URL = 'http://localhost:1984'


def _go2rtc_available():
    """Check if go2rtc is running on localhost:1984."""
    try:
        req = urllib.request.Request(f'{GO2RTC_CI_URL}/api/streams')
        with urllib.request.urlopen(req, timeout=3):
            return True
    except Exception:
        return False


class WebsocketFront:
    """A port in front of the live server that also serves the app's websockets.

    The live server is WSGI only, but pages reach go2rtc through the app's
    websocket routes (cameras.routing). Requests asking for a websocket go to
    an in-thread websockets server running those routes (behind channels'
    auth, like the hub); everything else is piped to the live server. In this
    process, so tests' patches apply.

    ``connections_override`` is the live server's: with SQLite in memory, a
    second connection to the shared database fails with "table is locked"
    while the live server writes, so the websockets' DB calls (channels'
    session lookup, the app's checks) use the live server's connection too.
    """

    def __init__(self, backend_port, connections_override=None):
        import asyncio
        import threading
        self.backend_port = backend_port
        self.connections_override = connections_override or {}
        self.loop = asyncio.new_event_loop()
        ready = threading.Event()
        self.thread = threading.Thread(target=self._run, args=(ready,), daemon=True)
        self.thread.start()
        ready.wait(10)

    def _run(self, ready):
        import asyncio
        from websockets.asyncio.server import serve
        asyncio.set_event_loop(self.loop)

        async def start():
            from channels.db import database_sync_to_async
            from django.db import connections

            # The same executor thread every later database_sync_to_async
            # call from this loop runs in.
            def share():
                for alias, conn in self.connections_override.items():
                    connections[alias] = conn
            await database_sync_to_async(share)()
            self.ws = await serve(self._websocket, "127.0.0.1", 0)
            self.front = await asyncio.start_server(self._connection, "127.0.0.1", 0)
            self.port = self.front.sockets[0].getsockname()[1]
            self.ws_port = list(self.ws.sockets)[0].getsockname()[1]
        self.loop.run_until_complete(start())
        ready.set()
        self.loop.run_forever()

    def stop(self):
        async def close():
            self.front.close()
            self.ws.close()
        import asyncio
        asyncio.run_coroutine_threadsafe(close(), self.loop).result(10)
        self.loop.call_soon_threadsafe(self.loop.stop)
        self.thread.join(10)

    async def _connection(self, reader, writer):
        import asyncio
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, asyncio.LimitOverrunError, ConnectionError):
            writer.close()
            return
        port = self.ws_port if b"upgrade: websocket" in head.lower() else self.backend_port
        try:
            up_reader, up_writer = await asyncio.open_connection("127.0.0.1", port)
        except OSError:
            writer.close()
            return
        up_writer.write(head)

        async def pipe(src, dst):
            try:
                while data := await src.read(65536):
                    dst.write(data)
                    await dst.drain()
            except (ConnectionError, asyncio.CancelledError):
                pass
            finally:
                try:
                    dst.close()
                except Exception:
                    pass
        await asyncio.gather(pipe(reader, up_writer), pipe(up_reader, writer))

    async def _websocket(self, conn):
        """Run the matching app route as ASGI over the websockets connection."""
        import asyncio
        from channels.auth import AuthMiddlewareStack
        from channels.routing import URLRouter
        from websockets.exceptions import ConnectionClosed
        from .routing import websocket_urlpatterns

        path, _, query = conn.request.path.partition("?")
        scope = {"type": "websocket", "path": path, "raw_path": path.encode(),
                 "query_string": query.encode(), "headers": [
                     (k.lower().encode("latin1"), v.encode("latin1")) for k, v in conn.request.headers.raw_items()],
                 "subprotocols": [], "client": ("127.0.0.1", 0), "server": ("127.0.0.1", self.port)}
        inbox = asyncio.Queue()
        await inbox.put({"type": "websocket.connect"})

        async def pump():
            try:
                async for message in conn:
                    key = "text" if isinstance(message, str) else "bytes"
                    await inbox.put({"type": "websocket.receive", key: message})
            except ConnectionClosed:
                pass
            await inbox.put({"type": "websocket.disconnect", "code": conn.close_code or 1000})

        async def send(message):
            if message["type"] == "websocket.send":
                await conn.send(message["text"] if message.get("text") is not None else message["bytes"])
            elif message["type"] == "websocket.close":
                await conn.close(message.get("code", 1000))

        app = AuthMiddlewareStack(URLRouter(websocket_urlpatterns))
        pumping = asyncio.ensure_future(pump())
        try:
            await app(scope, inbox.get, send)
        except (ValueError, ConnectionClosed):  # no route / client gone
            pass
        finally:
            pumping.cancel()
            await conn.close()


class WebsocketLiveServerTestCase(StaticLiveServerTestCase):
    """The live server, with the app's websockets (see :class:`WebsocketFront`)."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        cls.ws_front = WebsocketFront(cls.server_thread.port, cls.server_thread.connections_override)
        cls.addClassCleanup(cls.ws_front.stop)

    @classproperty
    def live_server_url(cls):
        return f"http://{cls.host}:{cls.ws_front.port}"


@tag('selenium')
@override_settings(
    GO2RTC_URL=GO2RTC_CI_URL,
    **protect_settings([]),
)
class CameraStreamSeleniumTests(WebsocketLiveServerTestCase):
    """Selenium smoke tests verifying video-stream elements load via go2rtc.

    Requires go2rtc running on localhost:1984 (provided in CI by Docker
    service container).  Tests register a synthetic ffmpeg test stream
    and verify the camera page renders functional video-stream elements
    with the correct data-mode attribute.
    """

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        if not _go2rtc_available():
            raise unittest.SkipTest('go2rtc not available on localhost:1984')

        cls.driver = cls._start_driver()
        cls.driver.implicitly_wait(5)
        cls._register_test_stream()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'driver'):
            cls.driver.quit()
        super().tearDownClass()

    @staticmethod
    def _start_driver(attempts=3):
        """Start Chrome and check its renderer answers a first navigation.

        A fresh headless Chrome sometimes never answers its first page load
        ("Timed out receiving message from renderer"), which by default
        hangs for 300s and fails the test. Start another browser instead.
        """
        from selenium import webdriver
        from selenium.common.exceptions import TimeoutException
        from tests.selenium_helpers import get_chrome_options

        for attempt in range(attempts):
            driver = webdriver.Chrome(options=get_chrome_options())
            driver.set_page_load_timeout(60)
            try:
                driver.get('about:blank')
                return driver
            except TimeoutException:
                driver.quit()
                if attempt == attempts - 1:
                    raise

    @classmethod
    def _register_test_stream(cls):
        """Register a synthetic test stream in go2rtc via its API.

        Uses ffmpeg's lavfi testsrc filter with H264 encoding, which is
        built into the go2rtc Docker image. This provides a real video
        stream without needing physical cameras.
        """
        source = 'ffmpeg:virtual?video#video=h264'
        encoded = urllib.parse.quote(source, safe='')
        url = f'{GO2RTC_CI_URL}/api/streams?name=test_cam&src={encoded}'
        req = urllib.request.Request(url, method='PUT', data=b'')
        with urllib.request.urlopen(req, timeout=10):
            pass

    def _wait_for_grid_video(self, timeout=20):
        """Wait for the grid to show frames before the step under test.

        The initial page load isn't what these tests time, and a cold
        go2rtc producer on a slow CI runner can take >10s to deliver.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        WebDriverWait(self.driver, timeout).until(
            lambda d: d.execute_script("""
                const els = document.querySelectorAll('video-stream');
                for (const el of els) {
                    const v = el.querySelector('video') || el.video;
                    if (v && v.readyState >= 2) return true;
                }
                return false;
            """),
            "No grid video reached readyState >= 2",
        )

    def setUp(self):
        self.driver.get('about:blank')
        self.driver.delete_all_cookies()

    def tearDown(self):
        # Leave the camera page before Django flushes the DB: its timers keep
        # hitting the live server, and a request racing the flush on the shared
        # in-memory SQLite connection breaks it for every later test.
        self.driver.get('about:blank')
        super().tearDown()

    def test_camera_page_renders_video_streams_with_correct_mode(self):
        """video-stream elements start WebRTC-only with an MSE fallback."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for at least one video-stream element
        WebDriverWait(self.driver, 10).until(
            EC.presence_of_element_located((By.CSS_SELECTOR, 'video-stream'))
        )

        elements = self.driver.find_elements(By.CSS_SELECTOR, 'video-stream')
        self.assertGreater(len(elements), 0, "No video-stream elements found")

        for el in elements:
            mode = el.get_attribute('data-mode')
            self.assertEqual(
                mode, 'webrtc/tcp',
                f"Expected data-mode='webrtc/tcp', got '{mode}'",
            )
            self.assertEqual(el.get_attribute('data-fallback-mode'), 'mse')

    def test_reload_keeps_the_picked_site_tab(self):
        """The hub reloads idle pages after an update; the site tab picked before it stays open."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        sites = [
            {'name': 'Sun Peaks', 'host': '192.168.10.1', 'cameras': []},
            {'name': 'Mercer Island', 'host': '192.168.1.26', 'cameras': []},
        ]
        active = "return document.querySelector('.site-tab.active')?.dataset.site"
        with patch('cameras.views.get_protect_cameras', return_value=sites), \
                patch('cameras.views._register_streams_with_go2rtc'):
            self.driver.get(f'{self.live_server_url}/cameras/')
            self.assertEqual(self.driver.execute_script(active), '0')
            self.driver.find_element(By.CSS_SELECTOR, '.site-tab[data-site="1"]').click()
            WebDriverWait(self.driver, 5).until(lambda d: d.execute_script(active) == '1')
            self.driver.refresh()
            WebDriverWait(self.driver, 10).until(
                lambda d: d.execute_script("return document.readyState") == 'complete')
            self.assertEqual(self.driver.execute_script(active), '1')

    def test_video_stream_custom_element_loads(self):
        """video-stream.js loads from go2rtc and registers the custom element.

        Verifies the full chain: Django renders the page, the browser fetches
        video-stream.js from go2rtc, and the custom element class registers.
        If go2rtc is unreachable or video-stream.js fails to load, this test
        catches it.
        """
        from selenium.webdriver.support.ui import WebDriverWait

        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for the custom element to be defined (video-stream.js loaded)
        is_defined = WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )
        self.assertTrue(is_defined, "video-stream custom element not registered")

    def test_video_receives_data_within_timeout(self):
        """Video stream starts receiving data within 10 seconds.

        After the page loads, go2rtc should negotiate a connection (MSE or
        WebRTC) and begin delivering frames.  We verify by checking that the
        video element has a non-zero readyState (HAVE_METADATA or higher)
        and currentTime is advancing, which proves actual media data arrived.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for video-stream custom element to be defined
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )

        # Wait for at least one video element inside a video-stream to have
        # readyState >= 2 (HAVE_CURRENT_DATA) which means frames are arriving
        has_data = WebDriverWait(self.driver, 10).until(
            lambda d: d.execute_script("""
                const els = document.querySelectorAll('video-stream');
                for (const el of els) {
                    const v = el.querySelector('video') || el.video;
                    if (v && v.readyState >= 2) return true;
                }
                return false;
            """)
        )
        self.assertTrue(has_data, "No video element reached readyState >= 2 within 10s")

    def test_fullscreen_enter_stream_loads_quickly(self):
        """After entering fullscreen, the high-res stream connects within 8s.

        This is the key regression test for the safeDisconnect() fix.  Before
        the fix, ondisconnect() setting video.src='' caused an async video
        error that killed the new WebSocket, resulting in a 15-second delay
        before RECONNECT_TIMEOUT triggered a retry.

        The test clicks the fullscreen overlay, waits for the fullscreen API
        to engage, then verifies a new WebSocket reaches OPEN state within
        5 seconds.  We check WebSocket state rather than video.readyState
        because Chrome headless may not render video in fullscreen mode, but
        the WS connection — which is what safeDisconnect protects — is
        fully testable.  If the race condition regresses, the WS gets killed
        and won't reopen for 15s, causing this test to time out.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        import time

        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for video-stream custom element and initial stream data
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )
        self._wait_for_grid_video()

        # Find test_cam — it's registered in go2rtc by setUpClass and the
        # view falls back to the go2rtc stream list (UNIFI_PROTECT_SITES=[]).
        test_cam = self.driver.find_element(
            By.CSS_SELECTOR, 'video-stream[data-stream-name="test_cam"]'
        )
        card = test_cam.find_element(By.XPATH, '..')
        overlay = card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay')
        overlay.click()

        # Verify fullscreen engaged (or skip if browser blocks it)
        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script(
                    "return document.fullscreenElement !== null"
                )
            )
        except Exception:
            self.skipTest("Browser did not enter fullscreen")

        # The critical assertion: the fullscreen stream must connect
        # within 5 seconds.  The old bug caused the new WS to
        # be killed by the stale onclose/video-error handlers, leaving ws
        # null until RECONNECT_TIMEOUT (15s) fired.
        start = time.time()
        ws_open = WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("""
                const fs = document.fullscreenElement;
                if (!fs) return false;
                const el = fs.querySelector('video-stream');
                if (!el) return false;
                // video-rtc closes and nulls ws once WebRTC wins, so
                // either transport being up counts as connected.
                return el.pcState === WebSocket.OPEN ||
                    (!!el.ws && el.ws.readyState === WebSocket.OPEN);
            """)
        )
        elapsed = time.time() - start
        self.assertTrue(
            ws_open,
            f"Fullscreen stream did not connect within 5s "
            f"(took {elapsed:.1f}s) — safeDisconnect may be broken"
        )

    def test_admin_grid_hides_replay_media(self):
        """Admin-only replay players stay hidden until fullscreen replay.

        A stray `.cam-card video` rule once out-ranked the hide rule, laying
        two empty <video>s over each live stream; Android WebView paints a
        play-button placeholder on those, so signed-in users saw no video.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        from selenium.webdriver.support import expected_conditions as EC

        cams = [{'name': 'Test Site', 'host': '192.168.10.1', 'cameras': [
            {'name': 'Test Cam', 'stream_name': 'test_cam', 'rtsp_url': 'rtsps://x',
             'rtsp_url_low': '', 'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
        ]}]
        with patch('cameras.policy.is_admin', return_value=True), \
                patch('cameras.config.protect_sites', return_value=[{'name': 'Test Site'}]), \
                patch('cameras.views.get_protect_cameras', return_value=cams), \
                patch('cameras.views._register_streams_with_go2rtc'), \
                patch('cameras.views._cached_poster', return_value=None):
            self.driver.get(f'{self.live_server_url}/cameras/')
            WebDriverWait(self.driver, 10).until(
                EC.presence_of_element_located((By.CSS_SELECTOR, '.cam-card video.cam-replay'))
            )
            shown = self.driver.execute_script("""
                return [...document.querySelectorAll('.cam-card video, .cam-card img')]
                    .filter(el => !el.closest('video-stream') && !el.matches('.cam-poster'))
                    .filter(el => el.getClientRects().length > 0)
                    .map(el => el.outerHTML.slice(0, 80));
            """)
        self.assertEqual(shown, [])

    FAKE_BRIDGE = """
        window.__talkCalls = [];
        window.BDHDisplay = {
            info() { return '{}'; },
            reload() {},
            talkStart(stream, port) { window.__talkCalls.push(['start', stream, port]); return 's1'; },
            talkStop() { window.__talkCalls.push(['stop']); },
        };
    """

    def _patch_talk_cams(self, bridge=True, mic_only=False):
        """One two-way (or mic-only) camera door1 on test_cam."""
        talkback = {'url': 'rtp://192.168.1.135:7004', 'codec': 'opus', 'rate': 24000}
        cam = {'name': 'Front Door', 'stream_name': 'test_cam', 'rtsp_url': 'rtsps://x',
               'rtsp_url_low': '', 'camera_id': 'door1', 'is_ptz': False, 'ptz_presets': 0,
               'has_mic': True, 'has_speaker': not mic_only,
               'talkback': None if mic_only else talkback}
        cams = [{'name': 'Test Site', 'host': '192.168.10.1', 'cameras': [cam]}]
        for p in (
            patch('cameras.config.protect_sites', return_value=[{'name': 'Test Site'}]),
            patch('cameras.views.get_protect_cameras', return_value=cams),
            patch('cameras.views._register_streams_with_go2rtc'),
            patch('cameras.views._cached_poster', return_value=None),
            patch('cameras.views.refresh_talkback', return_value=dict(cam, talkback=talkback)),
            patch('cameras.views._sync_streams'),
            patch('cameras.routing.issue_talk', return_value='tk1'),
        ):
            p.start()
            self.addCleanup(p.stop)
        if bridge:
            script = self.driver.execute_cdp_cmd(
                'Page.addScriptToEvaluateOnNewDocument', {'source': self.FAKE_BRIDGE})
            self.addCleanup(self.driver.execute_cdp_cmd,
                            'Page.removeScriptToEvaluateOnNewDocument', script)

    def _open_talk_page(self, bridge=True, mic_only=False):
        """Camera page in fullscreen on the door1 camera."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self._patch_talk_cams(bridge, mic_only)
        self.driver.get(f'{self.live_server_url}/cameras/')
        card = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, '.cam-card[data-camera-id="door1"]'))
        card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay').click()
        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script("return document.fullscreenElement === arguments[0]", card))
        except Exception:
            self.skipTest('Browser did not enter fullscreen')
        return card

    def _page_port(self):
        """Talk goes to the page's own port (the app relays it to go2rtc)."""
        return urllib.parse.urlsplit(self.live_server_url).port

    def _shown(self, card, selector):
        return self.driver.execute_script(
            "const el = arguments[0].querySelector(arguments[1]);"
            "return !!el && el.getClientRects().length > 0;", card, selector)

    def test_talk_button_needs_kiosk_bridge(self):
        """A plain browser gets sound but no talk button (no native mic path)."""
        card = self._open_talk_page(bridge=False)
        self.assertFalse(self._shown(card, '.cam-talk'))
        self.assertTrue(self._shown(card, '.cam-spk'))

    def test_talk_button_needs_speaker(self):
        card = self._open_talk_page(mic_only=True)
        self.assertFalse(self._shown(card, '.cam-talk'))
        self.assertTrue(self._shown(card, '.cam-spk'))

    def test_talk_tap_starts_native_talk_and_unmutes(self):
        """Talk opens muted, a tap unmutes and starts the bridge; native
        callbacks drive the UI, and leaving fullscreen stops talking."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        card = self._open_talk_page()
        spk = card.find_element(By.CSS_SELECTOR, '.cam-spk')
        self.assertEqual(spk.get_attribute('aria-pressed'), 'false',
                         'two-way cameras must open muted')
        self.assertTrue(self._shown(card, '.cam-talk'))

        card.find_element(By.CSS_SELECTOR, '.cam-talk').click()
        WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("return window.__talkCalls.length > 0"))
        self.assertEqual(self.driver.execute_script("return window.__talkCalls"),
                         [['start', 'tk1', self._page_port()]])
        self.assertEqual(spk.get_attribute('aria-pressed'), 'true')
        self.assertEqual(card.get_attribute('data-talk'), 'connecting')

        self.driver.execute_script("bdhTalk.state('talking', '', 's1'); bdhTalk.level(-10)")
        self.assertEqual(card.get_attribute('data-talk'), 'talking')
        self.assertTrue(self._shown(card, '.cam-talk-pill'))
        self.assertTrue(self._shown(card, '.cam-talk-ring'))
        # A late callback from an older session must not end this one
        self.driver.execute_script("bdhTalk.state('stopped', '', 'old')")
        self.assertEqual(card.get_attribute('data-talk'), 'talking')

        self.driver.execute_script("document.exitFullscreen()")
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script("return window.__talkCalls.length === 2"))
        self.assertEqual(self.driver.execute_script("return window.__talkCalls[1]"), ['stop'])
        self.assertIsNone(card.get_attribute('data-talk'))

    def test_talk_error_shows_generic_toast(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        card = self._open_talk_page()
        card.find_element(By.CSS_SELECTOR, '.cam-talk').click()
        WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("return window.__talkCalls.length > 0"))
        self.driver.execute_script("bdhTalk.state('error', 'permission denied', 's1')")
        toast = card.find_element(By.CSS_SELECTOR, '.cam-talk-toast')
        self.assertTrue(self._shown(card, '.cam-talk-toast.show'))
        self.assertEqual(toast.text, "Can't access the microphone")
        self.assertIsNone(card.get_attribute('data-talk'))
        self.assertEqual(self.driver.execute_script("return window.__talkCalls[1]"), ['stop'])

    def test_talk_prepare_failure_shows_toast(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        card = self._open_talk_page()
        with patch('cameras.views.refresh_talkback', return_value=None):
            card.find_element(By.CSS_SELECTOR, '.cam-talk').click()
            WebDriverWait(self.driver, 5).until(
                lambda d: d.execute_script(
                    "return !!document.querySelector('.cam-talk-toast.show')"))
        calls = self.driver.execute_script("return window.__talkCalls")
        self.assertNotIn('start', [c[0] for c in calls])
        self.assertIsNone(card.get_attribute('data-talk'))

    def _open_unlock_page(self):
        """Admin camera page in fullscreen on door1, mapped to a door; returns (card, unlock mock)."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        cam = {'name': 'Front Door', 'stream_name': 'test_cam', 'rtsp_url': 'rtsps://x',
               'rtsp_url_low': '', 'camera_id': 'door1', 'is_ptz': False, 'ptz_presets': 0}
        cams = [{'id': '1', 'name': 'Test Site', 'host': '192.168.10.1', 'cameras': [cam]}]
        site = {'id': '1', 'name': 'Test Site', 'host': '192.168.10.1', 'access_host': '192.168.10.1',
                'access_token': 't', 'access_pin': 'ab' * 32,
                'door_map': {'door1': {'id': 'd1', 'name': 'Front Door'}}}
        patches = (
            patch('cameras.policy.is_admin', return_value=True),
            patch('cameras.config.protect_sites', return_value=[site]),
            patch('cameras.views.get_protect_cameras', return_value=cams),
            patch('cameras.views._register_streams_with_go2rtc'),
            patch('cameras.views._cached_poster', return_value=None),
            patch('cameras.views._notify_unlock'),
            patch('cameras.views.access_api.unlock', return_value=True),
        )
        for p in patches:
            mock = p.start()
            self.addCleanup(p.stop)
        django_cache.clear()
        self.driver.get(f'{self.live_server_url}/cameras/')
        card = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, '.cam-card[data-camera-id="door1"]'))
        card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay').click()
        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script("return document.fullscreenElement === arguments[0]", card))
        except Exception:
            self.skipTest('Browser did not enter fullscreen')
        WebDriverWait(self.driver, 3).until(lambda d: self._shown(card, '.cam-unlock'))
        return card, mock

    def test_unlock_needs_a_full_hold(self):
        """Letting go before 3 s snaps the ring back and sends nothing; a full
        hold posts exactly once."""
        import time as _time
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        card, unlock = self._open_unlock_page()
        btn = card.find_element(By.CSS_SELECTOR, '.cam-unlock')
        wrap = card.find_element(By.CSS_SELECTOR, '.cam-unlock-wrap')
        ActionChains(self.driver).click_and_hold(btn).perform()
        WebDriverWait(self.driver, 2).until(lambda d: wrap.get_attribute('data-unlock') == 'holding')
        _time.sleep(1)
        ActionChains(self.driver).release().perform()
        self.assertIsNone(wrap.get_attribute('data-unlock'))
        _time.sleep(2.5)  # past when the early hold would have fired
        unlock.assert_not_called()

        start = _time.monotonic()
        ActionChains(self.driver).click_and_hold(btn).perform()
        WebDriverWait(self.driver, 8).until(lambda d: wrap.get_attribute('data-unlock') == 'ok')
        self.assertGreaterEqual(_time.monotonic() - start, 2.9)
        ActionChains(self.driver).release().perform()
        self.assertIn('Front Door unlocked', wrap.text)
        unlock.assert_called_once()
        self.assertEqual(unlock.call_args.args[2], 'd1')
        # Still holding past the result, or releasing, never sends another.
        _time.sleep(0.5)
        unlock.assert_called_once()

    def test_unlock_cancels_when_the_pointer_slides_off(self):
        import time as _time
        from selenium.webdriver.common.action_chains import ActionChains
        from selenium.webdriver.common.by import By

        card, unlock = self._open_unlock_page()
        btn = card.find_element(By.CSS_SELECTOR, '.cam-unlock')
        wrap = card.find_element(By.CSS_SELECTOR, '.cam-unlock-wrap')
        ActionChains(self.driver).click_and_hold(btn).pause(0.5).move_by_offset(200, -200).perform()
        self.assertIsNone(wrap.get_attribute('data-unlock'))
        _time.sleep(3)
        ActionChains(self.driver).release().perform()
        unlock.assert_not_called()

    def test_popup_in_iframe_talks_and_hears_kiosk_callbacks(self):
        """The doorbell pop-up (an iframe in the hub) opens with sound on and
        the mic off, and talk state reaches it.

        The kiosk can only call into its top frame, so it posts each callback
        into nested frames; without the page's message listener the button
        stayed stuck "connecting" with no level meter.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self._patch_talk_cams()
        self.driver.get(f'{self.live_server_url}/cameras/nope/')
        self.driver.execute_script("""
            document.body.innerHTML = '<iframe allow="autoplay; fullscreen" '
                + 'style="width:1100px;height:620px" src="/cameras/popup/door1/"></iframe>';
        """)
        frame = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.TAG_NAME, 'iframe'))
        self.driver.switch_to.frame(frame)
        self.addCleanup(self.driver.switch_to.default_content)
        card = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, '.cam-card[data-camera-id="door1"]'))
        self.assertEqual(card.find_element(By.CSS_SELECTOR, '.cam-spk').get_attribute('aria-pressed'),
                         'true', 'the pop-up opens with sound on')
        self.assertTrue(self._shown(card, '.cam-talk'))
        self.assertIsNone(card.get_attribute('data-talk'), 'the mic must start off')
        # Bottom-centre, not over the middle of the video
        box = self.driver.execute_script(
            "const r = document.querySelector('.cam-talk').getBoundingClientRect();"
            "return [r.left + r.width / 2, r.bottom, innerWidth, innerHeight];")
        self.assertAlmostEqual(box[0], box[2] / 2, delta=2)
        self.assertGreater(box[1], box[3] - 60)

        card.find_element(By.CSS_SELECTOR, '.cam-talk').click()
        WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("return window.__talkCalls.length > 0"))
        self.assertEqual(self.driver.execute_script("return window.__talkCalls"),
                         [['start', 'tk1', self._page_port()]])

        # What MainActivity.talkJs does from the top frame
        self.driver.switch_to.default_content()
        self.driver.execute_script("""
            for (const [fn, args] of [['state', ['talking', '', 's1']], ['level', [-10]]])
                frames[0].postMessage({bdhTalk: {fn, args}}, '*');
        """)
        self.driver.switch_to.frame(frame)
        WebDriverWait(self.driver, 3).until(lambda d: card.get_attribute('data-talk') == 'talking')
        # The level message is async too; check the target transform, not the
        # mid-transition computed one.
        try:
            WebDriverWait(self.driver, 3).until(lambda d: d.execute_script(
                "return document.querySelector('.talk-bars span').style.transform"))
        except Exception:
            pass
        bar = self.driver.execute_script(
            "return document.querySelector('.talk-bars span').style.transform")
        self.assertNotIn(bar, ('', 'scaleY(0.15)'), 'level meter did not move')
        # Only the kiosk's top frame may drive it
        self.driver.execute_script(
            "window.postMessage({bdhTalk: {fn: 'state', args: ['stopped', '', 's1']}}, '*')")
        time.sleep(0.2)
        self.assertEqual(card.get_attribute('data-talk'), 'talking')

    def test_popup_holds_the_hub_alert_while_talking_and_hangs_up(self):
        """The hub closes the alert when its countdown ends; the pop-up must
        hold it open while the mic is on, release it on mute, and Hang up must
        stop the mic and ask the hub to close it."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self._patch_talk_cams()
        self.driver.get(f'{self.live_server_url}/cameras/nope/')
        self.driver.execute_script("""
            window.__alert = [];
            addEventListener('message', e => { const d = e.data; if (d && d.bdh === 1 && d.type === 'alert') __alert.push(d.cmd); });
            document.body.innerHTML = '<iframe style="width:1100px;height:620px" '
                + 'src="/cameras/popup/door1/"></iframe>';
        """)
        frame = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.TAG_NAME, 'iframe'))
        self.driver.switch_to.frame(frame)
        self.addCleanup(self.driver.switch_to.default_content)
        card = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, '.cam-card.can-talk'))
        self.assertFalse(self._shown(card, '.cam-hangup'), 'no Hang up before talking')

        def hub_heard(last):
            # postMessage is async: wait until the parent has heard `last`.
            self.driver.switch_to.default_content()
            try:
                WebDriverWait(self.driver, 3).until(
                    lambda d: d.execute_script("return __alert[__alert.length-1]") == last)
            except Exception:
                pass
            got = self.driver.execute_script("return __alert.splice(0)")
            self.driver.switch_to.frame(frame)
            return got

        talk = card.find_element(By.CSS_SELECTOR, '.cam-talk')
        talk.click()
        WebDriverWait(self.driver, 3).until(lambda d: card.get_attribute('data-talk'))
        self.assertTrue(self._shown(card, '.cam-hangup'))
        self.assertEqual(set(hub_heard('hold')), {'hold'})
        talk.click()  # mute
        WebDriverWait(self.driver, 3).until(lambda d: not card.get_attribute('data-talk'))
        self.assertEqual(hub_heard('release')[-1:], ['release'])

        talk.click()
        WebDriverWait(self.driver, 3).until(lambda d: card.get_attribute('data-talk'))
        card.find_element(By.CSS_SELECTOR, '.cam-hangup').click()
        self.assertIsNone(card.get_attribute('data-talk'))
        self.assertEqual(self.driver.execute_script("return window.__talkCalls")[-1][0], 'stop')
        self.assertEqual(hub_heard('hangup')[-1:], ['hangup'])

    def test_popup_without_kiosk_has_sound_but_no_talk(self):
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self._patch_talk_cams(bridge=False)
        self.driver.get(f'{self.live_server_url}/cameras/popup/door1/')
        card = WebDriverWait(self.driver, 10).until(
            lambda d: d.find_element(By.CSS_SELECTOR, '.cam-card'))
        self.assertTrue(self._shown(card, '.cam-spk'))
        self.assertFalse(self._shown(card, '.cam-talk'))

    def test_mic_camera_sound_choice_is_remembered(self):
        """Mic-only cameras open with sound on; muting sticks for that camera."""
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        self.driver.get(f'{self.live_server_url}/cameras/')
        self.driver.execute_script("localStorage.clear()")
        card = self._open_talk_page(mic_only=True)
        spk = card.find_element(By.CSS_SELECTOR, '.cam-spk')
        self.assertEqual(spk.get_attribute('aria-pressed'), 'true')
        spk.click()
        self.assertEqual(spk.get_attribute('aria-pressed'), 'false')

        self.driver.execute_script("document.exitFullscreen()")
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script("return document.fullscreenElement === null"))
        card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay').click()
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script("return document.fullscreenElement === arguments[0]", card))
        self.assertEqual(spk.get_attribute('aria-pressed'), 'false')

    def test_slow_fullscreen_transition_never_shows_grid(self):
        """Tapping a tile covers the page with it before fullscreen lands.

        Android WebView enters fullscreen in steps (resize, then restyle),
        so the grid and its tiles being stopped used to flash on screen.
        """
        from selenium.webdriver.common.by import By

        self.driver.get(f'{self.live_server_url}/cameras/')
        self._wait_for_grid_video()
        # Hold requestFullscreen pending so the transition state is observable
        self.driver.execute_script("""
            Element.prototype.requestFullscreen = function () {
                return new Promise((res, rej) => { window.__fsReject = rej; });
            };
        """)
        card = self.driver.find_element(
            By.CSS_SELECTOR, 'video-stream[data-stream-name="test_cam"]'
        ).find_element(By.XPATH, '..')
        card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay').click()

        covered = self.driver.execute_script("""
            const card = arguments[0];
            const r = card.getBoundingClientRect();
            const pts = [[5, 5], [innerWidth - 5, 5], [5, innerHeight - 5],
                         [innerWidth - 5, innerHeight - 5], [innerWidth / 2, innerHeight / 2]];
            return r.width >= innerWidth - 1 && r.height >= innerHeight - 1 &&
                pts.every(([x, y]) => card.contains(document.elementFromPoint(x, y)));
        """, card)
        self.assertTrue(covered, 'tapped tile must cover the page while fullscreen is pending')

        self.driver.execute_script("window.__fsReject(new Error('denied'))")
        self.driver.execute_script("return new Promise(r => setTimeout(r, 50))")
        self.assertFalse(
            self.driver.execute_script(
                "return !!document.querySelector('.cam-card.fs-pending')"),
            'cover must clear if fullscreen is refused',
        )

    def test_fullscreen_close_after_next_exits_completely(self):
        """Close after stepping to the next camera leaves fullscreen entirely.

        Each prev/next press calls requestFullscreen on another card, which
        stacks fullscreen elements; a single exitFullscreen only pops back to
        the previous camera.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        # go2rtc names the exec RTSP path after a hash of the ffmpeg command,
        # so reusing test_cam's exact source makes both producers publish to
        # the same path at once. The loser breaks test_cam's producer until
        # go2rtc's 30s exec timeout, failing the next selenium test. A
        # different size gives test_cam2 its own command and path.
        source = urllib.parse.quote(
            'ffmpeg:virtual?video&size=720#video=h264', safe='',
        )
        req = urllib.request.Request(
            f'{GO2RTC_CI_URL}/api/streams?name=test_cam2&src={source}',
            method='PUT', data=b'',
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        self.addCleanup(lambda: urllib.request.urlopen(urllib.request.Request(
            f'{GO2RTC_CI_URL}/api/streams?src=test_cam2', method='DELETE',
        ), timeout=10))
        # Cleanups run LIFO: leave the page first so its test_cam2 consumers
        # close cleanly instead of being torn out from under the next test.
        self.addCleanup(self.driver.get, 'about:blank')

        self.driver.get(f'{self.live_server_url}/cameras/')
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )
        self.assertGreaterEqual(
            len(self.driver.find_elements(By.CSS_SELECTOR, '.cam-card')), 2,
        )

        first = self.driver.find_elements(By.CSS_SELECTOR, '.cam-card')[0]
        first.find_element(By.CSS_SELECTOR, '.cam-touch-overlay').click()
        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script(
                    "return document.fullscreenElement !== null"
                )
            )
        except Exception:
            self.skipTest("Browser did not enter fullscreen")

        first.find_element(By.CSS_SELECTOR, '.cam-fs-next').click()
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script(
                "const fs = document.fullscreenElement;"
                "return fs !== null && fs !== document.querySelector('.cam-card');"
            )
        )

        self.driver.execute_script(
            "document.fullscreenElement.querySelector('.cam-fs-close').click()"
        )
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script(
                "return document.fullscreenElement === null"
            )
        )

    def test_fullscreen_exit_streams_recover(self):
        """After exiting fullscreen, grid streams recover within 10 seconds.

        Verifies that stopStreams + initStreams via safeDisconnect properly
        tears down the fullscreen connection and re-establishes low-quality
        grid streams without getting stuck in a reconnect loop.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        import time

        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for initial streams
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )
        self._wait_for_grid_video()

        # Enter fullscreen on the test_cam card
        test_cam = self.driver.find_element(
            By.CSS_SELECTOR, 'video-stream[data-stream-name="test_cam"]'
        )
        card = test_cam.find_element(By.XPATH, '..')
        overlay = card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay')
        overlay.click()

        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script(
                    "return document.fullscreenElement !== null"
                )
            )
        except Exception:
            self.skipTest("Browser did not enter fullscreen")

        # Wait for fullscreen stream's WebSocket to open
        WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("""
                const fs = document.fullscreenElement;
                if (!fs) return false;
                const el = fs.querySelector('video-stream');
                if (!el) return false;
                // video-rtc closes and nulls ws once WebRTC wins, so
                // either transport being up counts as connected.
                return el.pcState === WebSocket.OPEN ||
                    (!!el.ws && el.ws.readyState === WebSocket.OPEN);
            """)
        )

        # Exit fullscreen
        self.driver.execute_script("document.exitFullscreen()")

        # Wait for fullscreen to exit
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script(
                "return document.fullscreenElement === null"
            )
        )
        WebDriverWait(self.driver, 2).until(
            lambda d: d.execute_script(
                "return !document.querySelector('.cam-card.fs-pending')"
            ),
            'fullscreen cover left on a tile after exit',
        )

        # Grid streams should recover — at least one video element should
        # reach readyState >= 2 within 10 seconds
        start = time.time()
        has_data = WebDriverWait(self.driver, 10).until(
            lambda d: d.execute_script("""
                const els = document.querySelectorAll('video-stream');
                for (const el of els) {
                    const v = el.querySelector('video') || el.video;
                    if (v && v.readyState >= 2) return true;
                }
                return false;
            """)
        )
        elapsed = time.time() - start
        self.assertTrue(
            has_data,
            f"Grid streams did not recover within 10s after fullscreen exit "
            f"(took {elapsed:.1f}s)"
        )

    def test_no_websocket_errors_during_fullscreen_cycle(self):
        """No WebSocket connection failures occur during fullscreen enter/exit.

        Monitors the browser console for WebSocket errors that would indicate
        the race condition where onclose() kills the new WebSocket or the
        video error handler closes it prematurely.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait
        import time

        # Enable browser logging
        self.driver.get(f'{self.live_server_url}/cameras/')

        # Wait for initial stream
        WebDriverWait(self.driver, 15).until(
            lambda d: d.execute_script(
                "return customElements.get('video-stream') !== undefined"
            )
        )
        self._wait_for_grid_video()

        # Inject WebSocket error tracker before fullscreen
        self.driver.execute_script("""
            window.__wsErrors = [];
            const OrigWebSocket = window.WebSocket;
            window.WebSocket = function(url, protocols) {
                const ws = protocols
                    ? new OrigWebSocket(url, protocols)
                    : new OrigWebSocket(url);
                ws.addEventListener('error', () => {
                    window.__wsErrors.push({
                        url: url,
                        time: Date.now(),
                        readyState: ws.readyState
                    });
                });
                return ws;
            };
            window.WebSocket.prototype = OrigWebSocket.prototype;
            window.WebSocket.CONNECTING = OrigWebSocket.CONNECTING;
            window.WebSocket.OPEN = OrigWebSocket.OPEN;
            window.WebSocket.CLOSING = OrigWebSocket.CLOSING;
            window.WebSocket.CLOSED = OrigWebSocket.CLOSED;
        """)

        # Enter fullscreen on the test_cam card
        test_cam = self.driver.find_element(
            By.CSS_SELECTOR, 'video-stream[data-stream-name="test_cam"]'
        )
        card = test_cam.find_element(By.XPATH, '..')
        overlay = card.find_element(By.CSS_SELECTOR, '.cam-touch-overlay')
        overlay.click()

        try:
            WebDriverWait(self.driver, 3).until(
                lambda d: d.execute_script(
                    "return document.fullscreenElement !== null"
                )
            )
        except Exception:
            self.skipTest("Browser did not enter fullscreen")

        # Wait for fullscreen stream's WebSocket to open
        WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("""
                const fs = document.fullscreenElement;
                if (!fs) return false;
                const el = fs.querySelector('video-stream');
                if (!el) return false;
                // video-rtc closes and nulls ws once WebRTC wins, so
                // either transport being up counts as connected.
                return el.pcState === WebSocket.OPEN ||
                    (!!el.ws && el.ws.readyState === WebSocket.OPEN);
            """)
        )

        # Exit fullscreen
        self.driver.execute_script("document.exitFullscreen()")
        WebDriverWait(self.driver, 3).until(
            lambda d: d.execute_script(
                "return document.fullscreenElement === null"
            )
        )

        # Wait for grid to recover
        WebDriverWait(self.driver, 10).until(
            lambda d: d.execute_script("""
                const els = document.querySelectorAll('video-stream');
                for (const el of els) {
                    const v = el.querySelector('video') || el.video;
                    if (v && v.readyState >= 2) return true;
                }
                return false;
            """)
        )

        # Give a brief moment for any deferred errors to fire
        time.sleep(1)

        # Check for WebSocket errors
        ws_errors = self.driver.execute_script("return window.__wsErrors || []")
        self.assertEqual(
            len(ws_errors), 0,
            f"WebSocket errors occurred during fullscreen cycle: {ws_errors}"
        )


class HomepageStatusTests(TestCase):
    @patch("cameras.homepage.get_protect_cameras")
    def test_counts_all_sites_only_for_owner(self, mock_get):
        from .homepage import status
        mock_get.return_value = [
            {"name": "Sun Peaks", "cameras": [{}] * 9},
            {"name": "Mercer Island", "cameras": [{}] * 7},
        ]
        self.assertEqual(status().text, "9 cameras")
        self.assertEqual(status(mode="dashboard").text, "9 cameras")
        self.assertEqual(status(mode="owner").text, "16 cameras")

    @patch("cameras.homepage.get_protect_cameras")
    def test_no_sites(self, mock_get):
        from .homepage import status
        mock_get.return_value = []
        self.assertIsNone(status())


# --- Protect events (private API) ---

import asyncio  # noqa: E402
import os  # noqa: E402
import tempfile  # noqa: E402
import time  # noqa: E402
from datetime import datetime, timedelta, timezone as dt_tz  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from . import events as ev_mod  # noqa: E402
from . import frames as frames_mod  # noqa: E402
from .models import ProtectEvent  # noqa: E402


def _body(resp):
    async def drain():
        return b"".join([chunk async for chunk in resp.streaming_content])
    return asyncio.run(drain())


def _ev(event_id, kinds, minutes_ago=5, camera_id="cam1", thumb=None):
    start = dj_tz.now() - timedelta(minutes=minutes_ago)
    return ProtectEvent.objects.create(
        event_id=event_id, site="Test Site", host="192.168.10.1", camera_id=camera_id,
        camera_name="Front Door", kinds=kinds, start=start, end=start + timedelta(seconds=20),
        score=80, thumbnail=thumb,
    )


class EventKindTests(TestCase):
    def test_kinds_for(self):
        self.assertEqual(ev_mod.kinds_for("ring", []), ["doorbell"])
        self.assertEqual(ev_mod.kinds_for("smartDetectZone", ["person", "face", "vehicle"]), ["person", "vehicle"])
        self.assertEqual(ev_mod.kinds_for("smartDetectLine", ["licensePlate", "pet"]), ["vehicle", "animal"])
        self.assertEqual(ev_mod.kinds_for("motion", []), ["motion"])
        # Motion carrying smart types duplicates its smartDetect event.
        self.assertEqual(ev_mod.kinds_for("motion", ["person"]), [])
        self.assertEqual(ev_mod.kinds_for("smartDetectZone", ["speak"]), [])
        self.assertEqual(ev_mod.kinds_for("sensorOpened", []), [])

    def test_normalize_raw(self):
        row = ev_mod.normalize_raw({
            "id": "e1", "type": "smartDetectZone", "smartDetectTypes": ["person"],
            "camera": "cam1", "start": 1_700_000_000_000, "end": None, "score": 91,
        })
        self.assertEqual(row["kinds"], ["person"])
        self.assertEqual(row["start"], datetime.fromtimestamp(1_700_000_000, tz=dt_tz.utc))
        self.assertIsNone(row["end"])
        self.assertIsNone(ev_mod.normalize_raw({"id": "e2", "type": "motion", "smartDetectTypes": ["person"],
                                                "camera": "cam1", "start": 1}))
        self.assertIsNone(ev_mod.normalize_raw({"id": "e3", "type": "ring", "camera": None, "start": 1}))


class EventStorageTests(TestCase):
    def setUp(self):
        # _db() closes connections, which would break TestCase's transaction.
        p = patch.object(ev_mod, "close_old_connections")
        p.start()
        self.addCleanup(p.stop)
        self.addCleanup(self._settle)

    @staticmethod
    def _settle():
        """Let pending change announcements fire so they can't leak into the next test."""
        import threading
        for t in threading.enumerate():
            if isinstance(t, threading.Timer):
                t.join()
        from bdh_sdk import events as bus
        bus.flush()

    def test_save_upserts_and_reports_missing_thumbs(self):
        start = dj_tz.now()
        row = {"event_id": "e1", "camera_id": "cam1", "kinds": ["person"], "start": start, "end": None, "score": 50}
        self.assertEqual(ev_mod._save("S", "h", {"cam1": "Front"}, [row]), [])  # ongoing: no thumb yet
        row["end"] = start + timedelta(seconds=10)
        self.assertEqual(ev_mod._save("S", "h", {"cam1": "Front"}, [row]), [("e1", ["person"])])
        self.assertEqual(ProtectEvent.objects.count(), 1)
        self.assertEqual(ProtectEvent.objects.get().camera_name, "Front")
        ev_mod._save_thumb("e1", b"jpg")
        self.assertEqual(ev_mod._save("S", "h", {}, [row]), [])

    def test_changes_are_pushed_once_per_burst(self):
        from bdh_sdk import events as bus
        self._settle()
        heard = []
        sub = bus.subscribe("cameras/#", heard.append)
        self.addCleanup(sub.cancel)
        row = {"event_id": "e1", "camera_id": "cam1", "kinds": ["person"], "start": dj_tz.now(), "end": None, "score": 50}
        with patch.object(ev_mod, "CHANGED_DELAY", 0.05):
            ev_mod._save("S", "h", {"cam1": "Front"}, [row])
            ev_mod._save("S", "h", {"cam1": "Front"}, [row])
            ev_mod._save_thumb("e1", b"jpg")
            ev_mod._save_thumb("missing", b"jpg")  # no row: nothing changed
            ev_mod._save("S", "h", {}, [])  # nothing to save
            self._settle()
            self.assertEqual([e.topic for e in heard], ["cameras/events/changed"])
            ev_mod._save_thumb("e1", b"jpg2")
            self._settle()
        self.assertEqual(len(heard), 2)

    def test_only_admins_hear_pushes(self):
        from bdh_sdk import push
        with patch("cameras.policy.is_admin", return_value=False):
            self.assertEqual(push.patterns_for(MagicMock(), "cameras"), [])
        with patch("cameras.policy.is_admin", return_value=True):
            self.assertEqual(push.patterns_for(MagicMock(), "cameras"), ["cameras/#"])

    def test_prune_drops_older_than_24h(self):
        _ev("old", ["person"], minutes_ago=25 * 60)
        _ev("new", ["person"], minutes_ago=60)
        self.assertEqual(ev_mod.prune(), 1)
        self.assertEqual(list(ProtectEvent.objects.values_list("event_id", flat=True)), ["new"])


class EventWorkerStartTests(TestCase):
    def test_should_start(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("BDH_CAMERAS_EVENTS", None)
            env.pop("RUN_MAIN", None)
            self.assertTrue(ev_mod.should_start(["/app/.venv/bin/daphne", "x.asgi:application"]))
            self.assertFalse(ev_mod.should_start(["manage.py", "test"]))
            self.assertFalse(ev_mod.should_start(["manage.py", "migrate"]))
            self.assertFalse(ev_mod.should_start(["manage.py", "runserver"]))
            self.assertTrue(ev_mod.should_start(["manage.py", "runserver", "--noreload"]))
            env["RUN_MAIN"] = "true"
            self.assertTrue(ev_mod.should_start(["manage.py", "runserver"]))
            env["BDH_CAMERAS_EVENTS"] = "off"
            self.assertFalse(ev_mod.should_start(["daphne"]))

    @override_settings(**protect_settings([
        {"host": "h1", "api_key": "k", "name": "A", "username": "u", "password": "p"},
        {"host": "h2", "api_key": "k", "name": "B"},
    ]))
    def test_start_only_sites_with_accounts(self):
        with patch.object(ev_mod, "_workers", {}), patch.object(ev_mod, "_started", False), \
                patch.object(ev_mod.SiteWorker, "start") as mock_start:
            ev_mod.start()
            ev_mod.start()
            self.assertEqual(list(ev_mod._workers), ["h1"])
            mock_start.assert_called_once()


class RingAlertTests(TestCase):
    def setUp(self):
        self.worker = ev_mod.SiteWorker({"name": "S", "host": "h", "username": "u", "password": "p"})
        self.worker.names = {"cam1": "Doorbell"}

        async def fake_in_db(fn, *args):
            return [] if fn is ev_mod._save else fn(*args)
        self.worker._in_db = fake_in_db

    def _handle(self, kinds, seconds_ago=2, wanted=("doorbell",), event_id="r1", admins_only=False):
        import asyncio
        row = {"event_id": event_id, "camera_id": "cam1", "kinds": kinds,
               "start": datetime.now(dt_tz.utc) - timedelta(seconds=seconds_ago), "end": None, "score": 0}
        pushed = []

        async def fake_push(*args):
            pushed.append(args)
        with patch.object(ev_mod, "_push_popup", fake_push), \
                patch("cameras.popups.camera_kinds", return_value=list(wanted)), \
                patch("cameras.popups.camera_sound", side_effect=lambda cam, kind: f"{kind}-chime"), \
                patch("cameras.popups.camera_admins_only", return_value=admins_only), \
                patch("cameras.config.popup_seconds", return_value=45):
            asyncio.run(self.worker._handle(row))
        return pushed

    def test_fresh_ring_pushes_alert(self):
        self.assertEqual(self._handle(["doorbell"]), [("cam1", "Doorbell", 45, "doorbell", "doorbell-chime", False)])

    def test_old_or_unwanted_does_not_push(self):
        self.assertEqual(self._handle(["doorbell"], seconds_ago=120), [])
        self.assertEqual(self._handle(["person"], event_id="r2"), [])
        self.assertEqual(self._handle(["doorbell"], wanted=(), event_id="r3"), [])

    def test_each_kind_pops_up_once_per_event(self):
        wanted = ("person", "package")
        self.assertEqual(self._handle([], wanted=wanted), [])
        self.assertEqual(self._handle(["person"], wanted=wanted), [("cam1", "Doorbell", 45, "person", "person-chime", False)])
        self.assertEqual(self._handle(["person"], wanted=wanted), [])
        self.assertEqual(self._handle(["person", "package"], wanted=wanted),
                         [("cam1", "Doorbell", 45, "package", "package-chime", False)])

    def test_push_builds_alert(self):
        import asyncio
        import sys
        import types
        fake = types.ModuleType("bdh_sdk.alerts")
        fake.Alert = lambda **kw: kw
        fake.push = MagicMock()
        with patch.dict(sys.modules, {"bdh_sdk.alerts": fake}), \
                patch("bdh_sdk.alerts", fake, create=True):
            asyncio.run(ev_mod._push_popup("cam1", "Front", 45))
            asyncio.run(ev_mod._push_popup("cam1", "Front", 45, "vehicle", "none", True))
        self.assertEqual([c.args[0] for c in fake.push.call_args_list], [{
            "app": "cameras", "key": "doorbell:cam1", "title": "Someone's at the door (Front)",
            "embed_url": "/cameras/popup/cam1/", "ttl": 45, "sound": None, "admins_only": False,
        }, {
            "app": "cameras", "key": "doorbell:cam1", "title": "Vehicle at Front",
            "embed_url": "/cameras/popup/cam1/", "ttl": 45, "sound": "none", "admins_only": True,
        }])

    def test_signed_in_only_camera_pops_up_for_admins_only(self):
        self.assertEqual(self._handle(["doorbell"], admins_only=True),
                         [("cam1", "Doorbell", 45, "doorbell", "doorbell-chime", True)])


class EventViewTests(TestCase):
    def setUp(self):
        self.is_admin = False
        p = patch("cameras.policy.is_admin", side_effect=lambda request: self.is_admin)
        p.start()
        self.addCleanup(p.stop)
        frames_mod.clear()
        self.addCleanup(frames_mod.clear)
        _ev("p1", ["person"], minutes_ago=10, thumb=b"jpg")
        _ev("m1", ["motion"], minutes_ago=5)
        _ev("d1", ["doorbell"], minutes_ago=1)

    def _admin(self):
        self.is_admin = True
        return Client()

    def test_history_endpoints_are_admin_only(self):
        for url in ("/cameras/events/", "/cameras/events/p1/thumb/",
                    "/cameras/snapshot/cam1/?ts=1700000000", "/cameras/clip/cam1/?start=1700000000"):
            self.assertEqual(Client().get(url).status_code, 403, url)

    def test_events_filtered_to_enabled_kinds(self):
        data = self._admin().get("/cameras/events/").json()
        self.assertEqual([e["id"] for e in data["events"]], ["d1", "p1"])  # motion is off by default
        self.assertNotIn("motion", data["kinds"])
        with patch("cameras.views.config.enabled_event_kinds", return_value=["motion"]):
            data = self._admin().get("/cameras/events/").json()
        self.assertEqual([e["id"] for e in data["events"]], ["m1"])

    def test_thumb_from_db(self):
        resp = self._admin().get("/cameras/events/p1/thumb/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.content, b"jpg")

    def test_thumb_fetched_through_worker(self):
        with patch("cameras.views.events.thumbnail", return_value=b"new") as fetch:
            resp = self._admin().get("/cameras/events/d1/thumb/")
        self.assertEqual(resp.content, b"new")
        fetch.assert_called_once_with("d1", "192.168.10.1", "cam1")

    def test_no_worker_is_503(self):
        with patch.object(ev_mod, "_workers", {}):
            c = self._admin()
            self.assertEqual(c.get("/cameras/snapshot/cam1/?ts=1700000000").status_code, 503)
            self.assertEqual(c.get("/cameras/clip/cam1/?start=1700000000").status_code, 503)
            self.assertEqual(c.get("/cameras/events/d1/thumb/").status_code, 503)

    def test_snapshot_parses_ts(self):
        client = MagicMock()
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            client.get_camera_snapshot.return_value = b"img"
            resp = self._admin().get("/cameras/snapshot/cam1/?ts=1700000000000&w=5000")
        self.assertEqual(resp.content, b"img")
        client.get_camera_snapshot.assert_called_once_with(
            "cam1", width=1920, dt=datetime.fromtimestamp(1_700_000_000, tz=dt_tz.utc))

    def test_historical_snapshot_is_resized_and_cached(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (2688, 1512), (40, 90, 160)).save(buf, "JPEG")
        client = MagicMock()
        client.get_camera_snapshot.return_value = buf.getvalue()
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            c = self._admin()
            first = c.get("/cameras/snapshot/cam1/?ts=1700000003&w=160")
            again = c.get("/cameras/snapshot/cam1/?ts=1700000001&w=160")  # same 5 s bucket
        self.assertEqual(Image.open(io.BytesIO(first.content)).size, (160, 90))
        self.assertEqual(again.content, first.content)
        client.get_camera_snapshot.assert_called_once_with(
            "cam1", width=160, dt=datetime.fromtimestamp(1_700_000_000, tz=dt_tz.utc))

    def test_scrub_card_frame_warms_full_view_cache(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (2688, 1512), (40, 90, 160)).save(buf, "JPEG")
        client = MagicMock()
        client.get_camera_snapshot.return_value = buf.getvalue()
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            c = self._admin()
            card = c.get("/cameras/snapshot/cam1/?ts=1700000002&w=320")
            full = c.get("/cameras/snapshot/cam1/?ts=1700000004&w=1280")
            c.get("/cameras/snapshot/cam1/?ts=1700000100&w=160")  # filmstrip widths don't warm
        self.assertEqual(Image.open(io.BytesIO(card.content)).size, (320, 180))
        self.assertEqual(Image.open(io.BytesIO(full.content)).size, (1280, 720))
        self.assertEqual(client.get_camera_snapshot.call_count, 2)  # the 1280 came from cache
        self.assertEqual(client.get_camera_snapshot.call_args_list[0].kwargs["width"], 1280)
        self.assertIsNone(frames_mod.get(("cam1", 1_700_000_100, 320)))

    def test_warm_widths_share_one_decode(self):
        import io
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (2688, 1512), (40, 90, 160)).save(buf, "JPEG")
        real_open = Image.open
        with patch("PIL.Image.open", side_effect=real_open) as opened:
            out = frames_mod.resize_many(buf.getvalue(), (320, 1280))
        self.assertEqual(opened.call_count, 1)
        self.assertEqual(Image.open(io.BytesIO(out[1280])).size, (1280, 720))
        self.assertEqual(Image.open(io.BytesIO(out[320])).size, (320, 180))

    def test_concurrent_resizes_are_bounded(self):
        import io
        import threading
        import time as time_mod
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 360)).save(buf, "JPEG")
        real_open = Image.open
        lock = threading.Lock()
        state = {"now": 0, "peak": 0}

        def slow_open(*a, **kw):
            with lock:
                state["now"] += 1
                state["peak"] = max(state["peak"], state["now"])
            time_mod.sleep(0.05)
            with lock:
                state["now"] -= 1
            return real_open(*a, **kw)

        with patch("PIL.Image.open", side_effect=slow_open):
            threads = [threading.Thread(target=frames_mod.resize, args=(buf.getvalue(), 160))
                       for _ in range(8)]
            for t in threads:
                t.start()
            for t in threads:
                t.join()
        self.assertEqual(state["peak"], frames_mod.DECODE_SLOTS)

    def test_heap_is_trimmed_once_a_burst_of_decodes_ends(self):
        import io
        import threading
        from PIL import Image
        buf = io.BytesIO()
        Image.new("RGB", (640, 360)).save(buf, "JPEG")
        real_open = Image.open
        release = threading.Event()
        started = threading.Semaphore(0)

        def held_open(*a, **kw):
            started.release()
            release.wait(5)
            return real_open(*a, **kw)

        libc = MagicMock()
        with patch.object(frames_mod, "_libc", libc), \
                patch("PIL.Image.open", side_effect=held_open):
            threads = [threading.Thread(target=frames_mod.resize, args=(buf.getvalue(), 160))
                       for _ in range(frames_mod.DECODE_SLOTS)]
            for t in threads:
                t.start()
            for _ in threads:
                started.acquire(timeout=5)
            libc.malloc_trim.assert_not_called()  # nothing freed mid-burst
            release.set()
            for t in threads:
                t.join()
        # Whichever decode finishes last trims; a concurrent one may also see 0.
        self.assertGreaterEqual(libc.malloc_trim.call_count, 1)
        self.assertLessEqual(libc.malloc_trim.call_count, len(threads))
        self.assertEqual(frames_mod._decoding, 0)

    def test_failed_decode_still_trims(self):
        libc = MagicMock()
        with patch.object(frames_mod, "_libc", libc):
            frames_mod.resize_many(b"not a jpeg", (320,))
        libc.malloc_trim.assert_called_once_with(0)
        self.assertEqual(frames_mod._decoding, 0)

    def test_undecodable_frame_is_served_as_is(self):
        self.assertEqual(frames_mod.resize_many(b"not a jpeg", (320, 1280)),
                         {320: b"not a jpeg", 1280: b"not a jpeg"})

    def test_live_snapshot_is_cached_and_refreshed_in_background(self):
        client = MagicMock()
        client.get_camera_snapshot.side_effect = [b"one", b"two", b"three"]
        refreshed = []
        # _refresh_async runs under _live_lock; record the call and run the
        # refresh after the request, as the background thread would.
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)), \
                patch("cameras.frames._ensure_warmer"), \
                patch("cameras.frames._refresh_async",
                      side_effect=lambda key, fetch: refreshed.append((key, fetch))):
            c = self._admin()
            first = c.get("/cameras/snapshot/cam1/?w=160")
            cached = c.get("/cameras/snapshot/cam1/?w=160")
            self.assertEqual(refreshed, [])
            frames_mod._live[("cam1", 160)][0] -= frames_mod.LIVE_FRESH + 1
            stale = c.get("/cameras/snapshot/cam1/?w=160")  # served at once, refreshed behind
            for key, fetch in refreshed:
                frames_mod._refresh(key, fetch)
            fresh = c.get("/cameras/snapshot/cam1/?w=160")
            frames_mod._live[("cam1", 160)][0] -= frames_mod.LIVE_MAX_AGE + 1
            too_old = c.get("/cameras/snapshot/cam1/?w=160")
        self.assertEqual([r.content for r in (first, cached, stale, fresh, too_old)],
                         [b"one", b"one", b"one", b"two", b"three"])
        self.assertEqual([key for key, _fetch in refreshed], [("cam1", 160)])
        self.assertEqual(first["Cache-Control"], "no-store")
        client.get_camera_snapshot.assert_called_with("cam1", width=160, dt=None)

    def test_live_snapshot_failure_is_not_cached(self):
        client = MagicMock()
        client.get_camera_snapshot.side_effect = [b"", b"img"]
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)), \
                patch("cameras.frames._ensure_warmer"):
            c = self._admin()
            self.assertEqual(c.get("/cameras/snapshot/cam1/?w=160").status_code, 404)
            self.assertEqual(c.get("/cameras/snapshot/cam1/?w=160").content, b"img")

    def test_clip_window_is_capped(self):
        client = MagicMock()
        client.get_camera_video.side_effect = lambda cam, s, e, output_file: output_file.write_bytes(b"mp4")
        start = 1_700_000_000
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"BDH_DATA_DIR": tmp}), \
                patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            resp = self._admin().get(f"/cameras/clip/cam1/?start={start}&end={start + 3600}")
            self.assertEqual(resp["Content-Type"], "video/mp4")
            self.assertEqual(_body(resp), b"mp4")
            _, s, e = client.get_camera_video.call_args.args
            self.assertEqual((e - s).total_seconds(), 120)
            self.assertEqual(self._admin().get("/cameras/clip/cam1/").status_code, 400)
            future = int(dj_tz.now().timestamp()) + 600
            self.assertEqual(self._admin().get(f"/cameras/clip/cam1/?start={future}").status_code, 400)

    def test_empty_clip_is_404(self):
        client = MagicMock()
        client.get_camera_video.side_effect = lambda cam, s, e, output_file: None
        with tempfile.TemporaryDirectory() as tmp, patch.dict("os.environ", {"BDH_DATA_DIR": tmp}), \
                patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            self.assertEqual(self._admin().get("/cameras/clip/cam1/?start=1700000000").status_code, 404)
            self.assertEqual(os.listdir(os.path.join(tmp, "clips")), [])


from . import clips as clips_mod  # noqa: E402


class ClipCacheTests(unittest.TestCase):
    """Clips go to disk, not memory: an in-memory export OOM-killed the app."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        p = patch.dict("os.environ", {"BDH_DATA_DIR": tmp.name})
        p.start()
        self.addCleanup(p.stop)
        self.start = datetime(2026, 11, 16, 12, 0, tzinfo=dt_tz.utc)
        self.end = self.start + timedelta(seconds=30)

    def _get(self, export, camera="cam1"):
        return clips_mod.get(camera, self.start, self.end, export, timeout=5)

    def test_export_is_cached(self):
        calls = []

        def export(path):
            calls.append(path)
            path.write_bytes(b"x" * 10)

        first = self._get(export)
        self.assertEqual(self._get(export), first)
        self.assertEqual(len(calls), 1)
        self.assertEqual(first.read_bytes(), b"x" * 10)
        self.assertEqual([p.name for p in clips_mod.clip_dir().iterdir()], [first.name])

    def test_failed_export_leaves_no_file(self):
        def export(path):
            path.write_bytes(b"partial")
            raise RuntimeError("boom")

        with self.assertRaises(RuntimeError):
            self._get(export)
        self.assertEqual(list(clips_mod.clip_dir().iterdir()), [])
        self.assertEqual(clips_mod._exports._value, clips_mod.MAX_EXPORTS)

    def test_camera_id_cannot_escape_the_folder(self):
        path = self._get(lambda p: p.write_bytes(b"x"), camera="../../etc/passwd")
        self.assertEqual(path.parent, clips_mod.clip_dir())

    def test_concurrent_exports_are_capped(self):
        import threading
        running, peak, lock = [0], [0], threading.Lock()
        release = threading.Event()

        def export(path):
            with lock:
                running[0] += 1
                peak[0] = max(peak[0], running[0])
            release.wait(5)
            with lock:
                running[0] -= 1
            path.write_bytes(b"x")

        threads = [threading.Thread(target=clips_mod.get, args=(
            f"cam{i}", self.start, self.end, export, 5)) for i in range(clips_mod.MAX_EXPORTS + 2)]
        for t in threads:
            t.start()
        time.sleep(0.3)
        self.assertEqual(peak[0], clips_mod.MAX_EXPORTS)
        release.set()
        for t in threads:
            t.join(5)
        self.assertEqual(len(list(clips_mod.clip_dir().glob("*.mp4"))), clips_mod.MAX_EXPORTS + 2)

    def test_prune_drops_old_then_oldest_over_budget(self):
        folder = clips_mod.clip_dir()
        folder.mkdir(parents=True)
        now = time.time()
        for name, age in (("old.mp4", clips_mod.CACHE_SECONDS + 5), ("a.mp4", 30), ("b.mp4", 20), ("c.mp4", 10)):
            (folder / name).write_bytes(b"x" * 100)
            os.utime(folder / name, (now - age, now - age))
        with patch.object(clips_mod, "CACHE_BYTES", 200):
            clips_mod.prune(keep=folder / "a.mp4", now=now)
        self.assertEqual(sorted(p.name for p in folder.iterdir()), ["a.mp4", "c.mp4"])

    def _serve(self, range_header=None):
        from django.test import RequestFactory
        path = self._get(lambda p: p.write_bytes(bytes(range(100))))
        headers = {"HTTP_RANGE": range_header} if range_header else {}
        return clips_mod.response(RequestFactory().get("/", **headers), path, "no-store")

    def test_full_response(self):
        resp = self._serve()
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["Content-Length"], "100")
        self.assertEqual(resp["Accept-Ranges"], "bytes")
        self.assertEqual(_body(resp), bytes(range(100)))

    def test_range_responses(self):
        for header, status, body, content_range in (
                ("bytes=10-19", 206, bytes(range(10, 20)), "bytes 10-19/100"),
                ("bytes=90-", 206, bytes(range(90, 100)), "bytes 90-99/100"),
                ("bytes=-5", 206, bytes(range(95, 100)), "bytes 95-99/100"),
                ("bytes=95-500", 206, bytes(range(95, 100)), "bytes 95-99/100")):
            resp = self._serve(header)
            self.assertEqual(resp.status_code, status, header)
            self.assertEqual(resp["Content-Range"], content_range, header)
            self.assertEqual(_body(resp), body, header)
        resp = self._serve("bytes=100-")
        self.assertEqual(resp.status_code, 416)
        self.assertEqual(resp["Content-Range"], "bytes */100")


@override_settings(
    GO2RTC_URL='http://localhost:1984',
    **protect_settings([{'host': '192.168.10.1', 'api_key': 'k', 'name': 'Test Site'}]),
)
class EventTemplateTests(TestCase):
    CAMS = [{'name': 'Test Site', 'host': '192.168.10.1', 'cameras': [
        {'name': 'Front Door', 'stream_name': 'front_door', 'rtsp_url': 'rtsps://x',
         'rtsp_url_low': 'rtsps://x_low', 'camera_id': 'cam1', 'is_ptz': False, 'ptz_presets': 0},
    ]}]

    def setUp(self):
        clear_cache()

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_guest_sees_live_only(self, mock_cameras, _):
        mock_cameras.return_value = self.CAMS
        html = Client().get("/cameras/").content.decode()
        self.assertIn('data-camera-id="cam1"', html)
        self.assertNotIn('ev-rail', html)
        self.assertNotIn('bdh/push.js', html)
        self.assertNotIn('cam-scrub', html)
        self.assertNotIn('<img class="cam-poster"', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_every_video_has_a_poster(self, mock_cameras, _):
        # Android WebView (the kiosk) paints a big grey play triangle on a poster-less
        # <video> until its first frame, e.g. while a scrubbed replay loads.
        import re
        mock_cameras.return_value = self.CAMS
        with patch("cameras.policy.is_admin", return_value=True):
            html = Client().get("/cameras/").content.decode()
        videos = re.findall(r'<video\s[^>]*>', html)
        self.assertTrue(videos)
        for tag in videos:
            self.assertRegex(tag, r'\sposter="[^"]+"', tag)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_admin_sees_rail_and_scrubber(self, mock_cameras, _):
        mock_cameras.return_value = self.CAMS
        with patch("cameras.policy.is_admin", return_value=True):
            html = Client().get("/cameras/").content.decode()
        # First (visible) site's stills load eagerly with the page
        self.assertIn('<img class="cam-poster" alt="" src="/cameras/snapshot/cam1/?w=640">', html)
        # Multi-line {# #} isn't a Django comment: it renders as text that pushes the video down
        self.assertNotIn('{#', html)
        self.assertIn('class="ev-rail"', html)
        self.assertIn('/static/bdh/push.js', html)
        self.assertNotIn('setInterval(loadEvents', html)
        self.assertIn('class="cam-scrub"', html)
        self.assertIn('data-kind="person"', html)
        self.assertNotIn('data-kind="motion"', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_cached_poster_is_inlined(self, mock_cameras, _):
        import time
        from cameras import frames
        mock_cameras.return_value = self.CAMS
        cached = {("cam1", 640): [time.monotonic(), b"jpg", 0]}
        with patch("cameras.policy.is_admin", return_value=True), \
                patch.dict(frames._live, cached, clear=True):
            html = Client().get("/cameras/").content.decode()
            stale = {("cam1", 640): [time.monotonic() - frames.LIVE_MAX_AGE - 1, b"jpg", 0]}
            with patch.dict(frames._live, stale, clear=True):
                old = Client().get("/cameras/").content.decode()
        self.assertIn('<img class="cam-poster" alt="" src="data:image/jpeg;base64,anBn">', html)
        self.assertIn('src="/cameras/snapshot/cam1/?w=640"', old)

    @patch("cameras.views._cached_poster", return_value=None)
    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_popup_is_public_and_live_only(self, mock_cameras, mock_register, _poster):
        mock_cameras.return_value = self.CAMS
        resp = Client().get("/cameras/popup/cam1/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(resp, 'data-stream-name="front_door"')
        # WebRTC first (no MSE race -> no black flash), MSE only on failure.
        self.assertContains(resp, 'data-mode="webrtc/tcp"')
        self.assertContains(resp, 'data-fallback-mode="mse"')
        self.assertNotContains(resp, "cam-scrub")
        self.assertNotContains(resp, 'class="poster"')
        mock_register.assert_called_once()
        self.assertEqual(Client().get("/cameras/popup/nope/").status_code, 404)

    @patch("cameras.views._cached_poster", return_value=None)
    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_popup_offers_sound_and_talk_by_camera(self, mock_cameras, _register, _poster):
        cam = {'name': 'Door', 'stream_name': 'door', 'rtsp_url': 'rtsps://x', 'camera_id': 'd1',
               'has_mic': True, 'has_speaker': True,
               'talkback': {'url': 'rtp://1.2.3.4:7004', 'codec': 'opus', 'rate': 24000}}
        mock_cameras.return_value = [{'name': 'S', 'cameras': [cam]}]
        html = Client().get("/cameras/popup/d1/").content.decode()
        self.assertIn('data-has-mic="1" data-talk-stream="door_talk"', html)
        self.assertIn('class="cam-spk"', html)
        self.assertIn('class="cam-talk"', html)
        mock_cameras.return_value = [{'name': 'S', 'cameras': [dict(cam, talkback=None)]}]
        html = Client().get("/cameras/popup/d1/").content.decode()
        self.assertNotIn('data-talk-stream', html)
        self.assertNotIn('class="cam-talk"', html)
        self.assertIn('class="cam-spk"', html)
        mock_cameras.return_value = [{'name': 'S', 'cameras': [dict(cam, has_mic=False, talkback=None)]}]
        html = Client().get("/cameras/popup/d1/").content.decode()
        self.assertNotIn('class="cam-spk"', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_popup_poster_is_cached_still_or_warms_cache(self, mock_cameras, _):
        import time
        from cameras import frames
        mock_cameras.return_value = self.CAMS
        cached = {("cam1", 640): [time.monotonic(), b"jpg", 0]}
        with patch.dict(frames._live, cached, clear=True):
            html = Client().get("/cameras/popup/cam1/").content.decode()
        self.assertIn('<img class="poster" alt="" src="data:image/jpeg;base64,anBn">', html)
        # Cold cache: no poster (never a blocking Protect fetch or the
        # admin-only snapshot URL), but the still is fetched for next time.
        with patch.dict(frames._live, {}, clear=True), \
                patch("cameras.views._fetch_live", return_value=b"new") as fetch, \
                patch("cameras.frames._ensure_warmer"):
            html = Client().get("/cameras/popup/cam1/").content.decode()
            for _ in range(50):
                if ("cam1", 640) in frames._live:
                    break
                time.sleep(0.02)
            warmed = frames._live.get(("cam1", 640))
        self.assertNotIn('class="poster"', html)
        self.assertNotIn("/snapshot/", html)
        fetch.assert_called_once_with(("cam1", 640))
        self.assertEqual(warmed[1], b"new")


from . import popups as popups_mod  # noqa: E402


class EventPopupConfigTests(TestCase):
    def test_matches_uses_domain_defaults(self):
        m = popups_mod.matches
        door = {"entity_id": "binary_sensor.door"}
        self.assertTrue(m(door, {"old": "off", "new": "on"}))
        self.assertFalse(m(door, {"old": "on", "new": "off"}))
        self.assertFalse(m(door, {"old": "unavailable", "new": "on"}))
        self.assertTrue(m({"entity_id": "binary_sensor.door", "to": "off"}, {"old": "on", "new": "off"}))
        lock = {"entity_id": "lock.front"}
        self.assertTrue(m(lock, {"old": "locked", "new": "unlocked"}))
        self.assertFalse(m(lock, {"old": "unlocked", "new": "locked"}))
        ring = {"entity_id": "event.doorbell"}
        self.assertTrue(m(ring, {"old": "2026-01-01T00:00:00", "new": "2026-01-01T00:01:00"}))
        self.assertTrue(m(ring, {"old": "unknown", "new": "2026-01-01T00:01:00"}))
        self.assertFalse(m(ring, {"old": "2026-01-01T00:01:00", "new": "unavailable"}))
        other = {"entity_id": "input_select.mode"}
        self.assertTrue(m(other, {"old": "a", "new": "b"}))
        self.assertFalse(m(other, {"old": "b", "new": "b"}))
        self.assertFalse(m(other, {"hook": 1}))

    def test_clean_drops_bad_parts(self):
        cfg = popups_mod.clean({"enabled": False, "cameras": {
            "cam1": {"kinds": ["person", "nope", "doorbell"], "triggers": [
                {"entity_id": "lock.front", "to": " unlocked "},
                {"entity_id": "lock.front", "to": "unlocked"},
                {"entity_id": "Bad Id"}, "x", {"entity_id": "event.ring", "to": ""}]},
            "cam2": {"kinds": [], "triggers": []},
            "cam3": "nope",
        }})
        self.assertEqual(cfg, {"enabled": False, "cameras": {"cam1": {
            "kinds": ["doorbell", "person"],
            "triggers": [{"entity_id": "lock.front", "to": "unlocked"}, {"entity_id": "event.ring"}]}}})
        self.assertEqual(popups_mod.clean("junk"), {"enabled": True, "cameras": {}})

    def test_clean_instances(self):
        cfg = popups_mod.clean({"cameras": {"cam1": {"kinds": [], "triggers": [
            {"entity_id": "lock.gate", "instance": "ha"},
            {"entity_id": "lock.gate"},
            {"entity_id": "lock.gate", "instance": "cabin"},
            {"entity_id": "lock.gate", "instance": "cabin"},
            {"entity_id": "lock.shed", "instance": "Bad-Id"},
            {"entity_id": "lock.shed", "instance": ""}]}}})
        self.assertEqual(cfg["cameras"]["cam1"]["triggers"], [
            {"entity_id": "lock.gate"}, {"entity_id": "lock.gate", "instance": "cabin"},
            {"entity_id": "lock.shed"}])

    def test_clean_keeps_only_non_default_sounds(self):
        cfg = popups_mod.clean({"cameras": {"cam1": {
            "kinds": ["doorbell", "person", "vehicle"],
            "sounds": {"doorbell": "ding-dong", "person": "none", "vehicle": "bogus", "animal": "kalimba"},
            "triggers": [{"entity_id": "lock.front", "sound": "westminster"},
                         {"entity_id": "event.ring", "sound": "knock-knock"},
                         {"entity_id": "binary_sensor.gate", "sound": "/x.mp3"}]}}})
        self.assertEqual(cfg["cameras"]["cam1"], {
            "kinds": ["doorbell", "person", "vehicle"], "sounds": {"person": "none"},
            "triggers": [{"entity_id": "lock.front", "sound": "westminster"},
                         {"entity_id": "event.ring"}, {"entity_id": "binary_sensor.gate"}]})

    def test_title_for(self):
        self.assertEqual(popups_mod.title_for({"entity_id": "lock.front", "new": "unlocked",
                                               "attributes": {"friendly_name": "Front lock"}}, "Cam"),
                         "Front lock unlocked")
        self.assertEqual(popups_mod.title_for({"entity_id": "event.ring", "new": "x"}, "Cam"), "Cam")


class PopupStoredConfigTests(TestCase):
    CAMS = [{"name": "Home", "cameras": [
        {"camera_id": "cam1", "name": "Front Door", "is_doorbell": True},
        {"camera_id": "cam2", "name": "Drive", "is_doorbell": False}]}]

    def setUp(self):
        from bdh_sdk import alerts, events
        from bdh_sdk.storage import app_state
        self.state = app_state("cameras")
        self.addCleanup(self.state.delete, popups_mod.STATE_KEY)
        self.events = events
        events.reset()
        self.pushed = []
        old = alerts._sender
        alerts.set_sender(self.pushed.append)
        self.addCleanup(alerts.set_sender, old)
        self.addCleanup(events.reset)
        self.addCleanup(popups_mod.stop)
        for target, kw in (
            ("cameras.config.popup_seconds", {"return_value": 45}),
            ("cameras.protect_api.get_protect_cameras", {"return_value": self.CAMS}),
            # Its database read runs in a worker thread, which can't see the test's transaction.
            ("cameras.popups.camera_admins_only", {"return_value": False}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_doorbells_ring_until_saved(self):
        self.assertEqual(popups_mod.camera_kinds("cam1"), ["doorbell"])
        self.assertEqual(popups_mod.current(), {"enabled": True, "cameras": {"cam1": {
            "kinds": ["doorbell"], "triggers": []}}})
        popups_mod.start()
        self.events.publish("ha/state/binary_sensor.front_door", {"old": "off", "new": "on"})
        self.events.flush(5)
        self.assertEqual(self.pushed, [])

    def test_saved_triggers_pop_up_their_camera(self):
        popups_mod.start()
        popups_mod.save({"cameras": {
            "cam2": {"kinds": ["vehicle"], "sounds": {"vehicle": "kalimba"},
                     "triggers": [{"entity_id": "lock.gate"}, {"entity_id": "lock.shed", "sound": "none"}]}}})
        self.assertEqual(popups_mod.camera_kinds("cam2"), ["vehicle"])
        self.assertEqual(popups_mod.camera_kinds("cam1"), [])
        self.assertEqual(popups_mod.camera_sound("cam2", "vehicle"), "kalimba")
        self.assertEqual(popups_mod.camera_sound("cam1", "person"), "notify")
        self.events.publish("ha/state/lock.gate", {"entity_id": "lock.gate", "old": "locked", "new": "unlocked",
                                                   "attributes": {"friendly_name": "Gate"}})
        self.events.publish("ha/state/lock.gate", {"entity_id": "lock.gate", "old": "unlocked", "new": "locked"})
        self.events.publish("ha/state/lock.shed", {"entity_id": "lock.shed", "old": "locked", "new": "unlocked",
                                                   "attributes": {"friendly_name": "Shed"}})
        self.events.flush(5)
        self.assertEqual([(a.as_dict()["key"], a.as_dict()["title"], a.as_dict()["sound"]) for a in self.pushed],
                         [("doorbell:cam2", "Gate unlocked", "knock-knock"),
                          ("doorbell:cam2", "Shed unlocked", "none")])

    def test_trigger_on_other_instance(self):
        popups_mod.start()
        popups_mod.save({"cameras": {"cam2": {"kinds": [], "triggers": [
            {"entity_id": "lock.gate", "instance": "cabin"}]}}})
        self.events.publish("ha/state/lock.gate", {"entity_id": "lock.gate", "old": "locked", "new": "unlocked"})
        self.events.publish("cabin/state/lock.gate", {"entity_id": "lock.gate", "old": "locked",
                                                      "new": "unlocked", "attributes": {"friendly_name": "Gate"}})
        self.events.flush(5)
        self.assertEqual([a.as_dict()["title"] for a in self.pushed], ["Gate unlocked"])

    def test_master_switch_off(self):
        popups_mod.start()
        popups_mod.save({"enabled": False, "cameras": {
            "cam2": {"kinds": ["vehicle"], "triggers": [{"entity_id": "lock.gate"}]}}})
        self.assertEqual(popups_mod.camera_kinds("cam2"), [])
        self.events.publish("ha/state/lock.gate", {"old": "locked", "new": "unlocked"})
        self.events.flush(5)
        self.assertEqual(self.pushed, [])


class PopupStartRetryTests(unittest.TestCase):
    """Settings are read from the hub, which may still be starting: pop-ups
    must keep trying rather than give up until the next settings save."""

    TARGET = ("ha/state/binary_sensor.front_door", {"trigger": {"entity_id": "binary_sensor.front_door"},
                                                    "camera_id": "cam1"})

    def setUp(self):
        from bdh_sdk import events
        events.reset()
        self.addCleanup(events.reset)
        self.addCleanup(popups_mod.stop)
        p = patch.object(popups_mod, "RETRY_FIRST", 0.01)
        p.start()
        self.addCleanup(p.stop)

    def wait_for(self, check, timeout=3.0):
        import time
        end = time.monotonic() + timeout
        while time.monotonic() < end:
            if check():
                return True
            time.sleep(0.01)
        return False

    def test_retries_until_settings_readable(self):
        from bdh_sdk import events
        reads = [RuntimeError("hub starting"), RuntimeError("hub starting"), [self.TARGET]]
        with patch.object(popups_mod, "_targets", side_effect=reads) as targets, \
                self.assertLogs("cameras.popups", "WARNING"):
            popups_mod.start()
            self.assertTrue(self.wait_for(lambda: popups_mod._subscriptions))
        self.assertEqual(targets.call_count, 3)
        self.assertIn("ha/state/binary_sensor.front_door", events.patterns())

    def test_stop_cancels_retry(self):
        with patch.object(popups_mod, "_targets", side_effect=RuntimeError("down")) as targets, \
                self.assertLogs("cameras.popups", "ERROR"):
            popups_mod.start()
            popups_mod.stop()
            import time
            time.sleep(0.1)
        self.assertEqual(targets.call_count, 1)

    def test_reload_supersedes_retry(self):
        reads = [RuntimeError("hub starting"), [self.TARGET], [self.TARGET]]
        with patch.object(popups_mod, "_targets", side_effect=reads) as targets, \
                self.assertLogs("cameras.popups", "ERROR"):
            popups_mod.start()
            popups_mod.reload()  # e.g. a settings save before the retry fires
            import time
            time.sleep(0.1)
        self.assertEqual(targets.call_count, 2)
        self.assertEqual(len(popups_mod._subscriptions), 1)


class PopupSettingsViewTests(TestCase):
    def setUp(self):
        from django.core.cache import cache
        from bdh_sdk.storage import app_state
        cache.clear()
        self.addCleanup(cache.clear)
        self.addCleanup(app_state("cameras").delete, popups_mod.STATE_KEY)
        self.addCleanup(popups_mod.stop)
        self.is_admin = False
        for target, kw in (
            ("cameras.policy.is_admin", {"side_effect": lambda request: self.is_admin}),
            ("cameras.views.get_protect_cameras", {"return_value": PopupStoredConfigTests.CAMS}),
            ("cameras.protect_api.get_protect_cameras", {"return_value": PopupStoredConfigTests.CAMS}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def test_admins_only(self):
        self.assertEqual(self.client.get("/cameras/settings/popups/").status_code, 404)
        self.assertEqual(self.client.get("/cameras/settings/popups/entities/").status_code, 404)
        self.assertEqual(self.client.post("/cameras/settings/popups/", "{}",
                                          content_type="application/json").status_code, 404)

    def test_page_and_save(self):
        self.is_admin = True
        resp = self.client.get("/cameras/settings/popups/")
        self.assertEqual(resp.status_code, 200)
        self.assertContains(resp, 'id="cp-data"')
        self.assertContains(resp, "Front Door")
        resp = self.client.post("/cameras/settings/popups/", json.dumps({"cameras": {
            "cam1": {"kinds": ["person"], "triggers": [{"entity_id": "event.ring"}]}}}),
            content_type="application/json")
        self.assertEqual(resp.status_code, 200)
        saved = {"enabled": True, "cameras": {"cam1": {"kinds": ["person"], "triggers": [{"entity_id": "event.ring"}]}}}
        self.assertEqual(resp.json(), saved)
        self.assertEqual(popups_mod.stored(), saved)
        self.assertEqual(self.client.post("/cameras/settings/popups/", "{nope",
                                          content_type="application/json").status_code, 400)

    def test_entities(self):
        self.is_admin = True
        states = [
            {"entity_id": "binary_sensor.front_door", "state": "off",
             "attributes": {"friendly_name": "Front Door", "device_class": "door"}},
            {"entity_id": "binary_sensor.front_door_motion", "state": "off",
             "attributes": {"friendly_name": "Front Door Motion", "device_class": "motion"}},
            {"entity_id": "sensor.front_door_battery", "state": "90",
             "attributes": {"friendly_name": "Front Door Battery"}},
            {"entity_id": "sensor.temp", "state": "20", "attributes": {}},
        ]
        registry = {
            "entities": [
                {"entity_id": "binary_sensor.front_door", "device_id": "d1", "area_id": None},
                {"entity_id": "binary_sensor.front_door_motion", "device_id": "d1", "area_id": "porch"},
                {"entity_id": "sensor.front_door_battery", "device_id": "d1", "entity_category": "diagnostic"},
                {"entity_id": "sensor.temp", "device_id": "gone", "area_id": "kitchen", "hidden_by": "user"}],
            "areas": [{"area_id": "hall", "name": "Hall"}, {"area_id": "kitchen", "name": "Kitchen"},
                      {"area_id": "porch", "name": "Porch"}],
            "devices": [
                {"id": "d1", "area_id": "hall", "name": "Front door", "name_by_user": None, "manufacturer": "Ring"},
                {"id": "unused", "name": "Unused"}, {"id": "gone", "name": "Gone", "disabled_by": "user"}],
        }
        with patch("bdh_sdk.ha.get_all_states", return_value=states), \
                patch("bdh_sdk.ha.get_registry", return_value=registry):
            data = self.client.get("/cameras/settings/popups/entities/").json()
        door = {"domain": "binary_sensor", "device": "d1", "category": "", "useful": True, "suggest": True,
                "instance": "ha"}
        self.assertEqual(data["entities"], [
            {"id": "binary_sensor.front_door", "name": "Front Door", "short": "Front Door", "area": "Hall",
             "state": "off", "device_class": "door", **door},
            {"id": "sensor.front_door_battery", "name": "Front Door Battery", "short": "Battery", "area": "Hall",
             "state": "90", "domain": "sensor", "device_class": "", "device": "d1", "category": "diagnostic",
             "useful": False, "suggest": False, "instance": "ha"},
            {"id": "binary_sensor.front_door_motion", "name": "Front Door Motion", "short": "Motion",
             "area": "Porch", "state": "off", "device_class": "motion", **door},
            {"id": "sensor.temp", "name": "sensor.temp", "short": "sensor.temp", "area": "Kitchen", "state": "20",
             "domain": "sensor", "device_class": "", "device": "", "category": "hidden", "useful": False,
             "suggest": False, "instance": "ha"},
        ])
        self.assertEqual(data["devices"], [{"id": "d1", "name": "Front door", "maker": "Ring", "area": "Hall",
                                            "instance": "ha"}])
        self.assertEqual(data["instances"], [{"id": "ha", "name": "Home Assistant", "site": "", "default": True}])

    def test_entities_several_instances(self):
        from bdh_sdk import ha
        self.is_admin = True
        insts = [{"id": "ha", "name": "Home", "site": "Mercer Island", "default": True},
                 {"id": "cabin", "name": "Cabin", "site": "Sun Peaks", "default": False},
                 {"id": "barn", "name": "Barn", "site": "", "default": False}]

        def states(instance=None):
            if instance == "barn":
                raise ha.HAError("down")
            return [{"entity_id": "lock.gate", "state": "locked",
                     "attributes": {"friendly_name": (instance or "home") + " gate"}}]

        def registry(instance=None):
            # An older hub: no "areas".
            return {"entities": [{"entity_id": "lock.gate", "device_id": "g"}],
                    "devices": [{"id": "g", "name": "Gate " + (instance or "home")}]}

        with patch("bdh_sdk.ha.instances", return_value=insts), \
                patch("bdh_sdk.ha.get_all_states", side_effect=states) as gs, \
                patch("bdh_sdk.ha.get_registry", side_effect=registry), \
                self.assertLogs("cameras.views", "WARNING"):
            data = self.client.get("/cameras/settings/popups/entities/").json()
        # The default instance is asked without ``instance`` (older hubs).
        self.assertEqual(gs.call_args_list[0], call())
        self.assertEqual([i["id"] for i in data["instances"]], ["ha", "cabin"])
        self.assertEqual([(e["instance"], e["id"], e["name"]) for e in data["entities"]],
                         [("ha", "lock.gate", "home gate"), ("cabin", "lock.gate", "cabin gate")])
        self.assertEqual([(d["instance"], d["name"]) for d in data["devices"]],
                         [("ha", "Gate home"), ("cabin", "Gate cabin")])

    def test_entities_ha_down(self):
        from bdh_sdk import ha
        self.is_admin = True
        with patch("bdh_sdk.ha.get_all_states", side_effect=ha.HAError("down")), \
                self.assertLogs("cameras.views", "WARNING"):
            self.assertEqual(self.client.get("/cameras/settings/popups/entities/").status_code, 503)


def _det_cam(camera_id, enabled, supported=None, doorbell=False):
    return {"camera_id": camera_id, "name": camera_id.title(), "is_doorbell": doorbell,
            "detections": {"enabled": enabled, "supported": supported or enabled}}


class DetectionTests(TestCase):
    def setUp(self):
        from bdh_sdk.storage import app_state
        self.state = app_state("cameras")
        for key in (_config_mod.SETTINGS_KEY, _config_mod.DETECTIONS_KEY, popups_mod.STATE_KEY):
            self.addCleanup(self.state.delete, key)

    def test_protect_kinds_objects_and_audio(self):
        self.assertEqual(_config_mod.protect_kinds(["alrmSpeak", "face", "smoke_cmonx", "nope", "car"]),
                         ["person", "vehicle", "smoke", "co", "speaking"])
        self.assertEqual(ev_mod.kinds_for("smartAudioDetect", ["alrmBabyCry"]), ["baby"])

    def test_camera_detections(self):
        from .protect_api import _detections
        cam = {"featureFlags": {"smartDetectTypes": ["person", "vehicle"], "smartDetectAudioTypes": ["alrmSmoke"]},
               "smartDetectSettings": {"objectTypes": ["person"], "audioTypes": []}}
        self.assertEqual(_detections(cam, True), {"supported": ["doorbell", "person", "vehicle", "motion", "smoke"],
                                                  "enabled": ["doorbell", "person", "motion"]})
        self.assertEqual(_detections({}, False), {"supported": ["motion"], "enabled": ["motion"]})

    def test_remember_detections(self):
        _config_mod.remember_detections("1", [_det_cam("a", ["person"]), {"camera_id": "b"}])
        self.assertEqual(_config_mod.known_detections(), {"1": [
            {"id": "a", "name": "A", "doorbell": False, "enabled": ["person"], "supported": ["person"]}]})

    def test_settings_fall_back_to_old_flat_settings(self):
        with patch("cameras.config.app_setting", side_effect=lambda app, key, default, **kw: {
                "show_animal": True, "show_person": False, "keep_open": False,
                "video_transport": "udp", "popup_seconds": 5}.get(key, default)):
            self.assertEqual(_config_mod.enabled_event_kinds(), ["doorbell", "vehicle", "package", "animal"])
            self.assertFalse(_config_mod.keep_open())
            self.assertEqual(_config_mod.video_transport(), "udp")
            self.assertEqual(_config_mod.popup_seconds(), 10)
            _config_mod.save_settings(events=["co", "person"], keep_open=True, video_transport="tcp")
            self.state.set(popups_mod.STATE_KEY, {"seconds": 90})
            self.assertEqual(_config_mod.enabled_event_kinds(), ["person", "co"])
            self.assertTrue(_config_mod.keep_open())
            self.assertEqual(_config_mod.video_mode(), "webrtc/tcp")
            self.assertEqual(_config_mod.popup_seconds(), 90)

    def test_popup_seconds_clamped(self):
        self.assertEqual(popups_mod.clean({"seconds": 5000})["seconds"], 600)
        self.assertEqual(popups_mod.clean({"seconds": "20"})["seconds"], 20)
        self.assertNotIn("seconds", popups_mod.clean({"seconds": "x"}))

    def test_unreachable_nvr_uses_last_known_detections(self):
        from .views import _detection_sites
        _config_mod.remember_detections("1", [_det_cam("a", ["person", "motion"])])
        sites = [{"id": "1", "name": "Home", "ok": False, "cameras": []},
                 {"id": "2", "name": "Cabin", "ok": True, "cameras": [_det_cam("b", ["vehicle"])]},
                 {"id": "3", "name": "Barn", "ok": True, "cameras": [{"camera_id": "c", "is_doorbell": True}]}]
        with patch("cameras.views.get_protect_cameras", return_value=sites):
            out = _detection_sites()
        self.assertEqual([(s["id"], s["stale"], [(c["id"], c["enabled"]) for c in s["cameras"]]) for s in out],
                         [("1", True, [("a", ["person", "motion"])]),
                          ("2", False, [("b", ["vehicle"])]),
                          ("3", True, [("c", ["doorbell", "motion"])])])


class MainSettingsViewTests(TestCase):
    URL = "/cameras/settings/"
    SITES = [{"id": "1", "name": "Home", "ok": True, "cameras": [
        _det_cam("front", ["doorbell", "person", "motion"], doorbell=True), _det_cam("drive", ["vehicle", "motion"])]}]

    def setUp(self):
        from bdh_sdk.storage import app_state
        self.state = app_state("cameras")
        for key in (_config_mod.SETTINGS_KEY, _config_mod.DETECTIONS_KEY, popups_mod.STATE_KEY):
            self.addCleanup(self.state.delete, key)
        self.is_admin = True
        self.nvrs = [{"id": "1", "name": "Home", "host": "10.0.0.1", "username": "",
                      "has_api_key": True, "has_password": False}]
        for target, kw in (
            ("cameras.policy.is_admin", {"side_effect": lambda request: self.is_admin}),
            ("cameras.views._nvr_rows", {"side_effect": lambda: [dict(n) for n in self.nvrs]}),
            ("cameras.views.get_protect_cameras", {"return_value": self.SITES}),
            ("cameras.protect_api.get_protect_cameras", {"return_value": self.SITES}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def page(self):
        resp = self.client.get(self.URL)
        self.assertEqual(resp.status_code, 200)
        return resp.context["page"]

    def post(self, body):
        return self.client.post(self.URL, json.dumps(body), content_type="application/json")

    def test_admins_only(self):
        self.is_admin = False
        self.assertEqual(self.client.get(self.URL).status_code, 404)
        self.assertEqual(self.post({"keep_open": False}).status_code, 404)

    def test_kinds_come_from_the_nvr(self):
        page = self.page()
        self.assertEqual([(k["id"], k["cameras"]) for k in page["kinds"]],
                         [("doorbell", 1), ("person", 1), ("vehicle", 1), ("motion", 2)])
        self.assertEqual((page["nvrs"][0]["cameras"], page["nvrs"][0]["ok"]), (2, True))
        self.assertEqual(page["popups"], {"enabled": True, "cameras": 1, "seconds": 60})

    def test_no_nvr(self):
        self.nvrs = []
        page = self.page()
        self.assertEqual((page["nvrs"], page["kinds"]), ([], []))
        self.assertContains(self.client.get(self.URL), "Needs an NVR")

    def test_save_keeps_picks_not_on_the_page(self):
        _config_mod.save_settings(events=["person", "smoke"])
        resp = self.post({"events": ["vehicle"], "shown": ["doorbell", "person", "vehicle", "motion"]})
        self.assertEqual(resp.json()["events"], ["vehicle", "smoke"])
        self.assertEqual(self.post({"keep_open": False, "video_transport": "udp"}).status_code, 200)
        self.assertEqual((_config_mod.keep_open(), _config_mod.video_transport(),
                          _config_mod.enabled_event_kinds()), (False, "udp", ["vehicle", "smoke"]))

    def test_save_rejects_junk(self):
        self.assertEqual(self.client.post(self.URL, "{nope", content_type="application/json").status_code, 400)
        self.assertEqual(self.post({"video_transport": "carrier-pigeon", "keep_open": "yes"}).status_code, 400)
        self.assertEqual(_config_mod.settings(), {})

    def test_echo_cancellation_comes_from_the_companion(self):
        """The aec URL is the supervisor's companion, not a setting."""
        self.assertEqual(self.post({"aec_url": "http://evil:1"}).status_code, 400)
        self.assertNotIn("aec_url", self.page())
        with patch.dict(os.environ, {"BDH_AEC_URL": "http://127.0.0.1:12200/"}):
            self.assertEqual(_config_mod.aec_url(), "http://127.0.0.1:12200")
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("BDH_AEC_URL", None)
            self.assertEqual(_config_mod.aec_url(), "")


# --- In the app container: everything goes through the hub broker ---


class RuntimeStartTests(unittest.TestCase):
    def test_container_starts_workers(self):
        with patch.dict("os.environ", {}, clear=False) as env:
            env.pop("BDH_CAMERAS_EVENTS", None)
            self.assertTrue(ev_mod.should_start(
                ["/usr/local/lib/python3.13/site-packages/bdh_sdk/runtime/__main__.py"]))
            self.assertFalse(ev_mod.should_start(
                ["/usr/local/lib/python3.13/site-packages/bdh_sdk/runtime/manage.py", "import_app_data"]))
            self.assertFalse(ev_mod.should_start(["/x/other/__main__.py", "migrate"]))

    def test_tables_ready(self):
        self.assertTrue(ev_mod.tables_ready())


class BrokerTests(TestCase):
    """The app with ``bdh_sdk.rpc`` pointed at a fake hub broker."""

    def setUp(self):
        from bdh_sdk import alerts, settings, storage
        from bdh_sdk.testing import FakeBroker

        saved = (settings._store, storage._backend, alerts._sender, alerts._dismisser)

        def restore():
            settings.set_store(saved[0])
            storage.set_state_backend(saved[1])
            alerts.set_sender(saved[2], saved[3])
        self.broker = FakeBroker(grants=("settings.*", "state.*", "alerts.*", "events.*", "secrets.*",
                                         "ha.get_all_states"), install=True, app_id="cameras")
        self.broker.start()
        self.addCleanup(restore)
        self.addCleanup(self.broker.stop)
        django_cache.clear()
        self.addCleanup(django_cache.clear)
        self.addCleanup(popups_mod.stop)

    def test_nvrs_from_state(self):
        from . import config
        self.broker.state[("cameras", "nvrs")] = [
            {"id": "2", "host": "10.0.0.2", "name": "Shop", "username": "viewer"},
            {"id": "3", "host": "10.0.0.3", "name": "Barn", "username": ""},  # no key: listed, but not a site
            {"id": "4", "host": ""}]
        self.broker.secrets.update({("cameras", "protect_api_key_2"): "k",
                                    ("cameras", "protect_password_2"): "pw"})
        config.forget_sites()
        self.addCleanup(config.forget_sites)
        with patch.object(config, "protect_sites", _real_protect_sites):
            self.assertEqual(config.protect_sites(), [
                {"id": "2", "host": "10.0.0.2", "api_key": "k", "name": "Shop",
                 "username": "viewer", "password": "pw",
                 "access_host": "", "access_token": "", "access_pin": "", "door_map": {}}])
            self.assertEqual([n["id"] for n in config.nvrs()], ["2", "3"])
            # Cached: no more secret reads within the minute.
            reads = len(self.broker.ops("secrets.get"))
            config.protect_sites()
            self.assertEqual(len(self.broker.ops("secrets.get")), reads)

    def _nvr_admin(self):
        from . import config
        config.forget_sites()
        self.addCleanup(config.forget_sites)
        for p in (patch.object(config, "protect_sites", _real_protect_sites),
                  patch("cameras.policy.is_admin", return_value=True),
                  patch("cameras.events.restart")):
            p.start()
            self.addCleanup(p.stop)
        self.broker.state[("cameras", "nvrs")] = []

    def _post(self, what, body):
        return self.client.post(f"/cameras/settings/nvrs/{what}/", json.dumps(body),
                                content_type="application/json")

    def test_nvr_pages_admin_only(self):
        self.assertEqual(self.client.get("/cameras/settings/nvrs/").status_code, 404)
        for what in ("save", "remove", "test", "access-test"):
            self.assertEqual(self._post(what, {}).status_code, 404)

    def test_nvr_add_edit_remove(self):
        self._nvr_admin()
        resp = self._post("save", {"name": "Home", "host": "https://10.0.0.1/protect", "api_key": "sekrit-key-1"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["nvrs"], [{"id": "1", "name": "Home", "host": "10.0.0.1", "username": "",
                                                "has_api_key": True, "has_password": False,
                                                "access_host": "", "has_access_token": False, "door_map": {}}])
        self.assertEqual(self.broker.secrets[("cameras", "protect_api_key_1")], "sekrit-key-1")

        # A blank key keeps the saved one; an account needs a password.
        resp = self._post("save", {"id": "1", "name": "Home", "host": "10.0.0.1", "username": "view"})
        self.assertEqual((resp.status_code, resp.json()["field"]), (400, "password"))
        resp = self._post("save", {"id": "1", "name": "House", "host": "10.0.0.1",
                                   "username": "view", "password": "pw"})
        self.assertEqual(resp.json()["nvrs"][0]["has_password"], True)
        self.assertEqual(self.broker.secrets[("cameras", "protect_api_key_1")], "sekrit-key-1")
        self.assertEqual(self.broker.secrets[("cameras", "protect_password_1")], "pw")
        # Dropping the account forgets its password.
        self._post("save", {"id": "1", "name": "House", "host": "10.0.0.1"})
        self.assertNotIn(("cameras", "protect_password_1"), self.broker.secrets)

        page = self.client.get("/cameras/settings/nvrs/")
        self.assertContains(page, "House")
        self.assertNotContains(page, "sekrit-key-1")

        resp = self._post("remove", {"id": "1"})
        self.assertEqual(resp.json()["nvrs"], [])
        self.assertNotIn(("cameras", "protect_api_key_1"), self.broker.secrets)
        self.assertEqual(self._post("remove", {"id": "1"}).status_code, 404)

    def test_nvr_save_rejects(self):
        self._nvr_admin()
        self._post("save", {"name": "Home", "host": "10.0.0.1", "api_key": "k"})
        for body, field in (
            ({"host": "10.0.0.2", "api_key": "k"}, "name"),
            ({"name": "Shop", "host": "not a host!", "api_key": "k"}, "host"),
            ({"name": "Shop", "host": "10.0.0.2"}, "api_key"),
            ({"name": "Shop", "host": "10.0.0.1", "api_key": "k"}, "host"),
            ({"name": "home", "host": "10.0.0.2", "api_key": "k"}, "name"),
        ):
            resp = self._post("save", body)
            self.assertEqual((resp.status_code, resp.json().get("field")), (400, field), body)
        self.assertEqual(self._post("save", {"id": "9", "name": "X", "host": "10.0.0.9"}).status_code, 404)

    def test_nvr_test(self):
        self._nvr_admin()
        self._post("save", {"name": "Home", "host": "10.0.0.1", "api_key": "saved"})

        def answer(status, data):
            resp = MagicMock(status_code=status, ok=status < 400)
            resp.json.return_value = data
            return resp

        with patch("cameras.protect_api._request", return_value=answer(200, [{}, {}])) as req:
            self.assertEqual(self._post("test", {"id": "1", "host": "10.0.0.1"}).json(),
                             {"ok": True, "cameras": 2})
            self.assertEqual(req.call_args.kwargs["headers"], {"X-API-KEY": "saved"})
        with patch("cameras.protect_api._request", return_value=answer(401, {})):
            self.assertEqual(self._post("test", {"host": "10.0.0.1", "api_key": "bad"}).json()["field"], "api_key")
        with patch("cameras.protect_api._request", return_value=answer(200, {"not": "cameras"})):
            self.assertFalse(self._post("test", {"host": "10.0.0.1", "api_key": "k"}).json()["ok"])
        with patch("cameras.protect_api._request", return_value=answer(200, [])), \
                patch("cameras.views._test_login", return_value=False):
            body = self._post("test", {"host": "10.0.0.1", "api_key": "k", "username": "u", "password": "p"}).json()
            self.assertEqual((body["ok"], body["field"]), (False, "password"))

    PIN = "ab" * 32

    def test_nvr_door_unlock_settings(self):
        """Door unlock is optional; a token needs the Access console's host,
        a blank token keeps the saved one, and turning it off forgets it all."""
        from . import config
        self._nvr_admin()
        self._post("save", {"name": "Home", "host": "10.0.0.5", "api_key": "k"})
        self.assertNotIn(("cameras", "access_token_1"), self.broker.secrets)
        self.assertEqual(config.protect_sites()[0]["door_map"], {})

        resp = self._post("save", {"id": "1", "name": "Home", "host": "10.0.0.5", "access_token": "tok"})
        self.assertEqual((resp.status_code, resp.json()["field"]), (400, "access_host"))
        resp = self._post("save", {"id": "1", "name": "Home", "host": "10.0.0.5", "access_host": "10.0.0.1"})
        self.assertEqual((resp.status_code, resp.json()["field"]), (400, "access_token"))

        door_map = {"cam1": {"id": "d1", "name": "Front Door"}, "bad": "junk"}
        with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN) as fetch:
            resp = self._post("save", {"id": "1", "name": "Home", "host": "10.0.0.5", "access_host": "10.0.0.1",
                                       "access_token": "tok", "door_map": door_map})
        self.assertEqual(resp.status_code, 200, resp.content)
        fetch.assert_called_once_with("10.0.0.1")
        row = resp.json()["nvrs"][0]
        self.assertEqual((row["access_host"], row["has_access_token"], row["door_map"]),
                         ("10.0.0.1", True, {"cam1": {"id": "d1", "name": "Front Door"}}))
        self.assertNotIn('"tok"', resp.content.decode())
        self.assertEqual(self.broker.secrets[("cameras", "access_token_1")], "tok")
        site = config.protect_sites()[0]
        self.assertEqual((site["access_host"], site["access_token"], site["access_pin"], site["door_map"]),
                         ("10.0.0.1", "tok", self.PIN, {"cam1": {"id": "d1", "name": "Front Door"}}))

        # A blank token keeps the saved one and the pinned certificate; no door_map keeps the map.
        with patch("cameras.views.access_api.fetch_pin") as fetch:
            resp = self._post("save", {"id": "1", "name": "House", "host": "10.0.0.5", "access_host": "10.0.0.1"})
        self.assertEqual(resp.status_code, 200)
        fetch.assert_not_called()
        self.assertEqual(self.broker.secrets[("cameras", "access_token_1")], "tok")
        self.assertEqual(config.nvrs()[0]["door_map"], {"cam1": {"id": "d1", "name": "Front Door"}})
        # Clearing the host while a token is saved is a mistake, not a removal.
        resp = self._post("save", {"id": "1", "name": "House", "host": "10.0.0.5", "access_host": ""})
        self.assertEqual((resp.status_code, resp.json()["field"]), (400, "access_host"))

        resp = self._post("save", {"id": "1", "name": "House", "host": "10.0.0.5", "access_remove": True})
        self.assertEqual(resp.status_code, 200)
        self.assertNotIn(("cameras", "access_token_1"), self.broker.secrets)
        self.assertEqual({k for k in config.nvrs()[0] if k.startswith("access") or k == "door_map"}, set())
        self.assertEqual(config.protect_sites()[0]["access_host"], "")

    def test_nvr_save_access_unreachable(self):
        from . import access_api
        self._nvr_admin()
        with patch("cameras.views.access_api.fetch_pin", side_effect=access_api.AccessError("unreachable")):
            resp = self._post("save", {"name": "Home", "host": "10.0.0.5", "api_key": "k",
                                       "access_host": "10.0.0.1", "access_token": "tok"})
        self.assertEqual((resp.status_code, resp.json()["field"]), (400, "access_host"))
        self.assertNotIn(("cameras", "access_token_1"), self.broker.secrets)

    def test_nvr_remove_forgets_access_token(self):
        self._nvr_admin()
        with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN):
            self._post("save", {"name": "Home", "host": "10.0.0.5", "api_key": "k",
                                "access_host": "10.0.0.1", "access_token": "tok"})
        self.assertIn(("cameras", "access_token_1"), self.broker.secrets)
        self._post("remove", {"id": "1"})
        self.assertNotIn(("cameras", "access_token_1"), self.broker.secrets)

    def test_nvr_access_test(self):
        from . import access_api, config
        self._nvr_admin()
        doors = [{"id": "d1", "name": "Front Door"}]
        with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN), \
                patch("cameras.views.access_api.list_doors", return_value=doors) as listed:
            body = self._post("access-test", {"access_host": "https://10.0.0.1:12445/", "access_token": "tok"}).json()
        self.assertEqual((body["ok"], body["doors"]), (True, doors))
        listed.assert_called_once_with("10.0.0.1:12445", "tok", self.PIN)

        # The tested certificate is the one saved, without fetching it again.
        with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN), \
                patch("cameras.views.access_api.list_doors", return_value=doors):
            self._post("access-test", {"access_host": "10.0.0.1", "access_token": "tok"})
        with patch("cameras.views.access_api.fetch_pin", side_effect=AssertionError("refetched")):
            resp = self._post("save", {"name": "Home", "host": "10.0.0.5", "api_key": "k",
                                       "access_host": "10.0.0.1", "access_token": "tok"})
        self.assertEqual(resp.status_code, 200, resp.content)
        self.assertEqual(config.nvrs()[0]["access_pin"], self.PIN)

        # A saved NVR tests with its saved token.
        with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN), \
                patch("cameras.views.access_api.list_doors", return_value=doors) as listed:
            self._post("access-test", {"id": "1", "access_host": "10.0.0.1"})
        self.assertEqual(listed.call_args.args[1], "tok")

        for err, field in (("unauthorized", "access_token"), ("unreachable", "access_host"),
                           ("bad_response", "access_host")):
            with patch("cameras.views.access_api.fetch_pin", return_value=self.PIN), \
                    patch("cameras.views.access_api.list_doors", side_effect=access_api.AccessError(err)):
                body = self._post("access-test", {"access_host": "10.0.0.1", "access_token": "bad"}).json()
            self.assertEqual((body["ok"], body["field"]), (False, field), err)
        self.assertEqual(self._post("access-test", {"access_host": "", "access_token": "t"}).json()["field"],
                         "access_host")
        self.assertEqual(self._post("access-test", {"access_host": "10.0.0.9"}).json()["field"], "access_token")

    def test_go2rtc_companion_wins(self):
        from . import config
        with patch.dict("os.environ", {"BDH_GO2RTC_URL": "http://127.0.0.1:11903"}):
            self.assertEqual(config.go2rtc_url(), "http://127.0.0.1:11903")

    def test_entity_picker_groups_devices_with_manifest_grants(self):
        """With only the manifest's permissions (as on a real hub), both instances' entities
        come back grouped under their devices and areas."""
        from bdh_sdk.testing import FakeBroker
        from .plugin import spec

        self.broker.stop()
        broker = FakeBroker.for_plugin(spec, install=True)
        broker.start()
        self.addCleanup(broker.stop)
        broker.instances = [{"id": "ha", "name": "Home", "site": "Mercer Island", "default": True},
                            {"id": "ha_bdl", "name": "Lodge", "site": "Sun Peaks", "default": False}]
        for inst, label in (("ha", "Front"), ("ha_bdl", "Lodge")):
            broker.ha_states[inst] = [
                {"entity_id": "binary_sensor.door", "state": "off",
                 "attributes": {"friendly_name": f"{label} Door", "device_class": "door"}}]
            broker.registries[inst] = {
                "entities": [{"entity_id": "binary_sensor.door", "device_id": "d"}],
                "devices": [{"id": "d", "name": f"{label}", "manufacturer": "Ring", "area_id": "porch"}],
                "areas": [{"area_id": "porch", "name": f"{label} Porch"}],
            }
        with patch("cameras.policy.is_admin", return_value=True):
            data = self.client.get("/cameras/settings/popups/entities/").json()
        self.assertEqual([(d["instance"], d["name"], d["area"]) for d in data["devices"]],
                         [("ha", "Front", "Front Porch"), ("ha_bdl", "Lodge", "Lodge Porch")])
        self.assertEqual([(e["instance"], e["device"], e["short"], e["area"]) for e in data["entities"]],
                         [("ha", "d", "Door", "Front Porch"), ("ha_bdl", "d", "Door", "Lodge Porch")])
        # Never the admin-only raw websocket op.
        self.assertEqual(broker.ops("ha.ws_commands"), [])

    def test_trigger_subscribes_and_pushes_alert(self):
        from bdh_sdk import events, rpc
        self.broker.state[("cameras", popups_mod.STATE_KEY)] = {
            "enabled": True,
            "cameras": {"cam1": {"kinds": [], "triggers": [{"entity_id": "binary_sensor.front_door"}]}}}
        cams = [{"cameras": [{"camera_id": "cam1", "name": "Front Door"}]}]
        with patch("cameras.protect_api.get_protect_cameras", return_value=cams):
            popups_mod.start()
            self.assertTrue(rpc.flush(5))
            self.assertEqual(self.broker.ops("events.subscribe")[-1],
                             {"patterns": ["ha/state/binary_sensor.front_door"]})
            events.deliver("ha/state/binary_sensor.front_door",
                           {"entity_id": "binary_sensor.front_door", "old": "off", "new": "on",
                            "attributes": {"friendly_name": "Front Door", "device_class": "door"}})
            events.flush(5)
            self.assertTrue(rpc.flush(5))
        pushed = self.broker.ops("alerts.push")
        self.assertEqual([(a["app"], a["key"], a["embed_url"]) for a in pushed],
                         [("cameras", "doorbell:cam1", "/cameras/popup/cam1/")])


class HubFrameLayoutTests(SimpleTestCase):
    """The app renders inside the hub's iframe, already below the hub navbar, so its
    CSS must not reserve space for that navbar (top:62px / 100vh-62px / phone 118px)
    nor scope rules under the hub's `.navbar`, which doesn't exist in the iframe."""

    def test_no_hub_navbar_offsets(self):
        import pathlib
        import re
        root = pathlib.Path(__file__).resolve().parent
        self.assertTrue((root / "templates").is_dir())
        pattern = re.compile(r"top\s*:\s*(6[0-9]|118)px|100vh\s*-\s*(6[0-9]|118)px|\.navbar(?![-\w])")
        hits = []
        for path in list(root.glob("templates/**/*.html")) + list(root.glob("static/**/*.css")):
            for n, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
                if pattern.search(line):
                    hits.append(f"{path.relative_to(root)}:{n}: {line.strip()}")
        self.assertEqual(hits, [])


# --- Guests: what anyone not signed in as an admin may see and do ---

from . import policy as policy_mod  # noqa: E402

GUEST_SITES = [
    {"id": "1", "name": "Home", "ok": True, "cameras": [
        {"camera_id": "pub", "name": "Porch", "stream_name": "porch", "rtsp_url": "rtsps://x",
         "is_ptz": True, "ptz_presets": 2, "has_mic": True, "has_speaker": True,
         "talkback": {"url": "rtp://1.2.3.4:7004", "codec": "opus", "rate": 24000}},
        {"camera_id": "shed", "name": "Shed", "stream_name": "shed", "rtsp_url": "rtsps://x"}]},
    {"id": "2", "name": "Cabin", "ok": True, "cameras": [
        {"camera_id": "priv", "name": "Cabin Door", "stream_name": "cabin_door", "rtsp_url": "rtsps://x",
         "is_ptz": True, "ptz_presets": 2, "has_speaker": True,
         "talkback": {"url": "rtp://1.2.3.5:7004", "codec": "opus", "rate": 24000}}]},
]


class GuestTestCase(TestCase):
    """Two NVRs; until the Guests page is saved, guests see only the first."""

    def setUp(self):
        from bdh_sdk.storage import app_state
        self.state = app_state("cameras")
        for key in (policy_mod.STATE_KEY, popups_mod.STATE_KEY):
            self.addCleanup(self.state.delete, key)
        self.is_admin = False
        for target, kw in (
            ("cameras.policy.is_admin", {"side_effect": lambda request: self.is_admin}),
            ("cameras.views.get_protect_cameras", {"return_value": GUEST_SITES}),
            ("cameras.protect_api.get_protect_cameras", {"return_value": GUEST_SITES}),
            ("cameras.config.protect_sites", {"return_value": [{"host": "10.0.0.1", "api_key": "k"}]}),
            ("cameras.views._register_streams_with_go2rtc", {}),
            ("cameras.views._cached_poster", {"return_value": None}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def guests(self, **cfg):
        cfg.setdefault("nvrs", {"1": {"default": "visible"}})
        return policy_mod.save(cfg)


class GuestPolicyTests(GuestTestCase):
    def visible(self):
        return policy_mod.visible_camera_ids()

    def test_first_nvr_until_saved(self):
        self.assertIsNone(policy_mod.stored())
        self.assertEqual(self.visible(), {"pub", "shed"})
        self.assertEqual((policy_mod.current()["talk"], policy_mod.current()["events"]), (True, "off"))
        # Whatever NVR is listed first, even one with no id.
        sites = [{"name": "Old", "cameras": [{"camera_id": "a"}]}, GUEST_SITES[0]]
        self.assertEqual(policy_mod.visible_camera_ids(sites=sites), {"a"})

    def test_nvrs_not_on_the_page_are_hidden(self):
        self.guests(nvrs={"2": {"default": "visible"}})
        self.assertEqual(self.visible(), {"priv"})
        self.guests(nvrs={})
        self.assertEqual(self.visible(), set())

    def test_camera_overrides(self):
        self.guests(nvrs={"1": {"default": "visible", "cameras": {"shed": "hidden"}},
                          "2": {"default": "hidden", "cameras": {"priv": "visible"}}})
        self.assertEqual(self.visible(), {"pub", "priv"})

    def test_clean(self):
        clean = policy_mod.clean
        self.assertEqual(clean({"events": "since"})["events"], "off")
        self.assertEqual(clean({"events": "since", "events_since": "nope"})["events"], "off")
        self.assertEqual(clean({"events": "forever", "talk": "no", "ptz": False}),
                         {"nvrs": {}, "talk": True, "ptz": False, "events": "off", "events_since": ""})
        # Overrides equal to the NVR's default aren't kept; junk is dropped.
        self.assertEqual(clean({"nvrs": {"1": {"default": "visible", "cameras": {
            "a": "visible", "b": "hidden", "c": "maybe"}}, "": {}, "3": "x"}})["nvrs"],
            {"1": {"default": "visible", "cameras": {"b": "hidden"}}})
        # A naive time is the hub's local time.
        since = clean({"events": "since", "events_since": "2026-05-24T18:00"})["events_since"]
        self.assertEqual(datetime.fromisoformat(since),
                         dj_tz.make_aware(datetime(2026, 5, 24, 18, 0), dj_tz.get_current_timezone()))

    def test_admins_can_do_everything(self):
        self.guests(nvrs={}, talk=False, ptz=False)
        self.is_admin = True
        request = MagicMock()
        self.assertTrue(policy_mod.can_view(request, "priv"))
        self.assertTrue(policy_mod.can_talk(request) and policy_mod.can_ptz(request))
        self.assertTrue(policy_mod.can_see_events(request))
        self.assertIsNone(policy_mod.event_window(request))

    def test_homepage_counts_what_guests_see(self):
        from bdh_sdk import OWNER
        from .homepage import status
        with patch("cameras.homepage.get_protect_cameras", return_value=GUEST_SITES):
            self.assertEqual(status().text, "2 cameras")
            self.assertEqual(status(mode=OWNER).text, "3 cameras")
            self.guests(nvrs={"2": {"default": "visible"}})
            self.assertEqual(status().text, "1 camera")

    def test_push_topics(self):
        from .views import push_topics
        request = MagicMock()
        self.assertEqual(push_topics(request), [])
        self.guests(events="24h")
        self.assertEqual(push_topics(request), [ev_mod.CHANGED_TOPIC])
        self.is_admin = True
        self.assertEqual(push_topics(request), ["cameras/#"])


class GuestEndpointTests(GuestTestCase):
    """Every route serving camera data checks the guest rules.

    A new route has to be listed in ROUTES (with how a guest asks it for a
    camera) or EXEMPT, so one can't be added without thinking about guests.
    """
    EXEMPT = {
        "camera_rtc_diag", "camera_rtc_events",  # browser diagnostics posted to the log
        "camera_go2rtc_script",  # go2rtc's player code
    }
    SETTINGS = {"cameras_settings", "cameras_popups", "cameras_guests", "cameras_popup_entities",
                "cameras_nvrs", "cameras_nvr_save", "cameras_nvr_remove", "cameras_nvr_test",
                "cameras_nvr_access_test"}
    # Admins only, for every camera (tested in DoorUnlockViewTests).
    ADMIN_ONLY = {"door_unlock"}

    def setUp(self):
        super().setUp()
        self.guests(events="24h")
        for camera_id in ("pub", "priv"):
            _ev(f"ev_{camera_id}", ["person"], minutes_ago=5, camera_id=camera_id, thumb=b"jpg")
        for target, kw in (
            ("cameras.views.ptz_goto_preset", {"return_value": True}),
            ("cameras.views.refresh_talkback", {"side_effect": self._camera}),
            ("cameras.views._sync_streams", {}),
            ("cameras.frames.live_get", {"return_value": b"img"}),
            ("cameras.views.clips.get", {"return_value": None}),
            ("cameras.routing.wsrelay.relay", {"new_callable": AsyncMock, "return_value": True}),
            ("cameras.routing.config.go2rtc_url", {"return_value": "http://127.0.0.1:11900"}),
            ("cameras.routing.config.aec_url", {"return_value": ""}),
        ):
            p = patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    @staticmethod
    def _camera(camera_id):
        return next(c for s in GUEST_SITES for c in s["cameras"] if c["camera_id"] == camera_id)

    def _feed(self, camera_id):
        html = self.client.get("/cameras/").content.decode()
        return 200 if f'data-camera-id="{camera_id}"' in html else 404

    def _events(self, camera_id):
        resp = self.client.get("/cameras/events/")
        if resp.status_code != 200:
            return resp.status_code
        return 200 if camera_id in [e["camera_id"] for e in resp.json()["events"]] else 404

    def _websocket(self, route, src):
        """Open one of cameras.routing's websockets: 200 if relayed, else its close code."""
        from asgiref.sync import async_to_sync
        from django.contrib.auth.models import AnonymousUser
        sent, inbox = [], [{"type": "websocket.connect"}]

        async def receive():
            return inbox.pop(0) if inbox else {"type": "websocket.disconnect", "code": 1000}

        async def send(message):
            sent.append(message)

        calls = self.relay.call_count
        scope = {"type": "websocket", "path": "/", "user": AnonymousUser(),
                 "query_string": urllib.parse.urlencode({"src": src}).encode()}
        async_to_sync(route)(scope, receive, send)
        if self.relay.call_count > calls:
            return 200
        return 403 if sent == [{"type": "websocket.close", "code": 4403}] else sent

    STREAMS = {"pub": "porch", "priv": "cabin_door"}

    def _watch(self, camera_id):
        from .routing import watch
        return self._websocket(watch, self.STREAMS[camera_id])

    def _talk(self, camera_id):
        from .routing import talk
        resp = self.client.post(f"/cameras/api/talk/prepare/?camera={camera_id}")
        if resp.status_code != 200:
            return resp.status_code
        return self._websocket(talk, resp.json()["stream"])

    def _ptz(self, camera_id):
        return self.client.post("/cameras/ptz/goto/", json.dumps({"camera_id": camera_id, "slot": 0}),
                                content_type="application/json").status_code

    ROUTES = {
        "cameras": _feed,
        "camera_events": _events,
        "camera_event_thumb": lambda self, cid: self.client.get(f"/cameras/events/ev_{cid}/thumb/").status_code,
        "camera_snapshot": lambda self, cid: self.client.get(f"/cameras/snapshot/{cid}/").status_code,
        # The clip cache is mocked empty: 404 means the request got past the checks.
        "camera_clip": lambda self, cid: (lambda s: 200 if s == 404 else s)(self.client.get(
            f"/cameras/clip/{cid}/?start={int(time.time()) - 60}").status_code),
        "camera_popup": lambda self, cid: self.client.get(f"/cameras/popup/{cid}/").status_code,
        "camera_talk_prepare": lambda self, cid: self.client.post(
            f"/cameras/api/talk/prepare/?camera={cid}").status_code,
        "ptz_goto": _ptz,
        "camera_watch_ws": _watch,
        "camera_talk_ws": _talk,
    }

    def test_every_route_is_classified(self):
        from .routing import websocket_urlpatterns
        from .urls import urlpatterns
        names = {p.name for p in urlpatterns + websocket_urlpatterns}
        self.assertEqual(names - set(self.ROUTES) - self.EXEMPT - self.SETTINGS - self.ADMIN_ONLY, set())

    def test_watch_relays_only_the_viewers_streams(self):
        """go2rtc gets just ``src``, on loopback; other streams of hidden cameras and
        talk streams are refused."""
        from .routing import watch
        self.assertEqual(self._websocket(watch, "porch_low"), 200)
        self.relay.assert_called_with(ANY, ANY, "ws://127.0.0.1:11900/api/ws?src=porch_low")
        for src in ("cabin_door_low", "porch_talk", "unknown", "", "porch&src=cabin_door"):
            with self.subTest(src=src):
                self.assertEqual(self._websocket(watch, src), 403)
        self.is_admin = True
        self.assertEqual(self._websocket(watch, "cabin_door_low"), 200)

    def test_talk_tokens_are_single_use(self):
        from . import routing
        token = self.client.post("/cameras/api/talk/prepare/?camera=pub").json()["stream"]
        self.assertNotIn("porch", token)
        self.assertEqual(self._websocket(routing.talk, token), 200)
        self.relay.assert_called_with(ANY, ANY, "ws://127.0.0.1:11900/api/talk?src=porch_talk")
        self.assertEqual(self._websocket(routing.talk, token), 403)
        self.assertEqual(self._websocket(routing.talk, "porch_talk"), 403)
        token = routing.issue_talk("porch_talk")
        routing._talks[token] = ("porch_talk", time.monotonic() - routing.TALK_TTL - 1)
        self.assertEqual(self._websocket(routing.talk, token), 403)

    def test_talk_goes_through_echo_canceller(self):
        from .routing import talk
        self.aec_url.return_value = "http://127.0.0.1:12200"
        token = self.client.post("/cameras/api/talk/prepare/?camera=pub").json()["stream"]
        self.assertEqual(self._websocket(talk, token), 200)
        self.relay.assert_called_with(ANY, ANY, "ws://127.0.0.1:12200/api/talk?src=porch_talk")

    def test_pages_use_the_relay(self):
        """Browsers never get go2rtc's address."""
        self.is_admin = True
        for url in ("/cameras/", "/cameras/popup/pub/"):
            with self.subTest(url=url):
                html = self.client.get(url).content.decode()
                self.assertIn("/cameras/api/ws", html)
                self.assertIn("/cameras/go2rtc/video-stream.js", html)
                self.assertNotIn("11900", html)

    def test_guests_only_reach_cameras_they_see(self):
        for name, ask in self.ROUTES.items():
            with self.subTest(route=name):
                self.assertEqual(ask(self, "pub"), 200, "visible camera")
                self.assertIn(ask(self, "priv"), (403, 404), "hidden camera")
        self.ptz_goto_preset.assert_called_once_with("pub", 0)
        # Once from camera_talk_prepare, once from camera_talk_ws's own prepare.
        self.assertEqual({c.args[0] for c in self.refresh_talkback.call_args_list}, {"pub"})

    def test_admins_reach_every_camera(self):
        self.is_admin = True
        for name, ask in self.ROUTES.items():
            with self.subTest(route=name):
                self.assertEqual(ask(self, "priv"), 200)

    def test_settings_are_admins_only(self):
        from django.urls import reverse
        for name in self.SETTINGS:
            with self.subTest(route=name):
                url = reverse(name)
                self.assertEqual(self.client.get(url).status_code, 404)
                self.assertEqual(self.client.post(url, "{}", content_type="application/json").status_code, 404)

    def test_events_off_hides_history(self):
        self.guests(events="off")
        for name in ("camera_events", "camera_event_thumb", "camera_snapshot", "camera_clip"):
            with self.subTest(route=name):
                self.assertEqual(self.ROUTES[name](self, "pub"), 403)
        html = self.client.get("/cameras/").content.decode()
        self.assertNotIn('class="ev-rail"', html)
        self.assertNotIn('<div class="cam-scrub">', html)

    def test_talk_and_ptz_switches(self):
        self.guests(talk=False, ptz=False)
        self.assertEqual(self.ROUTES["camera_talk_prepare"](self, "pub"), 404)
        self.assertEqual(self._talk("pub"), 404)
        self.assertEqual(self._ptz("pub"), 403)
        self.refresh_talkback.assert_not_called()
        self.ptz_goto_preset.assert_not_called()
        resp = self.client.get("/cameras/")
        stream = resp.context["sites"][0]["streams"][0]
        self.assertEqual((stream["talk_stream"], stream["is_ptz"], stream["ptz_presets"]), ("", False, 0))
        popup = self.client.get("/cameras/popup/pub/").content.decode()
        self.assertNotIn("data-talk-stream", popup)
        self.assertNotIn('class="cam-talk"', popup)
        self.assertIn('class="cam-spk"', popup)
        self.guests()
        self.assertIn('data-talk-stream="porch_talk"', self.client.get("/cameras/popup/pub/").content.decode())
        self.assertEqual(self.client.get("/cameras/").context["sites"][0]["streams"][0]["is_ptz"], True)


class GuestEventWindowTests(GuestTestCase):
    def setUp(self):
        super().setUp()
        _ev("new", ["person"], minutes_ago=10, camera_id="pub", thumb=b"jpg")
        _ev("old", ["person"], minutes_ago=120, camera_id="pub", thumb=b"jpg")
        self.client_mock = MagicMock()
        self.client_mock.get_camera_snapshot.return_value = None
        for target, kw in (
            ("cameras.views.events.run", {"side_effect": lambda cam, fn, **kw: fn(self.client_mock)}),
            ("cameras.views.clips.get", {"return_value": None}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)

    def ids(self):
        return [e["id"] for e in self.client.get("/cameras/events/").json()["events"]]

    def since(self, minutes_ago):
        local = dj_tz.localtime(dj_tz.now() - timedelta(minutes=minutes_ago)).replace(tzinfo=None)
        return self.guests(events="since", events_since=local.isoformat())

    def test_last_24_hours(self):
        self.guests(events="24h")
        self.assertEqual(self.ids(), ["new", "old"])
        day_ago = int(time.time()) - 25 * 3600
        self.assertEqual(self.client.get(f"/cameras/snapshot/pub/?ts={day_ago}").status_code, 403)
        self.assertEqual(self.client.get(f"/cameras/clip/pub/?start={day_ago}").status_code, 403)
        self.assertEqual(self.client.get(f"/cameras/snapshot/pub/?ts={day_ago + 7200}").status_code, 404)
        resp = self.client.get("/cameras/")
        self.assertAlmostEqual(resp.context["events_from"] / 1000, time.time() - 24 * 3600, delta=60)

    def test_since(self):
        self.since(60)
        self.assertEqual(self.ids(), ["new"])
        self.assertEqual(self.client.get("/cameras/events/new/thumb/").status_code, 200)
        self.assertEqual(self.client.get("/cameras/events/old/thumb/").status_code, 404)
        before = int(time.time()) - 90 * 60
        after = int(time.time()) - 30 * 60
        self.assertEqual(self.client.get(f"/cameras/clip/pub/?start={before}").status_code, 403)
        self.assertEqual(self.client.get(f"/cameras/clip/pub/?start={after}").status_code, 404)
        self.assertEqual(self.client.get(f"/cameras/snapshot/pub/?ts={before}").status_code, 403)
        self.assertEqual(self.client.get(f"/cameras/snapshot/pub/?ts={after}").status_code, 404)
        self.assertAlmostEqual(self.client.get("/cameras/").context["events_from"] / 1000,
                               time.time() - 3600, delta=60)

    def test_admins_see_everything_recorded(self):
        self.since(60)
        self.is_admin = True
        self.assertEqual(self.ids(), ["new", "old"])
        self.assertEqual(self.client.get("/cameras/events/old/thumb/").status_code, 200)
        old = int(time.time()) - 3 * 24 * 3600
        self.assertEqual(self.client.get(f"/cameras/clip/pub/?start={old}").status_code, 404)
        self.assertEqual(self.client.get(f"/cameras/clip/priv/?start={old}").status_code, 404)
        self.assertEqual(self.client.get("/cameras/").context["events_from"], 0)


class GuestPopupTests(GuestTestCase):
    def setUp(self):
        super().setUp()
        p = patch("bdh_sdk.alerts.push")
        self.push = p.start()
        self.addCleanup(p.stop)

    def test_audience(self):
        admins_only = popups_mod.camera_admins_only
        self.assertEqual((admins_only("pub"), admins_only("priv")), (False, True))
        popups_mod.save({"cameras": {
            "pub": {"kinds": ["person"], "audience": "admins"},
            "shed": {"kinds": ["person"], "audience": "everyone"},
            "priv": {"kinds": ["person"], "audience": "everyone"}}})
        self.assertEqual(popups_mod.stored()["cameras"]["pub"]["audience"], "admins")
        self.assertNotIn("audience", popups_mod.stored()["cameras"]["shed"])
        # Cameras guests can't see never pop up for them, whatever was picked.
        self.assertEqual([admins_only(c) for c in ("pub", "shed", "priv")], [True, False, True])

    def test_push_sets_admins_only(self):
        event = MagicMock(topic="ha/state/lock.gate", data={"entity_id": "lock.gate", "new": "unlocked",
                                                            "attributes": {"friendly_name": "Gate"}})
        popups_mod._push(event, {"entity_id": "lock.gate"}, "pub")
        popups_mod._push(event, {"entity_id": "lock.gate"}, "priv")
        self.assertEqual([(c.args[0].key, c.args[0].admins_only) for c in self.push.call_args_list],
                         [("doorbell:pub", False), ("doorbell:priv", True)])

    def test_settings_lock_hidden_cameras(self):
        self.is_admin = True
        page = self.client.get("/cameras/settings/popups/").context["page"]
        hidden = {c["id"]: c["guest_hidden"] for s in page["sites"] for c in s["cameras"]}
        self.assertEqual(hidden, {"pub": False, "shed": False, "priv": True})


class GuestSettingsViewTests(GuestTestCase):
    URL = "/cameras/settings/guests/"

    def setUp(self):
        super().setUp()
        self.is_admin = True
        p = patch("cameras.views._nvr_rows", return_value=[
            {"id": "1", "name": "Home", "host": "10.0.0.1", "username": "", "has_api_key": True,
             "has_password": False}])
        p.start()
        self.addCleanup(p.stop)

    def post(self, body):
        return self.client.post(self.URL, json.dumps(body), content_type="application/json")

    def test_page_shows_the_rules_in_effect(self):
        page = self.client.get(self.URL).context["page"]
        self.assertEqual([(s["id"], s["default"], [(c["id"], c["visible"]) for c in s["cameras"]])
                          for s in page["sites"]],
                         [("1", "visible", [("pub", True), ("shed", True)]), ("2", "hidden", [("priv", False)])])
        self.assertEqual((page["config"]["talk"], page["config"]["events"]), (True, "off"))

    def test_save(self):
        resp = self.post({"nvrs": {"1": {"default": "visible", "cameras": {"shed": "hidden"}},
                                   "2": {"default": "hidden"}},
                          "talk": False, "events": "since", "events_since": "2026-05-24T18:00"})
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp.json()["events_since"], "2026-05-24T18:00")
        cfg = policy_mod.stored()
        self.assertEqual((cfg["talk"], cfg["ptz"], cfg["events"]), (False, True, "since"))
        self.assertEqual(policy_mod.visible_camera_ids(), {"pub"})
        page = self.client.get(self.URL).context["page"]
        self.assertEqual(page["sites"][0]["cameras"][1], {"id": "shed", "name": "Shed", "visible": False})
        self.assertEqual(page["config"]["events_since"], "2026-05-24T18:00")

    def test_rejects_junk_and_guests(self):
        self.assertEqual(self.client.post(self.URL, "{nope", content_type="application/json").status_code, 400)
        self.assertIsNone(policy_mod.stored())
        self.is_admin = False
        self.assertEqual(self.client.get(self.URL).status_code, 404)
        self.assertEqual(self.post({"nvrs": {}}).status_code, 404)
        self.assertIsNone(policy_mod.stored())

    def test_main_settings_summary(self):
        self.guests(ptz=False, events="24h")
        resp = self.client.get("/cameras/settings/")
        self.assertEqual(resp.context["page"]["guests"],
                         {"visible": 2, "cameras": 3, "talk": True, "ptz": False, "events": "24h", "since": ""})
        self.assertContains(resp, 'href="/cameras/settings/guests/"')

# --- Door unlock (UniFi Access) ---

from . import access_api  # noqa: E402

DOOR_PIN = "ab" * 32
DOOR_SITES = [
    {"id": "1", "name": "Home", "host": "10.0.0.5", "api_key": "k", "access_host": "10.0.0.1",
     "access_token": "tok", "access_pin": DOOR_PIN,
     "door_map": {"pub": {"id": "d1", "name": "Front Door"}}},
    {"id": "2", "name": "Cabin", "host": "10.0.0.6", "api_key": "k", "access_host": "",
     "access_token": "", "access_pin": "", "door_map": {}},
]


class DoorUnlockViewTests(TestCase):
    def setUp(self):
        from django.test import RequestFactory
        self.factory = RequestFactory()
        self.is_admin = True
        self.sites = [dict(s) for s in DOOR_SITES]
        django_cache.clear()
        self.addCleanup(django_cache.clear)
        for target, kw in (
            ("cameras.policy.is_admin", {"side_effect": lambda request: self.is_admin}),
            ("cameras.config.protect_sites", {"side_effect": lambda: [dict(s) for s in self.sites]}),
            ("cameras.views.access_api.unlock", {"return_value": True}),
            ("cameras.views._hub_label", {"return_value": "Mercer Island"}),
            ("bdh_sdk.events.publish", {}),
        ):
            p = patch(target, **kw)
            setattr(self, target.rsplit(".", 1)[1], p.start())
            self.addCleanup(p.stop)

    def post(self, body, user="stefan"):
        from .views import door_unlock
        request = self.factory.post("/cameras/door/unlock/", json.dumps(body), content_type="application/json")
        request.user = MagicMock(username=user)
        resp = door_unlock(request)
        return resp.status_code, json.loads(resp.content)

    def test_guests_are_refused(self):
        self.is_admin = False
        self.assertEqual(self.post({"camera_id": "pub"})[0], 403)
        self.unlock.assert_not_called()

    def test_guests_are_refused_over_http(self):
        self.is_admin = False
        resp = self.client.post("/cameras/door/unlock/", json.dumps({"camera_id": "pub"}),
                                content_type="application/json")
        self.assertEqual(resp.status_code, 403)
        self.unlock.assert_not_called()

    def test_post_only(self):
        self.assertEqual(self.client.get("/cameras/door/unlock/").status_code, 405)

    def test_unmapped_camera(self):
        self.assertEqual(self.post({"camera_id": "shed"})[0], 404)
        self.assertEqual(self.post({})[0], 400)
        # A site without Access never unlocks, even with a stale map.
        self.sites[0]["access_token"] = ""
        self.assertEqual(self.post({"camera_id": "pub"})[0], 404)
        self.unlock.assert_not_called()

    def test_unlock_uses_the_server_side_door(self):
        status, body = self.post({"camera_id": "pub", "door_id": "evil", "host": "6.6.6.6"})
        self.assertEqual((status, body), (200, {"ok": True, "door": "Front Door"}))
        args = self.unlock.call_args.args
        self.assertEqual(args[:3], ("10.0.0.1", "tok", "d1"))
        self.assertEqual(args[5], DOOR_PIN)
        self.assertEqual(args[3], "bdh:stefan")
        self.assertIn("stefan", args[4])
        self.assertIn("Mercer Island", args[4])
        topic, data = self.publish.call_args.args
        self.assertEqual(topic, "cameras/door/unlocked")
        self.assertEqual(data["message"], "Front Door (Home) unlocked by stefan")
        self.assertNotIn("tok", json.dumps(data))

    def test_access_errors(self):
        for kind, status in (("unauthorized", 502), ("unreachable", 504), ("not_found", 502),
                             ("pin_mismatch", 502)):
            with self.subTest(kind=kind):
                self.unlock.side_effect = access_api.AccessError(kind)
                code, body = self.post({"camera_id": "pub"})
                self.assertEqual((code, body["ok"]), (status, False))
                self.assertNotIn("maybe_unlocked", body)
        self.publish.assert_not_called()

    def test_timeout_may_have_unlocked(self):
        self.unlock.side_effect = access_api.AccessError("timeout")
        code, body = self.post({"camera_id": "pub"})
        self.assertEqual((code, body["ok"], body["maybe_unlocked"]), (504, False, True))
        self.assertIn("may have unlocked", body["error"])
        self.assertEqual(self.unlock.call_count, 1)  # never retried
        self.assertEqual(self.publish.call_args.args[1]["result"], "timeout")

    def test_repeat_after_success_is_not_sent_again(self):
        self.assertEqual(self.post({"camera_id": "pub"})[1]["ok"], True)
        status, body = self.post({"camera_id": "pub"})
        self.assertEqual((status, body["ok"], body["duplicate"]), (200, True, True))
        self.assertEqual(self.unlock.call_count, 1)

    def test_repeat_after_failure_tries_again(self):
        self.unlock.side_effect = access_api.AccessError("unreachable")
        self.assertEqual(self.post({"camera_id": "pub"})[1]["ok"], False)
        self.unlock.side_effect = None
        self.assertEqual(self.post({"camera_id": "pub"}), (200, {"ok": True, "door": "Front Door"}))
        self.assertEqual(self.unlock.call_count, 2)

    def test_notification_failure_doesnt_fail_the_unlock(self):
        self.publish.side_effect = RuntimeError("broker down")
        self.assertEqual(self.post({"camera_id": "pub"}), (200, {"ok": True, "door": "Front Door"}))


class DoorButtonTemplateTests(GuestTestCase):
    """Only admins see the button, only for mapped cameras on NVRs with Access."""

    def setUp(self):
        super().setUp()
        self.guests()
        self.sites = [dict(s) for s in DOOR_SITES]
        p = patch("cameras.config.protect_sites", side_effect=lambda: [dict(s) for s in self.sites])
        p.start()
        self.addCleanup(p.stop)

    def feed(self):
        return self.client.get("/cameras/").content.decode()

    def popup(self):
        return self.client.get("/cameras/popup/pub/").content.decode()

    def test_guests_never_see_it(self):
        for html in (self.feed(), self.popup()):
            self.assertNotIn('class="cam-unlock"', html)
            self.assertNotIn("Front Door", html)

    def test_admins_see_it_on_mapped_cameras(self):
        self.is_admin = True
        html = self.feed()
        self.assertEqual(html.count('class="cam-unlock"'), 1)
        self.assertIn('data-door="Front Door"', html)
        popup = self.popup()
        self.assertIn('class="cam-unlock"', popup)
        self.assertIn('data-door="Front Door"', popup)
        self.assertNotIn('class="cam-unlock"', self.client.get("/cameras/popup/shed/").content.decode())

    def test_no_access_settings_no_button(self):
        self.is_admin = True
        self.sites[0].update(access_host="", access_token="")
        self.assertNotIn('class="cam-unlock"', self.feed())
        self.assertNotIn('class="cam-unlock"', self.popup())

    def test_guest_after_admin_has_no_door(self):
        self.is_admin = True
        self.assertIn("Front Door", self.feed())
        self.is_admin = False
        resp = self.client.get("/cameras/")
        self.assertNotIn("Front Door", resp.content.decode())
        self.assertTrue(all(not s.get("door_name") for site in resp.context["sites"] for s in site["streams"]))


class AccessApiTests(SimpleTestCase):
    """Against a local HTTPS server with a self-signed certificate."""

    @classmethod
    def setUpClass(cls):
        super().setUpClass()
        import http.server
        import ssl
        import subprocess
        import tempfile
        import threading
        from pathlib import Path

        cls.tmp = tempfile.TemporaryDirectory()
        cert, key = Path(cls.tmp.name, "c.pem"), Path(cls.tmp.name, "k.pem")
        try:
            subprocess.run(["openssl", "req", "-x509", "-newkey", "rsa:2048", "-nodes", "-days", "1",
                            "-subj", "/CN=access", "-keyout", str(key), "-out", str(cert)],
                           check=True, capture_output=True, timeout=60)
        except (OSError, subprocess.SubprocessError):
            cls.tmp.cleanup()
            raise unittest.SkipTest("openssl not available")
        cls.der = ssl.PEM_cert_to_DER_cert(cert.read_text())
        cls.requests = []
        test = cls

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *a):
                pass

            def _reply(self):
                length = int(self.headers.get("Content-Length") or 0)
                test.requests.append((self.command, self.path, self.headers.get("Authorization"),
                                      self.rfile.read(length) if length else b""))
                if self.headers.get("Authorization") != "Bearer good":
                    status, body = 401, {"code": "CODE_UNAUTHORIZED", "msg": "no"}
                elif self.path.endswith("/doors"):
                    status, body = 200, {"code": "SUCCESS", "data": [
                        {"id": "d2", "name": "Side Door", "door_lock_relay_status": "lock"},
                        {"id": "d1", "name": "Front Door", "full_name": "Home - Front Door"},
                        {"name": "no id"}]}
                elif self.path.endswith("/doors/gone/unlock"):
                    status, body = 200, {"code": "CODE_NOT_EXISTS", "msg": "no"}
                else:
                    status, body = 200, {"code": "SUCCESS", "data": None}
                raw = json.dumps(body).encode()
                self.send_response(status)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            do_GET = do_PUT = _reply

        cls.server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        cls.server.handle_error = lambda *a: None  # fetch_pin hangs up after the handshake
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.load_cert_chain(str(cert), str(key))
        cls.server.socket = ctx.wrap_socket(cls.server.socket, server_side=True)
        cls.host = f"127.0.0.1:{cls.server.server_address[1]}"
        threading.Thread(target=cls.server.serve_forever, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.shutdown()
        cls.server.server_close()
        cls.tmp.cleanup()
        super().tearDownClass()

    def setUp(self):
        self.requests.clear()

    def test_pin_and_doors(self):
        import hashlib
        pin = access_api.fetch_pin(self.host)
        self.assertEqual(pin, hashlib.sha256(self.der).hexdigest())
        self.assertEqual(access_api.list_doors(self.host, "good", pin),
                         [{"id": "d1", "name": "Front Door"}, {"id": "d2", "name": "Side Door"}])
        self.assertEqual(self.requests[-1][:3], ("GET", "/api/v1/developer/doors", "Bearer good"))

    def test_pin_mismatch(self):
        with self.assertRaises(access_api.AccessError) as err:
            access_api.list_doors(self.host, "good", "00" * 32)
        self.assertEqual(err.exception.kind, "pin_mismatch")
        self.assertEqual(self.requests, [])
        with self.assertRaises(access_api.AccessError) as err:
            access_api.unlock(self.host, "good", "d1", "a", "b", "")
        self.assertEqual(err.exception.kind, "pin_mismatch")

    def test_unlock(self):
        pin = access_api.fetch_pin(self.host)
        self.assertTrue(access_api.unlock(self.host, "good", "d1", "bdh:stefan", "BDH \u2013 stefan", pin))
        method, path, _, body = self.requests[-1]
        self.assertEqual((method, path), ("PUT", "/api/v1/developer/doors/d1/unlock"))
        self.assertEqual(json.loads(body), {"actor_id": "bdh:stefan", "actor_name": "BDH \u2013 stefan"})
        for token, door, kind in (("bad", "d1", "unauthorized"), ("good", "gone", "not_found")):
            with self.assertRaises(access_api.AccessError) as err:
                access_api.unlock(self.host, token, door, "a", "b", pin)
            self.assertEqual(err.exception.kind, kind)

    def test_unreachable(self):
        import socket
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        with self.assertRaises(access_api.AccessError) as err:
            access_api.fetch_pin(f"127.0.0.1:{port}")
        self.assertEqual(err.exception.kind, "unreachable")
        with self.assertRaises(access_api.AccessError) as err:
            access_api.unlock(f"127.0.0.1:{port}", "good", "d1", "a", "b", DOOR_PIN)
        self.assertEqual(err.exception.kind, "unreachable")

    def test_parse_doors_rejects_junk(self):
        for body in (None, {}, {"data": "x"}):
            with self.assertRaises(access_api.AccessError):
                access_api.parse_doors(body)
