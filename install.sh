#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")" && pwd)"
PLASMOID_DIR="$ROOT_DIR/plasmoid"
APPLET_ID="com.github.gustavobragac.plasmaaiusage"

if ! command -v kpackagetool6 >/dev/null; then
    echo "kpackagetool6 not found. Install with: sudo pacman -S plasma-framework"
    exit 1
fi

if kpackagetool6 --type Plasma/Applet --list 2>/dev/null | grep -q "$APPLET_ID"; then
    echo "Upgrading $APPLET_ID..."
    kpackagetool6 --type Plasma/Applet --upgrade "$PLASMOID_DIR"
else
    echo "Installing $APPLET_ID..."
    kpackagetool6 --type Plasma/Applet --install "$PLASMOID_DIR"
fi

echo
echo "Done. Right-click the panel → Add or Manage Widgets → search for 'Plasma AI Usage'."
echo
echo "Make sure the CLI helpers are on PATH:"
echo "  uv tool install --from . waybar-ai-usage"
echo "or:"
echo "  pip install --user ."
