# NDDev OpenCode Boundary

The NDDev manager launches OpenCode with a target-owned config and runtime
state. Project config discovery, external `.agents` or `.claude` skill scans,
Claude prompt compatibility, and sharing are disabled by manager-owned
environment variables during launch.

This is not a sandbox. The full-auto profile intentionally allows OpenCode
permissions. The boundary protects setup ownership and live-state separation:
no default `HOME`, no live `~/.config/opencode`, no caller-provided
`OPENCODE_*` overrides, and no inherited provider or package-manager secrets.
