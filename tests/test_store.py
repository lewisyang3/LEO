"""Raw-file store (A3.1). Pure: no database."""

import hashlib

import pytest

from leo.ingest.store import RawStore


def test_put_is_content_addressed_and_written_once(tmp_path):
    store = RawStore(tmp_path)
    raw = b"hello"
    sha = hashlib.sha256(raw).hexdigest()
    rel = store.put(raw, ".EML")
    assert rel == f"{sha[:2]}/{sha}.eml"
    assert store.read(rel) == raw
    mtime = store.path(rel).stat().st_mtime_ns
    assert store.put(raw, ".eml") == rel
    assert store.path(rel).stat().st_mtime_ns == mtime  # not rewritten
    assert [p.name for p in tmp_path.rglob("*") if p.is_file()] == [f"{sha}.eml"]  # no temp left


def test_suffix_is_part_of_the_path(tmp_path):
    store = RawStore(tmp_path)
    assert store.put(b"x", ".md") != store.put(b"x", ".pdf")


def test_path_rejects_escapes(tmp_path):
    with pytest.raises(ValueError):
        RawStore(tmp_path / "raw").path("../secret")


def test_stored_files_are_world_readable(tmp_path):
    store = RawStore(tmp_path)
    assert store.path(store.put(b"x", ".md")).stat().st_mode & 0o777 == 0o644
