"""Camera pop-ups: which camera shows live on the hub screen, and when.

Each camera can pop up for its own Protect detections (a doorbell ring, a
person, ...; see ``events.py``) and for Home Assistant entities ("Also when").
Admins choose these on the Pop-ups settings page, which saves app state
``popups``::

    {"enabled": true,
     "cameras": {"<camera_id>": {
         "kinds": ["doorbell", "person"],
         "sounds": {"person": "none"},
         "triggers": [{"entity_id": "binary_sensor.front_door"},
                      {"entity_id": "lock.front_door", "to": "unlocked", "sound": "westminster"}]}}}

A trigger without ``to`` fires on its domain's usual "something happened":
a binary sensor turning on, a lock unlocking, a new ``event.*`` event, or any
other state change.

Each pop-up plays a hub chime (``bdh_sdk.alerts.CHIMES``, or ``"none"``).
Only choices that differ from the defaults are stored: ``sounds`` per
detection kind (see :func:`kind_sound`) and ``sound`` per trigger
(``TRIGGER_SOUND``).

Until that page is saved the old settings apply: a ring pops up its doorbell
camera when ``doorbell_popup`` is on, and when ``event_popup`` is on each topic
in ``event_popups`` pops up the camera it names.

Pop-ups use the doorbell's alert key, so a new trigger while one is showing
just restarts its timer.
"""
import json
import logging
import re

from django.db import close_old_connections

from bdh_sdk.settings import app_setting

from . import config

logger = logging.getLogger(__name__)

# Protect detections a camera can pop up for, in display order.
POPUP_KINDS = ("doorbell", "person", "vehicle", "package", "animal")
STATE_KEY = "popups"
# State a trigger fires on when it has no ``to``; other domains fire on any change.
DEFAULT_TO = {"binary_sensor": "on", "lock": "unlocked"}

try:
    from bdh_sdk.alerts import CHIMES, SILENT
except ImportError:  # bdh-sdk < 0.17
    CHIMES = ("ding-dong", "soft-bell", "rising-trio", "notify", "westminster", "glass-ping", "low-pulse",
              "knock-knock", "ding-dong-ding", "harp-sweep", "kalimba", "single-ding")
    SILENT = "none"
KIND_SOUNDS = {"doorbell": "ding-dong", "person": "notify"}
OTHER_KIND_SOUND = "single-ding"
TRIGGER_SOUND = "knock-knock"

_OPENINGS = {"door", "garage_door", "opening", "window"}
_NO_STATE = {None, "", "unavailable", "unknown"}
_ENTITY_ID = re.compile(r"^[a-z0-9_]+\.[a-z0-9_]+$")
_subscriptions = []
_started = False


def popup_map():
    """Legacy ``{topic pattern: camera name}`` from ``event_popups`` (a table or a JSON string)."""
    raw = app_setting("cameras", "event_popups", {})
    if isinstance(raw, str):
        try:
            raw = json.loads(raw) if raw.strip() else {}
        except ValueError:
            logger.warning("event_popups is not valid JSON; no event pop-ups")
            return {}
    if not isinstance(raw, dict):
        logger.warning("event_popups must map topics to camera names; no event pop-ups")
        return {}
    return {str(topic): str(name) for topic, name in raw.items() if name}


def kind_sound(kind):
    """The default chime for a Protect detection kind."""
    return KIND_SOUNDS.get(kind, OTHER_KIND_SOUND)


def _sound(value, default):
    """``value`` if it's a valid choice other than ``default``, else None (use the default)."""
    if value in CHIMES or value == SILENT:
        return None if value == default else value
    return None


