#!/bin/bash

# NFS Client Setup Script for Worker Container
# This script is built into the worker container and mounts NFS share

set -e

MANAGER_IP="$1"

echo "Setting up NFS client on worker..."
echo "Manager IP: $MANAGER_IP"

# Install NFS client tools (if needed)
export DEBIAN_FRONTEND=noninteractive
apt-get update
apt-get install -y nfs-common rpcbind

# Start rpcbind and rpc.statd for NFS client
echo "Starting NFS client services..."
if ! pgrep rpcbind > /dev/null; then
    rpcbind &
    sleep 2
fi

if ! pgrep rpc.statd > /dev/null; then
    rpc.statd --no-notify &
    sleep 2
fi

# Create mount point
mkdir -p /shared

# Wait for manager to be ready and test connectivity
echo "Waiting for manager to be ready..."
for i in {1..12}; do
    if showmount -e "$MANAGER_IP" >/dev/null 2>&1; then
        echo "[OK] Manager NFS server is ready"
        break
    fi
    echo "Waiting for manager NFS server... ($i/12)"
    sleep 5
done

# Unmount if already mounted
umount /shared 2>/dev/null || true

# Mount NFS share with proper options (no locking issues)
echo "Mounting NFS share..."
if mount -t nfs -o addr="$MANAGER_IP",rw,soft,intr,nolock "$MANAGER_IP":/tmp/shared /shared; then
    echo "[OK] NFS mounted successfully"
    
    # Create worker identifier file
    WORKER_NAME=$(hostname | cut -c1-8)
    echo "Worker $WORKER_NAME ready at $(date)" > /shared/${WORKER_NAME}.txt
    
    # Add to fstab for persistence
    echo "$MANAGER_IP:/tmp/shared /shared nfs addr=$MANAGER_IP,rw,soft,intr,nolock 0 0" >> /etc/fstab
    
    echo "[OK] NFS client setup completed!"
    echo "  Mount point: /shared"
    ls -la /shared/ 2>/dev/null || true
    
else
    echo "[FAIL] Failed to mount NFS share"
    echo "  Will retry in background..."
    
    # Retry in background (non-blocking)
    (
        sleep 30
        echo "Retrying NFS mount..."
        mount -t nfs -o addr="$MANAGER_IP",rw,soft,intr,nolock "$MANAGER_IP":/tmp/shared /shared 2>/dev/null && \
        echo "[OK] NFS mounted on retry" || echo "[FAIL] NFS mount failed on retry"
    ) &
fi