"""Fill a demo Gmail account with the synthetic inbox (ROADMAP A8).

    uv run python scripts/seed_gmail.py [--folder DIR] [--drip SECONDS] [--only FILE]

Each `.eml` is imported with `messages.import`, so it lands in the Inbox as received mail,
keeping its From, To, Subject and dates (`internalDateSource=dateHeader`). `--drip` imports one
email every SECONDS, so they appear to arrive live while `make gmail-sync ARGS="--watch 10"`
runs. `--only` imports a single email (the 3.3 live demo).

This asks for its own consent with the `gmail.insert` scope. That token stays in memory and is
never stored, so the credential LEO keeps stays read-only.

Not idempotent: running it twice imports every email twice, and Gmail gives the copies new
ids, so sync stores them twice. Use a fresh demo account, or delete the emails first.
"""

import argparse
import base64
import sys
import time
from pathlib import Path

INSERT = "https://www.googleapis.com/auth/gmail.insert"
DEFAULT_FOLDER = Path("data/synthetic/inbox")


def main(argv: list[str] | None = None) -> None:
    from google_auth_oauthlib.flow import InstalledAppFlow
    from googleapiclient.discovery import build

    from leo.config import get_settings

    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--folder", type=Path, default=DEFAULT_FOLDER)
    parser.add_argument("--drip", type=float, metavar="SECONDS", help="pause between emails")
    parser.add_argument("--only", metavar="FILE", help="import just this file from the folder")
    parser.add_argument(
        "--client-secrets", type=Path, default=get_settings().leo_google_client_secrets
    )
    args = parser.parse_args(argv)

    files = sorted(p for p in args.folder.glob("*.eml") if not p.name.startswith("."))
    if args.only:
        files = [p for p in files if p.name == args.only]
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
        body = {
            "raw": base64.urlsafe_b64encode(path.read_bytes()).decode(),
            "labelIds": ["INBOX", "UNREAD"],
        }
        r = messages.import_(
            userId="me", body=body, internalDateSource="dateHeader", neverMarkSpam=True
        ).execute()
        print(f"{path.name} -> {r['id']}")


if __name__ == "__main__":
    main()
