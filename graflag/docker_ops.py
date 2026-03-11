"""Docker operations for GraFlag using Docker SDK."""

import json
import subprocess
import socket
import time
import yaml
from pathlib import Path
from typing import Dict, List, Optional
from enum import Enum
from datetime import datetime
import logging

import docker
from docker.types import ServiceMode, RestartPolicy, Resources, Mount, EndpointSpec

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
        return {var.value for var in cls}


class DockerManager:
    """Handle Docker Swarm operations via Docker SDK with SSH tunnel."""

    def __init__(self, ssh_manager, config, hosts_file: str = "hosts.yml"):
        self.ssh = ssh_manager
        self.config = config
        self.hosts_file = hosts_file
        self._client = None
        self._tunnel_proc = None
        self._tunnel_port = None

    @property
    def client(self) -> docker.DockerClient:
        """Lazy-initialize Docker client via SSH tunnel."""
        if self._client is None or (self._tunnel_proc and self._tunnel_proc.poll() is not None):
            self._connect()
        return self._client

    def _connect(self):
        """Establish SSH tunnel and create Docker client."""
        self.close()

        # Find free local port
        with socket.socket() as s:
            s.bind(('', 0))
            self._tunnel_port = s.getsockname()[1]

        # Build SSH tunnel command
        ssh_args = [
            'ssh', '-N',
            '-L', f'{self._tunnel_port}:/var/run/docker.sock',
            '-o', 'StrictHostKeyChecking=no',
            '-o', 'ExitOnForwardFailure=yes',
        ]
        if self.ssh.ssh_key:
            ssh_args.extend(['-i', str(Path(self.ssh.ssh_key).expanduser())])
        ssh_args.extend(['-p', str(self.ssh.ssh_port)])
        ssh_args.append(f'root@{self.ssh.manager_ip}')

        logger.debug(f"Starting SSH tunnel on port {self._tunnel_port}")
        self._tunnel_proc = subprocess.Popen(
            ssh_args, stdout=subprocess.DEVNULL, stderr=subprocess.PIPE
        )

        # Wait for tunnel to be ready
        for _ in range(20):
            time.sleep(0.3)
            if self._tunnel_proc.poll() is not None:
                stderr = self._tunnel_proc.stderr.read().decode()
                raise RuntimeError(f"SSH tunnel failed: {stderr}")
            try:
                with socket.socket() as s:
                    s.settimeout(1)
                    s.connect(('localhost', self._tunnel_port))
                break
            except (ConnectionRefusedError, OSError):
                continue
        else:
            self._tunnel_proc.terminate()
            raise RuntimeError("SSH tunnel failed to become ready")

        self._client = docker.DockerClient(
            base_url=f'tcp://localhost:{self._tunnel_port}',
            timeout=30
        )
        logger.debug("Docker client connected via SSH tunnel")

    def close(self):
        """Close Docker client and SSH tunnel."""
        if self._client:
            try:
                self._client.close()
            except Exception:
                pass
            self._client = None
        if self._tunnel_proc:
            self._tunnel_proc.terminate()
            try:
                self._tunnel_proc.wait(timeout=5)
            except subprocess.TimeoutExpired:
                self._tunnel_proc.kill()
            self._tunnel_proc = None

    def __del__(self):
        self.close()

    def _load_hosts(self) -> Dict:
        """Load hosts configuration from YAML file."""
        hosts_path = Path(self.hosts_file)
        if not hosts_path.exists():
            return {}
        with open(hosts_path, "r") as f:
            return yaml.safe_load(f) or {}

    # ========================================================================
    # Swarm Management
    # ========================================================================

    def setup_swarm_manager(self):
        """Initialize Docker Swarm on manager node."""
        logger.info("[SETUP] Initializing Docker Swarm on manager...")

        info = self.client.info()
        if info.get('Swarm', {}).get('LocalNodeState') == 'active':
            logger.info("[OK] Docker Swarm already initialized")
            return

        hosts = self._load_hosts()
        advertise_addr = hosts.get("manager", self.config.manager_ip)

        self.client.swarm.init(advertise_addr=advertise_addr)
        logger.info("[OK] Docker Swarm initialized")

    def get_swarm_token(self) -> str:
        """Get Docker Swarm worker join token."""
        swarm_attrs = self.client.swarm.attrs
        return swarm_attrs['JoinTokens']['Worker']

    def setup_workers(self, token: str):
        """Setup worker nodes to join the swarm (requires SSH to each worker)."""
        hosts = self._load_hosts()
        manager_ip = hosts.get("manager", self.config.manager_ip)
        workers = hosts.get("workers", [])

        if not workers:
            logger.warning("No workers defined in hosts.yml")
            return

        logger.info(f"[SETUP] Setting up {len(workers)} worker nodes...")

        for worker_ip in workers:
            check_cmd = (
                f"ssh -o StrictHostKeyChecking=no root@{worker_ip} "
                f"'docker info --format \"{{{{.Swarm.LocalNodeState}}}}\"'"
            )
            result = self.ssh.execute(check_cmd)

            if result.stdout.strip() == "active":
                logger.info(f"[OK] Worker {worker_ip} already in swarm")
                continue

            join_cmd = (
                f"ssh -o StrictHostKeyChecking=no root@{worker_ip} "
                f"'docker swarm join --token {token} {manager_ip}:2377'"
            )
            result = self.ssh.execute(join_cmd)

            if result.returncode == 0:
                logger.info(f"[OK] Worker {worker_ip} joined swarm")
            else:
                logger.error(f"[ERROR] Failed to join worker {worker_ip}: {result.stderr}")

    def get_nodes(self) -> List[Dict]:
        """Get list of swarm nodes."""
        nodes = []
        for node in self.client.nodes.list():
            attrs = node.attrs
            spec = attrs.get('Spec', {})
            status = attrs.get('Status', {})
            manager_status = attrs.get('ManagerStatus', {})

            nodes.append({
                'id': attrs.get('ID', ''),
                'hostname': attrs.get('Description', {}).get('Hostname', ''),
                'status': status.get('State', ''),
                'availability': spec.get('Availability', ''),
                'role': spec.get('Role', ''),
                'is_manager': bool(manager_status),
                'leader': manager_status.get('Leader', False),
            })
        return nodes

    # ========================================================================
    # Registry
    # ========================================================================

    def setup_local_registry(self):
        """Setup local Docker registry service on manager."""
        logger.info("[BUILD] Setting up local Docker registry...")

        existing = self.client.services.list(filters={'name': 'registry'})
        if existing:
            logger.info("[OK] Local registry already running")
            return

        self.client.services.create(
            image='registry:2',
            name='registry',
            mode=ServiceMode('replicated', replicas=1),
            endpoint_spec=EndpointSpec(ports={5000: (5000, 'tcp')}),
            mounts=[Mount(target='/var/lib/registry', source='registry-data', type='volume')],
            constraints=['node.role==manager'],
        )
        logger.info("[OK] Local registry started on port 5000")

    # ========================================================================
    # Image Build (SSH — build context is on remote host)
    # ========================================================================

    def build_method_image(self, method_name: str, tag: str = "latest") -> str:
        """Build method Docker image and push to local registry.

        Uses SSH because the build context resides on the remote host.

        Returns:
            Combined build and push log output.
        """
        method_name = method_name.lower()
        tag = tag.lower()
        logger.info(f"[BUILD] Building image {method_name}:{tag}...")

        local_image = f"{method_name}:{tag}"
        registry_image = f"{self.config.manager_ip}:5000/{method_name}:{tag}"

        build_log = []

        # Build image
        build_cmd = (
            f"docker build --network=host "
            f"-f {self.config.remote_shared_dir}/methods/{method_name}/Dockerfile "
            f"-t {local_image} -t {registry_image} "
            f"{self.config.remote_shared_dir}/"
        )
        result = self.ssh.execute(build_cmd)
        build_log.append(f"=== BUILD: {method_name}:{tag} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            raise RuntimeError(f"Failed to build image {method_name}:{tag}: {result.stderr}")

        # Push to registry
        logger.info(f"[INFO] Pushing {registry_image} to local registry...")
        result = self.ssh.execute(f"docker push {registry_image}")
        build_log.append(f"\n=== PUSH: {registry_image} ===\n")
        build_log.append(result.stdout or "")
        if result.stderr:
            build_log.append(result.stderr)

        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push to registry: {result.stderr}")
        else:
            logger.info("[OK] Image pushed to local registry")

        logger.info(f"[OK] Image {method_name}:{tag} built successfully")
        return "\n".join(build_log)

    def build_evaluator_image(self) -> str:
        """Build graflag-evaluator image and push to registry.

        Returns:
            Registry image path.
        """
        logger.info("[BUILD] Building graflag-evaluator image...")

        local_image = "graflag-evaluator:latest"
        registry_image = f"{self.config.manager_ip}:5000/graflag-evaluator:latest"

        # Check if image exists in registry
        check_cmd = f"docker manifest inspect {registry_image} > /dev/null 2>&1 && echo 'exists'"
        result = self.ssh.execute(check_cmd)

        if result.stdout.strip() == "exists":
            logger.info("[OK] Evaluator image already exists in registry")
            return registry_image

        # Build
        build_cmd = (
            f"cd {self.config.remote_shared_dir}/libs/graflag_evaluator && "
            f"docker build --network=host -t {local_image} -t {registry_image} ."
        )
        result = self.ssh.execute(build_cmd)
        if result.returncode != 0:
            raise RuntimeError(f"Failed to build evaluator image: {result.stderr}")

        # Push
        result = self.ssh.execute(f"docker push {registry_image}")
        if result.returncode != 0:
            logger.warning(f"[WARN] Failed to push evaluator to registry: {result.stderr}")
        else:
            logger.info("[OK] Evaluator image pushed to local registry")

        return registry_image

    # ========================================================================
    # Service Operations (Docker SDK)
    # ========================================================================

    def create_service(self, exp_name: str, method_name: str, dataset: str,
                       tag: str = "latest", gpu_required: bool = True,
                       method_params: dict = None) -> str:
        """Create Docker service for experiment."""
        method_params = method_params or {}
        registry_image = f"{self.config.manager_ip}:5000/{method_name}:{tag}"

        # Save service config
        self._save_service_config(exp_name, method_name, dataset, tag, gpu_required,
                                  method_params, registry_image)

        # Build environment variables
        env_vars = self._build_service_env(method_name, dataset, exp_name, method_params)

        # Mount shared directory
        shared_mount = Mount(
            target=self.config.remote_shared_dir,
            source=self.config.remote_shared_dir,
            type='bind'
        )

        # GPU resources
        resources = None
        if gpu_required:
            resources = Resources(
                generic_resources=[{
                    'DiscreteResourceSpec': {
                        'Kind': 'NVIDIA-GPU',
                        'Value': 0
                    }
                }]
            )
            logger.info(f"[INFO] Creating GPU-enabled service {exp_name}...")
        else:
            logger.info(f"[RUN] Creating service {exp_name}...")

        # Create service
        service = self.client.services.create(
            image=registry_image,
            name=exp_name,
            env=env_vars,
            mounts=[shared_mount],
            mode=ServiceMode('replicated', replicas=1),
            restart_policy=RestartPolicy(condition='none'),
            resources=resources,
            networks=['host'],
        )

        logger.info(f"[OK] Service {exp_name} created successfully")

        # Save service details
        self._save_service_details(exp_name, service.id)

        return exp_name

    def create_evaluation_service(self, experiment_name: str) -> str:
        """Create Docker service to run evaluation."""
        eval_service_name = f"eval__{experiment_name}"

        self._remove_service_if_exists(eval_service_name)
        logger.info(f"[INFO] Creating evaluation service: {eval_service_name}")

        registry_image = self.build_evaluator_image()

        service = self.client.services.create(
            image=registry_image,
            name=eval_service_name,
            args=[f"/shared/experiments/{experiment_name}"],
            mounts=[Mount(
                target='/shared',
                source=self.config.remote_shared_dir,
                type='bind'
            )],
            mode=ServiceMode('replicated', replicas=1),
            restart_policy=RestartPolicy(condition='none'),
            networks=['host'],
        )

        logger.info(f"[OK] Evaluation service {eval_service_name} created")
        return eval_service_name

    def list_services(self) -> List[Dict]:
        """List all Docker services."""
        services = []
        for svc in self.client.services.list():
            attrs = svc.attrs
            spec = attrs.get('Spec', {})
            mode = spec.get('Mode', {})

            # Desired replicas
            if 'Replicated' in mode:
                desired = mode['Replicated'].get('Replicas', 0)
            else:
                desired = 'global'

            # Image (strip sha256 digest for display)
            image = spec.get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image', '')
            if '@sha256:' in image:
                image = image.split('@')[0]

            # Running task count
            tasks = svc.tasks(filters={'desired-state': 'running'})
            running = sum(1 for t in tasks if t.get('Status', {}).get('State') == 'running')

            services.append({
                'name': spec.get('Name', ''),
                'id': attrs.get('ID', ''),
                'image': image,
                'replicas': f"{running}/{desired}" if isinstance(desired, int) else desired,
                'status': 'running' if running > 0 else 'pending',
            })
        return services

    def get_service_names(self) -> set:
        """Get set of all service names."""
        return {svc.name for svc in self.client.services.list()}

    def stop_service(self, service_name: str):
        """Stop and remove a service."""
        try:
            service = self.client.services.get(service_name)
            service.remove()
            logger.info(f"[OK] Service {service_name} stopped and removed")
        except docker.errors.NotFound:
            raise ValueError(f"Service {service_name} not found")
        except docker.errors.APIError as e:
            raise RuntimeError(f"Failed to stop service {service_name}: {e}")

    def _remove_service_if_exists(self, service_name: str) -> bool:
        """Remove a service if it exists."""
        try:
            service = self.client.services.get(service_name)
            service.remove()
            logger.info(f"[INFO] Removed existing service: {service_name}")
            return True
        except docker.errors.NotFound:
            return False

    def cleanup_finished_service(self, service_name: str):
        """Remove a finished service (safe if it doesn't exist)."""
        try:
            service = self.client.services.get(service_name)
            service.remove()
            logger.info(f"[INFO] Cleaned up finished service: {service_name}")
        except docker.errors.NotFound:
            pass
        except docker.errors.APIError as e:
            logger.warning(f"[WARN] Failed to clean up service {service_name}: {e}")

    def remove_evaluation_service(self, experiment_name: str):
        """Remove evaluation service for an experiment."""
        eval_service_name = f"eval__{experiment_name}"
        if self._remove_service_if_exists(eval_service_name):
            logger.info(f"[OK] Evaluation service {eval_service_name} removed")

    def get_service_logs(self, service_name: str, tail: int = 100) -> List[str]:
        """Get recent logs for a service.

        Uses SSH + Docker CLI because the Docker SDK log streaming
        is unreliable for swarm services.
        """
        if not self.service_exists(service_name):
            return []

        result = self.ssh.execute(
            f"docker service logs --tail {tail} {service_name} 2>&1"
        )
        if result.returncode == 0 and result.stdout.strip():
            return [line for line in result.stdout.strip().split('\n') if line.strip()]
        return []

    def follow_service_logs(self, service_name: str):
        """Follow service logs in real-time until task finishes.

        Uses SSH + Docker CLI subprocess because the Docker SDK's
        follow mode does not stream reliably for swarm services.
        """
        import time as _time

        if not self.service_exists(service_name):
            raise ValueError(f"Service {service_name} not found")

        logger.info("[INFO] Following service logs (press Ctrl+C to stop)...")

        # Build SSH command for log following
        ssh_args = ['ssh']
        if self.ssh.ssh_key:
            ssh_args.extend(['-i', str(Path(self.ssh.ssh_key).expanduser())])
        ssh_args.extend([
            '-p', str(self.ssh.ssh_port),
            '-o', 'StrictHostKeyChecking=no',
            f'root@{self.ssh.manager_ip}',
            f'docker service logs -f {service_name}',
        ])

        proc = subprocess.Popen(ssh_args)

        try:
            # Poll task state until it finishes
            while proc.poll() is None:
                _time.sleep(3)
                tasks = []
                try:
                    svc = self.client.services.get(service_name)
                    tasks = svc.tasks(filters={'desired-state': 'shutdown'})
                except Exception:
                    pass
                for task in tasks:
                    state = task.get('Status', {}).get('State', '').lower()
                    if state in ('complete', 'failed', 'shutdown', 'rejected'):
                        _time.sleep(2)  # Let final logs flush
                        proc.terminate()
                        proc.wait(timeout=5)
                        return
        except KeyboardInterrupt:
            proc.terminate()
            proc.wait(timeout=5)
            logger.info("[INFO] Log following interrupted")
        except Exception:
            proc.terminate()
            proc.wait(timeout=5)
            raise

    def service_exists(self, service_name: str) -> bool:
        """Check if a Docker service exists."""
        try:
            self.client.services.get(service_name)
            return True
        except docker.errors.NotFound:
            return False

    def is_service_failed(self, service_name: str) -> bool:
        """Check if a service exists but all its tasks have failed."""
        try:
            svc = self.client.services.get(service_name)
            tasks = svc.tasks()
            if not tasks:
                return False
            # Check if every task is in a terminal failure state
            for task in tasks:
                state = task.get('Status', {}).get('State', '').lower()
                if state not in ('failed', 'rejected', 'shutdown', 'orphaned'):
                    return False
            return True
        except docker.errors.NotFound:
            return False

    # ========================================================================
    # Cluster Status
    # ========================================================================

    def get_cluster_status(self) -> Dict:
        """Get Docker Swarm cluster status."""
        info = self.client.info()
        swarm = info.get('Swarm', {})

        return {
            'swarm_active': swarm.get('LocalNodeState') == 'active',
            'nodes': self.get_nodes(),
            'services': self.list_services(),
        }

    # ========================================================================
    # Internal
    # ========================================================================

    def _build_service_env(self, method_name, dataset, exp_name, method_params):
        """Build environment variable list for service."""
        method_env = load_method_env(self.ssh, self.config.remote_shared_dir, method_name)

        # Build env dict (method .env values as base)
        env_dict = dict(method_env)

        # Add required env vars
        env_dict['METHOD_NAME'] = method_name
        env_dict['DATA'] = f"{self.config.remote_shared_dir}/datasets/{dataset}/"
        env_dict['EXP'] = f"{self.config.remote_shared_dir}/experiments/{exp_name}/"

        # Override with method params (prefixed with _)
        reserved = ReservedEnvVars.get_names()
        for key, value in method_params.items():
            if key.upper() not in reserved:
                env_dict[f"_{key}"] = value
                logger.info(f"   Setting parameter: _{key}={value}")

        return [f"{k}={v}" for k, v in env_dict.items()]

    def _save_service_config(self, exp_name, method_name, dataset, tag,
                             gpu_required, method_params, registry_image):
        """Save service configuration to JSON."""
        env_contents = load_method_env(self.ssh, self.config.remote_shared_dir, method_name)

        reserved = ReservedEnvVars.get_names()
        for key, value in method_params.items():
            if key.upper() not in reserved:
                env_contents[key] = value

        service_config = {
            "experiment_name": exp_name,
            "method_name": method_name,
            "dataset": dataset,
            "tag": tag,
            "gpu_required": gpu_required,
            "registry_image": registry_image,
            "manager_ip": self.config.manager_ip,
            "timestamp": datetime.now().isoformat(),
            "data_path": f"{self.config.remote_shared_dir}/datasets/{dataset}/",
            "exp_path": f"{self.config.remote_shared_dir}/experiments/{exp_name}/",
            "env_contents": env_contents,
        }

        config_file = f"{self.config.remote_shared_dir}/experiments/{exp_name}/service_config.json"
        config_json = json.dumps(service_config, indent=2)
        self.ssh.execute(f"cat > {config_file} << 'EOF'\n{config_json}\nEOF")
        logger.info(f"[INFO] Saved service configuration to {config_file}")

    def _save_service_details(self, exp_name, service_id):
        """Save service details to JSON after creation."""
        try:
            service = self.client.services.get(exp_name)
            attrs = service.attrs

            details = {
                "service_id": service_id,
                "service_name": exp_name,
                "created_at": datetime.now().isoformat(),
                "image": attrs.get('Spec', {}).get('TaskTemplate', {}).get('ContainerSpec', {}).get('Image'),
                "created_at_docker": attrs.get('CreatedAt'),
            }

            # Get task info
            tasks = service.tasks()
            if tasks:
                task = tasks[0]
                details["worker"] = {
                    "node_id": task.get('NodeID', ''),
                    "task_id": task.get('ID', ''),
                    "state": task.get('Status', {}).get('State', ''),
                    "desired_state": task.get('DesiredState', ''),
                }

            details_file = f"{self.config.remote_shared_dir}/experiments/{exp_name}/service_details.json"
            details_json = json.dumps(details, indent=2)
            self.ssh.execute(f"cat > {details_file} << 'EOF'\n{details_json}\nEOF")
            logger.info(f"[INFO] Saved service details to {details_file}")
        except Exception as e:
            logger.warning(f"[WARN] Failed to save service details: {e}")
