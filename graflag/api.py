"""
GraFlag Python API for GUI Integration

This module provides a clean, stateful API layer for building GUIs on top of GraFlag.
It wraps the core functionality and provides structured data for visualization.

Design Principles:
1. Stateful: API maintains connection state and provides event callbacks
2. Structured Output: Returns dictionaries/objects instead of printing
3. Async-friendly: Methods can be wrapped for async execution
4. Progress Tracking: Callbacks for long-running operations
5. Error Handling: Proper exceptions with context
"""

from typing import Dict, List, Optional, Callable, Any, Tuple
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
import json
import logging

from .core import GraFlag, GraFlagError
from .config import GraflagConfig

logger = logging.getLogger(__name__)


# ============================================================================
# Data Models for API Responses
# ============================================================================

@dataclass
class ClusterInfo:
    """Cluster status information."""
    manager_ip: str
    is_connected: bool
    swarm_initialized: bool
    worker_nodes: List[Dict[str, str]] = field(default_factory=list)
    shared_dir: str = ""
    error: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MethodInfo:
    """Method metadata."""
    name: str
    description: str = ""
    source_code: str = ""
    supported_datasets: List[str] = field(default_factory=list)
    parameters: Dict[str, Any] = field(default_factory=dict)
    has_dockerfile: bool = False
    has_env: bool = False
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetInfo:
    """Dataset metadata."""
    name: str
    path: str
    size_mb: float = 0.0
    file_count: int = 0
    description: str = ""
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExperimentInfo:
    """Experiment metadata and status."""
    name: str
    method: str
    dataset: str
    timestamp: str
    status: str  # "building", "running", "completed", "failed", "stopped", "unknown"
    has_results: bool = False
    has_evaluation: bool = False
    results_path: Optional[str] = None
    evaluation_path: Optional[str] = None
    service_name: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ExperimentResults:
    """Parsed experiment results."""
    experiment_name: str
    method_name: str
    dataset: str
    metadata: Dict[str, Any] = field(default_factory=dict)
    execution_time_ms: Optional[float] = None
    peak_memory_mb: Optional[float] = None
    peak_gpu_memory_mb: Optional[float] = None
    result_type: Optional[str] = None
    scores_available: bool = False
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class EvaluationResults:
    """Parsed evaluation results."""
    experiment_name: str
    metrics: Dict[str, float] = field(default_factory=dict)
    plots_available: List[str] = field(default_factory=list)
    evaluation_path: Optional[str] = None
    
    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class BenchmarkProgress:
    """Progress information for benchmark execution."""
    experiment_name: str
    status: str  # "building", "starting", "running", "completed", "failed"
    message: str = ""
    log_lines: List[str] = field(default_factory=list)
    
    def to_dict(self) -> dict:
        return asdict(self)


# ============================================================================
# Main API Class
# ============================================================================

