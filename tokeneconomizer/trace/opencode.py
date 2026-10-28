"""OpenCode v2 session trace reader: SQLite DB, pointer JSON, and session export JSON.

Reads OpenCode v2's SQLite database (`~/.local/share/opencode/opencode.db`), pointer JSON files
pointing to a session in the DB, and `opencode session export` JSON files.
Rolls up descendant sub-agents (`parent_id`) into their root session.
"""
import json
import os
import sqlite3
from typing import Any, Dict, List, Optional, Tuple


def _default_opencode_db() -> str:
    xdg_data = os.environ.get("XDG_DATA_HOME") or os.path.expanduser("~/.local/share")
    return os.path.join(xdg_data, "opencode", "opencode.db")


def _resolve_session_source(path: str) -> Tuple[Optional[str], Optional[str], Optional[str], Optional[dict]]:
    """(db_path, session_id, cwd, export_json_dict)"""
    if not path or not os.path.exists(path):
        if "#" in path:
            db_p, sid = path.split("#", 1)
            if os.path.exists(db_p):
                return db_p, sid, None, None
        return None, None, None, None

    if path.endswith((".json", ".jsonl")):
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                content = fh.read().strip()
            if content.startswith("{"):
                try:
                    data = json.loads(content)
                except ValueError:
                    # Could be first line of JSONL
                    data = json.loads(content.splitlines()[0])
                if isinstance(data, dict):
                    if "session_id" in data and ("db_path" in data or os.path.exists(_default_opencode_db())):
                        db = data.get("db_path") or _default_opencode_db()
                        return db, data.get("session_id"), data.get("cwd"), None
                    if "messages" in data and "info" in data:
                        info = data.get("info") or {}
                        return None, info.get("id"), info.get("directory"), data
        except (OSError, ValueError):
            return None, None, None, None

    if path.endswith(".db"):
        return path, None, None, None

    return None, None, None, None


def opencode_session_header(transcript_path: str) -> Dict[str, Any]:
    """Header information for an OpenCode session: id, cwd, parent_id, db_path."""
    db_path, sid, cwd, export = _resolve_session_source(transcript_path)
    if export:
        info = export.get("info") or {}
        return {
            "id": info.get("id") or "",
            "cwd": info.get("directory") or cwd or "",
            "parent_id": info.get("parentID") or info.get("parent_id"),
            "db_path": None,
        }

    if not db_path or not os.path.exists(db_path):
        return {}

    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
        except sqlite3.Error:
            return {}

    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if sid:
            row = cur.execute("SELECT id, directory, parent_id FROM session_v2 WHERE id = ?", (sid,)).fetchone()
        else:
            row = cur.execute(
                "SELECT id, directory, parent_id FROM session_v2 ORDER BY time_updated DESC LIMIT 1"
            ).fetchone()
        if not row:
            return {}
        return {
            "id": row["id"],
            "cwd": row["directory"] or cwd or "",
            "parent_id": row["parent_id"],
            "db_path": db_path,
        }
    except sqlite3.Error:
        return {}
    finally:
        conn.close()


def _query_sessions_and_messages_sqlite(
    db_path: str, root_id: Optional[str], include_subagents: bool = True
) -> Tuple[List[dict], List[dict]]:
    """Query session metadata and messages in temporal sequence."""
    if not os.path.exists(db_path):
        return [], []

    try:
        conn = sqlite3.connect(f"file:{os.path.abspath(db_path)}?mode=ro", uri=True, timeout=5.0)
    except sqlite3.Error:
        try:
            conn = sqlite3.connect(str(db_path), timeout=5.0)
        except sqlite3.Error:
            return [], []

    try:
        conn.row_factory = sqlite3.Row
        cur = conn.cursor()
        if not root_id:
            row = cur.execute("SELECT id FROM session_v2 ORDER BY time_updated DESC LIMIT 1").fetchone()
            if not row:
                return [], []
            root_id = row["id"]

        if include_subagents:
            query = """
                WITH RECURSIVE tree AS (
                    SELECT id, parent_id, directory, model, cost,
                           tokens_input, tokens_output, tokens_reasoning,
                           tokens_cache_read, tokens_cache_write, time_created
                    FROM session_v2 WHERE id = ?
                    UNION ALL
                    SELECT s.id, s.parent_id, s.directory, s.model, s.cost,
                           s.tokens_input, s.tokens_output, s.tokens_reasoning,
                           s.tokens_cache_read, s.tokens_cache_write, s.time_created
                    FROM session_v2 s JOIN tree t ON s.parent_id = t.id
                )
                SELECT * FROM tree
            """
            s_rows = cur.execute(query, (root_id,)).fetchall()
        else:
            s_rows = cur.execute(
                "SELECT id, parent_id, directory, model, cost, tokens_input, tokens_output, "
                "tokens_reasoning, tokens_cache_read, tokens_cache_write, time_created "
                "FROM session_v2 WHERE id = ?",
                (root_id,),
            ).fetchall()

        sessions = [dict(r) for r in s_rows]
        if not sessions:
            return [], []

        sids = [s["id"] for s in sessions]
        placeholders = ",".join("?" for _ in sids)
        m_rows = cur.execute(
            f"SELECT m.id, m.session_id, m.type, m.seq, m.time_created, m.data FROM session_message m "
            f"JOIN session_v2 s ON m.session_id = s.id "
            f"WHERE m.session_id IN ({placeholders}) "
            f"ORDER BY s.time_created ASC, m.time_created ASC, m.seq ASC",
            sids,
        ).fetchall()

        messages = []
        for mr in m_rows:
            d = dict(mr)
            raw_data = d.get("data")
            if isinstance(raw_data, str):
                try:
                    d["data"] = json.loads(raw_data)
                except ValueError:
                    d["data"] = {}
            messages.append(d)

        return sessions, messages
    except sqlite3.Error:
        return [], []
    finally:
        conn.close()


