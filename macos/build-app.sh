#!/usr/bin/env bash
# Build "Arctis Sound Manager.app", the self-contained macOS app, into dist/.
# Needs uv and Homebrew's libusb (brew install libusb); see docs/macos.md.
set -euo pipefail

cd "$(dirname "$0")/.."

# Its own environment, on a Python uv manages (downloaded if missing):
# PyInstaller needs libpython as a shared library, which the Python behind
# a checkout's .venv often lacks (pyenv's default build, for one).
export UV_PROJECT_ENVIRONMENT="$PWD/build/app-venv"
export UV_PYTHON_PREFERENCE=only-managed
export UV_PYTHON=3.13
uv sync --frozen --no-dev

# The app icon, at every size macOS asks for.
iconset=build/ASM.iconset
rm -rf "$iconset" && mkdir -p "$iconset"
for size in 16 32 128 256 512; do
    sips -z $size $size docs/images/asm-logo.png --out "$iconset/icon_${size}x${size}.png" >/dev/null
    double=$((size * 2))
    sips -z $double $double docs/images/asm-logo.png --out "$iconset/icon_${size}x${size}@2x.png" >/dev/null
done
iconutil -c icns "$iconset" -o build/ASM.icns

uv run --frozen --no-dev --with 'pyinstaller>=6.11' pyinstaller --noconfirm --clean \
    --distpath dist --workpath build/pyinstaller macos/asm.spec

echo "Built: dist/Arctis Sound Manager.app"
