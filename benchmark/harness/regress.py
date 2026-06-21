"""
Before/after regression: run a fixed representative question set through whatever
extension OMOP_EXT_DIR points at. Usage:
  OMOP_EXT_DIR=<dir> python regress.py <label>
Saves results/<label>/ and prints a compact table.
"""
import asyncio, json, os, sys
from agent_runner import run_question, EXT_DIR

RESULTS = os.path.expanduser("~/Desktop/omop-agent-improvement/results")
# representative spread incl. the specific failure modes that were fixed
REGRESSION_IDS = [3, 10, 13, 21, 25, 29, 31, 40, 45, 46, 57, 61]


async def main():
    label = sys.argv[1]
    byid = {q["id"]: q for q in json.load(open("../questions/questions.json"))}
    outdir = os.path.join(RESULTS, label)
    os.makedirs(outdir, exist_ok=True)
    print(f"ext_dir = {EXT_DIR}")
    print(f"{'id':>3} {'tier':>4} {'iter':>4} {'tcalls':>6} {'tokens':>7} {'wall_s':>7}  err")
    summ = []
    for i, qid in enumerate(REGRESSION_IDS):
        if i:
            await asyncio.sleep(4)
        q = byid[qid]
        r = await run_question(q["question"])
        r["id"] = qid; r["tier"] = q["tier"]
        json.dump(r, open(os.path.join(outdir, f"q{qid:03d}.json"), "w"), indent=2, default=str)
        summ.append({"id": qid, "tier": q["tier"], "iterations": r["iterations"],
                     "tool_calls": r["tool_calls"], "total_tokens": r["total_tokens"],
                     "wall_seconds": round(r["wall_seconds"], 1), "error": r["error"],
                     "answer": r["answer"]})
        print(f"{qid:>3} {q['tier']:>4} {r['iterations']:>4} {r['tool_calls']:>6} "
              f"{r['total_tokens']:>7} {round(r['wall_seconds'],1):>7}  {'ERR' if r['error'] else ''}")
    json.dump(summ, open(os.path.join(outdir, "_summary.json"), "w"), indent=2, default=str)
    n = len(summ)
    print(f"\n{label}: avg_iter={sum(s['iterations'] for s in summ)/n:.1f} "
          f"avg_tcalls={sum(s['tool_calls'] for s in summ)/n:.1f} "
          f"avg_tokens={sum(s['total_tokens'] for s in summ)/n:,.0f} "
          f"avg_wall={sum(s['wall_seconds'] for s in summ)/n:.1f}s "
          f"errors={sum(1 for s in summ if s['error'])}")


if __name__ == "__main__":
    asyncio.run(main())
