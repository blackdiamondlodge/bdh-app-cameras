from bdh_sdk import Status

from .protect_api import get_protect_cameras


def status():
    # Count the first (local) site only; other sites are remote properties.
    sites = get_protect_cameras()
    count = len(sites[0]["cameras"]) if sites else 0
    if count == 0:
        return None
    return Status(f"{count} camera{'s' if count != 1 else ''}")
