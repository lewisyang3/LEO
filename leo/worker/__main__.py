"""Run via: LEO_WORKER_HANDLERS=myapp.worker_wiring:make_handlers uv run python -m leo.worker"""
from __future__ import annotations

import importlib
import logging
import os

from leo.worker.core import SingleWorker
from leo.worker.postgres import PostgresJobRepository


def main() -> None:
    logging.basicConfig(level=os.getenv("LOG_LEVEL", "INFO"))
    target = os.environ.get("LEO_WORKER_HANDLERS")
    if not target or ":" not in target:
        raise SystemExit("Set LEO_WORKER_HANDLERS=module.path:function_name (a function returning the 6 handlers)")
    module_name, fn_name = target.split(":", 1)
    factory = getattr(importlib.import_module(module_name), fn_name)
    SingleWorker(PostgresJobRepository(), factory()).serve_forever()


if __name__ == "__main__":
    main()
