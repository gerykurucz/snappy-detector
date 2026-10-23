"""License client for the SnappyAI desktop app (v9.x).

Drop-in replacement for the GitHub-fetching half of `online.py`. Written in the
same dependency-free style: stdlib only, plus your existing `edsig` module for
signature verification. No `requests`, no `cryptography`, nothing to add to the
PyInstaller freeze.

WHAT CHANGED vs the GitHub edition
----------------------------------
  * the entitlement source is an HTTP API, not a signed file in a public repo;
  * presence heartbeats are authenticated beats, not posts to a Discord webhook
    (so nobody can forge one and pause a paying customer);
  * seat arbitration is server-side and instant, so "in use elsewhere" is known
    in ~15 ms instead of ~20 minutes;
  * the Discord OAuth client secret is NO LONGER SHIPPED. The token exchange
    happens on the server; this module only ever sees a one-time session_code;
  * offline behaviour is preserved: a cached, signed, in-grace grant keeps the
    engine running when the server cannot be reached.

Strict policy is unchanged: no live verified answer AND no in-grace cached
grant = no engine.

INTEGRATION
-----------
Two hooks are injectable so this module never has to know about your browser
plumbing:

    license_client.OPEN_URL = online._open_url
    license_client.PROTECT   = online._dpapi_protect
    license_client.UNPROTECT = online._dpapi_unprotect

Typical use from gui.py:

    import license_client as lc

    lc.configure(base_url="https://api.example.com",
                 pubkey=build_secrets.entitlement_pubkey(),
                 secure_dir=online.secure_dir())

    session = lc.sign_in()
    ok, why = lc.gate(session)
    lc.start_heartbeat(session)
    ...
    lc.stop_heartbeat()
    lc.release(session)

SERVER REQUIREMENT
------------------
This module speaks the v1 HTTP API and nothing else. The single-file
entitlement publisher (`worker.js`, the Stripe-webhook-to-public-repo build)
does NOT implement it: it serves /healthz, /webhooks/stripe,
/entitlements.json and /admin/* only, so pointing this client at that worker
404s on every call and the gate refuses everyone. `required_routes()` is the
contract a license server has to answer. Until one exists the app ships the
GitHub-fetching path in `online.py`, and this module stays out of the freeze.
"""

from __future__ import annotations

import base64
import json
import os
import platform
import secrets
import socket
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

try:
    import system_core as edsig
except ImportError:
    edsig = None

try:
    import system_core as i18n
except ImportError:
    i18n = None

if i18n is not None:
    i18n.publish("en", {
        "gate.msg.no_subscription": "No subscription found for this "
                                    "Discord account.",
        "seat.expired": "Connection was lost and the session lapsed. "
                        "Signing in again will reclaim it.",
        "seat.preempted": "Another computer took over this session.",
        "seat.takeover_cooldown": "You switched computers too recently. "
                                  "Please wait a couple of minutes.",
    })





BASE_URL = ""
ENTITLEMENT_PUBKEY = ""
SECURE_DIR = ""

LOOPBACK_HOST = "127.0.0.1"
LOOPBACK_PORT = 48321
LOOPBACK_PATH = "/oauth"

HTTP_TIMEOUT = 15.0
LOGIN_TIMEOUT = 300.0
BEAT_JITTER_SECONDS = 3




LOCAL_GRACE_CEILING_SECONDS = 6 * 3600


OPEN_URL = webbrowser.open
PROTECT = None
UNPROTECT = None







DEV_AUTHORIZE_URL = ""

_log = []


def log(message):
    """English, append-only. Support evidence, same convention as online.py."""
    line = "%s %s" % (time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()), message)
    _log.append(line)
    try:
        sys.stderr.write("[license] " + line + "\n")
    except Exception:
        pass


def recent_log(limit=40):
    return list(_log[-limit:])


class LicenseError(Exception):
    """Everything degrades to this so the UI shows 'unreachable - retry'
    instead of a traceback."""


def configure(base_url=None, pubkey=None, secure_dir=None):
    """Set the deployment constants. Values come from build_secrets, never
    from config.json -- a user must not be able to repoint the app."""
    global BASE_URL, ENTITLEMENT_PUBKEY, SECURE_DIR
    if base_url:
        BASE_URL = base_url.rstrip("/")
    if pubkey:
        ENTITLEMENT_PUBKEY = pubkey.strip()
    if secure_dir:
        SECURE_DIR = secure_dir
    if not BASE_URL:
        raise LicenseError("license base_url is not configured")
    if not ENTITLEMENT_PUBKEY:
        raise LicenseError("no Ed25519 public key - a subscription can never "
                           "be verified, so nobody could get past the gate")
    if edsig is None:





        raise LicenseError("edsig module is missing from this build - grants "
                           "cannot be verified, so no subscription could ever "
                           "be confirmed. Check the PyInstaller spec includes "
                           "edsig.py.")
    if not SECURE_DIR:
        SECURE_DIR = _default_secure_dir()
    return BASE_URL