def opencode_prompt_count(transcript_path: str) -> int:
    """Count non-synthetic user prompts on the root session."""
    db_path, sid, cwd, export = _resolve_session_source(transcript_path)
    if export:
        count = 0
        for m in export.get("messages") or []:
            info = m.get("info") or {}
            if info.get("role") != "user":
                continue
            parts = m.get("parts") or []
            if any(p.get("synthetic") for p in parts if isinstance(p, dict)):
                continue
            count += 1
        return count

    if not db_path:
        return 0

    sessions, messages = _query_sessions_and_messages_sqlite(db_path, sid, include_subagents=False)
    if not sessions:
        return 0
    root_id = sessions[0]["id"]

    count = 0
    for m in messages:
        if m.get("session_id") != root_id:
            continue
        data = m.get("data") or {}
        role = data.get("role") or m.get("type")
        if role != "user":
            continue
        parts = data.get("parts") or []
        if any(isinstance(p, dict) and p.get("synthetic") for p in parts):
            continue
        count += 1

    return count


def opencode_session_to_claude(transcript_path: str, out_path: str) -> Optional[str]:
    """Convert OpenCode tool invocations to Claude-shaped JSONL format.

    Each assistant entry carries `tool_use` blocks and `cwd`.
    Each user entry carries the corresponding `tool_result` blocks and `is_error`.
    """
    db_path, sid, cwd, export = _resolve_session_source(transcript_path)

    sessions_by_id = {}
    messages = []
    if export:
        info = export.get("info") or {}
        root_id = info.get("id") or "export"
        sessions_by_id[root_id] = {"directory": info.get("directory") or cwd or ""}
        for m in export.get("messages") or []:
            minfo = m.get("info") or {}
            messages.append({
                "id": minfo.get("id"),
                "session_id": root_id,
                "data": {
                    "role": minfo.get("role"),
                    "parts": m.get("parts") or [],
                },
            })
    elif db_path:
        s_list, messages = _query_sessions_and_messages_sqlite(db_path, sid, include_subagents=True)
        if not s_list:
            return None
        for s in s_list:
            sessions_by_id[s["id"]] = s
    else:
        return None

    lines = []
    call_seq = 0
    for m in messages:
        data = m.get("data") or {}
        role = data.get("role") or m.get("type")
        if role != "assistant":
            continue

        sess = sessions_by_id.get(m.get("session_id"), {})
        sess_cwd = sess.get("directory") or cwd or ""

        parts = data.get("parts") or []
        for p in parts:
            if not isinstance(p, dict):
                continue
            ptype = p.get("type")
            if ptype not in ("tool", "tool-invocation"):
                continue

            tool_name = p.get("tool") or ""
            call_seq += 1
            call_id = p.get("callID") or p.get("id") or f"c_{m.get('id')}_{call_seq}"

            state = p.get("state") or {}
            tool_input = state.get("input") or {}
            workdir = tool_input.get("workdir") if isinstance(tool_input, dict) else None
            entry_cwd = workdir or sess_cwd

            output_text = state.get("output")
            if output_text is None:
                output_text = state.get("error") or ""
            if not isinstance(output_text, str):
                output_text = json.dumps(output_text)

            meta = state.get("metadata") or {}
            exit_code = meta.get("exit")
            is_error = (state.get("status") == "error") or (
                exit_code is not None and exit_code != 0
            )

            assistant_entry = {
                "type": "assistant",
                "cwd": entry_cwd,
                "message": {
                    "role": "assistant",
                    "content": [
                        {
                            "type": "tool_use",
                            "id": call_id,
                            "name": tool_name,
                            "input": tool_input,
                        }
                    ],
                },
            }
            user_entry = {
                "type": "user",
                "message": {
                    "role": "user",
                    "content": [
                        {
                            "type": "tool_result",
                            "tool_use_id": call_id,
                            "content": output_text,
                            "is_error": is_error,
                        }
                    ],
                },
            }
            lines.append(assistant_entry)
            lines.append(user_entry)

    if not lines:
        return None

    os.makedirs(os.path.dirname(os.path.abspath(out_path)), exist_ok=True)
    with open(out_path, "w", encoding="utf-8") as fh:
        for entry in lines:
            fh.write(json.dumps(entry) + "\n")

    return out_path


