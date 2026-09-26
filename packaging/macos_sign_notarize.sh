#!/bin/bash
# Sign the .app (every Mach-O inside, deepest first), build a DMG, notarize and staple it.
#
#   packaging/macos_sign_notarize.sh "dist/XHS Research Agent.app" out.dmg
#
# Env (all optional; without them the app is ad-hoc signed and not notarized):
#   MACOS_SIGN_IDENTITY   e.g. "Developer ID Application: Your Name (TEAMID)"
#   NOTARY_PROFILE        a `xcrun notarytool store-credentials` keychain profile (local builds), or
#   APPLE_ID, APPLE_TEAM_ID, APPLE_APP_PASSWORD   (app-specific password) for notarytool (CI)
set -euo pipefail
APP="$1"
DMG="$2"
HERE="$(cd "$(dirname "$0")" && pwd)"
ENT="$HERE/entitlements.plist"
IDENTITY="${MACOS_SIGN_IDENTITY:--}"   # "-" = ad-hoc

sign() {
  if [ "$IDENTITY" = "-" ]; then
    codesign --force --sign - "$1"
  else
    codesign --force --timestamp --options runtime --entitlements "$ENT" --sign "$IDENTITY" "$1"
  fi
}

echo "==> Signing Mach-O files inside $APP (identity: $IDENTITY)"
# Deepest paths first so containers are signed after their contents; skip symlinks.
find "$APP/Contents" -type f -print0 \
  | while IFS= read -r -d '' f; do
      if file -b "$f" | grep -q "Mach-O"; then
        printf '%s\t%s\n' "$(tr -cd '/' <<<"$f" | wc -c)" "$f"
      fi
    done \
  | sort -rn | cut -f2- \
  | while IFS= read -r f; do sign "$f"; done
sign "$APP"
codesign --verify --deep --strict --verbose=2 "$APP"

echo "==> Building $DMG"
STAGE="$(mktemp -d)"
cp -R "$APP" "$STAGE/"
ln -s /Applications "$STAGE/Applications"
rm -f "$DMG"
hdiutil create -volname "小红书调研助手" -srcfolder "$STAGE" -ov -format UDZO "$DMG"
rm -rf "$STAGE"

if [ -n "${NOTARY_PROFILE:-}" ]; then
  NOTARY_AUTH=(--keychain-profile "$NOTARY_PROFILE")
elif [ -n "${APPLE_ID:-}" ]; then
  NOTARY_AUTH=(--apple-id "$APPLE_ID" --team-id "$APPLE_TEAM_ID" --password "$APPLE_APP_PASSWORD")
fi
if [ "$IDENTITY" = "-" ] || [ -z "${NOTARY_AUTH+x}" ]; then
  echo "==> Not notarizing (no Developer ID identity / Apple credentials). Users will see a Gatekeeper warning."
  exit 0
fi
sign "$DMG"
echo "==> Notarizing (this can take several minutes)"
xcrun notarytool submit "$DMG" "${NOTARY_AUTH[@]}" --wait
xcrun stapler staple "$DMG"
spctl -a -t open --context context:primary-signature -v "$DMG"
echo "==> Done: signed, notarized and stapled"
