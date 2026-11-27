#!/bin/bash
# Builds "Agentic Spend.app" (universal arm64 + x86_64, macOS 13+), signs it
# ad hoc, and zips it for a GitHub release. Needs only the Xcode command line
# tools (swiftc, lipo, codesign). Not notarised: users approve it once in
# System Settings > Privacy & Security > Open Anyway.
#
#   macos/build.sh 1.5.0
set -euo pipefail
VERSION="${1:?usage: macos/build.sh VERSION}"
HERE="$(cd "$(dirname "$0")" && pwd)"
OUT="$HERE/out"
APP="$OUT/Agentic Spend.app"
SDK="$(xcrun --sdk macosx --show-sdk-path)"
rm -rf "$OUT"
mkdir -p "$OUT"

for arch in arm64 x86_64; do
  xcrun swiftc -swift-version 5 -O -parse-as-library \
    -sdk "$SDK" -target "${arch}-apple-macos13.0" \
    "$HERE"/Sources/*.swift -o "$OUT/AgenticSpend-$arch"
done
lipo -create -output "$OUT/AgenticSpend" "$OUT/AgenticSpend-arm64" "$OUT/AgenticSpend-x86_64"

mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$OUT/AgenticSpend" "$APP/Contents/MacOS/AgenticSpend"
sed "s/__VERSION__/$VERSION/g" "$HERE/Resources/Info.plist" > "$APP/Contents/Info.plist"
printf 'APPL????' > "$APP/Contents/PkgInfo"

# App icon from the dashboard favicon.
ICONSET="$OUT/AppIcon.iconset"
mkdir -p "$ICONSET"
for size in 16 32 128 256 512; do
  sips -z "$size" "$size" "$HERE/../favicon.png" --out "$ICONSET/icon_${size}x${size}.png" >/dev/null
  double=$((size * 2))
  sips -z "$double" "$double" "$HERE/../favicon.png" --out "$ICONSET/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# Fail the build if the menu bar label would be empty or zero width, or the
# bundle lost LSUIElement / its identifier (1.8.2 regression guard).
"$APP/Contents/MacOS/AgenticSpend" --self-test

# Signing: ad hoc by default (CI and releases). For your own dev builds you can
# use a self-signed code-signing certificate from your login keychain instead:
# set AGENTIC_SPEND_SIGN_IDENTITY, or name the certificate "Agentic Spend Local".
# Optional; releases never depend on it, and it does not change Gatekeeper or
# the menu bar (macOS keys menu bar items by bundle id, not by signature).
SIGN_ID="${AGENTIC_SPEND_SIGN_IDENTITY:-}"
if [[ -z "$SIGN_ID" ]] && security find-identity -v -p codesigning 2>/dev/null | grep -q '"Agentic Spend Local"'; then
  SIGN_ID="Agentic Spend Local"
fi
if [[ -n "$SIGN_ID" ]]; then
  echo "signing with identity: $SIGN_ID"
  codesign --force --deep --sign "$SIGN_ID" --timestamp=none "$APP"
else
  codesign --force --deep --sign - --timestamp=none "$APP"
fi
codesign --verify --deep --strict --verbose=2 "$APP"
lipo -archs "$APP/Contents/MacOS/AgenticSpend"

ZIP="$OUT/Agentic-Spend-$VERSION.zip"
ditto -c -k --sequesterRsrc --keepParent "$APP" "$ZIP"
shasum -a 256 "$ZIP"
