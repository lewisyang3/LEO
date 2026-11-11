"""Gmail sync: read-only, incremental by `historyId`, tokens encrypted at rest.

- `connect` runs Google's installed-app consent with the read-only scope, then stores the
  account and its encrypted token under its own tenant (`LEO_GMAIL_TENANT`, default
  `gmail-demo`). The inbox holds the same emails the seed loads, under different keys, so
  syncing into the seeded tenant would double every document.
- `sync` lists the inbox the first time, or when the history cursor has expired, and reads
  `history.list` (`messageAdded`) after that. Each message goes through `ingest()` keyed
  `v1:gmail:{account id}:{gmail message id}`, which queues its `classify` job. Message-ID,
  threadId and historyId are attributes, never part of a key.
- One transaction per account per run: documents and the new cursor commit together, so a
  crash leaves the old cursor, and the rerun is safe because every write is an upsert.

The `.eml` path (upload, seed) stays the fallback: nothing else depends on this module.
`python -m leo.ingest.gmail connect` / `sync [--watch N]`; see the README.
"""

import argparse
import base64
import datetime as dt
import json
import logging
import time
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.orm import Session

from leo.db import keys
from leo.db.models import ConnectedAccount, OAuthCredential
from leo.db.repo import Repo, upsert_tenant
from leo.ingest import secrets
from leo.ingest.ingest import document_exists, ingest
from leo.ingest.store import RawStore

PROVIDER = "gmail"
SOURCE_KIND = "gmail"
READONLY = "https://www.googleapis.com/auth/gmail.readonly"
INBOX = "INBOX"

log = logging.getLogger(__name__)


class HistoryExpired(Exception):
    """`history.list` answered 404: the cursor is too old (Gmail keeps about a week)."""


class MessageGone(Exception):
    """`messages.get` answered 404: the message was deleted after it was reported."""


class GmailClient(Protocol):
    def profile(self) -> tuple[str, str]:
        """(email address, current history id)."""
        ...

    def list_message_ids(self) -> Iterator[str]:
        """Every inbox message id, all pages. Spam and trash are left out."""
        ...

    def history(self, start_id: str) -> tuple[list[str], str]:
        """(ids of messages added to the inbox since `start_id`, the new history id).
        Raises `HistoryExpired`."""
        ...

    def get_raw(self, message_id: str) -> tuple[bytes, str | None]:
        """(RFC 822 bytes, thread id). Raises `MessageGone`."""
        ...


@dataclass(frozen=True)
class SyncResult:
    new: int
    seen: int
    gone: int
    full: bool


def sync(
    repo: Repo,
    store: RawStore,
    account: ConnectedAccount,
    client: GmailClient,
    now: dt.datetime | None = None,
) -> SyncResult:
    """Ingest every message not stored yet and move the cursor. Never commits."""
    full = account.history_id is None
    ids: list[str] = []
    if not full:
        try:
            ids, cursor = client.history(account.history_id)
        except HistoryExpired:
            log.warning("history %s expired for %s: full sync", account.history_id, account.id)
            full = True
    if full:
        # The cursor is read before listing: a message that arrives meanwhile is listed or
        # reported by the next history read, and a repeat is skipped by its key.
        _, cursor = client.profile()
        ids = list(client.list_message_ids())

    new = seen = gone = 0
    for message_id in dict.fromkeys(ids):  # in order, once each
        key = keys.gmail(str(account.id), message_id)
        if document_exists(repo, key):
            seen += 1
            continue
        try:
            raw, thread_id = client.get_raw(message_id)
        except MessageGone:
            gone += 1
            continue
        ingest(
            repo,
            store,
            raw,
            f"{message_id}.eml",
            path=f"gmail/{message_id}.eml",
            natural_key=key,
            source_kind=SOURCE_KIND,
            thread_id=thread_id,
        )
        new += 1
    repo.set_cursor(account.id, cursor, now or dt.datetime.now(dt.UTC))
    return SyncResult(new, seen, gone, full)


# --- Google ------------------------------------------------------------------------------------


def _status(e: Exception) -> int | None:
    resp = getattr(e, "resp", None)
    return getattr(resp, "status", None)


class GoogleGmailClient:
    """`GmailClient` over the Gmail API (`google-api-python-client`)."""

    def __init__(self, credentials: Any) -> None:
        from googleapiclient.discovery import build

        self._users = build("gmail", "v1", credentials=credentials, cache_discovery=False).users()

    def profile(self) -> tuple[str, str]:
        r = self._users.getProfile(userId="me").execute()
        return r["emailAddress"], str(r["historyId"])

    def list_message_ids(self) -> Iterator[str]:
        page = None
        while True:
            r = (
                self._users.messages()
                .list(userId="me", labelIds=[INBOX], pageToken=page, maxResults=500)
                .execute()
            )
            yield from (m["id"] for m in r.get("messages", []))
            if not (page := r.get("nextPageToken")):
                return

    def history(self, start_id: str) -> tuple[list[str], str]:
        from googleapiclient.errors import HttpError

        ids: list[str] = []
        page, latest = None, start_id
        try:
            while True:
                r = (
                    self._users.history()
                    .list(
                        userId="me",
                        startHistoryId=start_id,
                        historyTypes=["messageAdded"],
                        labelId=INBOX,
                        pageToken=page,
                    )
                    .execute()
                )
                for h in r.get("history", []):
                    ids += [a["message"]["id"] for a in h.get("messagesAdded", [])]
                latest = str(r.get("historyId", latest))
                if not (page := r.get("nextPageToken")):
                    return ids, latest
        except HttpError as e:
            if _status(e) == 404:
                raise HistoryExpired(start_id) from e
            raise

    def get_raw(self, message_id: str) -> tuple[bytes, str | None]:
        from googleapiclient.errors import HttpError

        try:
            r = self._users.messages().get(userId="me", id=message_id, format="raw").execute()
        except HttpError as e:
            if _status(e) == 404:
                raise MessageGone(message_id) from e
            raise
        return base64.urlsafe_b64decode(r["raw"]), r.get("threadId")


