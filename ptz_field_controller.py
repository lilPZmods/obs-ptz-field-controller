"""OBS Studio script: runs the PTZ Field Controller bridge for as long as OBS is open.

Add it under Tools > Scripts (Python must be set up on that window's Python Settings tab), then add the
controller as a dock under Docks > Custom Browser Docks, or by running install_obs_dock.py while OBS is closed.
"""
import sys
import threading
import webbrowser
from pathlib import Path
from urllib.request import urlopen

import obspython as obs

sys.path.insert(0, str(Path(__file__).parent))
import server  # noqa: E402  (the bridge, which sits beside this script)

bridge = None


def address():
    return f"http://127.0.0.1:{server.load_config()['port']}"


def already_running():
    try:
        with urlopen(f"{address()}/api/health", timeout=1) as response:
            return response.status == 200
    except OSError:
        return False


def start_bridge():
    global bridge
    if bridge is not None:
        return
    try:
        bridge = server.start()
    except OSError as error:
        # The port is taken: usually the bridge is already running by itself (as a login item, say), which is fine.
        if already_running():
            obs.script_log(obs.LOG_INFO, f"PTZ Field Controller: using the bridge already running at {address()}")
        else:
            obs.script_log(obs.LOG_WARNING, f"PTZ Field Controller could not start: {error}")
        return
    threading.Thread(target=bridge.serve_forever, daemon=True).start()
    obs.script_log(obs.LOG_INFO, f"PTZ Field Controller: running at {address()}")


def stop_bridge():
    global bridge
    if bridge is None:
        return
    # Order matters: stop taking requests, then stop every camera, then let go of the port.
    bridge.shutdown()
    server.stop(bridge)
    bridge = None


def script_description():
    return (
        "<b>PTZ Field Controller</b><br>"
        "Drives PTZ cameras from a game controller. This script runs the controller's bridge while OBS is open.<br><br>"
        f"Add it as a dock under <i>Docks &gt; Custom Browser Docks</i> with the address <code>{address()}</code>. "
        "Cameras are entered on the controller's own settings page."
    )


def open_in_browser(_properties, _property):
    webbrowser.open(address())
    return False


def script_properties():
    properties = obs.obs_properties_create()
    obs.obs_properties_add_button(properties, "open", "Open the controller in a browser", open_in_browser)
    return properties


def script_load(_settings):
    start_bridge()


def script_unload():
    stop_bridge()
