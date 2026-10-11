// Picture adjustments, sent to the selected camera as VISCA commands by the bridge.
const ADJUSTMENT_NAMES = {
  brightness_up: "Brighter", brightness_down: "Dimmer", red_up: "More red", red_down: "Less red", blue_up: "More blue", blue_down: "Less blue",
  manual_white_balance: "Manual white balance on", bright_mode: "Bright exposure mode on",
};

// Fastest speeds the cameras accept, in their own units.
const MAX_PAN_SPEED = 24;
const MAX_TILT_SPEED = 23;
const MAX_ZOOM_SPEED = 7;
// How sharply speed builds as a stick is pushed: above 1 keeps the first part of the travel slow for fine framing.
const PAN_CURVE = 1.8;
const ZOOM_CURVE = 1.3;
// Within this much of straight up/down or left/right (as a fraction of the stick's direction), the other axis stays still.
const AXIS_SNAP = 0.2;
// While a camera is moving its state is re-sent this often; the bridge stops the camera if the updates dry up.
const DRIVE_REPEAT_MS = 150;
const TOP_SPEED_STEPS = [0.25, 0.5, 0.75, 1];
// Sent with every command; the bridge refuses a tab still running older page code.
const CLIENT_VERSION = 8;
// How often the page asks the bridge what it already knows (camera list, stream status). This reads the bridge's
// memory only: the cameras themselves are looked at when a page opens, on save, and on the refresh button.
const SESSION_REFRESH_MS = 4000;

// Standard gamepad layout: 0 A, 1 B, 2 X, 3 Y, 4 LB, 5 RB, 6 LT, 7 RT, 8 Back, 9 Start, 10 left stick click,
// 11 right stick click, 12-15 D-pad up/down/left/right.
const ADJUST_BUTTONS = { 0: "red_down", 1: "red_up", 2: "blue_down", 3: "blue_up", 12: "brightness_up", 13: "brightness_down" };
const TOP_SPEED_BUTTONS = { 6: -1, 7: 1 };
const TAKEN_BUTTONS = { 0: ["A", "red gain"], 1: ["B", "red gain"], 2: ["X", "blue gain"], 3: ["Y", "blue gain"], 6: ["LT", "top speed"], 7: ["RT", "top speed"], 12: ["D-pad up", "brightness"], 13: ["D-pad down", "brightness"] };
// The buttons left over for selecting cameras, in the order they are offered: [short name, full name].
const SPARE_BUTTONS = { 10: ["L3", "CLICK LEFT STICK"], 11: ["R3", "CLICK RIGHT STICK"], 9: ["ST", "START"], 4: ["LB", "LEFT BUMPER"], 5: ["RB", "RIGHT BUMPER"], 8: ["BK", "BACK"], 14: ["←", "D-PAD LEFT"], 15: ["→", "D-PAD RIGHT"] };
const SPARE_BUTTON_ORDER = [10, 11, 9, 4, 5, 8, 14, 15];
// Keys that already do something else, so can't select a camera.
const TAKEN_KEYS = "abxyikws[]";

// Bumped from "ptz-controller-config": saved settings under the old key pointed at the wrong Companion pages.
const storageKey = "ptz-controller-config-v2";
const defaultConfig = {
  deadzone: 0.15,
  engage: 0.28,
  topSpeed: 1,
  selectedCamera: 0,
};

function loadConfig() {
  try {
    const saved = JSON.parse(localStorage.getItem(storageKey));
    if (!saved) return structuredClone(defaultConfig);
    return { ...structuredClone(defaultConfig), ...saved };
  } catch {
    return structuredClone(defaultConfig);
  }
}

const config = loadConfig();
let selectedCamera = config.selectedCamera ?? 0;
// The cameras as the bridge has them: name, address, select button and key, and what is known of each one's stream.
let cameras = [];
// The copy of the camera list being edited while settings is open; `from` is the camera's place in `cameras`, or null if new.
let draft = null;
// The camera in `draft` waiting for a button or key to be pressed for it, while the select-button prompt is up.
let assigning = null;
let streamRetries = 0;
// The VDO.Ninja room the bridge keeps: address, room, feed name, passwords and the crew to invite.
let vdo = null;
let vdoLinkSet = {};
// How other computers reach the controller: the password, the port and this computer's network addresses.
let remote = { password: "", port: 8765, on: false, addresses: [] };
// The bridge's video relay: OBS's output and cameras' RTSP streams, passed on over WebRTC without re-encoding.
let relay = { running: false, port: 0, key: "" };
// Hangs up on whatever the camera viewer is playing over WebRTC.
let stopWatching = null;
// The Google account invites go out from: its address, whether it is signed in, and whether an OAuth client is set.
let mail = { address: "", ready: false, clientId: "", clientReady: false };
// The stream address that last broke off in the viewer. It is not loaded again until refresh is pressed.
let failedSource = null;
// Inside the OBS dock the feed is not shown in a box or window; a separate OBS plugin does that job.
const inObs = new URLSearchParams(location.search).has("obs");
let activeGamepadIndex = null;
let rafId = null;
let commandQueue = Promise.resolve();
// What the sticks are asking of the selected camera: signed speeds in the camera's units (right, up and zoom-in positive).
const STILL = { pan: 0, tilt: 0, zoom: 0, zoomSpeed: 0 };
let drive = { ...STILL };
let driveSent = { camera: 0, ...STILL };
let driveSentAt = 0;
let driveInFlight = false;
let stopRepeats = 0;
let previousButtons = new Map();
const keyboardHeld = new Set();

const byId = (id) => document.getElementById(id);
const twoDigits = (number) => String(number).padStart(2, "0");
const escapeHtml = (text) => String(text).replace(/[&<>"]/g, (character) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;" })[character]);
// The kinds of camera the bridge can drive, with the port each usually listens on.
const CAMERA_TYPES = { sony: ["SONY VISCA", 52381], "visca-udp": ["VISCA UDP", 1259], "visca-tcp": ["VISCA TCP", 5678] };
const cameraSettings = (camera) => ({ name: camera.name.trim(), type: camera.type, host: camera.host.trim(), port: camera.port, streamUrl: camera.streamUrl.trim(), button: camera.button, key: camera.key });

