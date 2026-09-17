from __future__ import annotations

import sys
import types
from pathlib import Path

PACKAGE = "confluence_skill_under_test"
module = types.ModuleType(PACKAGE)
module.__path__ = [str(Path(__file__).resolve().parents[1])]
sys.modules[PACKAGE] = module
