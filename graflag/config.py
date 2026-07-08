"""Configuration management for GraFlag."""

import os
from pathlib import Path
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)


class GraflagConfig:
    """Handle configuration loading and validation for GraFlag."""
    
    def __init__(self, config_file: str = ".env"):
        """Initialize configuration from file."""
        self.config_file = config_file
        self.config = self._load_config()
        self._validate_required_config()
    
    def _load_config(self) -> Dict[str, str]:
        """Load configuration from .env file."""
        config = {}
        config_path = Path(self.config_file)

        if not config_path.exists():
            logger.warning(f"Configuration file {self.config_file} not found")
            return config

        with open(config_path, "r") as f:
            for line in f:
                line = line.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    config[key.strip()] = value.strip()

        return config
    
    def _validate_required_config(self):
        """Validate that required configuration is present."""
        required_keys = ["MANAGER_IP"]
        missing_keys = [key for key in required_keys if not self.get(key)]
        
        if missing_keys:
            raise ValueError(f"Missing required configuration: {', '.join(missing_keys)}")
    
    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get configuration value."""
        return self.config.get(key, default)
    
    @property
    def remote_shared_dir(self) -> str:
        """Get remote shared directory path."""
        return self.get("SHARED_DIR", "/shared")
    
    @property
    def manager_ip(self) -> str:
        """Get manager IP address."""
        return self.get("MANAGER_IP")
    
    @property
    def ssh_port(self) -> str:
        """Get SSH port."""
        return self.get("SSH_PORT", "22")
    
    @property
    def ssh_key(self) -> Optional[str]:
        """Get SSH key path."""
        return self.get("SSH_KEY")
    
    @property
    def nfs_port(self) -> str:
        """Get NFS port."""
        return self.get("NFS_PORT", "2049")