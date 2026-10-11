import base64
import hashlib
import hmac
import json
import mimetypes
import os
import re
import secrets
import shutil
import signal
import socket
import struct
import subprocess
import threading
import time
from email.message import EmailMessage
from http.client import HTTPException
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.error import HTTPError
from urllib.parse import parse_qs, unquote, urlencode, urlsplit
from urllib.request import (HTTPBasicAuthHandler, HTTPDigestAuthHandler, HTTPPasswordMgrWithDefaultRealm, Request,
                            build_opener, urlopen)


ROOT = Path(__file__).parent
# PTZ_CONFIG points the bridge at another settings file, e.g. to try it out without touching the real one.
CONFIG_PATH = Path(os.environ.get("PTZ_CONFIG") or ROOT / "config.json")
DEFAULT_CONFIG = {
    # All interfaces, so the controller can be opened from other computers. Use "127.0.0.1" to keep it to this computer.
    "host": "0.0.0.0",
    "port": 8765,
    # The cameras the sticks drive directly, in the order the controller selects them. Their addresses,
    # and any further cameras, are entered on the app's settings page.
    "cameras": [{"name": "PTZ1"}, {"name": "PTZ2"}, {"name": "PTZ3"}],
}

# Picture adjustments, as the VISCA commands Sony cameras take for them. The gains only act while the camera
# is in manual white balance, and brightness only in the Bright exposure mode, which the last two switch on.
ADJUSTMENTS = {
    "brightness_up": "8101040d02ff",
    "brightness_down": "8101040d03ff",
    "red_up": "8101040302ff",
    "red_down": "8101040303ff",
    "blue_up": "8101040402ff",
    "blue_down": "8101040403ff",
    "manual_white_balance": "8101043505ff",
    "bright_mode": "810104390dff",
}
# An adjustment the camera hasn't taken within this long is dropped rather than turning up late.
ADJUSTMENT_SECONDS = 2

# Sent by the page with every command, so a browser tab still running older page code is refused.
CLIENT_VERSION = 8

MAX_CAMERAS = 64
# The controller buttons that select the first three cameras until the settings page says otherwise:
# left stick click, right stick click, Start.
SELECT_BUTTONS = [10, 11, 9]


def clean_cameras(cameras):
    """Check a camera list from config.json or the settings page, filling in whatever is left out."""
    if not isinstance(cameras, list) or not 1 <= len(cameras) <= MAX_CAMERAS:
        raise ValueError(f"There must be between 1 and {MAX_CAMERAS} cameras")
    cleaned = []
    for index, camera in enumerate(cameras):
        if not isinstance(camera, dict):
            raise ValueError("Each camera must be an object")
        label = f"Camera {index + 1}"
        name = str(camera.get("name") or f"PTZ{index + 1}").strip()[:24]
        host = str(camera.get("host") or "").strip()
        if len(host) > 253 or not re.fullmatch(r"[A-Za-z0-9.:-]*", host):
            raise ValueError(f"{label}: \"{host}\" is not an IP address or host name")
        port = camera.get("port", 52381)
        if isinstance(port, bool) or not isinstance(port, int) or not 1 <= port <= 65535:
            raise ValueError(f"{label}: the port must be a whole number from 1 to 65535")
        stream_url = str(camera.get("streamUrl") or "").strip()
        if stream_url and (urlsplit(stream_url).scheme not in {"http", "https", "rtsp"} or not urlsplit(stream_url).hostname):
            raise ValueError(f"{label}: the stream address must start with http://, https:// or rtsp://")
        button = camera.get("button", SELECT_BUTTONS[index] if index < len(SELECT_BUTTONS) else None)
        if button is not None and (isinstance(button, bool) or not isinstance(button, int) or not 0 <= button <= 31):
            raise ValueError(f"{label}: the select button is not a controller button")
        key = camera.get("key", str(index + 1) if index < 9 else "")
        if not isinstance(key, str) or len(key) > 1:
            raise ValueError(f"{label}: the select key must be a single key")
        cleaned.append({"name": name, "host": host, "port": port, "streamUrl": stream_url, "button": button, "key": key.lower()})
    return cleaned

TIMEOUT = 1.5


def clean_vdo(vdo):
    """Check the VDO.Ninja settings, making up a room, stream name and passwords for whatever is left out.

    VDO.Ninja has no accounts: a room exists as soon as someone opens its address, so making up the names is
    all it takes to set one up.
    """
    vdo = vdo if isinstance(vdo, dict) else {}
    base = str(vdo.get("base") or "https://vdo.ninja").strip().rstrip("/")
    if urlsplit(base).scheme not in {"http", "https"} or not urlsplit(base).hostname:
        raise ValueError("The VDO.Ninja address must start with https://")
    cleaned = {"base": base}
    for name, label, made_up, may_be_empty in (
        ("room", "room name", f"ptz{secrets.token_hex(4)}", False),
        ("feed", "feed name", f"feed{secrets.token_hex(4)}", False),
        ("password", "room password", secrets.token_hex(6), True),
        ("codirector", "co-director password", secrets.token_hex(6), False),
    ):
        value = vdo.get(name)
        value = made_up if value is None or (not value and not may_be_empty) else str(value).strip()
        if not re.fullmatch(r"[A-Za-z0-9_]{0,40}", value):
            raise ValueError(f"The VDO.Ninja {label} can only hold letters, numbers and _ (up to 40)")
        cleaned[name] = value
    crew = vdo.get("crew", [])
    if not isinstance(crew, list) or len(crew) > 50:
        raise ValueError("The crew list can hold up to 50 people")
    cleaned["crew"] = []
    for person in crew:
        email = str(person.get("email", "")).strip() if isinstance(person, dict) else ""
        if not re.fullmatch(r"[^@\s,;<>\"]+@[^@\s,;<>\"]+\.[^@\s,;<>\"]+", email) or len(email) > 254:
            raise ValueError(f"\"{email}\" is not an email address")
        cleaned["crew"].append({"email": email, "role": "codirector" if person.get("role") == "codirector" else "viewer"})
    return cleaned


