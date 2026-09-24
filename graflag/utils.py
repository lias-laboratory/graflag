"""Utility functions for GraFlag."""

import re
from typing import Dict
import logging

logger = logging.getLogger(__name__)


#: What a method, dataset, experiment or image tag name may contain. Names that
#: arrive from outside the client -- the dashboard's HTTP requests, an MCP
#: client's tool calls, which an LLM composes -- are checked against this
#: before they reach anything that builds a remote command.
_SAFE_NAME = re.compile(r'^[A-Za-z0-9._-]{1,200}$')


def valid_name(name) -> bool:
    """True when `name` is safe to interpolate into a remote command."""
    return (isinstance(name, str) and bool(_SAFE_NAME.match(name))
            and '..' not in name)


def load_method_env(ssh_manager, remote_shared_dir: str, method_name: str) -> Dict[str, str]:
    """Load method environment variables from .env file.
    
    Args:
        ssh_manager: SSH manager instance for remote operations
        remote_shared_dir: Path to remote shared directory
        method_name: Name of the method
        
    Returns:
        Dictionary of environment variables from the method's .env file
    """
    # Imported here, not at the top: graflag-shared's contract tests load this
    # file on its own, by path, to compare parse_env_line with their copy.
    from .ssh import remote_path

    # Quoted: the method name is interpolated into a command the manager's
    # shell parses, and it was the one path here that went in bare.
    env_file_path = remote_path(remote_shared_dir, "methods", method_name, ".env")
    env_vars = {}

    # Check if .env file exists
    path_check = ssh_manager.execute(f"test -f {env_file_path} && echo 'exists' || echo 'missing'")
    if path_check.returncode == 0 and 'exists' in path_check.stdout:
        # Read .env file
        result = ssh_manager.execute(f"cat {env_file_path}")
        if result.returncode == 0:
            # Parse .env file into dictionary
            for line in result.stdout.split("\n"):
                parsed = parse_env_line(line)
                if parsed:
                    env_vars[parsed[0]] = parsed[1]

    return env_vars


def parse_env_line(line: str):
    """Parse one line of a ``.env`` file into ``(key, value)``, or None.

    Returns None for blanks and comment lines.

    Handles the idioms a hand-written ``.env`` actually contains, each of which
    used to be carried into the value verbatim:

    - ``export KEY=value`` -- the key became ``"export KEY"``, so the real key
      was never set and a file that visibly defined MANAGER_IP failed as
      "missing MANAGER_IP".
    - ``KEY=value  # comment`` -- the comment became part of the value, so
      ``SSH_PORT=22  # default`` produced ``ssh: Bad port '22  # default'``.
    - ``KEY="value"`` -- the quotes became part of the value. Since commands are
      built as argv with no shell to strip them, ``MANAGER_IP="10.0.0.1"``
      produced ``Could not resolve hostname "10.0.0.1"``.

    An inline comment must be preceded by whitespace, so values that legitimately
    contain ``#`` are preserved -- method ``.env`` files carry URLs with
    fragments, e.g. ``SOURCE_CODE=https://docs.pygod.org/...#pygod.detector.CoLA``.
    """
    line = line.strip()
    if not line or line.startswith("#") or "=" not in line:
        return None

    if line.startswith("export ") or line.startswith("export\t"):
        line = line[len("export"):].lstrip()
        if "=" not in line:
            return None

    key, _, value = line.partition("=")
    key = key.strip()
    if not key:
        return None

    value = value.strip()

    # A quoted value is taken literally, including any '#' inside it, and ends
    # at its closing quote -- whatever follows is a comment.
    #
    # Matching on "first and last character are the same quote" instead put the
    # two rules in the wrong order: KEY="value"  # note ends in `e`, so it was
    # not a quoted value, and the comment rule then returned `"value"` with the
    # quotes still attached. Since commands are built as argv with no shell to
    # strip them, that is the `Could not resolve hostname "10.0.0.1"` failure
    # this function exists to prevent, reachable by adding a comment to a line
    # that already worked.
    if value[:1] in ("'", '"'):
        closing = value.find(value[0], 1)
        if closing != -1:
            return key, value[1:closing]

    # Otherwise an inline comment starts at whitespace followed by '#'.
    for i, ch in enumerate(value):
        if ch == "#" and i > 0 and value[i - 1] in (" ", "\t"):
            value = value[:i]
            break

    return key, value.strip()
