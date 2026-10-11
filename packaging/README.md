# PTZ Field Controller

Drive PTZ cameras from a game controller, in a dock inside OBS Studio or in any browser.

- **Right stick** pans and tilts, **left stick** zooms. Speed follows how far the stick is pushed, like a hardware joystick controller.
- Cameras are driven directly with **VISCA over IP** (Sony's UDP form, port 52381).
- Any number of cameras. Each is selected with a controller button or a keyboard key of your choosing.
- If the selected camera offers a video stream the browser can show, it appears above the controls.
- Dark by default, with a light theme.
- Brightness and red/blue gain from the D-pad and face buttons, also sent as VISCA.
- A VDO.Ninja room is made for you, to watch the switcher feed on another device and invite crew by email.

## What you need

- OBS Studio 32 for the macOS plugin (it is built against OBS 32.2). The script works with OBS 28 or later.
- Nothing else on a Mac: the plugin carries everything it needs. (The script for other platforms needs Python 3.9 or later.)
- Cameras that take VISCA over IP on UDP, on the same network. Developed with Sony SRG cameras.
- A game controller with the standard layout (Xbox, PlayStation and most others).

## Install

**macOS (Apple silicon)**

1. Double-click `ptz-field-controller-1.2.1-macos-arm64.pkg` and click through the installer. It puts the plugin in your OBS plugins folder; there are no files to move and no password to enter.
2. Open OBS (quit and reopen it if it was running). The controller appears as its own panel the first time; drag it into the OBS window to dock it. Later it is under Docks → PTZ Field Controller.
3. Press the gear in the panel, enter each camera's IP address, and save.
4. Plug in the controller and press any button on it.

The installer is not signed with an Apple developer certificate, so the first time macOS says it "cannot be opened". Right-click the file and choose Open, or allow it under System Settings → Privacy & Security.

To remove it, delete `~/Library/Application Support/obs-studio/plugins/ptz-field-controller.plugin`.

**Windows, Linux and Intel Macs, as a script**

1. Unzip the script folder somewhere it can stay.
2. Tools → Scripts → Python Settings: point OBS at your Python. Then on the Scripts tab press **+** and choose `ptz_field_controller.py`.
3. Docks → Custom Browser Docks: name it `PTZ Field Controller`, address `http://127.0.0.1:8765/?obs=1`.

## Using it

| Control | Does |
| --- | --- |
| Right stick | Pan and tilt |
| Left stick up/down | Zoom in/out |
| Left stick click, right stick click, Start | Select camera 1, 2, 3 |
| LT / RT | Lower / raise the top speed |
| Arrow keys, W/S | Pan/tilt and zoom from the keyboard |
| 1–9 | Select a camera from the keyboard |

Adding a camera in settings asks which controller button should select it. The spare buttons are the stick clicks, Start, Back, the bumpers and D-pad left/right. When they are used up, further cameras are selected with a keyboard key.

D-pad up/down make the picture brighter or dimmer; A/B change red gain and X/Y blue gain. The camera must be in Bright exposure mode and manual white balance for those; the two buttons on the Balance panel switch them on.

## Video

The bridge looks for a stream on each camera when it starts, when a page opens and when you press Refresh: Motion JPEG and still pictures on ports 80 and 8080, then RTSP on 554 and 8554. Motion JPEG and stills show directly. RTSP needs `ffmpeg` installed on the same computer, because browsers cannot play it. If your camera's stream is somewhere unusual or needs a sign-in, enter its address in that camera's settings, for example `http://user:password@192.168.1.20/mjpeg`.

## Crew invites

Invite collaborators keeps the crew's email addresses and sends each their link, to view the director's feed or to co-direct. Sending uses Google sign-in: make an OAuth client (Desktop app) with the Gmail API enabled in Google Cloud Console, paste its ID and secret under Email in settings, and press Sign in with Google in a browser on the same computer.

## Other computers

The controller can be opened from another computer at `http://<this computer>:8765`. A password is made up for this the first time the controller runs; settings shows it, with the address to use, and lets you change it. Set `"host": "127.0.0.1"` in `config.json` to turn remote access off.

## Good to know

- A moving camera stops by itself within about a second if the dock closes, OBS quits or the connection drops.
- UDP port 52381 on the computer must be free: Sony cameras send their replies there.
- Settings are in `config.json`: in OBS's `plugin_config/ptz-field-controller` folder for the plugin, beside the files for the script. Delete it to start again.
- Licence: GPL-2.0-or-later.
