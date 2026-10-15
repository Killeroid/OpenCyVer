from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name: it must match `name` in pyproject.toml
    __version__ = version("OpenCyVer")
except PackageNotFoundError:
    __version__ = "unknown"

from opencyver.validators.properties_validator import PropertiesValidator
from opencyver.validators.schema_validator import SchemaValidator
from opencyver.validators.syntax_validator import SyntaxValidator

__all__ = [
    "PropertiesValidator",
    "SchemaValidator",
    "SyntaxValidator",
    "__version__",
]
