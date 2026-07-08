#!/bin/bash

# Check if host public key file is provided
if [ -z "$1" ]; then
  echo "Usage: $0 <host_public_key_file>"
  exit 1
fi

# Check if hosts.yml exists
if [ ! -f "hosts.yml" ]; then
  echo "Error: hosts.yml not found"
  exit 1
fi

echo "Generating SSH keys..."
ssh-keygen -t ed25519 -f /tmp/manager_key -N ''

# Copy keys to the build contexts
cp $1 ./manager/
cp /tmp/manager_key ./manager/
cp /tmp/manager_key.pub ./manager/
cp /tmp/manager_key.pub ./worker/

echo "Generating docker-compose.yml from hosts.yml..."

# Generate docker-compose.yml dynamically
generate_docker_compose() {
  local manager_ip=$(grep "^manager:" hosts.yml | cut -d' ' -f2)
  local subnet=$(grep "^subnet:" hosts.yml | cut -d' ' -f2)

  
  cat > docker-compose.yml << EOF
services:
  manager:
    build:
        context: ./manager
        dockerfile: Dockerfile.manager
        args:
            HOST_PUBKEY: \${HOST_PUBKEY}
            MANAGER_PRIVKEY: \${MANAGER_PRIVKEY}
            MANAGER_PUBKEY: \${MANAGER_PUBKEY}
            MANAGER_IP: ${manager_ip}
    container_name: manager
    hostname: manager
    privileged: true
    networks:
      gf-virtual-net:
        ipv4_address: ${manager_ip}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

EOF

  # Generate worker services dynamically
  local worker_num=1
  grep -A 100 "^workers:" hosts.yml | grep "^ *-" | while read -r line; do
    local worker_ip=$(echo "$line" | sed 's/^ *- *//')
    cat >> docker-compose.yml << EOF
  worker${worker_num}:
    build:
        context: ./worker
        dockerfile: Dockerfile.worker
        args:
            MANAGER_PUBKEY: \${MANAGER_PUBKEY}
            MANAGER_IP: ${manager_ip}
    container_name: worker${worker_num}
    hostname: worker${worker_num}
    privileged: true
    depends_on:
        - manager
    networks:
      gf-virtual-net:
        ipv4_address: ${worker_ip}
    deploy:
      resources:
        reservations:
          devices:
            - driver: nvidia
              count: all
              capabilities: [gpu]

EOF
    worker_num=$((worker_num + 1))
  done

  # Add networks section
  cat >> docker-compose.yml << EOF
networks:
  gf-virtual-net:
    driver: bridge
    ipam:
      config:
        - subnet: ${subnet}
EOF
}

# Generate the docker-compose.yml file
generate_docker_compose

echo "Starting containers..."
# Get just the filename for the build args
MANAGER_IP=${manager_ip} HOST_PUBKEY=$(basename $1) MANAGER_PRIVKEY=manager_key MANAGER_PUBKEY=manager_key.pub docker compose up --build -d

echo "Cleaning up temporary files..."
rm ./manager/$(basename $1) /tmp/manager_key /tmp/manager_key.pub ./manager/manager_key ./manager/manager_key.pub ./worker/manager_key.pub

echo "Deployment completed!"