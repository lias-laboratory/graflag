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
    ServiceCleanupResult, ClearReport,
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

    def __init__(self, config_file: Optional[str] = None, log_level: int = logging.INFO):
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

    def count_experiments(self) -> int:
        """Total experiments on the share, for reporting truncation."""
        return self.core.count_experiments()

    def list_experiments(self, limit: int = 50,
                         offset: int = 0) -> List[ExperimentInfo]:
        """List recent experiments."""
        try:
            return self.core.list_experiments(limit=limit, offset=offset)
        except Exception as e:
            logger.error(f"Error listing experiments: {e}")
            return []

    def get_experiment_details(self, experiment_name: str) -> Optional[ExperimentInfo]:
        """Get details for a specific experiment.

        One probe of that experiment; this used to list 500 and search them,
        which also missed any experiment older than the 500 newest.
        """
        try:
            return self.core.get_experiment(experiment_name)
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
        exp_name: Optional[str] = None,
        keep_service: bool = False,
    ) -> str:
        """Run an experiment. Returns experiment name.

        Waits for the run without streaming its output (`follow=False`): the
        dashboard calls this from a background thread, where the stream only
        reached the server's console, and following it kept a Docker SDK call
        in a loop for the whole run.
        """
        return self.core.run(
            method_name=method,
            dataset=dataset,
            tag=tag,
            build=build,
            gpu=gpu,
            method_params=method_params or {},
            exp_name=exp_name,
            keep_service=keep_service,
            follow=False,
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
        """Run evaluation on an experiment (quietly, see :meth:`run`)."""
        self.core.evaluate(experiment_name, follow=False)
        return True

    # ========================================================================
    # Services
    # ========================================================================

    def list_running_services(self) -> List[Dict[str, str]]:
        """List running Docker services.

        Deliberately not error-safe, unlike its neighbours. Returning [] on
        failure made a dropped SSH tunnel look exactly like a cluster with
        nothing running: the Services panel emptied, the count read 0, and
        nothing anywhere said the cluster had not been reached. All three
        callers in the GUI handle the exception -- the route answers 500 so
        the panel can say so, and the updater declines to broadcast a list
        it does not have.
        """
        return self.core.list_services()

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

    def cleanup_services(
        self, experiment: Optional[str] = None, dry_run: bool = False
    ) -> List[ServiceCleanupResult]:
        """Remove Swarm services for finished experiments."""
        try:
            return self.core.cleanup_services(experiment=experiment, dry_run=dry_run)
        except Exception as e:
            logger.error(f"Error cleaning up services: {e}")
            return []

    def clear(self, apply: bool = False, share: bool = True,
              images: bool = True, collect: bool = False) -> ClearReport:
        """Report -- or remove -- storage and images nothing owns any more.

        A failure comes back as an empty report carrying the message, not as
        an exception: the GUI polls this beside the experiment list and a
        raise here would take the page down over housekeeping.
        """
        try:
            return self.core.clear(apply=apply, share=share,
                                   images=images, collect=collect)
        except Exception as e:
            logger.error(f"Error clearing cluster storage: {e}")
            return ClearReport(applied=False, errors=[str(e)])

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
