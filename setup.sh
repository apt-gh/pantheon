#!/usr/bin/env bash
# apt-gh — Ubuntu APT Mirror setup script
# Usage: curl -fsSL https://apt-definisi.pages.dev/setup.sh | sudo bash
set -euo pipefail

MIRROR_URL="https://apt-definisi.pages.dev"
KEY_URL="${MIRROR_URL}/key.gpg"
KEYRING_PATH="/etc/apt/keyrings/apt-gh.gpg"
LIST_PATH="/etc/apt/sources.list.d/apt-gh.list"

if [ "$(id -u)" -ne 0 ]; then
  echo "Error: This script must be run as root (use sudo)."
  exit 1
fi

if ! command -v lsb_release &> /dev/null; then
  echo "Error: lsb_release not found. Is this Ubuntu?"
  exit 1
fi

CODENAME=$(lsb_release -cs)
SUPPORTED=("noble" "jammy" "focal")

FOUND=false
for s in "${SUPPORTED[@]}"; do
  if [ "$CODENAME" = "$s" ]; then
    FOUND=true
    break
  fi
done

if [ "$FOUND" = false ]; then
  echo "Warning: codename '$CODENAME' is not officially supported."
  echo "Supported: ${SUPPORTED[*]}"
  echo "Continuing anyway..."
fi

echo "Setting up apt-gh mirror for Ubuntu $CODENAME..."

echo "  Importing GPG key..."
mkdir -p /etc/apt/keyrings
curl -fsSL "$KEY_URL" | gpg --dearmor -o "$KEYRING_PATH"

echo "  Adding mirror to sources.list.d..."
cat > "$LIST_PATH" << EOF
deb [signed-by=${KEYRING_PATH}] ${MIRROR_URL}/ubuntu ${CODENAME} main restricted universe multiverse
EOF

echo "  Done! Run 'apt update' to start using the mirror."
echo ""
echo "  Mirror URL: ${MIRROR_URL}"
echo "  Sources:    ${LIST_PATH}"
echo "  Keyring:    ${KEYRING_PATH}"
