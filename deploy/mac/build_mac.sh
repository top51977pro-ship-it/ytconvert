#!/bin/bash
# Build YTConvert.app and a drag-to-Applications DMG. Runs on macOS (the
# GitHub Actions workflow in .github/workflows/mac.yml).
#
#   bash deploy/mac/build_mac.sh arm64     # Apple Silicon
#   bash deploy/mac/build_mac.sh x86_64    # Intel (built and run under Rosetta)
#
# Layout:
#   YTConvert.app/Contents/
#     MacOS/YTConvert          launcher script
#     Resources/python/        standalone CPython (python-build-standalone) + yt-dlp
#     Resources/app/           app.py, web/, bin/ (ffmpeg, ffprobe, qjs)
#     Resources/YTConvert.icns
set -euo pipefail

ARCH="${1:?arch: arm64 or x86_64}"
ROOT="$(cd "$(dirname "$0")/../.." && pwd)"
OUT="$ROOT/dist-mac/$ARCH"
VERSION="1.1.0"
case "$ARCH" in
  arm64)  PBS=aarch64-apple-darwin; FF=arm64; QJS=qjs-darwin-arm64;  RUN="" ;;
  x86_64) PBS=x86_64-apple-darwin;  FF=amd64; QJS=qjs-darwin-x86_64; RUN="arch -x86_64" ;;
  *) echo "unknown arch $ARCH"; exit 1 ;;
esac

rm -rf "$OUT" && mkdir -p "$OUT"
WORK="$(mktemp -d)"
APP="$OUT/YTConvert.app"
C="$APP/Contents"
R="$C/Resources"
mkdir -p "$C/MacOS" "$R/app/bin"
auth=()
[ -n "${GH_TOKEN:-}" ] && auth=(-H "Authorization: Bearer $GH_TOKEN")

echo "==> Python"
PY_URL=$(curl -fsSL "${auth[@]}" https://api.github.com/repos/astral-sh/python-build-standalone/releases/latest |
  /usr/bin/python3 -c "import sys, json
names = [a['browser_download_url'] for a in json.load(sys.stdin)['assets']
         if a['name'].startswith('cpython-3.12.') and a['name'].endswith('-$PBS-install_only_stripped.tar.gz')]
print(names[0])")
echo "   $PY_URL"
curl -fsSL "$PY_URL" | tar -xz -C "$R"
PYLIB="$R/python/lib/python3.12"
rm -rf "$PYLIB"/{test,idlelib,tkinter,turtledemo,ensurepip,lib2to3} "$R/python/lib"/{itcl*,tcl*,tk*,thread*} \
       "$R/python/lib"/libtcl* "$R/python/lib"/libtk* "$R/python/share" 2>/dev/null || true
$RUN "$R/python/bin/python3" -m pip install -q --no-cache-dir --disable-pip-version-check "yt-dlp[default]"
$RUN "$R/python/bin/python3" -c "import yt_dlp, ssl; print('   yt-dlp', yt_dlp.version.__version__, '|', ssl.OPENSSL_VERSION)"

echo "==> FFmpeg + QuickJS"
for tool in ffmpeg ffprobe; do
  if ! curl -fsSL "https://ffmpeg.martin-riedl.de/redirect/latest/macos/$FF/release/$tool.zip" -o "$WORK/$tool.zip"; then
    echo "martin-riedl.de download failed for $tool/$FF"; exit 1
  fi
  unzip -q -o "$WORK/$tool.zip" -d "$R/app/bin"
done
curl -fsSL "https://github.com/quickjs-ng/quickjs/releases/download/v0.17.0/$QJS" -o "$R/app/bin/qjs"
chmod +x "$R/app/bin/"*
file "$R/app/bin/"* | sed 's/^/   /'
$RUN "$R/app/bin/ffmpeg" -hide_banner -version | head -1 | sed 's/^/   /'

echo "==> App"
cp "$ROOT/app.py" "$R/app/"
cp -R "$ROOT/web" "$R/app/web"
rm -f "$R/app/web/online.html"  # website-only page

cat > "$C/MacOS/YTConvert" <<'EOF'
#!/bin/bash
# YTConvert launcher. The first time the downloaded app is opened, macOS asks
# the user to approve it (System Settings > Privacy & Security > Open Anyway).
# Once they have, clear the download quarantine from the rest of our own
# bundle so the bundled Python and FFmpeg can start too.
C="$(cd "$(dirname "$0")/.." && pwd)"
/usr/bin/xattr -dr com.apple.quarantine "$C/.." 2>/dev/null
exec "$C/Resources/python/bin/python3" "$C/Resources/app/app.py" "$@"
EOF
chmod +x "$C/MacOS/YTConvert"

cat > "$C/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>YTConvert</string>
  <key>CFBundleDisplayName</key><string>YTConvert</string>
  <key>CFBundleIdentifier</key><string>app.ytconvert.mac</string>
  <key>CFBundleVersion</key><string>$VERSION</string>
  <key>CFBundleShortVersionString</key><string>$VERSION</string>
  <key>CFBundleExecutable</key><string>YTConvert</string>
  <key>CFBundleIconFile</key><string>YTConvert</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
EOF

echo "==> Icon"
ICONSET="$WORK/YTConvert.iconset"
mkdir -p "$ICONSET"
for s in 16 32 128 256 512; do
  sips -z $s $s "$ROOT/deploy/mac/icon-1024.png" --out "$ICONSET/icon_${s}x${s}.png" >/dev/null
  sips -z $((s*2)) $((s*2)) "$ROOT/deploy/mac/icon-1024.png" --out "$ICONSET/icon_${s}x${s}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$R/YTConvert.icns"

echo "==> Ad-hoc signing"
# Apple Silicon refuses to run unsigned code, and an unsigned bundle is
# reported as "damaged" with no way to open it. Ad-hoc signatures (no Apple
# account) make it "unverified" instead, which the user can approve.
find "$R" -type f | while read -r f; do
  if file -b "$f" | grep -q "Mach-O"; then codesign --force --sign - "$f" 2>/dev/null; fi
done
codesign --force --sign - "$APP"
codesign --verify --strict --verbose=1 "$APP" 2>&1 | sed 's/^/   /'

echo "==> DMG"
mkdir -p "$WORK/dmg"
cp -R "$APP" "$WORK/dmg/"
ln -s /Applications "$WORK/dmg/Applications"
hdiutil create -quiet -volname "YTConvert" -srcfolder "$WORK/dmg" -ov -format UDZO "$OUT/YTConvert-mac-$ARCH.dmg"
du -sh "$APP" "$OUT/YTConvert-mac-$ARCH.dmg" | sed 's/^/   /'