function setTheme(theme) {
  document.documentElement.dataset.theme = theme;
  document.querySelector('meta[name="theme-color"]').content = theme === "dark" ? "#0e1513" : "#e8eee9";
  byId("darkModeInput").checked = theme === "dark";
  try {
    localStorage.setItem("ptz-theme", theme);
  } catch {
    // Storage is blocked: the choice simply lasts until the page is reloaded.
  }
}

function postJson(path, body) {
  return fetch(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify({ v: CLIENT_VERSION, ...body }) });
}

function saveConfig() {
  config.selectedCamera = selectedCamera;
  localStorage.setItem(storageKey, JSON.stringify(config));
}

function setToast(message, error = false) {
  const toast = byId("toast");
  toast.textContent = message;
  toast.classList.toggle("error", error);
  toast.classList.add("visible");
  clearTimeout(setToast.timer);
  setToast.timer = setTimeout(() => toast.classList.remove("visible"), 2600);
}

// Button presses go to the bridge one at a time, in order.
function enqueue(path, payload) {
  commandQueue = commandQueue.then(async () => {
    const response = await fetch(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(payload),
    });
    if (response.status === 401) {
      // Signed out (or the password changed): reloading shows the sign-in page.
      location.reload();
      return;
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || `Controller bridge returned ${response.status}`);
    }
    return response.json().catch(() => ({}));
  }).catch((error) => {
    setToast(error.message, true);
  });
  return commandQueue;
}

function driveBody(camera, state) {
  return JSON.stringify({ v: CLIENT_VERSION, camera, ...state });
}

function postDrive(camera, state) {
  return fetch("/api/drive", { method: "POST", headers: { "Content-Type": "application/json" }, body: driveBody(camera, state), keepalive: true });
}

// Sends the sticks' current state to the bridge whenever it changes, and keeps repeating it while the camera moves.
// Only the newest state matters, so nothing queues up behind a slow request.
function pumpDrive(now) {
  if (driveInFlight || !cameras.length) return;
  const moving = drive.pan !== 0 || drive.tilt !== 0 || drive.zoom !== 0;
  const changed = driveSent.camera !== selectedCamera || Object.keys(STILL).some((key) => drive[key] !== driveSent[key]);
  const due = now - driveSentAt >= DRIVE_REPEAT_MS;
  if (!changed && !(due && (moving || stopRepeats > 0))) return;

  // A stop is repeated a couple of times in case one is lost on the way.
  if (changed) stopRepeats = moving ? 0 : 2;
  else if (!moving) stopRepeats -= 1;
  driveSent = { camera: selectedCamera, ...drive };
  driveSentAt = now;
  driveInFlight = true;
  postDrive(driveSent.camera, drive).then(async (response) => {
    if (response.status === 401) {
      location.reload();
      return;
    }
    if (!response.ok) {
      const result = await response.json().catch(() => ({}));
      throw new Error(result.error || `Controller bridge returned ${response.status}`);
    }
  }).catch((error) => {
    setToast(error.message, true);
  }).finally(() => {
    driveInFlight = false;
  });
}

// Stops a camera right now, outside the usual pacing. `beacon` survives the page being closed.
function stopCamera(camera, beacon = false) {
  if (beacon && navigator.sendBeacon) navigator.sendBeacon("/api/drive", new Blob([driveBody(camera, STILL)], { type: "application/json" }));
  else postDrive(camera, STILL).catch(() => {});
}

function stopMoving(beacon = false) {
  drive = { ...STILL };
  stopCamera(selectedCamera, beacon);
}

function selectCamera(index) {
  if (index < 0 || index >= cameras.length || index === selectedCamera) return;
  stopMoving();
  selectedCamera = index;
  saveConfig();
  updateCameraUI();
}

// Takes the camera list from the bridge. The strip and shortcuts are only rebuilt when a camera's settings changed.
function adoptCameras(list) {
  const before = JSON.stringify(cameras.map(cameraSettings));
  cameras = list;
  if (selectedCamera >= cameras.length) selectedCamera = 0;
  if (draft && !draft.length) openSettings(true);
  if (JSON.stringify(cameras.map(cameraSettings)) !== before) renderCameras();
  updateViewer();
  renderStreamStates();
}

function refreshSession() {
  fetch("/api/session").then((response) => {
    if (response.status === 401) location.reload();
    return response.json();
  }).then((session) => {
    adoptCameras(session.cameras ?? []);
    if (session.vdo && !byId("settingsDrawer").classList.contains("open")) adoptRoom(session);
  }).catch(() => {});
}

function shortcut(camera) {
  const button = SPARE_BUTTONS[camera.button];
  return {
    glyph: button?.[0] ?? (camera.button !== null ? `B${camera.button}` : camera.key.toUpperCase() || "–"),
    text: [button?.[1] ?? (camera.button !== null ? `BUTTON ${camera.button}` : ""), camera.key && `KEY ${camera.key.toUpperCase()}`].filter(Boolean).join(" · ") || "NO SHORTCUT",
  };
}

function renderCameras() {
  byId("cameraStrip").innerHTML = cameras.map((camera, index) => `<button class="camera-select" type="button" data-camera="${index}"><span class="camera-number">${twoDigits(index + 1)}</span><span class="camera-title">${escapeHtml(camera.name)}</span><span class="camera-mark"></span></button>`).join("");
  byId("quickSelect").innerHTML = cameras.map((camera, index) => {
    const { glyph, text } = shortcut(camera);
    return `<div class="trigger-item" data-camera="${index}"><span class="trigger-glyph">${escapeHtml(glyph)}</span><div><strong>${escapeHtml(camera.name)}</strong><small>${escapeHtml(text)}</small></div></div>`;
  }).join("");
  updateCameraUI();
}

