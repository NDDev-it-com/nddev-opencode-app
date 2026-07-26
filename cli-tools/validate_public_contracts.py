#!/usr/bin/env python3
"""Validate public nddev-opencode-app contracts without private harness input."""

from __future__ import annotations

import json
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
MANAGED_FILES = [
    "opencode.json",
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "agents/nddev-builder.md",
]
WORKFLOWS = [
    "actionlint.yml",
    "codeql.yml",
    "dependency-review.yml",
    "release.yml",
    "scorecard.yml",
    "secret-scan.yml",
    "zizmor.yml",
]
EXPECTED_BUN_ARGV = ["add", "--global", "--exact", "--trust", "opencode-ai@1.18.5"]
EXPECTED_INTEGRITY = "sha512-Q0jlX4ihn7veMeYsLX3c4PYFAKIURU3GIpXt1FnhNxNn3v8+RpIZ8z9umG5D0r8g8Smp9fZLGjgLe/9mJ4NyYw=="
EXPECTED_SHASUM = "91dcee1ca87ac6f445b4fbf7a3375de170acbfe6"


def load_json(relative: str, errors: list[str]) -> dict[str, Any] | None:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing required JSON file: {relative}")
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        errors.append(f"{relative}: invalid JSON: {exc}")
        return None
    if not isinstance(value, dict):
        errors.append(f"{relative}: top-level value must be an object")
        return None
    return value


def check_text(relative: str, errors: list[str]) -> None:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing required text file: {relative}")
        return
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{relative}: unreadable text: {exc}")
        return
    if not text.strip() or not text.endswith("\n"):
        errors.append(f"{relative}: must be non-empty LF-terminated text")


def check_setup(setup_id: str, errors: list[str]) -> None:
    root = ROOT / "setups" / setup_id
    metadata = load_json(f"setups/{setup_id}/setup.json", errors)
    config = load_json(f"setups/{setup_id}/opencode.json", errors)
    if metadata is not None:
        if metadata.get("id") != setup_id:
            errors.append(f"setups/{setup_id}/setup.json: id mismatch")
        if metadata.get("managed_files") != MANAGED_FILES:
            errors.append(f"setups/{setup_id}/setup.json: managed_files mismatch")
        if metadata.get("builder_enabled") is not True:
            errors.append(f"setups/{setup_id}/setup.json: builder must be enabled")
    if config is not None:
        if config.get("$schema") != "https://opencode.ai/config.json":
            errors.append(f"setups/{setup_id}/opencode.json: current schema required")
        if "tools" in config or "tool" in config:
            errors.append(f"setups/{setup_id}/opencode.json: legacy tools config is forbidden")
        permission = config.get("permission")
        if not isinstance(permission, dict):
            errors.append(f"setups/{setup_id}/opencode.json: permission object required")
        elif setup_id == "safe":
            if permission.get("edit") != "deny" or permission.get("bash") != "ask":
                errors.append("safe setup: expected deny edits and ask shell")
            if (permission.get("skill") or {}).get("nddev-builder") != "allow":
                errors.append("safe setup: nddev-builder skill must be allowed")
            if (permission.get("task") or {}).get("nddev-builder") != "allow":
                errors.append("safe setup: nddev-builder task must be allowed")
        elif setup_id == "balanced":
            if permission.get("edit") != "ask" or permission.get("bash") != "ask":
                errors.append("balanced setup: expected ask edits and shell")
            if permission.get("external_directory") != "ask" or permission.get("webfetch") != "ask":
                errors.append("balanced setup: expected gated external directory and web fetch")
            if (permission.get("skill") or {}).get("nddev-builder") != "allow":
                errors.append("balanced setup: nddev-builder skill must be allowed")
            if (permission.get("task") or {}).get("nddev-builder") != "allow":
                errors.append("balanced setup: nddev-builder task must be allowed")
        elif setup_id == "full-auto":
            if permission.get("edit") != "allow" or permission.get("bash") != "allow":
                errors.append("full-auto setup: expected allow edits and shell")
            if permission.get("skill") != {"*": "allow"}:
                errors.append("full-auto setup: all skills must be allowed")
            if permission.get("task") != {"*": "allow"}:
                errors.append("full-auto setup: all tasks must be allowed")
    for relative in MANAGED_FILES:
        path = root / relative
        if not path.is_file():
            errors.append(f"setups/{setup_id}: missing managed file {relative}")
    check_text(f"setups/{setup_id}/AGENTS.md", errors)
    check_text(f"setups/{setup_id}/skills/nddev-builder/SKILL.md", errors)
    check_text(f"setups/{setup_id}/agents/nddev-builder.md", errors)
    check_text(f"setups/{setup_id}/plugins/nddev-builder.js", errors)


