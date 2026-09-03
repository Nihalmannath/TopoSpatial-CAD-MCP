"""
pytest configuration for TopoSpatial-CAD MCP tests.

Adds src directory to sys.path to enable proper imports for testing.
This allows tests to use absolute imports like "from core import ..."
which work when the server is running.
"""

import os
import sys
from pathlib import Path

# Add src directory to path
src_path = Path(__file__).parent.parent / "src"
sys.path.insert(0, str(src_path))

if not hasattr(os, "add_dll_directory"):
    os.add_dll_directory = lambda path: None

# Mock Windows-only pydantic_core binary on non-Windows test environments
try:
    import pydantic_core._pydantic_core
except Exception:
    import unittest.mock
    core_mock = unittest.mock.MagicMock()
    core_mock.__version__ = "2.41.5"
    sys.modules["pydantic_core._pydantic_core"] = core_mock

import pytest

@pytest.fixture(autouse=True)
def reset_circuit_breaker():
    try:
        from adapters.com_worker import get_com_worker
        get_com_worker().circuit_breaker.reset()
    except Exception:
        pass
    yield
    try:
        from adapters.com_worker import get_com_worker
        get_com_worker().circuit_breaker.reset()
    except Exception:
        pass
