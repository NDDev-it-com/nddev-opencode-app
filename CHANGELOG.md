# Changelog

## Unreleased

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
