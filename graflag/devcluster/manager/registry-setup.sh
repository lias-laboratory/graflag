#!/bin/bash

# Docker Registry Setup Script for Manager Container
# This script configures Docker daemon for insecure registry access

set -e

echo "Setting up Docker registry configuration..."

MANAGER_IP="$1"

if [ -z "$MANAGER_IP" ]; then
    echo "[ERROR] MANAGER_IP not provided"
    echo "Usage: $0 <MANAGER_IP>"
    exit 1
fi

# Configure Docker daemon for insecure registry
echo "Configuring Docker daemon for registry access..."
mkdir -p /etc/docker

# Create or update daemon.json with registry configuration
cat > /etc/docker/daemon.json << EOF
{
  "insecure-registries": ["localhost:5000", "127.0.0.1:5000", "0.0.0.0:5000", "${MANAGER_IP}:5000"]
}
EOF

echo "[OK] Docker registry configuration completed!"
echo "  Configured insecure registries for: localhost:5000, 127.0.0.1:5000, 0.0.0.0:5000, ${MANAGER_IP}:5000"