def vdo_links(vdo):
    """The room's addresses. Viewers get the director's feed and nothing else; co-directors get the room's controls."""
    password = f"&password={vdo['password']}" if vdo["password"] else ""
    return {
        "director": f"{vdo['base']}/?director={vdo['room']}&push={vdo['feed']}{password}&codirector={vdo['codirector']}",
        "viewer": f"{vdo['base']}/?view={vdo['feed']}&solo&room={vdo['room']}{password}",
        "codirector": f"{vdo['base']}/?director={vdo['room']}{password}&codirector={vdo['codirector']}",
    }


GOOGLE_SIGN_IN = "https://accounts.google.com/o/oauth2/v2/auth"
GOOGLE_TOKEN = "https://oauth2.googleapis.com/token"
GOOGLE_REVOKE = "https://oauth2.googleapis.com/revoke"
GOOGLE_ACCOUNT = "https://openidconnect.googleapis.com/v1/userinfo"
GMAIL_SEND = "https://gmail.googleapis.com/gmail/v1/users/me/messages/send"
# What the controller asks Google for: who signed in, and permission to send mail as them. It cannot read mail.
GOOGLE_SCOPES = "openid email https://www.googleapis.com/auth/gmail.send"


def clean_google(google, current=None):
    """The Google sign-in used to send invites. `google` holds the OAuth client from the settings page; a blank
    secret keeps the saved one, and a different client forgets the sign-in made with the old one."""
    google = google if isinstance(google, dict) else {}
    current = {"clientId": "", "clientSecret": "", "refreshToken": "", "email": "", **(current or {})}
    client_id = str(google.get("clientId", current["clientId"])).strip()
    secret = str(google.get("clientSecret") or "").strip() or current["clientSecret"]
    if not re.fullmatch(r"[A-Za-z0-9._-]{0,200}", client_id) or not re.fullmatch(r"[A-Za-z0-9._-]{0,200}", secret):
        raise ValueError("That does not look like a Google OAuth client ID and secret")
    same_client = client_id == current["clientId"] and secret == current["clientSecret"]
    return {"clientId": client_id, "clientSecret": secret if client_id else "",
            "refreshToken": current["refreshToken"] if same_client else "", "email": current["email"] if same_client else ""}


def google_call(url, form=None, token=None, body=None):
    """Ask Google something and return its JSON answer: `form` is posted as a form, `body` as JSON, neither is a GET."""
    headers = {"Authorization": f"Bearer {token}"} if token else {}
    data = None
    if form is not None:
        data = urlencode(form).encode()
    elif body is not None:
        data = json.dumps(body).encode()
        headers["Content-Type"] = "application/json"
    try:
        with urlopen(Request(url, data=data, headers=headers), timeout=15) as reply:
            return json.loads(reply.read() or b"{}")
    except HTTPError as error:
        try:
            problem = json.loads(error.read())
        except ValueError:
            problem = {}
        detail = problem.get("error")
        code = detail if isinstance(detail, str) else (detail or {}).get("status", "")
        why = problem.get("error_description") or (detail.get("message") if isinstance(detail, dict) else "") or f"HTTP {error.code}"
        raise GoogleRefused(code, why)
    except OSError as error:
        raise RuntimeError(f"Google could not be reached: {error}")


class GoogleRefused(RuntimeError):
    def __init__(self, code, why):
        super().__init__(f"Google said no: {why}")
        self.code = code


INVITES = {
    "viewer": "You're invited to watch our production feed.\n\nOpen this link in Chrome or Safari:\n{link}\n\nYou will see and hear the director's feed.\n",
    "codirector": "You're invited to co-direct our production room.\n\nOpen this link in Chrome or Safari:\n{link}\n\nIt gives you the room's director controls.\n",
}


def send_invites(google, vdo, people):
    """Email each of `people` (entries from the crew list) the link for what they may do, as the signed-in Google account."""
    if not google.get("refreshToken"):
        raise ValueError("Sign in with Google in Settings first")
    # The sign-in is kept as a refresh token; each send trades it for a short-lived pass.
    access = google_call(GOOGLE_TOKEN, form={"client_id": google["clientId"], "client_secret": google["clientSecret"],
                                             "refresh_token": google["refreshToken"], "grant_type": "refresh_token"})["access_token"]
    links = vdo_links(vdo)
    for person in people:
        message = EmailMessage()
        message["From"] = google["email"]
        message["To"] = person["email"]
        message["Subject"] = "Production feed invite"
        message.set_content(INVITES[person["role"]].format(link=links[person["role"]]))
        google_call(GMAIL_SEND, token=access, body={"raw": base64.urlsafe_b64encode(message.as_bytes()).decode()})


def clean_password(password):
    if not isinstance(password, str) or not 6 <= len(password) <= 64 or password != password.strip() or not password.isprintable():
        raise ValueError("The remote password must be 6 to 64 characters, with no spaces at either end")
    return password


def own_addresses():
    """This computer's addresses on the local network, for telling people where to open the controller."""
    found = []
    try:
        # No packet is sent: asking which address would be used to reach the outside reveals the main one.
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            found.append(probe.getsockname()[0])
    except OSError:
        pass
    return [address for address in found if not address.startswith("127.")]


def find_ffmpeg(configured=""):
    """Where ffmpeg is: the path in config.json, the bin folder beside the bridge, or wherever the system keeps it."""
    candidates = [configured, ROOT / "bin" / "ffmpeg", ROOT / "bin" / "ffmpeg.exe", shutil.which("ffmpeg"), "/opt/homebrew/bin/ffmpeg", "/usr/local/bin/ffmpeg"]
    return next((str(candidate) for candidate in candidates if candidate and os.access(candidate, os.X_OK)), None)