API_PREFIX = "/api/v1"

REQUIRED_ROUTES = (
    ("GET", API_PREFIX + "/auth/discord/start"),
    ("POST", API_PREFIX + "/auth/session"),
    ("POST", API_PREFIX + "/access/beat"),
    ("POST", API_PREFIX + "/access/takeover"),
    ("POST", API_PREFIX + "/access/release"),
    ("GET", API_PREFIX + "/access/status"),
)


def required_routes():
    """(method, path) pairs a license server must implement."""
    return REQUIRED_ROUTES


def probe_server(timeout=10.0):
    """(ok, detail) - does BASE_URL actually answer this client's API?

    Run it once at deployment time, not per start-up: it exists so wiring
    this module to the publisher worker fails here with a readable verdict
    instead of as a cascade of 404s inside the gate.
    """
    missing = []
    for method, path in REQUIRED_ROUTES:
        try:
            status, _body = _request(path, method=method, timeout=timeout)
        except LicenseError as exc:
            return False, "server unreachable: %s" % exc
        if status == 404 or status == 405:
            missing.append("%s %s" % (method, path))
    if missing:
        return False, "server does not implement: %s" % ", ".join(missing)
    return True, "all %d routes answered" % len(REQUIRED_ROUTES)


def configured():
    return bool(BASE_URL and ENTITLEMENT_PUBKEY)


def _default_secure_dir():
    if os.name == "nt":
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "SnappyAI")
    else:
        path = os.path.join(os.path.expanduser("~"), ".snappyai")
    os.makedirs(path, exist_ok=True)
    return path


def _T(key, fallback):
    """Catalog lookup with an inline fallback, so this module works before the
    strings are added to i18n."""
    if i18n is not None:
        try:
            value = i18n.L(key)
            if value and value != key:
                return value
        except Exception:
            pass
    return fallback





def device_id():
    """A stable per-install identifier.

    Persisted once, next to session.json. Deliberately NOT a hardware
    fingerprint: a driver or BIOS update changes a fingerprint, which looks
    exactly like a second machine and evicts a paying customer for no reason.
    """
    path = os.path.join(SECURE_DIR or _default_secure_dir(), "device_id")
    try:
        with open(path, "r", encoding="utf-8") as fh:
            value = fh.read().strip()
            if len(value) >= 16:
                return value
    except OSError:
        pass
    value = secrets.token_hex(16)
    try:
        try:
            os.makedirs(os.path.dirname(path), exist_ok=True)
        except Exception:
            pass
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(value)
    except OSError as exc:
        log("could not persist device_id: %s" % exc)
    return value


def device_label():
    """A short label the account owner will recognise in the 'in use elsewhere'
    dialog. Never send a hostname, a MAC or an IP: the person reading that
    dialog may be a sibling, and they only need enough to tell two of their own
    machines apart - so this is the OS plus the first six characters of the
    random per-install id."""
    try:
        system = platform.system() or "PC"
        release = platform.release() or ""
        label = " ".join(x for x in (system, release) if x).strip()
        return ("%s (%s)" % (label or "This computer",
                             device_id()[:6]))[:64]
    except Exception:
        return "This computer"





def _request(path, data=None, token=None, timeout=None, method=None):
    """(status, parsed_body). Raises LicenseError only when the server could
    not be reached at all -- an HTTP error status is a normal answer here."""
    url = BASE_URL + path
    headers = {
        "Accept": "application/json",
        "User-Agent": "SnappyAI/9.0 (+license-client)",
    }
    body = None
    if data is not None:
        body = json.dumps(data).encode("utf-8")
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, data=body, headers=headers,
                                     method=method or ("POST" if body else "GET"))
    try:
        with urllib.request.urlopen(request, timeout=timeout or HTTP_TIMEOUT) as resp:
            raw = resp.read().decode("utf-8", "replace")
            try:
                data = json.loads(raw) if raw.strip() else {}
            except ValueError:
                raise LicenseError("invalid JSON from server")
            return resp.status, data
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except ValueError:
            return exc.code, {"detail": raw[:200]}
    except Exception as exc:
        raise LicenseError("license server unreachable: %s" % exc)





