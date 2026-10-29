/**
 * TokenEconomizer for OpenCode v2.2.0+.
 *
 * OpenCode server plugin:
 * - Injects frozen repository context (repomap + microskills) via `ctx.session.hook("context")`
 *   into both root sessions and child sub-agent sessions (`parentID`).
 * - Protects agent execution loops via `ctx.tool.hook("execute.after")` for OpenCode tools
 *   (`shell`, `read`, `edit`, `write`, `patch`, `glob`, `grep`, etc.), normalizing `shell`'s
 *   exit code and errors.
 * - Posts visible timeline notices to TUI and OpenChamber via `ctx.session.synthetic({ resume: false })`.
 * - Handles `/te` slash command directly inside chat.
 * - Records SQLite traces and aggregates sub-agent sessions into root session folding.
 */
import { spawn, spawnSync } from "node:child_process";
import { dirname, join } from "node:path";
import { fileURLToPath } from "node:url";

const HERE = (() => {
  try {
    return dirname(fileURLToPath(import.meta.url));
  } catch {
    return __dirname;
  }
})();
const SCRIPTS = join(HERE, "..", "scripts");
const HOOK_TIMEOUT_MS = 60_000;

const truthy = (v: string | undefined) =>
  ["1", "true", "yes", "on"].includes((v ?? "").trim().toLowerCase());

/** Detect whether OpenCode is running in one-shot `run` mode or interactive `tui`/server mode. */
function mode(): "run" | "tui" {
  const env = (process.env.TE_OPENCODE_MODE ?? "").trim().toLowerCase();
  if (env === "run" || env === "tui") return env;
  return process.argv.includes("run") ? "run" : "tui";
}

function launch(script: string, args: string[]): [string, string[]] {
  if (process.platform === "win32") {
    const modeArg = script === "te-hook" ? "hook" : "cli";
    return ["python", [join(SCRIPTS, "te_bootstrap.py"), modeArg, ...args]];
  }
  return [join(SCRIPTS, script), args];
}

interface SessionState {
  sessionID: string;
  parentID: string | null;
  cwd: string;
  manual: string;
  started: boolean;
  ended: boolean;
  turn: string;
  prompts: number;
}

/** Run one of the plugin's launchers with `input` on stdin; resolve to its stdout. */
function run(
  script: string,
  args: string[],
  input: string,
  extra: Record<string, string> = {},
): Promise<string> {
  return new Promise((resolve) => {
    let out = "";
    const [cmd, cmdArgs] = launch(script, args);
    let child;
    try {
      child = spawn(cmd, cmdArgs, {
        env: {
          ...process.env,
          PYTHONUTF8: "1",
          TE_OPENCODE_MODE: mode(),
          ...extra,
        },
        stdio: ["pipe", "pipe", "ignore"],
      });
    } catch {
      resolve("");
      return;
    }
    const timer = setTimeout(() => {
      child.kill("SIGKILL");
      resolve("");
    }, HOOK_TIMEOUT_MS);
    child.stdout.on("data", (d: Buffer) => (out += d.toString()));
    child.on("error", () => {
      clearTimeout(timer);
      resolve("");
    });
    child.on("close", () => {
      clearTimeout(timer);
      resolve(out);
    });
    child.stdin.on("error", () => {});
    child.stdin.end(input);
  });
}

/** One dispatcher call; `{}` (no change) on any failure. */
async function hook(
  payload: Record<string, unknown>,
  extra: Record<string, string> = {},
): Promise<any> {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return {};
  const out = await run("te-hook", ["--host", "opencode"], JSON.stringify(payload), extra);
  try {
    return JSON.parse(out.trim().split("\n").pop() || "{}") ?? {};
  } catch {
    return {};
  }
}

/** Synchronous dispatcher call for `process.once("exit")`, where async promises will not settle. */
function hookSync(
  payload: Record<string, unknown>,
  extra: Record<string, string> = {},
): any {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return {};
  const [cmd, cmdArgs] = launch("te-hook", ["--host", "opencode"]);
  try {
    const res = spawnSync(cmd, cmdArgs, {
      input: JSON.stringify(payload),
      encoding: "utf8",
      timeout: HOOK_TIMEOUT_MS,
      env: {
        ...process.env,
        PYTHONUTF8: "1",
        TE_OPENCODE_MODE: mode(),
        ...extra,
      },
      stdio: ["pipe", "pipe", "ignore"],
    });
    return JSON.parse((res.stdout || "").trim().split("\n").pop() || "{}") ?? {};
  } catch {
    return {};
  }
}

/** Post a notice to OpenCode UI (TUI / OpenChamber) via synthetic message or stderr. */
export function postNotice(ctx: any, sessionID: string, text: unknown) {
  if (typeof text !== "string" || !text.trim()) return;
  try {
    if (typeof ctx?.session?.synthetic === "function") {
      ctx.session
        .synthetic({
          sessionID,
          text,
          description: text,
          resume: false,
        })
        ?.catch?.(() => {});
    } else {
      console.error(text);
    }
  } catch {
    console.error(text);
  }
}