# Sony cameras send their VISCA replies to this port on the sender, whatever port the command came from.
VISCA_REPLY_PORT = 52381
VISCA_COMMAND = 0x0100
VISCA_CONTROL = 0x0200
MAX_PAN_SPEED = 24
MAX_TILT_SPEED = 23
MAX_ZOOM_SPEED = 7
# A camera that is moving stops by itself if the controller page goes quiet for this long
# (tab closed, network dropped), so it can never be left running.
DRIVE_TIMEOUT = 0.7
# The cameras silently ignore a command that arrives too soon after the previous one: measured on an
# SRG-300SE, commands 20 ms apart were still being dropped and 40 ms apart all landed.
SEND_GAP = 0.04
# A command the camera hasn't answered within this long is sent again.
ACK_TIMEOUT = 0.15
STILL = (0, 0)


class CameraLink:
    """Drives the cameras' pan, tilt and zoom directly with VISCA over IP, the way an RM-IP10 does.

    Each camera gets its own sender, which works from the newest stick position rather than a queue:
    it sends one command, waits for the camera to answer and for SEND_GAP to pass, then sends whatever
    is most out of date. A command only counts once the camera has answered it, so a stop that goes
    missing is sent again until it lands.

    Each camera also only accepts a command whose sequence number is higher than the last one it saw,
    from anyone, and Companion numbers its own commands independently. So the sequence is reset when a
    move starts and again when it ends (leaving the camera at zero, below Companion's count), and if the
    camera reports a sequence error mid-move it is reset and the move carries on.
    """

    def __init__(self, cameras, reply_port=VISCA_REPLY_PORT, send_gap=SEND_GAP):
        self.send_gap = send_gap
        self.closed = False
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            self.sock.bind(("0.0.0.0", reply_port))
            self.hears_replies = True
        except OSError:
            # Something else holds the reply port, so the cameras' answers can't be seen: every command is assumed to land.
            self.sock.bind(("0.0.0.0", 0))
            self.hears_replies = False
        self.cameras = [
            {"name": camera["name"], "address": (camera.get("host", ""), int(camera.get("port", 52381))), "changed": threading.Condition(),
             # want_*: what the sticks ask for now. have_*: what the camera has confirmed it is doing.
             "want_pan_tilt": STILL, "want_zoom": STILL, "have_pan_tilt": STILL, "have_zoom": STILL,
             "active": False, "needs_reset": False, "sequence": 0, "awaiting": None, "answered": False,
             "sent_at": 0.0, "last_axis": "zoom", "seen": 0.0,
             # One-off commands waiting to be sent, each with the time after which it is no longer worth sending.
             "once": []}
            for camera in cameras
        ]
        for camera in self.cameras:
            threading.Thread(target=self._run, args=(camera,), daemon=True).start()
        threading.Thread(target=self._read_replies, daemon=True).start()
        threading.Thread(target=self._watch, daemon=True).start()

    def drive(self, index, pan, tilt, zoom, zoom_speed):
        """Set a camera's motion. pan/tilt are signed speeds (right and up positive, 0 = still); zoom is -1 out, 0, 1 in."""
        camera = self.cameras[index]
        if not camera["address"][0]:
            # No address entered for this camera yet.
            return
        with camera["changed"]:
            camera["seen"] = time.monotonic()
            camera["want_pan_tilt"] = (pan, tilt)
            camera["want_zoom"] = (zoom, zoom_speed) if zoom else STILL
            camera["changed"].notify_all()

    def adjust(self, index, payload):
        """Send a camera a one-off command (a picture adjustment), in turn with whatever the sticks are asking of it."""
        camera = self.cameras[index]
        if not camera["address"][0]:
            return
        with camera["changed"]:
            camera["once"].append((payload, time.monotonic() + ADJUSTMENT_SECONDS))
            camera["changed"].notify_all()

    def status(self):
        """Per camera: whether a move is in progress, and whether the camera has confirmed everything asked of it."""
        return [
            {"name": camera["name"], "moving": camera["active"],
             "confirmed": camera["want_pan_tilt"] == camera["have_pan_tilt"] and camera["want_zoom"] == camera["have_zoom"]}
            for camera in self.cameras
        ]

    def stop_all(self):
        for camera in self.cameras:
            with camera["changed"]:
                camera["want_pan_tilt"] = camera["want_zoom"] = STILL
                camera["changed"].notify_all()

    def close(self):
        """Stop every camera and wait (briefly) for each to confirm before shutting down."""
        self.stop_all()
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline and any(camera["active"] for camera in self.cameras):
            time.sleep(0.02)
        self.closed = True
        self.sock.close()

    def _run(self, camera):
        changed = camera["changed"]
        while not self.closed:
            with changed:
                step = self._next_step(camera)
                if step is None:
                    changed.wait(0.1)
                    continue
                wait = camera["sent_at"] + self.send_gap - time.monotonic()
            if wait > 0:
                # Decide again after the gap, so the command carries the stick position as it is then.
                time.sleep(wait)
                continue

            with changed:
                step = self._next_step(camera)
                if step is None:
                    continue
                axis, value = step
                if axis == "reset":
                    camera["sequence"] = 0
                    camera["needs_reset"] = False
                    kind, payload = VISCA_CONTROL, b"\x01"
                else:
                    camera["sequence"] += 1
                    kind = VISCA_COMMAND
                    payload = value if axis == "once" else self._pan_tilt(*value) if axis == "pan_tilt" else self._zoom(*value)
                camera["awaiting"] = camera["sequence"]
                camera["answered"] = not self.hears_replies
                camera["sent_at"] = time.monotonic()
                try:
                    self.sock.sendto(struct.pack(">HHI", kind, len(payload), camera["sequence"]) + payload, camera["address"])
                except OSError:
                    camera["answered"] = False
                changed.wait_for(lambda: camera["answered"] or camera["needs_reset"] or self.closed, ACK_TIMEOUT)
                answered, camera["awaiting"] = camera["answered"], None
                if not answered:
                    # An unanswered reset has to be tried again before anything else can land.
                    camera["needs_reset"] = camera["needs_reset"] or axis == "reset"
                    continue
                if axis == "reset":
                    # A reset while nothing is asked for is the one that ends a move.
                    camera["active"] = value
                elif axis == "once":
                    if camera["once"] and camera["once"][0][0] is value:
                        camera["once"].pop(0)
                elif axis == "pan_tilt":
                    camera["have_pan_tilt"] = value
                    camera["last_axis"] = axis
                else:
                    camera["have_zoom"] = value
                    camera["last_axis"] = axis

    @staticmethod
    def _next_step(camera):
        """The one command most worth sending now, as (axis, value), or None when the camera is doing what is asked."""
        want = {"pan_tilt": camera["want_pan_tilt"], "zoom": camera["want_zoom"]}
        have = {"pan_tilt": camera["have_pan_tilt"], "zoom": camera["have_zoom"]}
        moving = any(value != STILL for value in want.values())
        once = camera["once"]
        while once and once[0][1] < time.monotonic():
            once.pop(0)
        if not camera["active"]:
            return ("reset", True) if moving or once else None
        if camera["needs_reset"]:
            return ("reset", True)
        behind = [axis for axis in want if want[axis] != have[axis]]
        # Stops go first, then one-off commands; otherwise the two axes take turns so neither waits on the other.
        stops = [axis for axis in behind if want[axis] == STILL]
        if stops:
            return (stops[0], STILL)
        if once:
            return ("once", once[0][0])
        if not behind:
            return None if moving else ("reset", False)
        axis = next((axis for axis in behind if axis != camera["last_axis"]), behind[0])
        return (axis, want[axis])

    @staticmethod
    def _pan_tilt(pan, tilt):
        pan_speed = min(abs(pan), MAX_PAN_SPEED) or 1
        tilt_speed = min(abs(tilt), MAX_TILT_SPEED) or 1
        pan_direction = 0x02 if pan > 0 else 0x01 if pan < 0 else 0x03
        tilt_direction = 0x01 if tilt > 0 else 0x02 if tilt < 0 else 0x03
        return bytes([0x81, 0x01, 0x06, 0x01, pan_speed, tilt_speed, pan_direction, tilt_direction, 0xFF])

    @staticmethod
    def _zoom(zoom, zoom_speed):
        speed = min(max(zoom_speed, 0), MAX_ZOOM_SPEED)
        return bytes([0x81, 0x01, 0x04, 0x07, (0x20 | speed) if zoom > 0 else (0x30 | speed) if zoom < 0 else 0x00, 0xFF])

    def _read_replies(self):
        while not self.closed:
            try:
                data, source = self.sock.recvfrom(64)
            except OSError:
                continue
            if len(data) < 9:
                continue
            kind, _, sequence = struct.unpack(">HHI", data[:8])
            for camera in self.cameras:
                if camera["address"][0] != source[0]:
                    continue
                with camera["changed"]:
                    if kind == VISCA_CONTROL and data[8:10] == b"\x0f\x01":
                        # The camera refused a command for its sequence number (Companion got one in between).
                        camera["needs_reset"] = camera["active"]
                    elif sequence == camera["awaiting"]:
                        camera["answered"] = True
                    camera["changed"].notify_all()

    def _watch(self):
        while not self.closed:
            time.sleep(0.1)
            now = time.monotonic()
            for camera in self.cameras:
                with camera["changed"]:
                    asked = camera["want_pan_tilt"] != STILL or camera["want_zoom"] != STILL
                    if asked and now - camera["seen"] > DRIVE_TIMEOUT:
                        camera["want_pan_tilt"] = camera["want_zoom"] = STILL
                        camera["changed"].notify_all()


