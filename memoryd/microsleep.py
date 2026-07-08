"""Micro-sleep (spec §5.4): the slice's only consolidation. Run nightly via cron:

  5 3 * * *  MEMORYD_DSN=... python3 -m memoryd.microsleep

Steps: drain spool -> retry missed extractions -> expire priming ->
decay stale candidates -> write daily digest (the slice's human dashboard).
Deliberately boring; deep-sleep replay/rewrite is post-slice and gated on
the regression harness existing first (spec Family H risk).
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from psycopg.rows import dict_row
from psycopg.types.json import Jsonb

from .core import CFG, new_id, pool
from .extract import run_extraction
from .ingest import drain_spool


def main() -> None:
    CFG.ensure_dirs()
    report: list[str] = [f"# memoryd digest — {date.today().isoformat()}", ""]

    drained = drain_spool()
    report.append(f"- spool drained: {drained}")

    with pool().connection() as conn:
        conn.row_factory = dict_row

        # retry: sessions captured (or failed mid-extraction — e.g. the Hermes
        # /extract path, which never writes a capture_ack) but never
        # successfully extracted
        pending = conn.execute(
            """SELECT DISTINCT e.session_id FROM events e
               WHERE ((e.kind='capture_ack'
                       AND e.payload->>'trigger' IN ('session_end','pre_compact'))
                      OR (e.kind='extraction_run' AND e.payload->>'ok'='false'))
                 AND NOT EXISTS (SELECT 1 FROM events x
                                 WHERE x.session_id=e.session_id
                                   AND x.kind='extraction_run'
                                   AND x.payload->>'ok'='true')
               LIMIT 25""").fetchall()
        retried = 0
        for row in pending:
            try:
                res = run_extraction(row["session_id"])
            except Exception:  # noqa: BLE001 — one bad session must not kill the night
                continue
            if res.get("ok"):
                retried += 1
        report.append(f"- extractions retried: {retried}/{len(pending)} pending")

        # expire priming (validity filter already hides them; make state explicit)
        n = conn.execute(
            """UPDATE memories SET status='superseded'
               WHERE type='priming' AND status='active'
                 AND valid_to < CURRENT_DATE RETURNING id""").fetchall()
        report.append(f"- priming expired: {len(n)}")

        # decay: candidates unconfirmed and unrecalled past their half-life -> rejected
        n = conn.execute(
            """UPDATE memories SET status='rejected'
               WHERE status='candidate' AND half_life_d IS NOT NULL
                 AND created_at < now() - (half_life_d || ' days')::interval
                 AND last_confirmed_at IS NULL AND recall_count = 0
               RETURNING id""").fetchall()
        report.append(f"- candidates decayed: {len(n)}")

        # backfill missing embeddings (write-time embed can fail; indexes are views)
        missing = conn.execute(
            """SELECT m.id, m.text FROM memories m
               LEFT JOIN mem_embeddings e ON e.memory_id = m.id
               WHERE e.memory_id IS NULL AND m.status IN ('active','candidate')
               LIMIT 500""").fetchall()
        if missing:
            from .embed import get_embedder, to_pgvector
            emb = get_embedder()
            for i in range(0, len(missing), 64):
                batch = missing[i:i + 64]
                vecs = emb.embed([r["text"] for r in batch])
                for r, v in zip(batch, vecs):
                    conn.execute(
                        "INSERT INTO mem_embeddings (memory_id, model, embedding) "
                        "VALUES (%s,%s,%s::vector) ON CONFLICT (memory_id) DO NOTHING",
                        (r["id"], emb.model, to_pgvector(v)))
        report.append(f"- embeddings backfilled: {len(missing)}")

        # health counters (Common-Memory-Picture-lite)
        counts = conn.execute(
            "SELECT status, count(*) FROM memories GROUP BY status").fetchall()
        report.append("\n## Memory state")
        for c in counts:
            report.append(f"- {c['status']}: {c['count']}")

        pend = conn.execute(
            "SELECT count(*) FROM review_queue WHERE NOT resolved").fetchone()["count"]
        report.append(f"\n## Needs you\n- pending reviews: {pend}"
                      + ("  <- run `memoryd review queue`" if pend else ""))

        vetoes = conn.execute(
            """SELECT count(*) FROM events WHERE kind='veto'
               AND ts > now() - interval '1 day'""").fetchone()["count"]
        if vetoes:
            report.append(f"- [!] canary/veto events in last 24h: {vetoes} — INVESTIGATE")

        misses = conn.execute(
            """SELECT count(*) FROM miss_signals
               WHERE ts > now() - interval '1 day'""").fetchone()["count"]
        report.append(f"- retrieval-miss signals (24h): {misses}")

        recalls = conn.execute(
            """SELECT count(*) AS n, coalesce(avg(latency_ms),0)::int AS avg_ms
               FROM recall_log WHERE ts > now() - interval '1 day'""").fetchone()
        report.append(f"- recalls (24h): {recalls['n']}, avg {recalls['avg_ms']}ms")

        try:
            from .evaluator import run_static_eval
            cases = conn.execute(
                "SELECT id, kind, input, expected FROM eval_cases "
                "WHERE enabled ORDER BY created_at, id LIMIT 50").fetchall()
            eval_result = run_static_eval(cases=[
                {"id": r["id"], "kind": r["kind"], "input": r["input"], "expected": r["expected"]}
                for r in cases
            ])
            conn.execute(
                "INSERT INTO eval_runs (id, profile, status, summary, metrics) "
                "VALUES (%s,%s,%s,%s,%s)",
                (new_id("eval"), eval_result["model_profile"],
                 "pass" if eval_result["failed"] == 0 else "fail",
                 Jsonb(eval_result), Jsonb({
                     "cases": eval_result["cases"],
                     "passed": eval_result["passed"],
                     "failed": eval_result["failed"],
                 })))
            report.append("- nightly eval: "
                          f"{eval_result['passed']}/{eval_result['cases']} passed")
        except Exception as e:  # noqa: BLE001 - migration may not be applied yet
            report.append(f"- nightly eval: skipped ({str(e)[:120]})")
        conn.commit()

    out = CFG.home / "digest" / f"{date.today().isoformat()}.md"
    out.write_text("\n".join(report) + "\n", encoding="utf-8")
    print("\n".join(report))
    print(f"\nwritten: {out}")


if __name__ == "__main__":
    main()
