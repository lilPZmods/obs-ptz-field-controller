// PTZ Field Controller for OBS Studio.
// Adds the controller to OBS as a dock and runs its bridge (server.py) while OBS is open.
//
// SPDX-License-Identifier: GPL-2.0-or-later

#include <obs-module.h>
#include <obs.hpp>
#include <obs-frontend-api.h>

#include <QAction>
#include <QCoreApplication>
#include <QDockWidget>
#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include <QTcpSocket>
#include <QTimer>
#include <QVBoxLayout>
#include <QWidget>

// The interface obs-browser offers for putting a browser in a dock, copied from obs-browser/panel.
#include "browser-panel.hpp"

OBS_DECLARE_MODULE()

MODULE_EXPORT const char *obs_module_name(void)
{
	return "PTZ Field Controller";
}

MODULE_EXPORT const char *obs_module_description(void)
{
	return "Drive PTZ cameras from a game controller, in a dock.";
}

static const char *DOCK_ID = "ptz-field-controller";
static const int DEFAULT_PORT = 8765;

static QProcess *bridge = nullptr;
static QCef *cef = nullptr;
static QCefWidget *browser = nullptr;

static QString configPath()
{
	BPtr<char> path = obs_module_config_path("config.json");
	return QString::fromUtf8(path.Get());
}

// The port the bridge serves on: whatever its settings file says, or the default before it has one.
static int bridgePort()
{
	QFile file(configPath());
	if (!file.open(QIODevice::ReadOnly))
		return DEFAULT_PORT;
	return QJsonDocument::fromJson(file.readAll()).object().value("port").toInt(DEFAULT_PORT);
}

static bool bridgeAnswers(int port, int waitMs)
{
	QTcpSocket socket;
	socket.connectToHost("127.0.0.1", static_cast<quint16>(port));
	return socket.waitForConnected(waitMs);
}

// Starts the bridge unless one is already running (as a login item, say), in which case that one is used.
static void startBridge(int port)
{
	if (bridgeAnswers(port, 300)) {
		blog(LOG_INFO, "[ptz-field-controller] using the bridge already running on port %d", port);
		return;
	}

	// The plugin carries the bridge as a ready-to-run program, so nothing else has to be installed.
	// A build without it falls back to running server.py with the computer's own Python.
	QString program;
	QStringList arguments;
	BPtr<char> packaged = obs_module_file("ptz-bridge/ptz-bridge");
	if (packaged) {
		program = QString::fromUtf8(packaged.Get());
	} else {
		BPtr<char> script = obs_module_file("server.py");
		if (!script) {
			blog(LOG_WARNING, "[ptz-field-controller] the bridge is missing from the plugin");
			return;
		}
		program = QStandardPaths::findExecutable("python3");
		if (program.isEmpty())
			program = QStandardPaths::findExecutable("python");
		if (program.isEmpty()) {
			blog(LOG_WARNING, "[ptz-field-controller] Python 3 was not found, so the bridge was not started");
			return;
		}
		arguments << QString::fromUtf8(script.Get());
	}

	BPtr<char> folder = obs_module_config_path("");
	os_mkdirs(folder.Get());
	// Settings live in OBS's own plugin settings folder, which stays put when the plugin is updated.
	QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
	environment.insert("PTZ_CONFIG", configPath());

	bridge = new QProcess();
	bridge->setProcessEnvironment(environment);
	bridge->setProcessChannelMode(QProcess::ForwardedChannels);
	bridge->start(program, arguments);
	if (!bridge->waitForStarted(3000)) {
		blog(LOG_WARNING, "[ptz-field-controller] could not start the bridge with %s", program.toUtf8().constData());
		delete bridge;
		bridge = nullptr;
		return;
	}
	// Give it a moment to start serving, so the dock's first page load finds it.
	for (int tries = 0; tries < 60 && !bridgeAnswers(bridgePort(), 100); tries++)
		os_sleep_ms(100);
	blog(LOG_INFO, "[ptz-field-controller] bridge started with %s", program.toUtf8().constData());
}

