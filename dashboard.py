#!/usr/bin/env python3
"""A live dashboard of my Linear tickets and GitHub PRs, with one-click Claude Code sessions.

Tickets (Todo / In Progress / In Review, plus any ticket one of my open PRs points at) with
their PRs nested underneath, each with a stage worked out from GitHub and Linear. Every row
gets "New session" (claude://code/new) and, when an earlier Claude Code session about it
exists, "Resume".

Run:  python3 dashboard.py --serve   → http://localhost:$PORT (live)
      python3 dashboard.py           → one-off dashboard.html
Config: .env next to this file (see .env.example). Needs `gh` logged in.
"""
import html
import json
import os
import re
import subprocess
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).parent


def _load_env(path: Path) -> None:
    """Minimal .env reader (KEY=VALUE lines); real environment variables win."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        line = line.split(" #")[0].strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip().strip("'\""))


_load_env(HERE / ".env")
GITHUB_REPOS = [r.strip() for r in os.environ.get("GITHUB_REPOS", "").split(",") if r.strip()]  # owner/name
CODE_DIR = Path(os.path.expanduser(os.environ.get("CODE_DIR", "~/code")))  # repos cloned here by name
DEFAULT_REPO = os.environ.get("DEFAULT_REPO") or (GITHUB_REPOS[0].split("/")[-1] if GITHUB_REPOS else "")
TICKET_FOLDER = CODE_DIR / DEFAULT_REPO  # where ticket sessions start
PORT = int(os.environ.get("PORT", "7777"))
SHOWN_STATES = {"Todo", "In Progress", "In Review"}
PROJECTS_DIR = Path.home() / ".claude/projects"
OUT = HERE / "dashboard.html"
CACHE = HERE / ".session-cache.json"
SCAN_VERSION = 3  # bump when _scan's output changes, to drop stale cache entries
DESKTOP_SESSIONS = Path.home() / "Library/Application Support/Claude/claude-code-sessions"
APP_VERSION = __import__("hashlib").sha1(Path(__file__).read_bytes()).hexdigest()[:12]
# Linear ids look like ENG-123. Narrowed to my teams' prefixes once tickets load (see build),
# so things like "GPT-4" in a PR title don't count as tickets.
TICKET_RE = re.compile(r"\b[A-Z][A-Z0-9]{1,9}-\d+\b")


# ---------- data ----------

PR_QUERY = """
query($q: String!) { search(query: $q, type: ISSUE, first: 50) { nodes { ... on PullRequest {
  number title url headRefName isDraft updatedAt reviewDecision
  repository { name nameWithOwner }
  commits(last: 1) { nodes { commit { committedDate statusCheckRollup { state } } } }
  latestReviews(first: 20) { nodes { state submittedAt author { login } } }
  reviewThreads(first: 100) { nodes { isResolved comments(last: 1) { nodes { createdAt author { login } } } } }
} } } }"""


def fetch_prs() -> list[dict]:
    """My open PRs in GITHUB_REPOS, with the facts the stage rules need."""
    me = subprocess.run(["gh", "api", "user", "--jq", ".login"],
                        capture_output=True, text=True, check=True).stdout.strip()
    q = f"is:pr is:open author:{me} " + " ".join(f"repo:{r}" for r in GITHUB_REPOS)
    out = subprocess.run(["gh", "api", "graphql", "-f", f"query={PR_QUERY}", "-f", f"q={q}"],
                         capture_output=True, text=True, check=True).stdout
    prs = []
    for n in json.loads(out)["data"]["search"]["nodes"]:
        if not n:
            continue
        commit = (n["commits"]["nodes"] or [{}])[0].get("commit") or {}
        last_commit = commit.get("committedDate") or ""
        open_threads = sum(
            1 for t in n["reviewThreads"]["nodes"] if not t["isResolved"] and t["comments"]["nodes"]
            and (t["comments"]["nodes"][-1]["author"] or {}).get("login") != me
            and t["comments"]["nodes"][-1]["createdAt"] > last_commit)
        changes_requested_since = any(
            r["state"] == "CHANGES_REQUESTED" and (r["submittedAt"] or "") > last_commit
            for r in n["latestReviews"]["nodes"])
        prs.append({**n, "repo": n["repository"]["name"], "full_repo": n["repository"]["nameWithOwner"],
                    "last_commit": last_commit,
                    "checks": (commit.get("statusCheckRollup") or {}).get("state"),
                    "open_threads": open_threads, "changes_requested_since": changes_requested_since})
    return prs


def local_branches() -> list[str]:
    """Branch names (local + remote-tracking) across the cloned repos, lowercased."""
    names = []
    for repo in GITHUB_REPOS:
        r = subprocess.run(["git", "-C", str(CODE_DIR / repo.split("/")[-1]), "for-each-ref", "--format=%(refname:short)",
                            "refs/heads", "refs/remotes"], capture_output=True, text=True)
        names += r.stdout.lower().split()
    return names


def linear_key() -> str:
    key = os.environ.get("LINEAR_API_KEY")
    if not key:
        raise RuntimeError("LINEAR_API_KEY isn't set; copy .env.example to .env and add one")
    return key


LINEAR_QUERY = """
{ viewer { assignedIssues(first: 100, filter: {state: {type: {in: ["unstarted", "started"]}}}) {
    nodes { identifier title url priority updatedAt state { name }
            attachments(first: 20) { nodes { url } } documents(first: 10) { nodes { title } } }
} } }
"""


def fetch_tickets() -> list[dict]:
    """My unstarted/started tickets. Tickets my PRs point at in other states come from fetch_issues."""
    req = urllib.request.Request(
        "https://api.linear.app/graphql", data=json.dumps({"query": LINEAR_QUERY}).encode(),
        headers={"Authorization": linear_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        body = json.load(r)
    if "errors" in body:
        raise RuntimeError(body["errors"])
    return body["data"]["viewer"]["assignedIssues"]["nodes"]


def fetch_issues(ids) -> list[dict]:
    """Look up specific tickets by identifier (one aliased GraphQL call)."""
    if not ids:
        return []
    fields = ("identifier title url priority updatedAt state { name } "
              "attachments(first: 20) { nodes { url } } documents(first: 10) { nodes { title } }")
    q = "{ " + " ".join(f'i{n}: issue(id: "{i}") {{ {fields} }}' for n, i in enumerate(ids)) + " }"
    req = urllib.request.Request(
        "https://api.linear.app/graphql", data=json.dumps({"query": q}).encode(),
        headers={"Authorization": linear_key(), "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=20) as r:
        data = json.load(r).get("data") or {}
    return [v for v in data.values() if v]


def pr_ticket_ids(pr: dict, tickets: list[dict]) -> set[str]:
    """Tickets a PR belongs to: Linear's GitHub attachment, or ENG-123 in its title or branch.
    Not the body: descriptions mention related/stacked tickets, which put PRs under the wrong ticket."""
    ids = {m.upper() for m in TICKET_RE.findall(f'{pr["title"]} {pr["headRefName"]}'.upper())}
    ids |= {t["identifier"] for t in tickets
            if any(a["url"].rstrip("/") == pr["url"] for a in t["attachments"]["nodes"])}
    return ids


# ---------- Claude Code sessions ----------

def _first_prompt(rec: dict) -> str | None:
    if rec.get("type") != "user" or rec.get("isMeta") or rec.get("isSidechain"):
        return None
    c = rec.get("message", {}).get("content")
    blocks = [c] if isinstance(c, str) else [
        b.get("text", "") for b in c or [] if isinstance(b, dict) and b.get("type") == "text"]
    # The desktop app prepends <system-reminder> blocks to the first message; skip those
    # (and command wrappers) block by block rather than rejecting the whole message.
    text = " ".join(t.strip() for t in blocks if t.strip() and not t.strip().startswith("<"))
    return text or None


def _turn_state(rec: dict, prev: str | None) -> str | None:
    """'waiting' once Claude ends a turn (or you interrupt it); 'working' while tools run."""
    if rec.get("isSidechain") or rec.get("type") not in ("user", "assistant"):
        return prev
    m = rec.get("message") or {}
    if rec["type"] == "assistant":
        return "waiting" if m.get("stop_reason") == "end_turn" else "working"
    c = m.get("content")
    if isinstance(c, list) and any(isinstance(b, dict) and b.get("type") == "tool_result" for b in c):
        return "working"
    text = c if isinstance(c, str) else " ".join(b.get("text", "") for b in c or [] if isinstance(b, dict))
    return "waiting" if "[Request interrupted" in text else ("working" if not rec.get("isMeta") else prev)


def _scan(path: Path) -> dict:
    branches, prompt, last, turn = set(), None, None, None
    with path.open(encoding="utf-8", errors="ignore") as f:
        for line in f:
            if '"gitBranch"' not in line and '"type":"user"' not in line and '"type":"assistant"' not in line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if b := rec.get("gitBranch"):
                branches.add(b)
            last = rec.get("timestamp") or last
            if prompt is None:
                prompt = _first_prompt(rec)
            turn = _turn_state(rec, turn)
    return {"branches": sorted(branches), "prompt": (prompt or "")[:300], "last": last, "turn": turn}


def desktop_index() -> dict[str, dict]:
    """CLI session id -> the desktop app's record of it (local_ id, title, archived)."""
    out = {}
    for f in DESKTOP_SESSIONS.glob("*/*/local_*.json"):
        try:
            d = json.loads(f.read_text())
        except (OSError, ValueError):
            continue
        if d.get("cliSessionId"):
            out[d["cliSessionId"]] = d
    return out