/** Normalize tool result text and error status across OpenCode v2 tools. */
export function extractToolOutcome(
  event: any,
): { output: string; exit_code?: number; is_error: boolean } {
  const tool = String(event?.tool || "").toLowerCase();
  const status = event?.status;

  if (tool === "shell" || tool === "bash") {
    const rawOut = event?.result?.output;
    let text = "";
    let exitCode: number | undefined;

    if (rawOut && typeof rawOut === "object") {
      text = rawOut.output ?? rawOut.content ?? "";
      if (typeof rawOut.exit === "number") {
        exitCode = rawOut.exit;
      }
    } else {
      text = String(event?.result?.content ?? rawOut ?? "");
    }
    if (exitCode === undefined && typeof event?.result?.metadata?.exit === "number") {
      exitCode = event.result.metadata.exit;
    }

    const isError =
      status === "error" || (typeof exitCode === "number" && exitCode !== 0);
    return {
      output: typeof text === "string" ? text : String(text),
      exit_code: exitCode,
      is_error: isError,
    };
  }

  const isError = status === "error";
  const text = isError
    ? String(event?.error ?? "")
    : String(event?.result?.content ?? event?.result?.output ?? "");

  return {
    output: text,
    exit_code: undefined,
    is_error: isError,
  };
}

/** Inject repository manual into event.system idempotently. */
export function injectSystemManual(event: any, manual: string): boolean {
  if (!manual || !event || !Array.isArray(event.system)) return false;
  const already = event.system.some(
    (entry: any) =>
      entry?.text &&
      (entry.text === manual ||
        entry.text.includes("<tokeneconomizer>") ||
        entry.text.includes(manual.slice(0, 40))),
  );
  if (already) return false;
  event.system.push({ type: "text", text: manual });
  return true;
}

