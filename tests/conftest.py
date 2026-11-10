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