class _CallbackHandler(BaseHTTPRequestHandler):
    server_version = "SnappyAI/9.0"

    def do_GET(self):
        parsed = urllib.parse.urlparse(self.path)
        if parsed.path != LOOPBACK_PATH:
            self.send_error(404)
            return
        query = urllib.parse.parse_qs(parsed.query)
        code = (query.get("session_code") or [""])[0]
        error = (query.get("error") or [""])[0]
        type(self).received = {"session_code": code, "error": error}

        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.end_headers()
        message = ("Signed in. You can close this tab and return to SnappyAI."
                   if code else "Sign-in failed. Close this tab and try again.")
        self.wfile.write(
            ("<!doctype html><meta charset='utf-8'><title>SnappyAI</title>"
             "<body style='font-family:system-ui;padding:3rem;text-align:center'>"
             "<h2>%s</h2></body>" % message).encode("utf-8")
        )

    def log_message(self, *args):
        pass


def _free_port(preferred):
    """Use the preferred port when it is free, otherwise let the OS choose.

    The fixed-port-only behaviour of the old flow was a support generator: one
    leftover process on 48321 and sign-in could not start at all.
    """
    for candidate in (preferred, 0):
        try:
            sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            sock.bind((LOOPBACK_HOST, candidate))
            port = sock.getsockname()[1]
            sock.close()
            return port
        except OSError:
            continue
    raise LicenseError("no free loopback port for the sign-in hand-back")


def _listen_for_session_code(port, timeout):
    """Block until the browser redirects back with ?session_code=..., or timeout.

    Returns as soon as the hand-back arrives. An earlier version of this waited
    out the whole LOGIN_TIMEOUT even after the code had been received, because
    the serve loop only exited on its deadline -- which made every sign-in take
    five minutes. The loop now polls `received` between short blocking accepts,
    so it returns within ~0.2 s of the redirect landing.
    """
    _CallbackHandler.received = None
    try:
        httpd = ThreadingHTTPServer((LOOPBACK_HOST, port), _CallbackHandler)
    except OSError as exc:
        raise LicenseError("no free loopback port for the sign-in hand-back (%s)"
                           % exc)

    httpd.timeout = 0.2
    deadline = time.time() + timeout
    server_thread = threading.Thread(target=_serve_until, args=(httpd, deadline),
                                     daemon=True, name="license-callback")
    server_thread.start()
    try:
        while time.time() < deadline:
            if _CallbackHandler.received is not None:
                break
            time.sleep(0.05)
    finally:



        httpd.timeout = 0.0
        try:
            httpd.server_close()
        except Exception:
            pass
        server_thread.join(timeout=2)

    got = _CallbackHandler.received or {}
    if got.get("error"):
        raise LicenseError(_T("online.cancelled",
                              "Discord authentication was cancelled (or failed). "
                              "Please try again."))
    code = got.get("session_code") or ""
    if not code:
        raise LicenseError("sign-in timed out waiting for the browser hand-back")
    return code


def _serve_until(httpd, deadline):
    """Serve until the deadline. Exits early once server_close() drops the
    socket, which is how _listen_for_session_code stops it."""
    while time.time() < deadline:
        try:
            httpd.handle_request()
        except OSError:
            return
        if _CallbackHandler.received is not None:
            return





def start_sign_in():
    """Begin a Discord sign-in. Returns (authorize_url, loopback_port).

    The redirect_uri given to the server must match the loopback URL, so the
    server needs to know the port. Two supported shapes:

      A) the server's DISCORD_REDIRECT_URI is your loopback URL and you pass
         `redirect=` to the callback (Discord must have that exact URI
         registered, so this only works for one fixed port); or
      B) -- the usual one -- the server's redirect_uri is its OWN callback, and
         it 302s back to your loopback with ?session_code=... That needs no
         Discord-side change per port. `start_sign_in` requests shape B.
    """
    port = _free_port(LOOPBACK_PORT)
    status, body = _request("/api/v1/auth/discord/start")
    if status != 200:
        raise LicenseError("could not start sign-in: %s" % body.get("detail"))
    authorize_url = body.get("authorize_url") or ""
    if not authorize_url:
        raise LicenseError("server returned no authorize_url")

    if DEV_AUTHORIZE_URL and not getattr(sys, "frozen", False):

        state = urllib.parse.parse_qs(
            urllib.parse.urlparse(authorize_url).query).get("state", [""])[0]
        authorize_url = "%s%scode=dev-code&state=%s" % (
            DEV_AUTHORIZE_URL, "&" if "?" in DEV_AUTHORIZE_URL else "?", state)
        log("DEV_AUTHORIZE_URL is set - skipping the provider hop")


    loopback = "http://%s:%d%s" % (LOOPBACK_HOST, port, LOOPBACK_PATH)
    separator = "&" if "?" in authorize_url else "?"
    authorize_url = authorize_url + separator + "redirect=" + urllib.parse.quote(loopback, safe="")

    log("sign-in started; waiting for the browser hand-back on port %d" % port)
    return authorize_url, port


