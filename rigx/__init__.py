"""rigx — a Nix-backed declarative build system."""

from importlib.metadata import PackageNotFoundError, version as _pkg_version

try:
    __version__ = _pkg_version("rigx")
except PackageNotFoundError:
    # Editable install without metadata recorded, or running from a source
    # checkout that hasn't been `pip install`-ed. Fall back to a marker that
    # makes the situation obvious rather than lying.
    __version__ = "0.0.0+unknown"
