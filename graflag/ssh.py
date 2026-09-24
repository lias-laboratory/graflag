"""SSH operations for GraFlag."""

import shlex
import subprocess
from pathlib import Path
from typing import List
import logging

logger = logging.getLogger(__name__)


def remote_path(*parts: str) -> str:
    """Join path components and quote the result for the remote shell.

    Every remote command is a string interpreted by the manager's shell, so
    any component that comes from user input (experiment name, method name,
    dataset) must be quoted before interpolation. Use this for paths and
    :func:`shlex.quote` for bare values.

    Separators are collapsed only at the join boundaries -- the content of a
    component is never altered, so a name that happens to contain ``/`` stays
    intact (and quoted) rather than being silently rewritten.
    """
    cleaned = []
    last = len(parts) - 1
    for i, part in enumerate(parts):
        p = str(part)
        if i > 0:
            p = p.lstrip("/")
        if i < last:
            p = p.rstrip("/")
        if p:
            cleaned.append(p)
    return shlex.quote("/".join(cleaned))


#: Options that make ssh fail instead of asking. A client with nobody at the
#: keyboard -- the MCP server, whose stdin and stdout are the protocol -- must
#: not wait on a password or passphrase prompt: the tool call would hang until
#: the client gave up, with nothing saying why.
NON_INTERACTIVE_OPTS = ["-o", "BatchMode=yes", "-o", "ConnectTimeout=15"]


class SSHManager:
    """Handle SSH operations to remote manager."""

    def __init__(self, manager_ip: str, ssh_port: str = "22", ssh_key: str = None,
                 batch_mode: bool = False):
        """Initialize SSH manager.

        Args:
            batch_mode: Never prompt (see :data:`NON_INTERACTIVE_OPTS`). Off by
                default, so a CLI user whose key needs a passphrase still gets
                asked for it.
        """
        self.manager_ip = manager_ip
        self.ssh_port = ssh_port
        self.ssh_key = ssh_key
        self.batch_mode = batch_mode

    def _option_args(self) -> List[str]:
        """The -o options every ssh this manager starts carries."""
        opts = ["-o", "StrictHostKeyChecking=no"]
        if self.batch_mode:
            opts.extend(NON_INTERACTIVE_OPTS)
        return opts

    def _ssh_args(self) -> List[str]:
        """Build the ssh argv prefix shared by execute() and log following."""
        args = ["ssh"]
        if self.ssh_key:
            args.extend(["-i", str(Path(self.ssh_key).expanduser())])
        args.extend(["-p", str(self.ssh_port)])
        args.extend(self._option_args())
        args.append(f"root@{self.manager_ip}")
        return args

    def execute(self, command: str, capture_output: bool = True) -> subprocess.CompletedProcess:
        """Execute command on manager via SSH.

        The command is passed to ssh as a single argv element, so no local
        shell ever parses it. This keeps quoting, heredocs and newlines in
        ``command`` intact -- the manager's shell sees exactly what was built
        here. Callers are still responsible for quoting values they
        interpolate (see :func:`remote_path`), because the *remote* shell does
        parse the string.
        """
        ssh_args = self._ssh_args() + [command]
        logger.debug(f"Executing SSH command: {command}")

        # stdin is /dev/null because ssh forwards whatever it can read from its
        # own stdin to the remote command, and reads eagerly. Nothing here
        # sends data that way (heredocs carry it inside `command`), so all an
        # inherited stdin could do is be consumed: the rest of a shell loop's
        # input (`while read m; do graflag run -m "$m" ...; done < list`), or,
        # under the MCP server, the client's next requests.
        return subprocess.run(
            ssh_args, capture_output=capture_output, text=True,
            stdin=subprocess.DEVNULL,
        )

    def path_exists(self, remote_shared_dir: str, path: str) -> bool:
        """Check if path exists on remote manager."""
        result = self.execute(f"test -e {remote_path(remote_shared_dir, path)}")
        return result.returncode == 0

    def read_file(self, remote_shared_dir: str, path: str) -> str:
        """Read file content from remote manager."""
        result = self.execute(f"cat {remote_path(remote_shared_dir, path)}")
        if result.returncode == 0:
            return result.stdout
        return ""

    def mkdir(self, remote_shared_dir: str, path: str) -> bool:
        """Create directory on remote manager."""
        result = self.execute(f"mkdir -p {remote_path(remote_shared_dir, path)}")
        return result.returncode == 0

    def list_dir(self, remote_shared_dir: str, path: str) -> List[str]:
        """List directory contents on remote manager."""
        result = self.execute(
            f"ls -1 {remote_path(remote_shared_dir, path)} 2>/dev/null || true"
        )
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
            recursive: Accepted for compatibility; rsync -a always recurses
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
        self.execute(f"mkdir -p {shlex.quote(parent_dir)}")
        
        logger.info(f"[INFO] Copying {len(local_paths)} item(s) to {self.manager_ip}:{remote_dest}")
        
        # Build rsync command - more robust than scp.
        # -a implies -r, so recursion is always on and the `recursive`
        # argument has no effect. Left that way deliberately: rsync without
        # -r skips directories *silently* and still exits 0, so honouring the
        # flag would turn a harmless no-op into a copy that quietly does
        # nothing. The CLI help documents recursion as automatic instead.
        rsync_parts = ["rsync", "-avz", "--progress", "--force"]
        
        # SSH options
        ssh_opts = self._option_args()
        if self.ssh_key:
            key_path = Path(self.ssh_key).expanduser()
            if str(key_path).endswith('.pub'):
                key_path = key_path.with_suffix('')
            ssh_opts.extend(["-i", str(key_path)])
        
        ssh_opts.extend(["-p", self.ssh_port])
        
        rsync_parts.extend(["-e", f"ssh {' '.join(ssh_opts)}"])
        
        # Add all source paths. rsync's trailing slash is load-bearing:
        # "src/" copies the *contents* of src into dest, "src" copies src
        # itself as a child of dest. Path() normalises that slash away, so
        # sync()'s f"{local_dir}/" silently became a copy into
        # methods/<name>/<name>/ -- a directory nothing reads -- and still
        # reported [OK]. Validate through Path, transmit the caller's spelling.
        for original, local_path_obj in zip(local_paths, local_path_objs):
            trailing = "/" if str(original).endswith(("/", "/.")) else ""
            rsync_parts.append(str(local_path_obj) + trailing)
        
        # Add destination
        rsync_parts.append(f"root@{self.manager_ip}:{remote_dest}")
        
        logger.debug(f"Executing rsync command: {' '.join(rsync_parts)}")
        
        result = subprocess.run(rsync_parts, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)  # see execute()
        
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
        ssh_opts = self._option_args()
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
        
        result = subprocess.run(rsync_parts, capture_output=True, text=True,
                                stdin=subprocess.DEVNULL)  # see execute()
        
        if result.returncode == 0:
            logger.info(f"[OK] Successfully copied {len(remote_paths)} item(s) to {local_dest}")
        else:
            raise RuntimeError(f"Failed to copy files with rsync: {result.stderr}")
        
        return str(local_dest_obj)