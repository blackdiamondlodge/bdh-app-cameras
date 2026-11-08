from bdh_sdk import OWNER, Status

from .protect_api import get_protect_cameras


def status(mode=None):
    # Match the cameras page: only signed-in owners see sites beyond the first.
    sites = get_protect_cameras()
    if mode != OWNER:
        sites = sites[:1]
    count = sum(len(site["cameras"]) for site in sites)
    if count == 0:
        return None
    return Status(f"{count} camera{'s' if count != 1 else ''}")
