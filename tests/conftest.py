"""Pytest config for the standalone factory: put the factory's parent dir on
sys.path so `import factory.<sub>` resolves (mirrors how `python -m factory.*` runs)."""
import os
import sys

_here = os.path.dirname(__file__)
_factory_parent = os.path.abspath(os.path.join(_here, "..", ".."))
if _factory_parent not in sys.path:
    sys.path.insert(0, _factory_parent)
