#!/usr/bin/env bash
# Installs TV Launcher for the current user (no root needed, works on Bazzite).
#   ./install.sh              install / update
#   ./install.sh --autostart  also start it automatically when KDE logs in
set -euo pipefail

SRC="$(cd "$(dirname "$0")" && pwd)"
APP_DIR="$HOME/.local/share/tv-launcher"
CONFIG_DIR="$HOME/.config/tv-launcher"
BIN="$HOME/.local/bin/tv-launcher"
DESKTOP="$HOME/.local/share/applications/tv-launcher.desktop"

echo "Setting up Python environment (first run downloads Qt, ~100 MB)..."
mkdir -p "$APP_DIR"
[ -x "$APP_DIR/venv/bin/python" ] || python3 -m venv "$APP_DIR/venv"
"$APP_DIR/venv/bin/pip" install --quiet --upgrade pip PySide6 PyYAML pillow-heif

cp "$SRC/launcher.py" "$APP_DIR/launcher.py"

# Never overwrite an existing config: it's yours to edit.
mkdir -p "$CONFIG_DIR/icons"
if [ ! -f "$CONFIG_DIR/apps.yaml" ]; then
  cp "$SRC/apps.yaml" "$CONFIG_DIR/apps.yaml"
  echo "Created $CONFIG_DIR/apps.yaml"
fi
[ -d "$SRC/icons" ] && cp -n "$SRC"/icons/* "$CONFIG_DIR/icons/" 2>/dev/null || true

mkdir -p "$(dirname "$BIN")"
cat > "$BIN" <<EOF
#!/usr/bin/env bash
exec "$APP_DIR/venv/bin/python" "$APP_DIR/launcher.py" "\$@"
EOF
chmod +x "$BIN"

mkdir -p "$(dirname "$DESKTOP")"
cat > "$DESKTOP" <<EOF
[Desktop Entry]
Type=Application
Name=TV Launcher
Comment=Fullscreen app launcher for the TV
Exec=$BIN
Icon=video-display
Categories=Utility;
EOF

if [ "${1:-}" = "--autostart" ]; then
  mkdir -p "$HOME/.config/autostart"
  cp "$DESKTOP" "$HOME/.config/autostart/tv-launcher.desktop"
  echo "Autostart enabled (remove ~/.config/autostart/tv-launcher.desktop to undo)."
fi

echo "Done. Run 'tv-launcher' or find 'TV Launcher' in the app menu."
echo "Edit your apps in: $CONFIG_DIR/apps.yaml"
