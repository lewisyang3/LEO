"""FastAPI app. Run with `make api`."""

from fastapi import FastAPI
from sqlalchemy import text
from sqlalchemy.exc import SQLAlchemyError

from leo.api.routes import router
from leo.db.session import get_engine

app = FastAPI(title="LEO")
app.include_router(router)


@app.get("/api/health")
def health() -> dict[str, str]:
    try:
        with get_engine().connect() as conn:
            conn.execute(text("SELECT 1"))
        db = "ok"
    except SQLAlchemyError:
        db = "error"
    return {"status": "ok", "db": db}
