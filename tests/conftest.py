import pytest
from sqlalchemy.orm import Session

from leo.db.session import get_engine


@pytest.fixture
def session():
    """A session inside one transaction that is rolled back. Needs `make up && make migrate`."""
    with get_engine().connect() as conn:
        trans = conn.begin()
        with Session(bind=conn, join_transaction_mode="create_savepoint") as s:
            yield s
        trans.rollback()


@pytest.fixture(autouse=True)
def _raw_dir(tmp_path, monkeypatch):
    """Point LEO_RAW_DIR at a temp folder, so no test (the seed included) writes to var/raw."""
    from leo.config import get_settings

    monkeypatch.setenv("LEO_RAW_DIR", str(tmp_path / "raw"))
    get_settings.cache_clear()
    yield tmp_path / "raw"
    get_settings.cache_clear()


@pytest.fixture
def raw_store(_raw_dir):
    """The test's raw-file store, the same folder the seed writes to."""
    from leo.ingest.store import RawStore

    return RawStore(_raw_dir)
