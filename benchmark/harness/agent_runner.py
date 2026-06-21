"""
Faithful test harness: drives MiMo (mimo-v2.5-pro) against the REAL ucsfomopagent
MCP server over stdio. Whatever tools/descriptions/instructions server.py exposes
are what the model sees, so source improvements are actually exercised.

System prompt is FIXED & neutral across all runs -> measured gains come from the
extension, not from harness tuning.
"""
import asyncio, json, os, subprocess, time, sys, contextlib
import httpx
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

EXT_DIR = os.environ.get("OMOP_EXT_DIR",
                         os.path.expanduser("~/.config/biorouter/extensions/ucsfomopagent"))
MIMO_HOST = "https://token-plan-sgp.xiaomimimo.com/v1"
MIMO_MODEL = "mimo-v2.5-pro"

# Fixed neutral system prompt — mirrors a generic BioRouter agent. Do NOT put
# OMOP-specific knowledge here; that belongs in the extension.
SYSTEM_PROMPT = (
    "You are a clinical data analyst assistant working inside BioRouter. "
    "You have access to tools for querying the UCSF OMOP electronic health record database. "
    "Answer the user's question by using the available tools. "
    "Be efficient: minimize the number of tool calls and wasted queries. "
    "When you have the answer, state it clearly and concisely, including the key number(s) "
    "and any important assumptions or caveats you made."
)

MAX_ITERS = 24
MIMO_TIMEOUT = 180


def _creds():
    """MiMo key from keychain; clinical creds via resilient db helper (auto-heals
    if the running app clobbers the keychain to 'dummy')."""
    import db
    blob = subprocess.check_output(
        ["security", "find-generic-password", "-s", "biorouter", "-a", "secrets", "-w"])
    d = json.loads(blob)
    u, p = db._creds()
    d["CLINICAL_RECORDS_USERNAME"] = u
    d["CLINICAL_RECORDS_PASSWORD"] = p
    return d


def _mcp_tools_to_openai(tools):
    out = []
    for t in tools:
        out.append({
            "type": "function",
            "function": {
                "name": t.name,
                "description": t.description or "",
                "parameters": t.inputSchema or {"type": "object", "properties": {}},
            },
        })
    return out


def _extract_text(result):
    """Pull text out of an MCP CallToolResult."""
    parts = []
    for c in result.content:
        if getattr(c, "type", None) == "text":
            parts.append(c.text)
        else:
            parts.append(str(c))
    txt = "\n".join(parts)
    if getattr(result, "isError", False):
        return "ERROR: " + txt
    return txt


