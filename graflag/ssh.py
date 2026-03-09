"""SSH operations for GraFlag."""

import subprocess
from pathlib import Path
from typing import List
import logging

logger = logging.getLogger(__name__)


class SSHManager:
    """Handle SSH operations to remote manager."""
    
    def __init__(self, manager_ip: str, ssh_port: str = "22", ssh_key: str = None):
        """Initialize SSH manager."""
        self.manager_ip = manager_ip
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
    
    def execute(self, command: str, capture_output: bool = True) -> subprocess.CompletedProcess:
        """Execute command on manager via SSH."""
        ssh_cmd = f"ssh -i {self.ssh_key} -p {self.ssh_port} -o StrictHostKeyChecking=no root@{self.manager_ip} '{command}'"
        logger.debug(f"Executing SSH command: {ssh_cmd}")

        return subprocess.run(
            ssh_cmd, shell=True, capture_output=capture_output, text=True
        )
    
    def path_exists(self, remote_shared_dir: str, path: str) -> bool:
        """Check if path exists on remote manager."""
        result = self.execute(f"test -e {remote_shared_dir}/{path}")
        return result.returncode == 0
    
    def read_file(self, remote_shared_dir: str, path: str) -> str:
        """Read file content from remote manager."""
        result = self.execute(f"cat {remote_shared_dir}/{path}")
        if result.returncode == 0:
            return result.stdout
        return ""
    
    def mkdir(self, remote_shared_dir: str, path: str) -> bool:
        """Create directory on remote manager."""
        result = self.execute(f"mkdir -p {remote_shared_dir}/{path}")
        return result.returncode == 0
    
    def list_dir(self, remote_shared_dir: str, path: str) -> List[str]:
        """List directory contents on remote manager."""
        result = self.execute(f"ls -1 {remote_shared_dir}/{path} 2>/dev/null || true")
        if result.returncode == 0 and result.stdout.strip():
            return [
                item.strip()
                for item in result.stdout.strip().split("\n")
                if item.strip()
            ]
        return []
    
    def copy_files(self, source_paths, dest_path: str, recursive: bool = False, from_remote: bool = False) -> str:
        """
        Copy files/directories bidirectionally via rsync.
        
        Args:
            source_paths: Source path(s) - can be single string or list
            dest_path: Destination path
            recursive: Include recursive flag (automatically added for directories)
            from_remote: If True, copy from remote to local; if False (default), copy from local to remote
        
        Returns:
            Destination path
        """
        # Handle single string or list of paths
        if isinstance(source_paths, str):
            source_paths = [source_paths]
        
        if from_remote:
            # Copy from remote to local
            return self._copy_from_remote(source_paths, dest_path, recursive)
        else:
            # Copy from local to remote
            return self._copy_to_remote(source_paths, dest_path, recursive)
    
    def _copy_to_remote(self, local_paths, remote_dest: str, recursive: bool = False) -> str:
        """Copy files/directories from local to remote via rsync."""
        # Validate all local paths exist
        local_path_objs = []
        for local_path in local_paths:
            local_path_obj = Path(local_path).expanduser()
            if not local_path_obj.exists():
                raise FileNotFoundError(f"Local path does not exist: {local_path}")
            local_path_objs.append(local_path_obj)
        
        # Ensure remote destination directory exists
        parent_dir = str(Path(remote_dest).parent)
        self.execute(f"mkdir -p {parent_dir}")
        
        logger.info(f"[INFO] Copying {len(local_paths)} item(s) to {self.manager_ip}:{remote_dest}")
        
        # Build rsync command - more robust than scp
        rsync_parts = ["rsync", "-avz", "--progress", "--force"]
        
        # SSH options
        ssh_opts = ["-o", "StrictHostKeyChecking=no"]
        if self.ssh_key:
            key_path = Path(self.ssh_key).expanduser()
            if str(key_path).endswith('.pub'):
                key_path = key_path.with_suffix('')
            ssh_opts.extend(["-i", str(key_path)])
        
        ssh_opts.extend(["-p", self.ssh_port])
        
        rsync_parts.extend(["-e", f"ssh {' '.join(ssh_opts)}"])
        
        # Add all source paths
        for local_path_obj in local_path_objs:
            rsync_parts.append(str(local_path_obj))
        
        # Add destination
        rsync_parts.append(f"root@{self.manager_ip}:{remote_dest}")
        
        logger.debug(f"Executing rsync command: {' '.join(rsync_parts)}")
        
        result = subprocess.run(rsync_parts, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"[OK] Successfully copied {len(local_paths)} item(s) to {remote_dest}")
        else:
            raise RuntimeError(f"Failed to copy files with rsync: {result.stderr}")
        
        return remote_dest
    
    def _copy_from_remote(self, remote_paths, local_dest: str, recursive: bool = False) -> str:
        """Copy files/directories from remote to local via rsync."""
        # Ensure local destination directory exists
        local_dest_obj = Path(local_dest).expanduser()
        local_dest_obj.parent.mkdir(parents=True, exist_ok=True)
        
        logger.info(f"[INFO] Copying {len(remote_paths)} item(s) from {self.manager_ip} to {local_dest}")
        
        # Build rsync command
        rsync_parts = ["rsync", "-avz", "--progress", "--force"]
        
        # SSH options
        ssh_opts = ["-o", "StrictHostKeyChecking=no"]
        if self.ssh_key:
            key_path = Path(self.ssh_key).expanduser()
            if str(key_path).endswith('.pub'):
                key_path = key_path.with_suffix('')
            ssh_opts.extend(["-i", str(key_path)])
        
        ssh_opts.extend(["-p", self.ssh_port])
        
        rsync_parts.extend(["-e", f"ssh {' '.join(ssh_opts)}"])
        
        # Add all source paths (remote)
        for remote_path in remote_paths:
            rsync_parts.append(f"root@{self.manager_ip}:{remote_path}")
        
        # Add destination (local)
        rsync_parts.append(str(local_dest_obj))
        
        logger.debug(f"Executing rsync command: {' '.join(rsync_parts)}")
        
        result = subprocess.run(rsync_parts, capture_output=True, text=True)
        
        if result.returncode == 0:
            logger.info(f"[OK] Successfully copied {len(remote_paths)} item(s) to {local_dest}")
        else:
            raise RuntimeError(f"Failed to copy files with rsync: {result.stderr}")
        
        return str(local_dest_obj)