def clean(raw):
    """A valid pop-up config from ``raw`` (e.g. a settings page POST); bad parts are dropped."""
    raw = raw if isinstance(raw, dict) else {}
    cameras = {}
    cams = raw.get("cameras")
    for camera_id, cam in (cams.items() if isinstance(cams, dict) else ()):
        if not isinstance(camera_id, str) or not camera_id or len(camera_id) > 100 or not isinstance(cam, dict):
            continue
        kinds = cam.get("kinds") if isinstance(cam.get("kinds"), list) else []
        triggers, seen = [], set()
        for trig in cam.get("triggers") if isinstance(cam.get("triggers"), list) else ():
            entity_id = trig.get("entity_id") if isinstance(trig, dict) else None
            if not isinstance(entity_id, str) or not _ENTITY_ID.match(entity_id):
                continue
            to = trig.get("to")
            to = to.strip()[:64] if isinstance(to, str) else ""
            if (entity_id, to) in seen:
                continue
            seen.add((entity_id, to))
            out = {"entity_id": entity_id, "to": to} if to else {"entity_id": entity_id}
            sound = _sound(trig.get("sound"), TRIGGER_SOUND)
            if sound:
                out["sound"] = sound
            triggers.append(out)
        entry = {"kinds": [k for k in POPUP_KINDS if k in kinds], "triggers": triggers}
        given = cam.get("sounds") if isinstance(cam.get("sounds"), dict) else {}
        sounds = {k: s for k in entry["kinds"] if (s := _sound(given.get(k), kind_sound(k)))}
        if sounds:
            entry["sounds"] = sounds
        if entry["kinds"] or entry["triggers"]:
            cameras[camera_id] = entry
    return {"enabled": raw.get("enabled", True) is not False, "cameras": cameras}


def stored():
    """The config saved from the settings page, or None if it was never saved."""
    from bdh_sdk.storage import app_state

    raw = app_state("cameras").get(STATE_KEY)
    return clean(raw) if isinstance(raw, dict) else None


def _all_cameras():
    from .protect_api import get_protect_cameras

    return [cam for site in get_protect_cameras() for cam in site.get("cameras", [])]


def legacy_config():
    """The old settings expressed as a pop-up config (to pre-fill the settings page)."""
    cams = _all_cameras()
    cameras = {}
    if config.doorbell_popup():
        for cam in cams:
            if cam.get("is_doorbell"):
                cameras[cam["camera_id"]] = {"kinds": ["doorbell"], "triggers": []}
    if config.event_popup():
        for topic, name in popup_map().items():
            cam = find_camera(name, cams)
            if cam is None or not topic.startswith("ha/state/"):
                continue
            entry = cameras.setdefault(cam["camera_id"], {"kinds": [], "triggers": []})
            entry["triggers"].append({"entity_id": topic[len("ha/state/"):]})
    return clean({"enabled": True, "cameras": cameras})


def current():
    """The pop-up config in effect: saved, else the legacy settings."""
    return stored() or legacy_config()


def save(raw):
    """Save a config from the settings page and apply it now. Returns the cleaned config."""
    from bdh_sdk.storage import app_state

    cfg = clean(raw)
    app_state("cameras").set(STATE_KEY, cfg)
    reload()
    return cfg


def camera_kinds(camera_id):
    """Protect detection kinds that pop up ``camera_id`` (reads the database)."""
    cfg = stored()
    if cfg is None:
        return ["doorbell"] if config.doorbell_popup() else []
    if not cfg["enabled"]:
        return []
    return cfg["cameras"].get(camera_id, {}).get("kinds", [])


def camera_sound(camera_id, kind):
    """The chime ``camera_id`` plays when it pops up for ``kind`` (reads the database)."""
    cfg = stored()
    sounds = (cfg["cameras"].get(camera_id, {}).get("sounds", {}) if cfg else {})
    return sounds.get(kind, kind_sound(kind))


def triggered(data):
    """Legacy ``event_popups`` rule: off to on, or any event that isn't a state change."""
    if isinstance(data, dict) and "new" in data:
        return data.get("old") == "off" and data.get("new") == "on"
    return True