# Where cameras of the common makes serve video a browser can show by itself, tried in this order on each web port.
MJPEG_PATHS = [
    "/mjpeg", "/image", "/video.mjpg", "/mjpg/video.mjpg", "/axis-cgi/mjpg/video.cgi",
    "/cgi-bin/mjpeg?resolution=640x360&framerate=15", "/videostream.cgi", "/stream.mjpg",
]
SNAPSHOT_PATHS = [
    "/snapshot.jpg", "/action_snapshot", "/oneshotimage1", "/oneshotimage.jpg", "/cgi-bin/camera?resolution=640",
    "/axis-cgi/jpg/image.cgi", "/cgi-bin/snapshot.cgi", "/snapshot.cgi", "/image.jpg", "/jpg/image.jpg", "/snap.jpg",
]
RTSP_PATHS = ["/video1", "/1", "/stream1", "/live", "/media/video1", "/h264", "/axis-media/media.amp", "/MediaInput/h264/stream_1", "/"]
STREAM_HTTP_PORTS = (80, 8080)
STREAM_RTSP_PORTS = (554, 8554)
# A stream that sends nothing for this long is taken to have stopped.
STREAM_TIMEOUT = 6
SNAPSHOT_FPS = 8


def url_host(host):
    return f"[{host}]" if ":" in host else host


def without_sign_in(url):
    """The address with any user:password@ taken out, so it can be shown and requested."""
    parts = urlsplit(url)
    return parts._replace(netloc=url_host(parts.hostname or "") + (f":{parts.port}" if parts.port else "")).geturl()


def open_url(url, timeout=TIMEOUT):
    """Open an http(s) address, signing in with the user:password@ it carries if the camera asks."""
    parts = urlsplit(url)
    plain = without_sign_in(url)
    handlers = []
    if parts.username:
        passwords = HTTPPasswordMgrWithDefaultRealm()
        passwords.add_password(None, plain, unquote(parts.username), unquote(parts.password or ""))
        handlers = [HTTPBasicAuthHandler(passwords), HTTPDigestAuthHandler(passwords)]
    return build_opener(*handlers).open(plain, timeout=timeout)


def listening(host, port):
    try:
        socket.create_connection((host, port), timeout=TIMEOUT).close()
        return True
    except OSError:
        return False


def rtsp_status(url):
    """Ask an RTSP server to describe a stream. Returns its status code, or None if nothing there speaks RTSP."""
    parts = urlsplit(url)
    try:
        with socket.create_connection((parts.hostname, parts.port or 554), timeout=TIMEOUT) as link:
            link.sendall(f"DESCRIBE {without_sign_in(url)} RTSP/1.0\r\nCSeq: 1\r\nAccept: application/sdp\r\n\r\n".encode())
            reply = link.recv(64).decode("latin-1").split()
    except OSError:
        return None
    return int(reply[1]) if len(reply) > 1 and reply[0].startswith("RTSP/") and reply[1].isdigit() else None


