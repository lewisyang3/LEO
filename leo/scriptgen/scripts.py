"""Generated scripts as `Script`s the worker can run (ROADMAP Phase 4).

`to_extraction` turns a script's output (the LLM extractor's merged JSON shape) into an
`Extraction` through `LLMExtractor.to_extraction`, so dates, amounts, billing periods,
agreement refs and derived deadlines are normalized exactly as the LLM's answers are. It is
strict where the LLM path is lenient: an item that doesn't validate, a quote that isn't in the
text, or a missing vendor name is a `ParseError`, so the document falls through to the LLM.

`StoredScripts` builds the worker's extractor for a tenant's promoted scripts. Each script
runs in a spawned child with a time limit, never in the worker process.
"""

import logging
from collections.abc import Sequence
from pathlib import Path
from typing import Any
from uuid import UUID

from leo.db.repo import Repo
from leo.interfaces import Clock, DocumentText, Extraction
from leo.llm import ResponseCache
from leo.pipeline.extract_llm import LLMExtractor
from leo.pipeline.extract_script import MAX_RECORDS, ParseError
from leo.scriptgen.sandbox import check_code, run_isolated

log = logging.getLogger(__name__)
TIMEOUT = 5.0  # seconds per script per document, child start included


def _quotes(ex: Extraction) -> list[str]:
    out = [q for t in ex.terms for q in t.quotes]
    out += [q for f in ex.invoice_facts for q in f.quotes]
    out += [c.quote for c in ex.contacts]
    return out


def to_extraction(doc: DocumentText, output: Any, clock: Clock) -> Extraction:
    """A script's output as an `Extraction`. Raises `ParseError` when it isn't a clean one."""
    if not isinstance(output, dict):
        raise ParseError(f"parse returned {type(output).__name__}, not a dict")
    if not output.get("vendor_name"):
        raise ParseError("no vendor_name")
    raw: dict[str, Any] = {}
    # The cache is never used: `to_extraction` makes no call.
    converter = LLMExtractor(clock, ResponseCache(Path("/nonexistent")))
    try:
        ex = converter.to_extraction(doc, output, raw)
    except Exception as e:
        raise ParseError(f"output doesn't convert: {type(e).__name__}: {e}") from e
    if errors := raw.get("item_errors"):
        raise ParseError("invalid items: " + "; ".join(errors))
    for key in ("terms", "invoice_facts", "contacts"):
        given = output.get(key) or []
        if not isinstance(given, list):
            raise ParseError(f"{key} is not a list")
    for q in _quotes(ex):
        if "\n" in q or q not in doc.text:
            raise ParseError(f"quote is not one line of the text: {q!r}")
    return ex


class StoredScriptExtractor:
    """Stage 2 for a tenant's promoted generated scripts. Each script runs in a spawned child
    with a time limit (`sandbox.run_isolated`), never in the worker, because a regex that
    backtracks without end can't be interrupted in-process. A timeout or a child error is
    logged and the next script is tried; a matched script whose `parse` fails returns None, as
    `ScriptExtractor` does, so the document falls through to the LLM."""

    engine = "script"

    def __init__(
        self, scripts: Sequence[tuple[str, str, str]], clock: Clock, timeout: float = TIMEOUT
    ) -> None:
        self.scripts = list(scripts)  # (name, version, code)
        self.clock = clock
        self.timeout = timeout
        self.records: dict[str, dict[str, Any]] = {}  # path -> the latest failure

    def _record(self, path: str, engine: str, error: str) -> None:
        self.records.pop(path, None)
        self.records[path] = {"script": engine, "error": error}
        while len(self.records) > MAX_RECORDS:
            del self.records[next(iter(self.records))]

    def extract(self, doc: DocumentText) -> Extraction | None:
        if "\r\n" in doc.text:
            doc = doc.model_copy(update={"text": doc.text.replace("\r\n", "\n")})
        path = doc.document.path
        for name, version, code in self.scripts:
            engine = f"script:{name}@{version}"
            ran = run_isolated(code, [doc.text], timeout=self.timeout)
            if ran["error"]:
                log.warning("%s on %s: %s", engine, path, ran["error"])
                self._record(path, engine, ran["error"])
                continue
            [r] = ran["results"]
            if not r["matches"]:
                continue
            try:
                if r["error"]:
                    raise ParseError(r["error"])
                ex = to_extraction(doc, r["output"], self.clock)
            except ParseError as e:
                self._record(path, engine, f"ParseError: {e}")
                return None
            raw = {**(ex.raw_output or {}), "script": name, "version": version}
            return ex.model_copy(update={"engine": engine, "raw_output": raw})
        return None


class StoredScripts:
    """Builds a tenant's `StoredScriptExtractor` from its promoted scripts (the latest version
    per name and sender), or None when it has none. Code that fails `check_code` is logged and
    skipped without running; the check is cached by row."""

    def __init__(self, clock: Clock, timeout: float = TIMEOUT) -> None:
        self.clock = clock
        self.timeout = timeout
        self._ok: dict[UUID, bool] = {}

    def __call__(self, repo: Repo) -> StoredScriptExtractor | None:
        scripts: list[tuple[str, str, str]] = []
        for row in repo.promoted_scripts():
            if row.id not in self._ok:
                errors = check_code(row.code)
                if errors:
                    log.error("stored script %s@%s skipped: %s", row.name, row.version, errors)
                self._ok[row.id] = not errors
            if self._ok[row.id]:
                scripts.append((row.name, row.version, row.code))
        return StoredScriptExtractor(scripts, self.clock, self.timeout) if scripts else None