def sign_in(timeout=None, opener=None):
    """Full Discord sign-in. Blocks until the round trip completes.

    Returns the SessionResponse dict (see `gate`). Raises LicenseError when the
    server cannot be reached or the user cancels.
    """
    if not configured():
        raise LicenseError("license client is not configured")

    authorize_url, port = start_sign_in()
    listener_error = {}

    def _wait():
        try:
            listener_error["code"] = _listen_for_session_code(
                port, timeout or LOGIN_TIMEOUT)
        except Exception as exc:
            listener_error["exc"] = exc

    waiter = threading.Thread(target=_wait, daemon=True, name="license-wait")
    waiter.start()




    try:
        opened = (opener or OPEN_URL)(authorize_url)
        if not opened:
            log("browser opener returned False")
            raise LicenseError("could not open browser")
    except Exception as exc:
        log("could not open the browser: %s" % exc)
        _CallbackHandler.received = {}
        waiter.join(timeout=5)
        raise LicenseError(_T("online.cancelled",
                              "Discord authentication could not start. "
                              "Please try again."))

    waiter.join(timeout=(timeout or LOGIN_TIMEOUT) + 5)
    if "exc" in listener_error:
        raise listener_error["exc"]
    session_code = listener_error.get("code")
    if not session_code:
        raise LicenseError("sign-in did not complete")

    return exchange_session(session_code)


def exchange_session(session_code):
    """Trade the one-time session_code for credentials + the seat verdict."""
    status, body = _request("/api/v1/auth/session", data={
        "session_code": session_code,
        "device_id": device_id(),
        "device_label": device_label(),
    })
    if status == 401:
        raise LicenseError("the sign-in code expired - please sign in again")
    if status not in (200, 409):
        raise LicenseError("sign-in failed: %s" % body.get("detail"))
    if body.get("grant"):
        cache_grant(body["grant"])
    if body.get("access_token"):
        save_session(body)
    log("sign-in complete: allowed=%s reason=%s scope=%s"
        % (body.get("allowed"), body.get("reason"), body.get("token_scope")))
    return body







BEAT_RECLAIM_ATTEMPTS = 3
BEAT_RECLAIM_BACKOFF_SECONDS = (1.0, 3.0, 6.0)


def beat(session, reclaim=True):
    """Renew the lease and return the current verdict (a SessionResponse).

    Raises LicenseError only when the server cannot be reached. A 401 means the
    access token expired and the caller must re-authenticate with the same
    session (the server re-issues on every successful beat).

    WHY THIS RETRIES. The server distinguishes two kinds of lost seat, and they
    need opposite reactions:

      seat-expired    the lease lapsed: a crash, a sleep, a network outage.
                      Nobody else necessarily holds the seat. Re-claiming almost
                      always succeeds, so we try up to BEAT_RECLAIM_ATTEMPTS
                      times with a short backoff before telling the user
                      anything. Without this, one dropped packet on hotel wifi
                      produced "account in use elsewhere" for a customer who was
                      the only person using the account -- a support ticket you
                      cannot win.

      seat-preempted  someone took the seat over, or the account was suspended.
                      Retrying is pointless and would fight the other machine.
                      Surface it immediately.

    A re-claim is a POST to /access/beat with the same device_id, which the
    server treats as a fresh claim for a device whose lease lapsed.
    """
    token = (session or {}).get("access_token")
    if not token:
        raise LicenseError("no access token - sign in first")

    body = _beat_once(session, token)
    if not reclaim or body.get("reason") != "seat-expired":
        return body

    for attempt, delay in enumerate(BEAT_RECLAIM_BACKOFF_SECONDS, start=1):
        log("lease lapsed (seat-expired); re-claiming, attempt %d/%d in %.0fs"
            % (attempt, BEAT_RECLAIM_ATTEMPTS, delay))
        time.sleep(delay)
        token = (session or {}).get("access_token") or token
        body = _beat_once(session, token)
        if body.get("reason") != "seat-expired":
            if body.get("allowed"):
                log("seat re-claimed after %d attempt(s)" % attempt)
            return body
    log("lease lapsed and could not be re-claimed after %d attempts"
        % BEAT_RECLAIM_ATTEMPTS)
    return body