def load_sessions() -> list[dict]:
    try:
        cache = json.loads(CACHE.read_text())
    except (OSError, ValueError):
        cache = {}
    sessions, fresh, desktop = [], {}, desktop_index()
    for path in PROJECTS_DIR.glob(re.sub(r"[^A-Za-z0-9]", "-", str(CODE_DIR)) + "*/*.jsonl"):
        key, mtime = str(path), path.stat().st_mtime
        old = cache.get(key, {})
        info = old if (old.get("mtime"), old.get("v")) == (mtime, SCAN_VERSION) else None
        info = info or {**_scan(path), "mtime": mtime, "v": SCAN_VERSION}
        fresh[key] = info
        if info["last"]:
            d = desktop.get(path.stem, {})
            if not d.get("isArchived"):
                sessions.append({**info, "id": path.stem, "local_id": d.get("sessionId"),
                                 "title": d.get("title") or "",
                                 # the app's own one-liner; display only, never used for the stage
                                 "note": (d.get("postTurnSummary") or {}).get("status_detail") or ""})
    try:
        CACHE.write_text(json.dumps(fresh))
    except OSError:
        pass
    return sorted(sessions, key=lambda s: s["last"], reverse=True)


def sessions_for_ticket(tid: str, sessions) -> list[dict]:
    t = tid.lower()
    return [s for s in sessions
            if any(t in b.lower() for b in s["branches"]) or re.search(rf"\b{t}\b", f'{s["prompt"]} {s["title"]}'.lower())]


