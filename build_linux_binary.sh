#!/usr/bin/env bash
set -Eeuo pipefail

APP_NAME="ailinux-kernel-builder"
PROJECT_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
BUILD_VENV="${BUILD_VENV:-$PROJECT_DIR/.build-venv}"
PYTHON="${PYTHON:-python3}"

if [[ "$(uname -s)" != "Linux" ]]; then
    echo "Fehler: Dieses Skript baut ausschließlich Linux-Binaries." >&2
    exit 1
fi

if ! command -v "$PYTHON" >/dev/null 2>&1; then
    echo "Fehler: Python-Interpreter nicht gefunden: $PYTHON" >&2
    exit 1
fi

if [[ ! -d "$BUILD_VENV" ]]; then
    echo "Erstelle isolierte Build-Umgebung: $BUILD_VENV"
    "$PYTHON" -m venv "$BUILD_VENV"
fi

BUILD_PYTHON="$BUILD_VENV/bin/python"
if [[ ! -x "$BUILD_PYTHON" ]]; then
    echo "Fehler: Ungültige Build-Umgebung: $BUILD_VENV" >&2
    exit 1
fi

echo "Installiere Build-Abhängigkeiten …"
"$BUILD_PYTHON" -m pip install --upgrade pip wheel
"$BUILD_PYTHON" -m pip install --upgrade -r "$PROJECT_DIR/requirements.txt" "pyinstaller>=6,<7"

mkdir -p "$PROJECT_DIR/build/pyinstaller" "$PROJECT_DIR/dist"

echo "Baue Linux-Binary …"
"$BUILD_PYTHON" -m PyInstaller \
    --noconfirm \
    --clean \
    --onefile \
    --windowed \
    --name "$APP_NAME" \
    --paths "$PROJECT_DIR" \
    --distpath "$PROJECT_DIR/dist" \
    --workpath "$PROJECT_DIR/build/pyinstaller" \
    --specpath "$PROJECT_DIR/build/pyinstaller" \
    "$PROJECT_DIR/ailinux-kernel-builder.py"

BINARY="$PROJECT_DIR/dist/$APP_NAME"
if [[ ! -x "$BINARY" ]]; then
    echo "Fehler: PyInstaller hat keine ausführbare Binary erzeugt." >&2
    exit 1
fi

echo
echo "Fertig: $BINARY"
sha256sum "$BINARY"
