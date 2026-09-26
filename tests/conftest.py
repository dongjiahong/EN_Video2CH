import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from zh_dub import logutil


@pytest.fixture(autouse=True)
def _reset_logutil():
    logutil.reset()
    yield
    logutil.reset()
