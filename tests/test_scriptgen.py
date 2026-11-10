"""Script generation (ROADMAP Phase 4): the sandbox, candidates, promotion and the worker.

The DB tests need Postgres running and migrated. Each works in its own tenant inside the
session's rolled-back transaction. They run the synthetic set offline from the committed
caches (extract and scriptgen), with the hand-written scripts left out.
"""

import datetime as dt
import json
import types
from decimal import Decimal
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import select

from leo.clock import FixedClock
from leo.config import Settings
from leo.db.models import ExtractorScript, Job
from leo.db.repo import Repo
from leo.e2e.checks import run_checks
from leo.fixtures import load_fixture
from leo.ingest import folder
from leo.interfaces import DocumentText, Extraction
from leo.llm import ResponseCache
from leo.pipeline.extract_script import ParseError, sender_address
from leo.scriptgen import generate
from leo.scriptgen.candidates import find_groups, load_documents, negatives
from leo.scriptgen.promote import check_candidate, compare, generate_and_promote, script_name
from leo.scriptgen.sandbox import (
    PROVIDED,
    SAFE_BUILTINS,
    TIMED_OUT,
    SandboxError,
    check_code,
    load_functions,
    run_isolated,
)
from leo.scriptgen.scripts import StoredScriptExtractor, StoredScripts, to_extraction
from leo.types import SourceDocument
from leo.worker import queue
from leo.worker.pipeline import Pipeline
from leo.worker.run import Worker

TODAY = dt.date(2026, 11, 2)
CLOCK = FixedClock(TODAY)
ROOT = Path("data/synthetic")
KESTREL = "billing@kestrelcloud.example"

OK = """
def matches(text):
    return re_search(r"^Invoice: X-\\d+$", text, MULTILINE) is not None

def parse(text):
    m = re_search(r"^Invoice: (X-\\d+)$", text, MULTILINE)
    if m is None:
        raise ValueError("no invoice line")
    return {"vendor_name": "Acme Corp.", "invoice_facts": [{
        "invoice_number": m.group(1), "invoice_date": "2026-09-01",
        "billing_period": "2026-09-01/2026-09-30", "amount": "10.00", "quotes": [m.group(0)],
        "seats_billed": None,
    }]}
"""
TEXT = "From: Acme <billing@acme.example>\n\nAcme Corp.\nInvoice: X-1\n"


def _doc(text: str = TEXT, path: str = "inbox/x.eml") -> DocumentText:
    return DocumentText(document=SourceDocument(path=path, doc_class=None, vendor=None), text=text)


# --- Sandbox ---------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "code, why",
    [
        ("import os\n" + OK, "imports"),
        ("from re import search\n" + OK, "imports"),
        (OK + "\nX = ().__class__.__bases__\n", "'__class__'"),
        (OK + "\ndef f():\n    return open('/etc/passwd')\n", "'open'"),
        (OK + "\ndef f(x):\n    return getattr(x, 'y')\n", "'getattr'"),
        (OK + "\ndef f():\n    return '{0.x}'.format(1)\n", "'format'"),
        (OK + "\ndef f():\n    g = (i for i in [])\n    return g.gi_frame\n", "'gi_frame'"),
        (OK + "\ndef f():\n    try:\n        pass\n    except:\n        pass\n", "catch"),
        (OK + "\nprint('hi')\n", "top level"),
        ("def matches(text):\n    return True\n", "parse"),
    ],
)
def test_check_code_rejects(code, why):
    errors = check_code(code)
    assert errors and any(why in e for e in errors), errors


def test_check_code_accepts_a_plain_module():
    assert check_code(OK) == []


def _reachable(depth: int):
    """Every object reachable from the namespace through attributes the check allows."""
    seen: set[int] = set()
    frontier = [*PROVIDED.values(), *SAFE_BUILTINS.values()]
    out = []
    for _ in range(depth):
        nxt = []
        for obj in frontier:
            if id(obj) in seen:
                continue
            seen.add(id(obj))
            out.append(obj)
            for name in dir(obj):
                if name.startswith(("_", "gi_", "cr_", "ag_", "f_", "tb_", "co_")):
                    continue
                try:
                    nxt.append(getattr(obj, name))
                except Exception:
                    continue
        frontier = nxt
    return out


