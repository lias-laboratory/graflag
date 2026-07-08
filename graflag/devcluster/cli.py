"""CLI handler for graflag devcluster command."""

import os
import sys
import subprocess
from pathlib import Path


def main(hosts_yml: str = None, pubkey: str = None, down: bool = False):
    """Deploy or tear down the development cluster.

    Args:
        hosts_yml: Path to hosts.yml configuration file.
        pubkey: Path to SSH public key file (default: ~/.ssh/id_ed25519.pub).
        down: If True, stop and remove the cluster.
    """
    devcluster_dir = Path(__file__).parent

    if down:
        compose_file = devcluster_dir / "docker-compose.yml"
        if not compose_file.exists():
            print("[ERROR] No docker-compose.yml found -- cluster not deployed?")
            sys.exit(1)
        print("[INFO] Stopping and removing devcluster...")
        # Set empty build args to suppress docker compose warnings during teardown
        env = {**os.environ, "HOST_PUBKEY": "", "MANAGER_PRIVKEY": "", "MANAGER_PUBKEY": ""}
        try:
            result = subprocess.run(
                ["docker", "compose", "down", "-v"],
                cwd=str(devcluster_dir),
                env=env,
            )
            sys.exit(result.returncode)
        except KeyboardInterrupt:
            print("\n[INFO] Interrupted")
            sys.exit(1)

    # Deploy requires hosts_yml
    if not hosts_yml:
        print("[ERROR] devcluster requires --hosts <path-to-hosts.yml> or --down")
        sys.exit(1)

    hosts_path = Path(hosts_yml).resolve()
    if not hosts_path.exists():
        print(f"[ERROR] hosts.yml not found: {hosts_path}")
        sys.exit(1)

    if pubkey is None:
        pubkey = str(Path.home() / ".ssh" / "id_ed25519.pub")

    pubkey_path = Path(pubkey).resolve()
    if not pubkey_path.exists():
        print(f"[ERROR] Public key not found: {pubkey_path}")
        sys.exit(1)

    deploy_script = devcluster_dir / "deploy.sh"
    if not deploy_script.exists():
        print(f"[ERROR] deploy.sh not found: {deploy_script}")
        sys.exit(1)

    # Copy hosts.yml to devcluster dir if it's not already there
    target_hosts = devcluster_dir / "hosts.yml"
    if hosts_path != target_hosts.resolve():
        import shutil
        shutil.copy2(str(hosts_path), str(target_hosts))

    try:
        result = subprocess.run(
            ["bash", str(deploy_script), str(pubkey_path)],
            cwd=str(devcluster_dir),
        )
        sys.exit(result.returncode)
    except KeyboardInterrupt:
        print("\n[INFO] Interrupted")
        sys.exit(1)