def main() -> int:
    errors: list[str] = []
    version_file = ROOT / "VERSION"
    version_text = (
        version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    )
    version = load_json("build/version.json", errors)
    manifest = load_json("build/manifest.json", errors)
    contract = load_json("config/nddev-contract.json", errors)
    baseline = load_json("references/opencode-baseline.json", errors)

    if version is not None:
        if version.get("build_version") != version_text:
            errors.append("VERSION disagrees with build/version.json:build_version")
        if version.get("opencode_package") != "opencode-ai":
            errors.append("build/version.json: opencode_package must be opencode-ai")
        if version.get("opencode_tested") != "1.18.5":
            errors.append("build/version.json: opencode_tested must be 1.18.5")
        if version.get("opencode_command") != "opencode":
            errors.append("build/version.json: opencode_command must be opencode")
        if version.get("opencode_registry_integrity") != EXPECTED_INTEGRITY:
            errors.append("build/version.json: opencode registry integrity mismatch")
        if version.get("opencode_registry_shasum") != EXPECTED_SHASUM:
            errors.append("build/version.json: opencode registry shasum mismatch")
    if manifest is not None:
        if manifest.get("build_version") != version_text:
            errors.append("build/manifest.json: build_version mismatch")
        if manifest.get("setup_ids") != ["safe", "balanced", "full-auto"]:
            errors.append("build/manifest.json: setup_ids mismatch")
        builder = manifest.get("builder")
        if not isinstance(builder, dict) or builder.get("projection") != "native":
            errors.append("build/manifest.json: native builder projection required")
        elif builder.get("marketplace") is not None:
            errors.append("build/manifest.json: OpenCode marketplace must remain null")
        software = manifest.get("software_lifecycle")
        if not isinstance(software, dict):
            errors.append("build/manifest.json: software_lifecycle required")
        else:
            if software.get("install_argv") != EXPECTED_BUN_ARGV:
                errors.append("build/manifest.json: Bun install argv mismatch")
            if software.get("status_executes_binary") is not False:
                errors.append("build/manifest.json: status must not execute binary")
            if software.get("registry", {}).get("integrity") != EXPECTED_INTEGRITY:
                errors.append("build/manifest.json: registry integrity mismatch")
            if software.get("registry", {}).get("shasum") != EXPECTED_SHASUM:
                errors.append("build/manifest.json: registry shasum mismatch")
    if contract is not None:
        if contract.get("manifest_ref") != "build/manifest.json":
            errors.append("config/nddev-contract.json: manifest_ref mismatch")
        if (contract.get("setup_system") or {}).get("setup_ids") != [
            "safe",
            "balanced",
            "full-auto",
        ]:
            errors.append("config/nddev-contract.json: setup_ids mismatch")
        builder = contract.get("builder")
        if not isinstance(builder, dict) or builder.get("projection") != "native":
            errors.append("config/nddev-contract.json: native builder projection required")
        elif builder.get("marketplace") is not None:
            errors.append("config/nddev-contract.json: marketplace must be null")
        elif builder.get("enabled_in_setups") != ["safe", "balanced", "full-auto"]:
            errors.append("config/nddev-contract.json: builder must be enabled in all setups")
        software = contract.get("software_lifecycle")
        if not isinstance(software, dict):
            errors.append("config/nddev-contract.json: software_lifecycle required")
        else:
            if software.get("install_argv") != EXPECTED_BUN_ARGV:
                errors.append("config/nddev-contract.json: Bun install argv mismatch")
            if software.get("status_executes_binary") is not False:
                errors.append("config/nddev-contract.json: status must not execute binary")
            if software.get("entrypoint") != "bin/opencode":
                errors.append("config/nddev-contract.json: entrypoint must be bin/opencode")
            if software.get("registry", {}).get("integrity") != EXPECTED_INTEGRITY:
                errors.append("config/nddev-contract.json: registry integrity mismatch")
            if software.get("registry", {}).get("shasum") != EXPECTED_SHASUM:
                errors.append("config/nddev-contract.json: registry shasum mismatch")
    if baseline is not None:
        if baseline.get("configuration", {}).get("marketplace") is not None:
            errors.append("references/opencode-baseline.json: marketplace must be null")
        release = baseline.get("release")
        if not isinstance(release, dict):
            errors.append("references/opencode-baseline.json: release required")
        else:
            if release.get("npm_package") != "opencode-ai":
                errors.append("references/opencode-baseline.json: package mismatch")
            if release.get("npm_version") != "1.18.5":
                errors.append("references/opencode-baseline.json: version mismatch")
            if release.get("scripts", {}).get("postinstall") != "node ./postinstall.mjs":
                errors.append("references/opencode-baseline.json: postinstall mismatch")
            if release.get("dist", {}).get("integrity") != EXPECTED_INTEGRITY:
                errors.append("references/opencode-baseline.json: integrity mismatch")
            if release.get("dist", {}).get("shasum") != EXPECTED_SHASUM:
                errors.append("references/opencode-baseline.json: shasum mismatch")

    for setup_id in ("safe", "balanced", "full-auto"):
        check_setup(setup_id, errors)
    for relative in (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "cli-tools/nddev_opencode.py",
    ):
        check_text(relative, errors)
    for workflow in WORKFLOWS:
        check_text(f".github/workflows/{workflow}", errors)

    if errors:
        print(f"validate_public_contracts.py: FAIL ({len(errors)} error(s))")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
