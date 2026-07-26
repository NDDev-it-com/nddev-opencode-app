# NDDev OpenCode Setup Manager

`nddev-opencode-app` installs and switches complete OpenCode setup variants in
an explicit target directory. It never defaults to `~/.config/opencode`.

## Setups

- `safe`: read-first permissions, edits denied, shell and external-directory
  access gated, with the native `nddev-builder` projection enabled.
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
python3 cli-tools/nddev_opencode.py switch --setup full-auto --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py restore --backup 0 --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py remove --target /absolute/opencode-target --json
```

Launch OpenCode through the managed target:

```bash
python3 cli-tools/nddev_opencode.py launch --target /absolute/opencode-target -- run "hello"
```

`launch` sets `OPENCODE_CONFIG`, `OPENCODE_CONFIG_DIR`, `HOME`, and XDG
directories for the child process so standard OpenCode global config discovery
does not read the operator's live config.

## Ownership

The manager owns only:

- `permission`, `autoupdate`, and `share` in `opencode.json`
- `AGENTS.md`
- the native builder files listed above
- `NDDEV-OPENCODE-SETUP.json`

Other `opencode.json` keys, provider choices, auth files, sessions, caches, and
unrelated files are preserved.
