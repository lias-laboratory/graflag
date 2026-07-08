"""
GraFlag - Graph Anomaly Detection Benchmarking Tool

A tool for benchmarking Graph Anomaly Detection methods using Docker Swarm
across multiple nodes with shared NFS storage.
"""

from .core import GraFlag, GraFlagError
from .config import GraflagConfig, CONFIG_FILE

__version__ = "1.0.0"
__all__ = ["GraFlag", "GraFlagError", "GraflagConfig", "CONFIG_FILE"]
