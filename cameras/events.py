"""UniFi Protect event worker: keeps the last 24 hours of detections.

One daemon thread per Protect site that has a local (view-only) account.
Each thread runs its own asyncio loop with a uiprotect ``ProtectApiClient``:
it backfills 24 hours of events, then follows the private websocket. Database
work runs on a separate single-thread executor so the ORM never runs inside
the event loop. Views borrow a site's client with :func:`run`.
"""
import asyncio
import logging
import os
import sys
import threading
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from django.db import close_old_connections

from . import config, popups

logger = logging.getLogger(__name__)

HISTORY = timedelta(hours=24)
# A detection older than this when it arrives doesn't pop up (e.g. after a reconnect).
POPUP_FRESH = timedelta(seconds=30)
POPPED_MAX = 200
PRUNE_EVERY = 3600
THUMB_SIZE = 360
TIMEOUT = 30

_SMART_KIND = {
    "person": "person", "face": "person",
    "vehicle": "vehicle", "licensePlate": "vehicle", "car": "vehicle",
    "package": "package",
    "animal": "animal", "pet": "animal",
}
_SMART_TYPES = {"smartDetectZone", "smartDetectLine"}

_workers = {}
_started = False
_start_lock = threading.Lock()


class WorkerUnavailable(Exception):
    """No running worker (no Protect account configured, or not connected yet)."""


def kinds_for(event_type, smart_types):
    """Map a Protect event type and smart-detect types to our kinds."""
    if event_type == "ring":
        return ["doorbell"]
    if event_type in _SMART_TYPES:
        kinds = []
        for t in smart_types or ():
            kind = _SMART_KIND.get(t)
            if kind and kind not in kinds:
                kinds.append(kind)
        return kinds
    if event_type == "motion" and not smart_types:
        # Motion events with smart types duplicate their smartDetect event.
        return ["motion"]
    return []


def _from_ms(ms):
    return datetime.fromtimestamp(ms / 1000, tz=timezone.utc) if ms else None


def _enum_value(v):
    return getattr(v, "value", v)


def normalize_raw(raw):
    """Event fields from a ``get_events_raw`` dict, or None if not wanted."""
    kinds = kinds_for(raw.get("type"), raw.get("smartDetectTypes"))
    if not kinds or not raw.get("camera") or not raw.get("start"):
        return None
    return {
        "event_id": raw["id"],
        "camera_id": raw["camera"],
        "kinds": kinds,
        "start": _from_ms(raw["start"]),
        "end": _from_ms(raw.get("end")),
        "score": raw.get("score") or 0,
    }


def normalize_obj(event):
    """Event fields from a uiprotect ``Event`` model, or None if not wanted."""
    smart = [_enum_value(t) for t in (event.smart_detect_types or ())]
    kinds = kinds_for(_enum_value(event.type), smart)
    if not kinds or not event.camera_id or not event.start:
        return None
    return {
        "event_id": event.id,
        "camera_id": event.camera_id,
        "kinds": kinds,
        "start": event.start,
        "end": event.end,
        "score": event.score or 0,
    }


def _db(fn):
    def wrapped(*args, **kwargs):
        close_old_connections()
        try:
            return fn(*args, **kwargs)
        finally:
            close_old_connections()
    return wrapped


@_db
def _save(site, host, names, rows):
    """Upsert events; returns ids that still need a thumbnail."""
    from .models import ProtectEvent

    missing = []
    for row in rows:
        obj, _ = ProtectEvent.objects.update_or_create(
            event_id=row["event_id"],
            defaults={
                "site": site,
                "host": host,
                "camera_id": row["camera_id"],
                "camera_name": names.get(row["camera_id"], ""),
                "kinds": row["kinds"],
                "start": row["start"],
                "end": row["end"],
                "score": row["score"],
            },
        )
        if row["end"] is not None and obj.thumbnail is None:
            missing.append((row["event_id"], row["kinds"]))
    return missing


@_db
def _save_thumb(event_id, data):
    from .models import ProtectEvent

    ProtectEvent.objects.filter(event_id=event_id).update(thumbnail=data)


@_db
def prune():
    from django.utils import timezone as djtz

    from .models import ProtectEvent

    return ProtectEvent.objects.filter(start__lt=djtz.now() - HISTORY).delete()[0]


_KIND_LABEL = {"person": "Person", "vehicle": "Vehicle", "package": "Package", "animal": "Animal"}


