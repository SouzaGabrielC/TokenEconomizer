# OpenCode v2 Plugin Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Implement a first-class OpenCode v2 (`v2.2.0+`) server plugin and host integration (`opencode`) for TokenEconomizer with full support for TUI, OpenChamber (GUI), and spec-driven sub-agent workflows.

**Architecture:** A zero-dependency TypeScript server plugin (`plugin/opencode/tokeneconomizer.ts`) hooks into OpenCode v2's `setup(ctx)` lifecycle (`ctx.event.subscribe`, `ctx.session.hook("prompt" | "context")`, `ctx.tool.hook("execute.after")`, `ctx.command.transform`) and bridges to the stdlib-only Python runtime (`te-hook --host opencode`). In Python, `OpenCodeHost` (`tokeneconomizer/plugin/hosts.py`), `tokeneconomizer/trace/opencode.py` (reading OpenCode's SQLite `opencode.db` and rolling up child sub-agent sessions), and `sidecall.opencode_call` (`tokeneconomizer/plugin/sidecall.py`) integrate OpenCode with `repomap`, `microskills`, `loopguard`, and `ledger`.

**Tech Stack:** Python >= 3.10 standard library (`sqlite3`, `json`, `subprocess`, `os`), TypeScript (Node.js / Bun native ESM without runtime npm dependencies), `pytest`.

**Spec:** `docs/superpowers/specs/2026-10-28-opencode-plugin-design.md`

## Global Constraints

- `tokeneconomizer/` must use Python >= 3.10 standard library only — zero third-party PyPI imports (`tests/test_plugin_package.py::test_the_package_is_stdlib_only_and_never_touches_the_benchmark_harness`).
- Mechanism packages (`tokeneconomizer/microskills/`, `tokeneconomizer/toolprune/`, `tokeneconomizer/loopguard/`, `tokeneconomizer/trace/`) must never import `tokeneconomizer.plugin` (`tests/test_plugin_package.py::test_the_mechanisms_never_import_the_plugin_runtime`).
- No `package-lock.json`, `npm-shrinkwrap.json`, `bun.lock`, or `bun.lockb` may exist in `plugin/` (`tests/test_plugin_package.py::test_the_plugin_directory_is_also_a_pi_package`).
- Every hook call and plugin callback must fail open (`{}` on any error or timeout, logging diagnostics to `~/.tokeneconomizer/errors.log`).
- Context injection (`repomap` + `microskills`) must go through `ctx.session.hook("context")` (`event.system.push`) on both root and sub-agent sessions, never mutating persisted user prompt messages.

## Review Focus

1. **Missing, empty, or locked SQLite `opencode.db` at fold time:** When `transcript_path` points to a non-existent `.db` file, an unrecorded `session_id`, or a database concurrently written by OpenCode, `opencode_session_to_claude`, `opencode_prompt_count`, and `opencode_session_usage` must return `None` / `0` / empty usage cleanly without raising `sqlite3.Error`. (Tested in Task 1)
2. **Recursive sub-agents (`parent_id` chains) and synthetic user messages:** When a root session spawns sub-agents that themselves spawn nested sub-agents or receive synthetic messages (`part.synthetic == True`), `opencode_prompt_count` must count only real human prompts on the root session, while `opencode_session_to_claude` and `opencode_session_usage` aggregate tool calls and token usage across the entire descendant tree without double-counting message tokens vs `session_v2` summary columns. (Tested in Task 1)
3. **OpenCode v2 `shell` non-zero exit codes vs tool exceptions:** In OpenCode v2.2.0, a failing shell command (`exit: 1`) completes with `status: "completed"` and `result.output.exit = 1`, while a tool error arrives with `status: "error"` and `event.error`. Both must normalize to `is_error = True` / non-zero `exit_code` so `TurnGuard` and `envfacts` detect failures. (Tested in Task 2 & Task 4)
4. **Resumed sessions or sub-agents entering `context` before `session.created` and multi-step turns:** If `ctx.session.hook("context")` runs for a session or sub-agent before `session.created` was seen, it must lazily initialize the session once and must not push duplicate copies of the frozen manual into `event.system` if `event.system` already contains it. (Tested in Task 4)
5. **Side-call recursion (`TE_SIDECALL=1`) and auth vs rate-limit handling on `opencode run`:** `TE_SIDECALL=1` must make `setup(ctx)` in `plugin/opencode/tokeneconomizer.ts` a no-op so `opencode run` never triggers recursive hooks, and `sidecall.opencode_call` must pause on auth errors while not pausing on transient `429` rate limits. (Tested in Task 3 & Task 4)

