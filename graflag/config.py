"""Configuration management for GraFlag."""

import os
from pathlib import Path
from typing import Dict, Optional

from .utils import parse_env_line
import logging

logger = logging.getLogger(__name__)

CONFIG_DIR = Path.home() / ".config" / "graflag"
CONFIG_FILE = CONFIG_DIR / "config.env"

DEFAULTS = {
    "SSH_PORT": "22",
    "SHARED_DIR": "/shared",
    "NFS_PORT": "2049",
    # Where a method image gets graflag_runner and graflag_bond: "local"
    # builds them from SHARED_DIR/libs, "pypi" installs the published wheels.
    #
    # Local is the default because it is what the rest of the tool already
    # implies. `graflag sync --lib` exists to update the libraries on the
    # share, and with PyPI wheels baked into every image it changed nothing
    # that ran -- the command did its job and the next container still ran the
    # published code. The share is already the source of truth for methods and
    # datasets; this makes it the source of truth for the libraries too.
    #
    # Set GRAFLAG_LIBS=pypi to pin images to a release instead.
    "GRAFLAG_LIBS": "local",
}

PROMPTS = [
    ("MANAGER_IP", "Manager IP address", None),
    ("SSH_PORT", "SSH port", "22"),
    ("SSH_KEY", "SSH private key path", "~/.ssh/id_ed25519"),
    ("SHARED_DIR", "Remote shared directory", "/shared"),
    ("HOSTS_FILE", "Hosts file (hosts.yml) path", "hosts.yml"),
]


def _looks_like_graflag_config(path: Path) -> bool:
    """True if the file defines MANAGER_IP, the one required GraFlag key.

    A bare `.env` in the working directory is a near-universal convention, so
    picking one up purely because it exists meant any unrelated project's file
    became the GraFlag config -- and `graflag setup` then refused to run the
    wizard because "the config exists", leaving no way out but deleting
    somebody else's file.
    """
    try:
        with open(path, "r") as fh:
            return any(
                (parsed := parse_env_line(line)) and parsed[0] == "MANAGER_IP"
                for line in fh
            )
    except OSError:
        return False


def get_config_path(override: Optional[str] = None) -> Path:
    """Resolve the config file path.

    Order: explicit override > a GraFlag-looking `.env` in the working
    directory > the standard user config location.
    """
    if override:
        return Path(override).expanduser()

    cwd_env = Path.cwd() / ".env"
    if cwd_env.exists():
        if _looks_like_graflag_config(cwd_env):
            return cwd_env
        logger.debug(
            f"Ignoring {cwd_env}: no MANAGER_IP, so it is not a GraFlag config"
        )
    return CONFIG_FILE


def init_config(target: Optional[Path] = None) -> Path:
    """Interactively create configuration file.

    Writes to `target` when given. Hardcoding CONFIG_FILE meant
    `graflag setup --config /etc/graflag/prod.env` ran the whole wizard, wrote
    somewhere else, and then failed for the file the user named -- with no way
    to break the loop.
    """
    target = Path(target).expanduser() if target else CONFIG_FILE
    print("GraFlag configuration")
    print(f"Config will be saved to: {target}\n")

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

    target.parent.mkdir(parents=True, exist_ok=True)
    with open(target, "w") as f:
        f.write("# GraFlag Configuration\n")
        for key, _, _ in PROMPTS:
            f.write(f"{key}={values[key]}\n")

    print(f"\n[OK] Configuration saved to {target}")
    return target


class GraflagConfig:
    """Handle configuration loading and validation for GraFlag."""

    def __init__(self, config_file: Optional[str] = None):
        """Initialize configuration from file."""
        self.config_path = get_config_path(config_file)
        if config_file and not self.config_path.exists():
            # Silently falling back surfaced as a misleading
            # "Missing required configuration: MANAGER_IP".
            raise ValueError(f"Configuration file not found: {self.config_path}")
        self.config = self._load_config()
        self._validate_required_config()

    def _load_config(self) -> Dict[str, str]:
        """Load configuration from .env file."""
        config = dict(DEFAULTS)

        if not self.config_path.exists():
            return config

        with open(self.config_path, "r") as f:
            for line in f:
                parsed = parse_env_line(line)
                if parsed:
                    config[parsed[0]] = parsed[1]

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

    @property
    def graflag_libs(self) -> str:
        return self.get("GRAFLAG_LIBS", "local").strip().lower()