def matches(trigger, data):
    """True when the HA state change ``data`` (``{old, new, ...}``) should fire ``trigger``."""
    if not isinstance(data, dict) or "new" not in data:
        return False
    old, new = data.get("old"), data.get("new")
    if new == old or new in _NO_STATE:
        return False
    domain = trigger["entity_id"].split(".", 1)[0]
    if domain == "event":
        # The state is the time of the last event, so any new value is a new event.
        return not trigger.get("to")
    if old in _NO_STATE:
        # Coming back from unavailable (e.g. an HA restart) isn't an event.
        return False
    want = trigger.get("to") or DEFAULT_TO.get(domain)
    return want is None or new == want


def find_camera(name, cams=None):
    """The Protect camera named ``name`` (case-insensitive), or None."""
    wanted = name.strip().lower()
    for cam in _all_cameras() if cams is None else cams:
        if (cam.get("name") or "").strip().lower() == wanted:
            return cam
    return None


def _camera_by_id(camera_id):
    for cam in _all_cameras():
        if cam.get("camera_id") == camera_id:
            return cam
    return None


def title_for(data, camera_name):
    data = data if isinstance(data, dict) else {}
    attrs = data.get("attributes") if isinstance(data.get("attributes"), dict) else {}
    label = attrs.get("friendly_name") or camera_name
    new = data.get("new", "on")
    if attrs.get("device_class") in _OPENINGS and new == "on":
        return f"{label} opened"
    if str(data.get("entity_id", "")).startswith("lock.") and new == "unlocked":
        return f"{label} unlocked"
    return label


def handle(event, trigger=None, camera_id=None, camera_name=None):
    """Pop up a camera for ``event``: by id for saved triggers, by name for legacy ones."""
    if not (matches(trigger, event.data) if trigger else triggered(event.data)):
        return
    close_old_connections()
    try:
        if not camera_id and not config.event_popup():
            return
        cam = _camera_by_id(camera_id) if camera_id else find_camera(camera_name or "")
        if cam is None:
            logger.warning("Event %s: no camera %r", event.topic, camera_id or camera_name)
            return
        from bdh_sdk import alerts

        alerts.push(alerts.Alert(
            app="cameras",
            key=f"doorbell:{cam['camera_id']}",
            title=title_for(event.data, cam.get("name") or camera_name or ""),
            embed_url=f"/cameras/popup/{cam['camera_id']}/",
            ttl=config.popup_seconds(),
            sound=(trigger or {}).get("sound", TRIGGER_SOUND),
        ))
    except Exception:
        logger.exception("Event pop-up for %s failed", event.topic)
    finally:
        close_old_connections()


def _targets():
    """``(topic pattern, handler kwargs)`` for every trigger in effect."""
    cfg = stored()
    if cfg is not None:
        if not cfg["enabled"]:
            return []
        return [
            (f"ha/state/{trig['entity_id']}", {"trigger": trig, "camera_id": camera_id})
            for camera_id, cam in cfg["cameras"].items()
            for trig in cam["triggers"]
        ]
    if not config.event_popup():
        return []
    # Legacy topics are resolved to cameras by name when they fire.
    return [(pattern, {"camera_name": name}) for pattern, name in popup_map().items()]


def start():
    """Subscribe to every trigger's event topic."""
    global _started
    _started = True
    try:
        targets = _targets()
    except Exception:
        logger.exception("Could not read the pop-up settings")
        return
    if not targets:
        return
    try:
        from bdh_sdk import events
    except ImportError:
        logger.warning("bdh_sdk.events not available; no event pop-ups")
        return
    for pattern, kwargs in targets:
        try:
            _subscriptions.append(events.subscribe(pattern, lambda e, kw=kwargs: handle(e, **kw)))
        except ValueError as err:
            logger.warning("Pop-ups: %s", err)
    logger.info("Camera pop-ups for %d event topic(s)", len(_subscriptions))


def stop():
    global _started
    _started = False
    while _subscriptions:
        _subscriptions.pop().cancel()


def reload():
    """Re-subscribe after the settings change (only where pop-ups were started)."""
    if _started:
        stop()
        start()
