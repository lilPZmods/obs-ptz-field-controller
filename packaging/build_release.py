"""Builds the downloads into dist/:

    ptz-field-controller-<version>-macos-arm64.pkg   the installer: double-click, and the plugin is in OBS
    ptz-field-controller-<version>-script.zip        the OBS script and bridge, for other platforms

    obs-plugin/build_macos.sh && python3 packaging/build_release.py

Only the files listed here go in, so config.json (which holds the password) can never be swept up.
"""
import plistlib
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

VERSION = "1.3.0"
ROOT = Path(__file__).parent.parent
FOLDER = "ptz-field-controller"
IDENTIFIER = "ca.arraymusic.ptz-field-controller"
GUIDE = ROOT / "packaging" / "README.md"
SCRIPT_FILES = ["server.py", "index.html", "app.css", "app.js", "qrcode.js", "ptz_field_controller.py", "install_obs_dock.py", "LICENSE"]

# The installer puts the plugin in the signed-in user's own OBS plugins folder, so it never asks for a password.
DISTRIBUTION = f"""<?xml version="1.0" encoding="utf-8"?>
<installer-gui-script minSpecVersion="2">
  <title>PTZ Field Controller</title>
  <options customize="never" require-scripts="false" hostArchitectures="arm64"/>
  <domains enable_anywhere="false" enable_currentUserHome="true" enable_localSystem="false"/>
  <conclusion file="conclusion.html" mime-type="text/html"/>
  <choices-outline><line choice="plugin"/></choices-outline>
  <choice id="plugin" title="PTZ Field Controller"><pkg-ref id="{IDENTIFIER}"/></choice>
  <pkg-ref id="{IDENTIFIER}" version="{VERSION}">plugin.pkg</pkg-ref>
</installer-gui-script>
"""
CONCLUSION = """<html><body style="font: 13px -apple-system, sans-serif">
<h3>PTZ Field Controller is installed.</h3>
<p>Open OBS Studio (quit and reopen it if it was running). The controller appears as its own panel; drag it wherever you like.</p>
<p>Press the gear in the panel to enter your cameras' IP addresses, then press any button on your game controller.</p>
</body></html>
"""


def build_installer(plugin, target):
    with tempfile.TemporaryDirectory() as folder:
        work = Path(folder)
        shutil.copytree(plugin, work / "root" / plugin.name, symlinks=True)
        # Without this, the installer treats the plugin as movable and may "update" a copy found elsewhere on the disk.
        subprocess.run(["pkgbuild", "--analyze", "--root", str(work / "root"), str(work / "component.plist")], check=True, capture_output=True)
        components = plistlib.loads((work / "component.plist").read_bytes())
        for component in components:
            component["BundleIsRelocatable"] = False
        (work / "component.plist").write_bytes(plistlib.dumps(components))
        (work / "packages").mkdir()
        subprocess.run(["pkgbuild", "--root", str(work / "root"), "--component-plist", str(work / "component.plist"), "--identifier", IDENTIFIER,
                        "--version", VERSION, "--install-location", "/Library/Application Support/obs-studio/plugins",
                        str(work / "packages" / "plugin.pkg")], check=True, capture_output=True)
        (work / "resources").mkdir()
        (work / "resources" / "conclusion.html").write_text(CONCLUSION)
        (work / "distribution.xml").write_text(DISTRIBUTION)
        subprocess.run(["productbuild", "--distribution", str(work / "distribution.xml"), "--package-path", str(work / "packages"),
                        "--resources", str(work / "resources"), str(target)], check=True, capture_output=True)


if __name__ == "__main__":
    dist = ROOT / "dist"
    dist.mkdir(exist_ok=True)

    target = dist / f"{FOLDER}-{VERSION}-script.zip"
    with zipfile.ZipFile(target, "w", zipfile.ZIP_DEFLATED) as archive:
        for name in SCRIPT_FILES:
            archive.write(ROOT / name, f"{FOLDER}/{name}")
        archive.write(GUIDE, f"{FOLDER}/README.md")
    print(f"Wrote {target}")

    plugin = dist / f"{FOLDER}.plugin"
    if not plugin.exists():
        print("No compiled plugin in dist/: run obs-plugin/build_macos.sh to include it.")
    else:
        target = dist / f"{FOLDER}-{VERSION}-macos-arm64.pkg"
        build_installer(plugin, target)
        print(f"Wrote {target}")