def opencode_session_usage(transcript_path: str, include_subagents: bool = True) -> Dict[str, Any]:
    """Token usage and request statistics, including descendant sub-agents when requested."""
    out = {
        "input_tokens": 0,
        "cache_read_input_tokens": 0,
        "cache_creation_input_tokens": 0,
        "output_tokens": 0,
        "cost_usd_vendor": None,
        "requests": 0,
        "model": None,
        "files": 1,
    }

    db_path, sid, cwd, export = _resolve_session_source(transcript_path)
    if export:
        models = {}
        for m in export.get("messages") or []:
            minfo = m.get("info") or {}
            if minfo.get("role") != "assistant":
                continue
            tokens = minfo.get("tokens") or {}
            if not tokens:
                continue
            out["requests"] += 1
            out["input_tokens"] += int(tokens.get("input") or 0)
            cache = tokens.get("cache") or {}
            out["cache_read_input_tokens"] += int(cache.get("read") or 0)
            out["cache_creation_input_tokens"] += int(cache.get("write") or 0)
            out["output_tokens"] += int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
            m_cost = minfo.get("cost")
            if m_cost is not None:
                out["cost_usd_vendor"] = (out["cost_usd_vendor"] or 0.0) + float(m_cost)
            mod = minfo.get("modelID") or minfo.get("model")
            if mod:
                models[mod] = models.get(mod, 0) + 1
        if models:
            out["model"] = max(models, key=models.get)
        return out

    if not db_path:
        return out

    sessions, messages = _query_sessions_and_messages_sqlite(
        db_path, sid, include_subagents=include_subagents
    )
    if not sessions:
        return out

    models = {}
    message_usage_found = False

    for m in messages:
        data = m.get("data") or {}
        role = data.get("role") or m.get("type")
        if role != "assistant":
            continue
        tokens = data.get("tokens")
        if not isinstance(tokens, dict):
            continue

        inp = int(tokens.get("input") or 0)
        outp = int(tokens.get("output") or 0) + int(tokens.get("reasoning") or 0)
        cache = tokens.get("cache") or {}
        cr = int(cache.get("read") or 0)
        cw = int(cache.get("write") or 0)

        if inp or outp or cr or cw:
            message_usage_found = True
            out["requests"] += 1
            out["input_tokens"] += inp
            out["cache_read_input_tokens"] += cr
            out["cache_creation_input_tokens"] += cw
            out["output_tokens"] += outp

            cost = data.get("cost")
            if cost is not None:
                out["cost_usd_vendor"] = (out["cost_usd_vendor"] or 0.0) + float(cost)

            mod = data.get("modelID") or data.get("model")
            if mod:
                models[mod] = models.get(mod, 0) + 1

    if not message_usage_found:
        # Fall back to aggregate columns in session_v2
        for s in sessions:
            inp = int(s.get("tokens_input") or 0)
            outp = int(s.get("tokens_output") or 0) + int(s.get("tokens_reasoning") or 0)
            cr = int(s.get("tokens_cache_read") or 0)
            cw = int(s.get("tokens_cache_write") or 0)
            if inp or outp or cr or cw:
                out["requests"] += 1
                out["input_tokens"] += inp
                out["cache_read_input_tokens"] += cr
                out["cache_creation_input_tokens"] += cw
                out["output_tokens"] += outp
                scost = s.get("cost")
                if scost is not None:
                    out["cost_usd_vendor"] = (out["cost_usd_vendor"] or 0.0) + float(scost)
                mod = s.get("model")
                if mod:
                    models[mod] = models.get(mod, 0) + 1

    if models:
        out["model"] = max(models, key=models.get)
    elif sessions and sessions[0].get("model"):
        out["model"] = sessions[0]["model"]

    return out


def opencode_json_reply(stdout: str) -> Tuple[str, Optional[Dict[str, Any]]]:
    """Parse `opencode run --format json` stdout into reply text and usage dictionary."""
    if not stdout or not stdout.strip():
        return "", None

    texts = []
    usage = None
    for line in stdout.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if not isinstance(ev, dict):
            continue

        etype = ev.get("type")
        part = ev.get("part") or {}

        if etype == "text":
            txt = part.get("text")
            if isinstance(txt, str):
                texts.append(txt)
        elif etype == "step_finish":
            toks = part.get("tokens") or {}
            cache = toks.get("cache") or {}
            usage = {
                "input_tokens": int(toks.get("input") or 0),
                "cache_read_input_tokens": int(cache.get("read") or 0),
                "cache_creation_input_tokens": int(cache.get("write") or 0),
                "output_tokens": int(toks.get("output") or 0) + int(toks.get("reasoning") or 0),
                "cost_usd": part.get("cost"),
                "model": part.get("modelID") or part.get("model"),
            }

    return "".join(texts), usage