---

### Task 1: OpenCode v2 Trace & SQLite Reader (`tokeneconomizer/trace/opencode.py`)

**Files:**
- Create: `tokeneconomizer/trace/opencode.py`
- Modify: `tokeneconomizer/trace/__init__.py`
- Create: `tests/test_opencode_trace.py`

**Interfaces:**
- Consumes: Python stdlib `sqlite3`, `json`, `os`.
- Produces (in `tokeneconomizer/trace/opencode.py` and re-exported in `tokeneconomizer/trace/__init__.py`):
  - `opencode_session_header(transcript_path: str) -> dict` — returns `{"id": str, "cwd": str, "parent_id": str | None, "db_path": str | None}` (or `{}` if unreadable).
  - `opencode_session_to_claude(transcript_path: str, out_path: str) -> str | None` — writes Claude-shaped JSONL (`assistant` with `tool_use` and `cwd`, `user` with `tool_result` and `is_error`) for the root session and all descendant sub-agent sessions (`parent_id`), returning `out_path` or `None` if unreadable/empty.
  - `opencode_prompt_count(transcript_path: str) -> int` — counts non-synthetic user messages on the root session.
  - `opencode_session_usage(transcript_path: str, include_subagents: bool = True) -> dict` — returns `{"input_tokens": int, "cache_read_input_tokens": int, "cache_creation_input_tokens": int, "output_tokens": int, "cost_usd_vendor": float | None, "requests": int, "model": str | None, "files": int}`.
  - `opencode_json_reply(stdout: str) -> tuple[str, dict | None]` — parses `opencode run --format json` JSONL output into `(reply_text, usage_dict | None)`.

- [ ] **Step 1: Write the failing tests in `tests/test_opencode_trace.py`**

