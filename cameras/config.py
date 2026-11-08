"""Camera app settings: the go2rtc server and the UniFi Protect sites."""
from django.conf import settings

from bdh_sdk.settings import app_setting

MAX_PROTECT_SITES = 9


def go2rtc_url():
    """go2rtc API URL (BDH_CAMERAS_GO2RTC_URL, legacy GO2RTC_URL)."""
    return app_setting("cameras", "go2rtc_url", "http://localhost:1984", legacy=("GO2RTC_URL",))


def protect_sites():
    """UniFi Protect sites as dicts with host, api_key and name.

    ``settings.UNIFI_PROTECT_SITES`` wins when defined. Otherwise sites come
    from numbered env vars ``BDH_CAMERAS_PROTECT_{HOST,API_KEY,NAME}_<n>``
    (legacy ``UNIFI_PROTECT_{HOST,API_KEY,NAME}_<n>``), n = 1..9; a site
    needs both a host and an API key.
    """
    configured = getattr(settings, "UNIFI_PROTECT_SITES", None)
    if configured is not None:
        return configured
    sites = []
    for i in range(1, MAX_PROTECT_SITES + 1):
        host = app_setting("cameras", f"protect_host_{i}", "", legacy=(f"UNIFI_PROTECT_HOST_{i}",))
        api_key = app_setting("cameras", f"protect_api_key_{i}", "", legacy=(f"UNIFI_PROTECT_API_KEY_{i}",))
        name = app_setting("cameras", f"protect_name_{i}", f"Site {i}", legacy=(f"UNIFI_PROTECT_NAME_{i}",))
        if host and api_key:
            sites.append({"host": host, "api_key": api_key, "name": name})
    return sites