function updateCameraUI() {
  const camera = cameras[selectedCamera];
  document.querySelectorAll(".camera-select").forEach((button) => {
    const isSelected = Number(button.dataset.camera) === selectedCamera;
    button.classList.toggle("selected", isSelected);
    button.setAttribute("aria-pressed", String(isSelected));
    button.querySelector(".camera-mark").textContent = isSelected ? "ON AIR" : "SELECT";
  });
  document.querySelectorAll(".trigger-item").forEach((item) => item.classList.toggle("selected", Number(item.dataset.camera) === selectedCamera));
  byId("activeCameraName").textContent = `CAM ${twoDigits(selectedCamera + 1)}`;
  byId("activePage").textContent = camera ? `${camera.name} · ${camera.host || "NO ADDRESS"}`.toUpperCase() : "";
  updateViewer();
}

// Shows the selected camera's stream whenever the bridge has found one, and says why not otherwise.
function updateViewer() {
  const camera = cameras[selectedCamera];
  const stream = camera?.stream ?? {};
  // A camera's RTSP stream comes through the relay as WebRTC; Motion JPEG and stills come as a picture.
  const direct = Boolean(stream.webrtc && relay.running);
  const wanted = !stream.playable ? "" : direct ? `whep:camera${selectedCamera + 1}?stream=${stream.id}&try=${streamRetries}` : `/api/stream/${selectedCamera}?stream=${stream.id}&try=${streamRetries}`;
  const source = wanted === failedSource ? "" : wanted;
  const image = byId("viewerImage");
  if (image.dataset.source !== source) {
    image.dataset.source = source;
    stopWatching?.();
    stopWatching = null;
    image.hidden = direct;
    byId("viewerVideo").hidden = !direct || !source;
    // Changing or removing src makes the browser hang up on the stream it was showing.
    if (source && !direct) image.src = source;
    else image.removeAttribute("src");
    if (source && direct) {
      stopWatching = watchRelay(byId("viewerVideo"), `camera${selectedCamera + 1}`, () => {
        failedSource = source;
        updateViewer();
      });
    }
  }
  byId("viewer").hidden = !source;
  byId("viewerCaption").textContent = camera ? `${camera.name} · LIVE`.toUpperCase() : "";
  // The pill only appears for a stream that was there and has gone; a camera that never had one shows nothing.
  const dropped = Boolean(camera) && (stream.state === "dropped" || (wanted !== "" && wanted === failedSource));
  byId("streamPill").hidden = !dropped;
  byId("streamPillText").textContent = dropped ? `${camera.name} STREAM DROPPED`.toUpperCase() : "";
}

// Plays one of the relay's streams in a video element with WHEP, the standard way to watch a WebRTC stream.
// Returns a function that hangs up. `onLost` is called if the stream can't be had or breaks off.
function watchRelay(video, path, onLost) {
  const link = new RTCPeerConnection();
  let over = false;
  const lost = () => {
    if (over) return;
    over = true;
    link.close();
    onLost();
  };
  link.addTransceiver("video", { direction: "recvonly" });
  link.addTransceiver("audio", { direction: "recvonly" });
  link.ontrack = (event) => { video.srcObject = event.streams[0]; };
  link.onconnectionstatechange = () => { if (["failed", "disconnected"].includes(link.connectionState)) lost(); };
  (async () => {
    await link.setLocalDescription(await link.createOffer());
    const answer = await fetch(`${location.protocol}//${location.hostname}:${relay.port}/${path}/whep`, {
      method: "POST",
      headers: { "Content-Type": "application/sdp", Authorization: `Basic ${btoa(`viewer:${relay.key}`)}` },
      body: link.localDescription.sdp,
    });
    if (!answer.ok) throw new Error(`The relay returned ${answer.status}`);
    await link.setRemoteDescription({ type: "answer", sdp: await answer.text() });
  })().catch(lost);
  return () => {
    over = true;
    link.close();
    video.srcObject = null;
  };
}

// Where to watch the switcher feed: OBS's own output through the bridge's relay, or the VDO.Ninja room.
// Another device needs this computer's network address, not "127.0.0.1".
function feedLink(source, forAnotherDevice) {
  if (source === "vdo") return vdoLinks().viewer;
  const local = ["127.0.0.1", "localhost"].includes(location.hostname);
  const host = forAnotherDevice && local ? remote.addresses[0] ?? location.hostname : location.hostname;
  return `${location.protocol}//${host}:${location.port || remote.port}/feed?port=${relay.port}&key=${relay.key}`;
}

function lookForStreams(announce = true) {
  failedSource = null;
  streamRetries += 1;
  postJson("/api/streams", {}).then(refreshSession).catch(() => {});
  if (announce) setToast("Looking for video streams on the cameras");
}

// --- VDO.Ninja: the switcher feed and the crew ---

function adoptRoom(room) {
  vdo = room.vdo;
  vdoLinkSet = room.links;
  remote = room.remote;
  relay = room.relay;
  document.documentElement.classList.toggle("has-relay", relay.running);
  byId("remotePassword").value = remote.password;
  byId("remoteHelp").textContent = !remote.on
    ? "Remote access is switched off for this controller (host is 127.0.0.1 in config.json)."
    : `From another computer, open http://${remote.addresses[0] ?? "<this computer's address>"}:${remote.port} and enter this password. A browser on this computer needs none.`;
  mail = room.mail;
  for (const field of ["base", "room", "feed", "password", "codirector"]) byId(`vdo_${field}`).value = vdo[field];
  byId("googleClientId").value = mail.clientId;
  byId("googleSecret").placeholder = mail.clientReady ? "Saved. Type a new one to change it" : "";
  byId("googleStatus").textContent = mail.ready ? `Signed in as ${mail.address}. Invites are sent from this account.` : "Not signed in.";
  byId("googleSignIn").hidden = mail.ready;
  byId("googleSignOut").hidden = !mail.ready;
  if (!byId("inviteDialog").hidden) renderCrew();
}

const vdoLinks = () => vdoLinkSet;