```python
"""OpenCode v2 SQLite, pointer JSON, and export JSON transcript readers."""
import json
import sqlite3
import pytest
from tokeneconomizer import trace


def make_opencode_db(db_path, sessions, messages):
    conn = sqlite3.connect(str(db_path))
    conn.execute("""
        CREATE TABLE session_v2 (
            id TEXT PRIMARY KEY, project_id TEXT, parent_id TEXT, directory TEXT,
            title TEXT, cost REAL, tokens_input INTEGER, tokens_output INTEGER,
            tokens_reasoning INTEGER, tokens_cache_read INTEGER, tokens_cache_write INTEGER,
            model TEXT, agent TEXT, time_created INTEGER, time_updated INTEGER,
            time_idle INTEGER, idle_outcome TEXT
        )
    """)
    conn.execute("""
        CREATE TABLE session_message (
            id TEXT PRIMARY KEY, session_id TEXT, type TEXT, seq INTEGER,
            time_created INTEGER, time_updated INTEGER, data TEXT
        )
    """)
    for s in sessions:
        conn.execute(
            "INSERT INTO session_v2 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (s["id"], s.get("project_id", "p1"), s.get("parent_id"), s.get("directory", "/w/repo"),
             s.get("title", ""), s.get("cost", 0.0), s.get("tokens_input", 0),
             s.get("tokens_output", 0), s.get("tokens_reasoning", 0),
             s.get("tokens_cache_read", 0), s.get("tokens_cache_write", 0),
             s.get("model", "claude-sonnet-4-5"), s.get("agent", "build"),
             s.get("time_created", 1000), s.get("time_updated", 2000), None, "succeeded")
        )
    for m in messages:
        conn.execute(
            "INSERT INTO session_message VALUES (?,?,?,?,?,?,?)",
            (m["id"], m["session_id"], m.get("type", m["data"].get("role", "assistant")),
             m["seq"], m.get("time_created", 1000 + m["seq"]),
             m.get("time_updated", 1000 + m["seq"]), json.dumps(m["data"]))
        )
    conn.commit()
    conn.close()
    return str(db_path)


def test_sqlite_pointer_reads_root_and_recursive_subagents(tmp_path):
    db = make_opencode_db(
        tmp_path / "opencode.db",
        sessions=[
            {"id": "ses_root", "parent_id": None, "directory": "/w/repo", "model": "claude-sonnet-4-5"},
            {"id": "ses_sub1", "parent_id": "ses_root", "directory": "/w/repo", "model": "claude-sonnet-4-5"},
            {"id": "ses_sub2", "parent_id": "ses_sub1", "directory": "/w/repo/sub", "model": "claude-sonnet-4-5"},
        ],
        messages=[
            {"id": "m1", "session_id": "ses_root", "seq": 1, "data": {
                "role": "user", "parts": [{"type": "text", "text": "implement feature X"}]}},
            {"id": "m2", "session_id": "ses_root", "seq": 2, "data": {
                "role": "user", "parts": [{"type": "text", "text": "notice", "synthetic": True}]}},
            {"id": "m3", "session_id": "ses_root", "seq": 3, "data": {
                "role": "assistant", "modelID": "claude-sonnet-4-5",
                "tokens": {"input": 100, "output": 40, "reasoning": 10,
                           "cache": {"read": 1000, "write": 200}},
                "cost": 0.01,
                "parts": [{"type": "tool", "callID": "c1", "tool": "shell", "state": {
                    "status": "completed", "input": {"command": "pytest -q"},
                    "output": "pytest: command not found", "metadata": {"exit": 127}}}]}},
            {"id": "m4", "session_id": "ses_sub1", "seq": 1, "data": {
                "role": "user", "parts": [{"type": "text", "text": "subagent prompt"}]}},
            {"id": "m5", "session_id": "ses_sub1", "seq": 2, "data": {
                "role": "assistant", "modelID": "claude-sonnet-4-5",
                "tokens": {"input": 50, "output": 20, "reasoning": 5,
                           "cache": {"read": 500, "write": 0}},
                "cost": 0.005,
                "parts": [{"type": "tool", "callID": "c2", "tool": "shell", "state": {
                    "status": "completed", "input": {"command": "python -m pytest -q"},
                    "output": "3 passed", "metadata": {"exit": 0}}}]}},
            {"id": "m6", "session_id": "ses_sub2", "seq": 1, "data": {
                "role": "assistant", "modelID": "claude-sonnet-4-5",
                "tokens": {"input": 25, "output": 10, "reasoning": 0,
                           "cache": {"read": 250, "write": 50}},
                "cost": 0.002,
                "parts": [{"type": "tool", "callID": "c3", "tool": "read", "state": {
                    "status": "error", "input": {"filePath": "/w/repo/missing.py"},
                    "error": "ENOENT: no such file"}}]}},
        ],
    )
    ptr = tmp_path / "ses_root.json"
    ptr.write_text(json.dumps({"session_id": "ses_root", "cwd": "/w/repo", "db_path": db}))

    hdr = trace.opencode_session_header(str(ptr))
    assert hdr["id"] == "ses_root" and hdr["cwd"] == "/w/repo"

    # Only the 1 genuine human prompt on ses_root is counted (not synthetic m2, not subagent m4)
    assert trace.opencode_prompt_count(str(ptr)) == 1

    out_jsonl = str(tmp_path / "claude.jsonl")
    assert trace.opencode_session_to_claude(str(ptr), out_jsonl) == out_jsonl
    calls = trace.load_trajectory(out_jsonl)
    assert [(name, ok) for name, _a, _r, ok in calls] == [
        ("shell", False), ("shell", True), ("read", False)
    ]
    assert trace.call_cwds(out_jsonl) == ["/w/repo", "/w/repo", "/w/repo/sub"]

    usage = trace.opencode_session_usage(str(ptr))
    assert usage["requests"] == 3
    assert usage["input_tokens"] == 175
    assert usage["cache_read_input_tokens"] == 1750
    assert usage["cache_creation_input_tokens"] == 250
    assert usage["output_tokens"] == (40 + 10) + (20 + 5) + 10
    assert usage["cost_usd_vendor"] == pytest.approx(0.017)
    assert usage["model"] == "claude-sonnet-4-5"


def test_missing_or_corrupt_inputs_fail_open(tmp_path):
    missing = str(tmp_path / "nope.json")
    assert trace.opencode_session_header(missing) == {}
    assert trace.opencode_prompt_count(missing) == 0
    assert trace.opencode_session_to_claude(missing, str(tmp_path / "out.jsonl")) is None
    assert trace.opencode_session_usage(missing)["requests"] == 0


def test_opencode_json_reply_parses_run_events():
    events = [
        {"type": "step_start", "sessionID": "ses_1"},
        {"type": "text", "part": {"type": "text", "text": "- use python -m pytest"}},
        {"type": "step_finish", "part": {
            "reason": "stop", "cost": 0.0012, "modelID": "claude-haiku-4-5",
            "tokens": {"input": 400, "output": 25, "reasoning": 5,
                       "cache": {"read": 100, "write": 0}}}},
    ]
    text, usage = trace.opencode_json_reply("\n".join(json.dumps(e) for e in events))
    assert text == "- use python -m pytest"
    assert usage == {
        "input_tokens": 400, "cache_read_input_tokens": 100,
        "cache_creation_input_tokens": 0, "output_tokens": 30,
        "cost_usd": 0.0012, "model": "claude-haiku-4-5"
    }
    assert trace.opencode_json_reply("") == ("", None)
```