async def run_question(question, model=MIMO_MODEL, max_iters=MAX_ITERS, verbose=False):
    """Run one question end-to-end. Returns a metrics dict."""
    creds = _creds()
    api_key = creds["XIAOMI_MIMO_API_KEY"]
    env = dict(os.environ)
    env["CLINICAL_RECORDS_USERNAME"] = creds["CLINICAL_RECORDS_USERNAME"]
    env["CLINICAL_RECORDS_PASSWORD"] = creds["CLINICAL_RECORDS_PASSWORD"]
    env["OMOP_LOG_LEVEL"] = "WARNING"

    params = StdioServerParameters(
        command="uv",
        args=["run", "--directory", EXT_DIR, "ucsfomopagent"],
        env=env,
    )

    rec = {
        "question": question, "model": model, "answer": None, "error": None,
        "iterations": 0, "tool_calls": 0, "tool_call_log": [],
        "prompt_tokens": 0, "completion_tokens": 0, "reasoning_tokens": 0, "total_tokens": 0,
        "tool_db_seconds": 0.0, "wall_seconds": 0.0, "tool_names": [],
        "server_instructions_len": 0, "n_tools": 0, "transcript": [],
    }
    t_start = time.time()

    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            instructions = (init.serverInfo and getattr(init, "instructions", None)) or getattr(init, "instructions", None) or ""
            rec["server_instructions_len"] = len(instructions or "")
            tools_resp = await session.list_tools()
            oai_tools = _mcp_tools_to_openai(tools_resp.tools)
            rec["n_tools"] = len(oai_tools)
            tool_index = {t.name: t for t in tools_resp.tools}

            sys_content = SYSTEM_PROMPT
            if instructions:
                sys_content += "\n\n# Extension instructions\n" + instructions

            messages = [
                {"role": "system", "content": sys_content},
                {"role": "user", "content": question},
            ]

            async with httpx.AsyncClient(timeout=MIMO_TIMEOUT) as client:
                for it in range(max_iters):
                    rec["iterations"] = it + 1
                    data = None
                    last_exc = None
                    for attempt in range(6):  # backoff on 429 / transient 5xx
                        try:
                            resp = await client.post(
                                f"{MIMO_HOST}/chat/completions",
                                headers={"Authorization": f"Bearer {api_key}"},
                                json={"model": model, "messages": messages,
                                      "tools": oai_tools, "tool_choice": "auto",
                                      "temperature": 0.1},
                            )
                            if resp.status_code in (429, 500, 502, 503, 529):
                                wait = min(2 ** attempt * 3, 60)
                                await asyncio.sleep(wait)
                                last_exc = f"{resp.status_code}"
                                continue
                            resp.raise_for_status()
                            data = resp.json()
                            break
                        except Exception as e:
                            last_exc = e
                            await asyncio.sleep(min(2 ** attempt * 3, 60))
                    if data is None:
                        rec["error"] = f"MiMo call failed at iter {it} after retries: {last_exc}"
                        break

                    usage = data.get("usage", {}) or {}
                    rec["prompt_tokens"] += usage.get("prompt_tokens", 0)
                    rec["completion_tokens"] += usage.get("completion_tokens", 0)
                    rec["reasoning_tokens"] += (usage.get("completion_tokens_details", {}) or {}).get("reasoning_tokens", 0)
                    rec["total_tokens"] += usage.get("total_tokens", 0)

                    choice = data["choices"][0]
                    msg = choice["message"]
                    finish = choice.get("finish_reason")

                    assistant_msg = {"role": "assistant", "content": msg.get("content")}
                    if msg.get("tool_calls"):
                        assistant_msg["tool_calls"] = msg["tool_calls"]
                    messages.append(assistant_msg)

                    if not msg.get("tool_calls"):
                        rec["answer"] = msg.get("content")
                        break

                    for tc in msg["tool_calls"]:
                        fname = tc["function"]["name"]
                        rec["tool_calls"] += 1
                        rec["tool_names"].append(fname)
                        try:
                            args = json.loads(tc["function"]["arguments"] or "{}")
                        except Exception:
                            args = {}
                        if fname not in tool_index:
                            tool_result_text = f"ERROR: unknown tool {fname}"
                        else:
                            tdb = time.time()
                            try:
                                r = await session.call_tool(fname, args)
                                tool_result_text = _extract_text(r)
                            except Exception as e:
                                tool_result_text = f"ERROR: tool execution failed: {e}"
                            rec["tool_db_seconds"] += time.time() - tdb
                        # log (truncate big payloads)
                        rec["tool_call_log"].append({
                            "tool": fname, "args": args,
                            "result_preview": tool_result_text[:500],
                            "result_len": len(tool_result_text),
                        })
                        if verbose:
                            print(f"  [{fname}] args={json.dumps(args)[:120]} -> {len(tool_result_text)} chars")
                        messages.append({
                            "role": "tool", "tool_call_id": tc["id"],
                            "content": tool_result_text[:20000],
                        })
                else:
                    rec["error"] = f"hit max_iters={max_iters} without final answer"

    rec["wall_seconds"] = time.time() - t_start
    rec["transcript"] = messages
    return rec


if __name__ == "__main__":
    q = sys.argv[1] if len(sys.argv) > 1 else "How many patients are in the database?"
    r = asyncio.run(run_question(q, verbose=True))
    print(json.dumps({k: v for k, v in r.items() if k != "transcript"}, indent=2, default=str))
    print("\nANSWER:\n", r["answer"])