async function saveVdo(changes) {
  const response = await postJson("/api/vdo", { vdo: { ...vdo, ...changes } });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Controller bridge returned ${response.status}`);
  adoptRoom(result);
}

function copyText(text, what) {
  const done = () => setToast(`${what} copied`);
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => setToast(text));
    return;
  }
  // Pages opened from another computer over plain http have no clipboard access; select-and-copy still works.
  const field = Object.assign(document.createElement("textarea"), { value: text });
  document.body.append(field);
  field.select();
  document.execCommand("copy");
  field.remove();
  done();
}

function openFeedBox(link) {
  byId("feedFrame").src = link;
  byId("feedBox").hidden = false;
}

function closeFeedBox() {
  byId("feedBox").hidden = true;
  // Emptying the frame hangs up on the feed.
  byId("feedFrame").removeAttribute("src");
}

function openFeedWindow(link) {
  if (!window.open(link, "ptz-switcher-feed", "popup,width=960,height=560")) setToast("The browser blocked the new window — allow pop-ups for this page", true);
}

function openDeviceDialog(link) {
  const code = qrcode(0, "M");
  code.addData(link);
  code.make();
  byId("deviceQr").innerHTML = code.createSvgTag({ cellSize: 5, margin: 3, scalable: true });
  byId("deviceLink").value = link;
  byId("deviceDialog").hidden = false;
}

// Saves the Google OAuth client from the settings boxes, if it was changed.
async function saveGoogleClient() {
  const wanted = { clientId: byId("googleClientId").value.trim(), clientSecret: byId("googleSecret").value.trim() };
  if (wanted.clientId === mail.clientId && !wanted.clientSecret) return;
  const response = await postJson("/api/google", { google: wanted });
  const result = await response.json().catch(() => ({}));
  if (!response.ok) throw new Error(result.error || `Controller bridge returned ${response.status}`);
  byId("googleSecret").value = "";
  adoptRoom(result);
}

async function googleSignIn() {
  // Google only returns a sign-in to this computer's own address, and refuses to show it inside OBS's built-in browser.
  if (inObs || !["127.0.0.1", "localhost"].includes(location.hostname)) {
    setToast("Sign in to Google from a web browser on the computer running the controller, at http://127.0.0.1:" + (location.port || "8765"), true);
    return;
  }
  try {
    await saveGoogleClient();
  } catch (error) {
    setToast(error.message, true);
    return;
  }
  if (!mail.clientReady) {
    setToast("Enter the Google client ID and secret first", true);
    return;
  }
  location.href = "/api/google/start";
}

// Emails each person their link from the Google account signed in under settings.
async function sendInvites(people) {
  if (!people.length) return;
  if (!mail.ready) {
    setToast("Sign in with Google in Settings to send invites", true);
    return;
  }
  setToast(`Sending ${people.length > 1 ? `${people.length} invites` : "the invite"}…`);
  try {
    const response = await postJson("/api/invite", { emails: people.map((person) => person.email) });
    const result = await response.json().catch(() => ({}));
    if (!response.ok) throw new Error(result.error || `Controller bridge returned ${response.status}`);
    setToast(result.sent > 1 ? `${result.sent} invites sent from ${mail.address}` : `Invite sent to ${people[0].email}`);
  } catch (error) {
    setToast(error.message, true);
  }
}

function renderCrew() {
  byId("inviteRoom").textContent = `VDO.NINJA ROOM · ${vdo.room}`.toUpperCase();
  byId("crewList").innerHTML = vdo.crew.map((person, index) => `<div class="crew-row" data-index="${index}">
    <span class="crew-email">${escapeHtml(person.email)}</span>
    <select data-role aria-label="What ${escapeHtml(person.email)} can do"><option value="viewer"${person.role === "viewer" ? " selected" : ""}>VIEW FEED</option><option value="codirector"${person.role === "codirector" ? " selected" : ""}>CO-DIRECTOR</option></select>
    <button class="quiet-button" type="button" data-send>EMAIL</button><button class="quiet-button" type="button" data-copy>COPY</button>
    <button class="remove-camera" type="button" data-drop aria-label="Remove ${escapeHtml(person.email)}">×</button>
  </div>`).join("") || `<p class="field-help">Nobody yet. Add the crew's email addresses below.</p>`;
  byId("inviteEveryone").hidden = !vdo.crew.length;
  byId("inviteFrom").textContent = mail.ready ? `Invites are emailed from ${mail.address}.` : "To email invites, sign in with Google in Settings. Until then, use Copy.";
}

function addCrew() {
  // Several addresses can be pasted at once, separated by commas, semicolons or spaces.
  const emails = byId("crewEmail").value.split(/[\s,;]+/).filter(Boolean);
  if (!emails.length) return;
  const added = emails.filter((email) => !vdo.crew.some((person) => person.email.toLowerCase() === email.toLowerCase())).map((email) => ({ email, role: byId("crewRole").value }));
  saveVdo({ crew: [...vdo.crew, ...added] }).then(() => {
    byId("crewEmail").value = "";
    if (added.length && confirm(`Send ${added.length > 1 ? `these ${added.length} people their invites` : `${added[0].email} an invite`} now?`)) sendInvites(added);
  }).catch((error) => setToast(error.message, true));
}


function streamStateText(camera) {
  const stream = camera.from === null ? null : cameras[camera.from]?.stream;
  if (!stream) return ["Save to start looking for this camera's video stream", false];
  if (stream.state === "looking") return ["Looking for a video stream…", false];
  if (stream.state !== "found") return [stream.note ?? "No video stream found", false];
  return [`${stream.kind.toUpperCase()} stream found · ${stream.address}${stream.note ? ` — ${stream.note}` : ""}`, stream.playable];
}

function renderStreamStates() {
  if (!draft) return;
  document.querySelectorAll("#cameraList .camera-row").forEach((row) => {
    const [text, showing] = streamStateText(draft[Number(row.dataset.index)]);
    const state = row.querySelector(".stream-state");
    state.textContent = text;
    state.classList.toggle("found", showing);
  });
}

