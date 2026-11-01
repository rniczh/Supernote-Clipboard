#!/bin/bash
# Downloads a JDK 17 and the Android SDK pieces needed to package the plugin
# into ./.toolchain, so nothing is installed system-wide. macOS only.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$ROOT/.toolchain"
ARCH="$(uname -m)"
[[ "$ARCH" == "arm64" ]] && JDK_ARCH=aarch64 || JDK_ARCH=x64

mkdir -p "$T"
cd "$T"

if [[ ! -d jdk ]]; then
  echo "Downloading JDK 17..."
  curl -fsSL -o jdk.tar.gz "https://api.adoptium.net/v3/binary/latest/17/ga/mac/$JDK_ARCH/jdk/hotspot/normal/eclipse"
  mkdir -p jdk && tar -xzf jdk.tar.gz -C jdk --strip-components=1 && rm jdk.tar.gz
fi
export JAVA_HOME="$T/jdk/Contents/Home"

if [[ ! -d android-sdk/cmdline-tools/latest ]]; then
  echo "Downloading Android command-line tools..."
  curl -fsSL -o cmdtools.zip "https://dl.google.com/android/repository/commandlinetools-mac-13114758_latest.zip"
  mkdir -p android-sdk/cmdline-tools
  unzip -q cmdtools.zip -d android-sdk/cmdline-tools
  mv android-sdk/cmdline-tools/cmdline-tools android-sdk/cmdline-tools/latest
  rm cmdtools.zip
fi

SDKMANAGER="android-sdk/cmdline-tools/latest/bin/sdkmanager --sdk_root=$T/android-sdk"
yes | $SDKMANAGER --licenses >/dev/null
$SDKMANAGER "platforms;android-35" "build-tools;35.0.0"
echo "Toolchain ready in $T"
