"""The package reports one version, and it comes from bt_proxy/__init__.py.

pyproject.toml must not hard-code a version. It must read the version from
bt_proxy.__version__, so the two can never differ.
"""

from __future__ import annotations

import re
from importlib import metadata
from pathlib import Path

import pytest
import tomllib

import bt_proxy

PYPROJECT = Path(__file__).resolve().parent.parent / "pyproject.toml"


def _project_table() -> dict:
    return tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))


def test_pyproject_does_not_hard_code_a_version() -> None:
    project = _project_table()["project"]
    assert "version" not in project, (
        "pyproject.toml [project] sets a static version; remove it and use "
        "dynamic = [\"version\"] so bt_proxy/__init__.py is the only source"
    )
    assert "version" in project.get("dynamic", []), (
        "pyproject.toml must declare version as dynamic"
    )


def test_pyproject_reads_version_from_package_attribute() -> None:
    dynamic = (
        _project_table().get("tool", {}).get("setuptools", {}).get("dynamic", {})
    )
    assert dynamic.get("version") == {"attr": "bt_proxy.__version__"}, (
        "pyproject.toml must set [tool.setuptools.dynamic] version to "
        "{attr = \"bt_proxy.__version__\"}"
    )


def test_package_version_is_well_formed() -> None:
    assert re.fullmatch(r"\d+\.\d+\.\d+", bt_proxy.__version__), (
        f"bt_proxy.__version__ is {bt_proxy.__version__!r}, want MAJOR.MINOR.PATCH"
    )


def test_installed_metadata_matches_package_version() -> None:
    try:
        installed = metadata.version("bt-proxy")
    except metadata.PackageNotFoundError:
        pytest.skip("bt-proxy is not installed; nothing to compare")
    assert installed == bt_proxy.__version__, (
        f"installed metadata says {installed}, bt_proxy.__version__ says "
        f"{bt_proxy.__version__}; reinstall the package"
    )