def sessions_for_pr(pr: dict, sessions) -> list[dict]:
    n = pr["number"]
    pat = re.compile(rf"(\bPR\s*#?|#|/pull/){n}\b", re.I)
    return [s for s in sessions if pr["headRefName"] in s["branches"] or pat.search(f'{s["prompt"]} {s["title"]}')]


# ---------- rendering ----------

def repo_folder(name: str) -> Path:
    p = CODE_DIR / name
    return p if p.is_dir() else TICKET_FOLDER


def new_link(prompt: str, folder: Path) -> str:
    return "claude://code/new?" + urllib.parse.urlencode(
        {"folder": str(folder), "q": prompt}, quote_via=urllib.parse.quote)


def resume_link(s: dict) -> str:
    if s.get("local_id"):
        return f"claude://code/continue?session={s['local_id']}"
    return f"claude://resume?session={s['id']}"


def ago(iso: str) -> str:
    d = datetime.now().astimezone() - datetime.fromisoformat(iso.replace("Z", "+00:00"))
    if d.days >= 1:
        return f"{d.days}d ago"
    h = d.seconds // 3600
    return f"{h}h ago" if h else f"{max(d.seconds // 60, 1)}m ago"


e = html.escape


def actions(new_href: str, sessions: list[dict]) -> str:
    out = f'<a class="btn" href="{e(new_href)}">New session</a>'
    if sessions:
        s = sessions[0]
        out = (f'<a class="btn primary" href="{e(resume_link(s))}" '
               f'title="{e((s["title"] or s["prompt"])[:120])}">Resume · {ago(s["last"])}</a>') + out
    if len(sessions) > 1:
        items = "".join(f'<li><a href="{e(resume_link(s))}">{ago(s["last"])} — '
                        f'{e((s["title"] or s["prompt"])[:90] or "(no prompt)")}</a></li>' for s in sessions[1:8])
        out += f'<details class="more"><summary>+{len(sessions) - 1}</summary><ul>{items}</ul></details>'
    return f'<div class="actions">{out}{HIDE_BTN}</div>'


HIDE_BTN = '<button class="hide" title="Hide (comes back on new activity)">×</button>'


# ---------- stages ----------
# Each stage comes from one fact in GitHub or Linear; `why` says which, and shows on hover.
PR_STAGES = ["Changes requested", "Ready to merge", "Draft PR", "In review"]  # a multi-PR ticket shows the first
STAGE_ORDER = ["Ready to merge", "Changes requested", "In review", "Draft PR",
               "Building", "Spec", "No work found", "Not started"]  # latest first
SPEC_RE = re.compile(r"^\s*(spec|implementation plan)\b", re.I)


def pr_stage(pr: dict) -> tuple[str, str]:
    """(stage, why) from GitHub's own fields."""
    n = f'#{pr["number"]}'
    if pr["isDraft"]:
        return "Draft PR", f"{n} is marked draft"
    if pr["changes_requested_since"]:
        return "Changes requested", f"{n}: a reviewer requested changes after your last commit"
    if pr["open_threads"]:
        k = pr["open_threads"]
        return "Changes requested", f"{n}: {k} unresolved review comment{'s' * (k > 1)} newer than your last commit"
    if pr["reviewDecision"] == "APPROVED":
        return "Ready to merge", f"{n} is approved"
    if pr["reviewDecision"] == "CHANGES_REQUESTED":
        return "In review", f"{n}: you pushed after changes were requested; waiting on a re-review"
    return "In review", f"{n} is ready for review; waiting on reviewers"


