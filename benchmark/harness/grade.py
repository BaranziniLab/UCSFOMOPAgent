"""
Grade a results batch using MiMo as an LLM judge.
Usage: python grade.py <label>   e.g. python grade.py v0_batch1
Reads results/<label>/q*.json + questions/questions.json, writes results/<label>/_grades.json
"""
import json, os, sys, subprocess
import httpx

MIMO_HOST = "https://token-plan-sgp.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"
RESULTS_DIR = os.path.expanduser("~/Desktop/omop-agent-improvement/results")
QFILE = os.path.expanduser("~/Desktop/omop-agent-improvement/questions/questions.json")

JUDGE_SYS = (
    "You are a strict grader for a clinical-database QA agent. Given a question, the "
    "GROUND TRUTH (a value computed directly against the database, or a rubric for "
    "open-ended questions), and the AGENT'S ANSWER, decide a verdict.\n"
    "Verdicts:\n"
    "- CORRECT: the agent's key numeric answer matches the ground truth (allow tiny rounding; "
    "ignore formatting like commas). For distributions, the top items/order are right.\n"
    "- PARTIAL: right approach / close but a meaningful number is off, incomplete, or only "
    "partially matches; or (rubric) hits some but not all checkpoints.\n"
    "- WRONG: no answer, wrong number, or wrong approach.\n"
    "For rubric (open-ended) questions, judge whether the answer satisfies the checkpoints.\n"
    'Respond ONLY with compact JSON: {"verdict":"CORRECT|PARTIAL|WRONG","reason":"<one sentence>"}'
)


def api_key():
    blob = subprocess.check_output(
        ["security", "find-generic-password", "-s", "biorouter", "-a", "secrets", "-w"])
    return json.loads(blob)["XIAOMI_MIMO_API_KEY"]


def judge(client, key, q, gt, rubric, answer):
    gt_block = f"GROUND TRUTH VALUE:\n{gt}" if gt else f"RUBRIC CHECKPOINTS:\n{json.dumps(rubric)}"
    user = (f"QUESTION:\n{q}\n\n{gt_block}\n\nAGENT'S ANSWER:\n{answer or '(no answer / error)'}")
    import time as _t
    for attempt in range(6):
        resp = client.post(f"{MIMO_HOST}/chat/completions",
                           headers={"Authorization": f"Bearer {key}"},
                           json={"model": MIMO_MODEL, "temperature": 0,
                                 "messages": [{"role": "system", "content": JUDGE_SYS},
                                              {"role": "user", "content": user}]})
        if resp.status_code in (429, 500, 502, 503, 529):
            _t.sleep(min(2 ** attempt * 3, 60)); continue
        resp.raise_for_status()
        break
    txt = resp.json()["choices"][0]["message"]["content"].strip()
    # strip code fences if any
    if txt.startswith("```"):
        txt = txt.split("```")[1].lstrip("json").strip()
    try:
        return json.loads(txt)
    except Exception:
        v = "CORRECT" if "CORRECT" in txt else ("PARTIAL" if "PARTIAL" in txt else "WRONG")
        return {"verdict": v, "reason": txt[:120]}


def main():
    label = sys.argv[1]
    outdir = os.path.join(RESULTS_DIR, label)
    questions = {q["id"]: q for q in json.load(open(QFILE))}
    key = api_key()
    grades = []
    files = sorted(f for f in os.listdir(outdir) if f.startswith("q") and f.endswith(".json"))
    with httpx.Client(timeout=120) as client:
        for fn in files:
            rec = json.load(open(os.path.join(outdir, fn)))
            qid = rec["id"]
            q = questions[qid]
            g = judge(client, key, q["question"], q.get("ground_truth_value"),
                      q.get("rubric"), rec.get("answer"))
            g.update({"id": qid, "tier": q["tier"],
                      "iterations": rec["iterations"], "tool_calls": rec["tool_calls"],
                      "total_tokens": rec["total_tokens"],
                      "wall_seconds": round(rec["wall_seconds"], 1),
                      "had_error": bool(rec["error"])})
            grades.append(g)
            print(f"q{qid:>3} T{q['tier']} {g['verdict']:>7}  {g['reason'][:80]}")

    with open(os.path.join(outdir, "_grades.json"), "w") as f:
        json.dump(grades, f, indent=2)

    from collections import Counter
    c = Counter(g["verdict"] for g in grades)
    n = len(grades)
    score = (c["CORRECT"] + 0.5 * c["PARTIAL"]) / n
    print(f"\n=== {label} grades (n={n}) ===")
    print(f"  CORRECT={c['CORRECT']}  PARTIAL={c['PARTIAL']}  WRONG={c['WRONG']}")
    print(f"  weighted score = {score:.1%}")
    print(f"  avg iterations={sum(g['iterations'] for g in grades)/n:.1f}  "
          f"avg tool_calls={sum(g['tool_calls'] for g in grades)/n:.1f}  "
          f"avg tokens={sum(g['total_tokens'] for g in grades)/n:,.0f}  "
          f"avg wall={sum(g['wall_seconds'] for g in grades)/n:.1f}s")


if __name__ == "__main__":
    main()
