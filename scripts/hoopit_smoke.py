"""Read-only check of a Hoopit personal access token against the real API.

    uv run --project familiekilder python -I familiekilder/scripts/hoopit_smoke.py \
        --token-file PATH [--posts 5] [--days 14]

or with the token on stdin (``... hoopit_smoke.py < PATH``). Never pass the
token as an argument: it would end up in the shell history.

The script uses a new, temporary data root, so it neither reads nor changes
a stored connection. It checks the token's format, then makes only GET
requests through the package's own client (pinned versions, one request a
second, no retry):

1. the first pages of ``me/posts/``, up to ``--posts`` posts,
2. the comments of the first listed post that has any (at most the first
   ``--posts`` posts are tried),
3. ``me/occurrences/`` for the next ``--days`` days.

It prints one JSON object with counts, IDs, titles and times, and any
version warning Hoopit sent. It never prints the token, comment texts, post
texts or names of people.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import UTC, datetime, timedelta
from pathlib import Path


def _read_token(path: str | None) -> str:
    if path:
        return Path(path).read_text(encoding="utf-8")
    if sys.stdin.isatty():
        raise SystemExit("Send tokenet på stdin eller med --token-file, aldri som argument.")
    return sys.stdin.read(4097)


def run(token: str, *, posts: int, days: int) -> dict:
    from familiekilder import hoopit, hoopit_auth
    from familiekilder.hoopit_auth import HoopitAccess

    value = hoopit_auth.validate_token_format(token)
    with tempfile.TemporaryDirectory(prefix="hoopit-smoke-") as directory:
        root = Path(directory) / "root"
        root.mkdir(mode=0o700)
        limiter = hoopit.BudgetedRateLimiter(
            hoopit.PersistentRateLimiter(root), hoopit.RequestBudget(4 + posts + 25)
        )
        client = hoopit.Client(
            HoopitAccess(token=value, account_scope="0" * 64),
            hoopit.default_transport(),
            limiter,
        )
        client.versions = hoopit.VersionWatch(root)
        report: dict = {"token_format": "ok"}

        listed = []
        for page in hoopit.iter_pages(client, "posts", label="innlegg", maximum_pages=3):
            listed.extend(page)
            if len(listed) >= posts:
                break
        listed = listed[:posts]
        report["posts"] = [
            {
                "id": hoopit.identifier(post.get("id"), "innleggs-ID"),
                "title": post.get("title"),
                "created_at": post.get("created_at"),
                "group": (post.get("group") or {}).get("name"),
            }
            for post in listed
        ]

        report["comments"] = None
        for post in listed:
            post_id = hoopit.identifier(post.get("id"), "innleggs-ID")
            tree = hoopit.post_comments(client, post_id)
            if tree:
                replies = sum(len(row.get("_replies", [])) for row in tree)
                report["comments"] = {
                    "post_id": post_id,
                    "top_level": len(tree),
                    "replies": replies,
                    "ids": [hoopit.identifier(row.get("id"), "kommentar-ID") for row in tree],
                }
                break

        now = datetime.now(UTC)
        occurrences = hoopit.fetch_occurrences(
            client, now, now + timedelta(days=days), maximum_pages=5
        )
        report["occurrences"] = [
            {
                "event_id": hoopit.identifier(row.get("event_id"), "aktivitets-ID"),
                "id": hoopit.identifier(row.get("id"), "forekomst-ID"),
                "title": row.get("title"),
                "event_type": row.get("event_type"),
                "start": row.get("start"),
                "cancelled": row.get("cancelled"),
                "responses": len(row.get("responses") or []),
            }
            for row in occurrences
        ]
        report["requests"] = limiter.budget.used
        version_file = hoopit_auth.api_version_path(root)
        report["api_version_warning"] = (
            hoopit.version_warning_text(json.loads(version_file.read_text(encoding="utf-8")))
            if version_file.exists()
            else None
        )
        return report


def main() -> int:
    parser = argparse.ArgumentParser(description="Les-bare-kontroll av et Hoopit-token.")
    parser.add_argument("--token-file", help="Fil med tokenet; utelat for å lese stdin")
    parser.add_argument("--posts", type=int, default=5, choices=range(1, 21), metavar="1-20")
    parser.add_argument("--days", type=int, default=14, choices=range(1, 61), metavar="1-60")
    args = parser.parse_args()
    try:
        report = run(_read_token(args.token_file), posts=args.posts, days=args.days)
    except Exception as error:  # noqa: BLE001 - the package's errors carry no token
        report = {"error": type(error).__name__, "detail": str(error)}
        print(json.dumps(report, ensure_ascii=False, indent=2))
        return 1
    print(json.dumps(report, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