def _credentials(info: dict) -> Any:
    from google.oauth2.credentials import Credentials

    return Credentials.from_authorized_user_info(info, [READONLY])


def google_client(repo: Repo, account: ConnectedAccount) -> GmailClient:
    """Decrypt the account's token, refresh it if it has expired (storing the new one
    encrypted), and return a client."""
    from google.auth.transport.requests import Request

    cred = repo.session.scalar(
        select(OAuthCredential).where(
            OAuthCredential.tenant_id == repo.tenant_id, OAuthCredential.account_id == account.id
        )
    )
    if cred is None:
        raise RuntimeError(f"no stored token for {account.email_address}: run make gmail-connect")
    creds = _credentials(json.loads(secrets.decrypt(cred.ciphertext)))
    if not creds.valid:
        creds.refresh(Request())
        repo.set_credential(account.id, secrets.encrypt(creds.to_json().encode()), READONLY)
    return GoogleGmailClient(creds)


# --- Commands ----------------------------------------------------------------------------------


def connect(session: Session, client_secrets: Path, tenant_slug: str) -> ConnectedAccount:
    """Consent in the browser (read-only), then store the account and its encrypted token.
    Doesn't commit."""
    from google_auth_oauthlib.flow import InstalledAppFlow

    secrets.encrypt(b"")  # fail on a missing LEO_SECRET_KEY before opening the browser
    if not client_secrets.is_file():
        raise FileNotFoundError(f"no OAuth client file at {client_secrets} (see the README)")
    flow = InstalledAppFlow.from_client_secrets_file(str(client_secrets), [READONLY])
    # Force the consent screen: a repeat consent can omit the refresh token otherwise, and a
    # reconnect would overwrite the stored token with one sync can't load.
    creds = flow.run_local_server(port=0, prompt="consent")
    email, _ = GoogleGmailClient(creds).profile()
    return store_account(session, tenant_slug, email, creds.to_json().encode())


def store_account(session: Session, tenant_slug: str, email: str, token: bytes) -> ConnectedAccount:
    """Upsert the tenant, the account (a reconnect keeps its id) and the encrypted token."""
    tenant_id = upsert_tenant(session, tenant_slug, f"Gmail sync ({tenant_slug})")
    repo = Repo(session, tenant_id)
    account_id = repo.upsert_account(PROVIDER, email)
    repo.set_credential(account_id, secrets.encrypt(token), READONLY)
    return session.get_one(ConnectedAccount, account_id)


ClientFactory = Callable[[Repo, ConnectedAccount], GmailClient]


def sync_all(
    session: Session,
    store: RawStore,
    client_for: ClientFactory = google_client,
    tenant_id: UUID | None = None,
) -> list[tuple[str, SyncResult | None]]:
    """Sync every connected Gmail account (of one tenant, if given: the tests use that),
    committing each on its own. An account that fails is rolled back and logged, and the
    rest still sync."""
    q = select(ConnectedAccount).where(ConnectedAccount.provider == PROVIDER)
    if tenant_id is not None:
        q = q.where(ConnectedAccount.tenant_id == tenant_id)
    accounts = session.scalars(q.order_by(ConnectedAccount.email_address)).all()
    results: list[tuple[str, SyncResult | None]] = []
    for account in accounts:
        address = account.email_address
        try:
            repo = Repo(session, account.tenant_id)
            result = sync(repo, store, account, client_for(repo, account))
            session.commit()
        except Exception:
            session.rollback()
            log.exception("sync failed for %s", address)
            result = None
        results.append((address, result))
    return results


def main(argv: list[str] | None = None) -> None:
    from leo.config import get_settings
    from leo.db.session import get_sessionmaker

    settings = get_settings()
    parser = argparse.ArgumentParser(prog="python -m leo.ingest.gmail", description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    c = sub.add_parser("connect", help="connect a Gmail account (opens the browser)")
    c.add_argument("--client-secrets", type=Path, default=settings.leo_google_client_secrets)
    c.add_argument("--tenant", default=settings.leo_gmail_tenant)
    s = sub.add_parser("sync", help="sync every connected account")
    s.add_argument("--watch", type=float, metavar="SECONDS", help="repeat every SECONDS")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

    with get_sessionmaker()() as session:
        if args.command == "connect":
            try:
                account = connect(session, args.client_secrets, args.tenant)
            except (secrets.SecretKeyMissing, FileNotFoundError) as e:
                raise SystemExit(str(e)) from None
            session.commit()
            print(f"connected {account.email_address} to tenant {args.tenant}")
            return
        store = RawStore(settings.leo_raw_dir)
        try:
            while True:
                results = sync_all(session, store)
                if not results:
                    print("no connected account: run make gmail-connect")
                for address, r in results:
                    if r is not None:
                        kind = "full" if r.full else "incremental"
                        print(f"{address}: {r.new} new, {r.seen} seen, {r.gone} gone ({kind})")
                if args.watch is None:
                    return
                time.sleep(args.watch)
        except KeyboardInterrupt:
            pass


if __name__ == "__main__":
    main()
