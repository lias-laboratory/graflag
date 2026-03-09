"""Utility functions for GraFlag."""

from typing import Dict
import logging

logger = logging.getLogger(__name__)


def load_method_env(ssh_manager, remote_shared_dir: str, method_name: str) -> Dict[str, str]:
    """Load method environment variables from .env file.
    
    Args:
        ssh_manager: SSH manager instance for remote operations
        remote_shared_dir: Path to remote shared directory
        method_name: Name of the method
        
    Returns:
        Dictionary of environment variables from the method's .env file
    """
    env_file_path = f"{remote_shared_dir}/methods/{method_name}/.env"
    env_vars = {}

    # Check if .env file exists
    path_check = ssh_manager.execute(f"test -f {env_file_path} && echo 'exists' || echo 'missing'")
    if path_check.returncode == 0 and 'exists' in path_check.stdout:
        # Read .env file
        result = ssh_manager.execute(f"cat {env_file_path}")
        if result.returncode == 0:
            # Parse .env file into dictionary
            for line in result.stdout.split("\n"):
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    env_vars[key.strip()] = value.strip()

    return env_vars