_SESSION_FIELDS = ("access_token", "token_scope", "expires_in",
                   "heartbeat_in", "seat_nonce", "device_id", "email_hash",
                   "grant")


def _absorb(session, body):
    """Fold a SessionResponse into the live session, then persist the merge.

    Saving the response wholesale used to replace the session file, so every
    field the server left out (identity hash, lease timing) disappeared from
    disk and the next start-up had to sign in again.
    """
    if not isinstance(session, dict) or not isinstance(body, dict):
        return session
    for key in _SESSION_FIELDS:
        if body.get(key) is not None:
            session[key] = body[key]
    if body.get("access_token"):
        save_session(session)
    return session


def _beat_once(session, token):
    status, body = _request("/api/v1/access/beat", data={
        "device_id": device_id(),
        "device_label": device_label(),
    }, token=token)
    if status == 401:
        raise LicenseError("session expired - sign in again")
    if status not in (200, 409):
        raise LicenseError("heartbeat failed: %s" % body.get("detail"))
    if body.get("grant"):
        cache_grant(body["grant"])
    _absorb(session, body)
    return body


def take_over(session):
    """Evict every other device on this account and claim the seat.

    This is the recovery path for the customer whose laptop is asleep in another
    country. Rate-limited server-side (one per device per 120 s), audited, and
    repeated use is flagged as probable account sharing.
    """
    token = _token_for(session)
    status, body = _request("/api/v1/access/takeover", data={
        "device_id": device_id(),
        "device_label": device_label(),
    }, token=token)
    if status == 429:
        raise LicenseError(_T("seat.takeover_cooldown",
                              "You switched computers too recently. "
                              "Please wait a couple of minutes."))
    if status == 401:
        raise LicenseError("session expired - sign in again")
    if status not in (200, 409):
        raise LicenseError("takeover failed: %s" % body.get("detail"))
    if body.get("grant"):
        cache_grant(body["grant"])
    _absorb(session, body)
    log("takeover: allowed=%s message=%s" % (body.get("allowed"), body.get("message")))
    return body


def release(session):
    """Graceful exit: free the seat at once instead of making the other machine
    wait out the lease. Best effort -- a failure just means the lease lapses."""
    token = (session or {}).get("access_token")
    if not token:
        return False
    try:
        status, body = _request("/api/v1/access/release",
                          data={"device_id": device_id()}, token=token, timeout=5)
        if 200 <= status < 300:
            log("seat released")
            return True
        return False
    except LicenseError as exc:
        log("release failed (the lease will lapse on its own): %s" % exc)
        return False


def seat_status(session):
    """Live counter for the 'account in use elsewhere' dialog."""
    status, body = _request("/api/v1/access/status", token=_token_for(session))
    if status != 200:
        raise LicenseError("could not read seat status")
    return body


def _token_for(session):
    token = (session or {}).get("access_token")
    if not token:
        raise LicenseError("no access token - sign in first")
    return token





def canonical_bytes(doc):
    """Identical to online._canonical_bytes / builder.sign_document:
    sorted keys, compact separators, `sig` excluded."""
    payload = {k: v for k, v in doc.items() if k != "sig"}
    return json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")


def verify_grant(grant):
    """True only for a valid Ed25519 signature under the pinned public key."""
    if edsig is None:
        log("edsig module unavailable - cannot verify the grant")
        return False
    if not isinstance(grant, dict):
        return False
    try:
        pub = base64.b64decode(ENTITLEMENT_PUBKEY)
        sig = base64.b64decode(str(grant.get("sig", "")))
    except Exception:
        return False
    try:
        return bool(edsig.verify(pub, canonical_bytes(grant), sig))
    except Exception:
        return False


def grant_row(grant, identity_hash):
    """This customer's row out of a grant. A grant only ever contains ONE row,
    which is the privacy fix: there is no public list of subscribers any more."""
    rows = (grant or {}).get("rows")
    if not isinstance(rows, dict) or not identity_hash:
        return None
    row = rows.get(identity_hash)
    return row if isinstance(row, dict) else None


def _grace_deadline(grant, cached_at):
    """min(server not_after, when THIS COPY was cached + the local ceiling).

    Both ends matter: `not_after` alone would let a grant cached a month ago
    keep the engine running offline for as long as the subscription lasts, and
    the ceiling alone would ignore a subscription that ends tomorrow. An
    unknown cache time answers 0, which fails closed.
    """
    try:
        not_after = int((grant or {}).get("not_after", 0))
    except (TypeError, ValueError):
        return 0
    try:
        cached_at = int(cached_at or 0)
    except (TypeError, ValueError):
        return 0
    if not_after <= 0 or cached_at <= 0:
        return 0
    return min(not_after, cached_at + LOCAL_GRACE_CEILING_SECONDS)