/** OpenCode v2 server plugin setup function. */
export async function setup(ctx: any) {
  if (truthy(process.env.TE_SIDECALL) || truthy(process.env.TE_DISABLE)) return;

  const sessions = new Map<string, SessionState>();

  const getOrCreateSession = (
    sid: string,
    parentID: string | null = null,
    dir?: string,
  ): SessionState => {
    let s = sessions.get(sid);
    if (!s) {
      const cwd = dir || ctx.worktree || ctx.directory || process.cwd();
      s = {
        sessionID: sid,
        parentID: parentID,
        cwd,
        manual: "",
        started: false,
        ended: false,
        turn: `${sid}:0`,
        prompts: 0,
      };
      sessions.set(sid, s);
    } else if (parentID !== null && s.parentID === null) {
      s.parentID = parentID;
    }
    return s;
  };

  const endSession = async (sid: string, reason: string) => {
    const state = sessions.get(sid);
    if (!state || state.parentID || state.ended) return;
    state.ended = true;
    await hook({
      session_id: sid,
      parent_id: null,
      cwd: state.cwd,
      hook_event_name: "SessionEnd",
      reason,
    });
  };

  const endSessionSync = (sid: string, reason: string) => {
    const state = sessions.get(sid);
    if (!state || state.parentID || state.ended) return;
    state.ended = true;
    hookSync({
      session_id: sid,
      parent_id: null,
      cwd: state.cwd,
      hook_event_name: "SessionEnd",
      reason,
    });
  };

  process.once("exit", () => {
    for (const sid of sessions.keys()) {
      endSessionSync(sid, "exit");
    }
  });

  if (ctx.event?.subscribe) {
    ctx.event.subscribe(async (event: any) => {
      const type = event?.type;
      const sid =
        event?.sessionID || event?.id || event?.info?.id || event?.info?.sessionID;
      if (!sid) return;

      if (type === "session.created") {
        const parentID = event.parentID ?? event.info?.parentID ?? null;
        const cwd =
          event.location?.directory ||
          event.info?.directory ||
          ctx.worktree ||
          ctx.directory ||
          process.cwd();
        const state = getOrCreateSession(sid, parentID, cwd);

        if (!parentID) {
          const out = await hook({
            session_id: sid,
            parent_id: null,
            cwd,
            hook_event_name: "SessionStart",
            source: "startup",
          });
          state.manual = out?.hookSpecificOutput?.additionalContext ?? "";
          state.started = true;
          postNotice(ctx, sid, out?.systemMessage);
        } else {
          const parentState = sessions.get(parentID);
          if (parentState && parentState.manual) {
            state.manual = parentState.manual;
            state.started = true;
          } else {
            const out = await hook({
              session_id: sid,
              parent_id: parentID,
              cwd,
              hook_event_name: "SessionStart",
              source: "startup",
              is_subagent: false,
            });
            state.manual = out?.hookSpecificOutput?.additionalContext ?? "";
            state.started = true;
            postNotice(ctx, sid, out?.systemMessage);
          }
        }
      } else if (
        type === "session.execution.succeeded" ||
        type === "session.execution.failed" ||
        type === "session.execution.interrupted"
      ) {
        const state = sessions.get(sid);
        const cwd = state?.cwd || ctx.worktree || ctx.directory || process.cwd();
        const turn = state?.turn || `${sid}:1`;
        const out = await hook({
          session_id: sid,
          parent_id: state?.parentID ?? null,
          cwd,
          hook_event_name: "Stop",
          prompt_id: turn,
          stop_hook_active: false,
        });
        postNotice(ctx, sid, out?.systemMessage);
        if (mode() === "run" && (!state || !state.parentID)) {
          await endSession(sid, type);
        }
      } else if (type === "session.deleted" || type === "session.archived") {
        await endSession(sid, type);
        sessions.delete(sid);
      }
    });
  }

  if (ctx.session?.hook) {
    ctx.session.hook("prompt", async (event: any) => {
      const sid = event?.sessionID;
      if (!sid) return;
      const state = getOrCreateSession(sid);
      const text =
        event?.prompt?.text ??
        (typeof event?.prompt === "string" ? event.prompt : "");

      if (text.startsWith("/te")) {
        const argv = text.slice(3).trim().split(/\s+/).filter(Boolean);
        const out = await run(
          "te",
          [
            "--host",
            "opencode",
            "--cwd",
            state.cwd,
            ...(argv.length ? argv : ["status"]),
          ],
          "",
          { TE_SESSION_ID: sid },
        );
        postNotice(
          ctx,
          sid,
          out.trim() || "te: no output (is Python 3.10+ on PATH?)",
        );
        return;
      }

      state.prompts += 1;
      state.turn = `${sid}:${state.prompts}`;

      if (!state.started) {
        const startOut = await hook({
          session_id: sid,
          parent_id: state.parentID,
          cwd: state.cwd,
          hook_event_name: "SessionStart",
          source: "startup",
          is_subagent: false,
        });
        state.manual = startOut?.hookSpecificOutput?.additionalContext ?? "";
        state.started = true;
        postNotice(ctx, sid, startOut?.systemMessage);
      }

      const out = await hook({
        session_id: sid,
        parent_id: state.parentID,
        cwd: state.cwd,
        hook_event_name: "UserPromptSubmit",
        prompt_id: state.turn,
        prompt: text,
      });

      const extra = out?.hookSpecificOutput?.additionalContext;
      if (extra) {
        state.manual = state.manual ? `${state.manual}\n\n${extra}` : extra;
      }
      postNotice(ctx, sid, out?.systemMessage);
    });

    ctx.session.hook("context", async (event: any) => {
      const sid = event?.sessionID;
      if (!sid) return;
      const state = getOrCreateSession(sid);

      if (!state.started) {
        const startOut = await hook({
          session_id: sid,
          parent_id: state.parentID,
          cwd: state.cwd,
          hook_event_name: "SessionStart",
          source: "startup",
          is_subagent: false,
        });
        state.manual = startOut?.hookSpecificOutput?.additionalContext ?? "";
        state.started = true;
        postNotice(ctx, sid, startOut?.systemMessage);
      }

      if (state.manual) {
        injectSystemManual(event, state.manual);
      }
    });
  }

  if (ctx.tool?.hook) {
    ctx.tool.hook("execute.after", async (event: any) => {
      const sid = event?.sessionID;
      if (!sid) return;
      const state = getOrCreateSession(sid);
      const outcome = extractToolOutcome(event);
      const turn = state.turn || `${sid}:1`;

      const out = await hook({
        session_id: sid,
        parent_id: state.parentID,
        cwd: state.cwd,
        hook_event_name: "PostToolUse",
        prompt_id: turn,
        tool_name: event.tool,
        tool_use_id: event.callID || event.id || "",
        tool_input: event.input ?? {},
        tool_response: {
          output: outcome.output,
          content: outcome.output,
          is_error: outcome.is_error,
          isError: outcome.is_error,
          exit_code: outcome.exit_code,
        },
      });

      const nudge = out?.hookSpecificOutput?.additionalContext;
      if (nudge) {
        if (event.result && typeof event.result.content === "string") {
          event.result.content += `\n\n${nudge}`;
        } else if (event.result && Array.isArray(event.result.content)) {
          event.result.content.push({ type: "text", text: `\n\n${nudge}` });
        }
        postNotice(ctx, sid, nudge);
      }
      postNotice(ctx, sid, out?.systemMessage);
    });
  }

  if (ctx.command?.transform) {
    ctx.command.transform({
      name: "te",
      description:
        "TokenEconomizer: status | microskills | config | repos (what it learned here, what it did)",
    });
  }

  return async () => {
    for (const sid of sessions.keys()) {
      await endSession(sid, "shutdown");
    }
  };
}

const tokeneconomizer = {
  id: "tokeneconomizer",
  setup,
};

export default tokeneconomizer;