def test_no_module_or_dangerous_builtin_is_reachable_from_the_namespace():
    import builtins

    dangerous = {id(getattr(builtins, n)) for n in ("exec", "eval", "open", "compile")}
    dangerous.add(id(builtins.__import__))
    for obj in _reachable(3):
        assert not isinstance(obj, types.ModuleType), obj
        assert id(obj) not in dangerous, obj


def test_the_namespace_has_no_whole_module():
    assert not any(isinstance(v, types.ModuleType) for v in PROVIDED.values())


def test_load_functions_runs_in_the_restricted_namespace():
    matches, parse = load_functions(OK)
    assert matches(TEXT)
    assert parse(TEXT)["invoice_facts"][0]["invoice_number"] == "X-1"
    with pytest.raises(SandboxError):
        load_functions(
            "def matches(text):\n    return len(text)\ndef parse(text):\n    pass\n"
            "X = undefined_name\n"
        )


def test_strptime_works_but_other_imports_do_not():
    code = OK + "\ndef day(s):\n    return datetime.strptime(s, '%B %d, %Y').date()\n"
    matches, _ = load_functions(code)
    ns = matches.__globals__
    assert ns["day"]("September 1, 2026") == dt.date(2026, 9, 1)
    with pytest.raises(ImportError):
        ns["__builtins__"]["__import__"]("os")


def test_the_stored_extractor_fails_closed():
    raising_matches = "def matches(text):\n    return [][1]\ndef parse(text):\n    return {}\n"
    raising_parse = "def matches(text):\n    return True\ndef parse(text):\n    return {}['x']\n"
    x = StoredScriptExtractor([("gen-a", "1", raising_matches)], CLOCK)
    assert x.extract(_doc()) is None and x.records == {}
    x = StoredScriptExtractor([("gen-b", "1", raising_parse)], CLOCK)
    assert x.extract(_doc()) is None
    assert x.records["inbox/x.eml"]["script"] == "script:gen-b@1"
    assert "KeyError" in x.records["inbox/x.eml"]["error"]


def test_the_stored_extractor_runs_a_script_in_a_child_and_sets_the_engine():
    x = StoredScriptExtractor([("gen-ok", "3", OK)], CLOCK)
    ex = x.extract(_doc(TEXT.replace("\n", "\r\n")))
    assert ex is not None and ex.engine == "script:gen-ok@3"
    assert ex.raw_output["script"] == "gen-ok" and ex.invoice_facts[0].invoice_number == "X-1"


def test_a_runaway_stored_script_times_out_and_the_next_one_runs():
    runaway = (
        "def matches(text):\n    return re_search(r'^(a+)+$', 'a' * 40 + 'b') is not None\n"
        "def parse(text):\n    return {}\n"
    )
    x = StoredScriptExtractor([("gen-slow", "1", runaway), ("gen-ok", "1", OK)], CLOCK, timeout=2)
    ex = x.extract(_doc())
    assert ex is not None and ex.engine == "script:gen-ok@1"
    assert x.records["inbox/x.eml"] == {"script": "script:gen-slow@1", "error": TIMED_OUT}


def test_run_isolated_times_out_on_a_runaway_regex():
    code = (
        "def matches(text):\n    return re_search(r'^(a+)+$', text) is not None\n"
        "def parse(text):\n    return {}\n"
    )
    out = run_isolated(code, ["a" * 40 + "b"], timeout=2)
    assert out == {"error": TIMED_OUT, "results": []}


def test_run_isolated_reports_each_text():
    out = run_isolated(OK, [TEXT, "nothing"], timeout=20)
    assert out["error"] is None
    assert [r["matches"] for r in out["results"]] == [True, False]
    assert out["results"][0]["output"]["vendor_name"] == "Acme Corp."


