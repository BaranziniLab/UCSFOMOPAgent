"""
Run a batch of questions through the harness, save full transcripts to disk,
print a compact metrics table. Usage:
  python run_batch.py <questions.json> <label> [id_start] [id_end]
"""
import asyncio, json, os, sys, time
from agent_runner import run_question

RESULTS_DIR = os.path.expanduser("~/Desktop/omop-agent-improvement/results")


async def main():
    qfile = sys.argv[1]
    label = sys.argv[2]                      # e.g. "v0_batch1"
    id_start = int(sys.argv[3]) if len(sys.argv) > 3 else 1
    id_end = int(sys.argv[4]) if len(sys.argv) > 4 else 999

    questions = json.load(open(qfile))
    batch = [q for q in questions if id_start <= q["id"] <= id_end]

    outdir = os.path.join(RESULTS_DIR, label)
    os.makedirs(outdir, exist_ok=True)
    summary = []

    print(f"Running {len(batch)} questions [{id_start}..{id_end}] label={label}\n")
    print(f"{'id':>3} {'tier':>4} {'iter':>4} {'tcalls':>6} {'tok':>7} {'db_s':>6} {'wall_s':>7}  ok  question")
    for qi, q in enumerate(batch):
        if qi:
            await asyncio.sleep(4)  # gentle pacing to avoid MiMo rate limits
        rec = await run_question(q["question"])
        rec["id"] = q["id"]
        rec["tier"] = q.get("tier")
        rec["ground_truth"] = q.get("ground_truth")
        # save full record (with transcript) to disk
        with open(os.path.join(outdir, f"q{q['id']:03d}.json"), "w") as f:
            json.dump(rec, f, indent=2, default=str)
        err = "ERR" if rec["error"] else "  "
        s = {
            "id": q["id"], "tier": q.get("tier"), "iterations": rec["iterations"],
            "tool_calls": rec["tool_calls"], "total_tokens": rec["total_tokens"],
            "tool_db_seconds": round(rec["tool_db_seconds"], 1),
            "wall_seconds": round(rec["wall_seconds"], 1),
            "error": rec["error"], "answer": rec["answer"],
            "tool_names": rec["tool_names"],
        }
        summary.append(s)
        print(f"{q['id']:>3} {str(q.get('tier')):>4} {rec['iterations']:>4} "
              f"{rec['tool_calls']:>6} {rec['total_tokens']:>7} "
              f"{round(rec['tool_db_seconds'],1):>6} {round(rec['wall_seconds'],1):>7}  "
              f"{err:>3} {q['question'][:60]}")

    with open(os.path.join(outdir, "_summary.json"), "w") as f:
        json.dump(summary, f, indent=2, default=str)

    # aggregate
    n = len(summary)
    errs = sum(1 for s in summary if s["error"])
    print(f"\n=== {label}: {n} questions ===")
    print(f"  errors/timeouts : {errs}")
    print(f"  avg iterations  : {sum(s['iterations'] for s in summary)/n:.1f}")
    print(f"  avg tool_calls  : {sum(s['tool_calls'] for s in summary)/n:.1f}")
    print(f"  avg tokens      : {sum(s['total_tokens'] for s in summary)/n:,.0f}")
    print(f"  total tokens    : {sum(s['total_tokens'] for s in summary):,}")
    print(f"  avg wall_s      : {sum(s['wall_seconds'] for s in summary)/n:.1f}")
    print(f"  saved -> {outdir}")


if __name__ == "__main__":
    asyncio.run(main())
