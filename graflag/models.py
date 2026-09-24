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
    #: ``upstream`` (the image runs the authors' implementation) or
    #: ``reimplementation`` (this repository's code, written from the paper).
    #: Empty for a method whose ``.env`` predates the key -- rendered as
    #: "unstated", which is what it is, rather than assumed to be either.
    integration: str = ""
    #: The shared image this method builds from, from ``IMAGE=`` in its
    #: ``.env``. Empty when the method builds its own -- which is the common
    #: case; the seventeen ``bond_*`` methods share ``bond_base``.
    image: str = ""
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
class ServiceCleanupResult:
    """Outcome of considering one finished service for removal."""
    experiment: str
    removed: bool
    reason: str

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClearItem:
    """One thing `graflag clear` considered removing, and why.

    Every candidate is reported whether or not it was removed, so a dry run
    and an applied run print the same rows -- the difference is `removed`.
    """
    #: experiment | dataset | image | registry | stray
    kind: str
    name: str
    #: The evidence for the verdict, in the words the report prints.
    reason: str
    #: Bytes on disk, 0 when not measured (registry repos before a GC).
    size_bytes: int = 0
    removed: bool = False

    def to_dict(self) -> dict:
        return asdict(self)


@dataclass
class ClearReport:
    """What `graflag clear` found, and what it did about it."""
    #: False for a dry run -- nothing was removed.
    applied: bool
    items: List[ClearItem] = field(default_factory=list)
    #: Bytes actually reclaimed. 0 on a dry run; on an applied run this is
    #: the sum over removed items, which for the registry counts only what a
    #: garbage collection actually freed, not the manifests deleted before it.
    freed_bytes: int = 0
    #: Non-fatal problems. Clearing is housekeeping: a failure to remove one
    #: item must not abort the sweep or fail the caller.
    errors: List[str] = field(default_factory=list)
    #: True when registry blobs were collected, which needs the registry
    #: stopped -- see `core.clear`.
    registry_collected: bool = False

    @property
    def removed(self) -> List[ClearItem]:
        return [i for i in self.items if i.removed]

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


@dataclass
class VerificationReport:
    """What `graflag verify` found in one experiment (see graflag.verify)."""
    experiment_name: str
    #: One {"level": "ERROR" | "WARN" | "OK", "message": ...} per check, in
    #: the order they ran.
    findings: List[Dict[str, str]] = field(default_factory=list)
    failed: int = 0
    warned: int = 0
    passed: int = 0
    #: The probe summary the findings were drawn from: counts, the reported
    #: and evaluated AUCs, the declared split. Never the scores themselves.
    probe: Dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        """True when no check failed. Warnings do not fail an experiment."""
        return self.failed == 0

    def to_dict(self) -> dict:
        return asdict(self)