function renderCameraRows() {
  byId("cameraList").innerHTML = draft.map((camera, index) => {
    const label = `Camera ${index + 1}`;
    return `<div class="camera-row" data-index="${index}">
      <span class="camera-number">${twoDigits(index + 1)}</span>
      <input data-field="name" aria-label="${label} name" maxlength="24" placeholder="Name" spellcheck="false" value="${escapeHtml(camera.name)}" />
      <input data-field="host" aria-label="${label} IP address" maxlength="253" placeholder="IP address" spellcheck="false" autocapitalize="off" value="${escapeHtml(camera.host)}" />
      <input data-field="port" aria-label="${label} VISCA port" type="number" min="1" max="65535" value="${camera.port}" />
      <button class="remove-camera" type="button" data-remove aria-label="Remove ${label}" title="Remove this camera">×</button>
      <select data-field="type" aria-label="${label} kind of camera" title="How the camera is spoken to. Sony cameras use Sony VISCA; PTZOptics and most other makes use plain VISCA over UDP or TCP.">${Object.entries(CAMERA_TYPES).map(([type, [name]]) => `<option value="${type}"${camera.type === type ? " selected" : ""}>${name}</option>`).join("")}</select>
      <button class="select-button" type="button" data-assign title="Change the button or key that selects this camera">SELECT WITH: ${escapeHtml(shortcut(camera).text)}</button>
      <input class="stream-field" data-field="streamUrl" aria-label="${label} stream address" placeholder="Stream address (optional, found automatically)" spellcheck="false" autocapitalize="off" value="${escapeHtml(camera.streamUrl)}" />
      <p class="stream-state"></p>
    </div>`;
  }).join("");
  renderStreamStates();
}

function freeButtons() {
  const pad = navigator.getGamepads?.()[activeGamepadIndex ?? -1];
  return SPARE_BUTTON_ORDER.filter((button) => (!pad || button < pad.buttons.length) && !draft.some((camera) => camera.button === button));
}

function renderAssignPrompt() {
  const camera = draft[assigning];
  const free = freeButtons();
  const key = camera.key.toUpperCase();
  let text;
  if (camera.button === null && !free.length) {
    text = "No buttons are left on the controller: every spare one already selects a camera. Use a keyboard command instead. "
      + (key ? `The ${key} key selects this camera; press another key now to change it.` : "Press the key that should select this camera.");
  } else {
    text = "Press the controller button that should select this camera."
      + (free.length ? ` Free: ${free.map((button) => SPARE_BUTTONS[button][1].toLowerCase()).join(", ")}.` : "")
      + (activeGamepadIndex === null ? " No controller is connected yet — press any button on it to wake it." : "")
      + " A keyboard key can select it as well: press one to set it.";
  }
  byId("assignTitle").textContent = `${camera.name || `Camera ${assigning + 1}`}`;
  byId("assignText").textContent = text;
  byId("assignNow").textContent = `NOW: ${shortcut(camera).text}`;
  byId("assignClear").hidden = camera.button === null;
}

function openAssignPrompt(index) {
  assigning = index;
  document.activeElement?.blur();
  renderAssignPrompt();
  byId("assignPrompt").hidden = false;
  byId("assignCard").focus();
}

function closeAssignPrompt() {
  if (assigning === null) return;
  assigning = null;
  byId("assignPrompt").hidden = true;
  if (draft) renderCameraRows();
}

// A controller button pressed while the prompt is up becomes the camera's select button, if nothing else uses it.
function assignButton(button) {
  const taken = TAKEN_BUTTONS[button];
  const other = draft.findIndex((camera, index) => camera.button === button && index !== assigning);
  if (taken) {
    setToast(`${taken[0]} already adjusts ${taken[1]} — pick another button`, true);
  } else if (other !== -1) {
    setToast(`That button already selects ${draft[other].name} — pick another`, true);
  } else {
    draft[assigning].button = button;
    setToast(`${SPARE_BUTTONS[button]?.[1] ?? `BUTTON ${button}`} will select ${draft[assigning].name}. Save settings to keep it.`);
    closeAssignPrompt();
  }
}

function assignKey(key) {
  const other = draft.findIndex((camera, index) => camera.key === key && index !== assigning);
  if (TAKEN_KEYS.includes(key)) {
    setToast(`The ${key.toUpperCase()} key already does something else — pick another`, true);
  } else if (other !== -1) {
    setToast(`The ${key.toUpperCase()} key already selects ${draft[other].name} — pick another`, true);
  } else {
    draft[assigning].key = key;
    setToast(`The ${key.toUpperCase()} key will select ${draft[assigning].name}. Save settings to keep it.`);
    closeAssignPrompt();
  }
}

function addCamera() {
  if (draft.length >= 64) {
    setToast("64 cameras is the most the controller handles", true);
    return;
  }
  const number = draft.length + 1;
  const key = number <= 9 && !draft.some((camera) => camera.key === String(number)) ? String(number) : "";
  draft.push({ name: `PTZ${number}`, type: "sony", host: "", port: 52381, streamUrl: "", button: null, key, from: null });
  renderCameraRows();
  byId("cameraList").lastElementChild.scrollIntoView({ block: "nearest" });
  openAssignPrompt(draft.length - 1);
}

async function saveSettings() {
  updateRangeLabels();
  const wanted = draft.map(cameraSettings);
  if (JSON.stringify(wanted) !== JSON.stringify(cameras.map(cameraSettings))) {
    if (wanted.some((camera) => !Number.isInteger(camera.port))) {
      setToast("Each camera's port must be a whole number", true);
      return;
    }
    stopMoving();
    let result = {};
    try {
      const response = await postJson("/api/cameras", { cameras: wanted });
      result = await response.json();
      if (!response.ok) throw new Error(result.error || `Controller bridge returned ${response.status}`);
    } catch (error) {
      setToast(error.message, true);
      return;
    }
    // The selection follows the camera, wherever it now sits in the list.
    selectedCamera = Math.max(0, draft.findIndex((camera) => camera.from === selectedCamera));
    adoptCameras(result.cameras);
  }
  try {
    await saveGoogleClient();
    const password = byId("remotePassword").value;
    if (vdo && password !== remote.password) {
      const response = await postJson("/api/password", { password });
      const result = await response.json().catch(() => ({}));
      if (!response.ok) throw new Error(result.error || `Controller bridge returned ${response.status}`);
      adoptRoom(result);
    }
  } catch (error) {
    setToast(error.message, true);
    return;
  }
  const vdoWanted = Object.fromEntries(["base", "room", "feed", "password", "codirector"].map((field) => [field, byId(`vdo_${field}`).value.trim()]));
  if (vdo && Object.entries(vdoWanted).some(([field, value]) => vdo[field] !== value)) {
    try {
      await saveVdo(vdoWanted);
    } catch (error) {
      setToast(error.message, true);
      return;
    }
  }
  saveConfig();
  setToast("Settings saved");
  openSettings(false);
}

