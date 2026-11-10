import pytest

from leo.db import reset


@pytest.mark.parametrize(
    ("url", "local"),
    [
        ("postgresql+psycopg://leo:leo@localhost:5432/leo", True),
        ("postgresql+psycopg://leo:leo@127.0.0.1/leo", True),
        ("postgresql+psycopg://leo:leo@db:5432/leo", True),
        ("postgresql+psycopg://u:p@prod.example.com/leo", False),
    ],
)
def test_is_local(url, local):
    assert reset.is_local(url) is local


def test_refuses_remote_database(monkeypatch, capsys):
    """Never touches the dev database: a test that dropped it would wipe other tests' rows."""
    from leo.config import get_settings

    monkeypatch.setenv("DATABASE_URL", "postgresql+psycopg://u:secret@prod.example.com/leo")
    get_settings.cache_clear()

    def boom(_engine):
        raise AssertionError("drop_schema called")

    monkeypatch.setattr(reset, "drop_schema", boom)
    with pytest.raises(SystemExit) as exc:
        reset.main([])
    assert exc.value.code == 2
    err = capsys.readouterr().err
    assert "refusing to reset non-local database" in err
    assert "secret" not in err
