# OpenCode v2 Plugin for TokenEconomizer — Design Specification

**Date:** 2026-10-28  
**Target Host:** OpenCode v2 (`>= 2.1.2`, verified against local OpenCode `v2.2.0`)  
**Status:** Ready for User Approval (Builder Check Passed)

---

## 1. Intent and the Why

The user relies on OpenCode (primarily via the **TUI** and the **OpenChamber** GUI client) to run **spec-driven development workflows** (such as `superpowers`) that decompose a specification into tasks executed by multiple **sub-agents**. Across a full spec-to-implementation run, sessions frequently consume \$15–\$20 in token spend.

The main sources of token waste in this workflow are:
1. **Cold-starting sub-agents and sessions:** The primary agent and each spawned sub-agent start without a map of the repository's real file paths, symbols, or working test/build commands, burning turns on `glob`/`grep`/`read` and failed path guesses.
2. **Repetitive failure loops:** Agents and sub-agents repeatedly try non-existent file paths, failing shell commands, or broken edit cycles without stepping back.
3. **Lack of cross-session repository memory & cost visibility:** Learnings discovered in one session (or by a sub-agent) are lost when the session ends, and there is no cache-aware token accounting across main sessions and sub-agents per repository.

TokenEconomizer already solves these problems for **Claude Code**, **Codex**, **GitHub Copilot CLI**, and **Pi**. Adding first-class **OpenCode v2** (`opencode`) host support brings the exact same mechanisms (`repomap`, `microskills`, `loopguard`, `toolprune`, and `ledger` accounting) to OpenCode across both TUI and GUI clients.

---

## 2. Goals, Non-Goals, and Anti-Goals

### Goals
- **Full host parity for `opencode` (`OpenCodeHost`):** Support OpenCode v2 (`v2.2.0+`) as a first-class host in `tokeneconomizer/plugin/hosts.py` alongside `claude`, `codex`, `copilot`, and `pi`.
- **Client-agnostic server plugin:** Implement the OpenCode v2 server plugin (`Plugin.define({ id: "tokeneconomizer", setup(ctx) })`) so that every feature works identically whether the user interacts through the **OpenCode TUI** or **OpenChamber (GUI)**.
- **First-class Sub-agent Support:**
  - Inject the repository's frozen manual (`repomap` + `microskills`) into **both** root sessions and child sub-agent sessions (`parentID != null`) via `ctx.session.hook("context")`, eliminating cold-start path guessing in sub-agents.
  - Run `loopguard` (`PostToolUse` via `ctx.tool.hook("execute.after")`) on both root sessions and sub-agent sessions so sub-agents are redirected mid-turn when they loop on failing commands or missing files.
  - Roll up sub-agent trajectories and token usage into the root session at fold time so one spec-driven feature run folds as one cohesive session in the repository's learning state and cost ledger (matching Claude Code's `subagents/` behavior).
- **Zero-recursion Stateless Side Calls:** Support paid `microskills` proposer and `loopguard` stuck judge side calls via OpenCode (`opencode run --format json` / stateless generation) guarded by `TE_SIDECALL=1` so side calls never trigger plugin hooks recursively.
- **Native SQLite Transcript & Usage Reader:** Read OpenCode v2's SQLite database (`<XDG_DATA_HOME>/opencode/opencode.db`, tables `session_v2` and `session_message`) and `opencode session export` JSON using Python's standard-library `sqlite3` module.
- **Seamless CLI & `/te` Command Integration:** Support `/te` inside OpenCode (TUI and OpenChamber) and auto-detect OpenCode in `te` CLI commands when run inside OpenCode's shell tool (`OPENCODE=1`, `OPENCODE_SESSION_ID`).

### Non-Goals
- **OpenCode v1 legacy plugin API:** We will not target the deprecated v1 function-export plugin API (`experimental.chat.messages.transform`) that fails to load on OpenCode v2.2.0.
- **Custom TUI-only UI widgets:** We will not build TUI-specific React/Ink components that would be invisible in OpenChamber; all notices and commands use server-level session/command primitives visible in both TUI and GUI.

