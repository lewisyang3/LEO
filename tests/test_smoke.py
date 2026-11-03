from fastapi.testclient import TestClient

from leo.api.app import app
from leo.config import Settings


def test_settings_defaults():
    settings = Settings(_env_file=None)
    assert settings.database_url.startswith("postgresql+psycopg://")
    assert settings.leo_model_extract == "claude-haiku-5-5"


def test_settings_ignore_unknown_env_keys(tmp_path):
    env = tmp_path / ".env"
    env.write_text("SOME_FUTURE_KEY=1\nLEO_TODAY=2026-11-02\n")
    settings = Settings(_env_file=env)
    assert str(settings.leo_today) == "2026-11-02"


def test_health_reaches_db():
    """Needs Postgres running: `make up`."""
    response = TestClient(app).get("/api/health")
    assert response.status_code == 200
    assert response.json() == {"status": "ok", "db": "ok"}, "is Postgres up? run `make up`"