def grant_within_grace(grant, cached_at=0):
    """Trust the SMALLER of the server's not_after and the local ceiling."""
    return _grace_deadline(grant, cached_at) > int(time.time())


def grant_remaining_seconds(grant, cached_at=0):
    return max(0, _grace_deadline(grant, cached_at) - int(time.time()))


def grant_device_matches(grant):
    """A grant is bound to one install. A copied grant must not authorise a
    second machine -- seats are arbitrated by the server and the grant says so
    (`seat.authoritative` is always false)."""
    seat = (grant or {}).get("seat") or {}
    return seat.get("device_id") == device_id()


ACTIVE_STATUSES = ("trial", "active", "grant", "canceling")


def row_is_active(row, now=None):
    if not isinstance(row, dict):
        return False
    if str(row.get("status", "")) not in ACTIVE_STATUSES:
        return False
    try:
        return float(row.get("until", 0) or 0) > (now if now is not None else time.time())
    except (TypeError, ValueError):
        return False





def gate(session, identity_hash=None):
    """(allowed, reason, detail). The single decision point gui.py calls.

    Fallback ladder, strict at every step:
      1. live beat            -> authoritative, ~15 ms
      2. cached signed grant  -> only if it verifies, is within grace, and
                                 names THIS device
      3. otherwise            -> refuse
    """
    detail = {}
    identity_hash = identity_hash or (session or {}).get("email_hash") or ""


    if (session or {}).get("access_token"):
        try:
            body = beat(session)
        except LicenseError as exc:
            log("beat failed, falling back to the cached grant: %s" % exc)
            body = None
        if body is not None:
            detail = body
            grant = body.get("grant")
            if not body.get("allowed"):
                reason = str(body.get("reason") or "denied")
                log("gate: refused by the server (%s)" % reason)
                return False, reason, body


            if grant is not None and not verify_grant(grant):
                log("gate: server said allowed but the grant signature is "
                    "INVALID - refusing")
                return False, "bad_signature", body

            row = grant_row(grant, identity_hash)
            if row is None:

                ent = body.get("entitlement") or {}
                try:
                    until = float(ent.get("until") or 0)
                except Exception:
                    until = 0.0
                if ent.get("active") and until > time.time():
                    return True, "live", body
                return False, "no_entitlement", body
            if not row_is_active(row):
                return False, str(row.get("status") or "no_entitlement"), body
            return True, "live", body


    cached, cached_at = load_cached_grant()
    if cached is not None:
        if not verify_grant(cached):
            log("gate: cached grant failed verification - refusing")
            return False, "bad_signature", detail
        if not grant_device_matches(cached):
            log("gate: cached grant belongs to another install - refusing")
            return False, "wrong_device", detail
        if not grant_within_grace(cached, cached_at):
            log("gate: cached grant is outside the offline grace window "
                "(cached %ds ago) - refusing"
                % (int(time.time()) - cached_at,))
            return False, "unreachable", detail
        row = grant_row(cached, identity_hash)
        if row_is_active(row):
            remaining = grant_remaining_seconds(cached, cached_at)
            log("gate: offline, running on the cached grant (%ds of grace left)"
                % remaining)
            detail = {"offline": True, "grace_remaining": remaining, "grant": cached}
            return True, "offline-grace", detail
        return False, str((row or {}).get("status") or "no_entitlement"), detail


    log("gate: server unreachable and no usable cached grant - refusing")
    return False, "unreachable", detail





_heartbeat = {"thread": None, "stop": None, "last": None, "verdict": None}


def start_heartbeat(session, on_verdict=None):
    """Renew the lease in the background for as long as the engine runs.

    `on_verdict(allowed, reason, detail)` is called on EVERY beat, so the UI can
    pause the moment another machine takes over -- which is what turns the
    1-seat limit from an annoyance into something a customer understands.

    With the shipped defaults the beat is 45 s and the lease is 600 s, so a
    client survives four consecutive missed beats. That margin is deliberate:
    silence is not proof of death, and evicting a paying customer over hotel
    wifi produces the one support ticket you cannot win. What reclaims a seat
    quickly is take_over(), not the lease expiring.
    """
    stop_heartbeat()
    stop = threading.Event()
    _heartbeat["stop"] = stop
    _heartbeat["verdict"] = None

    def _run():
        interval = float((session or {}).get("heartbeat_in") or 45)
        interval = max(15.0, min(interval, 300.0))
        while not stop.is_set():
            try:
                allowed, reason, detail = gate(session)
            except Exception as exc:
                log("heartbeat error: %s" % exc)
                allowed, reason, detail = False, "error", {}
            _heartbeat["last"] = time.time()
            _heartbeat["verdict"] = (allowed, reason, detail)
            if on_verdict is not None:
                try:
                    on_verdict(allowed, reason, detail)
                except Exception as exc:
                    log("on_verdict callback raised: %s" % exc)
            jitter = secrets.randbelow(BEAT_JITTER_SECONDS * 1000) / 1000.0
            if stop.wait(interval + jitter):
                return

    thread = threading.Thread(target=_run, daemon=True, name="license-heartbeat")
    _heartbeat["thread"] = thread
    thread.start()
    log("heartbeat started")
    return thread


