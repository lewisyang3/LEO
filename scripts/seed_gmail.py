"""Fill a demo Gmail account with the synthetic inbox (ROADMAP A8).

    uv run python scripts/seed_gmail.py [--folder DIR] [--drip SECONDS] [--only FILE] [--skip FILE]

Each `.eml` is imported with `messages.import`, so it lands in the Inbox as received mail,
keeping its From, To, Subject and dates (`internalDateSource=dateHeader`). `--drip` imports one
email every SECONDS, so they appear to arrive live while `make gmail-sync ARGS="--watch 10"`
runs. `--only` imports a single email (the 3.3 live demo). `--skip` (repeatable) leaves files out,
so the demo's first import holds back the email that arrives live. `--live` (with
`--only`) makes that email arrive as new: a fresh Message-ID, because Gmail otherwise matches an
earlier copy (even one in Trash) and just moves it back to the Inbox, which sync doesn't read as
a new message; and Gmail's receipt time, so it lands at the top of the Inbox, unread. The Date
header is kept, so LEO reads the email as sent when it says. Gmail adds its own Received headers
to every import anyway, so the bytes, and the LLM cache key, differ on each import regardless.

This asks for its own consent with the `gmail.insert` scope. That token stays in memory and is
never stored, so the credential LEO keeps stays read-only.

Not idempotent: running it twice imports every email twice, and Gmail gives the copies new
ids, so sync stores them twice. Use a fresh demo account, or delete the emails first.
"""

import argparse
import base64
import re
import sys
import time
import uuid
from pathlib import Path

INSERT = "https://www.googleapis.com/auth/gmail.insert"
DEFAULT_FOLDER = Path("data/synthetic/inbox")


def fresh_message_id(raw: bytes, token: str | None = None) -> bytes:
    """`raw` with its Message-ID replaced by a unique one, so Gmail imports it as a new message."""
    token = token or uuid.uuid4().hex[:12]
    out, n = re.subn(
        rb"^Message-ID:[ \t]*<([^>]*)>",
        lambda m: b"Message-ID: <live-" + token.encode() + b"." + m[1] + b">",
        raw,
        count=1,
        flags=re.MULTILINE | re.IGNORECASE,
    )
    if n == 0:
        raise ValueError("no Message-ID header to replace")
    return out


def select_files(folder: Path, only: str | None = None, skip: list[str] = ()) -> list[Path]:
    """The `.eml` files to import, in name order: just `only` if given, minus every `skip`."""
    files = sorted(p for p in folder.glob("*.eml") if not p.name.startswith("."))
    if only:
        files = [p for p in files if p.name == only]
    return [p for p in files if p.name not in set(skip)]


def main(argv: list[str] | None = None) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    from leo.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--drip", type=float, metavar="SECONDS", help="pause between emails")
    parser.add_argument("--only", metavar="FILE", help="import just this file from the folder")
    parser.add_argument(
        "--live",
        action="store_true",
        help="with --only: a fresh Message-ID and Gmail's receipt time, so it lands as new mail",
    )
    parser.add_argument(
        "--skip", metavar="FILE", action="append", default=[], help="leave this file out"
    )
    parser.add_argument(
        "--client-secrets", type=Path, default=get_settings().leo_google_client_secrets
    )
    args = parser.parse_args(argv)

    unknown = [n for n in args.skip if not (args.folder / n).is_file()]
    if unknown:
        sys.exit(f"--skip names no file in {args.folder}: {', '.join(unknown)}")
    if args.live and not args.only:
        sys.exit("--live needs --only FILE")
    files = select_files(args.folder, args.only, args.skip)
    if not files:
        sys.exit(f"no .eml files to import in {args.folder}")
    if not args.client_secrets.is_file():
        sys.exit(f"no OAuth client file at {args.client_secrets} (see the README)")

    print(f"importing {len(files)} email(s); a second run imports them again", file=sys.stderr)
    flow = InstalledAppFlow.from_client_secrets_file(str(args.client_secrets), [INSERT])
    creds = flow.run_local_server(port=0)
    messages = build("gmail", "v1", credentials=creds, cache_discovery=False).users().messages()
    for i, path in enumerate(files):
        if i and args.drip:
            time.sleep(args.drip)
        raw = path.read_bytes()
        if args.live:
            raw = fresh_message_id(raw)
        body = {
            "raw": base64.urlsafe_b64encode(raw).decode(),
            "labelIds": ["INBOX", "UNREAD"],
        }
        r = messages.import_(
            userId="me",
            body=body,
            internalDateSource="receivedTime" if args.live else "dateHeader",
            neverMarkSpam=True,
        ).execute()
        print(f"{path.name} -> {r['id']}")


if __name__ == "__main__":
    main()
