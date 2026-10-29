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
            {"id": "ses_root", "parent_id": None, "directory": "/w/repo", "model": "claude-sonnet-4-5", "time_created": 1000},
            {"id": "ses_sub1", "parent_id": "ses_root", "directory": "/w/repo", "model": "claude-sonnet-4-5", "time_created": 2000},
            {"id": "ses_sub2", "parent_id": "ses_sub1", "directory": "/w/repo/sub", "model": "claude-sonnet-4-5", "time_created": 3000},
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
    for bad in (None, "", missing):
        assert trace.opencode_session_header(bad) == {}
        assert trace.opencode_prompt_count(bad) == 0
        assert trace.opencode_session_to_claude(bad, str(tmp_path / "out.jsonl")) is None
        assert trace.opencode_session_usage(bad)["requests"] == 0


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
