import json
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock
from urllib.request import urlopen

import install_obs_dock
import server


class DockInstallerTests(unittest.TestCase):
    SETTINGS = "[General]\nFirstRun=true\n\n[BasicWindow]\ngeometry=Ad\\\\nQ\nExtraBrowserDocks=[]\nAlwaysOnTop=false\n\n[Basic]\nProfile=Untitled\n"

    def install(self, text, *arguments):
        """Run the installer against a settings file holding `text`, and return what the file holds afterwards."""
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "user.ini"
            path.write_bytes(text.encode())
            with mock.patch.object(install_obs_dock, "obs_settings_file", lambda: path), mock.patch.object(install_obs_dock, "obs_is_running", lambda: False), \
                    mock.patch.object(sys, "argv", ["install_obs_dock.py", *arguments]), mock.patch("builtins.print"):
                install_obs_dock.main()
            return path.read_bytes().decode()

    def docks(self, text):
        line = next(line for line in text.splitlines() if line.startswith("ExtraBrowserDocks="))
        return json.loads(line.partition("=")[2])

    def test_adds_the_dock_and_leaves_everything_else_alone(self):
        after = self.install(self.SETTINGS, "--url", "http://127.0.0.1:8765")
        self.assertEqual(self.docks(after), [{"title": "PTZ Field Controller", "url": "http://127.0.0.1:8765", "uuid": install_obs_dock.DOCK_ID}])
        self.assertEqual(after.replace(next(line for line in after.splitlines() if line.startswith("ExtraBrowserDocks=")), "ExtraBrowserDocks=[]"), self.SETTINGS)

    def test_adding_twice_keeps_one_dock_and_other_docks_stay(self):
        other = {"title": 'Chat "main"', "url": "https://example.com/chat", "uuid": "1234"}
        settings = self.SETTINGS.replace("ExtraBrowserDocks=[]", "ExtraBrowserDocks=" + install_obs_dock.escape(json.dumps([other])))
        once = self.install(settings, "--url", "http://127.0.0.1:8765")
        twice = self.install(once, "--url", "http://127.0.0.1:9000")
        self.assertEqual([dock["url"] for dock in json.loads(install_obs_dock.unescape(next(line for line in twice.splitlines() if line.startswith("ExtraBrowserDocks=")).partition("=")[2]))],
                         ["https://example.com/chat", "http://127.0.0.1:9000"])
        self.assertEqual(self.install(twice, "--remove"), settings)

    def test_settings_without_a_dock_list_get_one(self):
        self.assertEqual(len(self.docks(self.install("[General]\nFirstRun=true\n"))), 1)
        self.assertEqual(len(self.docks(self.install("[BasicWindow]\nAlwaysOnTop=false\n[Basic]\nProfile=Untitled\n"))), 1)

    def test_refuses_while_obs_is_running(self):
        with mock.patch.object(install_obs_dock, "obs_settings_file", lambda: Path("user.ini")), mock.patch.object(install_obs_dock, "obs_is_running", lambda: True), \
                mock.patch.object(sys, "argv", ["install_obs_dock.py"]):
            with self.assertRaises(SystemExit):
                install_obs_dock.main()


class ObsScriptTests(unittest.TestCase):
    def test_bridge_runs_while_the_script_is_loaded(self):
        logged = []
        obs = types.SimpleNamespace(LOG_INFO=1, LOG_WARNING=2, script_log=lambda _level, message: logged.append(message))
        with tempfile.TemporaryDirectory() as folder, mock.patch.dict(sys.modules, {"obspython": obs}), \
                mock.patch.object(server, "CONFIG_PATH", Path(folder) / "config.json"):
            server.CONFIG_PATH.write_text(json.dumps({"host": "127.0.0.1", "port": 0, "cameras": [{"name": "PTZ1"}]}))
            import ptz_field_controller as script
            script.script_load(None)
            try:
                port = script.bridge.server_port
                with urlopen(f"http://127.0.0.1:{port}/api/health", timeout=2) as response:
                    self.assertEqual(response.status, 200)
                with urlopen(f"http://127.0.0.1:{port}/", timeout=2) as response:
                    self.assertIn(b"PTZ / Field Controller", response.read())
            finally:
                script.script_unload()
            self.assertIsNone(script.bridge)
            with self.assertRaises(OSError):
                urlopen(f"http://127.0.0.1:{port}/api/health", timeout=1)
        self.assertTrue(logged[0].startswith("PTZ Field Controller: running at"))


if __name__ == "__main__":
    unittest.main()
