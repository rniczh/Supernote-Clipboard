#!/bin/bash
# Builds "Supernote Clipboard.app" into dist/. With --install, also replaces the copy in /Applications.
# Run it again after editing sn_clipboard.py or SupernoteClipboard.swift.
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
DESKTOP="$(dirname "$HERE")"
ROOT="$(dirname "$DESKTOP")"
APP_NAME="Supernote Clipboard"
OUT="$ROOT/dist/$APP_NAME.app"
WORK="$(mktemp -d)"
trap 'rm -rf "$WORK"' EXIT

VERSION="$(sed -n 's/^VERSION = "\(.*\)"$/\1/p' "$DESKTOP/sn_clipboard.py")"
export CLANG_MODULE_CACHE_PATH="$WORK/module-cache"

echo "Building $APP_NAME $VERSION"
rm -rf "$OUT"
mkdir -p "$OUT/Contents/MacOS" "$OUT/Contents/Resources"

for arch in arm64 x86_64; do
  xcrun swiftc -O -target "$arch-apple-macos13.0" -module-cache-path "$WORK/module-cache" \
    "$HERE/SupernoteClipboard.swift" -o "$WORK/SupernoteClipboard-$arch"
done
lipo -create "$WORK/SupernoteClipboard-arm64" "$WORK/SupernoteClipboard-x86_64" \
  -output "$OUT/Contents/MacOS/SupernoteClipboard"
cp "$DESKTOP/sn_clipboard.py" "$OUT/Contents/Resources/"
sed "s/__VERSION__/$VERSION/g" "$HERE/Info.plist" > "$OUT/Contents/Info.plist"

xcrun swiftc -O -module-cache-path "$WORK/module-cache" "$HERE/make_icon.swift" -o "$WORK/make_icon"
"$WORK/make_icon" "$WORK/icon.png"
ICONSET="$WORK/AppIcon.iconset"
mkdir "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$WORK/icon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  sips -z $((size * 2)) $((size * 2)) "$WORK/icon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$OUT/Contents/Resources/AppIcon.icns"

# Sign with a local self-signed certificate rather than ad hoc: macOS ties the Screen Recording
# permission to the signature, and an ad hoc signature changes with every build.
SIGN_NAME="Supernote Clipboard Local Signing"
SIGN_DIR="$HOME/Library/Application Support/Supernote Clipboard"
SIGN_KEYCHAIN="$SIGN_DIR/signing.keychain-db"
SIGN_PASSWORD_FILE="$SIGN_DIR/signing.keychain-password"

if [[ ! -f "$SIGN_KEYCHAIN" ]]; then
  echo "Creating the local signing certificate (once)"
  mkdir -p "$SIGN_DIR"
  (umask 077 && /usr/bin/openssl rand -hex 24 > "$SIGN_PASSWORD_FILE")
fi
SIGN_PASSWORD="$(cat "$SIGN_PASSWORD_FILE" 2>/dev/null || echo supernote-clipboard)"

if [[ ! -f "$SIGN_KEYCHAIN" ]]; then
  cat > "$WORK/cert.cnf" <<EOF
[req]
distinguished_name = dn
x509_extensions = ext
prompt = no
[dn]
CN = $SIGN_NAME
[ext]
basicConstraints = critical, CA:false
keyUsage = critical, digitalSignature
extendedKeyUsage = critical, codeSigning
EOF
  /usr/bin/openssl req -x509 -newkey rsa:2048 -nodes -days 36500 -config "$WORK/cert.cnf" \
    -keyout "$WORK/key.pem" -out "$WORK/cert.pem" 2>/dev/null
  /usr/bin/openssl pkcs12 -export -inkey "$WORK/key.pem" -in "$WORK/cert.pem" -name "$SIGN_NAME" \
    -out "$WORK/id.p12" -passout "pass:$SIGN_PASSWORD"
  security create-keychain -p "$SIGN_PASSWORD" "$SIGN_KEYCHAIN"
  security set-keychain-settings "$SIGN_KEYCHAIN"
  security unlock-keychain -p "$SIGN_PASSWORD" "$SIGN_KEYCHAIN"
  security import "$WORK/id.p12" -k "$SIGN_KEYCHAIN" -P "$SIGN_PASSWORD" -T /usr/bin/codesign >/dev/null
  security set-key-partition-list -S apple-tool:,apple:,codesign: -s -k "$SIGN_PASSWORD" "$SIGN_KEYCHAIN" >/dev/null
fi
security unlock-keychain -p "$SIGN_PASSWORD" "$SIGN_KEYCHAIN"
SIGN_HASH="$(security find-identity -p codesigning "$SIGN_KEYCHAIN" | awk '/"'"$SIGN_NAME"'"/ {print $2; exit}')"

# codesign only finds identities in the keychain search list, so add ours just for this command.
SEARCH_LIST=()
while IFS= read -r keychain; do
  SEARCH_LIST+=("$(echo "$keychain" | sed 's/^ *"//; s/"$//')")
done < <(security list-keychains -d user)
security list-keychains -d user -s "${SEARCH_LIST[@]}" "$SIGN_KEYCHAIN"
codesign --force --sign "$SIGN_HASH" "$OUT" || STATUS=$?
security list-keychains -d user -s "${SEARCH_LIST[@]}"
if [[ -n "${STATUS:-}" ]]; then
  echo "Signing failed; using an ad hoc signature (Screen Recording must be re-allowed after each build)."
  codesign --force --sign - "$OUT"
fi
echo "Built: $OUT"

if [[ "${1:-}" == "--install" ]]; then
  osascript -e 'if application id "com.rnic.supernote-clipboard" is running then tell application id "com.rnic.supernote-clipboard" to quit' >/dev/null 2>&1 || true
  sleep 1
  rm -rf "/Applications/$APP_NAME.app"
  cp -R "$OUT" /Applications/
  echo "Installed: /Applications/$APP_NAME.app"
fi
