"""Camera app settings: the go2rtc server and the UniFi Protect sites."""
from django.conf import settings

from bdh_sdk.settings import app_setting, as_bool

MAX_PROTECT_SITES = 9


def go2rtc_url():
    """go2rtc API URL (BDH_CAMERAS_GO2RTC_URL, legacy GO2RTC_URL)."""
    return app_setting("cameras", "go2rtc_url", "http://localhost:1984", legacy=("GO2RTC_URL",))


def protect_sites():
    """UniFi Protect sites as dicts with host, api_key, name, username and password.

    ``settings.UNIFI_PROTECT_SITES`` wins when defined. Otherwise sites come
    from numbered env vars ``BDH_CAMERAS_PROTECT_{HOST,API_KEY,NAME}_<n>``
    (legacy ``UNIFI_PROTECT_{HOST,API_KEY,NAME}_<n>``), n = 1..9; a site
    needs both a host and an API key. ``BDH_CAMERAS_PROTECT_{USERNAME,PASSWORD}_<n>``
    are an optional local Protect account (view only) for events and recordings.
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
            sites.append({
                "host": host,
                "api_key": api_key,
                "name": name,
                "username": app_setting("cameras", f"protect_username_{i}", ""),
                "password": app_setting("cameras", f"protect_password_{i}", ""),
            })
    return sites


# Event kinds in display order. ``motion`` is plain motion with no smart detection.
EVENT_KINDS = ("doorbell", "person", "vehicle", "package", "animal", "motion")
EVENT_KIND_DEFAULTS = {
    "doorbell": True, "person": True, "vehicle": True,
    "package": True, "animal": False, "motion": False,
}


def enabled_event_kinds():
    """Event kinds turned on in Settings (``show_<kind>``)."""
    return [
        k for k in EVENT_KINDS
        if app_setting("cameras", f"show_{k}", EVENT_KIND_DEFAULTS[k], cast=as_bool)
    ]


def doorbell_popup():
    return app_setting("cameras", "doorbell_popup", True, cast=as_bool)


def event_popup():
    return app_setting("cameras", "event_popup", True, cast=as_bool)


def keep_open():
    """Hold off the hub screensaver while the cameras page is open."""
    return app_setting("cameras", "keep_open", True, cast=as_bool)


def popup_seconds():
    try:
        return max(10, int(app_setting("cameras", "popup_seconds", 60)))
    except (TypeError, ValueError):
        return 60