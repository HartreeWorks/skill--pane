---
name: pane
description: This skill should be used when the user asks to branch the current conversation into a fresh agent session in a new Warp pane — e.g. "/pane work on 1 and 2", "let's do (1) and (3), new panes", "start that as a new pane", "spawn a pane to draft X", or "branch this into a new pane". It composes a handoff prompt per task and launches Claude or Codex in each new Warp split pane. Sibling `tab` and `window` skills do the same with new Warp tabs or windows.
---

# Spawn agent sessions in new Warp panes

Branch the current conversation into one or more fresh agent sessions, each in its
own new Warp pane. Useful when a session (e.g. a morning briefing) ends with a menu
of next actions and the user wants to peel some off into parallel work without
losing the current thread.

The mechanism is bundled: `scripts/spawn_agent_panes.py` drives Warp via AppleScript
(split pane, type a self-cleaning runner path, press Enter, refocus the origin pane).
The skill's job is to translate the request into a good handoff prompt per task,
then call that script.

Requires Warp (macOS) with Accessibility permission granted, plus the `claude`
and/or `codex` CLI on `PATH`.

## Workflow

1. **Resolve the tasks.** If the request references list numbers ("1 and 2", "do 3"),
   map them to the most recent enumerated list in the conversation (e.g. a briefing's
   numbered options). Otherwise treat the free-text request as the task(s). If numbers
   are referenced but no enumerated list exists, ask which tasks rather than guessing.
   One pane is spawned per task.

2. **Read the modifiers** from the request:
   - **Agent**: default to the agent currently running this skill — Claude Code →
     `claude`, Codex → `codex`. Override only if the user says so ("with codex",
     "use claude"). The script also auto-detects via the `CLAUDECODE` env var, but
     set `agent` explicitly per task to be safe.
   - **Branch**: set `"branch": true` if the user says "branch" or "fork" — the new
     pane then forks the *current* session (shared history) instead of starting fresh.
     Branch mode for `claude` depends on `CLAUDE_CODE_SESSION_ID`; if it is unset the
     script falls back to a *fresh* session and prints a warning to stderr. Watch for
     that warning — if it fires, the new session has no context, so re-run that task
     in fresh mode with a full standalone prompt. Also note: if this skill is running
     inside a subagent, `CLAUDE_CODE_SESSION_ID` is the subagent's session, not the
     user's main thread — so branch would fork the wrong conversation. Only branch
     from the main session.
   - **Mode**: this skill uses `--mode pane`. (The `tab` skill sets `--mode tab`; the
     `window` skill sets `--mode window`.)
   - **Directory**: default to the current working directory — it is usually correct,
     and assuming it keeps the common case instant. Set `dir` only when the user
     explicitly names a project or path. (To make this smarter, add your own rule —
     e.g. resolve project names against a personal project→path map.)
   - **Model** (claude only): default `opus`; override if asked (e.g. `sonnet`).

3. **Compose the prompt per task** — this is the part only the running agent can do
   well, because it has the conversation context the new session lacks:
   - **Fresh mode** (default): write a strong *standalone* handoff. The new agent
     starts with zero context, so include everything it needs (see checklist below).
   - **Branch mode**: write a *terse directive* ("Now focus on: …"). The fork already
     carries the full history — do not restate context it already has.

4. **Write the manifest** to a temp JSON file and run the bundled script, resolving
   its path relative to this skill's own directory (`$SKILL_DIR` = the folder
   containing this SKILL.md):
   ```bash
   python3 "$SKILL_DIR/scripts/spawn_agent_panes.py" --mode pane /tmp/pane-manifest.json
   ```
   (The script also accepts the manifest on stdin via `-`.)

5. **Fire immediately** — do not ask for confirmation. After spawning, print a terse
   one-line summary per pane, **always including the resolved directory** so a wrong
   guess is caught at a glance, e.g.
   `Spawned 2 panes — [1] claude: refactor the auth module (~/code/myapp) · [2] claude (branch): write the migration (cwd)`.
   Exception: if the user says "show me first", "dry run", or similar, print the
   composed prompt(s) and the resolved agent/dir/mode and wait, instead of spawning.

## Manifest format

A JSON array, one object per task:

```json
[
  {"agent": "claude", "model": "opus", "dir": "/Users/you/code/myapp", "branch": false,
   "prompt": "<full standalone handoff prompt>"},
  {"agent": "claude", "branch": true,
   "prompt": "Now focus on: write integration tests for the payments module."}
]
```

Per-field defaults: `agent` ← `CLAUDECODE` env (`1`→claude, else codex); `model` ←
`opus` (claude only); `dir` ← current working directory; `branch` ← `false`. Only
`prompt` is required.

## Good handoff prompt checklist (fresh mode)

A fresh session has none of this conversation's context. Each prompt must stand alone
and include:
- **The specific task** — what to produce, concretely.
- **Relevant context and decisions** from this conversation — the "why", constraints,
  prior choices, and any key facts the user mentioned.
- **Pointers to files/paths** the new agent should read first (absolute paths).
- **A clear first action** so the agent starts productively rather than re-planning.

Keep it focused — enough to act, not a transcript dump.

## Notes

- Spawning uses Warp keystroke automation and needs Accessibility permission for Warp
  (System Settings > Privacy & Security > Accessibility). On failure the script prints
  a permission hint and exits non-zero — relay that to the user.
- The manifest temp file is not auto-deleted; write it with `mktemp` and it is harmless
  litter in `/tmp`. (The promptfile and runner self-delete once the pane launches.)
- Panes get cramped beyond ~3 in one window; for many tasks, use the `tab` or `window`
  variant instead.
- `dir` must be an absolute path. Never invoke an interactive directory picker — it
  hangs in a non-interactive agent context.