def checks_failing(pr: dict) -> bool:
    return pr.get("checks") in ("FAILURE", "ERROR")


def ticket_stage(t: dict, kid_prs: list[dict], sess: list[dict], branches: list[str]) -> dict:
    tid = t["identifier"]
    if kid_prs:
        ranked = sorted((pr_stage(p) for p in kid_prs), key=lambda x: PR_STAGES.index(x[0]))
        (stage, why), rest = ranked[0], [st for st, _ in ranked[1:]]
        return {"stage": stage, "why": why, "extra": ", ".join(f"+1 PR {st.lower()}" for st in rest),
                "failing": any(checks_failing(p) for p in kid_prs)}
    docs = [d["title"] for d in (t.get("documents") or {}).get("nodes", [])]
    spec = next((d for d in docs if SPEC_RE.match(d)), None)
    if spec or any(tid.lower() in b for b in branches):
        why = f"spec “{spec}” is on the ticket" if spec else "a branch for it exists"
        return {"stage": "Building", "why": why + "; no PR yet"}
    if sess:
        return {"stage": "Spec", "why": "a session exists but no “Spec — …” document is on the ticket yet"}
    if t["state"]["name"] != "Todo":
        # Linear says work began, but nothing here mentions the ticket: say so rather than guess.
        return {"stage": "No work found",
                "why": f"Linear says {t['state']['name']}, but no session, branch or PR here mentions {tid}"}
    return {"stage": "Not started", "why": "no session, branch or PR yet"}


def stage_badges(st: dict, sess: list[dict] | None = None) -> str:
    cls = st["stage"].lower().replace(" ", "-")
    out = f'<span class="stage {cls}" title="{e(st["why"])}">{e(st["stage"])}</span>'
    if sess and sess[0].get("turn") == "working" and st["stage"] in ("Spec", "Building"):
        out += '<span class="badge working">Claude working</span>'
    if st.get("failing"):
        out += '<span class="badge failing" title="A check failed on the latest commit">Checks failing</span>'
    if st.get("extra"):
        out += f'<span class="extra">{e(st["extra"])}</span>'
    return out


def pr_row(pr: dict, sessions) -> str:
    stage, why = pr_stage(pr)
    badges = stage_badges({"stage": stage, "why": why, "failing": checks_failing(pr)})
    return (f'<div class="pr" data-item="p:{e(pr["repo"])}#{pr["number"]}" data-updated="{e(pr["updatedAt"])}">'
            f'<div class="main"><a class="title" href="{e(pr["url"])}" target="_blank">'
            f'{e(pr["title"])}</a><div class="meta">{badges}'
            f'{e(pr["repo"])} #{pr["number"]} · {ago(pr["updatedAt"])}</div></div>'
            + actions(new_link(f'See PR #{pr["number"]} ({pr["full_repo"]})',
                               repo_folder(pr["repo"])), sessions_for_pr(pr, sessions)) + "</div>")