function radialVector(x, y) {
  const magnitude = Math.hypot(x, y);
  if (magnitude <= config.deadzone) return { x: 0, y: 0, magnitude: 0 };
  const scaled = Math.min(1, (magnitude - config.deadzone) / (1 - config.deadzone));
  if (scaled < config.engage) return { x: 0, y: 0, magnitude: scaled };
  return { x: x / magnitude, y: y / magnitude, magnitude: scaled };
}

// Turns the sticks into camera speeds. The harder a stick is pushed, the faster that axis moves.
function stickDrive(pan, zoom, zoomAmount) {
  const next = { ...STILL };
  if (pan.x !== 0 || pan.y !== 0) {
    const amount = stickPush(pan.magnitude) ** PAN_CURVE * config.topSpeed;
    // Scale so the stronger axis gets the full amount; a diagonal then runs both axes at speed.
    const strongest = Math.max(Math.abs(pan.x), Math.abs(pan.y));
    const axisSpeed = (component, fastest) => {
      if (Math.abs(component) < AXIS_SNAP) return 0;
      return Math.sign(component) * Math.max(1, Math.round((Math.abs(component) / strongest) * amount * fastest));
    };
    next.pan = axisSpeed(pan.x, MAX_PAN_SPEED);
    next.tilt = axisSpeed(-pan.y, MAX_TILT_SPEED);
  }
  if (zoom !== 0) {
    next.zoom = zoom;
    next.zoomSpeed = Math.round(stickPush(zoomAmount) ** ZOOM_CURVE * config.topSpeed * MAX_ZOOM_SPEED);
  }
  return next;
}

function setVisuals(pan, zoom) {
  const panReach = pan.x || pan.y ? pan.magnitude : 0;
  const moves = [];
  if (drive.pan) moves.push(`${drive.pan > 0 ? "RIGHT" : "LEFT"} ${Math.abs(drive.pan)}`);
  if (drive.tilt) moves.push(`${drive.tilt > 0 ? "UP" : "DOWN"} ${Math.abs(drive.tilt)}`);
  byId("vectorReadout").textContent = `${(pan.x * panReach).toFixed(2)} / ${(pan.y * panReach).toFixed(2)}`;
  byId("driveReadout").textContent = moves.join(" · ") || (zoom ? `ZOOM ${zoom > 0 ? "IN" : "OUT"}` : "IDLE");
  byId("stickVisual").style.setProperty("--stick-x", `${pan.x * panReach * 31}px`);
  byId("stickVisual").style.setProperty("--stick-y", `${pan.y * panReach * 31}px`);
  byId("zoomKnob").style.setProperty("--zoom-shift", `${zoom * 60}px`);
  byId("zoomReadout").textContent = zoom ? `${zoom > 0 ? "IN" : "OUT"} · SPEED ${drive.zoomSpeed}` : "NEUTRAL";
}

// How far past the engagement point a stick is pushed, 0 (just moving) to 1 (full).
function stickPush(magnitude) {
  return Math.min(1, Math.max(0, (magnitude - config.engage) / (1 - config.engage)));
}

function updateTopSpeedUI() {
  byId("topSpeedValue").textContent = `${Math.round(config.topSpeed * 100)}%`;
}

function adjustTopSpeed(direction) {
  const index = TOP_SPEED_STEPS.findIndex((step) => step >= config.topSpeed);
  config.topSpeed = TOP_SPEED_STEPS[Math.min(TOP_SPEED_STEPS.length - 1, Math.max(0, index + direction))];
  saveConfig();
  updateTopSpeedUI();
  setToast(`Top speed ${Math.round(config.topSpeed * 100)}%`);
}

function fireMomentary(action) {
  if (!cameras.length) return;
  enqueue("/api/command", { v: CLIENT_VERSION, camera: selectedCamera, action });
}

function updateButtonAction(index, pressed, prev) {
  if (pressed === prev) return;
  if (!pressed) return;
  if (assigning !== null) {
    assignButton(index);
    return;
  }
  const camera = cameras.findIndex((candidate) => candidate.button === index);
  if (index in TOP_SPEED_BUTTONS) adjustTopSpeed(TOP_SPEED_BUTTONS[index]);
  else if (ADJUST_BUTTONS[index]) fireMomentary(ADJUST_BUTTONS[index]);
  else if (camera !== -1) selectCamera(camera);
}

function pollGamepad() {
  const pads = navigator.getGamepads?.() ?? [];
  const gamepad = activeGamepadIndex === null ? [...pads].find(Boolean) : pads[activeGamepadIndex];
  if (!gamepad) {
    if (activeGamepadIndex !== null) {
      activeGamepadIndex = null;
      stopMoving();
      previousButtons = new Map();
      byId("connectionState").classList.remove("connected");
      byId("connectionText").textContent = "Looking for controller";
      byId("gamepadName").textContent = "CONNECT CONTROLLER";
    }
    return null;
  }

  if (activeGamepadIndex !== gamepad.index) {
    activeGamepadIndex = gamepad.index;
    byId("connectionState").classList.add("connected");
    byId("connectionText").textContent = "Controller connected";
    byId("gamepadName").textContent = gamepad.id.slice(0, 42).toUpperCase();
    setToast(gamepad.mapping === "standard" ? "Controller ready" : "Controller connected, but not in the standard layout — buttons may be in different places", gamepad.mapping !== "standard");
  }

  const axes = gamepad.axes;
  const pan = radialVector(axes[2] || 0, axes[3] || 0);
  const zoomVector = radialVector(0, axes[1] || 0);
  const zoom = zoomVector.y < 0 ? 1 : zoomVector.y > 0 ? -1 : 0;
  const zoomAmount = zoomVector.magnitude;

  gamepad.buttons.forEach((button, index) => {
    const pressed = button.pressed || button.value > 0.6;
    updateButtonAction(index, pressed, previousButtons.get(index) ?? false);
    previousButtons.set(index, pressed);
  });
  return { pan, zoom, zoomAmount };
}

