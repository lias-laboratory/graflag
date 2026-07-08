#!/bin/bash

# Docker Registry Setup Script for Worker Container
# This script configures Docker daemon for insecure registry access

set -e

echo "Setting up Docker registry configuration for worker..."

MANAGER_IP="$1"

if [ -z "$MANAGER_IP" ]; then
    echo "[ERROR] MANAGER_IP not provided"
    echo "Usage: $0 <MANAGER_IP>"
    exit 1
fi

# Configure Docker daemon for insecure registry
echo "Configuring Docker daemon for registry access..."
mkdir -p /etc/docker

# Create daemon.json with registry configuration
cat > /etc/docker/daemon.json << EOF
{
  "insecure-registries": ["${MANAGER_IP}:5000"]
}
EOF

echo "[OK] Docker registry configuration completed!"
echo "  Configured insecure registry for: ${MANAGER_IP}:5000"