- [ ] **Step 2: Run test to verify it fails**

Run: `pytest tests/test_opencode_trace.py -v` (or `python3 -m unittest` / `pytest` runner)
Expected: FAIL with `AttributeError: module 'tokeneconomizer.trace' has no attribute 'opencode_session_header'`

- [ ] **Step 3: Implement `tokeneconomizer/trace/opencode.py` and re-export in `tokeneconomizer/trace/__init__.py`**

Implement `tokeneconomizer/trace/opencode.py` supporting:
1. Pointer JSON files (`{"session_id": ..., "db_path": ..., "cwd": ...}`), `<db_path>#<session_id>` strings, direct `.db` paths (picking the single/latest root session if no `#session_id`), and `opencode session export` JSON files (`{"info": {...}, "messages": [...]}`).
2. SQLite connection with `timeout=5.0` and `WITH RECURSIVE` query over `session_v2` to collect `(id, directory, parent_id, model, cost, tokens_*)` for the root session and all descendant sub-agent sessions when `include_subagents=True`.
3. Extraction of `tool` parts (`part["type"] == "tool"` or `"tool-invocation"`) from `session_message.data` (`parts` array or top-level message parts), computing `is_error = (state.get("status") == "error") or (exit_code not in (None, 0))` where `exit_code = (state.get("metadata") or {}).get("exit")`, and writing Claude-compatible JSONL lines with `"cwd": args.get("workdir") or session_directory` to `out_path`.
4. Fallback in `opencode_session_usage` to `session_v2` aggregate token columns (`tokens_input`, `tokens_output + tokens_reasoning`, `tokens_cache_read`, `tokens_cache_write`, `cost`, `model`) when a session row has no assistant message-level token breakdown.
5. Export `opencode_session_header`, `opencode_session_to_claude`, `opencode_prompt_count`, `opencode_session_usage`, and `opencode_json_reply` in `tokeneconomizer/trace/__init__.py`.

- [ ] **Step 4: Run test to verify it passes**

Run: `pytest tests/test_opencode_trace.py tests/test_trace.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tokeneconomizer/trace/opencode.py tokeneconomizer/trace/__init__.py tests/test_opencode_trace.py
git commit -m "feat(trace): add OpenCode v2 SQLite and export transcript reader"
```

---

### Task 2: OpenCode Host Adapter & LiveAdapter Integration (`tokeneconomizer/plugin/hosts.py` & `adapter.py`)

**Files:**
- Modify: `tokeneconomizer/plugin/hosts.py`
- Modify: `tokeneconomizer/plugin/adapter.py`
- Modify: `tests/test_live_core.py`
- Modify: `tests/test_live_hooks.py`
- Modify: `tests/test_live_repos.py`