class BrowserGone(Exception):
    pass


class StreamFinder:
    """Finds a video stream on each camera that the page can show, and passes it on to the browser.

    A browser can show Motion JPEG and still pictures by itself, so those are looked for first. It cannot
    show RTSP, which is what most cameras use for their main stream, so an RTSP stream is converted with
    ffmpeg when ffmpeg is installed and otherwise only reported.

    Cameras are only looked at when asked: once when the camera list is put to use, and after that when a
    page opens or its refresh button is pressed. Nothing is looked for on a timer.
    """

    def __init__(self, cameras, ffmpeg=None):
        self.cameras = cameras
        self.ffmpeg = ffmpeg
        self.found = [{"state": "looking", "id": 0} for _ in cameras]
        self.changes = 0
        self.closed = threading.Event()
        self.looking = set()
        self.looking_lock = threading.Lock()
        self.look_again()

    def status(self):
        """What the page needs to know about each camera's stream; the address is left out as it may hold a password."""
        return [{key: value for key, value in found.items() if key != "url"} for found in self.found]

    def look_again(self):
        with self.looking_lock:
            # One look at a time per camera: a second request while one is under way adds nothing.
            idle = [index for index in range(len(self.cameras)) if index not in self.looking]
            self.looking.update(idle)
        for index in idle:
            threading.Thread(target=self._look, args=(index,), daemon=True).start()

    def close(self):
        self.closed.set()

    def _look(self, index):
        try:
            self._set(index, self._find(self.cameras[index]))
        finally:
            with self.looking_lock:
                self.looking.discard(index)

    def _set(self, index, found):
        # A stream that was there and now is not has dropped; one that never was is simply none.
        if found["state"] == "none" and self.found[index]["state"] in {"found", "dropped"}:
            found = {**found, "state": "dropped"}
        if self.closed.is_set() or found == {key: value for key, value in self.found[index].items() if key != "id"}:
            return
        # A new id each time the answer changes tells the page to load the picture afresh.
        self.changes += 1
        self.found[index] = {**found, "id": self.changes}

    def _find(self, camera):
        if camera["streamUrl"]:
            found = self._check(camera["streamUrl"])
            if isinstance(found, dict):
                return found
            return {"state": "none", "note": "The stream address entered for this camera " + ("needs a user name and password" if found else "did not answer")}
        if not camera["host"]:
            return {"state": "none", "note": "No address entered for this camera"}

        host = url_host(camera["host"])
        candidates = [(f"http://{host}:{port}{path}", port) for port in STREAM_HTTP_PORTS for path in MJPEG_PATHS + SNAPSHOT_PATHS]
        candidates += [(f"rtsp://{host}:{port}{path}", port) for port in STREAM_RTSP_PORTS for path in RTSP_PATHS]
        ports = {}
        locked = False
        for url, port in candidates:
            if self.closed.is_set():
                break
            if port not in ports:
                ports[port] = listening(camera["host"], port)
            if not ports[port]:
                continue
            found = self._check(url)
            if isinstance(found, dict):
                return found
            locked = locked or found
        if locked:
            return {"state": "none", "note": "The camera asks for a user name and password. Enter its stream address in Settings as http://user:password@address/…"}
        return {"state": "none", "note": "No video stream found on this camera"}

    def _check(self, url):
        """What is at `url`: a description of the stream, True if it wants a sign-in, or False if there is no stream."""
        if urlsplit(url).scheme == "rtsp":
            status = rtsp_status(url)
            if status == 401 and not urlsplit(url).username:
                return True
            if status not in {200, 401}:
                return False
            found = {"state": "found", "kind": "rtsp", "url": url, "address": without_sign_in(url), "playable": bool(self.ffmpeg)}
            if not self.ffmpeg:
                found["note"] = "This camera streams RTSP, which a browser cannot show. Install ffmpeg on the computer running the controller to see it here."
            return found
        try:
            with open_url(url) as source:
                content_type = source.headers.get("Content-Type", "").lower()
                if content_type.startswith("multipart/x-mixed-replace"):
                    kind = "mjpeg"
                elif content_type.startswith(("image/jpeg", "image/jpg")) and source.read(2) == b"\xff\xd8":
                    kind = "snapshot"
                else:
                    return False
        except HTTPError as error:
            return error.code in {401, 403}
        except (OSError, HTTPException, ValueError):
            return False
        return {"state": "found", "kind": kind, "url": url, "address": without_sign_in(url), "playable": True}

    def send(self, index, handler):
        """Pass a camera's stream to the browser until either end hangs up. Raises LookupError if there is none to show."""
        found = self.found[index] if 0 <= index < len(self.found) else {}
        if not found.get("playable"):
            raise LookupError("No stream to show for this camera")
        begun = False

        def write(content_type, data):
            nonlocal begun
            try:
                if not begun:
                    begun = True
                    handler.send_response(200)
                    handler.send_header("Content-Type", content_type)
                    handler.send_header("Cache-Control", "no-store")
                    handler.end_headers()
                handler.wfile.write(data)
            except OSError:
                raise BrowserGone

        try:
            if found["kind"] == "mjpeg":
                with open_url(found["url"], timeout=STREAM_TIMEOUT) as source:
                    content_type = source.headers.get("Content-Type")
                    while not self.closed.is_set():
                        data = source.read1(65536)
                        if not data:
                            break
                        write(content_type, data)
            elif found["kind"] == "snapshot":
                while not self.closed.is_set():
                    started = time.monotonic()
                    with open_url(found["url"], timeout=STREAM_TIMEOUT) as source:
                        picture = source.read()
                    write("multipart/x-mixed-replace; boundary=frame",
                          b"--frame\r\nContent-Type: image/jpeg\r\nContent-Length: %d\r\n\r\n%b\r\n" % (len(picture), picture))
                    time.sleep(max(0, 1 / SNAPSHOT_FPS - (time.monotonic() - started)))
            else:
                command = [self.ffmpeg, "-loglevel", "error", "-rtsp_transport", "tcp", "-i", found["url"], "-an",
                           "-vf", "scale=960:-2", "-r", "15", "-q:v", "6", "-f", "mpjpeg", "-"]
                process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL)
                try:
                    while not self.closed.is_set():
                        data = process.stdout.read1(65536)
                        if not data:
                            break
                        write("multipart/x-mixed-replace; boundary=ffmpeg", data)
                finally:
                    process.kill()
                    process.wait()
        except BrowserGone:
            return
        except (OSError, HTTPException):
            pass
        # The camera stopped sending. It is not looked for again until someone asks.
        if self.found[index] is found:
            self._set(index, {"state": "none", "note": "The camera's stream stopped"})
        if not begun:
            raise LookupError("The camera's stream stopped")


