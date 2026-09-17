"""Keep skill-local imports valid when pytest starts from the Hub root."""

import pathlib
import sys

SKILL_ROOT = pathlib.Path(__file__).resolve().parents[1]
if str(SKILL_ROOT) not in sys.path:
    sys.path.insert(0, str(SKILL_ROOT))
