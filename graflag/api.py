"""
GraFlag Python API for GUI Integration.

Thin wrapper around GraFlag core that provides error-safe access
and returns structured dataclass objects suitable for GUI/web consumption.
"""

from typing import Dict, List, Optional, Any, Callable
import logging

from .core import GraFlag, GraFlagError
from .config import GraflagConfig
from .models import (
    ClusterInfo, MethodInfo, DatasetInfo, ExperimentInfo,
    ExperimentResults, EvaluationResults, RunProgress,
)

logger = logging.getLogger(__name__)


class GraFlagAPI:
    """
    High-level Python API for GraFlag operations.

    All methods return structured data (dataclasses) and catch exceptions
    to avoid crashing the GUI. Use the core GraFlag class directly for
    CLI-style usage where exceptions should propagate.

    Usage:
        api = GraFlagAPI(config_file=".env")

        cluster = api.get_cluster_info()
        methods = api.list_methods()
        experiments = api.list_experiments()
    """

    def __init__(self, config_file: str = ".env", log_level: int = logging.INFO):
        logging.basicConfig(level=log_level)
        self.core = GraFlag(config_file)
        self.config = self.core.config

    # ========================================================================
    # Cluster
    # ========================================================================

    def get_cluster_info(self) -> ClusterInfo:
        """Get cluster status information."""
        return self.core.status()

    def setup_cluster(self) -> Dict[str, Any]:
        """Setup GraFlag cluster."""
        try:
            self.core.setup()
            return {"success": True, "message": "Cluster setup completed"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # ========================================================================
    # Resources
    # ========================================================================

    def list_methods(self) -> List[MethodInfo]:
        """List available methods."""
        try:
            return self.core.list_methods()
        except Exception as e:
            logger.error(f"Error listing methods: {e}")
            return []

    def get_method_details(self, method_name: str) -> Optional[MethodInfo]:
        """Get details for a specific method."""
        try:
            methods = self.core.list_methods()
            for m in methods:
                if m.name == method_name:
                    return m
            return None
        except Exception as e:
            logger.error(f"Error getting method details: {e}")
            return None

    def list_datasets(self) -> List[DatasetInfo]:
        """List available datasets."""
        try:
            return self.core.list_datasets()
        except Exception as e:
            logger.error(f"Error listing datasets: {e}")
            return []

    def list_experiments(self, limit: int = 50) -> List[ExperimentInfo]:
        """List recent experiments."""
        try:
            return self.core.list_experiments(limit=limit)
        except Exception as e:
            logger.error(f"Error listing experiments: {e}")
            return []

    def get_experiment_details(self, experiment_name: str) -> Optional[ExperimentInfo]:
        """Get details for a specific experiment."""
        try:
            experiments = self.core.list_experiments(limit=500)
            for e in experiments:
                if e.name == experiment_name:
                    return e
            return None
        except Exception as e:
            logger.error(f"Error getting experiment details: {e}")
            return None

    # ========================================================================
    # Run
    # ========================================================================

    def run(
        self,
        method: str,
        dataset: str,
        tag: str = "latest",
        build: bool = False,
        gpu: bool = True,
        method_params: Optional[Dict[str, Any]] = None,
        on_progress: Optional[Callable[[RunProgress], None]] = None,
    ) -> str:
        """Run an experiment. Returns experiment name."""
        return self.core.run(
            method_name=method,
            dataset=dataset,
            tag=tag,
            build=build,
            gpu=gpu,
            method_params=method_params or {},
        )

    # ========================================================================
    # Results
    # ========================================================================

    def get_experiment_results(self, experiment_name: str) -> Optional[ExperimentResults]:
        """Get experiment results."""
        try:
            return self.core.get_experiment_results(experiment_name)
        except Exception as e:
            logger.error(f"Error getting results: {e}")
            return None

    def get_evaluation_results(self, experiment_name: str) -> Optional[EvaluationResults]:
        """Get evaluation results."""
        try:
            return self.core.get_evaluation_results(experiment_name)
        except Exception as e:
            logger.error(f"Error getting evaluation: {e}")
            return None

    def evaluate_experiment(self, experiment_name: str) -> bool:
        """Run evaluation on an experiment."""
        self.core.evaluate(experiment_name)
        return True

    # ========================================================================
    # Services
    # ========================================================================

    def list_running_services(self) -> List[Dict[str, str]]:
        """List running Docker services."""
        try:
            return self.core.list_services()
        except Exception as e:
            logger.error(f"Error listing services: {e}")
            return []

    def stop_experiment(self, experiment_name: str) -> bool:
        """Stop a running experiment."""
        try:
            self.core.stop(experiment_name)
            return True
        except Exception as e:
            logger.error(f"Error stopping experiment: {e}")
            return False

    def delete_experiment(self, experiment_name: str) -> bool:
        """Stop and delete an experiment."""
        try:
            self.core.stop(experiment_name, remove=True)
            return True
        except Exception as e:
            logger.error(f"Error deleting experiment: {e}")
            return False

    def get_experiment_logs(self, experiment_name: str, tail: int = 100) -> List[str]:
        """Get recent logs for an experiment."""
        try:
            return self.core.get_logs(experiment_name, tail=tail)
        except Exception as e:
            logger.error(f"Error getting logs: {e}")
            return []

    # ========================================================================
    # File Operations
    # ========================================================================

    def download_file(self, remote_path: str, local_path: str) -> bool:
        """Download a file from remote shared directory."""
        try:
            self.core.copy_files(remote_path, local_path, recursive=False, from_remote=True)
            return True
        except Exception as e:
            logger.error(f"Error downloading file: {e}")
            return False

    def download_directory(self, remote_path: str, local_path: str) -> bool:
        """Download a directory from remote shared directory."""
        try:
            self.core.copy_files(remote_path, local_path, recursive=True, from_remote=True)
            return True
        except Exception as e:
            logger.error(f"Error downloading directory: {e}")
            return False