def popup_title(kind, camera_name):
    if kind == "doorbell":
        return f"Someone's at the door ({camera_name})" if camera_name else "Someone's at the door"
    label = _KIND_LABEL.get(kind, kind.capitalize())
    return f"{label} at {camera_name}" if camera_name else f"{label} detected"


async def _push_popup(camera_id, camera_name, ttl, kind="doorbell", sound=None):
    try:
        from bdh_sdk import alerts
    except ImportError:
        logger.info("%s on %s; bdh_sdk.alerts not available, no pop-up", kind, camera_name)
        return
    alert = alerts.Alert(
        app="cameras",
        key=f"doorbell:{camera_id}",
        title=popup_title(kind, camera_name),
        embed_url=f"/cameras/popup/{camera_id}/",
        ttl=ttl,
        sound=sound,
    )
    try:
        if hasattr(alerts, "apush"):
            await alerts.apush(alert)
        else:
            await asyncio.get_running_loop().run_in_executor(None, alerts.push, alert)
    except Exception:
        logger.exception("Camera pop-up failed")


class SiteWorker:
    def __init__(self, site):
        self.site = site["name"]
        self.host = site["host"]
        self._username = site["username"]
        self._password = site["password"]
        self.client = None
        self.camera_ids = set()
        self.names = {}
        # event_id -> kinds already popped up, so updates to an event don't pop up again.
        self._popped = {}
        self.loop = None
        self._db_pool = ThreadPoolExecutor(1, thread_name_prefix=f"cameras-db-{self.host}")
        self._thread = threading.Thread(
            target=self._run_thread, name=f"cameras-events-{self.host}", daemon=True,
        )

    @property
    def ready(self):
        return self.client is not None and self.loop is not None

    def start(self):
        self._thread.start()

    def _run_thread(self):
        self.loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self.loop)
        self.loop.run_until_complete(self._main())

    async def _in_db(self, fn, *args):
        return await asyncio.get_running_loop().run_in_executor(self._db_pool, fn, *args)

    async def _main(self):
        backoff = 10
        while True:
            client = None
            try:
                client = await self._connect()
                backoff = 10
                while True:
                    await asyncio.sleep(PRUNE_EVERY)
                    removed = await self._in_db(prune)
                    if removed:
                        logger.debug("Pruned %d old Protect events", removed)
                    await self._refresh_names(client)
            except asyncio.CancelledError:
                raise
            except Exception:
                logger.exception("Protect events worker for %s failed; retrying in %ss", self.host, backoff)
            finally:
                self.client = None
                if client is not None:
                    try:
                        await client.async_disconnect_ws()
                        await client.close_session()
                    except Exception:
                        pass
            await asyncio.sleep(backoff)
            backoff = min(backoff * 2, 600)

    async def _connect(self):
        from uiprotect import ProtectApiClient
        from uiprotect.data import ModelType

        client = ProtectApiClient(
            self.host, 443, self._username, self._password,
            verify_ssl=False, store_sessions=False,
            subscribed_models={ModelType.EVENT}, ignore_stats=True,
        )
        await client.update()
        await self._refresh_names(client)
        await self._backfill(client)
        client.subscribe_websocket(self._on_message)
        self.client = client
        logger.info("Protect events worker connected to %s (%d cameras)", self.host, len(self.names))
        return client

    async def _refresh_names(self, client):
        cams = client.bootstrap.cameras
        self.names = {cid: cam.name for cid, cam in cams.items()}
        self.camera_ids = set(self.names)

    async def _backfill(self, client):
        from uiprotect.data import EventType

        end = datetime.now(timezone.utc)
        raw = await client.get_events_raw(
            start=end - HISTORY, end=end,
            types=[EventType.RING, EventType.MOTION, EventType.SMART_DETECT, EventType.SMART_DETECT_LINE],
        )
        rows = [r for r in (normalize_raw(e) for e in raw) if r]
        missing = await self._in_db(_save, self.site, self.host, self.names, rows)
        await self._in_db(prune)
        await self._fetch_thumbs(client, missing)
        logger.info("Backfilled %d Protect events from %s", len(rows), self.host)

    async def _fetch_thumbs(self, client, missing):
        # Settings may hit the DB, so read them off the event loop.
        enabled = set(await self._in_db(config.enabled_event_kinds))
        for event_id, kinds in missing:
            if enabled.intersection(kinds):
                await self._fetch_thumb(client, event_id)

    async def _fetch_thumb(self, client, event_id):
        try:
            data = await client.get_event_thumbnail(event_id, width=THUMB_SIZE, retry_timeout=10)
        except Exception as err:
            logger.debug("Thumbnail %s failed: %s", event_id, err)
            return None
        if data:
            await self._in_db(_save_thumb, event_id, data)
        return data

    def _on_message(self, msg):
        event = msg.new_obj
        if event is None or _enum_value(getattr(event, "model", None)) != "event":
            return
        row = normalize_obj(event)
        if row is None:
            return
        asyncio.get_running_loop().create_task(self._handle(row))

    async def _handle(self, row):
        try:
            missing = await self._in_db(_save, self.site, self.host, self.names, [row])
            await self._popup(row)
            if missing and self.client is not None:
                await self._fetch_thumbs(self.client, missing)
        except Exception:
            logger.exception("Failed to store Protect event %s", row["event_id"])

    async def _popup(self, row):
        """Pop up the camera for a fresh detection of a kind it is set to pop up for.

        Smart detections often arrive as updates to an event that started as
        plain motion, so each kind pops up at most once per event.
        """
        if datetime.now(timezone.utc) - row["start"] >= POPUP_FRESH:
            return
        popped = self._popped.get(row["event_id"], set())
        new = [k for k in popups.POPUP_KINDS if k in row["kinds"] and k not in popped]
        if not new:
            return
        wanted = set(await self._in_db(popups.camera_kinds, row["camera_id"]))
        fire = [k for k in new if k in wanted]
        if not fire:
            return
        self._popped[row["event_id"]] = popped | set(fire)
        while len(self._popped) > POPPED_MAX:
            del self._popped[next(iter(self._popped))]
        ttl = await self._in_db(config.popup_seconds)
        sound = await self._in_db(popups.camera_sound, row["camera_id"], fire[0])
        await _push_popup(row["camera_id"], self.names.get(row["camera_id"], ""), ttl, fire[0], sound)

    def submit(self, coro_fn):
        """Run ``coro_fn(client)`` on this worker's loop; returns a concurrent Future."""
        if not self.ready:
            raise WorkerUnavailable(self.host)
        client = self.client
        return asyncio.run_coroutine_threadsafe(coro_fn(client), self.loop)


