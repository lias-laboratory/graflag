"""Docker operations for GraFlag."""

import yaml
from pathlib import Path
from typing import Dict
import logging

logger = logging.getLogger(__name__)


class DockerManager:
    """Handle Docker Swarm and registry operations."""
    
    def __init__(self, ssh_manager, config, hosts_file: str = "hosts.yml"):
        """Initialize Docker manager."""
        self.ssh = ssh_manager
        self.config = config
        self.hosts_file = hosts_file
    
    def _load_hosts(self) -> Dict:
        """Load hosts configuration from YAML file."""
        hosts_path = Path(self.hosts_file)
        if not hosts_path.exists():
            logger.warning(f"Hosts file {self.hosts_file} not found")
            return {}

        with open(hosts_path, "r") as f:
            return yaml.safe_load(f)
    
    def get_swarm_token(self) -> str:
        """Get Docker Swarm join token."""
        result = self.ssh.execute("docker swarm join-token worker -q")
        if result.returncode != 0:
            raise RuntimeError(f"Failed to get swarm token: {result.stderr}")
        return result.stdout.strip()
    
    def setup_local_registry(self):
        """Setup local Docker registry on manager."""
        logger.info("🏗️  Setting up local Docker registry...")
        
        # Check if registry is already running
        result = self.ssh.execute("docker ps -a --filter name=registry --format '{{.Names}}'")
        if "registry" in result.stdout:
            logger.info("✅ Local registry already running")
            return
        
        # Start local registry container
        registry_cmd = """docker service create \
            --name registry \
            --publish published=5000,target=5000 \
            --mount type=volume,source=registry-data,target=/var/lib/registry \
            --constraint 'node.role==manager' \
            --replicas 1 \
            registry:2"""
        
        result = self.ssh.execute(registry_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to start registry: {result.stderr}")
        
        logger.info("✅ Local registry started on port 5000")
    
    def setup_swarm_manager(self):
        """Initialize Docker Swarm on manager node."""
        logger.info("🚀 Initializing Docker Swarm on manager...")

        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")

        # Check if swarm is already initialized
        result = self.ssh.execute("docker info --format '{{.Swarm.LocalNodeState}}'")
        if result.stdout.strip() == "active":
            logger.info("✅ Docker Swarm already initialized on manager")
            return

        # Initialize swarm
        swarm_cmd = f"docker swarm init --advertise-addr {manager_ip}"
        result = self.ssh.execute(swarm_cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to initialize swarm: {result.stderr}")

        logger.info("✅ Docker Swarm initialized on manager")
    
    def setup_workers(self, token: str):
        """Setup worker nodes to join the swarm."""
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")

        workers = hosts.get("workers", [])

        if not workers:
            logger.warning("No workers defined in hosts.yml")
            return

        logger.info(f"🔧 Setting up {len(workers)} worker nodes...")

        for worker_ip in workers:
            logger.info(f"Setting up worker {worker_ip}...")

            # Check if worker is already in swarm
            check_cmd = f"ssh -o StrictHostKeyChecking=no root@{worker_ip} 'docker info --format \"{{{{.Swarm.LocalNodeState}}}}\"'"
            result = self.ssh.execute(check_cmd)

            if result.stdout.strip() == "active":
                logger.info(f"✅ Worker {worker_ip} already in swarm")
                continue

            # Join worker to swarm
            join_cmd = f"ssh -o StrictHostKeyChecking=no root@{worker_ip} 'docker swarm join --token {token} {manager_ip}:2377'"
            result = self.ssh.execute(join_cmd)

            if result.returncode == 0:
                logger.info(f"✅ Worker {worker_ip} joined swarm")
            else:
                logger.error(f"❌ Failed to join worker {worker_ip}: {result.stderr}")
    
    def label_gpu_nodes(self):
        """Label all nodes with GPU support."""
        logger.info("🏷️  Labeling all nodes with GPU support...")
        
        # Get node IDs and label them
        result = self.ssh.execute("docker node ls -q")
        if result.returncode != 0:
            logger.error(f"❌ Failed to get node list: {result.stderr}")
            return
        
        node_ids = result.stdout.strip().split('\n')
        for node_id in node_ids:
            if node_id.strip():
                self.ssh.execute(f"docker node update --label-add gpu=true {node_id}")
        
        logger.info("✅ All nodes labeled with gpu=true")
    
    def build_method_image(self, method_name: str, tag: str = "latest") -> None:
        """Build method Docker image and push to local registry."""
        # Make parameters case-insensitive  
        method_name = method_name.lower()
        tag = tag.lower()
        
        logger.info(f"🔨 Building image {method_name}:{tag}...")
        
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")
        
        local_image = f"{method_name}:{tag}"
        registry_image = f"{manager_ip}:5000/{method_name}:{tag}"

        # Build image with both tags
        build_cmd = f"docker build --network=host -f {self.config.remote_shared_dir}/methods/{method_name}/Dockerfile -t {local_image} -t {registry_image} {self.config.remote_shared_dir}/methods/{method_name}/"
        result = self.ssh.execute(build_cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to build image {method_name}:{tag}: {result.stderr}")

        # Push to local registry
        logger.info(f"📤 Pushing {registry_image} to local registry...")
        push_cmd = f"docker push {registry_image}"
        result = self.ssh.execute(push_cmd)
        
        if result.returncode != 0:
            logger.warning(f"⚠️  Failed to push to registry: {result.stderr}")
        else:
            logger.info(f"✅ Image pushed to local registry")

        logger.info(f"✅ Image {method_name}:{tag} built successfully")
    
    def create_service(self, exp_name: str, method_name: str, dataset: str, tag: str = "latest", gpu_required: bool = True) -> str:
        """Create Docker service for experiment."""
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")
        
        # Create service using image from local registry (one-time task)
        registry_image = f"{manager_ip}:5000/{method_name}:{tag}"
        
        # Base service command
        service_cmd = f"docker service create --quiet -d --name {exp_name} --restart-condition none"
        
        # Add GPU constraints and resources if required
        if gpu_required:
            service_cmd += " --generic-resource NVIDIA-GPU=0"
            logger.info(f"🎮 Creating GPU-enabled service {exp_name}...")
        else:
            logger.info(f"🚀 Creating service {exp_name}...")
        
        # Add environment and mount options
        service_cmd += f" --env-file {self.config.remote_shared_dir}/methods/{method_name}/.env"
        service_cmd += f" --env DATA={self.config.remote_shared_dir}/datasets/{dataset}/"
        service_cmd += f" --env EXP={self.config.remote_shared_dir}/experiments/{exp_name}/"
        service_cmd += f" --mount type=bind,source={self.config.remote_shared_dir},target={self.config.remote_shared_dir}"
        service_cmd += f" {registry_image}"

        result = self.ssh.execute(service_cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create service {exp_name}: {result.stderr}")

        logger.info(f"✅ Service {exp_name} created successfully")
        return exp_name
    
    def get_service_logs(self, experiment_name: str, follow: bool = False, tee_file: str = None):
        """Show logs for an experiment/service."""
        logger.info(f"📋 Getting logs for experiment: {experiment_name}")
        
        # Check if service exists (active or completed)
        check_cmd = f"docker service ls --filter name={experiment_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)
        
        if not result.stdout.strip():
            raise ValueError(f"Service {experiment_name} not found")
        
        # Build logs command
        logs_cmd = f"docker service logs"
        if follow:
            logs_cmd += " -f"
            logger.info(f"📺 Following logs for {experiment_name} (press Ctrl+C to stop)...")
        else:
            logger.info(f"📋 Showing logs for {experiment_name}...")
            
        logs_cmd += f" {experiment_name}"
        
        # Build SSH command
        ssh_cmd = f"ssh -i {self.ssh.ssh_key} -p {self.ssh.ssh_port} -o StrictHostKeyChecking=no root@{self.ssh.manager_ip} '{logs_cmd}'"
        
        # Add local tee functionality if output file specified
        if tee_file:
            # Expand local path and ensure directory exists
            tee_path = Path(tee_file).expanduser().resolve()
            tee_path.parent.mkdir(parents=True, exist_ok=True)
            ssh_cmd += f" | tee {tee_path}"
            logger.info(f"💾 Saving logs to local file: {tee_path}")
        
        # Execute SSH command with local tee
        import subprocess
        logger.debug(f"Executing command: {ssh_cmd}")
        result = subprocess.run(ssh_cmd, shell=True, text=True)
        
        if not follow:
            if tee_file:
                logger.info(f"✅ Logs displayed and saved to {tee_file} for {experiment_name}")
            else:
                logger.info(f"✅ Logs displayed for {experiment_name}")
    
    def get_cluster_status(self):
        """Get Docker Swarm cluster status."""
        logger.info("📊 Cluster Status:")

        # Show swarm nodes
        result = self.ssh.execute("docker node ls")
        if result.returncode == 0:
            print("\n🖥️  Swarm Nodes:")
            print(result.stdout)