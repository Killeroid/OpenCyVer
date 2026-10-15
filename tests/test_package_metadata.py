"""The names of the project agree: the distribution, the import name, the version and the metadata codes."""

import importlib.metadata
import importlib.util
import pathlib

import pytest

import opencyver
from opencyver import backend

tomllib = pytest.importorskip("tomllib")  # Python >= 3.11
PYPROJECT = tomllib.loads(
    (pathlib.Path(__file__).parents[1] / "pyproject.toml").read_text()
)


def test_the_distribution_is_called_opencyver():
    assert PYPROJECT["project"]["name"] == "OpenCyVer"
    assert importlib.metadata.metadata("OpenCyVer")["Name"] == "OpenCyVer"


def test_the_version_comes_from_the_installed_distribution():
    # __init__ looks the version up by distribution name; a wrong name silently gives "unknown"
    assert opencyver.__version__ != "unknown"
    assert (
        opencyver.__version__
        == importlib.metadata.version("OpenCyVer")
        == PYPROJECT["project"]["version"]
    )


def test_the_import_name_is_the_module_name_in_pyproject():
    assert PYPROJECT["tool"]["uv"]["build-backend"]["module-name"] == "opencyver"
    assert pathlib.Path(opencyver.__file__).parent.name == "opencyver"


def test_the_codes_of_opencyver_share_one_namespace():
    ours = [
        backend.UNKNOWN_LABEL,
        backend.UNKNOWN_REL_TYPE,
        backend.UNKNOWN_PROPERTY,
        backend.UNKNOWN_INDEX,
        backend.PARAMETER_NOT_PROVIDED,
        backend.ARITHMETIC_ERROR,
    ]
    assert all(code.startswith("OpenCyVer.") for code in ours)
    # the errors of the database keep the database's name
    assert backend.SYNTAX_ERROR.startswith("FalkorDB.")
    assert backend.EXECUTION_FAILED.startswith("FalkorDB.")


def test_the_old_name_is_gone():
    old = "Cy" + "Ver"
    assert importlib.util.find_spec(old) is None
    assert importlib.util.find_spec(old.lower()) is None
