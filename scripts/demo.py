#!/usr/bin/env python3
"""Render the dashboard with made-up data (for the README screenshot): writes docs/demo.html."""
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import dashboard as d  # noqa: E402

now = datetime.now(timezone.utc)
ago = lambda **kw: (now - timedelta(**kw)).strftime("%Y-%m-%dT%H:%M:%SZ")  # noqa: E731


def ticket(i, title, state, docs=(), hrs=3):
    return {"identifier": f"ENG-{i}", "title": title, "url": f"https://linear.app/acme/issue/ENG-{i}",
            "priority": 2, "updatedAt": ago(hours=hrs), "state": {"name": state},
            "attachments": {"nodes": []}, "documents": {"nodes": [{"title": t} for t in docs]}}


def pr(n, title, branch, draft=False, decision=None, threads=0, checks="SUCCESS", hrs=2):
    return {"number": n, "title": title, "url": f"https://github.com/acme/api/pull/{n}", "headRefName": branch,
            "isDraft": draft, "updatedAt": ago(hours=hrs), "reviewDecision": decision,
            "repo": "api", "full_repo": "acme/api", "last_commit": ago(hours=hrs), "checks": checks,
            "open_threads": threads, "changes_requested_since": False}


tickets = [
    ticket(412, "Rate-limit the public signup endpoint", "In Progress", hrs=1),
    ticket(398, "Export invoices as CSV", "In Progress", ["Spec — CSV invoice export"], hrs=5),
    ticket(405, "Dark mode for the settings page", "In Progress", hrs=8),
    ticket(377, "Retry failed webhook deliveries", "In Review", hrs=20),
    ticket(421, "Show last login on the account page", "Todo", hrs=30),
    ticket(430, "Paginate the audit log", "Todo", hrs=50),
]
prs = [
    pr(1187, "Rate-limit signup by IP (ENG-412)", "you/eng-412-rate-limit-signup", decision="APPROVED"),
    pr(1191, "Webhook retries with backoff (ENG-377)", "you/eng-377-webhook-retries", threads=2, hrs=6),
    pr(1193, "Dark mode tokens for settings (ENG-405)", "you/eng-405-dark-mode", draft=True, checks="FAILURE"),
    pr(1170, "Bump pytest to 8.3", "you/bump-pytest", hrs=26),
]
sessions = [
    {"id": "s1", "local_id": "local_demo1", "title": "Ticket ENG-412", "prompt": "See ticket ENG-412",
     "branches": ["you/eng-412-rate-limit-signup"], "last": ago(minutes=40), "turn": "waiting",
     "note": "PR #1187 approved; ready to merge"},
    {"id": "s2", "local_id": "local_demo2", "title": "Ticket ENG-398", "prompt": "See ticket ENG-398",
     "branches": ["main"], "last": ago(minutes=2), "turn": "working", "note": ""},
    {"id": "s3", "local_id": "local_demo3", "title": "Ticket ENG-405", "prompt": "See ticket ENG-405",
     "branches": ["you/eng-405-dark-mode"], "last": ago(hours=3), "turn": "waiting",
     "note": "draft PR open; contrast check failing on two tokens"},
]

d.TICKET_RE = __import__("re").compile(r"\bENG-\d+\b")
d.CODE_DIR = Path("/Users/you/code")  # keep your real paths out of the demo links
d.TICKET_FOLDER = d.CODE_DIR / "api"
out = ROOT / "docs" / "demo.html"
out.parent.mkdir(exist_ok=True)
out.write_text(d.render(tickets, prs, sessions, [], branches=[]))
print(out)