### Anti-Goals (What Would Make This a Failure Even If It Works)
- **Polluting or mutating persisted user prompts:** Never prepend the `repomap` or `microskills` manual onto the user's saved message text in `session_message` (which would bloat the visible chat history in OpenChamber/TUI). Context injection must go through `ctx.session.hook("context")` (`event.system.push(...)`).
- **Breaking prompt caching mid-session:** The injected manual for a session (and its sub-agents) must be frozen once at session start and reused identically on every step so provider prompt caches stay warm.
- **Counting every sub-agent as a separate session fold:** Spawning 15 sub-agents in one spec-driven workflow must not increment `sessions_folded` by 15 (which would prematurely decay `microskills` facts and trigger the paid proposer every few minutes).
- **Blocking or crashing OpenCode:** Every hook and Python bridge call must fail open (`{}` on any error or timeout, logging diagnostics to `~/.tokeneconomizer/errors.log`).
- **Adding third-party Python dependencies:** `tokeneconomizer/` must remain strictly Python >= 3.10 standard library only.

---

## 3. Constraints & Architectural Rules

From `CLAUDE.md` and the existing codebase architecture:
1. **Stdlib-only Python core:** `tokeneconomizer/` must use only the Python >= 3.10 standard library (`sqlite3`, `json`, `os`, `subprocess`, etc.).
2. **One-way dependency boundary:** Mechanism packages (`tokeneconomizer/microskills/`, `tokeneconomizer/toolprune/`, `tokeneconomizer/loopguard/`, `tokeneconomizer/trace/`) must never import `tokeneconomizer.plugin`. All OpenCode transcript/SQLite parsing lives in `tokeneconomizer/trace/opencode.py` and is re-exported via `tokeneconomizer/trace/__init__.py`.
3. **Fast, fail-open hooks:** The TypeScript plugin (`plugin/opencode/tokeneconomizer.ts`) shells out to `te-hook` with `--host opencode` and a hard timeout (`10_000 ms`), returning `{}` on any failure. Heavy work (`fold`, `repomap refresh`, `judge`) runs detached via `ctx.spawn`.
4. **Personal/Work Environment Isolation:** Development and verification use the local OpenCode v2.2.0 installation without requiring or touching external enterprise credentials.

---

## 4. Technical Design

### 4.1 Decisions Agreed With User vs. Builder Calls

**Agreed with User:**
- Target **OpenCode v2.2.0** server plugin architecture (`Plugin.define({ id, setup })`) so it works in both **TUI** and **OpenChamber**.
- Inject `repomap` + `microskills` via `ctx.session.hook("context")` (`event.system.push`) into **both main sessions and sub-agent sessions (`parentID`)**.
- Run `loopguard` on `ctx.tool.hook("execute.after")` for OpenCode v2's real tool names (`shell`, `read`, `edit`, `write`, `patch`, `glob`, `grep`, etc.) using `shell`'s integer `exit` code.
- Read sessions, sub-agents, and token usage from `opencode.db` (`session_v2` and `session_message`) and deliver notices via `ctx.session.synthetic({ resume: false })` plus `/te` command handling.

