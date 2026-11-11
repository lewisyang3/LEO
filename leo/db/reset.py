"""`make reset`: drop the schema, rerun the migrations, reseed.

A reseed alone keeps every approval and edit, because upserts never write `action.state`,
so a clean demo needs this. Dropping `public` (rather than `alembic downgrade base`) doesn't
depend on any downgrade working; the init migration recreates `pg_trgm`. Refuses a database
that isn't local unless `--force` is given.
"""

import argparse
import sys

from sqlalchemy import Engine, create_engine, pool, text
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError

from leo.config import get_settings

LOCAL_HOSTS = {"localhost", "127.0.0.1", "::1", "db"}


def is_local(url: str) -> bool:
    """True for the compose database: localhost, loopback, or the `db` service name."""
    return make_url(url).host in LOCAL_HOSTS


def drop_schema(engine: Engine) -> None:
    """Drop and recreate `public` in one transaction. Waits at most 5s for locks."""
    with engine.begin() as conn:
        conn.execute(text("SET LOCAL lock_timeout = '5s'"))
        conn.execute(text("DROP SCHEMA public CASCADE"))
        conn.execute(text("CREATE SCHEMA public"))


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="python -m leo.db.reset", description=__doc__)
    parser.add_argument("--force", action="store_true", help="reset a non-local database")
    args = parser.parse_args(argv)

    url = get_settings().database_url
    shown = make_url(url).render_as_string(hide_password=True)
    if not (is_local(url) or args.force):
        print(f"refusing to reset non-local database {shown} (pass --force)", file=sys.stderr)
        sys.exit(2)

    print(f"resetting {shown}", flush=True)  # before alembic's log lines on stderr
    engine = create_engine(url, poolclass=pool.NullPool)
    try:
        drop_schema(engine)
    except OperationalError as e:
        if "lock timeout" not in str(e):
            raise
        print("the database is busy: stop `make api` and `make worker` first", file=sys.stderr)
        sys.exit(1)
    finally:
        engine.dispose()

    from alembic import command
    from alembic.config import Config

    from leo.db import seed

    command.upgrade(Config("alembic.ini"), "head")
    seed.main()


if __name__ == "__main__":
    main()
