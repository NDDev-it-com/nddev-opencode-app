---
name: nddev-builder
description: Build, review, and validate NDDev AI-tool setup artifacts using native OpenCode context.
license: AGPL-3.0-or-later
compatibility: opencode
metadata:
  owner: nddev-opencode-app
  projection: native
---

# NDDev Builder

Use this skill when creating or reviewing NDDev setup modules, OpenCode native
agents, skills, plugins, or public contract artifacts. Keep implementation
changes in public module boundaries and private tests in the owning validation
slice.

Prefer current OpenCode surfaces:

- `opencode.json` for configuration and permissions.
- `plugins/*.js` or `plugins/*.ts` for local plugin hooks.
- `skills/<name>/SKILL.md` for reusable instructions.
- `agents/<name>.md` for native subagents.