static void stopBridge()
{
	if (!bridge)
		return;
	// A polite stop lets the bridge halt every camera before it goes.
	bridge->terminate();
	if (!bridge->waitForFinished(3000))
		bridge->kill();
	delete bridge;
	bridge = nullptr;
}

// ---------------------------------------------------------------------------------------------
// Sending OBS's program picture to the bridge's video relay
//
// The relay hands video to viewers over WebRTC without touching it, so something has to give it
// video WebRTC can carry: H.264 with no B-frames, and Opus sound. OBS's own stream is rarely that
// (B-frames and AAC are the usual settings, and it only exists while streaming), so the plugin runs
// a small output of its own beside it: the Mac's hardware encoder at 720p, sent by WHIP. OBS's
// stream and recording are left exactly as they are.
// ---------------------------------------------------------------------------------------------

static obs_output_t *relayOutput = nullptr;
static obs_service_t *relayService = nullptr;
static obs_encoder_t *relayVideo = nullptr;
static obs_encoder_t *relayAudio = nullptr;
static int relayPort = 0;
static bool relayWanted = true;
static bool relayClosing = false;
static bool relaySaidWhy = false;

static void tryRelayOutput();

static QString relayOffMarker()
{
	BPtr<char> path = obs_module_config_path("relay-output-off");
	return QString::fromUtf8(path.Get());
}

// The port the bridge's relay takes WHIP on, or 0 when the bridge or its relay isn't running.
static int askBridgeForRelayPort()
{
	QTcpSocket socket;
	socket.connectToHost("127.0.0.1", static_cast<quint16>(bridgePort()));
	if (!socket.waitForConnected(500))
		return 0;
	socket.write("GET /api/session HTTP/1.0\r\nHost: 127.0.0.1\r\n\r\n");
	QByteArray reply;
	while (socket.waitForReadyRead(1500))
		reply += socket.readAll();
	reply += socket.readAll();
	const QJsonObject relay = QJsonDocument::fromJson(reply.mid(reply.indexOf("\r\n\r\n") + 4)).object().value("relay").toObject();
	return relay.value("running").toBool() ? relay.value("port").toInt() : 0;
}

static void releaseRelayOutput()
{
	if (relayOutput) {
		obs_output_force_stop(relayOutput);
		obs_output_release(relayOutput);
	}
	obs_encoder_release(relayVideo);
	obs_encoder_release(relayAudio);
	obs_service_release(relayService);
	relayOutput = nullptr;
	relayVideo = relayAudio = nullptr;
	relayService = nullptr;
}

static void retryRelayOutputIn(int milliseconds)
{
	if (!relayClosing && relayWanted)
		QTimer::singleShot(milliseconds, qApp, &tryRelayOutput);
}

// Called on one of OBS's own threads when the output stops for any reason (the relay restarted, say).
static void onRelayOutputStopped(void *, calldata_t *)
{
	obs_queue_task(OBS_TASK_UI, [](void *) { retryRelayOutputIn(5000); }, nullptr, false);
}

