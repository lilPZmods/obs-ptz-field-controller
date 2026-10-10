# PTZ / Field Controller

A game-controller interface for Sony PTZ cameras (three to start with; add as many as you like in settings).

- **Pan, tilt, zoom, brightness and red/blue gain** all go straight from this computer to the cameras over VISCA, the way a Sony RM-IP10 does. Speed follows how far the sticks are pushed. Nothing depends on Companion.
- **Video**: if the selected camera offers a stream the browser can show, it appears above the controls.
- **Switcher feed and crew**: a VDO.Ninja room is made automatically; the feed can be watched in a box, a window or on another device, and crew can be invited by email.
- It runs in a browser tab or as a dock inside OBS Studio (native plugin).

## Run

`python3 server.py` starts the bridge, the small program that talks to the cameras. No Python packages are required. The OBS plugin starts it by itself when none is running.

- On the same computer: open <http://127.0.0.1:8765>. No password.
- From any other computer: open `http://<this computer>:8765`, enter the password, and plug the game controller into that computer. Use Chrome or Safari. A controller is only seen by the computer it is plugged into.
- In OBS: see [OBS dock](#obs-dock).

`config.json`, created the first time the bridge runs, holds `password`, `port`, `host` (`0.0.0.0` allows other computers; `127.0.0.1` keeps it to this one) plus the camera list, VDO.Ninja room and Google sign-in, which are edited on the settings page. Restart the bridge after editing it by hand.

## Settings page

The gear button opens settings.

- **Dark mode** is on by default. The switch here, or the sun/moon button in the top bar, changes it. The choice is remembered per browser.
- **Cameras**: each camera's name, IP address and VISCA port (52381 on Sony cameras), the button and key that select it, and an optional stream address. **Save settings** writes the list to `config.json` on this computer and starts using it straight away, for every computer that has the controller open.
- **Add camera** adds another and asks which controller button should select it. The spare buttons are the stick clicks, Start, Back, the two bumpers and D-pad left/right. When they are all taken the prompt says so and the camera is selected from the keyboard instead: number keys 1–9 are given out automatically, and any other free key can be pressed to set it.
- **VDO.Ninja**: the room's address, name, feed name and passwords. They are made up the first time the bridge runs; change them only to use a room you already have. **New room** makes fresh ones.
- The stick dead zone and move-engagement threshold are saved per browser, as before.

The Mac running the controller must be on the same network as the cameras, and UDP port 52381 on it must be free (the cameras send their replies there).

## Video

The controller looks at the cameras for a stream only when asked: once when the camera list is put to use (bridge start, or saving settings), once each time a page is opened, and when **Refresh** is pressed (under the camera strip, or **Look for streams again** in settings). Nothing runs on a timer. If a stream that was showing stops, a red **stream dropped** pill appears with its own Refresh; a camera that never had a stream shows nothing at all. It tries the usual Motion JPEG and still-picture addresses on ports 80 and 8080, then RTSP on 554 and 8554.

- Motion JPEG and still pictures show directly.
- RTSP (H.264) cannot be shown by a browser, so the controller converts it with `ffmpeg`. It looks for ffmpeg at the `ffmpeg` path in `config.json`, then `bin/ffmpeg` in this folder, then the usual system places.
- If a camera's stream is somewhere unusual or needs a sign-in, enter it in that camera's stream address, e.g. `http://user:password@192.168.1.20/mjpeg` or `rtsp://192.168.1.20/video1`.

## Switcher feed and crew (VDO.Ninja)

VDO.Ninja has no accounts: a room exists as soon as its address is opened, so the controller makes up a room name, feed name and passwords and keeps them in `config.json`.

1. On the computer at the switcher, press **Open director room** (in settings or the invite box) and share the switcher's picture and sound there. That is the director's feed.
2. **Switcher feed** on the main page shows it: in a **popup box** over the controls (drag it by its bar, resize from the corner), in a **new window**, or on **another device**, which shows a QR code and link for an iPad or phone.
3. **Invite collaborators** keeps the crew's email addresses. Each person either views the director's feed only (the default) or is a co-director with the room's director controls. **Email** sends their invite from the Google account signed in under settings; **Copy** copies their link.

Sending uses Google sign-in (OAuth) and the Gmail API. The controller asks only for permission to send mail as you; it cannot read mail. Set it up once:

1. In Google Cloud Console, make a project, enable the **Gmail API**, and set up the OAuth consent screen (choose *Internal* for a Google Workspace account; otherwise add yourself as a test user).
2. Credentials → Create credentials → **OAuth client ID** → **Desktop app**. Paste its client ID and secret under Email in settings.
3. Press **Sign in with Google** in a browser on this computer at <http://127.0.0.1:8765> (Google only returns a sign-in to the computer's own address, and refuses OBS's built-in browser).

The sign-in is kept in `config.json` on this computer and renewed by itself; the secret and sign-in are never sent to a browser. Invites only go to addresses on the crew list. **Sign out** withdraws the permission at Google.

In the OBS dock only **another device** is offered; a separate OBS plugin will handle viewing there.

## How the cameras are driven

Another controller on the network, such as Companion, can share each camera with this one. A camera only accepts a command numbered higher than the last one it saw, so the controller resets the camera's count when a move starts and again when it ends, which keeps the other controller's commands working in between. If it sends a command to a camera in the middle of a stick move, the controller notices the camera refusing its next command, resets, and carries on.

The cameras silently ignore a command that arrives less than about 20–40 ms after the previous one. So the controller sends each camera one command at a time, 40 ms apart, always from the newest stick position, and treats a command as done only when the camera answers it; an unanswered stop is sent again until it lands.

A moving camera stops by itself within about a second if the controller page closes, loses its connection, or the server is stopped.

## Controller Map

- Right stick: pan and tilt in eight directions. Push further to move faster.
- Left stick up/down: zoom in/out. Push further to zoom faster.
- Click left stick: Camera 1; click right stick: Camera 2; Start: Camera 3. Further cameras use whatever was assigned when they were added; all of these can be changed in settings.
- Left trigger: lower the top speed; right trigger: raise it (25%, 50%, 75%, 100%). The sticks still ramp from slow up to that top speed.
- D-pad up/down: brighter/dimmer (the camera must be in Bright exposure mode).
- A: less red; B: more red. X: less blue; Y: more blue (the camera must be in manual white balance).
- **Set manual WB** and **Set bright mode** on the Balance panel put the selected camera in those modes.
- Keyboard: arrows pan/tilt, W/S zoom, A/B red, X/Y blue, I/K brightness, [ / ] lower/raise top speed, 1–9 (or the key set in settings) select cameras.

The bridge refuses commands from a browser tab running older page code; reload the tab if that happens.

## OBS dock

`obs-plugin/` is a native OBS plugin. It adds **Docks → PTZ Field Controller** and, when no bridge is already running, starts one for as long as OBS is open.

- Build and install: quit OBS, then `obs-plugin/build_macos.sh --install`. It needs only the command line tools, plus the OBS and Qt headers in `.build/` (the script's header says where they come from).
- It is installed at `~/Library/Application Support/obs-studio/plugins/ptz-field-controller.plugin`. Delete that folder to remove it.
- `ptz_field_controller.py` (an OBS script) and `install_obs_dock.py` (adds a plain browser dock) do the same job without compiling, for Windows and Linux.

`packaging/` holds what is needed to publish it: `build_release.py` makes the zips in `dist/`, `README.md` is the guide that goes inside them, and `OBS_FORUM_LISTING.md` is the text for the OBS forum's Resources page.

Licensed under the GNU General Public License, version 2 or later (see `LICENSE`), the same licence as OBS Studio. `qrcode.js` is Kazuhiko Arase's QR Code Generator, MIT licence.

## Tests

```sh
python3 -m unittest discover -s tests
```