# --- Output conversion and comparison --------------------------------------------------------


def test_to_extraction_normalizes_like_the_llm_and_rejects_bad_quotes():
    _, parse = load_functions(OK)
    ex = to_extraction(_doc(), parse(TEXT), CLOCK)
    [f] = ex.invoice_facts
    assert f.billing_period == "2026-09" and f.amount == Decimal("10.00")
    assert f.document == "inbox/x.eml"
    out = parse(TEXT)
    out["invoice_facts"][0]["quotes"] = ["Acme Corp.\nInvoice: X-1"]
    with pytest.raises(ParseError, match="one line"):
        to_extraction(_doc(), out, CLOCK)
    out["invoice_facts"][0]["quotes"] = ["not in the text"]
    with pytest.raises(ParseError):
        to_extraction(_doc(), out, CLOCK)
    with pytest.raises(ParseError, match="vendor_name"):
        to_extraction(_doc(), {"invoice_facts": []}, CLOCK)


def test_compare_ignores_quotes_and_decimal_places_but_not_values():
    _, parse = load_functions(OK)
    a = to_extraction(_doc(), parse(TEXT), CLOCK)
    out = parse(TEXT)
    out["invoice_facts"][0]["amount"] = 10.0
    out["invoice_facts"][0]["quotes"] = ["Invoice: X-1", "Acme Corp."]
    assert compare(a, to_extraction(_doc(), out, CLOCK)) == []
    out["invoice_facts"][0]["amount"] = "11.00"
    [diff] = compare(a, to_extraction(_doc(), out, CLOCK))
    assert diff.startswith("invoice_facts:")


def test_sender_address():
    assert sender_address("From: Kestrel <Billing@KestrelCloud.example>\r\n\r\nbody") == (
        "billing@kestrelcloud.example"
    )
    assert sender_address("# A contract\n\nNo headers.") == ""


# --- The synthetic set, scripts off ----------------------------------------------------------


def _have_caches() -> bool:
    root = Settings(_env_file=None).leo_llm_cache_dir
    return any((root / "extract").glob("*.json")) and any((root / "scriptgen").glob("*.json"))


needs_cache = pytest.mark.skipif(not _have_caches(), reason="no committed extract/scriptgen cache")


def _offline(monkeypatch):
    from leo.config import get_settings

    monkeypatch.setenv("LEO_LLM_OFFLINE", "1")
    get_settings.cache_clear()
    return get_settings()


def _run(session, raw_store, scripts: list[str] = ()) -> Repo:
    """The synthetic set in a fresh tenant, hand-written scripts off, drained offline. Each
    code in `scripts` is stored as a promoted generated script before the run."""
    slug = f"scriptgen-test-{uuid4().hex[:8]}"
    tenant_id, _ = folder.load(session, ROOT, slug, raw_store)
    repo = Repo(session, tenant_id)
    for i, code in enumerate(scripts):
        repo.add_extractor_script(f"gen-test-{i}", code, None, 1.0, dt.datetime.now(dt.UTC))
    session.commit()
    pipeline = Pipeline.default(
        CLOCK,
        classifier="fixture",
        drafter="template",
        sender="Brightwater",
        builtin_scripts=False,
    )
    Worker(session, pipeline, raw_store, CLOCK, tenant_id=tenant_id).drain()
    return repo


def _engines(repo: Repo) -> dict[str, str]:
    from leo.db.models import SourceDocument as Row

    q = (
        select(Row.path, Job.processed_by)
        .join(Job, Job.document_id == Row.id)
        .where(Job.tenant_id == repo.tenant_id, Job.stage == queue.EXTRACT)
    )
    return dict(repo.session.execute(q).all())


@pytest.fixture
def llm_run(session, raw_store, monkeypatch):
    settings = _offline(monkeypatch)
    repo = _run(session, raw_store)
    return repo, load_documents(session, repo.tenant_id, raw_store), settings


