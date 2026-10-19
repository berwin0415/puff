"""Local HTTP fixture service.

The service deliberately uses only the Python standard library so it can run
inside the validation image without adding a web framework dependency.
"""

from .server import FixtureState, create_server

__all__ = ["FixtureState", "create_server"]