def render(tickets, prs, sessions, errors, branches=()) -> str:
    by_id = {t["identifier"]: t for t in tickets}
    pr_links = {pr["url"]: pr_ticket_ids(pr, tickets) for pr in prs}

    def ticket_block(t: dict, kid_prs: list[dict]) -> str:
        tid, n = t["identifier"], len(kid_prs)
        sess = sessions_for_ticket(tid, sessions)
        st = stages[tid]
        note = sess[0].get("note") if sess else ""
        kids = "".join(pr_row(p, sessions) for p in kid_prs)
        cls = {"In Progress": "started", "In Review": "review"}.get(t["state"]["name"], "")
        return (
            f'<section class="ticket" data-group="t:{e(tid)}" data-item="t:{e(tid)}" '
            f'data-updated="{e(t["updatedAt"])}"><div class="head"><div class="main">'
            f'<a class="title" href="{e(t["url"])}" target="_blank">{e(t["title"])}</a>'
            f'<div class="meta"><span class="badge {cls}">{e(t["state"]["name"])}</span>'
            f'{e(tid)} · {ago(t["updatedAt"])}'
            + (f'<button class="tog chip" data-toggle><span class="chev">▾</span> '
               f'<span class="n">{n} PR{"s" if n > 1 else ""}</span></button>' if n else "")
            + f'</div><div class="stageline">{stage_badges(st, sess)}</div>'
            + (f'<div class="note" title="The desktop app\'s own summary of the last session">{e(note)}</div>' if note else "")
            + '</div>'
            + actions(new_link(f"See ticket {tid}", TICKET_FOLDER), sess)
            + f'</div>{f"<div class=prs>{kids}</div>" if kids else ""}</section>')

    # Tickets = my Todo / In Progress / In Review, plus any ticket one of my open PRs points at
    # (whatever its state). PRs with no ticket at all are the only thing left for the second list.
    pr_ticket = {i for ids in pr_links.values() for i in ids if i in by_id}
    order = {"In Progress": 0, "In Review": 1, "Todo": 2}
    shown = sorted((t for t in tickets if t["identifier"] in pr_ticket or (
        t["state"]["name"] in SHOWN_STATES and not t.get("linked_only"))),
        key=lambda t: (order.get(t["state"]["name"], 3), t["priority"] or 9, t["identifier"]))
    stages = {t["identifier"]: ticket_stage(t, [p for p in prs if t["identifier"] in pr_links[p["url"]]],
                                            sessions_for_ticket(t["identifier"], sessions), branches)
              for t in shown}
    # Within each Linear state, furthest-along work first; priority order kept within a stage.
    shown.sort(key=lambda t: (order.get(t["state"]["name"], 3), STAGE_ORDER.index(stages[t["identifier"]]["stage"])))
    blocks = [ticket_block(t, [p for p in prs if t["identifier"] in pr_links[p["url"]]]) for t in shown]
    loose = [pr_row(p, sessions) for p in prs if not (pr_links[p["url"]] & pr_ticket)]

    err = "".join(f'<p class="err">{e(x)}</p>' for x in errors)
    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><meta name="app-version" content="{APP_VERSION}"><title>My Work · Ticket to Agent Session Manager</title>