class GraFlagAPI:
    """
    High-level Python API for GraFlag operations.
    
    This class provides a stateful, structured interface suitable for GUI development.
    All methods return structured data (dataclasses/dicts) instead of printing to console.
    
    Usage:
        api = GraFlagAPI(config_file=".env")
        
        # Get cluster info
        cluster = api.get_cluster_info()
        
        # List resources
        methods = api.list_methods()
        datasets = api.list_datasets()
        experiments = api.list_experiments()
        
        # Run benchmark
        exp_name = api.run_benchmark(
            method="generaldyg",
            dataset="generaldyg_btc_alpha",
            on_progress=lambda progress: print(progress.message)
        )
        
        # Get results
        results = api.get_experiment_results(exp_name)
        
        # Evaluate
        evaluation = api.evaluate_experiment(exp_name)
    """
    
    def __init__(self, config_file: str = ".env", log_level: int = logging.INFO):
        """
        Initialize GraFlag API.
        
        Args:
            config_file: Path to configuration file
            log_level: Logging level
        """
        logging.basicConfig(level=log_level)
        self.core = GraFlag(config_file)
        self.config = self.core.config
    
    # ========================================================================
    # Cluster Management
    # ========================================================================
    
    def get_cluster_info(self) -> ClusterInfo:
        """
        Get cluster status and information.
        
        Returns:
            ClusterInfo object with cluster details
        """
        try:
            # Test SSH connection
            is_connected = False
            swarm_initialized = False
            workers = []
            error = None
            
            try:
                result = self.core.ssh.execute("echo test")
                is_connected = result.returncode == 0
            except Exception as e:
                error = f"SSH connection failed: {str(e)}"
            
            # Check swarm status
            if is_connected:
                try:
                    result = self.core.ssh.execute('docker info --format "{{.Swarm.LocalNodeState}}"')
                    swarm_initialized = "active" in result.stdout.lower()

                    if swarm_initialized:
                        # Get all nodes with their role
                        result = self.core.ssh.execute(
                            'docker node ls --format "{{.Hostname}}|{{.Status}}|{{.Availability}}|{{.ManagerStatus}}"'
                        )
                        if result.returncode == 0 and result.stdout.strip():
                            for line in result.stdout.strip().split('\n'):
                                if line.strip():
                                    parts = line.split('|')
                                    if len(parts) >= 4:
                                        # ManagerStatus is empty for workers, "Leader" or "Reachable" for managers
                                        is_manager = bool(parts[3].strip())
                                        workers.append({
                                            'hostname': parts[0],
                                            'status': parts[1],
                                            'availability': parts[2],
                                            'is_manager': is_manager
                                        })
                except Exception as e:
                    logger.warning(f"Could not get swarm info: {e}")
            
            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=is_connected,
                swarm_initialized=swarm_initialized,
                worker_nodes=workers,
                shared_dir=self.config.remote_shared_dir,
                error=error
            )
            
        except Exception as e:
            return ClusterInfo(
                manager_ip=self.config.manager_ip,
                is_connected=False,
                swarm_initialized=False,
                error=str(e)
            )
    
    def setup_cluster(self) -> Dict[str, Any]:
        """
        Setup GraFlag cluster (initialize swarm and workers).
        
        Returns:
            Dictionary with setup status
        """
        try:
            self.core.setup()
            return {
                "success": True,
                "message": "Cluster setup completed successfully"
            }
        except Exception as e:
            return {
                "success": False,
                "error": str(e)
            }
    
    # ========================================================================
    # Resource Discovery
    # ========================================================================
    
    def list_methods(self) -> List[MethodInfo]:
        """
        List all available methods.
        
        Returns:
            List of MethodInfo objects
        """
        methods = []
        
        try:
            if not self.core.ssh.path_exists(self.config.remote_shared_dir, "methods"):
                return methods
            
            method_names = self.core.ssh.list_dir(self.config.remote_shared_dir, "methods")
            
            for method_name in method_names:
                method_info = self._get_method_info(method_name)
                if method_info:
                    methods.append(method_info)
        
        except Exception as e:
            logger.error(f"Error listing methods: {e}")
        
        return methods
    
    def get_method_details(self, method_name: str) -> Optional[MethodInfo]:
        """
        Get detailed information about a specific method.
        
        Args:
            method_name: Name of the method
            
        Returns:
            MethodInfo object or None if not found
        """
        return self._get_method_info(method_name)
    
    def _get_method_info(self, method_name: str) -> Optional[MethodInfo]:
        """Internal: Get method information."""
        try:
            method_path = f"methods/{method_name}"
            
            # Check if method exists
            if not self.core.ssh.path_exists(self.config.remote_shared_dir, method_path):
                return None
            
            # Load .env file if exists
            env_vars = {}
            parameters = {}
            has_env = False
            
            if self.core.ssh.path_exists(self.config.remote_shared_dir, f"{method_path}/.env"):
                has_env = True
                env_vars = self.core._load_method_env(method_name)
                
                # Extract parameters (those starting with _)
                parameters = {
                    key: value for key, value in env_vars.items()
                    if key.startswith('_')
                }
            
            # Check for Dockerfile
            has_dockerfile = self.core.ssh.path_exists(
                self.config.remote_shared_dir, f"{method_path}/Dockerfile"
            )
            
            return MethodInfo(
                name=method_name,
                description=env_vars.get("DESCRIPTION", ""),
                source_code=env_vars.get("SOURCE_CODE", ""),
                supported_datasets=[s.strip() for s in env_vars.get("SUPPORTED_DATASETS", "").split(",") if s.strip()],
                parameters=parameters,
                has_dockerfile=has_dockerfile,
                has_env=has_env
            )
            
        except Exception as e:
            logger.error(f"Error getting method info for {method_name}: {e}")
            return None
    
    def list_datasets(self) -> List[DatasetInfo]:
        """
        List all available datasets.
        
        Returns:
            List of DatasetInfo objects
        """
        datasets = []
        
        try:
            if not self.core.ssh.path_exists(self.config.remote_shared_dir, "datasets"):
                return datasets
            
            dataset_names = self.core.ssh.list_dir(self.config.remote_shared_dir, "datasets")
            
            for dataset_name in dataset_names:
                dataset_path = f"{self.config.remote_shared_dir}/datasets/{dataset_name}"
                
                # Get dataset size
                result = self.core.ssh.execute(f"du -sm {dataset_path} 2>/dev/null || echo '0'")
                size_mb = 0.0
                if result.returncode == 0:
                    try:
                        size_mb = float(result.stdout.split()[0])
                    except:
                        pass
                
                # Count files
                result = self.core.ssh.execute(
                    f"find {dataset_path} -type f 2>/dev/null | wc -l"
                )
                file_count = 0
                if result.returncode == 0:
                    try:
                        file_count = int(result.stdout.strip())
                    except:
                        pass
                
                datasets.append(DatasetInfo(
                    name=dataset_name,
                    path=dataset_path,
                    size_mb=size_mb,
                    file_count=file_count
                ))
        
        except Exception as e:
            logger.error(f"Error listing datasets: {e}")
        
        return datasets
    
    def list_experiments(self, limit: int = 50) -> List[ExperimentInfo]:
        """
        List recent experiments.

        Args:
            limit: Maximum number of experiments to return

        Returns:
            List of ExperimentInfo objects (sorted by creation timestamp in name, most recent first)
        """
        experiments = []

        try:
            if not self.core.ssh.path_exists(self.config.remote_shared_dir, "experiments"):
                return experiments

            # Get all experiments (not sorted by modification time)
            result = self.core.ssh.execute(
                f"ls -1 {self.config.remote_shared_dir}/experiments/ 2>/dev/null || true"
            )

            if result.returncode == 0 and result.stdout.strip():
                exp_names = [exp.strip() for exp in result.stdout.strip().split("\n") if exp.strip()]

                # Fetch running services once (reduces SSH calls significantly)
                running_services = set()
                svc_result = self.core.ssh.execute('docker service ls --format "{{.Name}}"')
                if svc_result.returncode == 0 and svc_result.stdout.strip():
                    running_services = set(svc_result.stdout.strip().split('\n'))

                for exp_name in exp_names:
                    exp_info = self._get_experiment_info(exp_name, running_services=running_services)
                    if exp_info:
                        experiments.append(exp_info)

                # Sort by timestamp in experiment name (most recent first)
                # Format: exp__method__dataset__YYYYMMDD_HHMMSS
                def get_timestamp_key(exp: ExperimentInfo) -> str:
                    # Return timestamp part for sorting, or empty string if not found
                    return exp.timestamp if exp.timestamp else ""

                experiments.sort(key=get_timestamp_key, reverse=True)
                experiments = experiments[:limit]

        except Exception as e:
            logger.error(f"Error listing experiments: {e}")

        return experiments
    
    def get_experiment_details(self, experiment_name: str) -> Optional[ExperimentInfo]:
        """
        Get detailed information about a specific experiment.
        
        Args:
            experiment_name: Name of the experiment
            
        Returns:
            ExperimentInfo object or None if not found
        """
        return self._get_experiment_info(experiment_name)
    
    def _get_experiment_info(self, exp_name: str, running_services: Optional[set] = None) -> Optional[ExperimentInfo]:
        """Internal: Get experiment information."""
        try:
            exp_path = f"experiments/{exp_name}"
            full_exp_path = f"{self.config.remote_shared_dir}/{exp_path}"

            # Single SSH call to check all files and read status.json at once
            check_cmd = f"""
            echo "EXISTS:$(test -d {full_exp_path} && echo 1 || echo 0)"
            echo "RESULTS:$(test -f {full_exp_path}/results.json && echo 1 || echo 0)"
            echo "EVAL:$(test -f {full_exp_path}/eval/evaluation.json && echo 1 || echo 0)"
            echo "STATUS_JSON:$(cat {full_exp_path}/status.json 2>/dev/null || echo '')"
            """
            result = self.core.ssh.execute(check_cmd)

            if result.returncode != 0:
                return None

            # Parse results
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

            # Parse status.json if available
            runner_status = None
            if status_json_raw.strip():
                try:
                    status_data = json.loads(status_json_raw)
                    runner_status = status_data.get("status")
                except (json.JSONDecodeError, ValueError):
                    pass

            # Parse experiment name: exp__method__dataset__timestamp
            parts = exp_name.split("__")
            method = parts[1] if len(parts) > 1 else "unknown"
            dataset = parts[2] if len(parts) > 2 else "unknown"
            timestamp = parts[3] if len(parts) > 3 else ""

            # Check if Docker service still exists (use cached list if provided)
            if running_services is not None:
                service_exists = exp_name in running_services
            else:
                svc_result = self.core.ssh.execute(f"docker service ls --filter name={exp_name} --format '{{{{.Name}}}}'")
                service_exists = svc_result.returncode == 0 and exp_name in svc_result.stdout

            # Determine status
            # status.json is the source of truth once written.
            # Docker service existence is only used as fallback before status.json exists.
            #
            # States: building, running, completed, failed, stopped, unknown
            if runner_status in ("completed", "failed"):
                # Terminal states from the runner — definitive
                status = runner_status
            elif runner_status == "building":
                # Image is being built (written by core before build)
                status = "building"
            elif runner_status == "running":
                # Runner wrote "running" — check if service still exists
                status = "running" if service_exists else "stopped"
            elif service_exists:
                # No status.json yet but service exists — just started
                status = "running"
            elif has_results or has_evaluation:
                # Legacy experiments without status.json
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
                results_path=f"{self.config.remote_shared_dir}/{exp_path}/results.json" if has_results else None,
                evaluation_path=f"{self.config.remote_shared_dir}/{exp_path}/eval" if has_evaluation else None,
                service_name=exp_name if service_exists else None
            )

        except Exception as e:
            logger.error(f"Error getting experiment info for {exp_name}: {e}")
            return None
    
    # ========================================================================
    # Benchmark Execution
    # ========================================================================
    
    def run_benchmark(
        self,
        method: str,
        dataset: str,
        tag: str = "latest",
        build: bool = False,
        gpu: bool = True,
        method_params: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable[[BenchmarkProgress], None]] = None
    ) -> str:
        """
        Run a benchmark experiment.
        
        Args:
            method: Method name
            dataset: Dataset name
            tag: Docker image tag
            build: Whether to build image before running
            gpu: Enable GPU support
            method_params: Method-specific parameters
            on_progress: Callback for progress updates
            
        Returns:
            Experiment name
            
        Raises:
            GraFlagError: If benchmark fails
        """
        try:
            exp_name = self.core.benchmark(
                method_name=method,
                dataset=dataset,
                tag=tag,
                build=build,
                gpu=gpu,
                method_params=method_params or {}
            )
            
            return exp_name
            
        except Exception as e:
            raise GraFlagError(f"Benchmark failed: {str(e)}")
    
    # ========================================================================
    # Results and Evaluation
    # ========================================================================
    
    def get_experiment_results(self, experiment_name: str) -> Optional[ExperimentResults]:
        """
        Get experiment results (from results.json).
        
        Args:
            experiment_name: Name of the experiment
            
        Returns:
            ExperimentResults object or None if not available
        """
        try:
            results_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/results.json"
            
            # Read results.json
            result = self.core.ssh.execute(f"cat {results_path} 2>/dev/null")
            if result.returncode != 0:
                return None
            
            data = json.loads(result.stdout)
            
            metadata = data.get("metadata", {})

            # Read resource metrics (standardized field names from add_resource_metrics)
            exec_time_ms = metadata.get("exec_time_ms")

            return ExperimentResults(
                experiment_name=experiment_name,
                method_name=metadata.get("method_name", ""),
                dataset=metadata.get("dataset", ""),
                metadata=metadata,
                execution_time_ms=exec_time_ms,
                peak_memory_mb=metadata.get("peak_memory_mb"),
                peak_gpu_memory_mb=metadata.get("peak_gpu_mb"),
                result_type=data.get("result_type"),
                scores_available="scores" in data or "scores_file" in data
            )
            
        except Exception as e:
            logger.error(f"Error getting results for {experiment_name}: {e}")
            return None
    
    def get_evaluation_results(self, experiment_name: str) -> Optional[EvaluationResults]:
        """
        Get evaluation results (from eval/evaluation.json).
        
        Args:
            experiment_name: Name of the experiment
            
        Returns:
            EvaluationResults object or None if not available
        """
        try:
            eval_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/eval"
            eval_json = f"{eval_path}/evaluation.json"
            
            # Read evaluation.json
            result = self.core.ssh.execute(f"cat {eval_json} 2>/dev/null")
            if result.returncode != 0:
                return None
            
            data = json.loads(result.stdout)
            
            # Check for plot files - get all PNG files in eval directory
            plots = []
            eval_rel_path = f"experiments/{experiment_name}/eval"
            eval_full_path = f"{self.config.remote_shared_dir}/{eval_rel_path}"

            # List all PNG files in eval directory
            result = self.core.ssh.execute(f"ls -1 {eval_full_path}/*.png 2>/dev/null || true")
            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        # Extract just the filename from full path
                        plot_name = line.strip().split('/')[-1]
                        plots.append(plot_name)
            
            return EvaluationResults(
                experiment_name=experiment_name,
                metrics=data.get("metrics", {}),
                plots_available=plots,
                evaluation_path=eval_path
            )
            
        except Exception as e:
            logger.error(f"Error getting evaluation for {experiment_name}: {e}")
            return None
    
    def evaluate_experiment(self, experiment_name: str) -> bool:
        """
        Run evaluation on an experiment.
        
        Args:
            experiment_name: Name of the experiment
            
        Returns:
            True if evaluation started successfully
            
        Raises:
            GraFlagError: If evaluation fails
        """
        try:
            self.core.evaluate(experiment_name)
            return True
        except Exception as e:
            raise GraFlagError(f"Evaluation failed: {str(e)}")
    
    # ========================================================================
    # Service Management
    # ========================================================================
    
    def list_running_services(self) -> List[Dict[str, str]]:
        """
        List currently running Docker services.
        
        Returns:
            List of service information dictionaries
        """
        services = []
        
        try:
            # Use double quotes to avoid shell parsing issues
            result = self.core.ssh.execute(
                'docker service ls --format "{{.Name}}\\t{{.Replicas}}\\t{{.Image}}"'
            )

            if result.returncode == 0 and result.stdout.strip():
                for line in result.stdout.strip().split('\n'):
                    if line.strip():
                        parts = line.split('\t')
                        if len(parts) >= 3:
                            replicas = parts[1]
                            # Derive status from replicas (e.g. "1/1" -> running, "0/1" -> pending)
                            try:
                                current, desired = replicas.split('/')
                                if int(current) >= int(desired) and int(desired) > 0:
                                    status = 'running'
                                elif int(current) > 0:
                                    status = 'partially running'
                                else:
                                    status = 'pending'
                            except (ValueError, ZeroDivisionError):
                                status = 'unknown'
                            services.append({
                                'name': parts[0],
                                'replicas': parts[1],
                                'image': parts[2],
                                'status': status
                            })
        
        except Exception as e:
            logger.error(f"Error listing services: {e}")
        
        return services
    
    def stop_experiment(self, experiment_name: str) -> bool:
        """
        Stop a running experiment.

        Args:
            experiment_name: Name of the experiment

        Returns:
            True if stopped successfully
        """
        try:
            self.core.stop(experiment_name)
            return True
        except Exception as e:
            logger.error(f"Error stopping experiment {experiment_name}: {e}")
            return False

    def delete_experiment(self, experiment_name: str) -> bool:
        """
        Stop and delete an experiment (service + directory).

        Args:
            experiment_name: Name of the experiment

        Returns:
            True if deleted successfully
        """
        try:
            self.core.stop(experiment_name, remove=True)
            return True
        except Exception as e:
            logger.error(f"Error deleting experiment {experiment_name}: {e}")
            return False
    
    def get_experiment_logs(self, experiment_name: str, tail: int = 100) -> List[str]:
        """
        Get logs for an experiment.

        Tries Docker service logs first, then falls back to saved method_output.txt.

        Args:
            experiment_name: Name of the experiment
            tail: Number of recent lines to return

        Returns:
            List of log lines
        """
        try:
            # Try Docker service logs first
            result = self.core.ssh.execute(
                f"docker service logs --tail {tail} {experiment_name} 2>&1"
            )

            if result.returncode == 0 and result.stdout.strip():
                logs = [line for line in result.stdout.strip().split('\n') if line.strip()]
                if logs:
                    return logs

            # Fall back to saved method_output.txt
            output_path = f"{self.config.remote_shared_dir}/experiments/{experiment_name}/method_output.txt"
            result = self.core.ssh.execute(f"tail -n {tail} {output_path} 2>/dev/null")
            if result.returncode == 0 and result.stdout.strip():
                return [line for line in result.stdout.strip().split('\n') if line.strip()]

            return []

        except Exception as e:
            logger.error(f"Error getting logs for {experiment_name}: {e}")
            return []
    
    # ========================================================================
    # File Operations
    # ========================================================================
    
    def download_file(self, remote_path: str, local_path: str) -> bool:
        """
        Download a file from remote shared directory.
        
        Args:
            remote_path: Path relative to shared directory
            local_path: Local destination path
            
        Returns:
            True if successful
        """
        try:
            self.core.copy_files(remote_path, local_path, recursive=False, from_remote=True)
            return True
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return False
    
    def download_directory(self, remote_path: str, local_path: str) -> bool:
        """
        Download a directory from remote shared directory.
        
        Args:
            remote_path: Path relative to shared directory
            local_path: Local destination path
            
        Returns:
            True if successful
        """
        try:
            self.core.copy_files(remote_path, local_path, recursive=True, from_remote=True)
            return True
        except Exception as e:
            logger.error(f"Error downloading directory: {e}")
            return False
