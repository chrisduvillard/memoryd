#!/usr/bin/env python3
"""M5 test — vector channel + rerank + S12 rebuild integrity.

Checks:
  embedder determinism (same text -> identical vector)
  similarity sanity (overlapping text > unrelated text cosine)
  write-time embedding rows exist after extraction-style insert
  vector-only retrieval: a memory with NO FTS term overlap with the query
    still surfaces via the vector channel (hash embedder: shared trigrams)
  hybrid rerank merges both channels without duplicates
  S12: drop embeddings -> /admin/rebuild-indexes -> frozen-query packets identical
"""
from __future__ import annotations

import json
import sys
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.stdout.reconfigure(encoding="utf-8")  # ✓/✗ on cp1252 Windows consoles

from psycopg.rows import dict_row  # noqa: E402
from memoryd.core import CFG, append_event, new_id, pool  # noqa: E402
from memoryd.embed import HashEmbedder, to_pgvector  # noqa: E402

PASS: list[str] = []
FAIL: list[str] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print(("  ✓ " if ok else "  ✗ ") + name + (f" — {detail}" if detail and not ok else ""))


def http(path: str, body: dict | None = None) -> tuple[int, dict]:
    req = urllib.request.Request(
        f"http://127.0.0.1:{CFG.port}{path}",
        data=json.dumps(body or {}).encode(),
        headers={"Content-Type": "application/json"}, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=30) as r:
            return r.status, json.loads(r.read())
    except urllib.error.HTTPError as e:
        return e.code, json.loads(e.read() or b"{}")


def cosine(a: list[float], b: list[float]) -> float:
    return sum(x * y for x, y in zip(a, b))


def main() -> int:
    print("== embedder unit checks ==")
    emb = HashEmbedder()
    v1 = emb.embed(["Alex prefers the ergonomic split keyboard with VIM bindings"])[0]
    v2 = emb.embed(["Alex prefers the ergonomic split keyboard with VIM bindings"])[0]
    v3 = emb.embed(["keyboard preferences: ergonomic split board, VIM keybindings enabled"])[0]
    v4 = emb.embed(["quarterly futures positioning report for energy markets"])[0]
    check("determinism (identical texts -> identical vectors)", v1 == v2)
    check("similarity sanity (related > unrelated)",
          cosine(v1, v3) > cosine(v1, v4) + 0.1,
          f"related={cosine(v1, v3):.3f} unrelated={cosine(v1, v4):.3f}")

    print("== seed memories ==")
    sid = "vectest-" + new_id("s")[-6:]
    with pool().connection() as conn:
        conn.row_factory = dict_row
        evt = append_event(conn, kind="user_message", session_id=sid,
                           project="vectest", payload={"text": "seed"})

        def seed(mtype: str, text: str, project: str | None = "vectest") -> str:
            mid = new_id("mem")
            conn.execute(
                """INSERT INTO memories (id,type,text,project,authority,confidence,status)
                   VALUES (%s,%s,%s,%s,'A2',0.8,'candidate')""", (mid, mtype, text, project))
            conn.execute("INSERT INTO memory_sources (memory_id,event_id) VALUES (%s,%s)",
                         (mid, evt))
            conn.execute("UPDATE memories SET status='active' WHERE id=%s", (mid,))
            v = emb.embed([text])[0]
            conn.execute("INSERT INTO mem_embeddings (memory_id,model,embedding) "
                         "VALUES (%s,%s,%s::vector)", (mid, emb.model, to_pgvector(v)))
            return mid

        # vector-only target: the query will use the words "keybindings"/"editor
        # config" — this memory shares NO >=4-char FTS lexeme with the query,
        # but shares character trigrams (keyb/bind/...) so the hash embedder
        # ranks it close. FTS alone cannot find it.
        m_vec = seed("workflow", "VIM keybinding setup across VSCode and Obsidian on an ergonomic split keyboard")
        # fts+vector target: shares the literal word "keybindings"
        m_both = seed("technical_fact", "editor keybindings config lives in dotfiles repo under vim/")
        # noise
        seed("technical_fact", "exchange funding rates refresh hourly in the market monitor")
        conn.commit()

        query = "how do I change my keybindings editor config?"
        fts_probe = conn.execute(
            """SELECT id FROM memories WHERE fts @@ to_tsquery('simple','keybindings | editor | config')
               AND id = %s""", (m_vec,)).fetchone()
        check("setup valid: vector-target invisible to FTS", fts_probe is None)

    print("== hybrid recall ==")
    code, pkt = http("/recall", {"prompt": query, "session_id": sid, "project": "vectest"})
    check("recall 200", code == 200, str(pkt)[:200])
    check("vector channel active", "vector" in pkt.get("channels", []), str(pkt.get("channels")))
    md = pkt.get("markdown", "")
    check("FTS+vector memory retrieved", m_both in md)
    check("vector-ONLY memory retrieved (FTS would miss it)", m_vec in md)
    check("no duplicate lines in packet",
          len(md.splitlines()) == len(set(md.splitlines())))

    print("== S12 rebuild integrity ==")
    frozen = [
        {"prompt": query, "session_id": "s12", "project": "vectest"},
        {"prompt": "exchange funding rates market monitor", "session_id": "s12",
         "project": "vectest"},
    ]
    # sync first: S12 asserts rebuild determinism from a FAITHFUL index state.
    # (An unsynced 'before' snapshot would make divergence a correct outcome —
    # which is exactly what this catches if you remove this line.)
    code, rb0 = http("/admin/rebuild-indexes")
    check("pre-sync rebuild ok", code == 200 and rb0.get("ok"), str(rb0))
    before = [http("/recall", q)[1].get("markdown") for q in frozen]
    with pool().connection() as conn:
        conn.execute("TRUNCATE mem_embeddings")
        n = conn.execute("SELECT count(*) FROM mem_embeddings").fetchone()[0]
        conn.commit()
    check("embeddings dropped", n == 0)
    code, rb = http("/admin/rebuild-indexes")
    check("rebuild endpoint ok", code == 200 and rb.get("ok"), str(rb))
    check("rebuild re-embedded rows", rb.get("reembedded", 0) >= 3, str(rb))
    after = [http("/recall", q)[1].get("markdown") for q in frozen]
    check("frozen-query packets identical after rebuild (S12)", before == after,
          "packet divergence detected")

    print(f"\n{len(PASS)} passed, {len(FAIL)} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