def stop_heartbeat():
    stop = _heartbeat.get("stop")
    if stop is not None:
        stop.set()
    thread = _heartbeat.get("thread")
    if thread is not None and thread.is_alive():
        thread.join(timeout=3)
    _heartbeat["thread"] = None
    _heartbeat["stop"] = None
    log("heartbeat stopped")


def last_verdict():
    """(allowed, reason, detail, age_seconds) from the most recent beat, or None."""
    verdict = _heartbeat.get("verdict")
    last = _heartbeat.get("last")
    if verdict is None or last is None:
        return None
    return verdict[0], verdict[1], verdict[2], time.time() - last


def heartbeat_alive(max_age=None):
    last = _heartbeat.get("last")
    if last is None:
        return False
    return (time.time() - last) <= (max_age or 180)





def _protect(raw):
    """At-rest protection, degrading to plaintext rather than failing.

    The DPAPI hook returns None off-Windows and when protection fails; that
    must not become a TypeError inside save_session(), which would surface to
    the customer as a crash at sign-in. Same reasoning as online.save_session(),
    which explicitly falls back when DPAPI round-trips badly.
    """
    if PROTECT is not None:
        try:
            blob = PROTECT(raw)
        except Exception as exc:
            log("PROTECT hook raised, storing obfuscated: %s" % exc)
            return raw
        if blob is None:
            log("PROTECT hook returned None (DPAPI unavailable) - storing "
                "obfuscated, not encrypted")
            return raw
        return blob
    return raw


def _unprotect(raw):
    """Inverse of _protect. Returns b"" rather than None on any failure, so
    callers can treat the result as bytes unconditionally."""
    if UNPROTECT is not None:
        try:
            out = UNPROTECT(raw)
        except Exception as exc:
            log("UNPROTECT hook raised: %s" % exc)
            return b""
        if out is None:

            return b""
        return out
    return raw












def _write_protected(path, payload_bytes):
    """Write bytes under DPAPI when available, obfuscated otherwise."""
    prot = "b64"
    blob = _protect(payload_bytes)
    if isinstance(blob, (bytes, bytearray)) and blob != payload_bytes:
        roundtrip = _unprotect(bytes(blob))
        if roundtrip == payload_bytes:
            payload_bytes, prot = bytes(blob), "dpapi"
        else:
            log("%s: DPAPI round-trip failed - storing obfuscated, not encrypted"
                % os.path.basename(path))
    envelope = json.dumps({"v": 1, "prot": prot,
                           "data": base64.b64encode(payload_bytes).decode()})
    with open(path, "wb") as fh:
        fh.write(envelope.encode("utf-8"))
    return True


def _read_protected(path):
    """Inverse of _write_protected. Returns bytes, or None."""
    try:
        with open(path, "rb") as fh:
            envelope = json.loads(fh.read().decode("utf-8"))
    except (OSError, ValueError):
        return None
    try:
        raw = base64.b64decode(envelope.get("data", ""))
    except (ValueError, TypeError):
        return None
    if envelope.get("prot") == "dpapi":
        raw = _unprotect(raw)
        if not raw:
            # _unprotect answers b"" (never None) on failure per its
            # contract - a DPAPI blob from another Windows profile cannot
            # be decrypted here.
            log("%s was protected for another Windows profile - ignoring it"
                % os.path.basename(path))
            return None
    return raw


def _path(name):
    return os.path.join(SECURE_DIR or _default_secure_dir(), name)






TOKEN_FILE = "license_token.json"


