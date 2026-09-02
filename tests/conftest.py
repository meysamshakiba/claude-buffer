import sys
from pathlib import Path

import pytest

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
sys.path.insert(0, str(SCRIPTS))


@pytest.fixture
def qpath(tmp_path):
    """A queue file of its own, so tests never touch the real ~/.claude queue."""
    return tmp_path / "queue.md"
