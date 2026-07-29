# NDDev OpenCode Setup Manager

`nddev-opencode-app` plans, installs, updates, switches, migrates, restores,
launches, and removes a native OpenCode `nddev-builder` setup in an explicit target
directory. It never defaults to `~/.config/opencode` and never uses the
operator's live `HOME`.

## Setup and profiles

The content setup is `nddev-builder`. Runtime posture is selected separately:

- `full-auto` is the default profile. Its exact OpenCode config is owned by
  `profiles/full-auto/opencode.json`.
- `safe` is optional. It denies edits, asks for shell and external-directory
  access, keeps read/search context available, and allows the native
  `nddev-builder` skill and subagent.

OpenCode sharing is disabled by the selected profile and by the launch boundary
declared in `build/manifest.json`.

## Native builder projection

OpenCode does not document a marketplace format. This module projects
`nddev-builder` onto documented native surfaces. The exact managed projection
is owned by `setups/nddev-builder/setup.json` and `build/manifest.json`.

## Usage

```bash
python3 cli-tools/nddev_opencode.py list --json
python3 cli-tools/nddev_opencode.py plan --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py install --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py update --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py switch --profile safe --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py migrate --target /absolute/opencode-target --profile full-auto --json
python3 cli-tools/nddev_opencode.py restore --backup 0 --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py remove --target /absolute/opencode-target --json
```

All non-launch commands support `--json`. `status` is read-only and never
executes the OpenCode binary:

```bash
python3 cli-tools/nddev_opencode.py status --target /absolute/opencode-target --json
```

## Target-owned OpenCode binary

The software lifecycle uses the pinned official GitHub release asset contract
in `build/version.json`; it does not use Bun or npm for installation.

```bash
python3 cli-tools/nddev_opencode.py software-status --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py install-cli --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py update-cli --target /absolute/opencode-target --json
python3 cli-tools/nddev_opencode.py remove-cli --target /absolute/opencode-target --json
```

Downloaded archives fail closed on metadata, size, or digest mismatch.
Extraction accepts exactly one regular `opencode` binary and rejects unsafe
archive entries. Provenance details are owned by `build/version.json` and
`references/opencode-baseline.json`.

## Supported product hosts

The user-facing product scope is macOS plus Ubuntu desktop/server on arm64 and
x64. Ubuntu desktop and server share the same structured Ubuntu/glibc host
check. The exact canonical host IDs, unsupported categories, vendor artifact
mapping, and official-version-floor observation are owned by
`build/version.json`.

On x64 hosts, the manager chooses the upstream `*-x64-baseline` CLI asset when
AVX2 is not available. Baseline selection does not create a separate NDDev
product host ID; it is an artifact-selection detail.

The upstream release also publishes package families that are not part of this
CLI manager. The observed-vs-supported separation is recorded in
`references/opencode-baseline.json`.

## Launch boundary

Launch through the manager:

```bash
python3 cli-tools/nddev_opencode.py launch --target /absolute/opencode-target -- run "hello"
```

`launch` sets target-owned runtime directories and OpenCode config scope. It
also forces the source-used OpenCode runtime flags declared in
`build/manifest.json`.

Caller-provided `OPENCODE_*`, provider credentials, package-manager tokens,
and live-home state are not inherited. External and internal persistent locks
are acquired in order and held through the child process. Lock descriptors are
close-on-exec and non-inherited. Immediately before handoff the manager
revalidates the target-owned executable identity and digest.

This is not a sandbox and does not claim same-UID tamper-proofing.

## Ownership

The exact managed files, stamps, config keys, and target-owned software layout
are owned by `build/manifest.json`, `config/nddev-contract.json`, and
`cli-tools/nddev_opencode.py`. Other OpenCode config, provider choices, auth
files, sessions, caches, and unknown user files are preserved. Legacy
`balanced` targets require an explicit `--profile safe|full-auto`.

## Public validation

The validator disables bytecode creation internally, so this plain command is
cache-free:

```bash
python3 cli-tools/validate_public_contracts.py
```

For additional caller-side enforcement, `python3 -B` is also safe:

```bash
python3 -B cli-tools/validate_public_contracts.py
```
