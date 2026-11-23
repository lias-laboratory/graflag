"""Exceptions shared by the client's layers.

``GraFlagError`` lives here rather than in core.py so that ssh.py, which
core imports, can raise a subclass of it; ``graflag.core.GraFlagError`` is
the same class, re-exported.
"""


class GraFlagError(Exception):
    """Custom exception for GraFlag errors."""


class ManagerUnreachable(GraFlagError):
    """ssh itself failed: the manager was not reached, so nothing ran there."""