function pollKeyboard() {
  const x = (keyboardHeld.has("ArrowRight") ? 1 : 0) - (keyboardHeld.has("ArrowLeft") ? 1 : 0);
  const y = (keyboardHeld.has("ArrowDown") ? 1 : 0) - (keyboardHeld.has("ArrowUp") ? 1 : 0);
  const pan = radialVector(x, y);
  const zoom = keyboardHeld.has("w") ? 1 : keyboardHeld.has("s") ? -1 : 0;
  return { pan, zoom, zoomAmount: 1 };
}

function animationFrame() {
  const pad = pollGamepad();
  const keys = pollKeyboard();
  const padActive = pad && (pad.pan.x !== 0 || pad.pan.y !== 0 || pad.zoom !== 0);
  const input = padActive || !(keys.pan.x || keys.pan.y || keys.zoom) ? pad ?? keys : keys;
  drive = stickDrive(input.pan, input.zoom, input.zoomAmount);
  pumpDrive(performance.now());
  setVisuals(input.pan, input.zoom);
  rafId = requestAnimationFrame(animationFrame);
}

function openSettings(open) {
  closeAssignPrompt();
  draft = open ? cameras.map((camera, index) => ({ ...cameraSettings(camera), from: index })) : null;
  if (open) renderCameraRows();
  byId("settingsDrawer").classList.toggle("open", open);
  byId("settingsDrawer").setAttribute("aria-hidden", String(!open));
  byId("drawerScrim").classList.toggle("visible", open);
}

function updateRangeLabels() {
  config.deadzone = Number(byId("deadzoneInput").value) / 100;
  config.engage = Number(byId("engageInput").value) / 100;
  byId("deadzoneOutput").textContent = `${Math.round(config.deadzone * 100)}%`;
  byId("engageOutput").textContent = `${Math.round(config.engage * 100)}%`;
  byId("deadzoneValue").textContent = config.deadzone.toFixed(2);
}

