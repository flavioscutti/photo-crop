#!/bin/bash
# install.sh - Photo Crop installation script
# Target: Debian-based Linux (Raspberry Pi OS, Ubuntu, Debian, etc.)

set -e

echo "=== Photo Crop Installation ==="
echo ""

# 1. Check if running as root
if [ "$EUID" -ne 0 ]; then
  echo "Please run as root (use: sudo ./install.sh)"
  exit 1
fi

# 2. Detect package manager and install system dependencies
if command -v apt-get &> /dev/null; then
    echo "[1/3] Installing system dependencies with apt..."
    apt-get update
    apt-get install -y python3 python3-tk python3-opencv python3-numpy python3-pil python3-pil.imagetk libimage-exiftool-perl fonts-inter
elif command -v dnf &> /dev/null; then
    echo "[1/3] Installing system dependencies with dnf..."
    dnf install -y python3 python3-tkinter python3-opencv python3-numpy python3-pillow python3-pillow-tk perl-Image-ExifTool rsms-inter-fonts
elif command -v pacman &> /dev/null; then
    echo "[1/3] Installing system dependencies with pacman..."
    pacman -S --noconfirm tk python-opencv python-numpy python-pillow perl-image-exiftool inter-font
else
    echo "Warning: Unknown package manager. Please install the dependencies manually:"
    echo "python3, tkinter, opencv, numpy, pillow, exiftool, inter font"
fi

# 3. Verify exiftool
if ! command -v exiftool &> /dev/null; then
    echo ""
    echo "WARNING: exiftool not found. Photo Crop will work, but without metadata copying."
    echo "You can install it later with: sudo apt install libimage-exiftool-perl"
fi

# 4. Create the desktop launcher
echo ""
echo "[2/3] Creating desktop launcher..."

# Find the absolute path of the script directory
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APP_PATH="$SCRIPT_DIR/photo_crop.py"

# Determine the user's local applications folder
# If run with sudo, use the SUDO_USER's home
if [ -n "$SUDO_USER" ]; then
    USER_HOME=$(eval echo "~$SUDO_USER")
else
    USER_HOME=$HOME
fi
DESKTOP_DIR="$USER_HOME/.local/share/applications"

mkdir -p "$DESKTOP_DIR"

cat > "$DESKTOP_DIR/photo-crop.desktop" <<DESKTOP
[Desktop Entry]
Name=Photo Crop
Comment=Batch crop of analog photo scans
Exec=python3 "$APP_PATH"
Icon=camera-photo
Terminal=false
Type=Application
Categories=Graphics;Photography;
DESKTOP

chmod +x "$DESKTOP_DIR/photo-crop.desktop"

echo ""
echo "[3/3] Installation complete!"
echo "You can now launch Photo Crop from the Graphics menu."
echo "Or run it from the terminal: python3 $APP_PATH"