def worker_for_camera(camera_id):
    for worker in _workers.values():
        if camera_id in worker.camera_ids:
            return worker
    return None


def worker_for_host(host):
    return _workers.get(host)


def available():
    """True when at least one site has a connected worker."""
    return any(w.ready for w in _workers.values())


def run(camera_id, coro_fn, timeout=TIMEOUT):
    """Run ``coro_fn(client)`` on the worker owning ``camera_id`` and wait (sync callers)."""
    worker = worker_for_camera(camera_id)
    if worker is None:
        raise WorkerUnavailable(camera_id)
    return worker.submit(coro_fn).result(timeout)


def thumbnail(event_id, host, camera_id, timeout=TIMEOUT):
    """Fetch (and store) a missing thumbnail through the owning worker."""
    worker = worker_for_host(host) or worker_for_camera(camera_id)
    if worker is None:
        raise WorkerUnavailable(host)
    return worker.submit(lambda client: worker._fetch_thumb(client, event_id)).result(timeout)


_SKIP_COMMANDS = {
    "test", "migrate", "makemigrations", "collectstatic", "shell", "check",
    "hub_smoke", "showmigrations", "createsuperuser", "dbshell", "sqlmigrate",
}


def should_start(argv=None):
    """Start only in the server process (Daphne, or runserver's reloaded child)."""
    if os.environ.get("BDH_CAMERAS_EVENTS", "").lower() in ("0", "false", "off", "no"):
        return False
    argv = sys.argv if argv is None else argv
    prog = os.path.basename(argv[0]) if argv else ""
    if "daphne" in prog:
        return True
    if len(argv) > 1 and prog in ("manage.py", "django-admin", "__main__.py"):
        if argv[1] == "runserver":
            return os.environ.get("RUN_MAIN") == "true" or "--noreload" in argv
        return False
    return False


def start():
    """Start one worker per Protect site with a local account (idempotent)."""
    global _started
    with _start_lock:
        if _started:
            return
        _started = True
        try:
            sites = config.protect_sites()
        except Exception:
            logger.exception("Could not read Protect sites; events worker not started")
            return
        for site in sites:
            if not (site.get("username") and site.get("password")):
                continue
            worker = SiteWorker(site)
            _workers[site["host"]] = worker
            worker.start()
        if not _workers:
            logger.info("No Protect accounts configured; camera events are off")