@needs_cache
def test_kestrel_is_the_only_candidate_with_two_distinct_examples(llm_run):
    _, docs, _ = llm_run
    [group] = find_groups(docs)
    assert group.sender == KESTREL
    assert sorted(e.path for e in group.examples) == [
        "inbox/05_kestrel_invoice_2026_09.eml",
        "inbox/06_kestrel_invoice_2026_10.eml",  # 07 is the same row
    ]
    assert [e.sha256 for e in group.examples] == sorted(e.sha256 for e in group.examples)
    assert script_name(group) == "gen-kestrel-cloud-hosting"
    # The contracts have no sender and form no group, even at a threshold of 1.
    assert "" not in {g.sender for g in find_groups(docs, threshold=1)}
    others = negatives(docs, group)
    assert others and all(d.sender != KESTREL for d in others)


@needs_cache
def test_a_needs_review_or_duplicate_document_is_not_an_example(llm_run):
    _, docs, _ = llm_run
    [d05] = [d for d in docs if d.path.startswith("inbox/05")]
    d05.verified = None  # what a needs_review item gives
    assert find_groups(docs) == []
    d05.verified, d05.duplicate = docs[0].verified, True
    assert find_groups(docs) == []


@needs_cache
def test_an_llm_answer_with_item_errors_is_not_an_example(llm_run, raw_store):
    repo, _, _ = llm_run
    from leo.db.models import SourceDocument as Row

    job = repo.session.scalars(
        select(Job)
        .join(Row, Job.document_id == Row.id)
        .where(
            Job.tenant_id == repo.tenant_id,
            Job.stage == queue.EXTRACT,
            Row.path == "inbox/05_kestrel_invoice_2026_09.eml",
        )
    ).one()
    out = dict(job.raw_model_output)
    out["raw_output"] = {**out["raw_output"], "item_errors": ["invoice_facts[1]: bad"]}
    job.raw_model_output = out
    repo.session.flush()
    docs = load_documents(repo.session, repo.tenant_id, raw_store)
    [d05] = [d for d in docs if d.path.startswith("inbox/05")]
    assert d05.llm_output is None and not d05.is_example
    assert find_groups(docs) == []


@needs_cache
def test_the_cached_proposal_is_promoted_and_skips_the_group_after(llm_run):
    repo, docs, settings = llm_run
    [group] = find_groups(docs)
    cache = ResponseCache(settings.leo_llm_cache_dir)
    out = generate_and_promote(repo, group, negatives(docs, group), CLOCK, cache, settings)
    assert out.promoted == "1" and len(out.attempts) == 1
    [row] = repo.promoted_scripts()
    assert (row.name, row.version, row.matcher, row.accuracy) == (
        "gen-kestrel-cloud-hosting",
        "1",
        KESTREL,
        Decimal("1.0000"),
    )
    # A sender a promoted script covers is no longer a candidate; a hand-written one doesn't
    # count (the run had them off).
    covered = {r.matcher for r in repo.promoted_scripts()}
    assert find_groups(docs, covered=covered) == []


@needs_cache
def test_a_wrong_or_greedy_candidate_is_rejected_and_stored(llm_run):
    repo, docs, settings = llm_run
    [group] = find_groups(docs)
    others = negatives(docs, group)
    code = json.loads(next((settings.leo_llm_cache_dir / "scriptgen").glob("*.json")).read_text())[
        "output"
    ]["code"]
    assert check_candidate(code, group, others, CLOCK).promotable

    wrong = code.replace('float(tm.group(1).replace(",", ""))', "1.0")
    assert wrong != code
    check = check_candidate(wrong, group, others, CLOCK)
    assert check.accuracy == 0 and not check.promotable
    assert all("invoice_facts: expected" in f for f in check.failures)

    greedy = code.replace(
        "def matches(text):", "def matches(text):\n    return True\n\ndef unused(text):"
    )
    check = check_candidate(greedy, group, others, CLOCK)
    assert check.accuracy == 1.0 and check.false_matches and not check.promotable

    # A rejected attempt is stored, unpromoted, with its accuracy.
    def fake_run(code, texts, timeout):
        return {
            "error": None,
            "results": [{"matches": False, "output": None, "error": None}] * len(texts),
        }

    cache = ResponseCache(settings.leo_llm_cache_dir)
    out = generate_and_promote(
        repo, group, others, CLOCK, cache, settings, max_attempts=1, run=fake_run
    )
    assert out.promoted is None
    [row] = repo.session.scalars(
        select(ExtractorScript).where(ExtractorScript.tenant_id == repo.tenant_id)
    ).all()
    assert row.promoted_at is None and row.accuracy == 0
    assert repo.promoted_scripts() == []


