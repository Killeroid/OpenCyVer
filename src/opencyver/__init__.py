from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("OpenCyVer")
except PackageNotFoundError:
    __version__ = "unknown"

from opencyver.validators.syntax_validator import SyntaxValidator

__all__ = [
    "SyntaxValidator",
    "__version__",
]
