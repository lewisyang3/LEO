"""A `VendorResolver` backed by the store (ROADMAP A5).

Lookup is an exact match on the normalized name or an alias, then a fuzzy match in two steps:

1. `pg_trgm` finds candidates. Each vendor name and alias is scored as
   `greatest(similarity, word_similarity)` on its `match_form`, and every candidate at or above
   `CANDIDATE_THRESHOLD` is kept, best first (ties to the lowest vendor id).
2. The first candidate whose words agree (`leo.match.names.words_agree`) wins.
   `word_similarity` alone rewards one shared word, so "metro cafe" would match
   "metro water and power" at 0.55. The word check rejects that, and still accepts
   "halcyon payrl svcs" for "halcyon payroll services".

Every name or descriptor that resolves is recorded as an alias, so later lookups are exact.
"""

from uuid import UUID

from sqlalchemy import select, text, union_all

from leo.db import keys
from leo.db.models import Vendor, VendorAlias
from leo.db.repo import Repo
from leo.match.names import (
    LEGAL_SUFFIX_PATTERN,
    clean_descriptor,
    descriptor_display_name,
    display_name,
    match_form,
    words_agree,
)

CANDIDATE_THRESHOLD = 0.3

_FUZZY = text(
    """
    WITH cand AS (
        SELECT id AS vendor_id, normalized_name AS n FROM vendor WHERE tenant_id = :tenant
        UNION ALL
        SELECT vendor_id, normalized_alias FROM vendor_alias WHERE tenant_id = :tenant
    ), scored AS (
        SELECT vendor_id, regexp_replace(n, :suffix, '') AS m FROM cand
    )
    SELECT vendor_id, m, greatest(similarity(:q, m), word_similarity(:q, m)) AS score
    FROM scored
    WHERE greatest(similarity(:q, m), word_similarity(:q, m)) >= :threshold
    ORDER BY score DESC, vendor_id
    """
)


class DbVendorResolver:
    def __init__(self, repo: Repo) -> None:
        self.repo = repo

    def _exact(self, name: str) -> UUID | None:
        n = keys.normalize_name(name)
        t = self.repo.tenant_id
        hits = union_all(
            select(Vendor.id.label("vendor_id")).where(
                Vendor.tenant_id == t, Vendor.normalized_name == n
            ),
            select(VendorAlias.vendor_id).where(
                VendorAlias.tenant_id == t, VendorAlias.normalized_alias == n
            ),
        ).subquery()
        return self.repo.session.scalar(select(hits.c.vendor_id).order_by(hits.c.vendor_id))

    def _fuzzy(self, name: str) -> UUID | None:
        q = match_form(name)
        if not q:
            return None
        params = {
            "tenant": self.repo.tenant_id,
            "suffix": LEGAL_SUFFIX_PATTERN,
            "q": q,
            "threshold": CANDIDATE_THRESHOLD,
        }
        for row in self.repo.session.execute(_FUZZY, params):
            if words_agree(q, row.m):
                return row.vendor_id
        return None

    def _find(self, name: str) -> UUID | None:
        return self._exact(name) or self._fuzzy(name)

    def find_vendor(self, name: str) -> str | None:
        vid = self._find(name)
        return None if vid is None else str(vid)

    def vendor_for_name(self, name: str) -> str:
        vid = self._find(name) or self.repo.upsert_vendor(display_name(name))
        self.repo.add_alias(vid, name)
        return str(vid)

    def vendor_for_descriptor(self, descriptor: str) -> str:
        cleaned = clean_descriptor(descriptor)
        vid = (
            self._exact(descriptor)
            or (self._find(cleaned) if cleaned else None)
            or self.repo.upsert_vendor(descriptor_display_name(descriptor) or descriptor)
        )
        self.repo.add_alias(vid, descriptor)
        return str(vid)
