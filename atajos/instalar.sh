#!/bin/bash
# Arma Atajos.app (sin Dock; barrita de íconos en la orilla derecha), la instala en /Applications y la deja abriendo
# sola al iniciar sesión para que ⌃⌥⌘A funcione siempre. Tras cambiar el código basta reiniciarla:
#   pkill -f atajos/atajos.py; open -a /Applications/Atajos.app
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Atajos.app"
TMP="$(mktemp -d)"

# Entorno de Python (la primera vez): con uv si está, si no con venv. Necesita Python 3.10+.
if [ ! -x "$AQUI/.venv/bin/python" ]; then
  echo "Creando el entorno de Python…"
  if command -v uv >/dev/null 2>&1; then
    uv venv -q --python 3.12 "$AQUI/.venv"
    uv pip install -q --python "$AQUI/.venv/bin/python" -r "$AQUI/requirements.txt"
  else
    python3 -m venv "$AQUI/.venv"
    "$AQUI/.venv/bin/pip" install -q -r "$AQUI/requirements.txt"
  fi
fi

# Ícono: cuadrícula de 4 piezas de vidrio, una naranja.
"$AQUI/.venv/bin/python" - "$TMP" <<'PY'
import os, sys
from PIL import Image, ImageDraw
out = sys.argv[1]
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
d.rounded_rectangle([100, 100, 924, 924], radius=185, fill=(18, 18, 20, 255))
m, g, a = 250, 44, (924 - 100 - 2 * 150 - 44) / 2
pos = [(250, 250), (250 + a + g, 250), (250, 250 + a + g), (250 + a + g, 250 + a + g)]
for i, (x, y) in enumerate(pos):
    col = (250, 76, 3, 255) if i == 0 else (255, 255, 255, 46 if i != 3 else 70)
    d.rounded_rectangle([x, y, x + a, y + a], radius=58, fill=col)
im.save(f"{out}/icono.png")
os.makedirs(f"{out}/icono.iconset")
for s in (16, 32, 128, 256, 512):
    im.resize((s, s), Image.LANCZOS).save(f"{out}/icono.iconset/icon_{s}x{s}.png")
    im.resize((s * 2, s * 2), Image.LANCZOS).save(f"{out}/icono.iconset/icon_{s}x{s}@2x.png")
PY
iconutil -c icns "$TMP/icono.iconset" -o "$TMP/icono.icns"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/icono.icns" "$APP/Contents/Resources/icono.icns"

cat > "$APP/Contents/MacOS/Atajos" <<SH
#!/bin/bash
exec "$AQUI/.venv/bin/python" "$AQUI/atajos.py" "\$@" >>"\$HOME/Library/Logs/Atajos.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/Atajos"

cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Atajos</string>
  <key>CFBundleDisplayName</key><string>Atajos</string>
  <key>CFBundleIdentifier</key><string>com.vareliuu.atajos</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Atajos</string>
  <key>CFBundleIconFile</key><string>icono</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>LSUIElement</key><true/>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSAppleEventsUsageDescription</key><string>Atajos controla Spotify (canción actual, pausa, siguiente).</string>
</dict></plist>
PL

codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
rm -rf "$TMP"

AGENTE="$HOME/Library/LaunchAgents/com.vareliuu.atajos.plist"
cat > "$AGENTE" <<AG
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.vareliuu.atajos</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-g</string><string>-a</string><string>$APP</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
AG
echo "Instalada en $APP"
