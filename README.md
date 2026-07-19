# pane

A Claude Code skill that branches the current conversation into a fresh agent session (Claude or Codex) in a new Warp split pane, seeded with a handoff prompt composed from the conversation. Sibling `tab` and `window` variants do the same in new Warp tabs or windows. Supports forking the current session (`--fork-session` / `codex fork`) so the new pane inherits full history.

## Documentation

See [SKILL.md](./SKILL.md) for complete documentation and usage instructions.

## Installation

```bash
# Run install
npx skills add HartreeWorks/skill--pane
```

Requires Warp (macOS) with Accessibility permission, and the `claude` and/or `codex` CLI on your `PATH`.

## About

Created by [Peter Hartree](https://x.com/peterhartree). For updates, follow [AI Wow](https://wow.pjh.is), my AI uplift newsletter.

Find more skills at [HartreeWorks/skills](https://github.com/HartreeWorks/skills).
