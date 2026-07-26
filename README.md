# NDDev OpenCode Setup Manager

`nddev-opencode-app` installs and switches complete OpenCode setup variants in
an explicit target directory. It never defaults to `~/.config/opencode`.

## Setups

- `safe`: read-first permissions, edits denied, shell and external-directory
  access gated, with the native `nddev-builder` projection enabled.
- `balanced`: reviewed-change permissions, edits and shell commands gated while
  read/search/context surfaces and `nddev-builder` remain available.
- `full-auto`: current OpenCode permission keys set to allow, with the same
  `nddev-builder` projection enabled.

## Native Builder Projection

OpenCode does not document a marketplace format. This module therefore projects
`nddev-builder` onto documented native surfaces:

- `plugins/nddev-builder.js`
- `skills/nddev-builder/SKILL.md`
- `agents/nddev-builder.md`

The manager writes those files into the selected target and preserves them when
switching or restoring setups.

## Usage

```bash
python3 cli-tools/nddev_opencode.py list --json
python3 cli-tools/nddev_opencode.py plan --setup safe --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py install --setup safe --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py switch --setup balanced --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py switch --setup full-auto --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py restore --backup 0 --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py remove --target /absolute/opencode-target --json
```

Install the target-owned OpenCode binary with the official Bun package:

```bash
python3 cli-tools/nddev_opencode.py software-status --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py install-cli --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py update-cli --target /absolute/opencode-target --json
```

The manager uses `bun add --global --exact --trust opencode-ai@1.18.5` with
stage-local `BUN_INSTALL_GLOBAL_DIR`, `BUN_INSTALL_BIN`,
`BUN_INSTALL_CACHE_DIR`, `HOME`, `XDG_CONFIG_HOME`, and `TMPDIR`. Only
`install/global` and `bin` are persisted into the target-owned software tree.
Status is read-only and never executes the target binary.

Launch OpenCode through the managed target:

```bash
python3 cli-tools/nddev_opencode.py launch --target /absolute/opencode-target -- run "hello"
```

`launch` sets `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `HOME`, and XDG
directories for the child process so standard OpenCode global config discovery
does not read the operator's live config. It validates setup and target-owned
software under the target lock, releases the lock, then runs
`/absolute/opencode-target/bin/opencode`.

## Ownership

The manager owns only:

- `permission`, `autoupdate`, and `share` in `opencode.json`
- `AGENTS.md`
- the native builder files listed above
- `NDDEV-OPENCODE-SETUP.json`
- `NDDEV-OPENCODE-SOFTWARE.json`
- `.nddev-opencode-software/current`
- `bin/opencode`

Other `opencode.json` keys, provider choices, auth files, sessions, caches, and
unrelated files are preserved.
