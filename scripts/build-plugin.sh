#!/bin/bash
# Builds the Supernote plugin package and copies it to dist/PCClipboard.snplg.
# Uses ./.toolchain (see setup-toolchain.sh) when present, otherwise your own
# JAVA_HOME / ANDROID_HOME.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
T="$ROOT/.toolchain"
if [[ -d "$T/jdk" ]]; then
  export JAVA_HOME="$T/jdk/Contents/Home"
  export ANDROID_HOME="$T/android-sdk"
  export GRADLE_USER_HOME="$T/gradle"
  export PATH="$JAVA_HOME/bin:$PATH"
fi

cd "$ROOT/supernote-plugin"
[[ -d node_modules ]] || npm install
npx tsc --noEmit
./buildPlugin.sh
rm -f android_project_react_packages.txt

mkdir -p "$ROOT/dist"
cp build/outputs/PCClipboard.snplg "$ROOT/dist/PCClipboard.snplg"
echo "Plugin package: $ROOT/dist/PCClipboard.snplg"
