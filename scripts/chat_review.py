"""
Pull the web chat's unreviewed transcripts for an app-improvement review, and mark
them reviewed afterwards so the next review starts where this one stopped.

Against the deployed app (REVIEW_TOKEN must be set on both ends):

    PUBLIC_URL=https://<your-host> REVIEW_TOKEN=... python3 scripts/chat_review.py list
    PUBLIC_URL=https://<your-host> REVIEW_TOKEN=... python3 scripts/chat_review.py mark 12 13

Or straight against a DB file (a local dev DB; marking a backup copy changes
nothing in production):

    python3 scripts/chat_review.py --db ./journal_dev.db list

`list` prints the queue as readable text (`--json` for the raw payload): each
thread's unreviewed messages, tool calls with their inputs, tool results trimmed.
`mark ID… [--note TEXT]` advances each thread's watermark to its latest message.
If the chat may have moved on since you listed, pass `--through N` (the "review
through id" in the thread's header) so anything newer stays in the queue.
"""
import argparse
import json
import os
import sys
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
RESULT_CHARS = 600  # tool results get long (candidate lists); the input is the signal


def _api(method: str, path: str, body: dict | None = None):
    base = (os.environ.get("PUBLIC_URL") or "").rstrip("/")
    token = os.environ.get("REVIEW_TOKEN") or ""
    if not base or not token:
        sys.exit("set PUBLIC_URL and REVIEW_TOKEN (or pass --db)")
    req = urllib.request.Request(
        base + "/app" + path, method=method,
        data=json.dumps(body).encode() if body is not None else None,
        headers={"Authorization": f"Bearer {token}", "Content-Type": "application/json"})
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def _local(db_path: str):
    os.environ["JOURNAL_DB"] = db_path
    sys.path.insert(0, ROOT)
    import server
    server.init_db()
    return server


def _render(t: dict) -> str:
    out = [f"=== thread {t['id']} · {t['agent']}"
           + (f" · {t['context_key']}" if t["context_key"] else "")
           + f" · started {t['started_day']} · {t['message_count']} msgs"
           + f" · review through id {t['last_message_id']} ==="]
    for m in t["messages"]:
        c = m["content"]
        if m["role"] == "error":
            out.append(f"[{m['id']}] ERROR: {c}")
        elif isinstance(c, str):
            out.append(f"[{m['id']}] USER: {c}")
        else:
            for b in c:
                kind = b.get("type")
                if kind == "text":
                    out.append(f"[{m['id']}] ASSISTANT: {b['text']}")
                elif kind == "tool_use":
                    out.append(f"[{m['id']}] → {b['name']}({json.dumps(b.get('input'))})")
                elif kind == "tool_result":
                    res = str(b.get("content", ""))
                    if len(res) > RESULT_CHARS:
                        res = res[:RESULT_CHARS] + "…"
                    out.append(f"[{m['id']}] ← {'ERROR ' if b.get('is_error') else ''}{res}")
    return "\n".join(out)


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[1])
    ap.add_argument("--db", help="read/write this DB file directly instead of the API")
    sub = ap.add_subparsers(dest="cmd", required=True)
    ls = sub.add_parser("list")
    ls.add_argument("--status", default="unreviewed", choices=["unreviewed", "reviewed", "all"])
    ls.add_argument("--limit", type=int, default=50)
    ls.add_argument("--json", action="store_true")
    mk = sub.add_parser("mark")
    mk.add_argument("ids", type=int, nargs="+")
    mk.add_argument("--through", type=int)
    mk.add_argument("--note")
    a = ap.parse_args()
    S = _local(a.db) if a.db else None

    if a.cmd == "list":
        flag = {"unreviewed": False, "reviewed": True, "all": None}[a.status]
        threads = (S.chat_threads(reviewed=flag, limit=a.limit) if S
                   else _api("GET", f"/api/chats?status={a.status}&limit={a.limit}")["threads"])
        if a.json:
            print(json.dumps(threads, indent=1))
        else:
            print("\n\n".join(_render(t) for t in threads) or "nothing to review")
        return

    if a.through is not None and len(a.ids) > 1:
        sys.exit("--through applies to one thread at a time")
    for tid in a.ids:
        body = {"through": a.through, "note": a.note}
        res = (S.chat_mark_reviewed(tid, a.through, a.note) if S
               else _api("POST", f"/api/chats/{tid}/reviewed", body))
        print(json.dumps(res))


if __name__ == "__main__":
    main()
