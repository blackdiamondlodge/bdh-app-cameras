"""UniFi Access developer API: list doors and unlock one (door unlock).

The console has a self-signed certificate, so instead of CA checks the
certificate's SHA-256 fingerprint is pinned: captured when the NVRs settings
page tests Access, and required to match on every call after that.
Tokens are never logged.
"""
import hashlib
import logging
import re
import socket
import ssl

import requests
from requests.adapters import HTTPAdapter

log = logging.getLogger(__name__)

PORT = 12445
TIMEOUT = (3, 8)
PIN_RE = re.compile(r"^[0-9a-f]{64}$")


class AccessError(Exception):
    """kind: unreachable, timeout, unauthorized, not_found, pin_mismatch or bad_response."""

    def __init__(self, kind, message=""):
        super().__init__(message or kind)
        self.kind = kind


def _split(host):
    """``(name, port)`` from ``host``, ``host:port`` or ``[v6]:port``."""
    host = (host or "").strip()
    m = re.match(r"^\[([^\]]+)\](?::(\d+))?$", host) or re.match(r"^([^:]+)(?::(\d+))?$", host)
    if m:
        return m.group(1), int(m.group(2) or PORT)
    return host, PORT


def _base(host):
    name, port = _split(host)
    if ":" in name:
        name = f"[{name}]"
    return f"https://{name}:{port}/api/v1/developer"


def clean_pin(pin):
    pin = (pin or "").replace(":", "").strip().lower()
    return pin if PIN_RE.match(pin) else ""


def fetch_pin(host):
    """The console's certificate SHA-256 fingerprint (hex), trusting it on first use."""
    name, port = _split(host)
    ctx = ssl.create_default_context()
    ctx.check_hostname = False
    ctx.verify_mode = ssl.CERT_NONE
    try:
        with socket.create_connection((name, port), timeout=TIMEOUT[0]) as sock:
            sock.settimeout(TIMEOUT[1])
            with ctx.wrap_socket(sock, server_hostname=name) as tls:
                der = tls.getpeercert(binary_form=True)
    except socket.timeout as e:
        raise AccessError("unreachable", f"Couldn't reach {host}") from e
    except (OSError, ssl.SSLError) as e:
        raise AccessError("unreachable", f"Couldn't reach {host}") from e
    if not der:
        raise AccessError("bad_response", "No certificate")
    return hashlib.sha256(der).hexdigest()


class _PinnedAdapter(HTTPAdapter):
    """Checks the server certificate's fingerprint rather than its CA chain."""

    def __init__(self, pin, **kwargs):
        self._pin = pin
        super().__init__(**kwargs)

    def init_poolmanager(self, *args, **kwargs):
        kwargs["assert_fingerprint"] = self._pin
        kwargs["cert_reqs"] = "CERT_NONE"
        kwargs["assert_hostname"] = False
        super().init_poolmanager(*args, **kwargs)

    def cert_verify(self, conn, url, verify, cert):
        super().cert_verify(conn, url, False, cert)
        conn.cert_reqs = "CERT_NONE"
        conn.assert_fingerprint = self._pin


def _request(method, host, token, path, pin, json=None):
    pin = clean_pin(pin)
    if not pin:
        raise AccessError("pin_mismatch", "No saved certificate for Access; test it on the NVRs settings page")
    url = _base(host) + path
    with requests.Session() as s:
        s.mount("https://", _PinnedAdapter(pin))
        try:
            r = s.request(method, url, json=json, timeout=TIMEOUT, verify=False,
                          headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
        except requests.exceptions.SSLError as e:
            if "ingerprint" in str(e):
                raise AccessError("pin_mismatch", "Access's certificate changed") from e
            raise AccessError("unreachable", f"Couldn't reach {host}") from e
        except requests.exceptions.ConnectTimeout as e:
            raise AccessError("unreachable", f"Couldn't reach {host}") from e
        except requests.exceptions.ReadTimeout as e:
            raise AccessError("timeout", "No reply from Access") from e
        except requests.exceptions.ConnectionError as e:
            raise AccessError("unreachable", f"Couldn't reach {host}") from e
        except requests.RequestException as e:
            raise AccessError("unreachable", f"Couldn't reach {host}") from e
    try:
        body = r.json()
    except ValueError:
        body = None
    code = body.get("code") if isinstance(body, dict) else None
    if r.status_code in (401, 403) or code in ("CODE_UNAUTHORIZED", "CODE_AUTH_FAILED", "CODE_ACCESS_TOKEN_INVALID"):
        raise AccessError("unauthorized", "Access turned down that token")
    if code == "CODE_NOT_EXISTS" or r.status_code == 404:
        raise AccessError("not_found", "Access doesn't know that door")
    if r.status_code >= 400 or code != "SUCCESS":
        log.warning("Access %s %s: HTTP %s code %s", method, path.split("/")[1], r.status_code, code)
        raise AccessError("bad_response", "Access sent an unexpected reply")
    return body


def parse_doors(body):
    """``[{id, name}]`` from a GET /doors reply, sorted by name."""
    data = body.get("data") if isinstance(body, dict) else None
    if not isinstance(data, list):
        raise AccessError("bad_response", "Access sent an unexpected reply")
    doors = []
    for d in data:
        if isinstance(d, dict) and isinstance(d.get("id"), str) and d["id"]:
            doors.append({"id": d["id"], "name": str(d.get("name") or d.get("full_name") or d["id"])})
    return sorted(doors, key=lambda d: d["name"].lower())


def list_doors(host, token, pin=None):
    """The console's doors; with no pin, the certificate is fetched and trusted first."""
    pin = clean_pin(pin) or fetch_pin(host)
    return parse_doors(_request("GET", host, token, "/doors", pin))


def unlock(host, token, door_id, actor_id, actor_name, pin):
    """Momentarily unlock a door (Access relocks it)."""
    body = {"actor_id": actor_id, "actor_name": actor_name} if actor_id and actor_name else None
    _request("PUT", host, token, f"/doors/{requests.utils.quote(door_id, safe='')}/unlock", pin, json=body)
    return True
