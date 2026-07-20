---
name: pane
description: This skill should be used when the user asks to branch the current conversation into a fresh agent session in a new Warp pane — e.g. "/pane work on 1 and 2", "let's do (1) and (3), new panes", "start that as a new pane", "spawn a pane to draft X", or "branch this into a new pane". It composes a handoff prompt per task and launches Claude or Codex in each new Warp split pane. The sibling `tab` and `window` skills are identical but open new Warp tabs or windows.
---

# Spawn agent sessions in new Warp panes

Branch the current conversation into one or more fresh agent sessions, each in its
own new Warp pane. Useful when a session (e.g. a morning briefing) ends with a menu
of next actions and the user wants to peel some off into parallel work without
losing the current thread.

The mechanism is two-phase. `scripts/spawn_agent_panes.py reserve` immediately opens
the requested Warp destinations, starts a short waiter in each, and returns focus to
the origin. The agent then composes the handoffs without touching Warp. `fulfil`
atomically publishes each completed launch payload; the waiter claims and executes it.

## Workflow

1. **Resolve the tasks.** If the request references list numbers ("1 and 2", "do 3"),
   map them to the most recent enumerated list in the conversation (e.g. the
   briefing's numbered options). Otherwise treat the free-text request as the task(s).
   If numbers are referenced but no enumerated list exists in the conversation, ask
   which tasks rather than guessing. One pane is spawned per task.

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
   - **Directory**: default to the current working directory — do not infer or look
     anything up (this keeps the common case instant). Two exceptions: if the user
     explicitly names a project/path, use that; and if cwd is `plans-and-reviews`,
     infer the target project (see "Inferring the directory" below).
   - **Model** (claude only): default `opus`; override if asked (e.g. `sonnet`).

3. **Handle previews before reserving.** If the user says "show me first", "dry run",
   or similar, compose and show the proposed prompts and resolved settings, then wait.
   Do not create empty Warp destinations for a preview.

4. **Reserve immediately.** Once the task count, mode, and any directory confidence
   question are resolved, reserve before composing the full handoffs:
   ```bash
   python3 ~/.claude/skills/pane/scripts/spawn_agent_panes.py reserve --mode pane --count 2
   ```
   Record the reservation directory from the JSON printed on stdout. This command
   foregrounds Warp briefly, opens one destination per task, types a short waiter,
   returns focus after each opening, and succeeds only after every waiter reports
   `waiting`. Do not continue to handoff composition if reservation fails.

5. **Compose the prompt per task** — this is the part only the running agent can do
   well, because it has the conversation context the new session lacks:
   - **Fresh mode** (default): write a strong *standalone* handoff. The new agent
     starts with zero context, so include everything it needs (see checklist below).
   - **Branch mode**: write a *terse directive* ("Now focus on: …"). The fork already
     carries the full history — do not restate context it already has.

6. **Write the manifest and fulfil the reservation**:
   ```bash
   python3 ~/.claude/skills/pane/scripts/spawn_agent_panes.py fulfil /tmp/pane-reservation.ABC123 /tmp/pane-manifest.json
   ```
   The second path may be `-` to read the manifest from stdin. `fulfil` does not
   activate Warp or send keystrokes. It validates every task first, writes each
   payloads, then publishes them to the waiting sessions.

7. **Report the launches.** Print a terse one-line summary per pane, **always including the
   resolved directory** so a wrong guess is caught at a glance, e.g.
   `Spawned 2 panes — [1] claude: Client strategy doc (~/Documents/Projects/client-strategy) · [2] claude (branch): crux questions for Alex (cwd)`.

If work cannot reach `fulfil` after a successful reservation, release the waiters:
```bash
python3 ~/.claude/skills/pane/scripts/spawn_agent_panes.py cancel /tmp/pane-reservation.ABC123
```
Do not cancel after fulfilment begins. Waiters otherwise time out after 10 minutes.

## Inferring the directory

The default is the current working directory, with no lookup — keep it that way in
almost every context. Inference happens in only two cases:

1. **The user explicitly names a project or path.** Use it. If it is a project name
   ("the T3A repo", "AI Wow"), resolve it via `~/.claude/references/project-map.md`,
   picking by task type when a project has several paths (an AI Wow *blog post* → the
   website dir `~/Documents/www/AI Wow/wow.pjh.is`, not the strategy dir).
2. **cwd is `plans-and-reviews`.** Planning there is usually *about* a project whose
   work lives elsewhere, so invert the default: infer the target project from the task
   via the project map and set `dir` to its repo. Only this context triggers inference.

**Confidence gate — flag before handoff if unsure.** Whenever you resolve a `dir` by
inference (not an explicit user-named path), judge how confident you are:
- **Confident** (the task clearly maps to one project): best-guess and show — proceed
  without asking. The resolved directory appears in the summary, so a wrong guess is
  easy to spot and re-run.
- **Unsure** (the task maps to no clear project, or is ambiguous between several):
  **stop and ask which project/directory before spawning.** Do not silently guess a
  directory you are not confident about — a handoff into the wrong repo wastes a whole
  fresh session. Name your best candidate(s) in the question so the user can confirm with
  one word.

If a named project is absent from the map, fall back to cwd — never invent a path.
`dir` must always be an absolute, existing directory.

## Manifest format

A JSON array, one object per task:

```json
[
  {"agent": "claude", "model": "opus", "dir": "~/Documents/Projects/infra", "branch": false,
   "prompt": "<full standalone handoff prompt>"},
  {"agent": "claude", "branch": true,
   "prompt": "Now focus on: draft my crux questions for Alex for Monday."}
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

- Reservation activates Warp, verifies it is frontmost, opens every destination from
  the currently active origin, and immediately returns focus after each opening.
- Reserve before lengthy prompt composition. This narrows the remaining targeting
  race to the short interval between the user's request and the reserve command; later
  tab, window, or app changes cannot affect already-opened destinations.
- Reservation verifies that all waiters started. A display-sleep or input-suppression
  failure therefore returns non-zero instead of reporting an unverified spawn.
- Spawning uses Warp keystroke automation and needs Accessibility permission for Warp
  (System Settings > Privacy & Security > Accessibility). On failure the script prints
  a permission hint and exits non-zero — relay that to the user.
- Reservation and manifest files live in `/tmp`. Payload and prompt files self-clean
  when their agent exits; small state files may remain until macOS clears `/tmp`.
- Panes get cramped beyond ~3 in one window; for many tasks, suggest the `tab` or
  `window` skill.
- `dir` must be an absolute path. Never invoke the interactive project picker
  (`coding_agent_launcher.py`) — it hangs in a non-interactive agent context.