**Author / Sensible Default Calls (Explicitly Listed for Review):**
1. **Sub-agent Session Pinning & Rollup (Author Call):** Sub-agent sessions (`parentID != null`) get their own `TurnGuard` state during execution (so a stuck sub-agent is nudged without corrupting the parent session's tool counter) and receive the parent repository's frozen manual (`repomap` + `microskills`) in `context`, but only root sessions (`parentID == null`) schedule `fold_session`. When a root session folds, `trace/opencode.py` queries the root session **plus all descendant sessions (`parent_id`)** from `opencode.db` so all sub-agent tool calls and token costs are folded once into the repository's state and ledger.
2. **Per-Session Transcript Pointer File (Author Call):** OpenCode stores all sessions in a single SQLite file (`~/.local/share/opencode/opencode.db`). Because `fold.py` (`pending_sessions`, `last_activity`) and `repos.py` (`_transcript_cwd`, `_merge_host`) expect each session's `transcript` to be a path on disk with a per-session `mtime` and a readable `"cwd"` field, the OpenCode integration maintains a lightweight per-session pointer JSON file at `<state_root>/sessions/opencode-logs/<sessionID>.json` containing `{"session_id": "<id>", "parent_id": "<parentID>", "cwd": "<directory>", "db_path": "<path/to/opencode.db>"}` updated on session events. `trace/opencode.py` accepts this pointer JSON, a direct `.db` path (or `<db>#<sessionID>`), or an `opencode session export` JSON file.
3. **Runtime Tool Pruning in `context` Hook (Author Call):** OpenCode v2's `ctx.session.hook("context")` passes mutable `event.tools` (a map of tool definitions sent to the model) and `denylist` can store applied pruned tools per repo in `.opencode/te-denylist.json` (or `opencode.json`). For v1 parity with `pi`, `denylist.supports("opencode")` is `False` by default so built-in core tools (`shell`, `read`, `edit`, `write`, `patch`, `glob`, `grep`) are never accidentally stripped, while still recording `used_tools` in `trace/opencode.py` for future MCP server pruning.
4. **Packaging Layout (Author Call):** Keep `plugin/opencode/tokeneconomizer.ts` alongside `plugin/pi/tokeneconomizer.ts`, export `"./server": "./plugin/opencode/tokeneconomizer.ts"` and `"./opencode": "./plugin/opencode/tokeneconomizer.ts"` in both root `package.json` and `plugin/package.json`, and include `plugin/opencode/` automatically in `scripts/build_plugin.py`.

---

### 4.2 OpenCode v2.2.0 Server Plugin (`plugin/opencode/tokeneconomizer.ts`)

 verified against the local OpenCode `v2.2.0` binary (`PluginModule.load`):
- OpenCode resolves server plugins via `package.json` `exports["./server"]` or `exports["."]` (or a direct file path in `opencode.json` `"plugin": [...]`).
- The module exports a default object `{ id: "tokeneconomizer", setup(ctx) }` (compatible with `Plugin.define` without requiring runtime `@opencode-ai/plugin` installation).
- Guard at top of `setup(ctx)`: if `process.env.TE_SIDECALL === "1"`, return immediately so side calls do zero plugin work.

#### Hook & Event Wiring in `setup(ctx)`:
1. **Session State Tracking (`Map<string, SessionState>`):**
   - Tracks `sessionID`, `parentID`, `cwd` (`location?.directory || ctx.worktree || ctx.directory`), `manual` (frozen `additionalContext` string), `started` boolean, `firstPromptSeen` boolean, and `dbPath`.
   - Writes/touches the pointer file `<state_root>/sessions/opencode-logs/<sessionID>.json` whenever the session is created, receives a prompt, executes a tool, or finishes execution.

2. **`ctx.event.subscribe()` (Background Async Loop with Cleanup on Return):**
   - **`session.created`:**
     - Extracts `sessionID = info.sessionID`, `parentID = info.parentID ?? null`, `cwd = info.location?.directory || ctx.worktree || ctx.directory`.
     - For root sessions (`!parentID`): calls `te-hook --host opencode` with `hook_event_name: "SessionStart"`, caches `additionalContext` as the session's `manual`, and emits any `systemMessage` notice via `postNotice(ctx, sessionID, msg)`.
     - For child sub-agent sessions (`parentID`): inherits the parent session's cached `manual` (or calls `SessionStart` with `is_subagent: false` / reads the repo's frozen manual if the parent wasn't seen in memory) and initializes its own `TurnGuard` session state so `loopguard` protects the sub-agent's turn.
   - **`session.execution.succeeded` / `session.execution.failed` / `session.execution.interrupted`:**
     - Calls `te-hook --host opencode` with `hook_event_name: "Stop"` for `sessionID`. If `systemMessage` is returned (e.g., `still-stuck` notice), posts it via `postNotice`.
     - For root sessions (`!parentID`): also calls `te-hook --host opencode` with `hook_event_name: "SessionEnd"` so `LiveController` marks `ended` and spawns `te fold` detached (once per session, guarded by `folded.json`; if a user sends another prompt in the same root session later, `LiveController` continues tracking turns and `te fold --refold` / final fold updates the session). *Refinement:* To avoid premature mid-conversation folding while still folding when the user stops using the TUI/GUI, `session.deleted` and `session.archived` trigger `SessionEnd` immediately, while `session.execution.*` updates the pointer file `mtime` and calls `Stop` (and when running non-interactively via `opencode run`, or after `STALE_S = 600` of inactivity, or on `session.deleted`/`session.archived`/process exit cleanup, `SessionEnd` triggers `fold`). Additionally, cleanup returned by `setup(ctx)` triggers `SessionEnd` for all active root sessions when OpenCode exits.

3. **`ctx.session.hook("prompt", async (event) => ...)`:**
   - Runs when a user prompt is admitted (`{ sessionID, messageID, prompt: { text } }`).
   - Ensures `SessionStart` has run for `sessionID`.
   - Calls `te-hook --host opencode` with `hook_event_name: "UserPromptSubmit"` and `prompt: event.prompt.text`.
   - If `additionalContext` is returned (e.g., prompt-ranked `repomap` on the first prompt when `repomap=on`), appends it to the session's frozen `manual`.
   - If `systemMessage` is returned, posts it via `postNotice(ctx, event.sessionID, msg)`.

4. **`ctx.session.hook("context", async (event) => ...)`:**
   - Runs before each primary model call (`{ sessionID, agent, model, system, messages, tools }`).
   - Ensures `SessionStart` has run for `sessionID` (handling resumed sessions or sub-agents whose `session.created` fired before hook registration).
   - If the session (root or sub-agent) has a non-empty `manual`, pushes `{ type: "text", text: state.manual }` onto `event.system` (avoiding duplicate pushes if already present).

5. **`ctx.tool.hook("execute.after", async (event) => ...)`:**
   - Handles both `event.status === "completed"` and `event.status === "error"`.
   - Normalizes tool result text and exit status:
     - For `tool === "shell"` (or `"bash"`): `event.result?.output` in OpenCode v2.2.0 is `{ output: string, exit: number | null, signal: string | null, status: string }`. Extracts `exit_code = event.result.output.exit`, `output = event.result.output.output ?? event.result.content`, and `is_error = (event.status === "error") || (typeof exit_code === "number" && exit_code !== 0)`.
     - For all other tools (`read`, `edit`, `write`, `patch`, `glob`, `grep`, etc.): `is_error = (event.status === "error")`, `output = event.status === "error" ? String(event.error) : String(event.result?.content ?? event.result?.output ?? "")`.
   - Calls `te-hook --host opencode` with `hook_event_name: "PostToolUse"`, `tool_name: event.tool`, `tool_input: event.input`, `tool_response: { output, exit_code, is_error }`.
   - If `additionalContext` (a `loopguard` redirect nudge) is returned:
     - When `event.status === "completed"` and `typeof event.result?.content === "string"`, appends `\n\n${additionalContext}` to `event.result.content`.
     - Also injects via `ctx.session.synthetic({ sessionID: event.sessionID, text: additionalContext, resume: false })` when `event.status === "error"` (since error events have no mutable `result.content`).
   - If `systemMessage` is returned, posts it via `postNotice(ctx, event.sessionID, msg)`.

6. **Notices in TUI and OpenChamber (`postNotice`):**
   - Calls `ctx.session.synthetic({ sessionID, text: msg, description: msg, resume: false })`.
   - Verified in OpenCode v2.2.0's reducer (`Au`): a synthetic user message with `description` renders visibly in the timeline of both the TUI and OpenChamber without triggering a model turn (`resume: false`).

7. **Slash Command `/te` (`ctx.command.transform`):**
   - Registers `/te` in `ctx.command.transform` (`description: "Inspect and steer TokenEconomizer (status, microskills, repomap, config, doctor)"`).
   - Intercepts `/te ...` in `ctx.session.hook("prompt")` (or when invoked as a command), runs `te --host opencode --cwd <cwd> <args>`, and displays the output via `postNotice` so the user can inspect status/config directly inside TUI or OpenChamber.

---

### 4.3 Host Adapter (`tokeneconomizer/plugin/hosts.py`)

Add `OpenCodeHost(Host)` and register `"opencode": OpenCodeHost` in `HOSTS`:

- `name = "opencode"`
- `command_hint = "/te"`
- **Tool Name Mapping (`TOOL_MAP` & ` vocabulary`):**
  - OpenCode v2 tool names normalized for `TurnGuard` and `LiveAdapter`:
    - `"shell"` / `"bash"` -> `"bash"` (in `turn_event`) and `"shell"` in `bash_names`
    - `"read"` -> `"read"`
    - `"edit"` / `"write"` / `"patch"` / `"multiedit"` -> `"edit"` (in `turn_event`) and `edit_names = ("edit", "write", "patch", "multiedit")`
    - `"glob"` / `"grep"` / `"list"` -> `"search"`
  - `protected_tools`: `frozenset({"shell", "bash", "read", "edit", "write", "patch", "glob", "grep", "question", "skill", "subagent", "todo", "webfetch", "websearch"})`
- **Sub-agent Handling (`is_subagent`):**
  - `is_subagent(payload)` returns `bool(payload.get("is_subagent"))`.
  - Note: For sub-agents in OpenCode, the TS plugin passes `is_subagent: false` on `SessionStart` (or reuses the parent's manual) so sub-agents receive the manual and `TurnGuard` protection, and never calls `SessionEnd` on child sessions (`parentID != null`) so child sessions are not folded separately.
- **Environment Detection (`default_host` & `env_session_id`):**
  - OpenCode v2's `ShellTool` sets `OPENCODE="1"`, `AI_AGENT="opencode"`, and `OPENCODE_SESSION_ID="<sessionID>"`.
  - `OpenCodeHost.env_session_id(env)` returns `env.get("OPENCODE_SESSION_ID") or None`.
  - `default_host(env, recent=None)` checks `if env.get("OPENCODE") == "1" or env.get("OPENCODE_SESSION_ID"): return "opencode"`.
- **Transcript & Usage Delegation:**
  - `resolve_transcript(payload)`: returns `payload.get("transcript_path")` if present; otherwise resolves `<XDG_DATA_HOME>/opencode/opencode.db` and writes/updates the pointer file `<state_root>/sessions/opencode-logs/<session_id>.json`.
  - `mechanism_trace(transcript, workdir)`: calls `trace.opencode_session_to_claude(transcript, os.path.join(workdir, "trace.jsonl"))`.
  - `prompt_count(transcript)`: calls `trace.opencode_prompt_count(transcript)`.
  - `session_usage(transcript)`: calls `trace.opencode_session_usage(transcript)`.

---

### 4.4 Transcript & SQLite Parser (`tokeneconomizer/trace/opencode.py`)

Uses Python stdlib `sqlite3` and `json` only. Supports three input formats for `transcript_path`:
1. **Pointer JSON file** (`{"session_id": ..., "db_path": ..., "cwd": ...}` written by the plugin).
2. **`db_path#session_id`** string or direct `.db` SQLite file path.
3. **`opencode session export` JSON file** (`{"info": {...}, "messages": [{"info": {...}, "parts": [...]}]}`).

#### Key Functions:
1. **`opencode_session_to_claude(transcript_path, out_path)`:**
   - Queries `session_v2` for the root `session_id` and all descendant sub-agent sessions (`WITH RECURSIVE children AS (SELECT id, directory FROM session_v2 WHERE id = ? UNION ALL SELECT s.id, s.directory FROM session_v2 s JOIN children c ON s.parent_id = c.id)`).
   - Queries `session_message` rows (`ORDER BY time_created ASC, seq ASC`) for the root session and all descendant sub-agent sessions.
   - Extracts tool parts from `data` JSON (`part.type == "tool"`):
     - Tool name: `part["tool"]` (e.g. `"shell"`, `"read"`, `"edit"`, `"write"`, `"patch"`, `"glob"`, `"grep"`).
     - Tool call ID: `part.get("callID") or part.get("id")`.
     - Args: `part["state"]["input"]` (dict).
     - Result text & status from `part["state"]`:
       - `status == "completed"`: for `"shell"`, `output` may be in `state["output"]` (string) or `state["metadata"]["output"]`, and exit code in `state["metadata"]["exit"]`. `is_error = (exit_code not in (None, 0))`.
       - `status == "error"`: `output = state.get("error") or ""`, `is_error = True`.
     - Working directory (`cwd`): `args.get("workdir") or session_directory`.
   - Emits Claude-compatible `assistant` (`tool_use`) and `user` (`tool_result`) JSONL lines to `out_path` so `load_trajectory(out_path)` and `call_cwds(out_path)` work out of the box with zero changes to `microskills` or `toolprune`.

2. **`opencode_prompt_count(transcript_path)`:**
   - Counts root-session user messages (`data.role == "user"` or `type == "user"`) that are genuine user prompts (excluding synthetic/subtask/system messages where `part.synthetic == True`).

3. **`opencode_session_usage(transcript_path, include_subagents=True)`:**
   - Sums token usage across the root session and (when `include_subagents=True`) all descendant sub-agent sessions (`parent_id`):
     - Reads assistant messages (`role == "assistant"`) from `session_message` (or falls back to `session_v2` aggregate columns `tokens_input`, `tokens_output`, `tokens_cache_read`, `tokens_cache_write` if message-level tokens are absent):
       - `input_tokens`: sum of `tokens.input`
       - `cache_read_input_tokens`: sum of `tokens.cache.read`
       - `cache_creation_input_tokens`: sum of `tokens.cache.write`
       - `output_tokens`: sum of `tokens.output + tokens.reasoning`
       - `requests`: count of assistant messages carrying token usage (`tokens.total > 0` or `tokens.input > 0` or `tokens.output > 0`)
       - `model`: most frequent `modelID` (or `session_v2.model`) across assistant messages.

---

### 4.5 Side Calls (`tokeneconomizer/plugin/sidecall.py` & `cli.py`)

- Add `"opencode"` to `sidecall.runner_for(host, cfg)`:
  - Uses `opencode run --format json "<prompt>"` (with `--model <cfg.sidecall_model>` when configured) in a temporary directory with `TE_SIDECALL="1"` in the environment.
  - Parses the JSON event stream from `opencode run --format json` (extracting assistant text and `tokens` usage from `step_finish` / assistant message events for accurate `overhead` accounting in `ledger.jsonl`).
  - Handles auth/not-signed-in errors via `sidecall._pause("opencode", ...)` just like `claude`, `codex`, `copilot`, and `pi`.
- Update `tokeneconomizer/plugin/cli.py`:
  - Include `"opencode"` in `cmd_doctor` agent checks (`shutil.which("opencode")`, checking `~/.config/opencode/opencode.json` and SQLite db path).
  - Update CLI help strings to list `opencode` alongside `claude | codex | copilot | pi`.

---

## 5. What Is Left to the Builder

The builder has full discretion over:
- Internal helper naming and TypeScript type definitions inside `plugin/opencode/tokeneconomizer.ts` (keeping zero runtime npm dependencies so the file runs directly under OpenCode's Bun/Node loader).
- Exact SQL query batching and defensive JSON parsing inside `tokeneconomizer/trace/opencode.py` for variations between `session_message.data` shapes across OpenCode 2.1.x and 2.2.x.
- Test fixture construction in `tests/test_opencode_trace.py` and `tests/test_plugin_package.py` (creating in-memory/tempfile SQLite databases matching OpenCode v2.2.0's `session_v2` and `session_message` schema).
