from importlib.metadata import PackageNotFoundError, version

try:
    # The distribution name: it must match `name` in pyproject.toml
    __version__ = version("OpenCyVer")
except PackageNotFoundError:
    __version__ = "unknown"

from opencyver.validators.properties_validator import (
    AsyncPropertiesValidator,
    PropertiesValidator,
)
from opencyver.validators.schema_validator import AsyncSchemaValidator, SchemaValidator
from opencyver.validators.syntax_validator import AsyncSyntaxValidator, SyntaxValidator

__all__ = [
    "AsyncPropertiesValidator",
    "AsyncSchemaValidator",
    "AsyncSyntaxValidator",
    "PropertiesValidator",
    "SchemaValidator",
    "SyntaxValidator",
    "__version__",
]