SESSION_COOKIE = "ptz_session"
SESSION_SECONDS = 30 * 24 * 3600
# After this many wrong passwords from one address within the window, further attempts are refused.
MAX_FAILED_LOGINS = 8
FAILED_LOGIN_WINDOW = 600

LOGIN_PAGE = b"""<!doctype html>
<html lang="en"><head><meta charset="UTF-8" /><meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>PTZ / Field Controller</title>
<style>
:root { --paper: #0e1513; --ink: #e3ece5; --line: #2c3d36; --field: #131d1a; --button: #b9f04d; --on-button: #10201a; --bad: #ef8a70; }
:root[data-theme="light"] { --paper: #e8eee9; --ink: #1d2a23; --line: #bdcbc2; --field: #f7f8f4; --button: #1d2a23; --on-button: #f7f8f4; --bad: #a33a2a; }
body { margin: 0; min-height: 100vh; display: grid; place-items: center; background: var(--paper); color: var(--ink); font: 14px ui-monospace, Menlo, monospace; }
form { width: min(320px, calc(100vw - 48px)); display: grid; gap: 12px; }
h1 { margin: 0 0 6px; font-size: 13px; letter-spacing: 0.12em; }
input, button { height: 44px; padding: 0 12px; border: 1px solid var(--line); background: var(--field); color: inherit; font: inherit; }
button { background: var(--button); color: var(--on-button); letter-spacing: 0.1em; cursor: pointer; }
p { margin: 0; min-height: 18px; color: var(--bad); font-size: 12px; }
</style>
<script>try { document.documentElement.dataset.theme = localStorage.getItem("ptz-theme") || "dark"; } catch { document.documentElement.dataset.theme = "dark"; }</script>
</head><body>
<form id="login"><h1>FIELD / PTZ</h1>
<input id="password" type="password" placeholder="Password" autocomplete="current-password" autofocus />
<button type="submit">UNLOCK</button><p id="error" role="alert"></p></form>
<script>
document.getElementById("login").addEventListener("submit", async (event) => {
  event.preventDefault();
  const response = await fetch("/api/login", { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ password: document.getElementById("password").value }) });
  if (response.ok) { location.reload(); return; }
  const result = await response.json().catch(() => ({}));
  document.getElementById("error").textContent = result.error || "Could not sign in";
});
</script></body></html>"""


def load_config():
    """Read config.json, creating it with a new password the first time the server runs."""
    saved = {}
    if CONFIG_PATH.exists():
        saved = json.loads(CONFIG_PATH.read_text())
    config = {**DEFAULT_CONFIG, **saved}
    config["cameras"] = clean_cameras(config["cameras"])
    config["vdo"] = clean_vdo(config.get("vdo"))
    config["google"] = clean_google(None, config.get("google"))
    config.pop("mail", None)
    if not config.get("password"):
        config["password"] = "-".join(secrets.token_hex(2) for _ in range(3))
    if not config.get("secret"):
        config["secret"] = secrets.token_hex(32)
    if config != saved:
        save_config(config)
    return config


def save_config(config):
    # Written beside the real file and swapped in, so a crash part-way can't leave half a config.json.
    partial = CONFIG_PATH.with_name(CONFIG_PATH.name + ".part")
    partial.write_text(json.dumps(config, indent=2) + "\n")
    os.chmod(partial, 0o600)
    os.replace(partial, CONFIG_PATH)


