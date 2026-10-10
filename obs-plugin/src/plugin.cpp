// PTZ Field Controller for OBS Studio.
// Adds the controller to OBS as a dock and runs its bridge (server.py) while OBS is open.
//
// SPDX-License-Identifier: GPL-2.0-or-later

#include <obs-module.h>
#include <obs-frontend-api.h>

#include <QFile>
#include <QJsonDocument>
#include <QJsonObject>
#include <QLabel>
#include <QProcess>
#include <QProcessEnvironment>
#include <QStandardPaths>
#include <QTcpSocket>
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

	BPtr<char> script = obs_module_file("server.py");
	if (!script) {
		blog(LOG_WARNING, "[ptz-field-controller] server.py is missing from the plugin");
		return;
	}
	QString python = QStandardPaths::findExecutable("python3");
	if (python.isEmpty())
		python = QStandardPaths::findExecutable("python");
#ifdef __APPLE__
	if (python.isEmpty())
		python = "/usr/bin/python3";
#endif
	if (python.isEmpty()) {
		blog(LOG_WARNING, "[ptz-field-controller] Python 3 was not found, so the bridge was not started");
		return;
	}

	BPtr<char> folder = obs_module_config_path("");
	os_mkdirs(folder.Get());
	// Settings live in OBS's own plugin settings folder, which stays put when the plugin is updated.
	QProcessEnvironment environment = QProcessEnvironment::systemEnvironment();
	environment.insert("PTZ_CONFIG", configPath());

	bridge = new QProcess();
	bridge->setProcessEnvironment(environment);
	bridge->setProcessChannelMode(QProcess::ForwardedChannels);
	bridge->start(python, {QString::fromUtf8(script.Get())});
	if (!bridge->waitForStarted(3000)) {
		blog(LOG_WARNING, "[ptz-field-controller] could not start the bridge with %s", python.toUtf8().constData());
		delete bridge;
		bridge = nullptr;
		return;
	}
	// Give it a moment to start serving, so the dock's first page load finds it.
	for (int tries = 0; tries < 30 && !bridgeAnswers(bridgePort(), 100); tries++)
		os_sleep_ms(100);
	blog(LOG_INFO, "[ptz-field-controller] bridge started with %s", python.toUtf8().constData());
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
		layout->addWidget(cef->create_widget(panel, url));
	} else {
		QLabel *label = new QLabel(QString("OBS was built without its browser, so the controller can't be shown here.\nOpen %1 in a web browser instead.")
						   .arg(QString::fromStdString(url)));
		label->setAlignment(Qt::AlignCenter);
		label->setWordWrap(true);
		layout->addWidget(label);
	}
	panel->setMinimumSize(280, 200);
	obs_frontend_add_dock_by_id(DOCK_ID, "PTZ Field Controller", panel);
	blog(LOG_INFO, "[ptz-field-controller] dock added for %s", url.c_str());
}

static void onFrontendEvent(enum obs_frontend_event event, void *)
{
	// The dock is made once OBS has finished loading, by which time the browser plugin is ready.
	if (event == OBS_FRONTEND_EVENT_FINISHED_LOADING)
		addDock();
	else if (event == OBS_FRONTEND_EVENT_EXIT)
		stopBridge();
}

bool obs_module_load(void)
{
	obs_frontend_add_event_callback(onFrontendEvent, nullptr);
	return true;
}

void obs_module_unload(void)
{
	obs_frontend_remove_event_callback(onFrontendEvent, nullptr);
	stopBridge();
}
