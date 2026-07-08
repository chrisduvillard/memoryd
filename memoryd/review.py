"""Review CLI — the slice's human control plane (spec §5.4, L4).

Usage:
  python -m memoryd.review queue                 # pending review items
  python -m memoryd.review mem <mem_id>          # inspect a memory + sources + treatments
  python -m memoryd.review approve <queue_id>    # resolve: promote / confirm
  python -m memoryd.review reject <queue_id>     # resolve: reject / keep old
  python -m memoryd.review supersede <old> <new> [reason]   # explicit supersession
  python -m memoryd.review confirm <mem_id>      # user confirms -> active + treatment
"""
from __future__ import annotations

import sys

from psycopg.rows import dict_row

from .core import pool


def _q(conn):
    rows = conn.execute(
        """SELECT q.id, q.kind, q.memory_id, q.detail, q.ts, m.type, m.text, m.status
           FROM review_queue q LEFT JOIN memories m ON m.id=q.memory_id
           WHERE NOT q.resolved ORDER BY q.ts""").fetchall()
    if not rows:
        print("review queue empty")
    for r in rows:
        print(f"#{r['id']} [{r['kind']}] ({r['type']},{r['status']}) {r['memory_id']}")
        print(f"    {r['text']}")
        if r["detail"]:
            print(f"    detail: {r['detail']}")


def _mem(conn, mid: str):
    m = conn.execute("SELECT * FROM memories WHERE id=%s", (mid,)).fetchone()
    if not m:
        print("not found")
        return
    for k in ("id", "type", "status", "authority", "confidence", "project",
              "scope", "sensitivity", "valid_from", "valid_to", "text", "struct"):
        print(f"{k:12} {m[k]}")
    for s in conn.execute("SELECT event_id FROM memory_sources WHERE memory_id=%s", (mid,)):
        print(f"{'source':12} {s['event_id']}")
    for t in conn.execute("SELECT kind, by_ref, note, at FROM treatments "
                          "WHERE memory_id=%s ORDER BY at", (mid,)):
        print(f"{'treatment':12} {t['kind']} by {t['by_ref']}: {t['note']}")


def _resolve(conn, qid: int, approve: bool):
    q = conn.execute("SELECT * FROM review_queue WHERE id=%s AND NOT resolved",
                     (qid,)).fetchone()
    if not q:
        print("queue item not found or already resolved")
        return
    if approve and q["memory_id"]:
        if q["kind"] in ("promotion_request", "chaperone_hold"):
            conn.execute("UPDATE memories SET status='active', last_confirmed_at=now(), "
                         "useful_count = useful_count + 1 WHERE id=%s", (q["memory_id"],))
            conn.execute("INSERT INTO treatments (memory_id, kind, note) "
                         "VALUES (%s,'confirmed_by_user','approved via review CLI')",
                         (q["memory_id"],))
        elif q["kind"] == "contradiction":
            # approving a contradiction = the NEW memory wins -> supersede old
            new = (q["detail"] or {}).get("new_memory")
            if new:
                conn.execute("UPDATE memories SET status='active' WHERE id=%s AND status='candidate'",
                             (new,))
                conn.execute("INSERT INTO supersessions (old_id,new_id,reason) "
                             "VALUES (%s,%s,'user resolved contradiction') "
                             "ON CONFLICT DO NOTHING", (q["memory_id"], new))
    elif not approve and q["memory_id"] and q["kind"] in ("promotion_request", "chaperone_hold"):
        conn.execute("UPDATE memories SET status='rejected' WHERE id=%s", (q["memory_id"],))
        conn.execute("INSERT INTO treatments (memory_id, kind, note) "
                     "VALUES (%s,'contradicted_by_user','rejected via review CLI')",
                     (q["memory_id"],))
    conn.execute("UPDATE review_queue SET resolved=TRUE, resolution=%s WHERE id=%s",
                 ("approved" if approve else "rejected", qid))
    print(f"#{qid} {'approved' if approve else 'rejected'}")


def main() -> None:
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        return
    with pool().connection() as conn:
        conn.row_factory = dict_row
        cmd = args[0]
        if cmd == "queue":
            _q(conn)
        elif cmd == "mem" and len(args) > 1:
            _mem(conn, args[1])
        elif cmd == "approve" and len(args) > 1:
            _resolve(conn, int(args[1]), True)
        elif cmd == "reject" and len(args) > 1:
            _resolve(conn, int(args[1]), False)
        elif cmd == "confirm" and len(args) > 1:
            conn.execute("UPDATE memories SET status='active', last_confirmed_at=now(), "
                         "useful_count = useful_count + 1 WHERE id=%s", (args[1],))
            conn.execute("INSERT INTO treatments (memory_id, kind, note) "
                         "VALUES (%s,'confirmed_by_user','direct confirm')", (args[1],))
            print("confirmed")
        elif cmd == "supersede" and len(args) > 2:
            reason = args[3] if len(args) > 3 else "manual supersession"
            conn.execute("INSERT INTO supersessions (old_id,new_id,reason) VALUES (%s,%s,%s)",
                         (args[1], args[2], reason))
            print("superseded")
        else:
            print(__doc__)
        conn.commit()


if __name__ == "__main__":
    main()
