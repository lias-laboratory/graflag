"""Core GraFlag functionality."""

import os
import sys
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Dict, List
import logging

from .config import GraflagConfig
from .ssh import SSHManager
from .docker_ops import DockerManager

logger = logging.getLogger(__name__)


class GraFlagError(Exception):
    """Custom exception for GraFlag errors."""
    pass


class GraFlag:
    """Main GraFlag orchestration class."""

    def __init__(self, config_file: str = ".env"):
        """Initialize GraFlag with configuration."""
        try:
            self.config = GraflagConfig(config_file)
        except ValueError as e:
            raise GraFlagError(str(e))
        
        # Initialize SSH manager
        self.ssh = SSHManager(
            manager_ip=self.config.manager_ip,
            ssh_port=self.config.ssh_port,
            ssh_key=self.config.ssh_key
        )
        
        # Initialize Docker manager
        self.docker = DockerManager(self.ssh, self.config)

    def _load_method_env(self, method_name: str) -> Dict[str, str]:
        """Load method environment variables."""
        env_file_path = f"methods/{method_name}/.env"
        env_vars = {}

        if self.ssh.path_exists(self.config.remote_shared_dir, env_file_path):
            content = self.ssh.read_file(self.config.remote_shared_dir, env_file_path)
            for line in content.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()

        return env_vars

    def _create_experiment_dir(self, exp_name: str):
        """Create experiment directory."""
        exp_dir_path = f"experiments/{exp_name}"
        self.ssh.mkdir(self.config.remote_shared_dir, exp_dir_path)
        return exp_dir_path

    def copy_to_remote(self, local_paths, remote_path: str, recursive: bool = False):
        """Copy files/directories from local to remote shared directory."""
        # Construct remote destination - always within remote_shared_dir
        # Remove leading slash if present to ensure it's relative
        clean_remote_path = remote_path.lstrip('/')
        remote_dest = f"{self.config.remote_shared_dir}/{clean_remote_path}"
        
        return self.ssh.copy_to_remote(local_paths, remote_dest, recursive)

    def mount_nfs(self, shared_dir: str):
        """Mount NFS share on local machine."""
        logger.info("📁 Mounting NFS share on local machine...")

        # Create mount directory
        mount_dir = Path(shared_dir).expanduser()
        try:
            mount_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("⚠️  Stale NFS mount detected, cleaning up...")
            subprocess.run(
                f"sudo umount -l {mount_dir}", shell=True, capture_output=True
            )
            mount_dir.mkdir(parents=True, exist_ok=True)

        # Check if already mounted
        result = subprocess.run(f"mountpoint -q {mount_dir}", shell=True)
        if result.returncode == 0:
            logger.info(f"✅ NFS already mounted at {mount_dir}")
            return

        # Mount NFS
        mount_cmd = f"sudo mount -t nfs -o addr={self.config.manager_ip},port={self.config.nfs_port},vers=3,hard,intr,rsize=8192,wsize=8192,timeo=30,retrans=3 {self.config.manager_ip}:/tmp/shared {mount_dir}"

        result = subprocess.run(mount_cmd, shell=True)
        if result.returncode == 0:
            logger.info(f"✅ NFS mounted at {mount_dir}")
        else:
            raise GraFlagError(f"Failed to mount NFS at {mount_dir}")

    def setup(self):
        """Setup GraFlag cluster: initialize swarm and setup workers."""
        logger.info("🚀 Setting up GraFlag cluster...")

        try:
            # Setup swarm
            self.docker.setup_swarm_manager()
            token = self.docker.get_swarm_token()
            self.docker.setup_workers(token)
            self.docker.setup_local_registry()

            logger.info("✅ GraFlag cluster setup completed!")

            # Show cluster status
            self.status()

        except Exception as e:
            logger.error(f"❌ Setup failed: {e}")
            sys.exit(1)

    def status(self):
        """Show cluster status."""
        # Show Docker cluster status
        self.docker.get_cluster_status()

        # Show shared directory via SSH
        print(f"\n📁 Shared Directory: {self.config.remote_shared_dir}")
        shared_contents = self.ssh.list_dir(self.config.remote_shared_dir, "")
        if shared_contents:
            print("   Contents:")
            for item in shared_contents:
                print(f"     - {item}")
        else:
            print("   Status: ❌ Cannot access shared directory")

    def benchmark(
        self, method_name: str, dataset: str, tag: str = "latest", build: bool = False
    ):
        """Run benchmark experiment."""
        # Make parameters case-insensitive
        method_name = method_name.lower()
        dataset = dataset.lower()
        tag = tag.lower()
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        exp_name = f"exp__{method_name}__{dataset}__{timestamp}"

        logger.info(f"🧪 Running benchmark: {exp_name}")

        try:
            # Validate method exists
            method_path = f"methods/{method_name}"
            if not self.ssh.path_exists(self.config.remote_shared_dir, method_path):
                raise GraFlagError(
                    f"Method {method_name} not found in {self.config.remote_shared_dir}/{method_path}"
                )

            # Validate dataset exists
            dataset_path = f"datasets/{dataset}"
            if not self.ssh.path_exists(self.config.remote_shared_dir, dataset_path):
                raise GraFlagError(
                    f"Dataset {dataset} not found in {self.config.remote_shared_dir}/{dataset_path}"
                )

            # Build image if requested
            if build:
                self.docker.build_method_image(method_name, tag)

            # Create experiment directory
            exp_dir = self._create_experiment_dir(exp_name)
            logger.info(f"📁 Experiment directory: {self.config.remote_shared_dir}/{exp_dir}")

            # Load method environment
            method_env = self._load_method_env(method_name)

            # Create service
            self.docker.create_service(exp_name, method_name, dataset, tag)
            
            logger.info(f"📺 Following service logs (press Ctrl+C to stop)...")
            
            # Follow logs with output forwarded to terminal
            service_cmd = f"docker service logs -f {exp_name}"
            result = self.ssh.execute(service_cmd, capture_output=False)

            logger.info(f"📊 Monitor later with: docker service logs {exp_name}")

            return exp_name

        except Exception as e:
            logger.error(f"❌ Benchmark failed: {e}")
            sys.exit(1)

    def list_methods(self):
        """List available methods."""
        if not self.ssh.path_exists(self.config.remote_shared_dir, "methods"):
            logger.info("No methods directory found")
            return

        logger.info("📋 Available Methods:")
        methods = self.ssh.list_dir(self.config.remote_shared_dir, "methods")
        for method_name in methods:
            if self.ssh.path_exists(self.config.remote_shared_dir, f"methods/{method_name}/.env"):
                env_vars = self._load_method_env(method_name)
                supported_data = env_vars.get("SUPPORTED_DATA", "Unknown")
                print(f"  - {method_name} (Supports: {supported_data})")
            else:
                print(f"  - {method_name} (No .env file)")

    def list_datasets(self):
        """List available datasets."""
        if not self.ssh.path_exists(self.config.remote_shared_dir, "datasets"):
            logger.info("No datasets directory found")
            return

        logger.info("📋 Available Datasets:")
        datasets = self.ssh.list_dir(self.config.remote_shared_dir, "datasets")
        for dataset_name in datasets:
            print(f"  - {dataset_name}")

    def list_experiments(self):
        """List experiments."""
        if not self.ssh.path_exists(self.config.remote_shared_dir, "experiments"):
            logger.info("No experiments directory found")
            return

        logger.info("📋 Recent Experiments:")
        # Get experiments sorted by modification time (most recent first)
        result = self.ssh.execute(
            f"ls -1t {self.config.remote_shared_dir}/experiments/ 2>/dev/null | head -10 || true"
        )
        if result.returncode == 0 and result.stdout.strip():
            experiments = [
                exp.strip() for exp in result.stdout.strip().split("\n") if exp.strip()
            ]
            for exp_name in experiments:
                print(f"  - {exp_name}")
        else:
            logger.info("No experiments found")

    def logs(self, experiment_name: str, follow: bool = False, tee_file: str = None):
        """Show logs for an experiment/service."""
        try:
            self.docker.get_service_logs(experiment_name, follow, tee_file)
        except ValueError as e:
            raise GraFlagError(str(e))