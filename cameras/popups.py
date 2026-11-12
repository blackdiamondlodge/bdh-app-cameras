"""Camera pop-ups for hub events (``bdh_sdk.events``), e.g. a door opening.

The ``event_popups`` setting maps event topics to camera names. When an event
fires, the camera is shown live on the hub screen, as for a doorbell ring. The
pop-up uses the doorbell's alert key, so a new event while it is showing just
restarts its timer.
"""
import json
import logging

from django.db import close_old_connections

from bdh_sdk.settings import app_setting

from . import config

logger = logging.getLogger(__name__)

_OPENINGS = {"door", "garage_door", "opening", "window"}
_subscriptions = []


def popup_map():
    """``{topic pattern: camera name}`` from ``event_popups`` (a table or a JSON string)."""
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


def triggered(data):
    """True for a state change from off to on, or for any event that isn't a state change."""
    if isinstance(data, dict) and "new" in data:
        return data.get("old") == "off" and data.get("new") == "on"
    return True


def find_camera(name):
    """The Protect camera named ``name`` (case-insensitive), or None."""
    from .protect_api import get_protect_cameras

    wanted = name.strip().lower()
    for site in get_protect_cameras():
        for cam in site.get("cameras", []):
            if (cam.get("name") or "").strip().lower() == wanted:
                return cam
    return None


def title_for(data, camera_name):
    attrs = data.get("attributes") if isinstance(data, dict) else None
    attrs = attrs if isinstance(attrs, dict) else {}
    label = attrs.get("friendly_name") or camera_name
    if attrs.get("device_class") in _OPENINGS:
        return f"{label} opened"
    return label


def handle(event, camera_name):
    if not triggered(event.data):
        return
    close_old_connections()
    try:
        if not config.event_popup():
            return
        cam = find_camera(camera_name)
        if cam is None:
            logger.warning("Event %s: no camera named %r", event.topic, camera_name)
            return
        from bdh_sdk import alerts

        alerts.push(alerts.Alert(
            app="cameras",
            key=f"doorbell:{cam['camera_id']}",
            title=title_for(event.data, cam.get("name") or camera_name),
            embed_url=f"/cameras/popup/{cam['camera_id']}/",
            ttl=config.popup_seconds(),
        ))
    except Exception:
        logger.exception("Event pop-up for %s failed", event.topic)
    finally:
        close_old_connections()


def start():
    """Subscribe to every topic in ``event_popups``."""
    try:
        mapping = popup_map()
    except Exception:
        logger.exception("Could not read event_popups")
        return
    if not mapping:
        return
    try:
        from bdh_sdk import events
    except ImportError:
        logger.warning("bdh_sdk.events not available; no event pop-ups")
        return
    for pattern, name in mapping.items():
        try:
            _subscriptions.append(events.subscribe(pattern, lambda e, n=name: handle(e, n)))
        except ValueError as err:
            logger.warning("event_popups: %s", err)
    logger.info("Camera pop-ups for %d event topic(s)", len(_subscriptions))


def stop():
    while _subscriptions:
        _subscriptions.pop().cancel()
