"""Resize and cache historical Protect frames.

Protect ignores ``width`` for historical snapshots and returns the full
sensor frame (~1.6 MB at 2688x1512), so we downscale here and keep the
small JPEGs in a byte-capped in-process LRU.
"""
import io
import logging
import threading
from collections import OrderedDict

logger = logging.getLogger(__name__)

CACHE_BYTES = 50 * 1024 * 1024
TS_STEP = 5  # seconds; scrub positions within this window share a frame

_cache = OrderedDict()
_size = 0
_lock = threading.Lock()


def round_ts(dt):
    return dt.replace(second=dt.second - dt.second % TS_STEP, microsecond=0)


def get(key):
    with _lock:
        data = _cache.get(key)
        if data is not None:
            _cache.move_to_end(key)
        return data


def put(key, data):
    global _size
    if len(data) > CACHE_BYTES:
        return
    with _lock:
        old = _cache.pop(key, None)
        if old is not None:
            _size -= len(old)
        _cache[key] = data
        _size += len(data)
        while _size > CACHE_BYTES:
            _, evicted = _cache.popitem(last=False)
            _size -= len(evicted)


def clear():
    global _size
    with _lock:
        _cache.clear()
        _size = 0


def resize(data, width):
    """Downscale a JPEG to ``width`` px wide; returns the input if it's already small."""
    try:
        from PIL import Image

        img = Image.open(io.BytesIO(data))
        if img.width <= width:
            return data
        img.draft('RGB', (width, img.height * width // img.width))
        img = img.convert('RGB')
        img.thumbnail((width, img.height * width // img.width), Image.LANCZOS)
        out = io.BytesIO()
        img.save(out, 'JPEG', quality=80, optimize=True)
        return out.getvalue()
    except Exception as err:
        logger.warning("Could not resize snapshot; serving original: %s", err)
        return data
