# OBS forum listing

For https://obsproject.com/forum/resources/ → Add resource. Category: **OBS Studio Plugins**.

**Title:** PTZ Field Controller

**Tag line:** Drive PTZ cameras from a game controller, in an OBS dock

**Version:** 1.2.1

**Supported platforms:** macOS on Apple silicon as a compiled plugin; Windows, Linux and Intel Macs through the included OBS script

**Minimum OBS Studio version:** 32.0.0 (the plugin is built against OBS 32.2)

**Source code URL:** https://github.com/lilPZmods/obs-ptz-field-controller

**Licence:** GPL-2.0-or-later

**Download:** choose "external download URL" and give https://github.com/lilPZmods/obs-ptz-field-controller/releases/latest

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

- macOS on Apple silicon, OBS 32. Nothing else to install
- Cameras that accept VISCA over IP on UDP. Developed with Sony SRG cameras
- A controller with the standard layout (Xbox, PlayStation and most others)

**Install (macOS, Apple silicon)**

1. Download the `.pkg` and double-click it. (It is not signed with an Apple certificate: right-click → Open the first time.)
2. Open OBS. The controller appears as a panel; drag it into the window to dock it.
3. Press the gear, enter your cameras' IP addresses and save.
4. Press any button on the controller to wake it.

**Other platforms**: the script download has an OBS Python script that does the same job; its README has the steps.

Full instructions are in the README inside the download.
