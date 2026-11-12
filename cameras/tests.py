import json
import unittest
import urllib.request
import urllib.parse
from django.test import TestCase, Client, override_settings, tag
from django.contrib.staticfiles.testing import StaticLiveServerTestCase
from django.contrib.auth.models import User
from unittest.mock import patch, MagicMock, call
from .views import get_go2rtc_streams, _register_streams_with_go2rtc
from django.core.cache import cache as django_cache
from .protect_api import (
    get_protect_cameras, clear_cache, _camera_name_to_stream_name,
    _fetch_cameras_from_site, _is_ptz_camera, ptz_goto_preset,
    PTZ_DEFAULT_PRESETS, CACHE_KEY_PREFIX, INCOMPLETE_CACHE_TTL,
)


class CameraConfigTests(TestCase):
    def test_protect_sites_from_env(self):
        from django.conf import settings
        from . import config
        env = {
            "UNIFI_PROTECT_HOST_1": "10.0.0.1", "UNIFI_PROTECT_API_KEY_1": "k1",
            "BDH_CAMERAS_PROTECT_HOST_2": "10.0.0.2", "BDH_CAMERAS_PROTECT_API_KEY_2": "k2",
            "BDH_CAMERAS_PROTECT_NAME_2": "Shop",
            "BDH_CAMERAS_PROTECT_USERNAME_2": "viewer", "BDH_CAMERAS_PROTECT_PASSWORD_2": "pw",
            "UNIFI_PROTECT_HOST_3": "10.0.0.3",  # no key: ignored
        }
        with self.settings(), patch.dict("os.environ", env):
            if hasattr(settings, "UNIFI_PROTECT_SITES"):
                del settings.UNIFI_PROTECT_SITES
            self.assertEqual(config.protect_sites(), [
                {"host": "10.0.0.1", "api_key": "k1", "name": "Site 1", "username": "", "password": ""},
                {"host": "10.0.0.2", "api_key": "k2", "name": "Shop", "username": "viewer", "password": "pw"},
            ])

    @override_settings(UNIFI_PROTECT_SITES=[{"host": "h", "api_key": "k", "name": "S"}])
    def test_settings_win(self):
        from . import config
        with patch.dict("os.environ", {"UNIFI_PROTECT_HOST_1": "x", "UNIFI_PROTECT_API_KEY_1": "y"}):
            self.assertEqual(config.protect_sites(), [{"host": "h", "api_key": "k", "name": "S"}])


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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
)
class FetchCamerasFromSiteTests(TestCase):
    """Tests for _fetch_cameras_from_site."""

    def setUp(self):
        clear_cache()

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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
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

    @override_settings(UNIFI_PROTECT_SITES=[])
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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
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
        # Uses the correct video-stream custom element (not video-rtc)
        self.assertIn("<video-stream", content)
        self.assertNotIn("<video-rtc", content)
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
        # Stream mode must include MSE — go2rtc needs MSE initialized
        # alongside WebRTC for the WebSocket negotiation to work.
        # Removing MSE breaks video loading entirely (PR #45 regression).
        self.assertIn('data-mode="webrtc,mse,mp4"', content)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_stream_mode_includes_mse(self, mock_cameras, mock_register):
        """Stream mode must be 'webrtc,mse,mp4' — MSE is required.

        go2rtc uses an if/else-if chain in onopen() to pick the first video
        protocol (mse > hls > mp4), then adds WebRTC independently. Without
        MSE, MP4 + WebRTC compete and fail to deliver video. MSE acts as
        a graceful placeholder that loses the race to WebRTC harmlessly.
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

        self.assertIn('data-mode="webrtc,mse,mp4"', content)
        # Ensure MSE was not accidentally removed
        self.assertNotIn('data-mode="webrtc,mp4"', content)

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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'k', 'name': 'Sun Peaks'}],
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
    UNIFI_PROTECT_SITES=[
        {'host': '192.168.10.1', 'api_key': 'key1', 'name': 'Sun Peaks'},
        {'host': '192.168.1.26', 'api_key': 'key2', 'name': 'Mercer Island'},
    ],
)
class CameraFeedViewAuthFilterTests(TestCase):
    """Only the primary site is visible to unauthenticated users."""

    def setUp(self):
        self.client = Client()
        clear_cache()
        self.user = User.objects.create_user(
            username='testuser', password='testpass',
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
        """Logged-in users see all configured sites."""
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
        """Logged-in users see the site tab bar with both sites."""
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
    UNIFI_PROTECT_SITES=[],
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


# --- PTZ API tests ---

class PtzDetectionTests(TestCase):
    """Tests for PTZ camera detection via safe slot 1000 probe."""

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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
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

    @override_settings(UNIFI_PROTECT_SITES=[])
    def test_returns_false_when_not_configured(self):
        """Returns False when Protect is not configured."""
        result = ptz_goto_preset('cam1', 0)

        self.assertFalse(result)


class FetchCamerasWithPtzTests(TestCase):
    """Tests for auto-detected PTZ info in _fetch_cameras_from_site results."""

    def setUp(self):
        clear_cache()

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
        UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'k', 'name': 'S'}],
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
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
)
class PtzGotoViewTests(TestCase):
    """Tests for the ptz_goto view endpoint."""

    def setUp(self):
        self.client = Client()

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


# --- PTZ template tests ---

@override_settings(
    GO2RTC_URL='http://localhost:1984',
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'test_api_key', 'name': 'Test Site'}],
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
    UNIFI_PROTECT_SITES=[
        {'host': '192.168.10.1', 'api_key': 'key1', 'name': 'Sun Peaks'},
        {'host': '192.168.1.26', 'api_key': 'key2', 'name': 'Mercer Island'},
    ],
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
            username='testuser', password='testpass',
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


@tag('selenium')
@override_settings(
    GO2RTC_URL=GO2RTC_CI_URL,
    UNIFI_PROTECT_SITES=[],
)
class CameraStreamSeleniumTests(StaticLiveServerTestCase):
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

        from selenium import webdriver
        from tests.selenium_helpers import get_chrome_options

        cls.driver = webdriver.Chrome(options=get_chrome_options())
        cls.driver.implicitly_wait(5)
        cls._register_test_stream()

    @classmethod
    def tearDownClass(cls):
        if hasattr(cls, 'driver'):
            cls.driver.quit()
        super().tearDownClass()

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

    def setUp(self):
        self.driver.get('about:blank')
        self.driver.delete_all_cookies()

    def test_camera_page_renders_video_streams_with_correct_mode(self):
        """video-stream elements appear with data-mode='webrtc,mse,mp4'.

        This is the key regression test for PR #45: removing MSE from
        data-mode broke video loading entirely because go2rtc's onopen()
        if/else-if chain needs MSE as a placeholder protocol.
        """
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
                mode, 'webrtc,mse,mp4',
                f"Expected data-mode='webrtc,mse,mp4', got '{mode}'",
            )

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

        # The critical assertion: the fullscreen stream's WebSocket must
        # reach OPEN within 5 seconds.  The old bug caused the new WS to
        # be killed by the stale onclose/video-error handlers, leaving ws
        # null until RECONNECT_TIMEOUT (15s) fired.
        start = time.time()
        ws_open = WebDriverWait(self.driver, 5).until(
            lambda d: d.execute_script("""
                const fs = document.fullscreenElement;
                if (!fs) return false;
                const el = fs.querySelector('video-stream');
                if (!el || !el.ws) return false;
                return el.ws.readyState === WebSocket.OPEN;
            """)
        )
        elapsed = time.time() - start
        self.assertTrue(
            ws_open,
            f"Fullscreen stream WebSocket did not open within 5s "
            f"(took {elapsed:.1f}s) — safeDisconnect may be broken"
        )

    def test_fullscreen_close_after_next_exits_completely(self):
        """Close after stepping to the next camera leaves fullscreen entirely.

        Each prev/next press calls requestFullscreen on another card, which
        stacks fullscreen elements; a single exitFullscreen only pops back to
        the previous camera.
        """
        from selenium.webdriver.common.by import By
        from selenium.webdriver.support.ui import WebDriverWait

        source = urllib.parse.quote('ffmpeg:virtual?video#video=h264', safe='')
        req = urllib.request.Request(
            f'{GO2RTC_CI_URL}/api/streams?name=test_cam2&src={source}',
            method='PUT', data=b'',
        )
        with urllib.request.urlopen(req, timeout=10):
            pass
        self.addCleanup(lambda: urllib.request.urlopen(urllib.request.Request(
            f'{GO2RTC_CI_URL}/api/streams?src=test_cam2', method='DELETE',
        ), timeout=10))

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
                if (!el || !el.ws) return false;
                return el.ws.readyState === WebSocket.OPEN;
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
                if (!el || !el.ws) return false;
                return el.ws.readyState === WebSocket.OPEN;
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

from datetime import datetime, timedelta, timezone as dt_tz  # noqa: E402
from django.utils import timezone as dj_tz  # noqa: E402
from . import events as ev_mod  # noqa: E402
from . import frames as frames_mod  # noqa: E402
from .models import ProtectEvent  # noqa: E402


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

    @override_settings(UNIFI_PROTECT_SITES=[
        {"host": "h1", "api_key": "k", "name": "A", "username": "u", "password": "p"},
        {"host": "h2", "api_key": "k", "name": "B"},
    ])
    def test_start_only_sites_with_accounts(self):
        with patch.object(ev_mod, "_workers", {}), patch.object(ev_mod, "_started", False), \
                patch.object(ev_mod.SiteWorker, "start") as mock_start:
            ev_mod.start()
            ev_mod.start()
            self.assertEqual(list(ev_mod._workers), ["h1"])
            mock_start.assert_called_once()


class RingAlertTests(TestCase):
    def _handle(self, kinds, seconds_ago, added=True, popup=True):
        import asyncio
        worker = ev_mod.SiteWorker({"name": "S", "host": "h", "username": "u", "password": "p"})
        worker.names = {"cam1": "Doorbell"}
        row = {"event_id": "r1", "camera_id": "cam1", "kinds": kinds,
               "start": datetime.now(dt_tz.utc) - timedelta(seconds=seconds_ago), "end": None, "score": 0}

        async def fake_in_db(fn, *args):
            return [] if fn is ev_mod._save else fn(*args)
        worker._in_db = fake_in_db
        pushed = []

        async def fake_push(*args):
            pushed.append(args)
        with patch.object(ev_mod, "_push_ring_alert", fake_push), \
                patch("cameras.config.doorbell_popup", return_value=popup), \
                patch("cameras.config.popup_seconds", return_value=45):
            asyncio.run(worker._handle(row, added))
        return pushed

    def test_fresh_ring_pushes_alert(self):
        self.assertEqual(self._handle(["doorbell"], 2), [("cam1", "Doorbell", 45)])

    def test_old_or_updated_or_non_ring_does_not_push(self):
        self.assertEqual(self._handle(["doorbell"], 120), [])
        self.assertEqual(self._handle(["doorbell"], 2, added=False), [])
        self.assertEqual(self._handle(["person"], 2), [])

    def test_popup_setting_off(self):
        self.assertEqual(self._handle(["doorbell"], 2, popup=False), [])

    def test_push_builds_alert(self):
        import asyncio
        import sys
        import types
        fake = types.ModuleType("bdh_sdk.alerts")
        fake.Alert = lambda **kw: kw
        fake.push = MagicMock()
        with patch.dict(sys.modules, {"bdh_sdk.alerts": fake}), \
                patch("bdh_sdk.alerts", fake, create=True):
            asyncio.run(ev_mod._push_ring_alert("cam1", "Front", 45))
        fake.push.assert_called_once_with({
            "app": "cameras", "key": "doorbell:cam1", "title": "Someone's at the door (Front)",
            "embed_url": "/cameras/popup/cam1/", "ttl": 45,
        })


class EventViewTests(TestCase):
    def setUp(self):
        self.is_admin = False
        p = patch("cameras.views.is_admin", side_effect=lambda request: self.is_admin)
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

    def test_live_snapshot_is_not_cached(self):
        client = MagicMock()
        client.get_camera_snapshot.return_value = b"live"
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            c = self._admin()
            c.get("/cameras/snapshot/cam1/?w=160")
            resp = c.get("/cameras/snapshot/cam1/?w=160")
        self.assertEqual(resp.content, b"live")
        self.assertEqual(resp["Cache-Control"], "no-store")
        self.assertEqual(client.get_camera_snapshot.call_count, 2)

    def test_clip_window_is_capped(self):
        client = MagicMock()
        client.get_camera_video.return_value = b"mp4"
        start = 1_700_000_000
        with patch("cameras.views.events.run", side_effect=lambda cam, fn, **kw: fn(client)):
            resp = self._admin().get(f"/cameras/clip/cam1/?start={start}&end={start + 3600}")
            self.assertEqual(resp["Content-Type"], "video/mp4")
            _, s, e = client.get_camera_video.call_args.args
            self.assertEqual((e - s).total_seconds(), 120)
            self.assertEqual(self._admin().get("/cameras/clip/cam1/").status_code, 400)
            future = int(dj_tz.now().timestamp()) + 600
            self.assertEqual(self._admin().get(f"/cameras/clip/cam1/?start={future}").status_code, 400)


@override_settings(
    GO2RTC_URL='http://localhost:1984',
    UNIFI_PROTECT_SITES=[{'host': '192.168.10.1', 'api_key': 'k', 'name': 'Test Site'}],
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
        self.assertNotIn('cam-scrub', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_admin_sees_rail_and_scrubber(self, mock_cameras, _):
        mock_cameras.return_value = self.CAMS
        with patch("cameras.views.is_admin", return_value=True):
            html = Client().get("/cameras/").content.decode()
        self.assertIn('class="ev-rail"', html)
        self.assertIn('class="cam-scrub"', html)
        self.assertIn('data-kind="person"', html)
        self.assertNotIn('data-kind="motion"', html)

    @patch("cameras.views._register_streams_with_go2rtc")
    @patch("cameras.views.get_protect_cameras")
    def test_popup_is_public_and_live_only(self, mock_cameras, mock_register):
        mock_cameras.return_value = self.CAMS
        resp = Client().get("/cameras/popup/cam1/")
        self.assertEqual(resp.status_code, 200)
        self.assertEqual(resp["X-Frame-Options"], "SAMEORIGIN")
        self.assertContains(resp, 'data-stream-name="front_door"')
        self.assertNotContains(resp, "cam-scrub")
        mock_register.assert_called_once()
        self.assertEqual(Client().get("/cameras/popup/nope/").status_code, 404)


from . import popups as popups_mod  # noqa: E402


class EventPopupConfigTests(TestCase):
    def test_popup_map_accepts_table_or_json(self):
        with override_settings(BDH_CAMERAS_EVENT_POPUPS={"ha/state/a.b": "Front Door", "x": ""}):
            self.assertEqual(popups_mod.popup_map(), {"ha/state/a.b": "Front Door"})
        with override_settings(BDH_CAMERAS_EVENT_POPUPS='{"hook/door": "Front Door"}'):
            self.assertEqual(popups_mod.popup_map(), {"hook/door": "Front Door"})

    def test_popup_map_rejects_bad_values(self):
        for bad in ("{not json", '["a"]', 5):
            with override_settings(BDH_CAMERAS_EVENT_POPUPS=bad):
                self.assertEqual(popups_mod.popup_map(), {})

    def test_triggered_only_on_off_to_on(self):
        self.assertTrue(popups_mod.triggered({"old": "off", "new": "on"}))
        self.assertFalse(popups_mod.triggered({"old": "on", "new": "off"}))
        self.assertFalse(popups_mod.triggered({"old": "unavailable", "new": "on"}))
        self.assertFalse(popups_mod.triggered({"old": "on", "new": "on"}))
        self.assertTrue(popups_mod.triggered({"anything": 1}))
        self.assertTrue(popups_mod.triggered(None))


@override_settings(BDH_CAMERAS_EVENT_POPUPS={
    "ha/state/binary_sensor.front_door": "front door",
    "hook/#": "Front Door",
    "bad/#/x": "Front Door",
})
class EventPopupTests(TestCase):
    def setUp(self):
        from bdh_sdk import alerts, events
        self.events = events
        events.reset()
        self.pushed = []
        old = alerts._sender
        alerts.set_sender(self.pushed.append)
        self.addCleanup(alerts.set_sender, old)
        self.addCleanup(events.reset)
        self.addCleanup(popups_mod.stop)
        self.enabled = True
        for target, kw in (
            ("cameras.config.event_popup", {"side_effect": lambda: self.enabled}),
            ("cameras.config.popup_seconds", {"return_value": 45}),
            ("cameras.protect_api.get_protect_cameras", {"return_value": [
                {"cameras": [{"camera_id": "cam1", "name": "Front Door"}]}]}),
        ):
            p = patch(target, **kw)
            p.start()
            self.addCleanup(p.stop)
        with self.assertLogs("cameras.popups", "WARNING"):
            popups_mod.start()

    def _publish(self, topic, data):
        self.events.publish(topic, data)
        self.events.flush(5)
        return [a.as_dict() for a in self.pushed]

    def test_door_opening_pops_up_camera(self):
        data = {"entity_id": "binary_sensor.front_door", "old": "off", "new": "on",
                "attributes": {"friendly_name": "Front Door", "device_class": "door"}}
        self.assertEqual(self._publish("ha/state/binary_sensor.front_door", data), [{
            "app": "cameras", "key": "doorbell:cam1", "title": "Front Door opened",
            "embed_url": "/cameras/popup/cam1/", "ttl": 45,
        }])

    def test_closing_does_not_pop_up(self):
        self.assertEqual(self._publish("ha/state/binary_sensor.front_door", {"old": "on", "new": "off"}), [])

    def test_webhook_event_pops_up_with_camera_name(self):
        pushed = self._publish("hook/person", {"who": "someone"})
        self.assertEqual([(a["key"], a["title"]) for a in pushed], [("doorbell:cam1", "Front Door")])

    def test_setting_off(self):
        self.enabled = False
        self.assertEqual(self._publish("hook/person", {}), [])

    def test_unknown_camera(self):
        with patch("cameras.protect_api.get_protect_cameras", return_value=[]), \
                self.assertLogs("cameras.popups", "WARNING"):
            self.assertEqual(self._publish("hook/person", {}), [])