**Interfaces:**
- Consumes: `trace.opencode_session_header`, `trace.opencode_session_to_claude`, `trace.opencode_prompt_count`, `trace.opencode_session_usage` from Task 1.
- Produces:
  - `OpenCodeHost(Host)` in `tokeneconomizer/plugin/hosts.py` registered under `HOSTS["opencode"] = OpenCodeHost`.
  - `hosts.default_host(env, recent=None)` returning `"opencode"` when `env.get("OPENCODE") == "1"` or `env.get("OPENCODE_SESSION_ID")` is set.
  - `OpenCodeHost.env_session_id(env)` returning `env.get("OPENCODE_SESSION_ID") or None`.
  - `OpenCodeHost.tool_call(payload)` normalizing OpenCode's `shell` / `read` / `edit` / `write` / `patch` / `glob` / `grep` tool payloads (including `exit_code` and `is_error`).
  - `OpenCodeHost.resolve_transcript(payload)` returning `payload["transcript_path"]` or creating/updating `<state_root>/sessions/opencode-logs/<session_id>.json` pointing to `<XDG_DATA_HOME>/opencode/opencode.db`.
  - `LiveAdapter` (`tokeneconomizer/plugin/adapter.py`) supporting `host.name == "opencode"` (`bash_names = ("Bash", "bash", "shell", "exec_command")`, `edit_names` including `"patch"` and `"multiedit"`, and `protected_tools()` returning OpenCode's core tools).

- [ ] **Step 1: Write the failing tests in `tests/test_live_core.py`, `tests/test_live_hooks.py`, and `tests/test_live_repos.py`**

Add to `tests/test_live_core.py`:
```python
def test_the_opencode_host_reads_what_the_plugin_sends(tmp_path, monkeypatch):
    h = hosts.HOSTS["opencode"]()
    assert hosts.default_host({"OPENCODE": "1"}, recent="claude") == "opencode"
    assert hosts.default_host({"OPENCODE_SESSION_ID": "ses_1"}) == "opencode"
    assert h.env_session_id({"OPENCODE_SESSION_ID": "ses_1"}) == "ses_1"
    name, args, text, code = h.tool_call({
        "tool_name": "shell",
        "tool_input": {"command": "pytest -q", "workdir": "/w/repo"},
        "tool_response": {"output": "FAILED test_a.py", "exit_code": 1, "is_error": True},
    })
    assert (name, args["command"], code) == ("shell", "pytest -q", 1) and "FAILED" in text
    ev = h.turn_event("shell", {"command": "pytest -q"}, text, code)
    assert ev["tool"] == "bash" and ev["exit_code"] == 1
    ev_err = h.turn_event("read", {"filePath": "/w/missing.py"}, "ENOENT", None,
                          {"tool_response": {"output": "ENOENT", "is_error": True}})
    assert ev_err["tool"] == "read" and ev_err["status"] == "error"
    assert h.headless({"TE_OPENCODE_MODE": "run"}) and not h.headless({})
    assert hosts.detect({"session_id": "ses_1"}, hint="opencode").name == "opencode"
```
And include `"opencode"` in `@pytest.mark.parametrize("host", ["claude", "codex", "copilot", "pi", "opencode"])` in `tests/test_live_hooks.py::test_the_guard_redirects_then_tells_the_person_how_to_stop_on_every_agent` (with `_stuck("opencode")` using `"edit"` and `"shell"`), and in `AGENTS` in `tests/test_live_repos.py`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_live_core.py -k opencode -v`
Expected: FAIL with `KeyError: 'opencode'`

- [ ] **Step 3: Implement `OpenCodeHost` in `tokeneconomizer/plugin/hosts.py` and update `tokeneconomizer/plugin/adapter.py`**

In `tokeneconomizer/plugin/hosts.py`:
- Add `OpenCodeHost(Host)` with:
  - `name = "opencode"`, `command_hint = "/te"`
  - `TOOL_MAP = {"shell": "bash", "bash": "bash", "read": "read", "edit": "edit", "write": "edit", "patch": "edit", "multiedit": "edit", "glob": "search", "grep": "search", "list": "search"}`
  - `headless(self, env)`: `env.get("TE_OPENCODE_MODE") == "run"`
  - `stop_hint(self, headless)`: `'Press Esc to interrupt the turn, or run `/te config set loopguard=off`.'` (interactive) and `'Stop the process (Ctrl-C) or run `te config set loopguard=off`.'` (headless)
  - `env_session_id(self, env)`: `env.get("OPENCODE_SESSION_ID") or None`
  - `is_subagent(self, payload)`: `bool((payload or {}).get("is_subagent"))`
  - `resolve_transcript(self, payload)`: if `payload.get("transcript_path")` is given, return it; otherwise write/update `<state_root>/sessions/opencode-logs/<session_id>.json` with `{"session_id": sid, "parent_id": payload.get("parent_id"), "cwd": payload.get("cwd"), "db_path": payload.get("db_path") or default_opencode_db()}` and return its path.
  - `tool_call(self, payload)` and `turn_event(...)`: extract `output`, `exit_code`, and `is_error` from `tool_response`, mapping `filePath` -> `file_path` for `TurnGuard` target extraction when needed.
- Register `"opencode": OpenCodeHost` in `HOSTS` and update `default_host(env, recent=None)` to check `if env.get("OPENCODE") == "1" or env.get("OPENCODE_SESSION_ID"): return "opencode"`.
- In `tokeneconomizer/plugin/adapter.py`:
  - Update `bash_names` to `("Bash", "bash", "shell", "exec_command")` and `edit_names` to `("Edit", "Write", "MultiEdit", "NotebookEdit", "edit", "write", "patch", "multiedit", "apply_patch")`.
  - Add `"filePath"` to `path_keys` in `Vocabulary` (`("file_path", "filePath", "path", "notebook_path")`) so OpenCode's `read`/`edit`/`write` (`input.filePath`) are recognized by `microskills` and `loopguard`.
  - In `LiveAdapter.protected_tools()`, return OpenCode's core tools when `self.host.name == "opencode"`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_live_core.py tests/test_live_hooks.py tests/test_live_repos.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tokeneconomizer/plugin/hosts.py tokeneconomizer/plugin/adapter.py tests/test_live_core.py tests/test_live_hooks.py tests/test_live_repos.py
git commit -m "feat(plugin): add OpenCodeHost and LiveAdapter tool vocabulary"
```

---

### Task 3: OpenCode Stateless Side Calls & CLI / Doctor Support (`tokeneconomizer/plugin/sidecall.py` & `cli.py`)

**Files:**
- Modify: `tokeneconomizer/plugin/sidecall.py`
- Modify: `tokeneconomizer/plugin/cli.py`
- Modify: `tests/conftest.py`
- Modify: `tests/test_live_core.py`
- Modify: `tests/test_live_hooks.py`

**Interfaces:**
- Consumes: `trace.opencode_json_reply` from Task 1, `OpenCodeHost` from Task 2.
- Produces:
  - `sidecall.opencode_call(prompt: str, model: str | None = None, timeout: int = TIMEOUT_S, cli: str = "opencode", cwd: str | None = None) -> tuple[str, float | None]`
  - `sidecall.runner_for("opencode", cfg)` returning a runner backed by `opencode_call`.
  - `cmd_doctor` in `tokeneconomizer/plugin/cli.py` reporting `opencode` CLI presence, plugin registration in `~/.config/opencode/opencode.json`, and side-call pause status.

- [ ] **Step 1: Write the failing tests in `tests/test_live_core.py` and `tests/test_live_hooks.py`**

Add to `tests/test_live_core.py`:
```python
@pytest.mark.real_sidecalls
def test_an_opencode_side_call_runs_with_te_sidecall_guard(monkeypatch):
    import subprocess
    from tokeneconomizer.plugin import sidecall
    calls = []
    events = [
        {"type": "text", "part": {"type": "text", "text": "- use python -m pytest"}},
        {"type": "step_finish", "part": {
            "reason": "stop", "cost": 0.001, "modelID": "claude-haiku-4-5",
            "tokens": {"input": 300, "output": 20, "reasoning": 0,
                       "cache": {"read": 0, "write": 0}}}},
    ]

    def fake_run(cmd, **kw):
        calls.append((cmd, kw))
        return subprocess.CompletedProcess(cmd, 0, stdout="\n".join(json.dumps(e) for e in events), stderr="")

    monkeypatch.setattr(sidecall.subprocess, "run", fake_run)
    text, eff = sidecall.opencode_call("propose facts")
    assert text == "- use python -m pytest" and eff and eff > 0
    cmd, kw = calls[0]
    assert cmd[:3] == ["opencode", "run", "--format"] and cmd[3] == "json"
    assert cmd[-2:] == ["--", "propose facts"]
    assert kw["env"]["TE_SIDECALL"] == "1" and kw["stdin"] is subprocess.DEVNULL
    calls.clear()
    sidecall.opencode_call("propose facts", model="anthropic/claude-haiku-4-5")
    assert "--model" in calls[0][0] and "anthropic/claude-haiku-4-5" in calls[0][0]
```
And in `tests/test_live_hooks.py`, verify `sidecall._no_reply("opencode", ...)` pauses on auth failure (`"not logged in"` / `"authentication"`) and does not pause on `429 rate limited`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_live_core.py -k opencode_side_call -v`
Expected: FAIL with `AttributeError: module 'tokeneconomizer.plugin.sidecall' has no attribute 'opencode_call'`

- [ ] **Step 3: Implement `opencode_call` in `tokeneconomizer/plugin/sidecall.py`, update `tests/conftest.py`, and update `tokeneconomizer/plugin/cli.py`**

1. In `tokeneconomizer/plugin/sidecall.py`:
   - Add `opencode_call(prompt, model=None, timeout=TIMEOUT_S, cli="opencode", cwd=None)` running `[cli, "run", "--format", "json"] + (["--model", model] if model else []) + ["--", prompt]` in a temporary directory with `TE_SIDECALL="1"`, parsing output via `trace.opencode_json_reply`, and computing effective tokens via `accounting.effective(usage)[0]`.
   - Wire `if host == "opencode":` in `runner_for(host, cfg)`.
2. In `tests/conftest.py`:
   - Add `"opencode_call"` to the monkeypatched sidecall tuple in `_no_model_calls`.
3. In `tokeneconomizer/plugin/cli.py`:
   - Add `"opencode"` to the `agents` check in `cmd_doctor` and inspect `~/.config/opencode/opencode.json` (or `$OPENCODE_CONFIG_DIR/opencode.json`) to report whether `tokeneconomizer` is configured.
   - Update module docstring host list to `(claude | codex | copilot | opencode | pi)`.

- [ ] **Step 4: Run tests to verify they pass**

Run: `pytest tests/test_live_core.py tests/test_live_hooks.py -v`
Expected: PASS

- [ ] **Step 5: Commit**

```bash
git add tokeneconomizer/plugin/sidecall.py tokeneconomizer/plugin/cli.py tests/conftest.py tests/test_live_core.py tests/test_live_hooks.py
git commit -m "feat(sidecall,cli): add OpenCode sidecall runner and doctor check"
```

---

### Task 4: OpenCode v2 Server Plugin (`plugin/opencode/tokeneconomizer.ts`), Package Exports & Docs

**Files:**
- Create: `plugin/opencode/tokeneconomizer.ts`
- Modify: `package.json`
- Modify: `plugin/package.json`
- Modify: `tests/test_plugin_package.py`
- Modify: `README.md`
- Modify: `docs/PLUGIN.md`

**Interfaces:**
- Consumes: `plugin/scripts/te-hook` (`--host opencode`) and `plugin/scripts/te` (`--host opencode`).
- Produces:
  - `plugin/opencode/tokeneconomizer.ts` exporting default `{ id: "tokeneconomizer", setup(ctx) }` and named helper exports (`extractToolOutcome`, `injectSystemManual`) for direct unit testing under Node `--experimental-strip-types`.
  - `exports` in `package.json` (`"./server": "./plugin/opencode/tokeneconomizer.ts"`, `"./opencode": "./plugin/opencode/tokeneconomizer.ts"`) and `plugin/package.json` (`"./server": "./opencode/tokeneconomizer.ts"`, `"./opencode": "./opencode/tokeneconomizer.ts"`).

- [ ] **Step 1: Write the failing packaging and TypeScript plugin tests in `tests/test_plugin_package.py`**

Add to `tests/test_plugin_package.py`:
1. `test_the_plugin_directory_is_also_an_opencode_v2_plugin()`: verifies `package.json` and `plugin/package.json` `exports["./server"]` and `exports["./opencode"]` point to the same `tokeneconomizer.ts` file and updates `test_the_plugin_directory_is_also_a_pi_package` to compare keys excluding `("pi", "exports")`.
2. `test_opencode_plugin_lifecycle_subagents_and_loopguard(tmp_path)`: runs `plugin/opencode/tokeneconomizer.ts` under `node --experimental-strip-types` with a mock OpenCode v2 `ctx`, verifying:
   - `TE_SIDECALL=1` makes `setup(ctx)` return without registering any hooks.
   - Root session (`session.created` with `parentID: undefined`) and sub-agent session (`session.created` with `parentID: "ses_root"`) both receive the frozen manual in `ctx.session.hook("context")` (`event.system`), and calling `context` multiple times in the same turn never duplicates the manual block in `event.system`.
   - `ctx.tool.hook("execute.after")` handles both `status: "completed"` with `result.output.exit = 1` (`shell`) and `status: "error"` (`read`), forwarding `exit_code` and `is_error` to `te-hook --host opencode`.
   - `/te status` in `ctx.session.hook("prompt")` runs `te --host opencode` and emits a synthetic notice via `ctx.session.synthetic({ resume: false })`.

- [ ] **Step 2: Run tests to verify they fail**

Run: `pytest tests/test_plugin_package.py -v`
Expected: FAIL because `plugin/opencode/tokeneconomizer.ts` and `exports` in `package.json` do not exist yet.

- [ ] **Step 3: Implement `plugin/opencode/tokeneconomizer.ts`, update `package.json` / `plugin/package.json`, and document OpenCode in `README.md` & `docs/PLUGIN.md`**

1. Create `plugin/opencode/tokeneconomizer.ts`:
   - Default export `{ id: "tokeneconomizer", async setup(ctx) }`.
   - Short-circuit at start of `setup(ctx)` if `process.env.TE_SIDECALL === "1"`.
   - Maintain per-session state (`cwd`, `parentID`, `manual`, `started`, `pointerPath`) and write `<state_root>/sessions/opencode-logs/<sessionID>.json` so `fold.py` and `repos.py` have per-session `mtime` and `cwd`.
   - Subscribe to `ctx.event.subscribe()` for `session.created`, `session.execution.succeeded`, `session.execution.failed`, `session.execution.interrupted`, `session.deleted`, `session.archived`.
   - Register `ctx.session.hook("prompt")`, `ctx.session.hook("context")`, `ctx.tool.hook("execute.after")`, and `ctx.command.transform`.
   - In `context`, lazily initialize sessions if `session.created` was not seen (e.g. resumed session or sub-agent), inherit the parent session's/repo's manual for sub-agents (`parentID != null`), and push `{ type: "text", text: manual }` onto `event.system` only if not already present.
   - Only trigger `SessionEnd` (`te fold`) for root sessions (`!parentID`) so sub-agents are rolled up into the root session fold.
2. Update `package.json` and `plugin/package.json` with `"exports": { "./server": ..., "./opencode": ... }` and `"opencode-plugin"` in `keywords`.
3. Add OpenCode v2 setup and usage instructions (including `opencode.json` `"plugin"` configuration for both TUI and OpenChamber) to `README.md` and `docs/PLUGIN.md`.

- [ ] **Step 4: Run the full test suite and bundle verification**

Run: `pytest -q`
Expected: All tests PASS (including `test_the_bundle_runs_without_a_checkout`, `test_the_mechanisms_never_import_the_plugin_runtime`, `test_the_package_is_stdlib_only_and_never_touches_the_benchmark_harness`, and the new OpenCode trace, host, sidecall, and plugin tests).

- [ ] **Step 5: Commit**

```bash
git add plugin/opencode/tokeneconomizer.ts package.json plugin/package.json tests/test_plugin_package.py README.md docs/PLUGIN.md
git commit -m "feat(opencode): add OpenCode v2 server plugin, package exports, and docs"
```
