#!/bin/bash
# Put ccusage's native Linux binary in vendor/ccusage, the place the Homebrew
# formula fills on macOS. The version is the one ccusage_bin.py expects, and
# the download is checked against the hash the npm registry publishes for it.
# Run again after an upgrade that changes CCUSAGE_VERSION.
set -euo pipefail

ROOT="$(dirname "$(dirname "$(readlink -f "${BASH_SOURCE[0]}")")")"
VERSION="$(sed -n 's/^CCUSAGE_VERSION = "\(.*\)"$/\1/p' "$ROOT/ccusage_bin.py")"
[[ -n "$VERSION" ]] || { echo "could not read CCUSAGE_VERSION from ccusage_bin.py" >&2; exit 1; }
case "$(uname -m)" in
  x86_64) ARCH=x64 ;;
  aarch64|arm64) ARCH=arm64 ;;
  *) echo "no ccusage build for $(uname -m)" >&2; exit 1 ;;
esac
PKG="ccusage-linux-$ARCH"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

curl -fsSL -o "$TMP/meta.json" "https://registry.npmjs.org/@ccusage/$PKG/$VERSION"
WANT="$(python3 -I -c 'import json,sys; print(json.load(open(sys.argv[1]))["dist"]["integrity"])' "$TMP/meta.json")"
curl -fsSL -o "$TMP/pkg.tgz" "https://registry.npmjs.org/@ccusage/$PKG/-/$PKG-$VERSION.tgz"
GOT="sha512-$(openssl dgst -sha512 -binary "$TMP/pkg.tgz" | base64 -w0)"
[[ "$GOT" == "$WANT" ]] || { echo "hash mismatch for $PKG $VERSION" >&2; exit 1; }

tar xzf "$TMP/pkg.tgz" -C "$TMP" package/bin/ccusage
mkdir -p "$ROOT/vendor"
install -m 0755 "$TMP/package/bin/ccusage" "$ROOT/vendor/ccusage"
"$ROOT/vendor/ccusage" --version
