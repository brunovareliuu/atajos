#!/bin/bash
# Arma Grabadora.app, la instala en /Applications y la deja abriendo sola (en segundo plano) al iniciar sesión,
# para que ⌃⌥⌘G funcione siempre. Tras cambiar el código no hace falta correrlo: la app lee app.py y ui/ de aquí.
set -euo pipefail
AQUI="$(cd "$(dirname "$0")" && pwd)"
APP="/Applications/Grabadora.app"
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

# Ícono: círculo naranja con micrófono blanco sobre negro.
"$AQUI/.venv/bin/python" - "$TMP" <<'PY'
import sys
from PIL import Image, ImageDraw, ImageFilter
out = sys.argv[1]
S = 1024
im = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(im)
m = 100
d.rounded_rectangle([m, m, S - m, S - m], radius=185, fill=(14, 14, 14, 255))
glow = Image.new("RGBA", (S, S), (0, 0, 0, 0))
ImageDraw.Draw(glow).ellipse([290, 290, 734, 734], fill=(250, 76, 3, 150))
im = Image.alpha_composite(im, glow.filter(ImageFilter.GaussianBlur(60)))
d = ImageDraw.Draw(im)
d.ellipse([302, 302, 722, 722], fill=(250, 76, 3, 255))
w = (255, 255, 255, 255)
d.rounded_rectangle([462, 392, 562, 572], radius=50, fill=w)
d.arc([412, 452, 612, 642], start=0, end=180, fill=w, width=26)
d.line([512, 642, 512, 676], fill=w, width=26)
d.rounded_rectangle([462, 664, 562, 688], radius=12, fill=w)
im.save(f"{out}/icono.png")
iconset = f"{out}/icono.iconset"
import os; os.makedirs(iconset)
for s in (16, 32, 128, 256, 512):
    im.resize((s, s), Image.LANCZOS).save(f"{iconset}/icon_{s}x{s}.png")
    im.resize((s * 2, s * 2), Image.LANCZOS).save(f"{iconset}/icon_{s}x{s}@2x.png")
PY
iconutil -c icns "$TMP/icono.iconset" -o "$TMP/icono.icns"
cp "$TMP/icono.png" "$AQUI/icono.png"

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$TMP/icono.icns" "$APP/Contents/Resources/icono.icns"

cat > "$APP/Contents/MacOS/Grabadora" <<SH
#!/bin/bash
exec "$AQUI/.venv/bin/python" "$AQUI/app.py" "\$@" >>"\$HOME/Library/Logs/Grabadora.log" 2>&1
SH
chmod +x "$APP/Contents/MacOS/Grabadora"

cat > "$APP/Contents/Info.plist" <<PL
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>CFBundleName</key><string>Grabadora</string>
  <key>CFBundleDisplayName</key><string>Grabadora</string>
  <key>CFBundleIdentifier</key><string>com.vareliuu.grabadora</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>Grabadora</string>
  <key>CFBundleIconFile</key><string>icono</string>
  <key>LSMinimumSystemVersion</key><string>13.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSMicrophoneUsageDescription</key><string>Grabadora usa el micrófono para grabar tus notas de voz y transcribirlas en tu Mac.</string>
</dict></plist>
PL

codesign --force --deep -s - "$APP" >/dev/null 2>&1 || true
touch "$APP"
rm -rf "$TMP"

# Abrir al iniciar sesión, sin ventana.
AGENTE="$HOME/Library/LaunchAgents/com.vareliuu.grabadora.plist"
cat > "$AGENTE" <<AG
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0"><dict>
  <key>Label</key><string>com.vareliuu.grabadora</string>
  <key>ProgramArguments</key><array>
    <string>/usr/bin/open</string><string>-g</string><string>-a</string><string>$APP</string>
    <string>--args</string><string>--fondo</string>
  </array>
  <key>RunAtLoad</key><true/>
</dict></plist>
AG
echo "Instalada en $APP"
