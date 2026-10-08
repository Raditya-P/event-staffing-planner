"""Test setup: a throwaway SQLite database and small engine settings so the suite runs in seconds.
Environment variables must be set before forecast_mcp is imported."""

import os
import tempfile
from pathlib import Path

_tmp = Path(tempfile.mkdtemp(prefix="forecast-mcp-tests-"))
os.environ["DATABASE_URL"] = f"sqlite:///{(_tmp / 'test.db').as_posix()}"
os.environ.update(
    FORECAST_MEMBERS="4",
    FORECAST_BOOST_ITERS="40",
    OPTIMIZER_POP_SIZE="30",
    OPTIMIZER_GENERATIONS="25",
    OPTIMIZER_SAMPLES="15",
    DEV_ROUTES="1",
)

import pytest  # noqa: E402

from forecast_mcp import db, jobs  # noqa: E402
from forecast_mcp.seed import demo_event_ids, ensure_demo_data  # noqa: E402

jobs.RUN_INLINE = True


@pytest.fixture(scope="session", autouse=True)
def demo_data():
    db.init_db()
    ensure_demo_data()
    return demo_event_ids()


@pytest.fixture
def event_id(demo_data):
    return demo_data[0]
