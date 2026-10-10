#!/bin/sh
# Builds ptz-field-controller.plugin for macOS with the command line tools alone (no Xcode, no CMake).
#
#   obs-plugin/build_macos.sh            build into dist/
#   obs-plugin/build_macos.sh --install  build, then copy into OBS's plugins folder (quit OBS first)
#
# It needs the headers OBS itself was built from, fetched once into .build/:
#   .build/obs-studio  the obs-studio source at the tag of the installed OBS (libobs and frontend/api)
#   .build/qt6         OBS's prebuilt Qt 6 (obs-deps release named in obs-studio's CMakePresets.json)
#   .build/simde       simd-everywhere/simde, which libobs's headers use on Apple silicon
# The plugin links against the copies of libobs and Qt inside OBS.app.
set -eu
cd "$(dirname "$0")/.."

OBS_APP="${OBS_APP:-/Applications/OBS.app}"
DEPS=.build
OUT=dist/ptz-field-controller.plugin
ARCH="${ARCH:-$(uname -m)}"

# libobs expects a generated obsconfig.h; this is all a plugin needs from it.
mkdir -p "$DEPS/generated"
printf '#pragma once\n#define OBS_RELEASE_CANDIDATE 0\n#define OBS_BETA 0\n' > "$DEPS/generated/obsconfig.h"

rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"

clang++ -std=c++17 -arch "$ARCH" -mmacosx-version-min=13.0 -O2 -fPIC -fvisibility=hidden -Wall \
  -DQT_NO_DEBUG \
  -I"$DEPS/generated" -I"$DEPS/simde" -I"$DEPS/obs-studio/libobs" -I"$DEPS/obs-studio/frontend/api" \
  -F"$DEPS/qt6/lib" \
  -I"$DEPS/qt6/lib/QtCore.framework/Headers" -I"$DEPS/qt6/lib/QtGui.framework/Headers" \
  -I"$DEPS/qt6/lib/QtWidgets.framework/Headers" -I"$DEPS/qt6/lib/QtNetwork.framework/Headers" \
  -c obs-plugin/src/plugin.cpp -o "$DEPS/plugin.o"

clang++ -arch "$ARCH" -mmacosx-version-min=13.0 -bundle \
  -F"$OBS_APP/Contents/Frameworks" -framework libobs \
  -framework QtCore -framework QtGui -framework QtWidgets -framework QtNetwork \
  "$OBS_APP/Contents/Frameworks/obs-frontend-api.dylib" \
  "$DEPS/plugin.o" -o "$OUT/Contents/MacOS/ptz-field-controller"

cp obs-plugin/Info.plist "$OUT/Contents/Info.plist"
cp server.py index.html app.css app.js qrcode.js LICENSE "$OUT/Contents/Resources/"
codesign --force --sign - "$OUT"
echo "Built $OUT"

if [ "${1:-}" = "--install" ]; then
  if pgrep -x OBS >/dev/null; then echo "Quit OBS first, then run this again." >&2; exit 1; fi
  PLUGINS="$HOME/Library/Application Support/obs-studio/plugins"
  mkdir -p "$PLUGINS"
  rm -rf "$PLUGINS/ptz-field-controller.plugin"
  cp -R "$OUT" "$PLUGINS/"
  echo "Installed. Open OBS and tick Docks > PTZ Field Controller."
fi
