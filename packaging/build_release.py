"""Builds the downloads for the OBS forum into dist/:

    ptz-field-controller-<version>-macos-arm64.zip   the compiled plugin (run obs-plugin/build_macos.sh first)
    ptz-field-controller-<version>-script.zip        the OBS script and bridge, for every other platform

    python3 packaging/build_release.py

Only the files listed here go in, so config.json (which holds the password) can never be swept up.
"""
import shutil
import subprocess
import tempfile
import zipfile
from pathlib import Path

VERSION = "1.1.0"
ROOT = Path(__file__).parent.parent
FOLDER = "ptz-field-controller"
GUIDE = ROOT / "packaging" / "README.md"
SCRIPT_FILES = ["server.py", "index.html", "app.css", "app.js", "qrcode.js", "ptz_field_controller.py", "install_obs_dock.py", "LICENSE"]

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
        target = dist / f"{FOLDER}-{VERSION}-macos-arm64.zip"
        with tempfile.TemporaryDirectory() as folder:
            shutil.copytree(plugin, Path(folder) / FOLDER / plugin.name, symlinks=True)
            shutil.copy(GUIDE, Path(folder) / FOLDER / "README.md")
            shutil.copy(ROOT / "LICENSE", Path(folder) / FOLDER / "LICENSE")
            target.unlink(missing_ok=True)
            # ditto keeps the bundle's code signature intact, which a plain zip would not.
            subprocess.run(["ditto", "-c", "-k", "--keepParent", str(Path(folder) / FOLDER), str(target)], check=True)
        print(f"Wrote {target}")
