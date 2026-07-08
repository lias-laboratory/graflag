"""Data models for GraFlag API responses."""

from typing import Dict, List, Optional, Any
from dataclasses import dataclass, field, asdict


@dataclass
class ClusterInfo:
    """Cluster status information."""
    manager_ip: str
    is_connected: bool
    swarm_initialized: bool
    worker_nodes: List[Dict[str, str]] = field(default_factory=list)
    shared_dir: str = ""
    shared_contents: List[str] = field(default_factory=list)
    services: List[Dict] = field(default_factory=list)
    error: Optional[str] = None

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class MethodInfo:
    """Method metadata."""
    name: str
    description: str = ""
    source_code: str = ""
    supported_data: str = ""
    parameters: Dict[str, Any] = field(default_factory=dict)
    has_dockerfile: bool = False
    has_env: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class DatasetInfo:
    """Dataset metadata."""
    name: str
    path: str = ""
    size_mb: float = 0.0
    file_count: int = 0

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
class RunProgress:
    """Progress information for run execution."""
    experiment_name: str
    status: str  # "building", "starting", "running", "completed", "failed"
    message: str = ""
    log_lines: List[str] = field(default_factory=list)

    def to_dict(self) -> dict:
        return asdict(self)
