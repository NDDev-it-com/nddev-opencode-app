# Changelog

## [0.3.0] - 2026-08-09

- Added the capability-negotiated public provider protocol v3 with exact
  HarnessBundle validation, pure planning, exact-digest application, status,
  backup, restore, and ownership-scoped removal.
- Preserve exact component/setup/bundle/plan provenance while retaining the
  native `opencode.json`, plugin, skill, agent, and command projections; MCP
  remains explicitly unsupported by this manager.
- Add durable crash recovery plus fail-closed JSON, skill-marker, ownership,
  and projection-kind validation before any target mutation.

## Unreleased

## [0.2.4] - 2026-08-08

- Update OpenCode from `1.18.14` to stable `1.18.15` and synchronize exact
  GitHub release identities for all six supported macOS and Ubuntu assets.
- Preserve the native setup/profile and lifecycle contracts; upstream changes
  repair chronological ordering, fork/revert history, truncation, and
  compaction cleanup without changing the supported command or archive model.

## [0.2.3] - 2026-08-06

- Update OpenCode from `1.18.13` to stable `1.18.14` and synchronize exact
  GitHub release identities for all six supported macOS and Ubuntu assets.
- Preserve the native setup, profile, lifecycle, host-selection, and isolated
  launch contracts; upstream changes are provider/runtime reliability fixes.

## [0.2.2] - 2026-08-05

- Update OpenCode from `1.18.10` to stable `1.18.13` using the canonical
  upstream release and exact digests and sizes for all six supported assets.
- Preserve the existing native setup, profile, lifecycle, and host-selection
  contracts while advancing only the verified runtime baseline.

## [0.2.1] - 2026-07-30

- Keep GitHub release and asset numeric identifiers, tag-ref currentness, and
  publication observations out of the runtime install and software stamp.
- Preserve exact supported artifact URL, size, SHA-256, version probing, and
  backward reading of schema-2 stamps containing legacy observation fields.

## 0.2.0 - 2026-07-30

- Restructure OpenCode setup ownership into the `nddev-builder` content setup
  plus `full-auto` and `safe` runtime profiles.
- Pin OpenCode to the official immutable GitHub release asset contract owned by
  `build/version.json`.
- Replace Bun/npm installation with exact release asset verification and safe
  single-binary extraction.
- Add schema-2 setup and software stamps, canonical `update`, `migrate`, and
  `remove-cli`.
- Hold external and internal persistent locks through launched OpenCode child
  processes.
- Force source-used OpenCode runtime flags for target-only config, disabled
  project config, disabled external skills/Claude compatibility, and disabled
  sharing.
- Scope CLI installation and launch to macOS plus Ubuntu desktop/server, with
  the exact host contract owned by `build/version.json`.
- Add native command/reference projection files and a CLAUDE bridge to the
  canonical `AGENTS.md`.

## 0.1.0

- Add target-explicit OpenCode setup manager with `safe` and `full-auto`
  variants.
- Add native `nddev-builder` projection using OpenCode plugins, skills, and
  Markdown agents.
- Add public contract, manifest, runtime baseline, and release workflow surface.
