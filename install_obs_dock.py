"""Adds PTZ Field Controller to OBS Studio as a dock, so it appears in OBS's Docks menu.

    python3 install_obs_dock.py             add the dock (or update its address)
    python3 install_obs_dock.py --remove    take it out again

Run it while OBS is closed: OBS writes its settings when it quits and would undo the change.
It does the same thing as adding the address by hand under Docks > Custom Browser Docks.
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path

TITLE = "PTZ Field Controller"
# Always the same id, so OBS remembers where the dock was put and adding it twice changes nothing.
DOCK_ID = str(uuid.uuid5(uuid.NAMESPACE_URL, "ptz-field-controller"))
SECTION = "[BasicWindow]"
KEY = "ExtraBrowserDocks="


def obs_settings_folders():
    home = Path.home()
    if sys.platform == "darwin":
        return [home / "Library/Application Support/obs-studio"]
    if sys.platform == "win32":
        return [Path(os.environ.get("APPDATA", home / "AppData/Roaming")) / "obs-studio"]
    return [Path(os.environ.get("XDG_CONFIG_HOME", home / ".config")) / "obs-studio",
            home / ".var/app/com.obsproject.Studio/config/obs-studio"]


def obs_settings_file():
    """OBS 31 and later keep the dock list in user.ini; earlier versions keep it in global.ini."""
    for folder in obs_settings_folders():
        for name in ("user.ini", "global.ini"):
            if (folder / name).exists():
                return folder / name
    return None


def obs_is_running():
    if sys.platform == "win32":
        tasks = subprocess.run(["tasklist"], capture_output=True, text=True).stdout.lower()
        return "obs64.exe" in tasks or "obs.exe" in tasks
    names = ["OBS"] if sys.platform == "darwin" else ["obs"]
    return any(subprocess.run(["pgrep", "-x", name], capture_output=True).returncode == 0 for name in names)


def default_address():
    port = 8765
    try:
        port = json.loads((Path(__file__).parent / "config.json").read_text()).get("port", port)
    except (OSError, ValueError):
        pass
    return f"http://127.0.0.1:{port}"


# OBS writes backslashes and line breaks inside a value as \\ \n \r.
def unescape(value):
    return value.replace("\\n", "\n").replace("\\r", "\r").replace("\\\\", "\\")


def escape(value):
    return value.replace("\\", "\\\\").replace("\n", "\\n").replace("\r", "\\r")


def change_docks(text, change):
    """Return the settings text with `change` applied to its list of browser docks; everything else is left as it was."""
    lines = text.splitlines(keepends=True)
    ending = "\r\n" if text.endswith("\r\n") else "\n"
    section = next((index for index, line in enumerate(lines) if line.strip() == SECTION), None)
    if section is None:
        if lines and not lines[-1].endswith("\n"):
            lines[-1] += ending
        lines += [ending, SECTION + ending]
        section = len(lines) - 1
    stop = next((index for index in range(section + 1, len(lines)) if lines[index].startswith("[")), len(lines))
    at = next((index for index in range(section + 1, stop) if lines[index].startswith(KEY)), None)
    docks = []
    if at is not None:
        try:
            docks = json.loads(unescape(lines[at].rstrip("\r\n")[len(KEY):]) or "[]")
        except ValueError:
            raise SystemExit("OBS's list of browser docks could not be read, so nothing was changed.")
    line = KEY + escape(json.dumps(change(docks), ensure_ascii=False)) + ending
    if at is None:
        lines.insert(section + 1, line)
    else:
        lines[at] = line
    return "".join(lines)


def main():
    parser = argparse.ArgumentParser(description="Add PTZ Field Controller to OBS Studio as a dock.")
    parser.add_argument("--remove", action="store_true", help="take the dock out of OBS")
    parser.add_argument("--url", default=default_address(), help="the controller's address (default: %(default)s)")
    parser.add_argument("--force", action="store_true", help="go ahead even though OBS looks to be running")
    options = parser.parse_args()

    path = obs_settings_file()
    if path is None:
        raise SystemExit("OBS Studio's settings were not found. Open OBS once, close it, and run this again.")
    if obs_is_running() and not options.force:
        raise SystemExit("OBS is running. Quit OBS first: it writes its settings when it closes and would undo this.")

    def change(docks):
        others = [dock for dock in docks if dock.get("uuid") != DOCK_ID]
        return others if options.remove else others + [{"title": TITLE, "url": options.url, "uuid": DOCK_ID}]

    before = path.read_bytes()
    marker = b"\xef\xbb\xbf" if before.startswith(b"\xef\xbb\xbf") else b""
    after = marker + change_docks(before[len(marker):].decode("utf-8"), change).encode("utf-8")
    if after == before:
        print("Nothing to change.")
        return
    backup = path.with_name(path.name + ".before-ptz-dock")
    if not backup.exists():
        shutil.copy2(path, backup)
    path.write_bytes(after)
    if options.remove:
        print(f"Removed the {TITLE} dock from OBS.")
    else:
        print(f"Added. Open OBS and tick Docks > {TITLE} ({options.url}).")
    print(f"OBS settings changed: {path}\nCopy from before the first change: {backup}")


if __name__ == "__main__":
    main()
