"""Gmail sync, against an in-memory `FakeGmail`: no network, no Google account.

Needs Postgres running and migrated (`make up && make migrate`).
"""

import pytest
from cryptography.fernet import Fernet
from sqlalchemy import func, select

from leo.db import keys
from leo.db.models import ConnectedAccount, Job, OAuthCredential, SourceDocument, new_uuid7
from leo.db.repo import Repo
from leo.fixtures import DATA_DIR
from leo.ingest import gmail, secrets
from leo.ingest.ingest import ingest

INBOX = sorted((DATA_DIR / "inbox").glob("*.eml"))
TOKEN = b'{"refresh_token": "1//very-secret-refresh-token"}'


class FakeGmail:
    """Messages `m1`, `m2`, ... from the synthetic inbox, and a history log."""

    def __init__(self, files=INBOX) -> None:
        self.messages = {f"m{i}": f.read_bytes() for i, f in enumerate(files, 1)}
        self.history_id = 100
        self.added: list[tuple[int, str]] = []  # (history id, message id)
        self.expired = False
        self.gone: set[str] = set()
        self.gets: list[str] = []

    def add(self, message_id: str, raw: bytes) -> None:
        self.history_id += 1
        self.messages[message_id] = raw
        self.added.append((self.history_id, message_id))

    def profile(self):
        return "ap@brightwater.example", str(self.history_id)

    def list_message_ids(self):
        yield from (m for m in self.messages if m not in self.gone)

    def history(self, start_id):
        if self.expired:
            raise gmail.HistoryExpired(start_id)
        ids = [m for h, m in self.added if h > int(start_id)]
        return ids, str(self.history_id)

    def get_raw(self, message_id):
        self.gets.append(message_id)
        if message_id in self.gone:
            raise gmail.MessageGone(message_id)
        return self.messages[message_id], f"thread-{message_id}"


@pytest.fixture(autouse=True)
def secret_key(monkeypatch):
    from leo.config import get_settings

    monkeypatch.setenv("LEO_SECRET_KEY", Fernet.generate_key().decode())
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture
def account(session):
    return gmail.store_account(
        session, f"gmail-test-{new_uuid7()}", "ap@brightwater.example", TOKEN
    )


@pytest.fixture
def repo(session, account):
    return Repo(session, account.tenant_id)


def _sync(repo, raw_store, account, client):
    result = gmail.sync(repo, raw_store, account, client)
    repo.session.flush()
    repo.session.refresh(account)
    return result


def _count(repo, model) -> int:
    return repo.session.scalar(
        select(func.count()).select_from(model).where(model.tenant_id == repo.tenant_id)
    )


def test_full_sync_stores_each_message_keyed_on_the_account(repo, raw_store, account):
    client = FakeGmail()
    r = _sync(repo, raw_store, account, client)
    assert (r.new, r.seen, r.gone, r.full) == (16, 0, 0, True)
    docs = repo.session.scalars(
        select(SourceDocument).where(SourceDocument.tenant_id == repo.tenant_id)
    ).all()
    assert {d.natural_key for d in docs} == {
        keys.gmail(str(account.id), m) for m in client.messages
    }
    assert {d.source_kind for d in docs} == {gmail.SOURCE_KIND}
    m1 = next(d for d in docs if d.natural_key.endswith(":m1"))
    assert (m1.path, m1.thread_id) == ("gmail/m1.eml", "thread-m1")
    assert m1.subject == "Important update to your Lumenpost plan pricing"
    assert m1.message_id  # parsed from the headers, an attribute only
    assert _count(repo, Job) == 16  # one classify job each
    assert account.history_id == "100"
    assert account.last_synced_at is not None


def test_second_sync_adds_nothing_and_fetches_nothing(repo, raw_store, account):
    client = FakeGmail()
    _sync(repo, raw_store, account, client)
    client.gets.clear()
    r = _sync(repo, raw_store, account, client)
    assert (r.new, r.full) == (0, False)
    assert client.gets == []
    assert _count(repo, SourceDocument) == 16
    assert account.history_id == "100"


def test_incremental_sync_stores_only_the_new_message(repo, raw_store, account):
    client = FakeGmail(INBOX[:3])
    _sync(repo, raw_store, account, client)
    client.gets.clear()
    client.add("m99", INBOX[5].read_bytes())
    r = _sync(repo, raw_store, account, client)
    assert (r.new, r.seen, r.full) == (1, 0, False)
    assert client.gets == ["m99"]
    assert account.history_id == "101"
    assert _count(repo, SourceDocument) == 4


def test_expired_history_falls_back_to_a_full_sync(repo, raw_store, account):
    client = FakeGmail(INBOX[:3])
    _sync(repo, raw_store, account, client)
    client.add("m99", INBOX[5].read_bytes())
    client.expired = True
    r = _sync(repo, raw_store, account, client)
    assert (r.new, r.seen, r.full) == (1, 3, True)
    assert _count(repo, SourceDocument) == 4


def test_a_deleted_message_is_skipped_and_the_cursor_moves(repo, raw_store, account):
    client = FakeGmail(INBOX[:2])
    _sync(repo, raw_store, account, client)
    client.add("m98", INBOX[4].read_bytes())
    client.add("m99", INBOX[5].read_bytes())
    client.gone.add("m98")
    r = _sync(repo, raw_store, account, client)
    assert (r.new, r.gone) == (1, 1)
    assert account.history_id == "102"


def test_reconnect_keeps_the_account_id(session, account):
    again = gmail.store_account(
        session, _slug(session, account), account.email_address, b'{"refresh_token": "new"}'
    )
    assert again.id == account.id
    creds = session.scalars(
        select(OAuthCredential).where(OAuthCredential.account_id == account.id)
    ).all()
    assert len(creds) == 1
    assert secrets.decrypt(creds[0].ciphertext) == b'{"refresh_token": "new"}'


def _slug(session, account):
    from leo.db.models import Tenant

    return session.get_one(Tenant, account.tenant_id).slug


def test_token_is_encrypted_at_rest(session, account):
    cred = session.scalar(select(OAuthCredential).where(OAuthCredential.account_id == account.id))
    assert b"very-secret-refresh-token" not in cred.ciphertext
    assert secrets.decrypt(cred.ciphertext) == TOKEN
    assert cred.scopes == gmail.READONLY


def test_missing_secret_key_says_how_to_make_one(monkeypatch):
    from leo.config import get_settings

    monkeypatch.setenv("LEO_SECRET_KEY", "")
    get_settings.cache_clear()
    with pytest.raises(secrets.SecretKeyMissing, match="Fernet.generate_key"):
        secrets.encrypt(TOKEN)


def test_a_synced_email_and_its_upload_are_separate_rows(repo, raw_store, account):
    _sync(repo, raw_store, account, FakeGmail(INBOX[:1]))
    upload = ingest(repo, raw_store, INBOX[0].read_bytes(), INBOX[0].name)
    assert upload.created
    assert upload.natural_key == keys.eml_hash(INBOX[0].read_bytes())
    assert _count(repo, SourceDocument) == 2


def test_sync_all_keeps_going_past_a_failing_account(session, raw_store, account):
    other = gmail.store_account(session, _slug(session, account), "ops@brightwater.example", TOKEN)

    def client_for(repo, acct):
        if acct.id == other.id:
            raise RuntimeError("token revoked")
        return FakeGmail(INBOX[:2])

    results = dict(gmail.sync_all(session, raw_store, client_for, tenant_id=account.tenant_id))
    assert results["ops@brightwater.example"] is None
    assert results["ap@brightwater.example"].new == 2
    assert session.get_one(ConnectedAccount, account.id).history_id == "100"