@needs_cache
def test_the_worker_extracts_with_a_promoted_script_and_the_checks_still_pass(
    session, raw_store, monkeypatch
):
    settings = _offline(monkeypatch)
    code = json.loads(next((settings.leo_llm_cache_dir / "scriptgen").glob("*.json")).read_text())[
        "output"
    ]["code"]
    repo = _run(session, raw_store, scripts=[code])
    engines = _engines(repo)
    assert engines["inbox/05_kestrel_invoice_2026_09.eml"] == "script:gen-test-0@1"
    assert engines["inbox/06_kestrel_invoice_2026_10.eml"] == "script:gen-test-0@1"
    assert sum(e.startswith("llm:") for e in engines.values()) == len(engines) - 2
    failed = [c.id for c in run_checks(session, repo.tenant_id, load_fixture()) if not c.ok]
    assert failed == []


def _tenant(session) -> Repo:
    from leo.db.repo import upsert_tenant

    return Repo(session, upsert_tenant(session, f"scriptgen-{uuid4().hex[:8]}", "Test"))


def test_a_stored_script_that_fails_the_check_is_skipped(session):
    repo = _tenant(session)
    now = dt.datetime.now(dt.UTC)
    assert StoredScripts(CLOCK)(repo) is None
    repo.add_extractor_script("gen-bad", "import os", None, 1.0, now)
    repo.add_extractor_script("gen-ok", OK, None, 1.0, now)
    repo.add_extractor_script("gen-ok", OK + "\n", None, 0.5, None)  # v2, not promoted
    x = StoredScripts(CLOCK)(repo)
    assert [(n, v) for n, v, _ in x.scripts] == [("gen-ok", "1")]


def test_two_senders_of_one_vendor_each_keep_their_script(session):
    repo = _tenant(session)
    now = dt.datetime.now(dt.UTC)
    repo.add_extractor_script("gen-acme", OK, "billing@acme.example", 1.0, now)  # v1
    repo.add_extractor_script("gen-acme", OK, "noreply@acme.example", 1.0, now)  # v2
    assert [(r.version, r.matcher) for r in repo.promoted_scripts()] == [
        ("1", "billing@acme.example"),
        ("2", "noreply@acme.example"),
    ]
    # A new version for one sender replaces only that sender's script.
    repo.add_extractor_script("gen-acme", OK, "billing@acme.example", 1.0, now)  # v3
    assert [(r.version, r.matcher) for r in repo.promoted_scripts()] == [
        ("3", "billing@acme.example"),
        ("2", "noreply@acme.example"),
    ]


def test_the_prompt_is_the_same_for_the_same_group_and_carries_failures():
    from leo.scriptgen.candidates import Group, StoredDocument

    ex = StoredDocument(
        id=uuid4(),
        path="a",
        sha256="aa",
        sender="x@y",
        doc=_doc(),
        duplicate=False,
        extract_engine="llm:m",
        verified=Extraction(vendor_name="Acme Corp.", engine="llm:m"),
        llm_output={"vendor_name": "Acme Corp.", "amount": 1.5},
    )
    g = Group("x@y", [ex])
    assert generate.user_prompt(g, []) == generate.user_prompt(g, [])
    p = generate.user_prompt(g, [generate.Attempt("def f(): pass", ["a: parse() raised X"])])
    assert "a: parse() raised X" in p and "rejected" in p
