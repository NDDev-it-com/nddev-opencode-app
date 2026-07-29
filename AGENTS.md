# nddev-opencode-app

This public repository contains only the OpenCode runtime setup manager,
public contracts, setup/profile sources, public documentation, and generic
public security workflow surfaces.

Keep private tests, benchmarks, evidence, memories, harness fixtures, and
validation slices outside this repository.

Use current OpenCode names and documented surfaces. The exact managed public
surface is owned by `setups/nddev-builder/setup.json` and
`build/manifest.json`; do not copy that ledger into prose.

The canonical setup is `setups/nddev-builder`. Runtime posture belongs in
`profiles/full-auto` and `profiles/safe`. Do not reintroduce legacy
`setups/safe`, `setups/balanced`, or `setups/full-auto` catalogs.

`AGENTS.md` is the canonical instruction file. `.claude/CLAUDE.md` only imports
this file for tools that understand Claude-style instruction bridges.

Do not introduce provider secrets, live user state, generated caches, runtime
logs, private harness paths, or test-only source overrides into public
artifacts.
