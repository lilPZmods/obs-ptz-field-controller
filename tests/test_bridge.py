import functools
import json
import socket
import struct
import tempfile
import threading
import time
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import Request, urlopen

import server
from server import Handler, clean_cameras

RESET = (0x0200, 0, "01")
STOP_PAN = "8101060101010303ff"
STOP_ZOOM = "8101040700ff"


class FakeCamera:
    """A UDP socket standing in for a camera: records every VISCA packet and answers like the real thing."""

    def __init__(self):
        self.sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.settimeout(0.02)
        self.port = self.sock.getsockname()[1]
        self.received = []
        # Payloads (hex) to swallow without answering, as a real camera does with commands sent too close together.
        self.ignore = []
        # Commands to refuse once with a sequence error, as happens when Companion gets a command in between.
        self.refuse = 0
        self.closed = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.closed:
            try:
                data, sender = self.sock.recvfrom(64)
            except (socket.timeout, OSError):
                continue
            kind, _, sequence = struct.unpack(">HHI", data[:8])
            payload = data[8:].hex()
            self.received.append((kind, sequence, payload))
            if payload in self.ignore:
                self.ignore.remove(payload)
            elif kind == 0x0200:
                self.sock.sendto(struct.pack(">HHI", 0x0201, 1, 0) + b"\x01", sender)
            elif self.refuse:
                self.refuse -= 1
                self.sock.sendto(struct.pack(">HHI", 0x0200, 2, sequence) + b"\x0f\x01", sender)
            else:
                self.sock.sendto(struct.pack(">HHI", 0x0111, 3, sequence) + b"\x90\x41\xff", sender)

    def packets(self, wait=0.3):
        """Everything received so far, after giving the bridge `wait` seconds to finish sending."""
        time.sleep(wait)
        received, self.received = self.received, []
        return received

    def close(self):
        self.closed = True
        self.sock.close()


JPEG = b"\xff\xd8 a picture \xff\xd9"


class FakeCameraWeb(BaseHTTPRequestHandler):
    """The web side of a camera: serves whichever of `pages` it has been given, and nothing else."""

    pages = {}

    def do_GET(self):
        page = self.pages.get(self.path)
        if page is None:
            self.send_response(404)
            self.end_headers()
            return
        content_type, body = page
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, _format, *_args):
        return


class BridgeTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.bridge = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        threading.Thread(target=cls.bridge.serve_forever, daemon=True).start()
        cls.base_url = f"http://127.0.0.1:{cls.bridge.server_port}"
        Handler.password = "correct-horse"
        Handler.secret = "test-secret"

    @classmethod
    def tearDownClass(cls):
        cls.bridge.shutdown()
        cls.bridge.server_close()

    def setUp(self):
        Handler.trust_this_computer = True
        Handler.failed_logins.clear()
        self.camera = FakeCamera()
        Handler.config = None
        self.use_cameras([{"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port}])

    def tearDown(self):
        Handler.cameras.close()
        Handler.streams.close()
        self.camera.close()

    def use_cameras(self, cameras):
        Handler.set_cameras(clean_cameras(cameras), reply_port=0, send_gap=0.01)

    def session(self):
        with urlopen(f"{self.base_url}/api/session", timeout=2) as response:
            return json.loads(response.read())

    def stream_of(self, pages, stream_url="", wait=3):
        """Give a camera a web server holding `pages`, and return what the bridge makes of it once it has looked."""
        FakeCameraWeb.pages = pages
        web = ThreadingHTTPServer(("127.0.0.1", 0), FakeCameraWeb)
        threading.Thread(target=web.serve_forever, daemon=True).start()
        self.addCleanup(web.server_close)
        self.addCleanup(web.shutdown)
        with mock.patch.object(server, "STREAM_HTTP_PORTS", (web.server_port,)), mock.patch.object(server, "STREAM_RTSP_PORTS", ()):
            self.use_cameras([{"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port, "streamUrl": stream_url.format(port=web.server_port)}])
            deadline = time.monotonic() + wait
            while time.monotonic() < deadline:
                stream = self.session()["cameras"][0]["stream"]
                if stream["state"] != "looking":
                    return stream
                time.sleep(0.05)
        self.fail("The bridge never finished looking for a stream")

    def send(self, data, path="/api/command", headers=None, want_response=False):
        request = Request(
            f"{self.base_url}{path}",
            data=json.dumps(data).encode(),
            headers={"Content-Type": "application/json", **(headers or {})},
            method="POST",
        )
        try:
            with urlopen(request, timeout=4) as response:
                return response if want_response else response.status
        except Exception as error:
            return error if want_response else error.code

    def command(self, action, camera=0):
        return {"v": 8, "action": action, "camera": camera}

    def drive(self, pan=0, tilt=0, zoom=0, zoom_speed=0, camera=0):
        return self.send({"v": 8, "camera": camera, "pan": pan, "tilt": tilt, "zoom": zoom, "zoomSpeed": zoom_speed}, "/api/drive")

    # Picture adjustments (brightness and colour)

    def test_adjustment_goes_to_the_camera_between_two_resets(self):
        self.assertEqual(self.send(self.command("blue_up")), 200)
        self.assertEqual(self.camera.packets(), [RESET, (0x0100, 1, "8101040402ff"), RESET])

    def test_each_adjustment_sends_its_own_command(self):
        wanted = {"brightness_up": "8101040d02ff", "brightness_down": "8101040d03ff", "red_up": "8101040302ff", "red_down": "8101040303ff",
                  "blue_down": "8101040403ff", "manual_white_balance": "8101043505ff", "bright_mode": "810104390dff"}
        for action, payload in wanted.items():
            self.assertEqual(self.send(self.command(action)), 200)
            self.assertIn((0x0100, 1, payload), self.camera.packets())

    def test_adjustment_during_a_move_leaves_the_move_running(self):
        self.drive(pan=6)
        self.camera.packets()
        self.assertEqual(self.send(self.command("red_down")), 200)
        self.assertEqual(self.camera.packets(), [(0x0100, 2, "8101040303ff")])
        self.drive()
        self.assertEqual(self.camera.packets(), [(0x0100, 3, STOP_PAN), RESET])

    def test_adjustment_the_camera_misses_is_sent_again(self):
        self.camera.ignore = ["8101040d02ff"]
        self.send(self.command("brightness_up"))
        self.assertEqual(self.camera.packets(wait=0.8), [RESET, (0x0100, 1, "8101040d02ff"), (0x0100, 2, "8101040d02ff"), RESET])

    def test_rejects_unknown_action_and_camera(self):
        for action in ("raw_visca", "pan_left"):
            self.assertEqual(self.send(self.command(action)), 400)
        self.assertEqual(self.send(self.command("red_up", camera=1)), 400)
        self.assertEqual(self.camera.packets(), [])

    def test_rejects_commands_from_old_page_code(self):
        data = self.command("brightness_up")
        data["v"] = 7
        self.assertEqual(self.send(data), 400)
        self.assertEqual(self.camera.packets(), [])

    # Direct camera moves

    def test_drive_resets_sequence_then_sends_the_move(self):
        self.assertEqual(self.drive(pan=10, tilt=-4), 200)
        self.assertEqual(self.camera.packets(), [RESET, (0x0100, 1, "810106010a040202ff")])

    def test_drive_changes_speed_without_stopping(self):
        self.drive(pan=-3)
        self.camera.packets()
        self.drive(pan=-24, tilt=23)
        self.assertEqual(self.camera.packets(), [(0x0100, 2, "8101060118170101ff")])

    def test_drive_zooms_at_the_given_speed(self):
        self.drive(zoom=1, zoom_speed=5)
        self.camera.packets()
        self.drive(zoom=-1, zoom_speed=0)
        self.assertEqual(self.camera.packets(), [(0x0100, 2, "8101040730ff")])

    def test_only_the_newest_stick_position_is_sent(self):
        for pan in range(1, 21):
            self.drive(pan=pan)
        sent = self.camera.packets()
        self.assertEqual(sent[-1][2], "8101060114010203ff")
        self.assertLess(len(sent), 21)

    def test_releasing_stops_both_axes_then_leaves_sequence_at_zero(self):
        self.drive(pan=5, zoom=1, zoom_speed=2)
        self.camera.packets()
        self.drive()
        self.assertEqual(self.camera.packets(), [(0x0100, 3, STOP_PAN), (0x0100, 4, STOP_ZOOM), RESET])

    def test_a_stop_the_camera_misses_is_sent_again(self):
        self.drive(pan=5, zoom=1, zoom_speed=2)
        self.camera.packets()
        self.camera.ignore = [STOP_ZOOM, STOP_ZOOM]
        self.drive()
        self.assertEqual(self.camera.packets(wait=0.8), [
            (0x0100, 3, STOP_PAN), (0x0100, 4, STOP_ZOOM), (0x0100, 5, STOP_ZOOM), (0x0100, 6, STOP_ZOOM), RESET,
        ])

    def test_staying_still_sends_nothing(self):
        self.drive()
        self.assertEqual(self.camera.packets(), [])

    def test_camera_stops_by_itself_when_the_page_goes_quiet(self):
        self.drive(pan=8)
        self.assertEqual(self.camera.packets(wait=1.2), [RESET, (0x0100, 1, "8101060108010203ff"), (0x0100, 2, STOP_PAN), RESET])

    def test_sequence_error_from_camera_resets_and_resends(self):
        self.drive(pan=8)
        self.camera.packets()
        self.camera.refuse = 1
        self.drive(pan=9)
        self.assertEqual(self.camera.packets(), [(0x0100, 2, "8101060109010203ff"), RESET, (0x0100, 1, "8101060109010203ff")])

    def test_closing_stops_a_moving_camera(self):
        self.drive(zoom=-1, zoom_speed=7)
        self.camera.packets()
        Handler.cameras.close()
        self.assertEqual(self.camera.packets(), [(0x0100, 2, STOP_ZOOM), RESET])

    def test_drive_rejects_bad_values(self):
        self.assertEqual(self.drive(pan=25), 400)
        self.assertEqual(self.drive(tilt=24), 400)
        self.assertEqual(self.drive(zoom=2), 400)
        self.assertEqual(self.drive(camera=1), 400)
        self.assertEqual(self.camera.packets(), [])

    def test_a_camera_with_no_address_is_left_alone(self):
        self.use_cameras([{"name": "PTZ1"}])
        self.assertEqual(self.drive(pan=5), 200)
        self.assertEqual(self.camera.packets(), [])

    # Cameras that take plain VISCA (PTZOptics and most other makes)

    def plain_udp_camera(self, answer=True):
        """A UDP socket standing in for a plain-VISCA camera: records each bare command and acknowledges it."""
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        sock.bind(("127.0.0.1", 0))
        sock.settimeout(0.02)
        received, state = [], {"closed": False}

        def serve():
            while not state["closed"]:
                try:
                    data, sender = sock.recvfrom(64)
                except OSError:
                    continue
                received.append(data.hex())
                if answer:
                    sock.sendto(b"\x90\x41\xff", sender)

        threading.Thread(target=serve, daemon=True).start()
        self.addCleanup(sock.close)
        self.addCleanup(state.update, closed=True)
        return sock.getsockname()[1], received

    def test_plain_visca_over_udp_sends_bare_commands_with_no_resets(self):
        port, received = self.plain_udp_camera()
        self.use_cameras([{"name": "PTZ1", "type": "visca-udp", "host": "127.0.0.1", "port": port}])
        self.assertEqual(self.drive(pan=10, tilt=-4), 200)
        time.sleep(0.3)
        self.assertEqual(received, ["810106010a040202ff"])
        self.assertTrue(self.session()["cameras"][0]["moving"])
        self.drive()
        self.send(self.command("red_up"))
        time.sleep(0.3)
        self.assertEqual(received[1:], [STOP_PAN, "8101040302ff"])
        self.assertFalse(self.session()["cameras"][0]["moving"])

    def test_plain_visca_camera_that_never_answers_is_not_sent_the_same_thing_forever(self):
        port, received = self.plain_udp_camera(answer=False)
        self.use_cameras([{"name": "PTZ1", "type": "visca-udp", "host": "127.0.0.1", "port": port}])
        self.drive(zoom=1, zoom_speed=3)
        # Short of the point where the bridge stops a camera whose page has gone quiet.
        time.sleep(0.6)
        self.assertEqual(received, ["8101040723ff"] * 3)

    def test_plain_visca_over_tcp(self):
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        self.addCleanup(listener.close)
        received = []

        def serve():
            try:
                link, _ = listener.accept()
            except OSError:
                return
            self.addCleanup(link.close)
            while True:
                try:
                    data = link.recv(64)
                except OSError:
                    return
                if not data:
                    return
                received.append(data.hex())
                link.sendall(b"\x90\x41\xff")

        threading.Thread(target=serve, daemon=True).start()
        self.use_cameras([{"name": "PTZ1", "type": "visca-tcp", "host": "127.0.0.1", "port": listener.getsockname()[1]}])
        self.drive(pan=-3)
        time.sleep(0.3)
        self.drive()
        time.sleep(0.3)
        self.assertEqual(received, ["8101060103010103ff", STOP_PAN])

    def test_plain_visca_over_tcp_to_a_camera_that_is_off_does_not_hold_things_up(self):
        closed = socket.socket()
        closed.bind(("127.0.0.1", 0))
        port = closed.getsockname()[1]
        closed.close()
        self.use_cameras([{"name": "PTZ1", "type": "visca-tcp", "host": "127.0.0.1", "port": port}])
        started = time.monotonic()
        for pan in (4, 5, 6):
            self.assertEqual(self.drive(pan=pan), 200)
        self.assertLess(time.monotonic() - started, 1.0)

    def test_each_kind_of_camera_gets_its_usual_port(self):
        cameras = clean_cameras([{"name": "A"}, {"name": "B", "type": "visca-udp"}, {"name": "C", "type": "visca-tcp"}, {"name": "D", "type": "visca-tcp", "port": 1234}])
        self.assertEqual([(camera["type"], camera["port"]) for camera in cameras], [("sony", 52381), ("visca-udp", 1259), ("visca-tcp", 5678), ("visca-tcp", 1234)])
        with self.assertRaises(ValueError):
            clean_cameras([{"name": "A", "type": "pelco"}])

    # Cameras entered on the settings page

    def test_saved_cameras_are_written_to_config_and_driven(self):
        second = FakeCamera()
        self.addCleanup(second.close)
        cameras = [
            {"name": "Stage", "type": "sony", "host": "127.0.0.1", "port": self.camera.port, "streamUrl": "", "button": 10, "key": "1"},
            {"name": "Balcony", "type": "sony", "host": "127.0.0.1", "port": second.port, "streamUrl": "", "button": 4, "key": "q"},
        ]
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(server, "CONFIG_PATH", Path(folder) / "config.json"), \
                mock.patch.object(server, "CameraLink", functools.partial(server.CameraLink, reply_port=0, send_gap=0.01)):
            Handler.config = {"port": 8765, "cameras": []}
            self.assertEqual(self.send({"v": 8, "cameras": cameras}, "/api/cameras"), 200)
            self.assertEqual(json.loads(server.CONFIG_PATH.read_text()), {"port": 8765, "cameras": cameras})
        self.assertEqual([(camera["name"], camera["button"], camera["key"]) for camera in self.session()["cameras"]], [("Stage", 10, "1"), ("Balcony", 4, "q")])
        self.assertEqual(self.drive(pan=3, camera=1), 200)
        self.assertEqual(second.packets(), [RESET, (0x0100, 1, "8101060103010203ff")])
        self.assertEqual(self.camera.packets(wait=0), [])

    def test_new_cameras_get_no_button_and_the_first_three_keep_theirs(self):
        cameras = clean_cameras([{"name": f"PTZ{number}"} for number in range(1, 12)])
        self.assertEqual([camera["button"] for camera in cameras], [10, 11, 9] + [None] * 8)
        self.assertEqual([camera["key"] for camera in cameras], list("123456789") + ["", ""])

    def test_bad_camera_settings_are_refused(self):
        good = {"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port}
        for change in ({"host": "not a host!"}, {"port": 70000}, {"streamUrl": "file:///etc/passwd"}, {"button": "start"}, {"key": "ab"}):
            self.assertEqual(self.send({"v": 8, "cameras": [{**good, **change}]}, "/api/cameras"), 400)
        self.assertEqual(self.send({"v": 8, "cameras": []}, "/api/cameras"), 400)
        self.assertEqual(self.send({"v": 7, "cameras": [good]}, "/api/cameras"), 400)
        self.assertEqual(self.drive(pan=4), 200)
        self.assertEqual(self.camera.packets(), [RESET, (0x0100, 1, "8101060104010203ff")])

    # Video streams

    def test_finds_a_motion_jpeg_stream_and_passes_it_on(self):
        part = b"--f\r\nContent-Type: image/jpeg\r\n\r\n" + JPEG + b"\r\n"
        stream = self.stream_of({"/video.mjpg": ("multipart/x-mixed-replace; boundary=f", part * 3)})
        self.assertEqual((stream["state"], stream["kind"], stream["playable"]), ("found", "mjpeg", True))
        self.assertTrue(stream["address"].endswith("/video.mjpg"))
        with urlopen(f"{self.base_url}/api/stream/0", timeout=2) as response:
            self.assertEqual(response.headers["Content-Type"], "multipart/x-mixed-replace; boundary=f")
            self.assertEqual(response.read(), part * 3)
        # The fake camera hung up after three pictures: a stream that was there and went is "dropped".
        time.sleep(0.2)
        self.assertEqual(self.session()["cameras"][0]["stream"]["state"], "dropped")

    def test_a_camera_that_never_had_a_stream_is_not_called_dropped(self):
        self.assertEqual(self.stream_of({})["state"], "none")
        self.send({"v": 8}, "/api/streams")
        time.sleep(0.4)
        self.assertEqual(self.session()["cameras"][0]["stream"]["state"], "none")

    def test_turns_still_pictures_into_a_stream(self):
        stream = self.stream_of({"/snapshot.jpg": ("image/jpeg", JPEG)})
        self.assertEqual((stream["state"], stream["kind"]), ("found", "snapshot"))
        with urlopen(f"{self.base_url}/api/stream/0", timeout=2) as response:
            self.assertTrue(response.headers["Content-Type"].startswith("multipart/x-mixed-replace"))
            first = response.read(200)
        self.assertIn(b"--frame\r\nContent-Type: image/jpeg", first)
        self.assertIn(JPEG, first)

    def test_a_web_page_is_not_mistaken_for_a_stream(self):
        stream = self.stream_of({path: ("text/html", b"<html>camera menu</html>") for path in server.MJPEG_PATHS + server.SNAPSHOT_PATHS})
        self.assertEqual(stream["state"], "none")
        with self.assertRaises(Exception) as refused:
            urlopen(f"{self.base_url}/api/stream/0", timeout=2)
        self.assertEqual(refused.exception.code, 404)

    def test_uses_the_stream_address_entered_in_settings(self):
        stream = self.stream_of({"/odd/place": ("image/jpeg", JPEG)}, stream_url="http://user:secret@127.0.0.1:{port}/odd/place")
        self.assertEqual((stream["state"], stream["kind"]), ("found", "snapshot"))
        self.assertNotIn("secret", json.dumps(stream))

    def test_reports_an_rtsp_stream_it_cannot_show(self):
        rtsp = socket.socket()
        rtsp.bind(("127.0.0.1", 0))
        rtsp.listen()
        self.addCleanup(rtsp.close)

        def answer():
            while True:
                try:
                    link, _ = rtsp.accept()
                except OSError:
                    return
                link.settimeout(1)
                try:
                    if link.recv(512).startswith(b"DESCRIBE"):
                        link.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
                except OSError:
                    pass
                link.close()

        threading.Thread(target=answer, daemon=True).start()
        with mock.patch.object(Handler, "ffmpeg", None), mock.patch.object(server, "STREAM_HTTP_PORTS", ()), \
                mock.patch.object(server, "STREAM_RTSP_PORTS", (rtsp.getsockname()[1],)):
            self.use_cameras([{"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port}])
            deadline = time.monotonic() + 3
            while (stream := self.session()["cameras"][0]["stream"])["state"] == "looking" and time.monotonic() < deadline:
                time.sleep(0.05)
        self.assertEqual((stream["state"], stream["kind"], stream["playable"]), ("found", "rtsp", False))
        self.assertIn("ffmpeg", stream["note"])

    # The video relay (WebRTC without re-encoding)

    def rtsp_camera(self):
        """A socket that answers RTSP DESCRIBE the way a camera with a stream does."""
        rtsp = socket.socket()
        rtsp.bind(("127.0.0.1", 0))
        rtsp.listen()
        self.addCleanup(rtsp.close)

        def answer():
            while True:
                try:
                    link, _ = rtsp.accept()
                except OSError:
                    return
                link.settimeout(1)
                try:
                    if link.recv(512).startswith(b"DESCRIBE"):
                        link.sendall(b"RTSP/1.0 200 OK\r\nCSeq: 1\r\n\r\n")
                except OSError:
                    pass
                link.close()

        threading.Thread(target=answer, daemon=True).start()
        return rtsp.getsockname()[1]

    def test_relay_settings_get_a_view_key_and_refuse_nonsense(self):
        first, second = server.clean_relay(None), server.clean_relay({})
        self.assertEqual((first["webrtcPort"], first["udpPort"], first["hosts"]), (8889, 8189, []))
        self.assertNotEqual(first["viewKey"], second["viewKey"])
        self.assertEqual(server.clean_relay(first), first)
        for bad in ({"webrtcPort": 0}, {"hosts": ["not a host!"]}, {"viewKey": "has spaces"}):
            with self.assertRaises(ValueError):
                server.clean_relay(bad)

    def test_relay_only_takes_video_from_this_computer_and_viewers_need_the_key(self):
        with tempfile.TemporaryDirectory() as folder:
            relay = server.MediaRelay(server.clean_relay({"viewKey": "abcd1234", "hosts": ["studio.example.com"]}), program="/usr/bin/true", folder=folder)
            relay.show_cameras({1: "rtsp://192.0.2.5:554/video1"})
            written = json.loads((Path(folder) / "relay.yml").read_text())
        everyone, viewer = written["authInternalUsers"]
        self.assertEqual((everyone["ips"], [entry["action"] for entry in everyone["permissions"]]), (["127.0.0.1", "::1"], ["publish", "read"]))
        self.assertEqual((viewer["user"], viewer["pass"], [entry["action"] for entry in viewer["permissions"]]), ("viewer", "abcd1234", ["read"]))
        self.assertEqual(written["paths"], {"program": {}, "camera2": {"source": "rtsp://192.0.2.5:554/video1", "sourceOnDemand": True, "rtspTransport": "tcp"}})
        self.assertEqual((written["webrtcAdditionalHosts"], written["rtspAddress"], written["rtmpAddress"]), (["studio.example.com"], "127.0.0.1:8554", "127.0.0.1:1935"))

    def test_a_camera_rtsp_stream_goes_through_the_relay_without_ffmpeg(self):
        shown = []
        relay = mock.Mock(running=True, settings=server.clean_relay({"viewKey": "abcd1234"}), show_cameras=lambda streams: shown.append(dict(streams)))
        port = self.rtsp_camera()
        with mock.patch.object(Handler, "relay", relay), mock.patch.object(Handler, "ffmpeg", None), \
                mock.patch.object(server, "STREAM_HTTP_PORTS", ()), mock.patch.object(server, "STREAM_RTSP_PORTS", (port,)):
            self.use_cameras([{"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port}])
            deadline = time.monotonic() + 3
            while (stream := self.session()["cameras"][0]["stream"])["state"] == "looking" and time.monotonic() < deadline:
                time.sleep(0.05)
            told = self.session()["relay"]
        self.assertEqual((stream["state"], stream["kind"], stream["playable"], stream["webrtc"]), ("found", "rtsp", True, True))
        self.assertNotIn("note", stream)
        self.assertEqual(shown[-1], {0: f"rtsp://127.0.0.1:{port}/video1"})
        self.assertEqual(told, {"running": True, "port": 8889, "key": "abcd1234"})

    def test_feed_page_needs_the_view_key_from_other_computers(self):
        relay = mock.Mock(running=True, settings=server.clean_relay({"viewKey": "abcd1234"}))
        with mock.patch.object(Handler, "relay", relay):
            with urlopen(f"{self.base_url}/feed", timeout=2) as response:
                self.assertIn(b"/whep", response.read())
            Handler.trust_this_computer = False
            for address, wanted in (("/feed", 401), ("/feed?key=wrong", 401), ("/feed?key=abcd1234", 200)):
                try:
                    with urlopen(f"{self.base_url}{address}", timeout=2) as response:
                        status = response.status
                except Exception as error:
                    status = error.code
                self.assertEqual(status, wanted, address)
        Handler.trust_this_computer = True
        with self.assertRaises(Exception) as missing:
            urlopen(f"{self.base_url}/feed", timeout=2)
        self.assertEqual(missing.exception.code, 404)

    def test_cameras_are_only_looked_at_when_asked(self):
        looked = []
        with mock.patch.object(server.StreamFinder, "_find", lambda _finder, camera: looked.append(camera["name"]) or {"state": "none", "note": "nothing"}):
            self.use_cameras([{"name": "PTZ1", "host": "127.0.0.1", "port": self.camera.port}])
            time.sleep(0.3)
            self.assertEqual(looked, ["PTZ1"])
            self.session()
            time.sleep(0.2)
            self.assertEqual(looked, ["PTZ1"])
            self.assertEqual(self.send({"v": 8}, "/api/streams"), 200)
            time.sleep(0.3)
            self.assertEqual(looked, ["PTZ1", "PTZ1"])

    # VDO.Ninja room

    def test_a_room_is_made_up_when_there_is_none(self):
        first, second = server.clean_vdo(None), server.clean_vdo({})
        self.assertEqual(first["base"], "https://vdo.ninja")
        self.assertTrue(all(first[name] for name in ("room", "feed", "password", "codirector")))
        self.assertNotEqual(first["room"], second["room"])
        self.assertEqual(first["crew"], [])

    def test_room_settings_and_crew_are_saved(self):
        vdo = {"base": "https://vdo.ninja", "room": "arrayroom", "feed": "switcher", "password": "", "codirector": "cd1",
               "crew": [{"email": "sound@example.com", "role": "viewer"}, {"email": "td@example.com", "role": "codirector"}]}
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(server, "CONFIG_PATH", Path(folder) / "config.json"):
            Handler.config = {"port": 8765}
            self.assertEqual(self.send({"v": 8, "vdo": vdo}, "/api/vdo"), 200)
            self.assertEqual(json.loads(server.CONFIG_PATH.read_text())["vdo"], vdo)
        self.assertEqual(self.session()["vdo"], vdo)

    def test_bad_room_settings_are_refused(self):
        good = server.clean_vdo(None)
        for change in ({"room": "has spaces"}, {"base": "javascript:alert(1)"}, {"crew": [{"email": "not-an-email"}]}, {"crew": [{"email": "a@b.co, c@d.co"}]}):
            self.assertEqual(self.send({"v": 8, "vdo": {**good, **change}}, "/api/vdo"), 400)

    def test_people_view_the_feed_unless_made_co_director(self):
        crew = server.clean_vdo({"crew": [{"email": "a@example.com"}, {"email": "b@example.com", "role": "admin"}, {"email": "c@example.com", "role": "codirector"}]})["crew"]
        self.assertEqual([person["role"] for person in crew], ["viewer", "viewer", "codirector"])

    def test_page_opens_with_the_obs_marker_in_its_address(self):
        for address in ("/", "/?obs=1"):
            with urlopen(f"{self.base_url}{address}", timeout=2) as response:
                self.assertIn(b"PTZ / Field Controller", response.read())

    # Google sign-in and emailed invites

    def google_setup(self, signed_in=True):
        """Stand in for Google: records every call the bridge makes and answers the way Google would."""
        Handler.vdo = server.clean_vdo({"room": "arrayroom", "feed": "switcher", "password": "pw", "codirector": "cd1",
                                        "crew": [{"email": "sound@example.com"}, {"email": "td@example.com", "role": "codirector"}]})
        Handler.google = {"clientId": "client-1", "clientSecret": "secret-1", "refreshToken": "refresh-1" if signed_in else "", "email": "me@gmail.com" if signed_in else ""}
        Handler.google_state = None
        calls = []

        def fake_google(url, form=None, token=None, body=None):
            calls.append((url, form, token, body))
            if url == server.GOOGLE_TOKEN and form.get("refresh_token") == "revoked":
                raise server.GoogleRefused("invalid_grant", "Token has been expired or revoked.")
            if url == server.GOOGLE_TOKEN:
                return {"access_token": "access-1", "refresh_token": "refresh-new"}
            if url == server.GOOGLE_ACCOUNT:
                return {"email": "me@gmail.com"}
            return {}

        patcher = mock.patch.object(server, "google_call", fake_google)
        patcher.start()
        self.addCleanup(patcher.stop)
        self.addCleanup(setattr, Handler, "google", server.clean_google(None))
        return calls

    def get(self, path):
        """Fetch a page without following redirects; returns (status, where it points)."""
        link = socket.create_connection(("127.0.0.1", self.bridge.server_port), timeout=2)
        link.sendall(f"GET {path} HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n".encode())
        reply = b""
        while chunk := link.recv(4096):
            reply += chunk
        link.close()
        head = reply.decode().split("\r\n")
        return int(head[0].split()[1]), next((line.split(": ", 1)[1] for line in head if line.startswith("Location: ")), None)

    def test_google_sign_in_round_trip(self):
        calls = self.google_setup(signed_in=False)
        status, where = self.get("/api/google/start")
        self.assertEqual(status, 302)
        asked = {name: values[0] for name, values in server.parse_qs(server.urlsplit(where).query).items()}
        self.assertTrue(where.startswith("https://accounts.google.com/o/oauth2/v2/auth?"))
        self.assertEqual((asked["client_id"], asked["access_type"], asked["response_type"]), ("client-1", "offline", "code"))
        self.assertEqual(asked["redirect_uri"], f"http://127.0.0.1:{self.bridge.server_port}/api/google/callback")
        self.assertIn("gmail.send", asked["scope"])
        self.assertNotIn("gmail.readonly", asked["scope"])

        status, where = self.get(f"/api/google/callback?code=the-code&state={asked['state']}")
        self.assertEqual(status, 302)
        self.assertIn("Signed+in+to+Google+as+me%40gmail.com", where)
        self.assertEqual(calls[0][1]["code"], "the-code")
        self.assertEqual((Handler.google["refreshToken"], Handler.google["email"]), ("refresh-new", "me@gmail.com"))
        self.assertEqual(self.session()["mail"], {"address": "me@gmail.com", "ready": True, "clientId": "client-1", "clientReady": True})

    def test_a_sign_in_answer_we_did_not_ask_for_is_refused(self):
        calls = self.google_setup(signed_in=False)
        self.get("/api/google/start")
        status, where = self.get("/api/google/callback?code=the-code&state=someone-elses")
        self.assertIn("not+started+from+this+controller", where)
        self.assertEqual((calls, Handler.google["refreshToken"]), ([], ""))
        # The state is single-use: the right answer arriving afterwards is refused too.
        self.assertIn("not+started", self.get("/api/google/callback?code=the-code&state=")[1])

    def test_invites_go_out_through_gmail_with_the_right_link_each(self):
        calls = self.google_setup()
        self.assertEqual(self.send({"v": 8, "emails": ["sound@example.com", "td@example.com"]}, "/api/invite"), 200)
        self.assertEqual(calls[0][1]["refresh_token"], "refresh-1")
        sent = [server.base64.urlsafe_b64decode(body["raw"]).decode() for url, _, token, body in calls[1:] if url == server.GMAIL_SEND and token == "access-1"]
        self.assertEqual(len(sent), 2)
        self.assertIn("To: sound@example.com", sent[0])
        self.assertIn("From: me@gmail.com", sent[0])
        self.assertIn("https://vdo.ninja/?view=switcher&solo&room=arrayroom&password=pw", sent[0])
        self.assertNotIn("director=", sent[0])
        self.assertIn("To: td@example.com", sent[1])
        self.assertIn("https://vdo.ninja/?director=arrayroom&password=pw&codirector=cd1", sent[1])

    def test_invites_only_go_to_the_crew_list(self):
        calls = self.google_setup()
        self.assertEqual(self.send({"v": 8, "emails": ["stranger@example.com"]}, "/api/invite"), 400)
        self.assertEqual(self.send({"v": 8, "emails": []}, "/api/invite"), 400)
        self.assertEqual(calls, [])

    def test_invites_need_a_sign_in_and_say_when_it_has_expired(self):
        self.google_setup(signed_in=False)
        self.assertEqual(self.send({"v": 8, "emails": ["sound@example.com"]}, "/api/invite"), 400)
        Handler.google = {**Handler.google, "refreshToken": "revoked", "email": "me@gmail.com"}
        response = self.send({"v": 8, "emails": ["sound@example.com"]}, "/api/invite", want_response=True)
        self.assertEqual(response.code, 502)
        self.assertIn("expired", json.loads(response.read())["error"])
        self.assertFalse(self.session()["mail"]["ready"])

    def test_google_secrets_are_saved_but_never_sent_back(self):
        self.google_setup()
        with tempfile.TemporaryDirectory() as folder, mock.patch.object(server, "CONFIG_PATH", Path(folder) / "config.json"):
            Handler.config = {"port": 8765}
            # Saving with the secret box left empty keeps the secret and the sign-in.
            self.assertEqual(self.send({"v": 8, "google": {"clientId": "client-1", "clientSecret": ""}}, "/api/google"), 200)
            self.assertEqual(Handler.google["refreshToken"], "refresh-1")
            # A different client forgets the sign-in made with the old one.
            self.assertEqual(self.send({"v": 8, "google": {"clientId": "client-2", "clientSecret": "secret-2"}}, "/api/google"), 200)
            self.assertEqual(json.loads(server.CONFIG_PATH.read_text())["google"], {"clientId": "client-2", "clientSecret": "secret-2", "refreshToken": "", "email": ""})
        Handler.google = {**Handler.google, "refreshToken": "refresh-9"}
        told = json.dumps(self.session())
        self.assertNotIn("secret-2", told)
        self.assertNotIn("refresh-9", told)

    def test_signing_out_withdraws_the_sign_in(self):
        calls = self.google_setup()
        self.assertEqual(self.send({"v": 8}, "/api/google/signout"), 200)
        self.assertEqual(calls, [(server.GOOGLE_REVOKE, {"token": "refresh-1"}, None, None)])
        self.assertEqual(self.session()["mail"]["ready"], False)

    # Sign-in

    def test_other_computers_need_the_password(self):
        Handler.trust_this_computer = False
        self.assertEqual(self.send(self.command("brightness_up")), 401)
        self.assertEqual(self.drive(pan=5), 401)
        with urlopen(f"{self.base_url}/", timeout=2) as response:
            self.assertIn(b'id="login"', response.read())
        self.assertEqual(self.camera.packets(), [])

    def test_wrong_password_is_refused(self):
        Handler.trust_this_computer = False
        self.assertEqual(self.send({"password": "nope"}, "/api/login"), 401)
        self.assertEqual(self.send(self.command("brightness_up"), headers={"Cookie": "ptz_session=guess"}), 401)

    def test_password_signs_in(self):
        Handler.trust_this_computer = False
        response = self.send({"password": "correct-horse"}, "/api/login", want_response=True)
        self.assertEqual(response.status, 200)
        cookie = response.headers["Set-Cookie"].split(";")[0]
        self.assertEqual(self.send(self.command("brightness_up"), headers={"Cookie": cookie}), 200)
        self.assertIn((0x0100, 1, "8101040d02ff"), self.camera.packets())

    def test_password_is_shown_in_settings_and_can_be_changed(self):
        Handler.trust_this_computer = False
        self.addCleanup(setattr, Handler, "password", "correct-horse")
        old = self.send({"password": "correct-horse"}, "/api/login", want_response=True).headers["Set-Cookie"].split(";")[0]
        request = Request(f"{self.base_url}/api/session", headers={"Cookie": old})
        with urlopen(request, timeout=2) as response:
            self.assertEqual(json.loads(response.read())["remote"]["password"], "correct-horse")

        with tempfile.TemporaryDirectory() as folder, mock.patch.object(server, "CONFIG_PATH", Path(folder) / "config.json"):
            Handler.config = {"port": 8765, "password": "correct-horse"}
            for bad in ("short", " padded pass ", 123456, ""):
                self.assertEqual(self.send({"v": 8, "password": bad}, "/api/password", headers={"Cookie": old}), 400)
            changed = self.send({"v": 8, "password": "battery-staple"}, "/api/password", headers={"Cookie": old}, want_response=True)
            self.assertEqual(changed.status, 200)
            self.assertEqual(json.loads(server.CONFIG_PATH.read_text())["password"], "battery-staple")
        new = changed.headers["Set-Cookie"].split(";")[0]
        # The browser that made the change stays signed in; the old pass and the old password stop working.
        self.assertEqual(self.drive(pan=0), 401)
        self.assertEqual(self.send({"v": 8}, "/api/streams", headers={"Cookie": new}), 200)
        self.assertEqual(self.send({"v": 8}, "/api/streams", headers={"Cookie": old}), 401)
        self.assertEqual(self.send({"password": "correct-horse"}, "/api/login"), 401)
        self.assertEqual(self.send({"password": "battery-staple"}, "/api/login"), 200)

    def test_password_cannot_be_read_without_signing_in(self):
        Handler.trust_this_computer = False
        with self.assertRaises(Exception) as refused:
            urlopen(f"{self.base_url}/api/session", timeout=2)
        self.assertEqual(refused.exception.code, 401)

    def test_repeated_wrong_passwords_lock_out(self):
        Handler.trust_this_computer = False
        Handler.failed_logins["127.0.0.1"] = [time.monotonic()] * 8
        self.assertEqual(self.send({"password": "correct-horse"}, "/api/login"), 429)

    def test_posts_from_other_sites_are_refused(self):
        self.assertEqual(self.send(self.command("brightness_up"), headers={"Origin": "http://evil.example"}), 403)


if __name__ == "__main__":
    unittest.main()