<style>
:root {{ --bg:#fafaf9; --card:#fff; --fg:#1c1917; --muted:#78716c; --line:#e7e5e4; --accent:#c2410c; --accent-fg:#fff; --chip:#f5f5f4; --started:#dbeafe; --started-fg:#1e40af; --nest:#f5f5f4; --review:#ede9fe; --review-fg:#5b21b6; }}
@media (prefers-color-scheme: dark) {{ :root:not([data-theme="light"]) {{ --bg:#1c1917; --card:#292524; --fg:#f5f5f4; --muted:#a8a29e; --line:#44403c; --accent:#fb923c; --accent-fg:#1c1917; --chip:#44403c; --started:#1e3a8a; --started-fg:#dbeafe; --nest:#231f1d; --review:#4c1d95; --review-fg:#ede9fe; }} }}
* {{ box-sizing:border-box; }}
body {{ margin:0; background:var(--bg); color:var(--fg); font:15px/1.45 -apple-system,system-ui,sans-serif; }}
main {{ max-width:900px; margin:0 auto; padding:16px 16px 64px; }}
.searchbar {{ position:sticky; top:0; z-index:10; background:var(--bg); padding:12px 16px; border-bottom:1px solid var(--line); }}
#q {{ display:block; width:100%; max-width:868px; margin:0 auto; font:inherit; font-size:14px; padding:8px 12px; border-radius:8px; border:1px solid var(--line); background:var(--card); color:var(--fg); outline:none; }}
#q:focus {{ border-color:var(--accent); }}
.nomatch {{ display:none !important; }}
body.searching .collapsed > .prs, body.searching .collapsed > .list {{ display:block; }}
body.searching .list:not(:has(> [data-item]:not(.dup):not(.nomatch)))::after {{ content:"No matches."; }}
header {{ display:flex; justify-content:space-between; align-items:baseline; gap:12px; flex-wrap:wrap; margin-bottom:8px; }}
h1 {{ font-size:22px; margin:0; }} h2 {{ font-size:13px; text-transform:uppercase; letter-spacing:.06em; color:var(--muted); margin:28px 0 8px; }}
.stamp {{ color:var(--muted); font-size:13px; }}
.list {{ background:var(--card); border:1px solid var(--line); border-radius:10px; overflow:hidden; }}
.ticket + .ticket, .list > .pr + .pr {{ border-top:1px solid var(--line); }}
.head, .pr {{ display:flex; gap:8px 14px; align-items:center; padding:12px 14px; }}
.prs {{ background:var(--nest); border-top:1px dashed var(--line); }}
.prs .pr {{ padding-left:34px; position:relative; }}
.prs .pr::before {{ content:"↳"; position:absolute; left:14px; top:12px; color:var(--muted); }}
.prs .pr + .pr {{ border-top:1px dashed var(--line); }}
.main {{ flex:1; min-width:0; }}
.title {{ color:var(--fg); text-decoration:none; font-weight:500; }} .title:hover {{ text-decoration:underline; }}
.pr .title {{ font-weight:400; }}
.meta {{ color:var(--muted); font-size:13px; margin-top:2px; }}
.badge {{ display:inline-block; font-size:11px; padding:1px 7px; border-radius:99px; background:var(--chip); margin-right:6px; }}
.badge.started {{ background:var(--started); color:var(--started-fg); }}
.badge.review {{ background:var(--review); color:var(--review-fg); }}
.stageline {{ display:flex; flex-wrap:wrap; align-items:center; gap:4px 6px; margin-top:6px; }}
.pr .meta .stage, .pr .meta .badge {{ margin-right:6px; }}
.stage {{ display:inline-block; font-size:11px; font-weight:600; padding:1px 8px; border-radius:4px; border:1px solid var(--line); color:var(--muted); cursor:help; }}
.stage.changes-requested {{ border-color:#dc2626; color:#dc2626; }}
.stage.ready-to-merge {{ border-color:#16a34a; color:#16a34a; }}
.stage.draft-pr, .stage.building {{ border-color:var(--accent); color:var(--accent); }}
.stage.in-review {{ border-color:#7c3aed; color:#7c3aed; }}
.stage.spec {{ border-color:#0891b2; color:#0891b2; }}
.stage.no-work-found {{ border-style:dashed; }}
.badge.working {{ background:var(--chip); }}
.badge.failing {{ background:#fee2e2; color:#b91c1c; }}
.extra {{ font-size:12px; color:var(--muted); }}
.note {{ font-size:12px; color:var(--muted); margin-top:4px; font-style:italic; }}
.actions {{ flex:none; display:flex; gap:6px; align-items:center; position:relative; }}
.btn {{ font-size:12px; font-weight:600; padding:5px 10px; border-radius:6px; text-decoration:none; border:1px solid var(--line); color:var(--fg); background:var(--card); white-space:nowrap; }}
.btn.primary {{ background:var(--accent); color:var(--accent-fg); border-color:transparent; }}
.btn:hover {{ filter:brightness(1.06); }}
.more summary {{ cursor:pointer; font-size:12px; color:var(--muted); list-style:none; padding:4px 6px; }}
.more ul {{ position:absolute; right:0; top:34px; z-index:5; width:min(420px,80vw); margin:0; padding:6px; list-style:none; background:var(--card); border:1px solid var(--line); border-radius:8px; box-shadow:0 6px 20px rgba(0,0,0,.12); }}
.more li a {{ display:block; padding:6px 8px; font-size:13px; color:var(--fg); text-decoration:none; border-radius:5px; }}
.more li a:hover {{ background:var(--chip); }}
.tog {{ cursor:pointer; user-select:none; background:none; border:0; padding:0; font:inherit; color:inherit; }}
.tog:hover {{ color:var(--fg); }}
.chev {{ display:inline-block; transition:transform .15s; }}
.collapsed .chev {{ transform:rotate(-90deg); }}
.collapsed > .prs, .collapsed > .list {{ display:none; }}
.toolbar {{ display:flex; gap:12px; margin-top:4px; font-size:13px; }}
.toolbar .tog {{ color:var(--muted); }}
.hide {{ flex:none; width:26px; height:26px; border-radius:6px; border:0; background:none; color:var(--muted); font-size:18px; line-height:1; cursor:pointer; }}
.hide:hover {{ background:var(--chip); color:var(--fg); }}
.dup {{ display:none !important; }}
#hidden-list .title {{ color:var(--muted); }}
#hidden-list .hide {{ font-size:12px; width:auto; padding:0 8px; border:1px solid var(--line); }}
#hidden-list > .pr + .ticket, #hidden-list > .ticket + .pr {{ border-top:1px solid var(--line); }}
.list:not(:has(> [data-item]:not(.dup):not(.nomatch)))::after {{ content:"Nothing here."; display:block; color:var(--muted); padding:12px 14px; }}
#hidden-wrap:not(:has([data-item]:not(.dup):not(.nomatch))) {{ display:none; }}
.prs:not(:has(> .pr)) {{ display:none; }}
.err {{ color:#dc2626; }}
.chip {{ margin-left:8px; padding:0 6px; border-radius:99px; background:var(--chip); font-size:12px; }}
.chip[hidden] {{ display:none; }}
.title {{ font-size:15px; }} .pr .title {{ font-size:14px; }}
.meta {{ display:flex; flex-wrap:wrap; align-items:center; gap:2px 0; }}
@media (max-width:700px) {{
  .head, .pr {{ flex-wrap:wrap; }}
  .main {{ flex-basis:100%; }}
  .actions {{ width:100%; }}
  .actions .hide {{ margin-left:auto; }}
  .prs .pr {{ padding-left:30px; }}
}}
</style></head><body>
<div class="searchbar"><input id="q" type="search" placeholder="Search tickets & PRs — title, ENG-123, #42, stage…   ( / )" autocomplete="off"></div>
<main>
<header><h1>My Work</h1><span class="toolbar"><button class="tog" id="collapse-all">Collapse all PRs</button><button class="tog" id="expand-all">Expand all</button></span><span class="stamp">Updated {datetime.now():%b %-d, %-I:%M %p} · live</span></header>
{err}
<div data-group="s:tickets"><h2 class="tog" data-toggle><span class="chev">▾</span> Tickets (<span class="count"></span>)</h2><div class="list">{"".join(blocks)}</div></div>
<div data-group="s:prs"><h2 class="tog" data-toggle><span class="chev">▾</span> PRs without a ticket (<span class="count"></span>)</h2><div class="list">{"".join(loose)}</div></div>
<div data-group="s:hidden" id="hidden-wrap" class="collapsed"><h2 class="tog" data-toggle><span class="chev">▾</span> Hidden (<span class="count"></span>)</h2><div class="list" id="hidden-list"></div></div>
</main>
<script>
// Collapse state lives in localStorage (keyed by data-group) and is re-applied after every live swap.
const KEY = "tasm:collapsed";
const load = () => {{ try {{ return new Set(JSON.parse(localStorage.getItem(KEY) || "[]")); }} catch (_) {{ return new Set(); }} }};
const save = (set) => {{ try {{ localStorage.setItem(KEY, JSON.stringify([...set])); }} catch (_) {{}} }};
const HKEY = "tasm:hidden";
const loadH = () => {{ try {{ return JSON.parse(localStorage.getItem(HKEY) || "{{}}"); }} catch (_) {{ return {{}}; }} }};
const saveH = (h) => {{ try {{ localStorage.setItem(HKEY, JSON.stringify(h)); }} catch (_) {{}} }};
function applyHidden() {{
  const h = loadH(), list = document.getElementById("hidden-list"), seen = new Set();
  document.querySelectorAll("[data-item]").forEach(el => {{
    const at = h[el.dataset.item], hidden = !!at && at >= el.dataset.updated;
    const inHidden = list.contains(el);
    if (hidden && !inHidden) {{
      el._home = document.createComment("home");
      el.replaceWith(el._home);
      list.append(el);
    }} else if (!hidden && inHidden && el._home) {{
      el._home.replaceWith(el);
      el._home = null;
    }}
    const b = el.querySelector(":scope > .actions > .hide, :scope > .head > .actions > .hide");
    if (b) b.textContent = hidden ? "Unhide" : "×";
  }});
  // A PR listed under two tickets: show one copy in Hidden.
  list.querySelectorAll(":scope > [data-item]").forEach(el => {{
    el.classList.toggle("dup", seen.has(el.dataset.item)); seen.add(el.dataset.item);
  }});
  document.querySelectorAll("section.ticket").forEach(t => {{
    const chip = t.querySelector(":scope > .head .chip"); if (!chip) return;
    const n = t.querySelectorAll(":scope > .prs > .pr").length;
    chip.hidden = !n;
    chip.querySelector(".n").textContent = `${{n}} PR${{n === 1 ? "" : "s"}}`;
  }});
  document.querySelectorAll("[data-group^='s:']").forEach(g => {{
    const c = g.querySelector(".count");
    if (c) c.textContent = g.querySelectorAll(".list > [data-item]:not(.dup):not(.nomatch)").length;
  }});
}}
function applyCollapsed() {{
  applyHidden();
  const c = load();
  document.querySelectorAll("[data-group]").forEach(g => g.classList.toggle("collapsed",
    g.id === "hidden-wrap" ? !c.has("open:s:hidden") : c.has(g.dataset.group)));
}}
document.addEventListener("click", ev => {{
  const hb = ev.target.closest(".hide");
  if (hb) {{
    const el = hb.closest("[data-item]"), h = loadH();
    if (document.getElementById("hidden-list").contains(el)) delete h[el.dataset.item];
    else h[el.dataset.item] = el.dataset.updated;
    saveH(h); applySearch(); return;
  }}
  const c = load(), all = ev.target.closest("#collapse-all, #expand-all");
  if (all) {{
    document.querySelectorAll('[data-group^="t:"]').forEach(g =>
      all.id === "collapse-all" ? c.add(g.dataset.group) : c.delete(g.dataset.group));
    if (all.id === "expand-all") {{ c.delete("s:tickets"); c.delete("s:prs"); }}
  }} else {{
    const t = ev.target.closest("[data-toggle]");
    if (!t) return;
    const g = t.closest("[data-group]"), k = g.id === "hidden-wrap" ? "open:s:hidden" : g.dataset.group;
    c.has(k) ? c.delete(k) : c.add(k);
  }}
  save(c); applyCollapsed();
}});
// Search: every word must appear. A matching ticket keeps all its PRs; a matching PR keeps its ticket.
function applySearch() {{
  const terms = document.getElementById("q").value.toLowerCase().split(/\\s+/).filter(Boolean);
  const hit = el => {{ const t = el.textContent.toLowerCase(); return terms.every(w => t.includes(w)); }};
  document.body.classList.toggle("searching", terms.length > 0);
  document.querySelectorAll(".nomatch").forEach(el => el.classList.remove("nomatch"));
  if (terms.length) {{
    document.querySelectorAll("section.ticket").forEach(t => {{
      const own = hit(t.querySelector(":scope > .head .main"));
      let any = false;
      t.querySelectorAll(":scope > .prs > .pr").forEach(pr => {{
        const m = own || hit(pr.querySelector(".main")); any ||= m;
        pr.classList.toggle("nomatch", !m);
      }});
      t.classList.toggle("nomatch", !own && !any);
    }});
    document.querySelectorAll(".list > .pr").forEach(pr => pr.classList.toggle("nomatch", !hit(pr.querySelector(".main"))));
  }}
  applyHidden();
}}
document.getElementById("q").addEventListener("input", applySearch);
addEventListener("keydown", ev => {{
  const q = document.getElementById("q");
  if (ev.key === "/" && document.activeElement !== q) {{ ev.preventDefault(); q.focus(); }}
  else if (ev.key === "Escape" && document.activeElement === q) {{ q.value = ""; applySearch(); q.blur(); }}
}});
applyCollapsed();
applySearch();

// Live mode (served by `dashboard.py --serve`): swap in fresh content every 60s and on tab focus.
// Skipped when opened as a file, or while a "+N" session menu is open.
if (location.protocol.startsWith("http")) {{
  async function refresh() {{
    if (document.querySelector("details[open]")) return;
    try {{
      const r = await fetch("/", {{cache: "no-store"}});
      const doc = new DOMParser().parseFromString(await r.text(), "text/html");
      const ver = d => d.querySelector('meta[name="app-version"]')?.content;
      if (ver(doc) !== ver(document)) {{ location.reload(); return; }}
      const fresh = doc.querySelector("main");
      if (fresh && fresh.innerHTML !== document.querySelector("main").innerHTML)
        {{ document.querySelector("main").replaceWith(fresh); applyCollapsed(); applySearch(); }}
    }} catch (_) {{}}
  }}
  setInterval(refresh, 60000);
  addEventListener("focus", refresh);
}}
</script></body></html>"""


def build() -> tuple[str, str]:
    prs, tickets, errors = [], [], []
    try:
        prs = fetch_prs()
    except Exception as ex:  # keep the page useful if one source is down
        errors.append(f"GitHub: {ex}")
    try:
        tickets = fetch_tickets()
    except Exception as ex:
        errors.append(f"Linear: {ex}")
    global TICKET_RE
    prefixes = sorted({t["identifier"].split("-")[0] for t in tickets})
    if prefixes:
        TICKET_RE = re.compile(rf"\b(?:{'|'.join(map(re.escape, prefixes))})-\d+\b")
    try:
        have = {t["identifier"] for t in tickets}
        linked = {i for p in prs for i in pr_ticket_ids(p, tickets)} - have
        tickets += [{**t, "linked_only": True} for t in fetch_issues(sorted(linked))]
    except Exception as ex:
        errors.append(f"Linear (linked tickets): {ex}")
    sessions = load_sessions()
    summary = (f"{len(prs)} PRs, {sum(t['state']['name'] in SHOWN_STATES for t in tickets)} tickets, "
               f"{len(sessions)} sessions scanned" + "".join(f"\n  ! {x}" for x in errors))
    try:
        branches = local_branches()
    except Exception as ex:
        branches = []
        errors.append(f"git: {ex}")
    return render(tickets, prs, sessions, errors, branches), summary


def serve(port: int = PORT, max_age: float = 30):
    """Rebuild on request (at most every `max_age`s) so an open tab stays live."""
    import threading
    import time
    from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

    lock, cached = threading.Lock(), {"at": 0.0, "html": b""}

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path not in ("/", "/index.html"):
                self.send_error(404)
                return
            with lock:
                if time.monotonic() - cached["at"] > max_age:
                    cached["html"] = build()[0].encode()
                    cached["at"] = time.monotonic()
                body = cached["html"]
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_):
            pass

    print(f"Serving http://localhost:{port}")
    ThreadingHTTPServer(("127.0.0.1", port), Handler).serve_forever()


def main():
    import sys
    if "--serve" in sys.argv:
        serve()
        return
    page, summary = build()
    OUT.write_text(page)
    print(f"{OUT}  ({summary})")


if __name__ == "__main__":
    main()