function initialize() {
  setTheme(document.documentElement.dataset.theme === "light" ? "light" : "dark");
  document.documentElement.classList.toggle("in-obs", inObs);
  // A page opening is a new session: have the bridge look at the cameras' streams once, quietly.
  lookForStreams(false);
  refreshSession();
  setInterval(refreshSession, SESSION_REFRESH_MS);
  byId("deadzoneInput").value = String(Math.round(config.deadzone * 100));
  byId("engageInput").value = String(Math.round(config.engage * 100));
  updateRangeLabels();
  updateTopSpeedUI();
  updateCameraUI();

  byId("cameraStrip").addEventListener("click", (event) => {
    const button = event.target.closest(".camera-select");
    if (button) selectCamera(Number(button.dataset.camera));
  });
  byId("themeToggle").addEventListener("click", () => setTheme(document.documentElement.dataset.theme === "dark" ? "light" : "dark"));
  byId("darkModeInput").addEventListener("change", (event) => setTheme(event.target.checked ? "dark" : "light"));
  byId("settingsToggle").addEventListener("click", () => openSettings(true));
  byId("settingsClose").addEventListener("click", () => openSettings(false));
  byId("drawerScrim").addEventListener("click", () => openSettings(false));
  byId("deadzoneInput").addEventListener("input", updateRangeLabels);
  byId("engageInput").addEventListener("input", updateRangeLabels);

  byId("cameraList").addEventListener("input", (event) => {
    const row = event.target.closest(".camera-row");
    const field = event.target.dataset.field;
    if (!row || !field) return;
    const camera = draft[Number(row.dataset.index)];
    if (field === "type") {
      // Changing the kind of camera moves the port to that kind's usual one, unless a port of your own was typed.
      if (camera.port === CAMERA_TYPES[camera.type][1]) {
        camera.port = CAMERA_TYPES[event.target.value][1];
        row.querySelector('[data-field="port"]').value = camera.port;
      }
      camera.type = event.target.value;
    } else {
      camera[field] = field === "port" ? Number(event.target.value) : event.target.value;
    }
  });
  byId("cameraList").addEventListener("click", (event) => {
    const row = event.target.closest(".camera-row");
    if (!row) return;
    const index = Number(row.dataset.index);
    if (event.target.matches("[data-assign]")) openAssignPrompt(index);
    if (!event.target.matches("[data-remove]")) return;
    if (draft.length === 1) {
      setToast("The controller needs at least one camera", true);
      return;
    }
    draft.splice(index, 1);
    renderCameraRows();
  });
  byId("addCamera").addEventListener("click", addCamera);
  byId("lookForStreams").addEventListener("click", () => lookForStreams());
  byId("refreshStream").addEventListener("click", () => lookForStreams());
  byId("refreshStreams").addEventListener("click", () => lookForStreams());
  byId("googleSignIn").addEventListener("click", googleSignIn);
  byId("googleSignOut").addEventListener("click", () => {
    postJson("/api/google/signout", {}).then((response) => response.json()).then((result) => {
      adoptRoom(result);
      setToast("Signed out of Google");
    }).catch((error) => setToast(error.message, true));
  });
  // Back from Google: say how the sign-in went, then tidy the address.
  const googleAnswer = new URLSearchParams(location.search).get("google");
  if (googleAnswer) {
    setToast(googleAnswer, !googleAnswer.startsWith("Signed in"));
    history.replaceState(null, "", location.pathname);
  }
  document.querySelectorAll("[data-adjust]").forEach((button) => button.addEventListener("click", () => {
    fireMomentary(button.dataset.adjust);
    setToast(ADJUSTMENT_NAMES[button.dataset.adjust]);
  }));

  // VDO.Ninja
  const closeFeedMenu = () => { byId("feedMenu").hidden = true; };
  byId("feedButton").addEventListener("click", (event) => {
    event.stopPropagation();
    if (vdo) byId("feedMenu").hidden = !byId("feedMenu").hidden;
  });
  byId("feedMenu").addEventListener("click", (event) => {
    const choice = event.target.closest("[data-feed]");
    if (!choice) return;
    const link = feedLink(choice.dataset.feed, choice.dataset.where === "device");
    byId("feedBox").dataset.link = link;
    if (choice.dataset.where === "box") openFeedBox(link);
    else if (choice.dataset.where === "window") openFeedWindow(link);
    else openDeviceDialog(link);
  });
  document.addEventListener("click", closeFeedMenu);
  byId("feedClose").addEventListener("click", closeFeedBox);
  byId("feedPopOut").addEventListener("click", () => {
    closeFeedBox();
    openFeedWindow(byId("feedBox").dataset.link);
  });
  byId("feedBar").addEventListener("pointerdown", (event) => {
    if (event.target.closest("button")) return;
    // Drag the box by its bar.
    const box = byId("feedBox");
    const start = box.getBoundingClientRect();
    const [grabX, grabY] = [event.clientX - start.left, event.clientY - start.top];
    const move = (moved) => {
      box.style.left = `${Math.max(0, Math.min(window.innerWidth - 80, moved.clientX - grabX))}px`;
      box.style.top = `${Math.max(0, Math.min(window.innerHeight - 30, moved.clientY - grabY))}px`;
      box.style.right = box.style.bottom = "auto";
    };
    byId("feedBar").setPointerCapture(event.pointerId);
    byId("feedBar").addEventListener("pointermove", move);
    byId("feedBar").addEventListener("pointerup", () => byId("feedBar").removeEventListener("pointermove", move), { once: true });
  });
  byId("deviceCopy").addEventListener("click", () => copyText(byId("deviceLink").value, "Feed link"));
  byId("deviceDone").addEventListener("click", () => { byId("deviceDialog").hidden = true; });
  byId("inviteButton").addEventListener("click", () => {
    if (!vdo) return;
    renderCrew();
    byId("inviteDialog").hidden = false;
  });
  byId("inviteDone").addEventListener("click", () => { byId("inviteDialog").hidden = true; });
  byId("inviteEveryone").addEventListener("click", () => sendInvites(vdo.crew));
  byId("crewAdd").addEventListener("click", addCrew);
  byId("crewEmail").addEventListener("keydown", (event) => {
    if (event.key === "Enter") addCrew();
  });
  byId("crewList").addEventListener("click", (event) => {
    const row = event.target.closest(".crew-row");
    if (!row) return;
    const person = vdo.crew[Number(row.dataset.index)];
    if (event.target.matches("[data-send]")) sendInvites([person]);
    if (event.target.matches("[data-copy]")) copyText(vdoLinks()[person.role], `${person.role === "codirector" ? "Co-director" : "Viewer"} link`);
    if (event.target.matches("[data-drop]")) saveVdo({ crew: vdo.crew.filter((other) => other !== person) }).catch((error) => setToast(error.message, true));
  });
  byId("crewList").addEventListener("change", (event) => {
    const row = event.target.closest(".crew-row");
    if (!row || !event.target.matches("[data-role]")) return;
    const crew = vdo.crew.map((person, index) => (index === Number(row.dataset.index) ? { ...person, role: event.target.value } : person));
    saveVdo({ crew }).catch((error) => setToast(error.message, true));
  });
  document.querySelectorAll("[data-open-director]").forEach((button) => button.addEventListener("click", () => {
    if (vdo) window.open(vdoLinks().director, "ptz-director-room");
  }));
  byId("vdoNewRoom").addEventListener("click", () => {
    if (!confirm("Make a new room? Links already sent to the crew will stop working.")) return;
    // Blank names are filled in by the bridge with fresh random ones.
    saveVdo({ room: "", feed: "", password: null, codirector: "" }).then(() => setToast("New VDO.Ninja room made")).catch((error) => setToast(error.message, true));
  });
  byId("assignDone").addEventListener("click", closeAssignPrompt);
  byId("assignClear").addEventListener("click", () => {
    draft[assigning].button = null;
    renderAssignPrompt();
  });
  byId("viewerImage").addEventListener("error", () => {
    // The stream broke off. Hide it and say so; it is only tried again when refresh is pressed.
    if (!byId("viewerImage").dataset.source) return;
    failedSource = byId("viewerImage").dataset.source;
    updateViewer();
  });

  byId("saveSettings").addEventListener("click", saveSettings);

  byId("stopButton").addEventListener("click", () => {
    drive = { ...STILL };
    cameras.forEach((_, camera) => stopCamera(camera));
    setToast("All cameras stopped");
  });

  window.addEventListener("gamepadconnected", () => {});
  window.addEventListener("gamepaddisconnected", () => {
    stopMoving();
    activeGamepadIndex = null;
  });
  window.addEventListener("keydown", (event) => {
    if (assigning !== null) {
      // The prompt is waiting for a key: Escape closes it, any single character becomes the camera's select key.
      if (event.key === "Escape") closeAssignPrompt();
      else if (event.key.length === 1 && event.key !== " " && !event.metaKey && !event.ctrlKey && !event.altKey && !event.repeat) {
        event.preventDefault();
        assignKey(event.key.toLowerCase());
      }
      return;
    }
    if (event.target instanceof HTMLInputElement || event.target instanceof HTMLTextAreaElement) return;
    if (["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight", " "].includes(event.key)) event.preventDefault();
    if (event.repeat) return;
    const key = event.key.toLowerCase();
    keyboardHeld.add(event.key.startsWith("Arrow") ? event.key : key);
    const momentary = { a: "red_down", b: "red_up", x: "blue_down", y: "blue_up", i: "brightness_up", k: "brightness_down" };
    if (key === "[" || key === "]") adjustTopSpeed(key === "[" ? -1 : 1);
    if (momentary[key]) fireMomentary(momentary[key]);
    if (key.length === 1) selectCamera(cameras.findIndex((camera) => camera.key === key));
    if (key === "escape") openSettings(false);
  });
  window.addEventListener("keyup", (event) => keyboardHeld.delete(event.key.startsWith("Arrow") ? event.key : event.key.toLowerCase()));
  window.addEventListener("blur", () => {
    keyboardHeld.clear();
    stopMoving();
  });
  document.addEventListener("visibilitychange", () => {
    if (document.hidden) stopMoving(true);
  });
  window.addEventListener("beforeunload", () => stopMoving(true));
  if (rafId !== null) cancelAnimationFrame(rafId);
  rafId = requestAnimationFrame(animationFrame);
}

initialize();