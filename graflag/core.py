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
from .utils import load_method_env

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
        return load_method_env(self.ssh, self.config.remote_shared_dir, method_name)

    def _create_experiment_dir(self, exp_name: str):
        """Create experiment directory."""
        exp_dir_path = f"experiments/{exp_name}"
        self.ssh.mkdir(self.config.remote_shared_dir, exp_dir_path)
        return exp_dir_path

    def _write_status(self, exp_dir: str, status: str, error: str = None):
        """Write status.json to an experiment directory on the remote."""
        import json
        data = {"status": status, "timestamp": datetime.now().isoformat()}
        if error:
            data["error"] = error
        status_data = json.dumps(data)
        status_path = f"{self.config.remote_shared_dir}/{exp_dir}/status.json"
        # Use heredoc to avoid quote escaping issues with SSH
        self.ssh.execute(f"cat > {status_path} << 'STATUSEOF'\n{status_data}\nSTATUSEOF")

    def copy_files(self, source_paths, dest_path: str, recursive: bool = False, from_remote: bool = False):
        """
        Copy files/directories bidirectionally.
        
        Args:
            source_paths: Source path(s)
            dest_path: Destination path
            recursive: Include recursive flag
            from_remote: If True, copy from remote to local; if False, copy from local to remote
        """
        if from_remote:
            # When copying from remote, source paths should be relative to remote_shared_dir
            remote_sources = []
            for src in (source_paths if isinstance(source_paths, list) else [source_paths]):
                clean_src = src.lstrip('/')
                remote_sources.append(f"{self.config.remote_shared_dir}/{clean_src}")
            return self.ssh.copy_files(remote_sources, dest_path, recursive, from_remote=True)
        else:
            # When copying to remote, dest is relative to remote_shared_dir
            clean_dest = dest_path.lstrip('/')
            remote_dest = f"{self.config.remote_shared_dir}/{clean_dest}"
            return self.ssh.copy_files(source_paths, remote_dest, recursive, from_remote=False)

    def mount_nfs(self, shared_dir: str):
        """Mount NFS share on local machine."""
        logger.info("[INFO] Mounting NFS share on local machine...")

        # Create mount directory
        mount_dir = Path(shared_dir).expanduser()
        try:
            mount_dir.mkdir(parents=True, exist_ok=True)
        except OSError as e:
            logger.warning("[WARN] Stale NFS mount detected, cleaning up...")
            subprocess.run(
                f"sudo umount -l {mount_dir}", shell=True, capture_output=True
            )
            mount_dir.mkdir(parents=True, exist_ok=True)

        # Check if already mounted
        result = subprocess.run(f"mountpoint -q {mount_dir}", shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS already mounted at {mount_dir}")
            return

        # Mount NFS
        mount_cmd = f"sudo mount -t nfs -o addr={self.config.manager_ip},port={self.config.nfs_port},vers=3,hard,intr,rsize=8192,wsize=8192,timeo=30,retrans=3 {self.config.manager_ip}:/tmp/shared {mount_dir}"

        result = subprocess.run(mount_cmd, shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS mounted at {mount_dir}")
        else:
            raise GraFlagError(f"Failed to mount NFS at {mount_dir}")

    def setup(self):
        """Setup GraFlag cluster: initialize swarm and setup workers."""
        logger.info("[SETUP] Setting up GraFlag cluster...")

        try:
            # Setup swarm
            self.docker.setup_swarm_manager()
            token = self.docker.get_swarm_token()
            self.docker.setup_workers(token)
            self.docker.setup_local_registry()

            logger.info("[OK] GraFlag cluster setup completed!")

            # Show cluster status
            self.status()

        except Exception as e:
            logger.error(f"[ERROR] Setup failed: {e}")
            sys.exit(1)

    def status(self):
        """Show cluster status."""
        # Show Docker cluster status
        self.docker.get_cluster_status()

        # Show shared directory via SSH
        print(f"\n[INFO] Shared Directory: {self.config.remote_shared_dir}")
        shared_contents = self.ssh.list_dir(self.config.remote_shared_dir, "")
        if shared_contents:
            print("   Contents:")
            for item in shared_contents:
                print(f"     - {item}")
        else:
            print("   Status: [ERROR] Cannot access shared directory")

    def benchmark(
        self, method_name: str, dataset: str, tag: str = "latest", build: bool = False, 
        gpu: bool = True, method_params: dict = None
    ):
        """Run benchmark experiment.
        
        Args:
            method_name: Name of the method to run
            dataset: Name of the dataset to use
            tag: Docker image tag
            build: Whether to build the image before running
            gpu: Whether to enable GPU support
            method_params: Dictionary of method-specific parameters (excluding DATA and EXP)
        """
        # Make parameters case-insensitive
        method_name = method_name.lower()
        dataset = dataset.lower()
        tag = tag.lower()
        method_params = method_params or {}
        
        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        exp_name = f"exp__{method_name}__{dataset}__{timestamp}"

        logger.info(f"[RUN] Running benchmark: {exp_name}")

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

            # Create experiment directory early so build logs can be saved
            exp_dir = self._create_experiment_dir(exp_name)
            logger.info(f"[INFO] Experiment directory: {self.config.remote_shared_dir}/{exp_dir}")

            # Build image if requested
            if build:
                self._write_status(exp_dir, "building")
                try:
                    build_log = self.docker.build_method_image(method_name, tag)
                except Exception as e:
                    self._write_status(exp_dir, "failed", error=f"Build failed: {e}")
                    raise
                # Save build log to experiment directory
                build_log_path = f"{self.config.remote_shared_dir}/{exp_dir}/build.log"
                self.ssh.execute(f"cat > {build_log_path} << 'BUILDEOF'\n{build_log}\nBUILDEOF")

            # Load method environment
            method_env = self._load_method_env(method_name)

            # Create service with GPU setting and method parameters
            self.docker.create_service(exp_name, method_name, dataset, tag, gpu, method_params)

            # Follow logs
            self.docker.follow_service_logs(exp_name)

            logger.info(f"[INFO] View logs later: graflag logs -e {exp_name}")

            return exp_name

        except Exception as e:
            logger.error(f"[ERROR] Benchmark failed: {e}")
            sys.exit(1)

    def list_methods(self):
        """List available methods."""
        if not self.ssh.path_exists(self.config.remote_shared_dir, "methods"):
            logger.info("No methods directory found")
            return

        logger.info("[INFO] Available Methods:")
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

        logger.info("[INFO] Available Datasets:")
        datasets = self.ssh.list_dir(self.config.remote_shared_dir, "datasets")
        for dataset_name in datasets:
            print(f"  - {dataset_name}")

    def list_experiments(self):
        """List experiments with status."""
        if not self.ssh.path_exists(self.config.remote_shared_dir, "experiments"):
            logger.info("No experiments directory found")
            return

        logger.info("[INFO] Recent Experiments:")
        # Get experiments and read their status.json in one SSH call
        result = self.ssh.execute(
            f'for d in $(ls -1t {self.config.remote_shared_dir}/experiments/ 2>/dev/null | head -20); do '
            f'  status=$(cat {self.config.remote_shared_dir}/experiments/$d/status.json 2>/dev/null | '
            f'    python3 -c "import sys,json; print(json.load(sys.stdin).get(\'status\',\'\'))" 2>/dev/null); '
            f'  has_results=$(test -f {self.config.remote_shared_dir}/experiments/$d/results.json && echo 1 || echo 0); '
            f'  has_eval=$(test -f {self.config.remote_shared_dir}/experiments/$d/eval/evaluation.json && echo 1 || echo 0); '
            f'  echo "$d|$status|$has_results|$has_eval"; '
            f'done'
        )
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split("\n"):
                if not line.strip():
                    continue
                parts = line.split("|")
                exp_name = parts[0]
                status = parts[1] if len(parts) > 1 and parts[1] else "unknown"
                has_results = parts[2] == "1" if len(parts) > 2 else False
                has_eval = parts[3] == "1" if len(parts) > 3 else False

                # Build status display
                tags = f"[{status}]"
                if has_results:
                    tags += " [results]"
                if has_eval:
                    tags += " [eval]"
                print(f"  - {exp_name}  {tags}")
        else:
            logger.info("No experiments found")
    
    def list_services(self):
        """List running services/experiments."""
        self.docker.get_running_services()

    def logs(self, experiment_name: str, follow: bool = False, tee_file: str = None):
        """Show logs for an experiment/service.

        Shows build log (if exists) + service logs.
        Falls back to reading method_output.txt if the Docker service no longer exists.
        """
        exp_base = f"experiments/{experiment_name}"
        output_parts = []

        # Show build log if it exists
        build_log_path = f"{exp_base}/build.log"
        if self.ssh.path_exists(self.config.remote_shared_dir, build_log_path):
            build_content = self.ssh.read_file(self.config.remote_shared_dir, build_log_path)
            if build_content.strip():
                output_parts.append(build_content)

        # Try Docker service logs first
        try:
            if output_parts:
                print("\n".join(output_parts))
                print("\n" + "=" * 60)
                print("=== SERVICE LOGS ===")
                print("=" * 60 + "\n")
            self.docker.get_service_logs(experiment_name, follow, tee_file)
            return
        except ValueError:
            pass

        # Service no longer exists -- fall back to method_output.txt
        output_path = f"{exp_base}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            logger.info(f"[INFO] Service removed. Showing saved output:")
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            output_parts.append(content)
            print("\n".join(output_parts))
            if tee_file:
                tee_path = Path(tee_file).expanduser().resolve()
                tee_path.parent.mkdir(parents=True, exist_ok=True)
                tee_path.write_text("\n".join(output_parts))
                logger.info(f"[INFO] Saved to {tee_path}")
        elif output_parts:
            # Only build log available
            print("\n".join(output_parts))
        else:
            raise GraFlagError(
                f"No logs found for experiment '{experiment_name}'"
            )

    def stop(self, experiment_name: str, remove: bool = False):
        """Stop a running experiment/service.

        Args:
            experiment_name: Name of the experiment
            remove: If True, also delete the experiment directory
        """
        logger.info(f"[STOP] Stopping experiment: {experiment_name}")
        try:
            self.docker.stop_service(experiment_name)
            logger.info(f"[OK] Service {experiment_name} stopped")
        except ValueError:
            logger.info(f"[INFO] No running service for {experiment_name}")

        if remove:
            exp_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}"
            if self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}"):
                self.ssh.execute(f"rm -rf {exp_path}")
                logger.info(f"[INFO] Deleted experiment directory: {exp_path}")
            else:
                logger.info(f"[INFO] Experiment directory not found: {exp_path}")
    
    def evaluate(self, experiment_name: str):
        """
        Evaluate an experiment: compute metrics and generate plots.
        Uses Docker service for consistent execution model.
        
        Args:
            experiment_name: Name of the experiment to evaluate
        """
        logger.info(f"[INFO] Evaluating experiment: {experiment_name}")
        
        # Check if experiment exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}"):
            raise GraFlagError(f"Experiment {experiment_name} not found")
        
        # Check if results.json exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}/results.json"):
            raise GraFlagError(f"results.json not found in experiment {experiment_name}")
        
        try:
            # Create evaluation service (handles image building automatically)
            eval_service_name = self.docker.create_evaluation_service(experiment_name)
            
            # Follow logs of the evaluation service
            self.docker.follow_service_logs(eval_service_name)
            
            # Clean up evaluation service after completion
            self.docker.remove_evaluation_service(experiment_name)
            
            # Show where results are saved
            eval_dir = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
            logger.info(f"[INFO] Evaluation results saved to: {eval_dir}")
            logger.info(f"   - evaluation.json - Computed metrics")
            logger.info(f"   - roc_curve.png - ROC curve plot")
            logger.info(f"   - pr_curve.png - Precision-Recall curve")
            logger.info(f"   - score_distribution.png - Score histogram")
            logger.info(f"   - spot_curves.png - Spot metrics (if available)")
            print(f"   - Copy to local: graflag.py copy --from-remote -s experiments/{experiment_name}/eval -d ./eval_{experiment_name}")
            print(f"   - View on remote: ssh to {self.config.manager_ip}")
            print(f"   - View logs later: graflag.py logs {eval_service_name}")
        
        except Exception as e:
            logger.error(f"[ERROR] Evaluation failed: {e}")
            raise GraFlagError(str(e))

    def sync(self, local_path: str, is_lib: bool = False):
        """
        Sync a local method or library directory to the remote shared storage.

        Args:
            local_path: Local directory path (must contain .env for methods)
            is_lib: If True, sync as a shared library to libs/ instead of methods/
        """
        local_dir = Path(local_path).resolve()

        if not local_dir.is_dir():
            raise GraFlagError(f"Path is not a directory: {local_dir}")

        if is_lib:
            # For libs, use the directory name
            lib_name = local_dir.name
            remote_dest = f"{self.config.remote_shared_dir}/libs/{lib_name}"
            logger.info(f"Syncing library '{lib_name}' to remote...")
        else:
            # For methods, read METHOD_NAME from .env
            env_file = local_dir / ".env"
            if not env_file.exists():
                raise GraFlagError(f"No .env file found in {local_dir}. Methods must have a .env file with METHOD_NAME.")

            method_name = None
            with open(env_file, 'r') as f:
                for line in f:
                    line = line.strip()
                    if line.startswith("METHOD_NAME="):
                        method_name = line.split("=", 1)[1].strip()
                        break

            if not method_name:
                raise GraFlagError(f"METHOD_NAME not found in {env_file}")

            remote_dest = f"{self.config.remote_shared_dir}/methods/{method_name}"
            logger.info(f"Syncing method '{method_name}' to remote...")

        # Use rsync via ssh.copy_files (add trailing slash to sync contents)
        self.ssh.copy_files(
            source_paths=[f"{local_dir}/"],
            dest_path=f"{remote_dest}/",
            recursive=True,
            from_remote=False
        )

        target_type = "library" if is_lib else "method"
        target_name = lib_name if is_lib else method_name
        logger.info(f"Synced {target_type} '{target_name}' to {remote_dest}")