def save_session(session):
    """Persist only what is needed to resume: the token, the identity hash and
    the lease timing. No email, no provider tokens, no card data."""
    keep = {
        "access_token": session.get("access_token"),
        "token_scope": session.get("token_scope"),
        "expires_in": session.get("expires_in"),
        "heartbeat_in": session.get("heartbeat_in"),
        "seat_nonce": session.get("seat_nonce"),
        "device_id": session.get("device_id"),
        "email_hash": (session.get("grant") or {}).get("subject", {}).get("email_hash")
                      or session.get("email_hash"),
        "saved_at": int(time.time()),
    }
    try:
        raw = json.dumps(keep).encode("utf-8")
        with open(_path("license_token.json"), "wb") as fh:
            fh.write(_protect(raw))
    except OSError as exc:
        log("could not save the session: %s" % exc)


def load_session():
    try:
        with open(_path("license_token.json"), "rb") as fh:
            data = json.loads(_unprotect(fh.read()).decode("utf-8"))
    except (OSError, ValueError):
        return None
    if not isinstance(data, dict) or not data.get("access_token"):
        return None
    return data


def clear_session():
    for name in (TOKEN_FILE, "grant.json"):
        try:
            os.remove(_path(name))
        except OSError:
            pass


def cache_grant(grant):
    """Store the signed grant for offline use, stamped with the time it was
    fetched. Verify BEFORE caching: writing a bad document would poison the
    only fallback the customer has. The stamp sits OUTSIDE the signed grant so
    the signature still verifies byte-for-byte."""
    if not verify_grant(grant):
        log("refusing to cache a grant that does not verify")
        return False
    envelope = {"cached_at": int(time.time()), "grant": grant}
    try:
        _write_protected(_path("grant.json"),
                         json.dumps(envelope).encode("utf-8"))
        return True
    except (OSError, TypeError, ValueError) as exc:
        log("could not cache the grant: %s" % exc)
        return False


def load_cached_grant():
    """(grant, cached_at); (None, 0) when there is nothing usable. A bare
    grant file written by an older build falls back to its mtime."""
    path = _path("grant.json")
    raw = _read_protected(path)
    if raw is None:
        return None, 0
    try:
        doc = json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None, 0
    if not isinstance(doc, dict):
        return None, 0
    cached_at = 0
    if isinstance(doc.get("grant"), dict):
        grant = doc["grant"]
        try:
            cached_at = int(doc.get("cached_at") or 0)
        except (TypeError, ValueError):
            cached_at = 0
    else:
        grant = doc
    if not cached_at:
        try:
            cached_at = int(os.path.getmtime(path))
        except OSError:
            cached_at = 0
    return grant, cached_at


def cached_grant_state():
    """For the settings dialog: how much offline grace is left."""
    grant, cached_at = load_cached_grant()
    if grant is None:
        return {"present": False}
    return {
        "present": True,
        "verifies": verify_grant(grant),
        "device_matches": grant_device_matches(grant),
        "within_grace": grant_within_grace(grant, cached_at),
        "grace_remaining_seconds": grant_remaining_seconds(grant, cached_at),
        "cached_at": cached_at,
        "issued_at": grant.get("issued_at"),
    }





def describe_seats(body):
    """Human text for the 'account in use elsewhere' dialog."""
    seats = (body or {}).get("seats") or {}
    holders = seats.get("holders") or []
    in_use = seats.get("in_use", 0)
    max_seats = seats.get("max_seats", 1)
    if not holders:
        return _T("shared.body", "This account is in use on another computer.")
    labels = [h.get("device_label") or "another computer" for h in holders]
    retry = body.get("retry_after") or 0
    text = "%s (%d/%d)" % (", ".join(labels), in_use, max_seats)
    if retry:
        text += " - free in about %d s" % retry
    return text


def describe_reason(reason, body=None):
    """One-line explanation per refusal reason, so the UI never shows a bare
    status string to a customer."""
    table = {
        "seat_taken": _T("shared.title", "Account in use elsewhere"),
        "no_entitlement": _T("gate.msg.no_subscription",
                             "No subscription found for this Discord account."),
        "past_due": _T("ent.st.past_due",
                       "Payment failed - update your card to restore access."),
        "canceled": _T("ent.st.canceled", "Your subscription has ended."),
        "refunded": "This subscription was refunded and is no longer active.",
        "bad_signature": "The licence could not be verified. Please contact support.",
        "wrong_device": "This licence belongs to a different installation.",
        "unreachable": _T("shared.unreachable", "Server unreachable - will re-check."),


        "seat-preempted": _T("seat.preempted",
                             "Another computer took over this session."),
        "seat-expired": _T("seat.expired",
                           "Connection was lost and the session lapsed. "
                           "Signing in again will reclaim it."),
        "seat-lost": _T("seat.preempted",
                        "Another computer took over this session."),
    }
    if reason in table:
        return table[reason]
    return (body or {}).get("message") or str(reason)
