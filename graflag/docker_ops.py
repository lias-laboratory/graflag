"""Docker operations for GraFlag."""

import time
import yaml
from pathlib import Path
from typing import Dict
from enum import Enum
import logging

from .utils import load_method_env

logger = logging.getLogger(__name__)


class ReservedEnvVars(Enum):
    """Reserved environment variable names that should not be overridden by method parameters."""
    DATA = 'DATA'
    EXP = 'EXP'
    METHOD_NAME = 'METHOD_NAME'
    COMMAND = 'COMMAND'
    MONITOR_INTERVAL = 'MONITOR_INTERVAL'
    
    @classmethod
    def get_names(cls):
        """Get set of all reserved variable names."""
        return {var.value for var in cls}


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
        logger.info("[BUILD] Setting up local Docker registry...")
        
        # Check if registry is already running
        result = self.ssh.execute("docker ps -a --filter name=registry --format '{{.Names}}'")
        if "registry" in result.stdout:
            logger.info("[OK] Local registry already running")
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
        
        logger.info("[OK] Local registry started on port 5000")
    
    def setup_swarm_manager(self):
        """Initialize Docker Swarm on manager node."""
        logger.info("[SETUP] Initializing Docker Swarm on manager...")

        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")

        # Check if swarm is already initialized
        result = self.ssh.execute("docker info --format '{{.Swarm.LocalNodeState}}'")
        if result.stdout.strip() == "active":
            logger.info("[OK] Docker Swarm already initialized on manager")
            return

        # Initialize swarm
        swarm_cmd = f"docker swarm init --advertise-addr {manager_ip}"
        result = self.ssh.execute(swarm_cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to initialize swarm: {result.stderr}")

        logger.info("[OK] Docker Swarm initialized on manager")
    
    def setup_workers(self, token: str):
        """Setup worker nodes to join the swarm."""
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")

        workers = hosts.get("workers", [])

        if not workers:
            logger.warning("No workers defined in hosts.yml")
            return

        logger.info(f"[SETUP] Setting up {len(workers)} worker nodes...")

        for worker_ip in workers:
            logger.info(f"Setting up worker {worker_ip}...")

            # Check if worker is already in swarm
            check_cmd = f"ssh -o StrictHostKeyChecking=no root@{worker_ip} 'docker info --format \"{{{{.Swarm.LocalNodeState}}}}\"'"
            result = self.ssh.execute(check_cmd)

            if result.stdout.strip() == "active":
                logger.info(f"[OK] Worker {worker_ip} already in swarm")
                continue

            # Join worker to swarm
            join_cmd = f"ssh -o StrictHostKeyChecking=no root@{worker_ip} 'docker swarm join --token {token} {manager_ip}:2377'"
            result = self.ssh.execute(join_cmd)

            if result.returncode == 0:
                logger.info(f"[OK] Worker {worker_ip} joined swarm")
            else:
                logger.error(f"[ERROR] Failed to join worker {worker_ip}: {result.stderr}")
    
    def label_gpu_nodes(self):
        """Label all nodes with GPU support."""
        logger.info("[INFO] Labeling all nodes with GPU support...")
        
        # Get node IDs and label them
        result = self.ssh.execute("docker node ls -q")
        if result.returncode != 0:
            logger.error(f"[ERROR] Failed to get node list: {result.stderr}")
            return
        
        node_ids = result.stdout.strip().split('\n')
        for node_id in node_ids:
            if node_id.strip():
                self.ssh.execute(f"docker node update --label-add gpu=true {node_id}")
        
        logger.info("[OK] All nodes labeled with gpu=true")
    
    def build_method_image(self, method_name: str, tag: str = "latest") -> str:
        """Build method Docker image and push to local registry.

        Returns:
            Combined build and push log output.
        """
        # Make parameters case-insensitive
        method_name = method_name.lower()
        tag = tag.lower()

        logger.info(f"[BUILD] Building image {method_name}:{tag}...")

        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")

        local_image = f"{method_name}:{tag}"
        registry_image = f"{manager_ip}:5000/{method_name}:{tag}"

        build_log = []

        # Build image with both tags
        # Build context is shared/ to access both methods/ and libs/
        build_cmd = f"docker build --network=host -f {self.config.remote_shared_dir}/methods/{method_name}/Dockerfile -t {local_image} -t {registry_image} {self.config.remote_shared_dir}/"
        result = self.ssh.execute(build_cmd)
        build_log.append(f"=== BUILD: {method_name}:{tag} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to build image {method_name}:{tag}: {result.stderr}")

        # Push to local registry
        logger.info(f"[INFO] Pushing {registry_image} to local registry...")
        push_cmd = f"docker push {registry_image}"
        result = self.ssh.execute(push_cmd)
        build_log.append(f"\n=== PUSH: {registry_image} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push to registry: {result.stderr}")
        else:
            logger.info(f"[OK] Image pushed to local registry")

        logger.info(f"[OK] Image {method_name}:{tag} built successfully")
        return "\n".join(build_log)
    
    def build_evaluator_image(self) -> str:
        """Build graflag-evaluator Docker image and push to local registry.
        
        Returns:
            Registry image path for the evaluator
        """
        logger.info("[BUILD] Building graflag-evaluator image...")
        
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")
        
        local_image = "graflag-evaluator:latest"
        registry_image = f"{manager_ip}:5000/graflag-evaluator:latest"
        
        # Check if image exists in registry
        check_cmd = f"docker manifest inspect {registry_image} > /dev/null 2>&1 && echo 'exists'"
        result = self.ssh.execute(check_cmd)
        
        if result.stdout.strip() == "exists":
            logger.info("[OK] Evaluator image already exists in registry")
            return registry_image
        
        # Build evaluator image with both tags
        build_cmd = f"cd {self.config.remote_shared_dir}/libs/graflag_evaluator && docker build --network=host -t {local_image} -t {registry_image} ."
        result = self.ssh.execute(build_cmd)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build evaluator image: {result.stderr}")
        
        # Push to local registry
        logger.info(f"[INFO] Pushing {registry_image} to local registry...")
        push_cmd = f"docker push {registry_image}"
        result = self.ssh.execute(push_cmd)

        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push evaluator to registry: {result.stderr}")
        else:
            logger.info(f"[OK] Evaluator image pushed to local registry")

        logger.info("[OK] Evaluator image built successfully")
        return registry_image
    
    def create_service(self, exp_name: str, method_name: str, dataset: str, tag: str = "latest", 
                      gpu_required: bool = True, method_params: dict = None) -> str:
        """Create Docker service for experiment.
        
        Args:
            exp_name: Name of the experiment/service
            method_name: Name of the method
            dataset: Name of the dataset
            tag: Docker image tag
            gpu_required: Whether GPU support is required
            method_params: Dictionary of method-specific parameters (excluding DATA and EXP)
        """
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager")
        method_params = method_params or {}
        
        # Create service using image from local registry (one-time task)
        registry_image = f"{manager_ip}:5000/{method_name}:{tag}"
        
        # Save service configuration before launching
        self._save_service_config(exp_name, method_name, dataset, tag, gpu_required, 
                                   method_params, registry_image, manager_ip)
        
        # Base service command
        service_cmd = f"docker service create --quiet -d --name {exp_name} --restart-condition none"
        
        # Use host's network for DNS resolution (allows internet access like worker containers)
        service_cmd += " --network host"
        
        # Add GPU constraints and resources if required
        if gpu_required:
            service_cmd += " --generic-resource NVIDIA-GPU=0"
            logger.info(f"[INFO] Creating GPU-enabled service {exp_name}...")
        else:
            logger.info(f"[RUN] Creating service {exp_name}...")
        
        # Add environment and mount options
        service_cmd += f" --env-file {self.config.remote_shared_dir}/methods/{method_name}/.env"
        service_cmd += f" --env METHOD_NAME={method_name}"  # For logging utilities
        service_cmd += f" --env DATA={self.config.remote_shared_dir}/datasets/{dataset}/"
        service_cmd += f" --env EXP={self.config.remote_shared_dir}/experiments/{exp_name}/"
        
        # Add method-specific parameters as environment variables
        # Filter out reserved variables
        for key, value in method_params.items():
            if key.upper() not in ReservedEnvVars.get_names():
                service_cmd += f" --env _{key}={value}"
                logger.info(f"   Setting parameter: _{key}={value}")
        
        service_cmd += f" --mount type=bind,source={self.config.remote_shared_dir},target={self.config.remote_shared_dir}"
        service_cmd += f" {registry_image}"

        result = self.ssh.execute(service_cmd)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to create service {exp_name}: {result.stderr}")

        logger.info(f"[OK] Service {exp_name} created successfully")
        
        # Save service details after creation
        service_id = result.stdout.strip()
        self._save_service_details(exp_name, service_id)
        
        return exp_name
    
    def create_evaluation_service(self, experiment_name: str) -> str:
        """Create Docker service to run evaluation for an experiment.
        
        Args:
            experiment_name: Name of the experiment to evaluate
            
        Returns:
            Service name for the evaluation task
        """
        eval_service_name = f"eval__{experiment_name}"
        
        # Remove existing evaluation service if it exists (from previous run)
        self._remove_service_if_exists(eval_service_name)
        
        logger.info(f"[INFO] Creating evaluation service: {eval_service_name}")
        
        # Build evaluator image if needed and get registry path
        registry_image = self.build_evaluator_image()
        
        # Create one-time evaluation service
        service_cmd = f"docker service create --quiet -d --name {eval_service_name} --restart-condition none"
        
        # Use host network
        service_cmd += " --network host"
        
        # Mount shared directory
        service_cmd += f" --mount type=bind,source={self.config.remote_shared_dir},target=/shared"
        
        # Use evaluator image from registry with experiment path as argument
        service_cmd += f" {registry_image} /shared/experiments/{experiment_name}"
        
        result = self.ssh.execute(service_cmd)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to create evaluation service: {result.stderr}")
        
        logger.info(f"[OK] Evaluation service {eval_service_name} created successfully")
        
        return eval_service_name
    
    def _remove_service_if_exists(self, service_name: str) -> bool:
        """Remove a service if it exists.
        
        Args:
            service_name: Name of the service to remove
            
        Returns:
            True if service was removed, False if it didn't exist
        """
        check_cmd = f"docker service ls --filter name={service_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)
        
        if result.stdout.strip() == service_name:
            logger.info(f"[INFO] Removing existing service: {service_name}")
            remove_cmd = f"docker service rm {service_name}"
            self.ssh.execute(remove_cmd)
            return True
        return False
    
    def cleanup_finished_service(self, service_name: str):
        """Remove a Docker service after it has finished running.

        Safe to call even if the service no longer exists.
        """
        check_cmd = f"docker service ls --filter name={service_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)

        if result.stdout.strip() == service_name:
            remove_cmd = f"docker service rm {service_name}"
            result = self.ssh.execute(remove_cmd)
            if result.returncode == 0:
                logger.info(f"[INFO] Cleaned up finished service: {service_name}")
            else:
                logger.warning(f"[WARN] Failed to clean up service {service_name}: {result.stderr}")

    def remove_evaluation_service(self, experiment_name: str):
        """Remove evaluation service for an experiment.
        
        Args:
            experiment_name: Name of the experiment
        """
        eval_service_name = f"eval__{experiment_name}"
        if self._remove_service_if_exists(eval_service_name):
            logger.info(f"[OK] Evaluation service {eval_service_name} removed")
    
    def _save_service_config(self, exp_name: str, method_name: str, dataset: str, tag: str,
                            gpu_required: bool, method_params: dict, registry_image: str, 
                            manager_ip: str) -> None:
        """Save service configuration to JSON before launching.
        
        Args:
            exp_name: Experiment name
            method_name: Method name
            dataset: Dataset name
            tag: Docker image tag
            gpu_required: Whether GPU is required
            method_params: Optional method-specific parameters passed by user
            registry_image: Registry image path
            manager_ip: Manager IP address
        """
        import json
        from datetime import datetime
        
        # Load .env file contents using utility function
        env_file_path = f"{self.config.remote_shared_dir}/methods/{method_name}/.env"
        env_contents = load_method_env(self.ssh, self.config.remote_shared_dir, method_name)
        
        # Override env_contents with method_params (same behavior as in create_service)
        # Filter out reserved variables
        for key, value in method_params.items():
            if key.upper() not in ReservedEnvVars.get_names():
                env_contents[key] = value
        
        # Prepare service configuration for logging
        service_config = {
            "experiment_name": exp_name,
            "method_name": method_name,
            "dataset": dataset,
            "tag": tag,
            "gpu_required": gpu_required,
            "registry_image": registry_image,
            "manager_ip": manager_ip,
            "timestamp": datetime.now().isoformat(),
            "data_path": f"{self.config.remote_shared_dir}/datasets/{dataset}/",
            "exp_path": f"{self.config.remote_shared_dir}/experiments/{exp_name}/",
            "env_file_path": env_file_path,
            "env_contents": env_contents  # Parameters from .env with method_params overrides
        }
        
        # Save service configuration to JSON file before launching
        config_file = f"{self.config.remote_shared_dir}/experiments/{exp_name}/service_config.json"
        config_json = json.dumps(service_config, indent=2)
        # Use cat with heredoc to avoid quote escaping issues
        save_config_cmd = f"cat > {config_file} << 'EOF'\n{config_json}\nEOF"
        self.ssh.execute(save_config_cmd)
        logger.info(f"[INFO] Saved service configuration to {config_file}")
    
    def _save_service_details(self, exp_name: str, service_id: str) -> None:
        """Save service details to JSON after creation."""
        import json
        
        service_details = self._get_service_details(exp_name, service_id)
        
        # Save service details to JSON file
        details_file = f"{self.config.remote_shared_dir}/experiments/{exp_name}/service_details.json"
        details_json = json.dumps(service_details, indent=2)
        # Use cat with heredoc to avoid quote escaping issues
        save_details_cmd = f"cat > {details_file} << 'EOF'\n{details_json}\nEOF"
        self.ssh.execute(save_details_cmd)
        logger.info(f"[INFO] Saved service details to {details_file}")
    
    def _get_service_details(self, exp_name: str, service_id: str) -> dict:
        """Get detailed service information including worker node."""
        import json
        from datetime import datetime
        
        # Get service inspect output
        inspect_cmd = f"docker service inspect {exp_name}"
        result = self.ssh.execute(inspect_cmd)
        
        service_details = {
            "service_id": service_id,
            "service_name": exp_name,
            "created_at": datetime.now().isoformat()
        }
        
        if result.returncode == 0:
            try:
                inspect_data = json.loads(result.stdout)
                if inspect_data and len(inspect_data) > 0:
                    service_info = inspect_data[0]
                    
                    # Extract key information
                    service_details.update({
                        "image": service_info.get("Spec", {}).get("TaskTemplate", {}).get("ContainerSpec", {}).get("Image"),
                        "created_at_docker": service_info.get("CreatedAt"),
                        "updated_at": service_info.get("UpdatedAt"),
                        "version": service_info.get("Version", {}).get("Index"),
                        "endpoint": service_info.get("Endpoint", {}),
                        "replicas": service_info.get("Spec", {}).get("Mode", {}),
                        "resources": service_info.get("Spec", {}).get("TaskTemplate", {}).get("Resources", {}),
                        "restart_policy": service_info.get("Spec", {}).get("TaskTemplate", {}).get("RestartPolicy", {}),
                        "placement": service_info.get("Spec", {}).get("TaskTemplate", {}).get("Placement", {}),
                    })
            except json.JSONDecodeError:
                logger.warning("Failed to parse service inspect output")
        
        # Get worker/node information where the service is running
        ps_cmd = f"docker service ps {exp_name} --format '{{{{json .}}}}'"
        ps_result = self.ssh.execute(ps_cmd)
        logger.info(ps_result.stdout.strip())
        
        if ps_result.returncode == 0 and ps_result.stdout.strip():
            try:
                # Parse first task (current running task)
                task_lines = ps_result.stdout.strip().split('\n')
                if task_lines:
                    task_info = json.loads(task_lines[0])
                    service_details["worker"] = {
                        "node": task_info.get("Node"),
                        "task_id": task_info.get("ID"),
                        "task_name": task_info.get("Name"),
                        "current_state": task_info.get("CurrentState"),
                        "desired_state": task_info.get("DesiredState"),
                        "error": task_info.get("Error", "")
                    }
            except json.JSONDecodeError:
                logger.warning("Failed to parse service ps output")
        
        return service_details
    
    def follow_service_logs(self, experiment_name: str):
        """Follow service logs in real-time, exiting when the task finishes.

        Args:
            experiment_name: Name of the experiment/service
        """
        import subprocess as sp

        # Check if service exists
        check_cmd = f"docker service ls --filter name={experiment_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)

        if not result.stdout.strip():
            raise ValueError(f"Service {experiment_name} not found")

        logger.info(f"[INFO] Following service logs (press Ctrl+C to stop)...")

        # Build SSH command for log following
        ssh_opts = f"-i {self.ssh.ssh_key} -p {self.ssh.ssh_port} -o StrictHostKeyChecking=no"
        logs_ssh_cmd = f"ssh {ssh_opts} root@{self.ssh.manager_ip} 'docker service logs -f {experiment_name}'"

        # Start log following in background
        proc = sp.Popen(logs_ssh_cmd, shell=True)

        try:
            # Poll task state until it finishes
            while proc.poll() is None:
                time.sleep(3)
                ps_result = self.ssh.execute(
                    f"docker service ps {experiment_name} --format '{{{{.CurrentState}}}}' --filter desired-state=shutdown"
                )
                state = ps_result.stdout.strip().lower()
                if state and ("complete" in state or "failed" in state or "shutdown" in state):
                    # Task finished -- wait a moment for final logs to flush, then stop
                    time.sleep(2)
                    proc.terminate()
                    proc.wait(timeout=5)
                    break
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("[INFO] Log following interrupted")
        except Exception:
            proc.terminate()
            proc.wait(timeout=5)
            raise
    
    def get_service_logs(self, experiment_name: str, follow: bool = False, tee_file: str = None):
        """Show logs for an experiment/service."""
        logger.info(f"[INFO] Getting logs for experiment: {experiment_name}")
        
        # Check if service exists (active or completed)
        check_cmd = f"docker service ls --filter name={experiment_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)
        
        if not result.stdout.strip():
            raise ValueError(f"Service {experiment_name} not found")
        
        # Build logs command
        logs_cmd = f"docker service logs"
        if follow:
            logs_cmd += " -f"
            logger.info(f"[INFO] Following logs for {experiment_name} (press Ctrl+C to stop)...")
        else:
            logger.info(f"[INFO] Showing logs for {experiment_name}...")
            
        logs_cmd += f" {experiment_name}"
        
        # Build SSH command
        ssh_cmd = f"ssh -i {self.ssh.ssh_key} -p {self.ssh.ssh_port} -o StrictHostKeyChecking=no root@{self.ssh.manager_ip} '{logs_cmd}'"
        
        # Add local tee functionality if output file specified
        if tee_file:
            # Expand local path and ensure directory exists
            tee_path = Path(tee_file).expanduser().resolve()
            tee_path.parent.mkdir(parents=True, exist_ok=True)
            ssh_cmd += f" | tee {tee_path}"
            logger.info(f"[INFO] Saving logs to local file: {tee_path}")
        
        # Execute SSH command with local tee
        import subprocess
        logger.debug(f"Executing command: {ssh_cmd}")
        result = subprocess.run(ssh_cmd, shell=True, text=True)
        
        if not follow:
            if tee_file:
                logger.info(f"[OK] Logs displayed and saved to {tee_file} for {experiment_name}")
            else:
                logger.info(f"[OK] Logs displayed for {experiment_name}")
    
    def stop_service(self, experiment_name: str):
        """Stop and remove a running service/experiment."""
        logger.info(f"[STOP] Stopping service: {experiment_name}")
        
        # Check if service exists
        check_cmd = f"docker service ls --filter name={experiment_name} --format '{{{{.Name}}}}'"
        result = self.ssh.execute(check_cmd)
        
        if not result.stdout.strip():
            raise ValueError(f"Service {experiment_name} not found")
        
        # Remove the service
        remove_cmd = f"docker service rm {experiment_name}"
        result = self.ssh.execute(remove_cmd)
        
        if result.returncode != 0:
            raise RuntimeError(f"Failed to stop service {experiment_name}: {result.stderr}")
        
        logger.info(f"[OK] Service {experiment_name} stopped and removed")
    
    def get_running_services(self):
        """Get list of running services/experiments."""
        result = self.ssh.execute("docker service ls --format '{{.Name}}\t{{.Mode}}\t{{.Replicas}}\t{{.Image}}'")
        if result.returncode == 0 and result.stdout.strip():
            print("\n[INFO] Running Services/Experiments:")
            print(f"{'NAME':<50} {'MODE':<15} {'REPLICAS':<15} {'IMAGE':<30}")
            print("-" * 110)
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    parts = line.split('\t')
                    if len(parts) >= 4:
                        name = parts[0][:49]
                        mode = parts[1][:14]
                        replicas = parts[2][:14]
                        image = parts[3][:29]
                        print(f"{name:<50} {mode:<15} {replicas:<15} {image:<30}")
        else:
            print("\n[INFO] Running Services/Experiments: None")
    
    def get_cluster_status(self):
        """Get Docker Swarm cluster status."""
        logger.info("[INFO] Cluster Status:")

        # Show swarm nodes
        result = self.ssh.execute("docker node ls")
        if result.returncode == 0:
            print("\n[INFO] Swarm Nodes:")
            print(result.stdout)
        
        # Show running services
        self.get_running_services()