class Handler(BaseHTTPRequestHandler):
    # Set from config.json when the server starts.
    # The whole of config.json, kept so that camera changes made on the settings page can be saved back to it.
    config = None
    camera_settings = []
    vdo = None
    google = clean_google(None)
    # The one Google sign-in under way, so that only the answer to our own request is accepted.
    google_state = None
    ffmpeg = None
    cameras = None
    streams = None
    cameras_lock = threading.Lock()
    password = ""
    secret = ""
    # A browser on this computer needs no password; every other computer does.
    trust_this_computer = True
    failed_logins = {}
    failed_logins_lock = threading.Lock()

    def _json(self, status, data, headers=()):
        body = json.dumps(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    @classmethod
    def set_cameras(cls, camera_settings, **link_options):
        """Switch to a new list of cameras: stop the ones being driven, then drive and look for streams on the new ones."""
        with cls.cameras_lock:
            if cls.cameras:
                cls.cameras.close()
            if cls.streams:
                cls.streams.close()
            cls.camera_settings = camera_settings
            cls.cameras = CameraLink(camera_settings, **link_options)
            cls.streams = StreamFinder(camera_settings, cls.ffmpeg)

    def _camera_list(self):
        streams = self.streams.status() if self.streams else []
        return [
            {**settings, **state, "stream": streams[index] if index < len(streams) else {"state": "none"}}
            for index, (settings, state) in enumerate(zip(self.camera_settings, self.cameras.status() if self.cameras else []))
        ]

    def _session_token(self):
        return hmac.new(self.secret.encode(), self.password.encode(), hashlib.sha256).hexdigest()

    def _session_cookie(self):
        return f"{SESSION_COOKIE}={self._session_token()}; Max-Age={SESSION_SECONDS}; Path=/; HttpOnly; SameSite=Strict"

    def _signed_in(self):
        if self.trust_this_computer and self.client_address[0] in {"127.0.0.1", "::1"}:
            return True
        if not self.password:
            return False
        for part in self.headers.get("Cookie", "").split(";"):
            name, _, value = part.strip().partition("=")
            if name == SESSION_COOKIE and hmac.compare_digest(value, self._session_token()):
                return True
        return False

    def _same_origin(self):
        """Refuse posts sent by other websites open in the same browser."""
        origin = self.headers.get("Origin")
        return origin is None or urlsplit(origin).netloc == self.headers.get("Host")

    def _login(self, data):
        address = self.client_address[0]
        now = time.monotonic()
        with self.failed_logins_lock:
            recent = [at for at in self.failed_logins.get(address, []) if now - at < FAILED_LOGIN_WINDOW]
            self.failed_logins[address] = recent
            locked = len(recent) >= MAX_FAILED_LOGINS
        if locked:
            self._json(429, {"error": "Too many wrong passwords. Try again in 10 minutes."})
            return

        supplied = data.get("password")
        if not self.password or not isinstance(supplied, str) or not hmac.compare_digest(supplied.encode(), self.password.encode()):
            with self.failed_logins_lock:
                self.failed_logins[address].append(now)
            time.sleep(1)
            self._json(401, {"error": "Wrong password"})
            return

        self._json(200, {"ok": True}, headers=[("Set-Cookie", self._session_cookie())])

    def do_GET(self):
        if self.path == "/api/health":
            self._json(200, {"ok": True})
            return

        requested = urlsplit(self.path).path
        if requested == "/":
            requested = "/index.html"
        if not self._signed_in():
            if requested == "/index.html":
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(LOGIN_PAGE)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(LOGIN_PAGE)
            else:
                self._json(401, {"error": "Sign in first"})
            return

        if requested == "/api/session":
            self._json(200, {"ok": True, "cameras": self._camera_list(), **self._room()})
            return
        if requested == "/api/google/start":
            self._google_start()
            return
        if requested == "/api/google/callback":
            self._google_finish(urlsplit(self.path).query)
            return
        stream = re.fullmatch(r"/api/stream/(\d+)", requested)
        if stream:
            try:
                if self.streams is None:
                    raise LookupError("No cameras are set up")
                self.streams.send(int(stream.group(1)), self)
            except LookupError as error:
                self._json(404, {"error": str(error)})
            return
        if requested not in {"/index.html", "/app.css", "/app.js", "/qrcode.js"}:
            self._json(404, {"error": "Not found"})
            return

        path = ROOT / requested.lstrip("/")
        try:
            content = path.read_bytes()
        except OSError:
            self._json(404, {"error": "App asset not found"})
            return

        content_type = mimetypes.guess_type(path.name)[0] or "application/octet-stream"
        self.send_response(200)
        self.send_header("Content-Type", f"{content_type}; charset=utf-8")
        self.send_header("Content-Length", str(len(content)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(content)

    def do_POST(self):
        routes = {"/api/command": self._command, "/api/drive": self._drive, "/api/cameras": self._save_cameras,
                  "/api/streams": self._look_for_streams, "/api/vdo": self._save_vdo, "/api/password": self._save_password, "/api/google": self._save_google,
                  "/api/google/signout": self._google_sign_out, "/api/invite": self._invite}
        route = routes.get(self.path)
        if route is None and self.path != "/api/login":
            self._json(404, {"error": "Not found"})
            return
        if not self._same_origin():
            self._json(403, {"error": "Request came from another site"})
            return
        if route is not None and not self._signed_in():
            self._json(401, {"error": "Sign in first"})
            return

        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length < 1 or length > 65536:
                raise ValueError("Request body size is invalid")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("Request body must be a JSON object")
            if route is None:
                self._login(data)
                return
            result = route(data)
            self._json(200, result, headers=result.pop("_headers", ()))
        except (ValueError, TypeError, json.JSONDecodeError) as error:
            self._json(400, {"error": str(error)})
        except Exception as error:
            self._json(502, {"error": str(error)})

    def _command(self, data):
        """A picture adjustment for one camera, sent straight to it as a VISCA command."""
        self._check_version(data)
        payload = ADJUSTMENTS.get(data.get("action"))
        if payload is None:
            raise ValueError("Unknown controller action")
        self.cameras.adjust(self._whole(data, "camera", 0, len(self.cameras.cameras) - 1), bytes.fromhex(payload))
        return {"ok": True}

    def _drive(self, data):
        """Stick position for one camera, sent straight to the camera."""
        self._check_version(data)
        index = self._whole(data, "camera", 0, len(self.cameras.cameras) - 1)
        pan = self._whole(data, "pan", -MAX_PAN_SPEED, MAX_PAN_SPEED)
        tilt = self._whole(data, "tilt", -MAX_TILT_SPEED, MAX_TILT_SPEED)
        zoom = self._whole(data, "zoom", -1, 1)
        zoom_speed = self._whole(data, "zoomSpeed", 0, MAX_ZOOM_SPEED)
        self.cameras.drive(index, pan, tilt, zoom, zoom_speed)
        return {"ok": True}

    def _save_cameras(self, data):
        """The camera list from the settings page: saved to config.json and put to use straight away."""
        self._check_version(data)
        camera_settings = clean_cameras(data.get("cameras"))
        if type(self).config is not None:
            type(self).config = {**self.config, "cameras": camera_settings}
            save_config(self.config)
        self.set_cameras(camera_settings)
        return {"ok": True, "cameras": self._camera_list()}

    def _room(self):
        """What the settings page shows besides cameras: the remote password, the VDO.Ninja room and its links, and
        whether invites can be emailed. Google's secret and sign-in never leave the bridge."""
        listening = self.server.server_address[0]
        return {"remote": {"password": self.password, "port": self.server.server_port, "on": listening not in {"127.0.0.1", "::1", "localhost"},
                           "addresses": own_addresses()},
                "vdo": self.vdo, "links": vdo_links(self.vdo) if self.vdo else {},
                "mail": {"address": self.google["email"], "ready": bool(self.google["refreshToken"]), "clientId": self.google["clientId"],
                         "clientReady": bool(self.google["clientId"] and self.google["clientSecret"])}}

    def _keep(self, name, value):
        setattr(type(self), name, value)
        if self.config is not None:
            type(self).config = {**self.config, name: value}
            save_config(self.config)

    def _save_password(self, data):
        """Change the password other computers sign in with. Everyone signed in with the old one is signed out,
        except the browser making the change, which is handed a pass for the new one."""
        self._check_version(data)
        self._keep("password", clean_password(data.get("password")))
        return {"ok": True, **self._room(), "_headers": [("Set-Cookie", self._session_cookie())]}

    def _save_vdo(self, data):
        self._check_version(data)
        self._keep("vdo", clean_vdo(data.get("vdo")))
        return {"ok": True, **self._room()}

    def _save_google(self, data):
        self._check_version(data)
        self._keep("google", clean_google(data.get("google"), self.google))
        return {"ok": True, **self._room()}

    def _google_sign_out(self, data):
        self._check_version(data)
        if self.google["refreshToken"]:
            try:
                google_call(GOOGLE_REVOKE, form={"token": self.google["refreshToken"]})
            except RuntimeError:
                pass  # Already withdrawn at Google's end, or Google is unreachable: forget it here regardless.
        self._keep("google", {**self.google, "refreshToken": "", "email": ""})
        return {"ok": True, **self._room()}

    def _google_redirect(self):
        """Google sends the browser back here after sign-in. It only accepts this computer's own address for that."""
        return f"http://127.0.0.1:{self.server.server_port}/api/google/callback"

    def _go(self, address):
        self.send_response(302)
        self.send_header("Location", address)
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _google_start(self):
        if not (self.google["clientId"] and self.google["clientSecret"]):
            self._go("/?google=" + urlencode({"": "Enter the Google client ID and secret in Settings first"})[1:])
            return
        type(self).google_state = secrets.token_urlsafe(24)
        # access_type=offline and prompt=consent make Google hand over a refresh token, so the sign-in lasts.
        self._go(GOOGLE_SIGN_IN + "?" + urlencode({
            "client_id": self.google["clientId"], "redirect_uri": self._google_redirect(), "response_type": "code",
            "scope": GOOGLE_SCOPES, "access_type": "offline", "prompt": "consent", "state": self.google_state}))

    def _google_finish(self, query):
        answer = {name: values[0] for name, values in parse_qs(query).items()}
        state, type(self).google_state = self.google_state, None
        try:
            if not state or not hmac.compare_digest(answer.get("state", ""), state):
                raise RuntimeError("That sign-in was not started from this controller. Try again from Settings.")
            if "code" not in answer:
                raise RuntimeError(f"Google sign-in was not completed ({answer.get('error', 'no answer')})")
            tokens = google_call(GOOGLE_TOKEN, form={"client_id": self.google["clientId"], "client_secret": self.google["clientSecret"],
                                                     "code": answer["code"], "grant_type": "authorization_code", "redirect_uri": self._google_redirect()})
            if not tokens.get("refresh_token"):
                raise RuntimeError("Google did not grant a lasting sign-in. Try again.")
            email = google_call(GOOGLE_ACCOUNT, token=tokens["access_token"]).get("email", "")
            self._keep("google", {**self.google, "refreshToken": tokens["refresh_token"], "email": email})
            self._go("/?google=" + urlencode({"": f"Signed in to Google as {email}"})[1:])
        except RuntimeError as error:
            self._go("/?google=" + urlencode({"": str(error)})[1:])

    def _invite(self, data):
        """Email invites to people on the crew list. Addresses not on the list are refused, so this can't mail strangers."""
        self._check_version(data)
        wanted = data.get("emails")
        if not isinstance(wanted, list) or not wanted:
            raise ValueError("Nobody to invite")
        people = [person for person in self.vdo["crew"] if person["email"] in wanted]
        if len(people) != len(set(wanted)):
            raise ValueError("Invites only go to people on the crew list")
        try:
            send_invites(self.google, self.vdo, people)
        except GoogleRefused as error:
            if error.code == "invalid_grant":
                # Google has withdrawn the sign-in (password change, access removed, or a test app's 7-day limit).
                self._keep("google", {**self.google, "refreshToken": ""})
                raise RuntimeError("The Google sign-in has expired. Sign in again in Settings.")
            raise
        return {"ok": True, "sent": len(people)}

    def _look_for_streams(self, data):
        self._check_version(data)
        if self.streams:
            self.streams.look_again()
        return {"ok": True}

    @staticmethod
    def _whole(data, name, lowest, highest):
        value = data.get(name)
        if isinstance(value, bool) or not isinstance(value, int) or not lowest <= value <= highest:
            raise ValueError(f"{name} must be a whole number from {lowest} to {highest}")
        return value

    @staticmethod
    def _check_version(data):
        if data.get("v") != CLIENT_VERSION:
            raise ValueError("This tab is running an old version of the controller. Reload the page.")

    def log_message(self, _format, *_args):
        return


def start(config=None):
    """Start the bridge with the settings in config.json and return its server, ready for serve_forever()."""
    config = config or load_config()
    Handler.config = config
    Handler.vdo = config["vdo"]
    Handler.google = config["google"]
    Handler.ffmpeg = find_ffmpeg(config.get("ffmpeg", ""))
    Handler.password = config["password"]
    Handler.secret = config["secret"]
    server = ThreadingHTTPServer((config["host"], config["port"]), Handler)
    Handler.set_cameras(config["cameras"])
    return server


def stop(server):
    """Stop every camera, then shut the bridge down."""
    Handler.cameras.close()
    Handler.streams.close()
    server.server_close()


if __name__ == "__main__":
    server = start()
    print(f"PTZ Game Controller running at http://127.0.0.1:{server.server_port} (ffmpeg: {Handler.ffmpeg or 'not found'})", flush=True)

    def interrupt(*_):
        raise KeyboardInterrupt

    # launchd stops the server with SIGTERM; treat it like Ctrl-C so no camera is left moving.
    signal.signal(signal.SIGTERM, interrupt)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nShutting down", flush=True)
    finally:
        stop(server)
