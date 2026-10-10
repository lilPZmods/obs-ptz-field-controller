# OBS forum listing

For https://obsproject.com/forum/resources/ → Add resource. Category: **OBS Studio Plugins**.

**Title:** PTZ Field Controller

**Tag line:** Drive PTZ cameras from a game controller, in an OBS dock

**Version:** 1.1.0

**Supported platforms:** macOS on Apple silicon as a compiled plugin; Windows, Linux and Intel Macs through the included OBS script

**Minimum OBS Studio version:** 28.0.0

**Source code URL:** _to fill in: the public repository_

**Licence:** GPL-2.0-or-later

**Download:** upload `dist/ptz-field-controller-1.1.0-macos-arm64.zip` (plugin) and `dist/ptz-field-controller-1.1.0-script.zip` (other platforms)

**Screenshots:** `packaging/screenshots/controller.png`, `packaging/screenshots/settings.png`

## Description

PTZ Field Controller turns a game controller into a PTZ joystick, docked inside OBS.

The right stick pans and tilts, the left stick zooms, and speed follows how far you push, the way a hardware joystick controller behaves. Cameras are driven directly over the network with VISCA over IP (Sony's UDP form, port 52381), so there is nothing between the stick and the camera to add lag.

**Features**

- Smooth, proportional pan, tilt and zoom from the sticks, with a top-speed limit on the triggers
- Any number of cameras; each is selected with a controller button or a keyboard key you choose
- Shows the selected camera's video in the dock when the camera offers Motion JPEG or still pictures (RTSP with ffmpeg installed)
- Cameras stop by themselves if the dock closes or the connection drops
- Dark and light themes
- Also works in an ordinary browser, including from another computer with a password
- Brightness and red/blue gain on the D-pad and face buttons
- Makes a VDO.Ninja room for the switcher feed: watch it on another device from a QR code, and invite crew by email as viewers or co-directors

**Requirements**

- Python 3.9 or later (no extra packages)
- Cameras that accept VISCA over IP on UDP. Developed with Sony SRG cameras
- A controller with the standard layout (Xbox, PlayStation and most others)

**Install (macOS, Apple silicon)**

1. Quit OBS and copy `ptz-field-controller.plugin` into `~/Library/Application Support/obs-studio/plugins/`.
2. Open OBS and tick Docks → PTZ Field Controller.
3. In the dock, press the gear, enter your cameras' IP addresses and save.
4. Press any button on the controller to wake it.

**Install (other platforms)**: add `ptz_field_controller.py` under Tools → Scripts, then a custom browser dock at `http://127.0.0.1:8765/?obs=1`.

Full instructions are in the README inside the download.