static bool makeRelayOutput(int port)
{
	OBSDataAutoRelease serviceSettings = obs_data_create();
	obs_data_set_string(serviceSettings, "server", ("http://127.0.0.1:" + std::to_string(port) + "/program/whip").c_str());
	obs_data_set_string(serviceSettings, "bearer_token", "");
	relayService = obs_service_create("whip_custom", "ptz-relay-service", serviceSettings, nullptr);
	relayOutput = obs_output_create("whip_output", "ptz-relay-output", nullptr, nullptr);
	if (!relayService || !relayOutput) {
		blog(LOG_WARNING, "[ptz-field-controller] this OBS has no WebRTC output, so the program picture can't be sent to the relay");
		return false;
	}

	// The hardware encoder where there is one (it costs next to nothing), x264 otherwise. Either way: no B-frames.
	const bool hardware = obs_get_encoder_codec("com.apple.videotoolbox.videoencoder.ave.avc") != nullptr;
	OBSDataAutoRelease videoSettings = obs_data_create();
	obs_data_set_int(videoSettings, "bitrate", 3000);
	obs_data_set_string(videoSettings, "rate_control", "CBR");
	obs_data_set_int(videoSettings, "keyint_sec", 1);
	obs_data_set_string(videoSettings, "profile", hardware ? "main" : "baseline");
	obs_data_set_bool(videoSettings, "bframes", false);
	obs_data_set_int(videoSettings, "bf", 0);
	obs_data_set_string(videoSettings, "preset", "veryfast");
	obs_data_set_string(videoSettings, "tune", "zerolatency");
	relayVideo = obs_video_encoder_create(hardware ? "com.apple.videotoolbox.videoencoder.ave.avc" : "obs_x264", "ptz-relay-video", videoSettings, nullptr);

	OBSDataAutoRelease audioSettings = obs_data_create();
	obs_data_set_int(audioSettings, "bitrate", 96);
	relayAudio = obs_audio_encoder_create("ffmpeg_opus", "ptz-relay-audio", audioSettings, 0, nullptr);
	if (!relayVideo || !relayAudio) {
		blog(LOG_WARNING, "[ptz-field-controller] the encoders for the relay could not be made");
		return false;
	}

	// 720p is plenty for framing shots, and keeps the load and the bandwidth small.
	struct obs_video_info video;
	if (obs_get_video_info(&video) && video.output_height > 720) {
		const uint32_t width = (video.output_width * 720 / video.output_height) & ~1u;
		obs_encoder_set_scaled_size(relayVideo, width, 720);
	}
	obs_encoder_set_video(relayVideo, obs_get_video());
	obs_encoder_set_audio(relayAudio, obs_get_audio());
	obs_output_set_service(relayOutput, relayService);
	obs_output_set_video_encoder(relayOutput, relayVideo);
	obs_output_set_audio_encoder(relayOutput, relayAudio, 0);
	signal_handler_connect(obs_output_get_signal_handler(relayOutput), "stop", onRelayOutputStopped, nullptr);
	relayPort = port;
	return true;
}

// Starts sending if it is wanted and not already under way; otherwise arranges to look again shortly.
static void tryRelayOutput()
{
	if (relayClosing || !relayWanted || (relayOutput && obs_output_active(relayOutput)))
		return;
	const int port = askBridgeForRelayPort();
	if (port == 0) {
		if (!relaySaidWhy)
			blog(LOG_INFO, "[ptz-field-controller] the video relay isn't running yet; will keep looking");
		relaySaidWhy = true;
		retryRelayOutputIn(10000);
		return;
	}
	if (relayOutput && port != relayPort)
		releaseRelayOutput();
	if (!relayOutput && !makeRelayOutput(port)) {
		releaseRelayOutput();
		relayWanted = false;
		return;
	}
	if (obs_output_start(relayOutput)) {
		blog(LOG_INFO, "[ptz-field-controller] sending the program picture to the relay on port %d", port);
		relaySaidWhy = false;
	} else {
		if (!relaySaidWhy)
			blog(LOG_WARNING, "[ptz-field-controller] could not start sending to the relay: %s", obs_output_get_last_error(relayOutput));
		relaySaidWhy = true;
		retryRelayOutputIn(10000);
	}
}

