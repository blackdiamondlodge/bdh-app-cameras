from bdh_sdk import Status

from .protect_api import get_protect_cameras


def status():
    count = sum(len(site["cameras"]) for site in get_protect_cameras())
    if count == 0:
        return None
    return Status(f"{count} camera{'s' if count != 1 else ''}")
