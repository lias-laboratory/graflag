"""Configuration management for GraFlag."""

import os
from pathlib import Path
from typing import Dict, Optional
import logging

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "graflag"
CONFIG_FILE = CONFIG_DIR / "config.env"

DEFAULTS = {
    "SSH_PORT": "22",
    "SHARED_DIR": "/shared",
    "NFS_PORT": "2049",
}

PROMPTS = [
    ("MANAGER_IP", "Manager IP address", None),
    ("SSH_PORT", "SSH port", "22"),
    ("SSH_KEY", "SSH private key path", "~/.ssh/id_ed25519"),
    ("SHARED_DIR", "Remote shared directory", "/shared"),
    ("HOSTS_FILE", "Hosts file (hosts.yml) path", "hosts.yml"),
]


def get_config_path(override: Optional[str] = None) -> Path:
    """Resolve config file path. Checks override, then cwd .env, then standard location."""
    if override and override != ".env":
        return Path(override)
    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        return cwd_env
    return CONFIG_FILE


def init_config() -> Path:
    """Interactively create configuration file."""
    print("GraFlag configuration")
    print(f"Config will be saved to: {CONFIG_FILE}\n")

    values = {}
    for key, prompt, default in PROMPTS:
        if default:
            raw = input(f"  {prompt} [{default}]: ").strip()
            values[key] = raw if raw else default
        else:
            while True:
                raw = input(f"  {prompt}: ").strip()
                if raw:
                    values[key] = raw
                    break
                print(f"    {key} is required.")

    CONFIG_DIR.mkdir(parents=True, exist_ok=True)
    with open(CONFIG_FILE, "w") as f:
        f.write("# GraFlag Configuration\n")
        for key, _, _ in PROMPTS:
            f.write(f"{key}={values[key]}\n")

    print(f"\n[OK] Configuration saved to {CONFIG_FILE}")
    return CONFIG_FILE


class GraflagConfig:
    """Handle configuration loading and validation for GraFlag."""

    def __init__(self, config_file: str = ".env"):
        """Initialize configuration from file."""
        self.config_path = get_config_path(config_file)
        self.config = self._load_config()
        self._validate_required_config()

    def _load_config(self) -> Dict[str, str]:
        """Load configuration from .env file."""
        config = dict(DEFAULTS)

        if not self.config_path.exists():
            return config

        with open(self.config_path, "r") as f:
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
            raise ValueError(
                f"Missing required configuration: {', '.join(missing_keys)}. "
                f"Run 'graflag setup' to configure."
            )

    def get(self, key: str, default: Optional[str] = None) -> Optional[str]:
        """Get configuration value."""
        return self.config.get(key, default)

    @property
    def remote_shared_dir(self) -> str:
        return self.get("SHARED_DIR", "/shared")

    @property
    def manager_ip(self) -> str:
        return self.get("MANAGER_IP")

    @property
    def ssh_port(self) -> str:
        return self.get("SSH_PORT", "22")

    @property
    def ssh_key(self) -> Optional[str]:
        return self.get("SSH_KEY")

    @property
    def nfs_port(self) -> str:
        return self.get("NFS_PORT", "2049")

    @property
    def hosts_file(self) -> Optional[str]:
        return self.get("HOSTS_FILE", "hosts.yml")
