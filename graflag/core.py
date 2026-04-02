"""Core GraFlag functionality."""

import inspect
import json
import subprocess
import textwrap
from datetime import datetime
from pathlib import Path
from typing import Callable, Dict, List, Optional
import logging

from .config import GraflagConfig
from .ssh import SSHManager
from .docker_ops import DockerManager
from .utils import load_method_env
from .models import (
    ClusterInfo, MethodInfo, DatasetInfo, ExperimentInfo,
    ExperimentResults, EvaluationResults,
)

logger = logging.getLogger(__name__)


class GraFlagError(Exception):
    """Custom exception for GraFlag errors."""
    pass


class GraFlag:
    """Main GraFlag orchestration class.

    All public methods return structured data. No direct printing to stdout
    (except follow_logs which streams in real time).
    """

    def __init__(self, config_file: str = ".env"):
        """Initialize GraFlag with configuration."""
        try:
            self.config = GraflagConfig(config_file)
        except ValueError as e:
            raise GraFlagError(str(e))

        self.ssh = SSHManager(
            manager_ip=self.config.manager_ip,
            ssh_port=self.config.ssh_port,
            ssh_key=self.config.ssh_key
        )
        self.docker = DockerManager(self.ssh, self.config, hosts_file=self.config.hosts_file)

    # ========================================================================
    # Cluster Management
    # ========================================================================

    def setup(self):
        """Setup GraFlag cluster: initialize swarm and setup workers."""
        logger.info("[SETUP] Setting up GraFlag cluster...")
        self.docker.setup_swarm_manager()
        token = self.docker.get_swarm_token()
        self.docker.setup_workers(token)
        self.docker.setup_local_registry()
        logger.info("[OK] GraFlag cluster setup completed!")

    def status(self) -> ClusterInfo:
        """Get cluster status.

        Returns:
            ClusterInfo with nodes, services, and shared directory info.
        """
        try:
            cluster = self.docker.get_cluster_status()
            shared_contents = self.ssh.list_dir(self.config.remote_shared_dir, "")

            nodes = cluster.get('nodes', [])
            worker_nodes = [
                {
                    'hostname': n['hostname'],
                    'status': n['status'],
                    'availability': n['availability'],
                    'is_manager': n['is_manager'],
                }
                for n in nodes
            ]

            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=True,
                swarm_initialized=cluster.get('swarm_active', False),
                worker_nodes=worker_nodes,
                shared_dir=self.config.remote_shared_dir,
                shared_contents=shared_contents,
                services=cluster.get('services', []),
            )
        except Exception as e:
            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=False,
                swarm_initialized=False,
                error=str(e),
            )

    # ========================================================================
    # Run
    # ========================================================================

    def run(
        self, method_name: str, dataset: str, tag: str = "latest",
        build: bool = False, gpu: bool = True, method_params: dict = None
    ) -> str:
        """Run experiment.

        Returns:
            Experiment name.

        Raises:
            GraFlagError: If run fails.
        """
        method_name = method_name.lower()
        dataset = dataset.lower()
        tag = tag.lower()
        method_params = method_params or {}

        timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        exp_name = f"exp__{method_name}__{dataset}__{timestamp}"

        logger.info(f"[RUN] Starting run: {exp_name}")

        # Validate method exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"methods/{method_name}"):
            raise GraFlagError(
                f"Method {method_name} not found in {self.config.remote_shared_dir}/methods/{method_name}"
            )

        # Validate dataset exists
        if not self.ssh.path_exists(self.config.remote_shared_dir, f"datasets/{dataset}"):
            raise GraFlagError(
                f"Dataset {dataset} not found in {self.config.remote_shared_dir}/datasets/{dataset}"
            )

        # Create experiment directory
        exp_dir = f"experiments/{exp_name}"
        self.ssh.mkdir(self.config.remote_shared_dir, exp_dir)
        logger.info(f"[INFO] Experiment directory: {self.config.remote_shared_dir}/{exp_dir}")

        # Build image if requested
        if build:
            self._write_status(exp_dir, "building")
            try:
                build_log = self.docker.build_method_image(method_name, tag)
            except Exception as e:
                self._write_status(exp_dir, "failed", error=f"Build failed: {e}")
                raise GraFlagError(f"Build failed: {e}")
            # Save build log
            build_log_path = f"{self.config.remote_shared_dir}/{exp_dir}/build.log"
            self.ssh.execute(f"cat > {build_log_path} << 'BUILDEOF'\n{build_log}\nBUILDEOF")

        # Create service
        self.docker.create_service(exp_name, method_name, dataset, tag, gpu, method_params)

        # Follow logs (streams to stdout)
        self.docker.follow_service_logs(exp_name)

        logger.info(f"[INFO] View logs later: graflag logs -e {exp_name}")
        return exp_name

    def register_metric(
        self, result_type: str, metric_func: Callable,
        experiment: str = None,
    ):
        """Register a custom metric as a plugin file on the cluster.

        The function source is extracted via ``inspect.getsource`` and written
        to a ``.py`` plugin file that the evaluator loads at runtime.

        Args:
            result_type: Result type the metric applies to
                (e.g. ``"EDGE_STREAM_ANOMALY_SCORES"``).
            metric_func: A function with signature
                ``(scores, ground_truth, **kwargs) -> Dict[str, float]``.
            experiment: If given, the plugin is scoped to that experiment
                (``custom_metrics/`` inside the experiment directory).
                Otherwise it is saved to the global plugins directory.

        Raises:
            GraFlagError: If the function source cannot be extracted or the
                file cannot be written.
        """
        func_name = metric_func.__name__
        try:
            source = textwrap.dedent(inspect.getsource(metric_func))
        except (OSError, TypeError) as e:
            raise GraFlagError(
                f"Cannot extract source of {func_name}: {e}. "
                "Create the plugin file manually instead."
            )

        plugin_content = (
            f'"""Auto-generated metric plugin: {func_name}"""\n'
            f"import numpy as np\n"
            f"from graflag_evaluator import MetricCalculator\n\n"
            f"{source}\n"
            f'MetricCalculator.register_metric("{result_type}", {func_name})\n'
        )

        if experiment:
            plugin_dir = (
                f"{self.config.remote_shared_dir}/experiments/"
                f"{experiment}/custom_metrics"
            )
        else:
            plugin_dir = (
                f"{self.config.remote_shared_dir}/libs/"
                f"graflag_evaluator/plugins"
            )

        self.ssh.execute(f"mkdir -p {plugin_dir}")
        plugin_path = f"{plugin_dir}/{func_name}.py"
        # Write via heredoc with a delimiter unlikely to appear in source
        self.ssh.execute(
            f"cat > {plugin_path} << 'PLUGINEOF'\n{plugin_content}PLUGINEOF"
        )
        logger.info(f"[OK] Saved metric plugin: {plugin_path}")

    def evaluate(self, experiment_name: str):
        """Evaluate an experiment: compute metrics and generate plots.

        Raises:
            GraFlagError: If evaluation fails.
        """
        logger.info(f"[INFO] Evaluating experiment: {experiment_name}")

        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}"):
            raise GraFlagError(f"Experiment {experiment_name} not found")

        if not self.ssh.path_exists(self.config.remote_shared_dir, f"experiments/{experiment_name}/results.json"):
            raise GraFlagError(f"results.json not found in experiment {experiment_name}")

        try:
            eval_service_name = self.docker.create_evaluation_service(experiment_name)
            self.docker.follow_service_logs(eval_service_name)
            self.docker.remove_evaluation_service(experiment_name)

            eval_dir = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
            logger.info(f"[INFO] Evaluation results saved to: {eval_dir}")
        except Exception as e:
            raise GraFlagError(f"Evaluation failed: {e}")

    # ========================================================================
    # Resource Discovery
    # ========================================================================

    def list_methods(self) -> List[MethodInfo]:
        """List available methods.

        Returns:
            List of MethodInfo objects.
        """
        methods_dir = f"{self.config.remote_shared_dir}/methods"

        # Single SSH call: list dirs, check files, and cat all .env files
        cmd = (
            f'for d in {methods_dir}/*/; do '
            f'  name=$(basename "$d"); '
            f'  has_env=$( [ -f "$d/.env" ] && echo 1 || echo 0 ); '
            f'  has_dockerfile=$( [ -f "$d/Dockerfile" ] && echo 1 || echo 0 ); '
            f'  echo "METHOD:$name:$has_env:$has_dockerfile"; '
            f'  if [ "$has_env" = "1" ]; then '
            f'    while IFS= read -r line || [ -n "$line" ]; do '
            f'      case "$line" in ""|\\#*) continue;; esac; '
            f'      echo "ENV:$name:$line"; '
            f'    done < "$d/.env"; '
            f'  fi; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            return []

        # Parse output
        method_meta = {}  # name -> {has_env, has_dockerfile}
        method_envs = {}  # name -> {key: value}

        for line in result.stdout.strip().split('\n'):
            if not line:
                continue
            if line.startswith('METHOD:'):
                parts = line.split(':', 3)
                if len(parts) == 4:
                    name = parts[1]
                    method_meta[name] = {
                        'has_env': parts[2] == '1',
                        'has_dockerfile': parts[3] == '1',
                    }
                    method_envs.setdefault(name, {})
            elif line.startswith('ENV:'):
                parts = line.split(':', 2)
                if len(parts) == 3:
                    name = parts[1]
                    env_line = parts[2].strip()
                    if '=' in env_line:
                        key, _, value = env_line.partition('=')
                        method_envs.setdefault(name, {})[key.strip()] = value.strip()

        methods = []
        for name in sorted(method_meta.keys()):
            meta = method_meta[name]
            env_vars = method_envs.get(name, {})
            parameters = {k: v for k, v in env_vars.items() if k.startswith('_')}

            methods.append(MethodInfo(
                name=name,
                description=env_vars.get("DESCRIPTION", ""),
                source_code=env_vars.get("SOURCE_CODE", ""),
                supported_data=env_vars.get("SUPPORTED_DATASETS", "Unknown"),
                parameters=parameters,
                has_dockerfile=meta['has_dockerfile'],
                has_env=meta['has_env'],
            ))

        return methods

    def list_datasets(self) -> List[DatasetInfo]:
        """List available datasets.

        Returns:
            List of DatasetInfo objects.
        """
        datasets_dir = f"{self.config.remote_shared_dir}/datasets"

        # Single SSH call: list all datasets with size and file count
        cmd = (
            f'for d in {datasets_dir}/*/; do '
            f'  [ -d "$d" ] || continue; '
            f'  name=$(basename "$d"); '
            f'  size=$(du -sm "$d" 2>/dev/null | cut -f1 || echo 0); '
            f'  count=$(find "$d" -type f 2>/dev/null | wc -l); '
            f'  echo "$name:$size:$count"; '
            f'done'
        )
        result = self.ssh.execute(cmd)
        if result.returncode != 0:
            return []

        datasets = []
        for line in result.stdout.strip().split('\n'):
            if not line or ':' not in line:
                continue
            parts = line.split(':')
            if len(parts) >= 3:
                name = parts[0]
                try:
                    size_mb = float(parts[1])
                except (ValueError, IndexError):
                    size_mb = 0.0
                try:
                    file_count = int(parts[2])
                except (ValueError, IndexError):
                    file_count = 0

                datasets.append(DatasetInfo(
                    name=name,
                    path=f"{datasets_dir}/{name}",
                    size_mb=size_mb,
                    file_count=file_count,
                ))

        return sorted(datasets, key=lambda d: d.name)

    def list_experiments(self, limit: int = 50) -> List[ExperimentInfo]:
        """List recent experiments.

        Returns:
            List of ExperimentInfo (most recent first).
        """
        if not self.ssh.path_exists(self.config.remote_shared_dir, "experiments"):
            return []

        result = self.ssh.execute(
            f"ls -1 {self.config.remote_shared_dir}/experiments/ 2>/dev/null || true"
        )

        if result.returncode != 0 or not result.stdout.strip():
            return []

        exp_names = [e.strip() for e in result.stdout.strip().split("\n") if e.strip()]

        # Fetch running services once
        try:
            running_services = self.docker.get_service_names()
        except Exception:
            running_services = set()

        experiments = []
        for name in exp_names:
            info = self._get_experiment_info(name, running_services)
            if info:
                experiments.append(info)

        # Sort by timestamp (most recent first)
        experiments.sort(key=lambda e: e.timestamp or "", reverse=True)
        return experiments[:limit]

    def list_services(self) -> List[Dict]:
        """List running Docker services.

        Returns:
            List of service dicts with name, replicas, image, status.
        """
        return self.docker.list_services()

    # ========================================================================
    # Logs
    # ========================================================================

    def get_logs(self, experiment_name: str, tail: int = 100) -> List[str]:
        """Get experiment logs (non-streaming).

        Tries Docker service logs first, then falls back to method_output.txt.

        Returns:
            List of log lines.
        """
        # Try Docker service logs
        logs = self.docker.get_service_logs(experiment_name, tail=tail)
        if logs:
            return logs

        # Fall back to saved output
        output_path = f"experiments/{experiment_name}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            if content.strip():
                lines = content.strip().split('\n')
                return lines[-tail:] if len(lines) > tail else lines

        return []

    def follow_logs(self, experiment_name: str, tee_file: str = None):
        """Follow logs for an experiment (streams to stdout).

        Shows build log (if exists) + service logs.
        Falls back to method_output.txt if the service is gone.
        """
        exp_base = f"experiments/{experiment_name}"
        output_parts = []

        # Show build log if it exists
        build_log_path = f"{exp_base}/build.log"
        if self.ssh.path_exists(self.config.remote_shared_dir, build_log_path):
            build_content = self.ssh.read_file(self.config.remote_shared_dir, build_log_path)
            if build_content.strip():
                output_parts.append(build_content)

        # Try Docker service logs (follow mode)
        if self.docker.service_exists(experiment_name):
            if output_parts:
                print("\n".join(output_parts))
                print("\n" + "=" * 60)
                print("=== SERVICE LOGS ===")
                print("=" * 60 + "\n")
            self.docker.follow_service_logs(experiment_name)
            self._save_tee(tee_file, output_parts)
            return

        # Service no longer exists -- fall back to method_output.txt
        output_path = f"{exp_base}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            logger.info("[INFO] Service removed. Showing saved output:")
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            output_parts.append(content)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        elif output_parts:
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        else:
            raise GraFlagError(f"No logs found for experiment '{experiment_name}'")

    def show_logs(self, experiment_name: str, tee_file: str = None):
        """Show logs (non-follow mode) — prints to stdout."""
        exp_base = f"experiments/{experiment_name}"
        output_parts = []

        # Show build log if it exists
        build_log_path = f"{exp_base}/build.log"
        if self.ssh.path_exists(self.config.remote_shared_dir, build_log_path):
            build_content = self.ssh.read_file(self.config.remote_shared_dir, build_log_path)
            if build_content.strip():
                output_parts.append(build_content)

        # Try Docker service logs (non-follow)
        logs = self.docker.get_service_logs(experiment_name)
        if logs:
            if output_parts:
                output_parts.append("\n" + "=" * 60)
                output_parts.append("=== SERVICE LOGS ===")
                output_parts.append("=" * 60 + "\n")
            output_parts.extend(logs)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
            return

        # Fall back to method_output.txt
        output_path = f"{exp_base}/method_output.txt"
        if self.ssh.path_exists(self.config.remote_shared_dir, output_path):
            logger.info("[INFO] Service removed. Showing saved output:")
            content = self.ssh.read_file(self.config.remote_shared_dir, output_path)
            output_parts.append(content)
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        elif output_parts:
            print("\n".join(output_parts))
            self._save_tee(tee_file, output_parts)
        else:
            raise GraFlagError(f"No logs found for experiment '{experiment_name}'")

    # ========================================================================
    # Service Control
    # ========================================================================

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

    # ========================================================================
    # Results
    # ========================================================================

    def get_experiment_results(self, experiment_name: str) -> Optional[ExperimentResults]:
        """Get experiment results from results.json."""
        results_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/results.json"
        result = self.ssh.execute(f"cat {results_path} 2>/dev/null")
        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        metadata = data.get("metadata", {})
        return ExperimentResults(
            experiment_name=experiment_name,
            method_name=metadata.get("method_name", ""),
            dataset=metadata.get("dataset", ""),
            metadata=metadata,
            execution_time_ms=metadata.get("exec_time_ms"),
            peak_memory_mb=metadata.get("peak_memory_mb"),
            peak_gpu_memory_mb=metadata.get("peak_gpu_mb"),
            result_type=data.get("result_type"),
            scores_available="scores" in data or "scores_file" in data,
        )

    def get_evaluation_results(self, experiment_name: str) -> Optional[EvaluationResults]:
        """Get evaluation results from eval/evaluation.json."""
        eval_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
        eval_json = f"{eval_path}/evaluation.json"

        result = self.ssh.execute(f"cat {eval_json} 2>/dev/null")
        if result.returncode != 0 or not result.stdout.strip():
            return None

        try:
            data = json.loads(result.stdout)
        except json.JSONDecodeError:
            return None

        # List plot files
        plots = []
        result = self.ssh.execute(f"ls -1 {eval_path}/*.png 2>/dev/null || true")
        if result.returncode == 0 and result.stdout.strip():
            for line in result.stdout.strip().split('\n'):
                if line.strip():
                    plots.append(line.strip().split('/')[-1])

        return EvaluationResults(
            experiment_name=experiment_name,
            metrics=data.get("metrics", {}),
            plots_available=plots,
            evaluation_path=eval_path,
        )

    # ========================================================================
    # File Operations
    # ========================================================================

    def copy_files(self, source_paths, dest_path: str, recursive: bool = False, from_remote: bool = False):
        """Copy files/directories bidirectionally."""
        if from_remote:
            remote_sources = []
            for src in (source_paths if isinstance(source_paths, list) else [source_paths]):
                clean_src = src.lstrip('/')
                remote_sources.append(f"{self.config.remote_shared_dir}/{clean_src}")
            return self.ssh.copy_files(remote_sources, dest_path, recursive, from_remote=True)
        else:
            clean_dest = dest_path.lstrip('/')
            remote_dest = f"{self.config.remote_shared_dir}/{clean_dest}"
            return self.ssh.copy_files(source_paths, remote_dest, recursive, from_remote=False)

    def mount_nfs(self, shared_dir: str):
        """Mount NFS share on local machine."""
        mount_dir = Path(shared_dir).expanduser()
        try:
            mount_dir.mkdir(parents=True, exist_ok=True)
        except OSError:
            logger.warning("[WARN] Stale NFS mount detected, cleaning up...")
            subprocess.run(f"sudo umount -l {mount_dir}", shell=True, capture_output=True)
            mount_dir.mkdir(parents=True, exist_ok=True)

        result = subprocess.run(f"mountpoint -q {mount_dir}", shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS already mounted at {mount_dir}")
            return

        mount_cmd = (
            f"sudo mount -t nfs "
            f"-o addr={self.config.manager_ip},port={self.config.nfs_port},"
            f"vers=3,hard,intr,rsize=8192,wsize=8192,timeo=30,retrans=3 "
            f"{self.config.manager_ip}:/tmp/shared {mount_dir}"
        )

        result = subprocess.run(mount_cmd, shell=True)
        if result.returncode == 0:
            logger.info(f"[OK] NFS mounted at {mount_dir}")
        else:
            raise GraFlagError(f"Failed to mount NFS at {mount_dir}")

    def sync(self, local_path: str, is_lib: bool = False):
        """Sync a local method or library directory to remote shared storage."""
        local_dir = Path(local_path).resolve()

        if not local_dir.is_dir():
            raise GraFlagError(f"Path is not a directory: {local_dir}")

        if is_lib:
            lib_name = local_dir.name
            remote_dest = f"{self.config.remote_shared_dir}/libs/{lib_name}"
            logger.info(f"Syncing library '{lib_name}' to remote...")
        else:
            env_file = local_dir / ".env"
            if not env_file.exists():
                raise GraFlagError(f"No .env file found in {local_dir}.")

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

        self.ssh.copy_files(
            source_paths=[f"{local_dir}/"],
            dest_path=f"{remote_dest}/",
            recursive=True,
            from_remote=False,
        )

        target_type = "library" if is_lib else "method"
        target_name = lib_name if is_lib else method_name
        logger.info(f"Synced {target_type} '{target_name}' to {remote_dest}")

    # ========================================================================
    # Internal Helpers
    # ========================================================================

    def _write_status(self, exp_dir: str, status: str, error: str = None):
        """Write status.json to an experiment directory on the remote."""
        data = {"status": status, "timestamp": datetime.now().isoformat()}
        if error:
            data["error"] = error
        status_data = json.dumps(data)
        status_path = f"{self.config.remote_shared_dir}/{exp_dir}/status.json"
        self.ssh.execute(f"cat > {status_path} << 'STATUSEOF'\n{status_data}\nSTATUSEOF")

    def _get_experiment_info(self, exp_name: str, running_services: set = None) -> Optional[ExperimentInfo]:
        """Get experiment information."""
        full_exp_path = f"{self.config.remote_shared_dir}/experiments/{exp_name}"

        # Single SSH call to check all files and read status.json
        check_cmd = (
            f'echo "EXISTS:$(test -d {full_exp_path} && echo 1 || echo 0)"\n'
            f'echo "RESULTS:$(test -f {full_exp_path}/results.json && echo 1 || echo 0)"\n'
            f'echo "EVAL:$(test -f {full_exp_path}/eval/evaluation.json && echo 1 || echo 0)"\n'
            f'echo "BUILD_LOG:$(test -f {full_exp_path}/build.log && echo 1 || echo 0)"\n'
            f'echo "STATUS_JSON:$(cat {full_exp_path}/status.json 2>/dev/null || echo \'\')"'
        )
        result = self.ssh.execute(check_cmd)
        if result.returncode != 0:
            return None

        checks = {}
        status_json_raw = ""
        for line in result.stdout.strip().split('\n'):
            if line.startswith('STATUS_JSON:'):
                status_json_raw = line[len('STATUS_JSON:'):]
            elif ':' in line:
                key, val = line.split(':', 1)
                checks[key] = val.strip() == '1'

        if not checks.get('EXISTS', False):
            return None

        has_results = checks.get('RESULTS', False)
        has_evaluation = checks.get('EVAL', False)
        has_build_log = checks.get('BUILD_LOG', False)

        # Parse status.json
        runner_status = None
        if status_json_raw.strip():
            try:
                status_data = json.loads(status_json_raw)
                runner_status = status_data.get("status")
            except (json.JSONDecodeError, ValueError):
                pass

        # Parse experiment name
        parts = exp_name.split("__")
        method = parts[1] if len(parts) > 1 else "unknown"
        dataset = parts[2] if len(parts) > 2 else "unknown"
        timestamp = parts[3] if len(parts) > 3 else ""

        # Check if service exists
        if running_services is not None:
            service_exists = exp_name in running_services
        else:
            service_exists = self.docker.service_exists(exp_name)

        # Check if service tasks all failed
        service_failed = service_exists and self.docker.is_service_failed(exp_name)

        # Determine status
        if runner_status in ("completed", "failed"):
            status = runner_status
        elif service_failed:
            status = "failed"
        elif runner_status == "building":
            # During build: service doesn't exist yet (expected).
            # Only mark failed if build finished (build.log exists) but no service was created.
            if service_exists:
                status = "building"
            elif has_build_log:
                status = "failed"  # build finished but service never created
            else:
                status = "building"  # still building, no service yet
        elif runner_status == "running":
            status = "running" if service_exists else "stopped"
        elif service_exists:
            status = "running"
        elif has_results or has_evaluation:
            status = "completed"
        else:
            status = "unknown"

        return ExperimentInfo(
            name=exp_name,
            method=method,
            dataset=dataset,
            timestamp=timestamp,
            status=status,
            has_results=has_results,
            has_evaluation=has_evaluation,
            results_path=f"{full_exp_path}/results.json" if has_results else None,
            evaluation_path=f"{full_exp_path}/eval" if has_evaluation else None,
            service_name=exp_name if service_exists else None,
        )

    def _save_tee(self, tee_file: str, output_parts: List[str]):
        """Save output to file if tee_file is specified."""
        if tee_file:
            tee_path = Path(tee_file).expanduser().resolve()
            tee_path.parent.mkdir(parents=True, exist_ok=True)
            tee_path.write_text("\n".join(output_parts))
            logger.info(f"[INFO] Saved to {tee_path}")
