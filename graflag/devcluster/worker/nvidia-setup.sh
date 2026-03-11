#!/bin/bash

# NVIDIA Container Toolkit Setup Script for Worker Container
# This script installs and configures NVIDIA Container Toolkit for GPU support in Docker Swarm

set -e

echo "Setting up NVIDIA Container Toolkit on worker..."

# Configure the repository
curl -fsSL https://nvidia.github.io/libnvidia-container/gpgkey | gpg --dearmor -o /usr/share/keyrings/nvidia-container-toolkit-keyring.gpg \
&& curl -s -L https://nvidia.github.io/libnvidia-container/stable/deb/nvidia-container-toolkit.list | sed 's#deb https://#deb [signed-by=/usr/share/keyrings/nvidia-container-toolkit-keyring.gpg] https://#g' | tee /etc/apt/sources.list.d/nvidia-container-toolkit.list \
&& apt-get update

# Install the NVIDIA Container Toolkit packages
apt-get install -y nvidia-container-toolkit

# Configure the container runtime by using the nvidia-ctk command
nvidia-ctk runtime configure --runtime=docker

echo "Configuring GPU support for Docker Swarm..."

# Get GPU UUID for Docker Swarm configuration
if command -v nvidia-smi >/dev/null 2>&1; then
    GPU_UUID=$(nvidia-smi -a | grep UUID | head -n1 | awk '{print $4}' | sed 's/GPU-//')
    if [ -n "$GPU_UUID" ]; then
        echo "Found GPU UUID: GPU-$GPU_UUID"
        
        # Read existing daemon.json and merge with GPU configuration
        if [ -f /etc/docker/daemon.json ]; then
            # Backup existing config
            cp /etc/docker/daemon.json /etc/docker/daemon.json.backup
            
            echo "Existing daemon.json found, merging configurations..."
            cat /etc/docker/daemon.json
            
            # Check if insecure-registries exists in the current config
            if grep -q "insecure-registries" /etc/docker/daemon.json; then
                echo "Found existing insecure-registries, preserving them..."
                # Extract the insecure-registries array properly
                REGISTRIES=$(cat /etc/docker/daemon.json | python3 -c "
import json, sys
config = json.load(sys.stdin)
if 'insecure-registries' in config:
    print(json.dumps(config['insecure-registries']))
else:
    print('[]')
")
                
                # Create new daemon.json with GPU support, preserving registry config
                cat > /etc/docker/daemon.json << EOF
{
  "insecure-registries": $REGISTRIES,
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia",
  "node-generic-resources": [
    "NVIDIA-GPU=GPU-$GPU_UUID"
  ]
}
EOF
            else
                echo "No insecure-registries found, creating GPU-only config..."
                # Create daemon.json with GPU support only
                cat > /etc/docker/daemon.json << EOF
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia",
  "node-generic-resources": [
    "NVIDIA-GPU=GPU-$GPU_UUID"
  ]
}
EOF
            fi
        else
            # Create daemon.json with GPU support only
            mkdir -p /etc/docker
            cat > /etc/docker/daemon.json << EOF
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia"
}
EOF
        fi
        
        # Configure NVIDIA container runtime config
        if [ -f /etc/nvidia-container-runtime/config.toml ]; then
            echo "Configuring NVIDIA container runtime..."
            sed -i 's/#swarm-resource = "DOCKER_RESOURCE_GPU"/swarm-resource = "DOCKER_RESOURCE_GPU"/' /etc/nvidia-container-runtime/config.toml
            echo "[OK] NVIDIA container runtime configured for Swarm"
        fi
        
        echo "[OK] GPU configuration completed for Docker Swarm"
        echo "  GPU UUID: GPU-$GPU_UUID"
        echo "  Runtime: nvidia (default)"
        echo "  Swarm resource: NVIDIA-GPU=GPU-$GPU_UUID"
    else
        echo "[WARN] GPU UUID not found, using basic NVIDIA runtime configuration"
        # Still configure basic NVIDIA runtime without UUID, preserving registry config
        if [ -f /etc/docker/daemon.json ]; then
            cp /etc/docker/daemon.json /etc/docker/daemon.json.backup
            
            # Check if insecure-registries exists
            if grep -q "insecure-registries" /etc/docker/daemon.json; then
                echo "Preserving existing insecure-registries..."
                REGISTRIES=$(cat /etc/docker/daemon.json | python3 -c "
import json, sys
config = json.load(sys.stdin)
if 'insecure-registries' in config:
    print(json.dumps(config['insecure-registries']))
else:
    print('[]')
")
                
                cat > /etc/docker/daemon.json << EOF
{
  "insecure-registries": $REGISTRIES,
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia"
}
EOF
            else
                cat > /etc/docker/daemon.json << EOF
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia"
}
EOF
            fi
        else
            mkdir -p /etc/docker
            cat > /etc/docker/daemon.json << EOF
{
  "runtimes": {
    "nvidia": {
      "path": "nvidia-container-runtime",
      "runtimeArgs": []
    }
  },
  "default-runtime": "nvidia"
}
EOF
        fi
    fi
else
    echo "[WARN] nvidia-smi not available, GPU support may be limited"
fi