static void setUpRelayOutput()
{
	relayWanted = !os_file_exists(relayOffMarker().toUtf8().constData());
	QAction *action = static_cast<QAction *>(obs_frontend_add_tools_menu_qaction("PTZ Field: send program video to the relay"));
	action->setCheckable(true);
	action->setChecked(relayWanted);
	QObject::connect(action, &QAction::toggled, [](bool on) {
		relayWanted = on;
		// Remembered as a marker file, so the choice outlasts OBS being closed.
		BPtr<char> folder = obs_module_config_path("");
		os_mkdirs(folder.Get());
		if (on) {
			os_unlink(relayOffMarker().toUtf8().constData());
			relaySaidWhy = false;
			tryRelayOutput();
		} else {
			os_quick_write_utf8_file(relayOffMarker().toUtf8().constData(), "1", 1, false);
			if (relayOutput)
				obs_output_stop(relayOutput);
		}
	});
	tryRelayOutput();
}

static void addDock()
{
	startBridge(bridgePort());
	// "obs" tells the page it is docked in OBS, where the switcher feed viewer is left to its own plugin.
	const std::string url = "http://127.0.0.1:" + std::to_string(bridgePort()) + "/?obs=1";

	QWidget *panel = new QWidget();
	QVBoxLayout *layout = new QVBoxLayout(panel);
	layout->setContentsMargins(0, 0, 0, 0);

	cef = obs_browser_init_panel();
	if (cef) {
		cef->init_browser();
		browser = cef->create_widget(panel, url);
		layout->addWidget(browser);
	} else {
		QLabel *label = new QLabel(QString("OBS was built without its browser, so the controller can't be shown here.\nOpen %1 in a web browser instead.")
						   .arg(QString::fromStdString(url)));
		label->setAlignment(Qt::AlignCenter);
		label->setWordWrap(true);
		layout->addWidget(label);
	}
	panel->setMinimumSize(280, 200);
	obs_frontend_add_dock_by_id(DOCK_ID, "PTZ Field Controller", panel);

	// The first time the plugin runs, show the dock, so nobody has to go looking for it in the Docks menu.
	// After that OBS remembers where it was put, and whether it was closed.
	BPtr<char> shownMarker = obs_module_config_path("dock-shown");
	if (!os_file_exists(shownMarker.Get())) {
		for (QWidget *parent = panel->parentWidget(); parent; parent = parent->parentWidget()) {
			if (QDockWidget *dock = qobject_cast<QDockWidget *>(parent)) {
				dock->setFloating(true);
				dock->resize(460, 760);
				dock->setVisible(true);
				break;
			}
		}
		BPtr<char> folder = obs_module_config_path("");
		os_mkdirs(folder.Get());
		os_quick_write_utf8_file(shownMarker.Get(), "1", 1, false);
	}
	blog(LOG_INFO, "[ptz-field-controller] dock added for %s", url.c_str());
}

// The page has to be closed while OBS's browser is still running. Left until OBS tears the window
// down, it is closed after the browser has gone, which crashes OBS on the way out.
static void removeDock()
{
	if (browser) {
		browser->closeBrowser();
		browser = nullptr;
	}
	obs_frontend_remove_dock(DOCK_ID);
}

static void onFrontendEvent(enum obs_frontend_event event, void *)
{
	// The dock is made once OBS has finished loading, by which time the browser plugin is ready.
	if (event == OBS_FRONTEND_EVENT_FINISHED_LOADING) {
		addDock();
		setUpRelayOutput();
	} else if (event == OBS_FRONTEND_EVENT_SCRIPTING_SHUTDOWN || event == OBS_FRONTEND_EVENT_EXIT) {
		// Whichever comes first as OBS quits; the second finds nothing left to do.
		if (browser || bridge)
			blog(LOG_INFO, "[ptz-field-controller] closing");
		relayClosing = true;
		if (relayOutput)
			signal_handler_disconnect(obs_output_get_signal_handler(relayOutput), "stop", onRelayOutputStopped, nullptr);
		releaseRelayOutput();
		removeDock();
		stopBridge();
	}
}

bool obs_module_load(void)
{
	obs_frontend_add_event_callback(onFrontendEvent, nullptr);
	return true;
}

void obs_module_unload(void)
{
	stopBridge();
}
