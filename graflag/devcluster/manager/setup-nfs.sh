#!/bin/bash

# NFS Server Setup Script for Manager Container
# This script is built into the manager container and sets up NFS server

set -e

echo "Setting up NFS server on manager..."

# Update package list and install NFS server packages (if not already installed)
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nfs-kernel-server rpcbind

# Create shared directory in /tmp (which supports NFS in containers)
mkdir -p /tmp/shared
chmod 755 /tmp/shared

# Create a symlink for easier access
ln -sf /tmp/shared /shared 2>/dev/null || true

# Configure exports (use /tmp/shared which supports NFS)
echo "/tmp/shared *(rw,sync,no_subtree_check,no_root_squash,insecure)" > /etc/exports

# Kill any existing NFS processes
pkill nfsd 2>/dev/null || true
pkill rpc.mountd 2>/dev/null || true
pkill rpcbind 2>/dev/null || true
sleep 2

# Start rpcbind (required for NFS)
echo "Starting rpcbind..."
if ! pgrep rpcbind > /dev/null; then
    rpcbind &
    sleep 2
else
    echo "rpcbind already running"
fi

# Start rpc.statd (required for NFS locking)
echo "Starting rpc.statd..."
if ! pgrep rpc.statd > /dev/null; then
    rpc.statd --no-notify &
    sleep 2
else
    echo "rpc.statd already running"
fi

# Start NFS kernel server components manually (more reliable in containers)
echo "Starting NFS kernel server..."
if ! pgrep nfsd > /dev/null; then
    rpc.nfsd 8
    sleep 2
else
    echo "nfsd already running"
fi

# Start mount daemon
echo "Starting mount daemon..."
if ! pgrep rpc.mountd > /dev/null; then
    rpc.mountd --port 20048 &
    sleep 2
else
    echo "rpc.mountd already running"
fi

# Export filesystems
echo "Exporting filesystems..."
exportfs -a

# Check status
echo "Checking NFS exports..."
exportfs -v
showmount -e localhost 2>/dev/null || echo "NFS server started (showmount may not work immediately)"

# Check if services are running
echo "Checking NFS services status..."
pgrep rpcbind >/dev/null && echo "[OK] rpcbind is running" || echo "[WARN] rpcbind may not be running"
pgrep nfsd >/dev/null && echo "[OK] nfsd is running" || echo "[WARN] nfsd may not be running"
pgrep rpc.statd >/dev/null && echo "[OK] rpc.statd is running" || echo "[WARN] rpc.statd may not be running"

# Create initial test file
echo "NFS server initialized at $(date)" > /shared/nfs_server_ready.txt

echo "[OK] NFS server setup completed!"
echo "  Shared directory: /tmp/shared (accessible via /shared symlink)"
echo "  Ready for client connections"