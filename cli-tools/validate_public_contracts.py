#!/usr/bin/env python3
"""Validate public nddev-opencode-app contracts without private harness input."""

# ruff: noqa: E402

from __future__ import annotations

import sys

sys.dont_write_bytecode = True

import contextlib
import importlib.util
import io
import json
import os
import stat
import subprocess
import tempfile
import zipfile
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
VERSION_TEXT = "0.2.0"
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
DEFAULT_PROFILE = "full-auto"
MANAGED_FILES = [
    "opencode.json",
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "skills/nddev-builder/references/native-surfaces.md",
    "skills/nddev-builder/references/security-boundary.md",
    "agents/nddev-builder.md",
    "commands/nddev-orient.md",
    "commands/nddev-validate.md",
]
CONTENT_FILES = MANAGED_FILES[1:]
SOURCE_USED_RUNTIME_FLAGS = {
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_SHARE",
}
EXPECTED_RELEASE = {
    "tag": "v1.18.8",
    "id": 360858647,
    "immutable": True,
    "tag_ref": "3c81a5d1ddceab377d9ad71c14899e6935333fdd",
    "target_commitish": "484f00ebf44fbb9ec938b2155dad42c34fc5a7a7",
}
EXPECTED_ARTIFACTS = {
    "darwin-arm64": {
        "id": 492336314,
        "name": "opencode-darwin-arm64.zip",
        "size": 45041487,
        "sha256": "0fb2e11a819dd97949f0f7e0348e0e0c4fd8c42b3a5ed7aee1f0d437c94b9f0c",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-darwin-arm64.zip",
    },
    "darwin-x64": {
        "id": 492336313,
        "name": "opencode-darwin-x64.zip",
        "size": 47279642,
        "sha256": "0193ed3f295bb93f073ae0e8fa0737e9b31f167464761901589401fd278d4cc4",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-darwin-x64.zip",
    },
    "darwin-x64-baseline": {
        "id": 492336312,
        "name": "opencode-darwin-x64-baseline.zip",
        "size": 47279642,
        "sha256": "16702f945bc94340c2bda3345ea936ef7927226a333f175b864ae253d9fc351e",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-darwin-x64-baseline.zip",
    },
    "linux-arm64": {
        "id": 492336388,
        "name": "opencode-linux-arm64.tar.gz",
        "size": 59208626,
        "sha256": "3e1b4f3bd12764c911f9211910608f85429b6209900a662c7ed27196c9033b93",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-linux-arm64.tar.gz",
    },
    "linux-x64": {
        "id": 492336385,
        "name": "opencode-linux-x64.tar.gz",
        "size": 59404172,
        "sha256": "b72014b8b53427fdb5a628d2433569ee7ccd289bd5c4490636064b24791c1305",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-linux-x64.tar.gz",
    },
    "linux-x64-baseline": {
        "id": 492336397,
        "name": "opencode-linux-x64-baseline.tar.gz",
        "size": 59404173,
        "sha256": "132b605fe6081e1daf1a59a43a83125db86864d59feb9c68320fafbe0cb0bdb1",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-linux-x64-baseline.tar.gz",
    },
}
EXPECTED_SUPPORTED_PRODUCT_HOSTS = {
    "macos-arm64": {
        "system": "darwin",
        "architecture": "arm64",
        "artifact_platforms": ["darwin-arm64"],
    },
    "macos-x64": {
        "system": "darwin",
        "architecture": "x64",
        "x64_baseline_selection": "x64 host without AVX2",
        "artifact_platforms": ["darwin-x64", "darwin-x64-baseline"],
    },
    "ubuntu-glibc-arm64": {
        "system": "linux",
        "distribution_id": "ubuntu",
        "distribution_metadata_source": "platform.freedesktop_os_release",
        "variants": ["desktop", "server"],
        "libc": "glibc",
        "official_distribution_version_floor": None,
        "official_distribution_version_floor_note": "no-official-floor",
        "architecture": "arm64",
        "artifact_platforms": ["linux-arm64"],
    },
    "ubuntu-glibc-x64": {
        "system": "linux",
        "distribution_id": "ubuntu",
        "distribution_metadata_source": "platform.freedesktop_os_release",
        "variants": ["desktop", "server"],
        "libc": "glibc",
        "official_distribution_version_floor": None,
        "official_distribution_version_floor_note": "no-official-floor",
        "architecture": "x64",
        "x64_baseline_selection": "x64 host without AVX2",
        "artifact_platforms": ["linux-x64", "linux-x64-baseline"],
    },
}
EXPECTED_UNSUPPORTED_PRODUCT_HOSTS = [
    "windows",
    "non-ubuntu-linux",
    "linux-musl",
    "unsupported-architecture",
]
EXPECTED_PRODUCT_UNSUPPORTED_CLI_ASSETS = {
    "windows": {
        "windows-arm64": {
            "id": 492338717,
            "name": "opencode-windows-arm64.zip",
            "size": 57687772,
            "sha256": "3a2c5a6f246bd0fdb395b35d8cc60f1be86f7f794a22cb517d2fe8de9aa951b4",
            "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-windows-arm64.zip",
            "format": "zip",
            "product_supported": False,
            "unsupported_category": "windows",
        },
        "windows-x64": {
            "id": 492338712,
            "name": "opencode-windows-x64.zip",
            "size": 59527527,
            "sha256": "85baa5de531db8d611fb5d9a62ffee00f6de69ae26e4845ec091dd2da4eb5fd1",
            "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-windows-x64.zip",
            "format": "zip",
            "product_supported": False,
            "unsupported_category": "windows",
        },
        "windows-x64-baseline": {
            "id": 492338711,
            "name": "opencode-windows-x64-baseline.zip",
            "size": 59527534,
            "sha256": "91d1b2e0faf5210ff06ae7d1014905531dfea723ab088455e553deafdb722006",
            "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.8/opencode-windows-x64-baseline.zip",
            "format": "zip",
            "product_supported": False,
            "unsupported_category": "windows",
        },
    }
}
EXPECTED_UPSTREAM_DISTRIBUTION_OBSERVATION = {
    "cli_asset_families": ["darwin", "linux-glibc", "linux-musl", "windows", "x64-baseline"],
    "desktop_packages_excluded_from_cli_manager": [".deb", ".rpm", "AppImage"],
    "ubuntu_glibc_version_floor": None,
    "ubuntu_glibc_version_floor_note": "no-official-floor",
    "product_unsupported_cli_assets": EXPECTED_PRODUCT_UNSUPPORTED_CLI_ASSETS,
}
EXPECTED_ARTIFACT_PRODUCT_HOST_MAP = {
    "darwin-arm64": {"product_host": "macos-arm64", "x64_baseline": False},
    "darwin-x64": {"product_host": "macos-x64", "x64_baseline": False},
    "darwin-x64-baseline": {"product_host": "macos-x64", "x64_baseline": True},
    "linux-arm64": {"product_host": "ubuntu-glibc-arm64", "x64_baseline": False},
    "linux-x64": {"product_host": "ubuntu-glibc-x64", "x64_baseline": False},
    "linux-x64-baseline": {"product_host": "ubuntu-glibc-x64", "x64_baseline": True},
}
WORKFLOWS = [
    "actionlint.yml",
    "codeql.yml",
    "dependency-review.yml",
    "release.yml",
    "scorecard.yml",
    "secret-scan.yml",
    "zizmor.yml",
]
FORBIDDEN_PUBLIC_PARTS = {
    "__pycache__",
    ".pytest_cache",
    ".mypy_cache",
    ".ruff_cache",
    ".serena",
    "evidence",
    "benchmarks",
    "fixtures",
    "tests",
    "validation",
    "memories",
}


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


def check_text(relative: str, errors: list[str]) -> str:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing required text file: {relative}")
        return ""
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError) as exc:
        errors.append(f"{relative}: unreadable text: {exc}")
        return ""
    if not text.strip() or not text.endswith("\n") or "\r" in text:
        errors.append(f"{relative}: must be non-empty LF-terminated text")
    return text


def check_executable(relative: str, errors: list[str]) -> None:
    path = ROOT / relative
    if not path.is_file():
        errors.append(f"missing executable file: {relative}")
        return
    if not (path.stat().st_mode & stat.S_IXUSR):
        errors.append(f"{relative}: owner executable bit required")


def check_no_forbidden_public_paths(errors: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        relative = path.relative_to(ROOT).as_posix()
        lower_parts = {part.lower() for part in path.relative_to(ROOT).parts}
        if FORBIDDEN_PUBLIC_PARTS & lower_parts:
            errors.append(f"forbidden public path: {relative}")
        if path.is_file() and path.suffix in {".pyc", ".pyo"}:
            errors.append(f"Python cache file is forbidden: {relative}")
        if path.is_file() and any(
            token in relative.lower() for token in ("private", "fixture", "evidence", "benchmark")
        ):
            errors.append(f"private/test/evidence naming is forbidden: {relative}")


def check_profile(profile_id: str, errors: list[str]) -> None:
    metadata = load_json(f"profiles/{profile_id}/profile.json", errors)
    config = load_json(f"profiles/{profile_id}/opencode.json", errors)
    if metadata is not None:
        if metadata.get("schema_version") != 2:
            errors.append(f"profiles/{profile_id}/profile.json: schema_version must be 2")
        if metadata.get("id") != profile_id:
            errors.append(f"profiles/{profile_id}/profile.json: id mismatch")
        if metadata.get("managed_config_keys") != ["autoupdate", "share", "permission"]:
            errors.append(f"profiles/{profile_id}/profile.json: managed_config_keys mismatch")
        if metadata.get("default") is not (profile_id == DEFAULT_PROFILE):
            errors.append(f"profiles/{profile_id}/profile.json: default flag mismatch")
    if config is not None:
        if config.get("$schema") != "https://opencode.ai/config.json":
            errors.append(f"profiles/{profile_id}/opencode.json: current schema required")
        if config.get("autoupdate") is not False:
            errors.append(f"profiles/{profile_id}/opencode.json: autoupdate must be false")
        if config.get("share") != "disabled":
            errors.append(f"profiles/{profile_id}/opencode.json: share must be disabled")
        if "tools" in config or "tool" in config:
            errors.append(f"profiles/{profile_id}/opencode.json: legacy tools config is forbidden")
        if profile_id == "full-auto":
            exact = {
                "$schema": "https://opencode.ai/config.json",
                "autoupdate": False,
                "share": "disabled",
                "permission": "allow",
            }
            if config != exact:
                errors.append(
                    "profiles/full-auto/opencode.json: exact official permission scalar shape required"
                )
        if profile_id == "safe":
            permission = config.get("permission")
            if not isinstance(permission, dict):
                errors.append("profiles/safe/opencode.json: permission object required")
            else:
                if permission.get("edit") != "deny" or permission.get("bash") != "ask":
                    errors.append("safe profile: expected deny edits and ask shell")
                if permission.get("external_directory") != "ask":
                    errors.append("safe profile: external_directory must ask")
                if (permission.get("skill") or {}).get("nddev-builder") != "allow":
                    errors.append("safe profile: nddev-builder skill must be allowed")
                if (permission.get("task") or {}).get("nddev-builder") != "allow":
                    errors.append("safe profile: nddev-builder task must be allowed")


def check_setup(errors: list[str]) -> None:
    metadata = load_json("setups/nddev-builder/setup.json", errors)
    if metadata is not None:
        if metadata.get("schema_version") != 2:
            errors.append("setups/nddev-builder/setup.json: schema_version must be 2")
        if metadata.get("id") != "nddev-builder":
            errors.append("setups/nddev-builder/setup.json: id mismatch")
        if metadata.get("content_files") != CONTENT_FILES:
            errors.append("setups/nddev-builder/setup.json: content_files mismatch")
        if metadata.get("builder_enabled") is not True:
            errors.append("setups/nddev-builder/setup.json: builder must be enabled")
    for relative in CONTENT_FILES:
        check_text(f"setups/nddev-builder/{relative}", errors)
    for legacy in ("safe", "balanced", "full-auto"):
        if (ROOT / "setups" / legacy).exists():
            errors.append(f"legacy setup directory must be removed: setups/{legacy}")


def check_release(
    version: dict[str, Any] | None, baseline: dict[str, Any] | None, errors: list[str]
) -> None:
    if version is None:
        return
    if version.get("schema_version") != 2:
        errors.append("build/version.json: schema_version must be 2")
    if version.get("build_version") != VERSION_TEXT:
        errors.append("build/version.json: build_version mismatch")
    if version.get("opencode_version") != "1.18.8":
        errors.append("build/version.json: opencode_version must be 1.18.8")
    release = version.get("release")
    if not isinstance(release, dict):
        errors.append("build/version.json: release object required")
    else:
        for key, expected in EXPECTED_RELEASE.items():
            if release.get(key) != expected:
                errors.append(f"build/version.json: release.{key} mismatch")
    if version.get("artifacts") != EXPECTED_ARTIFACTS:
        errors.append("build/version.json: pinned artifact table mismatch")
    if version.get("supported_product_hosts") != EXPECTED_SUPPORTED_PRODUCT_HOSTS:
        errors.append("build/version.json: supported_product_hosts canonical IDs mismatch")
    if version.get("unsupported_product_hosts") != EXPECTED_UNSUPPORTED_PRODUCT_HOSTS:
        errors.append("build/version.json: unsupported_product_hosts canonical categories mismatch")
    if (
        version.get("upstream_distribution_observation")
        != EXPECTED_UPSTREAM_DISTRIBUTION_OBSERVATION
    ):
        errors.append("build/version.json: upstream distribution observation mismatch")
    if version.get("artifact_product_host_map") != EXPECTED_ARTIFACT_PRODUCT_HOST_MAP:
        errors.append("build/version.json: artifact_product_host_map mismatch")
    npm = version.get("npm_observation")
    if not isinstance(npm, dict) or npm.get("install_channel") != "not-used-by-nddev-0.2.0":
        errors.append("build/version.json: npm must be observational only")
    if baseline is not None:
        flags = set(baseline.get("source_verified_runtime_flags") or [])
        if flags != SOURCE_USED_RUNTIME_FLAGS:
            errors.append("references/opencode-baseline.json: source-used runtime flags mismatch")
        if baseline.get("release", {}).get("cli_signature") is not None:
            errors.append("references/opencode-baseline.json: CLI signature must be explicit null")
        host_scope = baseline.get("product_host_scope")
        if not isinstance(host_scope, dict):
            errors.append("references/opencode-baseline.json: product_host_scope required")
        else:
            if host_scope.get("supported") != list(EXPECTED_SUPPORTED_PRODUCT_HOSTS):
                errors.append(
                    "references/opencode-baseline.json: supported product host IDs mismatch"
                )
            if host_scope.get("unsupported") != EXPECTED_UNSUPPORTED_PRODUCT_HOSTS:
                errors.append(
                    "references/opencode-baseline.json: unsupported product categories mismatch"
                )
            if host_scope.get("ubuntu_glibc_version_floor") is not None:
                errors.append("references/opencode-baseline.json: Ubuntu/glibc floor must be null")
            if host_scope.get("ubuntu_glibc_version_floor_note") != "no-official-floor":
                errors.append("references/opencode-baseline.json: Ubuntu/glibc floor note mismatch")
            if host_scope.get("observed_upstream_cli_asset_families") != [
                "darwin",
                "linux-glibc",
                "linux-musl",
                "windows",
                "x64-baseline",
            ]:
                errors.append("references/opencode-baseline.json: observed asset families mismatch")
            if (
                host_scope.get("product_unsupported_cli_assets_ref")
                != "build/version.json:upstream_distribution_observation.product_unsupported_cli_assets"
            ):
                errors.append("references/opencode-baseline.json: unsupported asset ref mismatch")


def check_manifest(manifest: dict[str, Any] | None, errors: list[str]) -> None:
    if manifest is None:
        return
    if manifest.get("schema_version") != 2 or manifest.get("build_version") != VERSION_TEXT:
        errors.append("build/manifest.json: schema/build version mismatch")
    if manifest.get("setup_ids") != SETUP_IDS:
        errors.append("build/manifest.json: setup_ids mismatch")
    if manifest.get("profile_ids") != PROFILE_IDS:
        errors.append("build/manifest.json: profile_ids mismatch")
    if manifest.get("default_profile") != DEFAULT_PROFILE:
        errors.append("build/manifest.json: default_profile mismatch")
    if manifest.get("managed_files")[: len(MANAGED_FILES)] != MANAGED_FILES:
        errors.append("build/manifest.json: managed_files mismatch")
    command_policy = manifest.get("command_policy") or {}
    expected_json_commands = [
        "list",
        "status",
        "plan",
        "install",
        "update",
        "switch",
        "migrate",
        "restore",
        "remove",
        "software-status",
        "install-cli",
        "update-cli",
        "remove-cli",
    ]
    if command_policy.get("json_supported") != expected_json_commands:
        errors.append("build/manifest.json: canonical JSON command list mismatch")
    expected_target_commands = expected_json_commands[1:] + ["launch"]
    if command_policy.get("target_required") != expected_target_commands:
        errors.append("build/manifest.json: canonical target command list mismatch")
    if command_policy.get("host_precheck_before_target_resolution") != expected_target_commands:
        errors.append("build/manifest.json: host precheck target command list mismatch")
    if command_policy.get("setup_update_uses_installed_identity") is not True:
        errors.append("build/manifest.json: setup update must use installed identity")
    if command_policy.get("read_only_commands_create_locks") is not True:
        errors.append("build/manifest.json: read-only commands must acquire lifecycle locks")
    if command_policy.get("noop_plan_empty_changes") is not True:
        errors.append("build/manifest.json: no-op plan contract missing")
    if command_policy.get("noop_mutation_writes_backup") is not False:
        errors.append("build/manifest.json: no-op mutation backup contract mismatch")
    builder = manifest.get("builder")
    if not isinstance(builder, dict) or builder.get("projection") != "native":
        errors.append("build/manifest.json: native builder projection required")
    elif builder.get("marketplace") is not None:
        errors.append("build/manifest.json: OpenCode marketplace must remain null")
    launch = manifest.get("runtime_launch")
    if not isinstance(launch, dict):
        errors.append("build/manifest.json: runtime_launch required")
    else:
        forced = launch.get("forced_environment")
        if not isinstance(forced, dict):
            errors.append("build/manifest.json: forced_environment required")
        else:
            if {
                key for key in forced if key.startswith("OPENCODE_DISABLE")
            } != SOURCE_USED_RUNTIME_FLAGS:
                errors.append("build/manifest.json: forced source-used env flags mismatch")
            if forced.get("OPENCODE_DISABLE_SHARE") != "1":
                errors.append("build/manifest.json: OPENCODE_DISABLE_SHARE must be forced")
        if (
            launch.get("supported_product_hosts_ref")
            != "build/version.json:supported_product_hosts"
        ):
            errors.append("build/manifest.json: runtime supported host ref mismatch")
        if (
            launch.get("unsupported_product_hosts_ref")
            != "build/version.json:unsupported_product_hosts"
        ):
            errors.append("build/manifest.json: runtime unsupported host ref mismatch")
        if (
            launch.get("artifact_product_host_map_ref")
            != "build/version.json:artifact_product_host_map"
        ):
            errors.append("build/manifest.json: runtime artifact host map ref mismatch")
        if launch.get("host_check_before_runtime") is not True:
            errors.append("build/manifest.json: runtime host check before handoff required")
        if launch.get("host_check_before_target_inspection") is not True:
            errors.append(
                "build/manifest.json: runtime host check before target inspection required"
            )
        if launch.get("runtime_dirs_real_private") is not True:
            errors.append("build/manifest.json: runtime dirs must be real private dirs")
        lock_policy = launch.get("lock_file_policy") or {}
        for key, expected in {
            "parent_real_private": True,
            "open_no_follow": True,
            "identity_revalidated": True,
            "mode": "0600",
            "nlink": 1,
        }.items():
            if lock_policy.get(key) != expected:
                errors.append(f"build/manifest.json: lock_file_policy.{key} mismatch")
    software = manifest.get("software_lifecycle")
    if not isinstance(software, dict):
        errors.append("build/manifest.json: software_lifecycle required")
    else:
        if software.get("install_channel") != "official-github-release-asset":
            errors.append("build/manifest.json: official release asset install channel required")
        if software.get("status_executes_binary") is not False:
            errors.append("build/manifest.json: status must not execute binary")
        if software.get("remove_command") is None:
            errors.append("build/manifest.json: remove-cli command required")
        if (
            software.get("supported_product_hosts_ref")
            != "build/version.json:supported_product_hosts"
        ):
            errors.append("build/manifest.json: software supported host ref mismatch")
        if (
            software.get("unsupported_product_hosts_ref")
            != "build/version.json:unsupported_product_hosts"
        ):
            errors.append("build/manifest.json: software unsupported host ref mismatch")
        if (
            software.get("artifact_product_host_map_ref")
            != "build/version.json:artifact_product_host_map"
        ):
            errors.append("build/manifest.json: software artifact host map ref mismatch")
        if (
            software.get("upstream_distribution_observation_ref")
            != "build/version.json:upstream_distribution_observation"
        ):
            errors.append("build/manifest.json: upstream observation ref mismatch")
        if software.get("host_check_before_network") is not True:
            errors.append("build/manifest.json: host check before network required")
        if software.get("host_check_before_stage") is not True:
            errors.append("build/manifest.json: host check before stage required")
        if software.get("host_check_before_target_inspection") is not True:
            errors.append("build/manifest.json: host check before target inspection required")
        download = software.get("download_policy") or {}
        for key in (
            "content_length_required",
            "bounded_chunked_read",
            "fail_on_short_or_long_body",
        ):
            if download.get(key) is not True:
                errors.append(f"build/manifest.json: download_policy.{key} required")
        transaction = software.get("transaction_rollback") or {}
        for key in (
            "fresh_install",
            "update",
            "remove",
            "restores_current_entrypoint_stamp_and_absence",
            "final_cleanup_required",
            "cleanup_retry",
        ):
            if transaction.get(key) is not True:
                errors.append(f"build/manifest.json: transaction_rollback.{key} required")
        if transaction.get("rollback_fault_retry") is not True:
            errors.append("build/manifest.json: transaction rollback fault retry required")
    backup = manifest.get("backup_policy") or {}
    if backup.get("envelope_schema") != 3:
        errors.append("build/manifest.json: backup schema 3 required")
    for key in (
        "envelope_exact_keys",
        "files_exact_known_paths",
        "sizes_exact_present_files",
        "digests_exact_present_files",
        "restore_transaction_rollback",
        "transactional_slot_replacement",
        "rollback_fault_retry",
        "final_cleanup_required",
        "cleanup_retry",
    ):
        if backup.get(key) is not True:
            errors.append(f"build/manifest.json: backup_policy.{key} required")
    if backup.get("failed_mutation_writes_backup") is not False:
        errors.append("build/manifest.json: failed mutation must not write backup")
    atomic = manifest.get("atomic_write_policy") or {}
    if atomic.get("order") != [
        "temp-write",
        "chmod",
        "file-fsync",
        "replace",
        "parent-fsync",
        "postcondition",
    ]:
        errors.append("build/manifest.json: atomic write order mismatch")
    if atomic.get("post_replace_failure_rolls_back") is not True:
        errors.append("build/manifest.json: post-replace rollback contract required")
    if atomic.get("fsync_errors_fail_closed") is not True:
        errors.append("build/manifest.json: fail-closed fsync contract required")


def check_contract(contract: dict[str, Any] | None, errors: list[str]) -> None:
    if contract is None:
        return
    if contract.get("contract_version") != 2:
        errors.append("config/nddev-contract.json: contract_version must be 2")
    if contract.get("manifest_ref") != "build/manifest.json":
        errors.append("config/nddev-contract.json: manifest_ref mismatch")
    setup = contract.get("setup_system") or {}
    if setup.get("setup_ids") != SETUP_IDS or setup.get("profile_ids") != PROFILE_IDS:
        errors.append("config/nddev-contract.json: setup/profile ids mismatch")
    if (
        setup.get("update_command")
        != "python3 cli-tools/nddev_opencode.py update --target <absolute-target> [--json]"
    ):
        errors.append("config/nddev-contract.json: target-only setup update_command required")
    expected_target_commands = [
        "status",
        "plan",
        "install",
        "update",
        "switch",
        "migrate",
        "restore",
        "remove",
        "software-status",
        "install-cli",
        "update-cli",
        "remove-cli",
        "launch",
    ]
    if setup.get("host_precheck_before_target_resolution") != expected_target_commands:
        errors.append("config/nddev-contract.json: host precheck target command list mismatch")
    if setup.get("update_uses_installed_identity") is not True:
        errors.append("config/nddev-contract.json: setup update must use installed identity")
    if setup.get("read_only_commands_create_locks") is not True:
        errors.append("config/nddev-contract.json: read-only commands must acquire lifecycle locks")
    if setup.get("noop_plan_empty_changes") is not True:
        errors.append("config/nddev-contract.json: no-op plan contract missing")
    if setup.get("noop_mutation_writes_backup") is not False:
        errors.append("config/nddev-contract.json: no-op mutation backup contract mismatch")
    managed = contract.get("managed_state") or {}
    for key in (
        "restore_transaction_rollback",
        "backup_envelope_exact_keys",
        "backup_files_exact_known_paths",
        "backup_sizes_exact_present_files",
        "backup_digests_exact_present_files",
    ):
        if managed.get(key) is not True:
            errors.append(f"config/nddev-contract.json: managed_state.{key} required")
    if managed.get("backup_envelope_schema") != 3:
        errors.append("config/nddev-contract.json: managed_state backup schema 3 required")
    profiles = contract.get("profiles") or {}
    full_auto = profiles.get("full-auto", {}).get("opencode_config")
    if full_auto != {
        "$schema": "https://opencode.ai/config.json",
        "autoupdate": False,
        "share": "disabled",
        "permission": "allow",
    }:
        errors.append("config/nddev-contract.json: full-auto exact config mismatch")
    launch = contract.get("runtime_launch") or {}
    forced = launch.get("source_proven_forced_environment") or {}
    if {key for key in forced if key.startswith("OPENCODE_DISABLE")} != SOURCE_USED_RUNTIME_FLAGS:
        errors.append("config/nddev-contract.json: source-proven forced env mismatch")
    if launch.get("locks", {}).get("same_uid_absolute_tamper_proof_claim") is not False:
        errors.append("config/nddev-contract.json: must not claim same-UID tamper-proofing")
    locks = launch.get("locks") or {}
    for key, expected in {
        "parent_real_private": True,
        "open_no_follow": True,
        "identity_revalidated": True,
        "mode": "0600",
        "nlink": 1,
    }.items():
        if locks.get(key) != expected:
            errors.append(f"config/nddev-contract.json: locks.{key} mismatch")
    if launch.get("runtime_dirs_real_private") is not True:
        errors.append("config/nddev-contract.json: runtime dirs must be real private dirs")
    if launch.get("reject_runtime_dir_symlink") is not True:
        errors.append("config/nddev-contract.json: runtime dir symlink rejection required")
    if launch.get("supported_product_hosts_ref") != "build/version.json:supported_product_hosts":
        errors.append("config/nddev-contract.json: runtime supported host ref mismatch")
    if (
        launch.get("unsupported_product_hosts_ref")
        != "build/version.json:unsupported_product_hosts"
    ):
        errors.append("config/nddev-contract.json: runtime unsupported host ref mismatch")
    if (
        launch.get("artifact_product_host_map_ref")
        != "build/version.json:artifact_product_host_map"
    ):
        errors.append("config/nddev-contract.json: runtime artifact host map ref mismatch")
    if launch.get("host_check_before_target_inspection") is not True:
        errors.append(
            "config/nddev-contract.json: runtime host check before target inspection required"
        )
    checks = launch.get("pre_handoff_checks")
    if not isinstance(checks, list) or "supported product host" not in checks:
        errors.append("config/nddev-contract.json: runtime pre-handoff host check required")
    software = contract.get("software_lifecycle") or {}
    if software.get("status_executes_binary") is not False:
        errors.append("config/nddev-contract.json: status must not execute binary")
    if software.get("install_channel") != "official-github-release-asset":
        errors.append("config/nddev-contract.json: install channel mismatch")
    if software.get("package_manager") is not None:
        errors.append("config/nddev-contract.json: package_manager must be null")
    if software.get("supported_product_hosts_ref") != "build/version.json:supported_product_hosts":
        errors.append("config/nddev-contract.json: software supported host ref mismatch")
    if (
        software.get("unsupported_product_hosts_ref")
        != "build/version.json:unsupported_product_hosts"
    ):
        errors.append("config/nddev-contract.json: software unsupported host ref mismatch")
    if (
        software.get("artifact_product_host_map_ref")
        != "build/version.json:artifact_product_host_map"
    ):
        errors.append("config/nddev-contract.json: software artifact host map ref mismatch")
    if (
        software.get("upstream_distribution_observation_ref")
        != "build/version.json:upstream_distribution_observation"
    ):
        errors.append("config/nddev-contract.json: upstream observation ref mismatch")
    if software.get("host_check_before_network") is not True:
        errors.append("config/nddev-contract.json: host check before network required")
    if software.get("host_check_before_stage") is not True:
        errors.append("config/nddev-contract.json: host check before stage required")
    if software.get("host_check_before_target_inspection") is not True:
        errors.append("config/nddev-contract.json: host check before target inspection required")
    download = software.get("download_policy") or {}
    for key in ("content_length_required", "bounded_chunked_read", "fail_on_short_or_long_body"):
        if download.get(key) is not True:
            errors.append(f"config/nddev-contract.json: download_policy.{key} required")
    transaction = software.get("transaction_rollback") or {}
    for key in (
        "fresh_install",
        "update",
        "remove",
        "restores_current_entrypoint_stamp_and_absence",
        "final_cleanup_required",
        "cleanup_retry",
    ):
        if transaction.get(key) is not True:
            errors.append(f"config/nddev-contract.json: transaction_rollback.{key} required")
    if transaction.get("rollback_fault_retry") is not True:
        errors.append("config/nddev-contract.json: transaction rollback fault retry required")
    migration = contract.get("migration") or {}
    if migration.get("legacy_setup_map", {}).get("balanced") != "explicit-profile-required":
        errors.append(
            "config/nddev-contract.json: balanced migration must require explicit profile"
        )
    safety = contract.get("safety") or {}
    for key in (
        "restore_transaction_rollback",
        "backup_envelope_exact_keys",
        "backup_files_exact_known_paths",
        "backup_sizes_exact_present_files",
        "backup_digests_exact_present_files",
    ):
        if safety.get(key) is not True:
            errors.append(f"config/nddev-contract.json: safety.{key} required")
    if safety.get("backup_envelope_schema") != 3:
        errors.append("config/nddev-contract.json: safety backup schema 3 required")
    if safety.get("noop_mutation_writes_backup") is not False:
        errors.append("config/nddev-contract.json: no-op mutation backup safety mismatch")
    if safety.get("backup_transactional_slot_replacement") is not True:
        errors.append("config/nddev-contract.json: backup transactional replacement required")
    if safety.get("backup_final_cleanup_required") is not True:
        errors.append("config/nddev-contract.json: backup final cleanup contract required")
    if safety.get("backup_cleanup_retry") is not True:
        errors.append("config/nddev-contract.json: backup cleanup retry contract required")
    if safety.get("failed_mutation_writes_backup") is not False:
        errors.append("config/nddev-contract.json: failed mutation backup safety mismatch")
    if safety.get("atomic_write_order") != [
        "temp-write",
        "chmod",
        "file-fsync",
        "replace",
        "parent-fsync",
        "postcondition",
    ]:
        errors.append("config/nddev-contract.json: atomic write order mismatch")
    if safety.get("post_replace_failure_rolls_back") is not True:
        errors.append("config/nddev-contract.json: post-replace rollback safety required")
    if safety.get("fsync_errors_fail_closed") is not True:
        errors.append("config/nddev-contract.json: fail-closed fsync safety required")


def load_manager(errors: list[str]) -> Any:
    path = ROOT / "cli-tools" / "nddev_opencode.py"
    spec = importlib.util.spec_from_file_location("nddev_opencode_public_contract", path)
    if spec is None or spec.loader is None:
        errors.append("cli-tools/nddev_opencode.py: cannot create import spec")
        return None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:  # noqa: BLE001 - validator reports import failure.
        errors.append(f"cli-tools/nddev_opencode.py: import failed: {exc}")
        return None
    return module


def python_cli_argv(script: Path, *args: str) -> list[str]:
    return [sys.executable, "-B", str(script), *args]


def subprocess_clean_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    env = {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "PYTHONDONTWRITEBYTECODE": "1",
    }
    if extra:
        env.update(extra)
    return env


def expect_host(
    manager: Any,
    errors: list[str],
    label: str,
    kwargs: dict[str, Any],
    expected: dict[str, Any],
) -> None:
    try:
        actual = manager.select_supported_host(**kwargs)
    except Exception as exc:  # noqa: BLE001 - validator reports public contract failure.
        errors.append(f"host selection {label}: unexpectedly rejected: {exc}")
        return
    for key, value in expected.items():
        if actual.get(key) != value:
            errors.append(
                f"host selection {label}: expected {key}={value!r}, got {actual.get(key)!r}"
            )


def expect_host_rejection(
    manager: Any,
    errors: list[str],
    label: str,
    kwargs: dict[str, Any],
    category: str,
) -> None:
    try:
        manager.select_supported_host(**kwargs)
    except Exception as exc:  # noqa: BLE001 - validator checks user-facing category text.
        if exc.__class__.__name__ != "ManagerError":
            errors.append(f"host rejection {label}: wrong exception type {exc.__class__.__name__}")
        if f"({category})" not in str(exc):
            errors.append(f"host rejection {label}: category {category!r} missing from {exc}")
        return
    errors.append(f"host rejection {label}: unexpectedly accepted")


def check_host_selection_smokes(errors: list[str]) -> None:
    manager = load_manager(errors)
    if manager is None:
        return
    if getattr(manager, "SUPPORTED_PRODUCT_HOSTS", None) != EXPECTED_SUPPORTED_PRODUCT_HOSTS:
        errors.append("cli-tools/nddev_opencode.py: SUPPORTED_PRODUCT_HOSTS mismatch")
    if getattr(manager, "UNSUPPORTED_PRODUCT_HOSTS", None) != EXPECTED_UNSUPPORTED_PRODUCT_HOSTS:
        errors.append("cli-tools/nddev_opencode.py: UNSUPPORTED_PRODUCT_HOSTS mismatch")
    if getattr(manager, "ARTIFACT_PRODUCT_HOSTS", None) != EXPECTED_ARTIFACT_PRODUCT_HOST_MAP:
        errors.append("cli-tools/nddev_opencode.py: ARTIFACT_PRODUCT_HOSTS mismatch")
    expect_host(
        manager,
        errors,
        "ubuntu x64 avx2",
        {
            "system": "linux",
            "machine": "x86_64",
            "os_release": {"ID": "ubuntu"},
            "libc": ("glibc", "2.39"),
            "avx2": True,
        },
        {
            "product_host": "ubuntu-glibc-x64",
            "artifact_platform": "linux-x64",
            "x64_baseline": False,
            "official_distribution_version_floor": None,
        },
    )
    expect_host(
        manager,
        errors,
        "ubuntu x64 baseline",
        {
            "system": "linux",
            "machine": "amd64",
            "os_release": {"ID": "ubuntu"},
            "libc": ("glibc", "2.35"),
            "avx2": False,
        },
        {
            "product_host": "ubuntu-glibc-x64",
            "artifact_platform": "linux-x64-baseline",
            "x64_baseline": True,
            "official_distribution_version_floor": None,
        },
    )
    expect_host(
        manager,
        errors,
        "ubuntu arm64",
        {
            "system": "linux",
            "machine": "aarch64",
            "os_release": {"ID": "ubuntu"},
            "libc": ("glibc", "2.31"),
            "avx2": None,
        },
        {
            "product_host": "ubuntu-glibc-arm64",
            "artifact_platform": "linux-arm64",
            "x64_baseline": False,
            "official_distribution_version_floor": None,
        },
    )
    expect_host(
        manager,
        errors,
        "macos arm64",
        {
            "system": "darwin",
            "machine": "arm64",
            "os_release": {},
            "libc": ("", ""),
            "avx2": None,
        },
        {
            "product_host": "macos-arm64",
            "artifact_platform": "darwin-arm64",
            "x64_baseline": False,
        },
    )
    expect_host(
        manager,
        errors,
        "macos x64 avx2",
        {
            "system": "darwin",
            "machine": "x86_64",
            "os_release": {},
            "libc": ("", ""),
            "avx2": True,
        },
        {
            "product_host": "macos-x64",
            "artifact_platform": "darwin-x64",
            "x64_baseline": False,
        },
    )
    expect_host(
        manager,
        errors,
        "macos x64 baseline",
        {
            "system": "darwin",
            "machine": "x86_64",
            "os_release": {},
            "libc": ("", ""),
            "avx2": False,
        },
        {
            "product_host": "macos-x64",
            "artifact_platform": "darwin-x64-baseline",
            "x64_baseline": True,
        },
    )
    expect_host_rejection(
        manager,
        errors,
        "debian glibc",
        {
            "system": "linux",
            "machine": "x86_64",
            "os_release": {"ID": "debian"},
            "libc": ("glibc", "2.36"),
            "avx2": True,
        },
        "non-ubuntu-linux",
    )
    expect_host_rejection(
        manager,
        errors,
        "unknown linux glibc",
        {
            "system": "linux",
            "machine": "x86_64",
            "os_release": {},
            "libc": ("glibc", "2.36"),
            "avx2": True,
        },
        "non-ubuntu-linux",
    )
    expect_host_rejection(
        manager,
        errors,
        "alpine musl",
        {
            "system": "linux",
            "machine": "x86_64",
            "os_release": {"ID": "alpine"},
            "libc": ("musl", "1.2.5"),
            "avx2": True,
        },
        "linux-musl",
    )
    expect_host_rejection(
        manager,
        errors,
        "ubuntu musl",
        {
            "system": "linux",
            "machine": "x86_64",
            "os_release": {"ID": "ubuntu"},
            "libc": ("musl", "1.2.5"),
            "avx2": True,
        },
        "linux-musl",
    )
    expect_host_rejection(
        manager,
        errors,
        "windows",
        {
            "system": "win32",
            "machine": "x86_64",
            "os_release": {},
            "libc": ("", ""),
            "avx2": True,
        },
        "windows",
    )
    expect_host_rejection(
        manager,
        errors,
        "ubuntu unsupported arch",
        {
            "system": "linux",
            "machine": "riscv64",
            "os_release": {"ID": "ubuntu"},
            "libc": ("glibc", "2.39"),
            "avx2": False,
        },
        "unsupported-architecture",
    )


def make_private_dir(path: Path) -> Path:
    path.mkdir(mode=0o700, parents=True, exist_ok=True)
    os.chmod(path, 0o700)
    return path


def path_signature(manager: Any, path: Path) -> Any:
    if not path.exists() and not path.is_symlink():
        return None
    rows: list[tuple[str, str, int, str | None]] = []
    entries = [path]
    if path.is_dir() and not path.is_symlink():
        entries.extend(sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()))
    for item in entries:
        info = item.lstat()
        relative = "." if item == path else item.relative_to(path).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            rows.append((relative, "symlink", mode, os.readlink(item)))
        elif stat.S_ISDIR(info.st_mode):
            rows.append((relative, "dir", mode, None))
        elif stat.S_ISREG(info.st_mode):
            rows.append((relative, "file", mode, manager.sha256_bytes(item.read_bytes())))
        else:
            rows.append((relative, "other", mode, None))
    return rows


def identity_mtime_signature(manager: Any, path: Path) -> Any:
    if not path.exists() and not path.is_symlink():
        return None
    rows: list[tuple[str, str, int, int, int, int, str | None]] = []
    entries = [path]
    if path.is_dir() and not path.is_symlink():
        entries.extend(sorted(path.rglob("*"), key=lambda item: item.relative_to(path).as_posix()))
    for item in entries:
        info = item.lstat()
        relative = "." if item == path else item.relative_to(path).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            rows.append(
                (
                    relative,
                    "symlink",
                    mode,
                    info.st_ino,
                    info.st_mtime_ns,
                    len(os.readlink(item)),
                    os.readlink(item),
                )
            )
        elif stat.S_ISDIR(info.st_mode):
            rows.append((relative, "dir", mode, info.st_ino, info.st_mtime_ns, 0, None))
        elif stat.S_ISREG(info.st_mode):
            rows.append(
                (
                    relative,
                    "file",
                    mode,
                    info.st_ino,
                    info.st_mtime_ns,
                    info.st_size,
                    manager.sha256_bytes(item.read_bytes()),
                )
            )
        else:
            rows.append(
                (relative, "other", mode, info.st_ino, info.st_mtime_ns, info.st_size, None)
            )
    return rows


def state_bundle_signature(manager: Any, root: Path, target: Path) -> Any:
    return {
        "root_topology": path_signature(manager, root),
        "target": identity_mtime_signature(manager, target),
        "backup": identity_mtime_signature(manager, manager.backup_root(target)),
        "lock_root": identity_mtime_signature(manager, manager.system_lock_root()),
    }


def software_identity_signature(manager: Any, target: Path) -> Any:
    return {
        "current": identity_mtime_signature(
            manager,
            target / manager.SOFTWARE_DIR_NAME / manager.SOFTWARE_CURRENT_NAME,
        ),
        "entrypoint": identity_mtime_signature(manager, target / "bin" / manager.OPENCODE_COMMAND),
        "stamp": identity_mtime_signature(manager, target / manager.SOFTWARE_STAMP_NAME),
    }


def external_lock_path(manager: Any, target: Path) -> Path:
    token = manager.sha256_bytes(str(target.resolve(strict=False)).encode("utf-8"))
    return manager.system_lock_root() / f"{token}.lock"


def lock_signature(manager: Any, target: Path) -> Any:
    path = external_lock_path(manager, target)
    return {
        "external_file": path_signature(manager, path),
        "external_parent_existed": path.parent.exists() or path.parent.is_symlink(),
        "internal": path_signature(manager, target / ".nddev-opencode-lock"),
    }


def one_shot_fault(manager: Any, point: str) -> tuple[Any, dict[str, bool]]:
    seen = {"value": False}

    def fault(actual: str) -> None:
        if actual == point and not seen["value"]:
            seen["value"] = True
            raise manager.ManagerError(f"fault at {actual}")

    return fault, seen


def one_shot_fsync_failure(manager: Any, fail_on_call: int) -> tuple[Any, dict[str, int]]:
    original = manager.os.fsync
    state = {"calls": 0}

    def injected(fd: int) -> None:
        state["calls"] += 1
        if state["calls"] == fail_on_call:
            raise OSError(f"validator fsync fault {fail_on_call}")
        original(fd)

    return injected, state


def expect_manager_error(
    manager: Any,
    errors: list[str],
    label: str,
    callback: Any,
    *,
    contains: str | None = None,
) -> None:
    try:
        callback()
    except Exception as exc:  # noqa: BLE001 - validator checks public failure boundary.
        if not isinstance(exc, manager.ManagerError):
            errors.append(f"{label}: wrong exception type {exc.__class__.__name__}")
            return
        if contains is not None and contains not in str(exc):
            errors.append(f"{label}: expected error containing {contains!r}, got {exc}")
        return
    errors.append(f"{label}: unexpectedly succeeded")


def fake_host() -> dict[str, Any]:
    return {
        "product_host": "ubuntu-glibc-x64",
        "system": "ubuntu",
        "distribution": "ubuntu",
        "libc": "glibc",
        "architecture": "x64",
        "x64_baseline": False,
        "artifact_platform": "linux-x64",
        "official_distribution_version_floor": None,
        "official_distribution_version_floor_note": "no-official-floor",
    }


def fake_archive(manager: Any) -> tuple[dict[str, Any], bytes, bytes]:
    binary = b"#!/bin/sh\necho opencode 1.18.8\n"
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w", compression=zipfile.ZIP_STORED) as zf:
        info = zipfile.ZipInfo("opencode")
        info.external_attr = (0o100700 & 0xFFFF) << 16
        zf.writestr(info, binary)
    archive = buffer.getvalue()
    artifact = {
        "id": 1,
        "name": "fake-opencode.zip",
        "size": len(archive),
        "sha256": manager.sha256_bytes(archive),
        "url": "https://example.invalid/fake-opencode.zip",
        "format": "zip",
    }
    return artifact, archive, binary


def fake_install_kwargs(manager: Any) -> dict[str, Any]:
    artifact, archive, _ = fake_archive(manager)

    def artifact_downloader(url: str, destination: Path, expected_size: int) -> None:
        if url != artifact["url"] or expected_size != artifact["size"]:
            raise manager.ManagerError("fake downloader received unexpected artifact request")
        manager.atomic_write(destination, archive)

    return {
        "host_detector": fake_host,
        "metadata_fetcher": lambda: {},
        "release_verifier": lambda data: None,
        "artifact_resolver": lambda key: artifact,
        "artifact_downloader": artifact_downloader,
        "version_probe": lambda binary, stage: manager.sha256_bytes(b"opencode 1.18.8"),
    }


def seed_current_software(manager: Any, target: Path, *, current: bool = True) -> None:
    make_private_dir(target)
    manager.ensure_target_private_directory(
        target,
        f"{manager.SOFTWARE_DIR_NAME}/{manager.SOFTWARE_CURRENT_NAME}/bin",
        "seed software",
        create=True,
    )
    old_binary = b"#!/bin/sh\necho old opencode 1.18.8\n"
    manager.atomic_write(
        target
        / manager.SOFTWARE_DIR_NAME
        / manager.SOFTWARE_CURRENT_NAME
        / "bin"
        / manager.OPENCODE_COMMAND,
        old_binary,
        mode=0o700,
    )
    manager.ensure_target_private_directory(target, "bin", "seed entrypoint", create=True)
    manager.atomic_write(target / "bin" / manager.OPENCODE_COMMAND, old_binary, mode=0o700)
    host = fake_host()
    artifact = manager.ARTIFACTS["linux-x64"]
    stamp = manager.software_stamp(
        target,
        host,
        "linux-x64",
        artifact,
        executable_digest=manager.sha256_bytes(old_binary),
        installed_tree_digest=manager.tree_sha256(
            target / manager.SOFTWARE_DIR_NAME / manager.SOFTWARE_CURRENT_NAME
        ),
        version_probe_digest=manager.sha256_bytes(b"old version probe"),
    )
    if not current:
        stamp["opencode_version"] = "0.0.0"
    manager.atomic_write(target / manager.SOFTWARE_STAMP_NAME, manager.canonical_json(stamp))


class FakeResponse:
    def __init__(self, headers: dict[str, str], body: bytes) -> None:
        self.headers = headers
        self._body = io.BytesIO(body)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def read(self, size: int = -1) -> bytes:
        return self._body.read(size)


def check_fsync_fail_closed_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-fsync-direct-") as raw:
        root = make_private_dir(Path(raw) / "root")
        path = root / "file"
        path.write_bytes(b"x")
        fd = os.open(path, os.O_RDWR)
        original = manager.os.fsync
        try:
            injected, state = one_shot_fsync_failure(manager, 1)
            manager.os.fsync = injected
            expect_manager_error(
                manager,
                errors,
                "fsync file descriptor fail-closed",
                lambda: manager.fsync_file_descriptor(fd),
                contains="file fsync failed",
            )
            if state["calls"] != 1:
                errors.append("fsync file descriptor fail-closed: injected fault not reached")
        finally:
            manager.os.fsync = original
            os.close(fd)

        original = manager.os.fsync
        try:
            injected, state = one_shot_fsync_failure(manager, 1)
            manager.os.fsync = injected
            expect_manager_error(
                manager,
                errors,
                "fsync directory fail-closed",
                lambda: manager.fsync_directory(root),
                contains="directory fsync failed",
            )
            if state["calls"] != 1:
                errors.append("fsync directory fail-closed: injected fault not reached")
        finally:
            manager.os.fsync = original

    for fail_on_call in (1, 2):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-fsync-rollback-") as raw:
            root = Path(raw)
            target = root / "target"
            before = state_bundle_signature(manager, root, target)
            original = manager.os.fsync
            try:
                injected, state = one_shot_fsync_failure(manager, fail_on_call)
                manager.os.fsync = injected
                expect_manager_error(
                    manager,
                    errors,
                    f"fsync lifecycle rollback call {fail_on_call}",
                    lambda: manager.install_or_switch(
                        target,
                        manager.render_profile("full-auto"),
                        operation="install",
                    ),
                    contains="fsync failed",
                )
                if state["calls"] < fail_on_call:
                    errors.append(
                        f"fsync lifecycle rollback call {fail_on_call}: injected fault not reached"
                    )
            finally:
                manager.os.fsync = original
            if state_bundle_signature(manager, root, target) != before:
                errors.append(
                    f"fsync lifecycle rollback call {fail_on_call}: state changed after rollback"
                )


def check_path_and_lock_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-path-smoke-") as raw:
        tmp = Path(raw)
        outside = make_private_dir(tmp / "outside")
        target = make_private_dir(tmp / "target")
        (target / "bin").symlink_to(outside, target_is_directory=True)
        before = path_signature(manager, outside)
        expect_manager_error(
            manager,
            errors,
            "bin symlink install preflight",
            lambda: manager.install_cli(target, update=False, **fake_install_kwargs(manager)),
            contains="entrypoint parent",
        )
        if (
            path_signature(manager, outside) != before
            or (outside / manager.OPENCODE_COMMAND).exists()
        ):
            errors.append("bin symlink install preflight: outside directory changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-runtime-smoke-") as raw:
        tmp = Path(raw)
        outside = make_private_dir(tmp / "outside")
        target = make_private_dir(tmp / "target")
        (target / ".runtime-home").symlink_to(outside, target_is_directory=True)
        before = path_signature(manager, outside)
        expect_manager_error(
            manager,
            errors,
            "runtime HOME symlink",
            lambda: manager.launch_env(target),
            contains="runtime directory",
        )
        if path_signature(manager, outside) != before:
            errors.append("runtime HOME symlink: outside directory changed")
        (target / ".runtime-home").unlink()
        unsafe = make_private_dir(target / ".runtime-home")
        os.chmod(unsafe, 0o755)
        expect_manager_error(
            manager,
            errors,
            "runtime HOME unsafe mode",
            lambda: manager.launch_env(target),
            contains="mode 0700",
        )

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-target-mode-") as raw:
        target = make_private_dir(Path(raw) / "target")
        os.chmod(target, 0o755)
        expect_manager_error(
            manager,
            errors,
            "target unsafe mode",
            lambda: manager.ensure_target_directory(target, create=False),
            contains="mode 0700",
        )

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-lock-smoke-") as raw:
        tmp = Path(raw)
        outside = make_private_dir(tmp / "outside")
        target = make_private_dir(tmp / "target")
        (target / ".nddev-opencode-lock").symlink_to(outside, target_is_directory=True)
        before = path_signature(manager, outside)
        expect_manager_error(
            manager,
            errors,
            "internal lock parent symlink",
            lambda: manager.lock_file(target / ".nddev-opencode-lock" / "lock"),
            contains="lock parent",
        )
        if path_signature(manager, outside) != before:
            errors.append("internal lock parent symlink: outside directory changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-lock-file-") as raw:
        target = make_private_dir(Path(raw) / "target")
        lock_parent = make_private_dir(target / ".nddev-opencode-lock")
        outside_lock = Path(raw) / "outside-lock"
        outside_lock.write_text("outside", encoding="utf-8")
        (lock_parent / "lock").symlink_to(outside_lock)
        expect_manager_error(
            manager,
            errors,
            "internal lock file symlink",
            lambda: manager.lock_file(lock_parent / "lock"),
            contains="cannot open lock file safely",
        )
        (lock_parent / "lock").unlink()
        (lock_parent / "lock").write_text("", encoding="utf-8")
        os.chmod(lock_parent / "lock", 0o644)
        expect_manager_error(
            manager,
            errors,
            "internal lock unsafe mode",
            lambda: manager.lock_file(lock_parent / "lock"),
            contains="mode 0600",
        )
        (lock_parent / "lock").unlink()
        (lock_parent / "lock").write_text("", encoding="utf-8")
        os.chmod(lock_parent / "lock", 0o600)
        os.link(lock_parent / "lock", lock_parent / "lock-hardlink")
        expect_manager_error(
            manager,
            errors,
            "internal lock hardlink",
            lambda: manager.lock_file(lock_parent / "lock"),
            contains="hard-link",
        )


def check_download_smokes(manager: Any, errors: list[str]) -> None:
    cases = [
        ("missing length", {}, b"abc", "missing Content-Length"),
        ("malformed length", {"Content-Length": "abc"}, b"abc", "malformed Content-Length"),
        ("short length header", {"Content-Length": "2"}, b"abc", "Content-Length mismatch"),
        ("long length header", {"Content-Length": "4"}, b"abc", "Content-Length mismatch"),
        ("short body", {"Content-Length": "3"}, b"ab", "size mismatch"),
        ("long body", {"Content-Length": "3"}, b"abcd", "exceeds pinned size"),
    ]
    for label, headers, body, expected in cases:
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-download-smoke-") as raw:
            root = make_private_dir(Path(raw) / "download")
            destination = root / "artifact.zip"
            expect_manager_error(
                manager,
                errors,
                f"download {label}",
                lambda h=headers, b=body: manager.download_artifact(
                    "https://example.invalid/artifact.zip",
                    destination,
                    3,
                    opener=lambda request, timeout: FakeResponse(h, b),
                ),
                contains=expected,
            )
            if destination.exists() or destination.is_symlink():
                errors.append(f"download {label}: destination was left behind")
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-download-ok-") as raw:
        root = make_private_dir(Path(raw) / "download")
        destination = root / "artifact.zip"
        manager.download_artifact(
            "https://example.invalid/artifact.zip",
            destination,
            3,
            opener=lambda request, timeout: FakeResponse({"Content-Length": "3"}, b"abc"),
        )
        if destination.read_bytes() != b"abc" or stat.S_IMODE(destination.stat().st_mode) != 0o600:
            errors.append("download valid response: content or mode mismatch")


def backup_slot_count(manager: Any, target: Path) -> int:
    root = manager.backup_root(target)
    if not root.exists():
        return 0
    return sum(1 for child in root.iterdir() if child.is_dir())


def check_noop_and_backup_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-noop-smoke-") as raw:
        target = Path(raw) / "target"
        profile = manager.render_profile("full-auto")
        first = manager.install_or_switch(target, profile, operation="install")
        if first.get("changed") is not True:
            errors.append("initial install: expected changed=true")
        count = backup_slot_count(manager, target)
        before_identity = identity_mtime_signature(manager, target)
        before_backup = path_signature(manager, manager.backup_root(target))
        plan = manager.plan_payload(target, profile)
        if plan.get("changed") is not False or plan.get("changes") != []:
            errors.append("no-op plan: expected changed=false and empty changes")
        repeat_install = manager.install_or_switch(target, profile, operation="install")
        repeat_switch = manager.install_or_switch(target, profile, operation="switch")
        repeat_update = manager.install_or_switch(target, profile, operation="update")
        if (
            repeat_install.get("changed") is not False
            or repeat_install.get("changes") != []
            or repeat_install.get("backup") is not None
        ):
            errors.append("no-op install: expected no backup/write")
        if (
            repeat_switch.get("changed") is not False
            or repeat_switch.get("changes") != []
            or repeat_switch.get("backup") is not None
        ):
            errors.append("no-op switch: expected no backup/write")
        if (
            repeat_update.get("changed") is not False
            or repeat_update.get("changes") != []
            or repeat_update.get("backup") is not None
        ):
            errors.append("no-op update: expected no backup/write")
        if backup_slot_count(manager, target) != count:
            errors.append("no-op install/switch/update changed backup count")
        if identity_mtime_signature(manager, target) != before_identity:
            errors.append("no-op install/switch/update changed target inode/mtime/content")
        if path_signature(manager, manager.backup_root(target)) != before_backup:
            errors.append("no-op install/switch/update changed backup pool")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-cli-update-noop-") as raw:
        target = Path(raw).resolve(strict=False) / "target"
        manager.install_or_switch(target, manager.render_profile("safe"), operation="install")
        before_identity = identity_mtime_signature(manager, target)
        before_backup = path_signature(manager, manager.backup_root(target))
        original_detect = manager.detect_supported_host
        manager.detect_supported_host = fake_host
        try:
            stdout = io.StringIO()
            with contextlib.redirect_stdout(stdout):
                rc = manager.main(["update", "--target", str(target), "--json"])
            if rc != 0:
                errors.append(f"CLI target-only update returned {rc}")
            else:
                payload = json.loads(stdout.getvalue())
                if payload.get("changed") is not False or payload.get("profile_id") != "safe":
                    errors.append("CLI target-only update did not preserve safe no-op identity")
        finally:
            manager.detect_supported_host = original_detect
        stamp = manager.load_stamp(target)
        if stamp is None or stamp.get("profile_id") != "safe":
            errors.append("CLI target-only update changed installed profile")
        if identity_mtime_signature(manager, target) != before_identity:
            errors.append("CLI target-only update changed target inode/mtime/content")
        if path_signature(manager, manager.backup_root(target)) != before_backup:
            errors.append("CLI target-only update changed backup pool")

        original_detect = manager.detect_supported_host
        manager.detect_supported_host = fake_host
        try:
            stderr = io.StringIO()
            with contextlib.redirect_stderr(stderr):
                rc = manager.main(
                    [
                        "update",
                        "--target",
                        str(target),
                        "--profile",
                        "full-auto",
                        "--json",
                    ]
                )
            if rc != 2:
                errors.append("CLI update with selection flags must be rejected")
        finally:
            manager.detect_supported_host = original_detect
        if manager.load_stamp(target).get("profile_id") != "safe":
            errors.append("CLI update with selection flags changed installed profile")


def write_manual_backup(
    manager: Any, target: Path, slot: int, files: dict[str, str | None]
) -> None:
    root = manager.backup_root(target)
    slot_dir = root / str(slot)
    make_private_dir(root)
    make_private_dir(slot_dir)
    payload = {
        "schema_version": manager.BACKUP_SCHEMA,
        "product_name": manager.PRODUCT_NAME,
        "build_version": manager.VERSION,
        "slot": slot,
        "operation": "manual-validator-smoke",
        "canonical_target": str(target),
        "files": files,
        "sizes": {
            relative: len(value.encode("utf-8"))
            for relative, value in files.items()
            if value is not None
        },
        "digests": {
            relative: manager.sha256_bytes(value.encode("utf-8"))
            for relative, value in files.items()
            if value is not None
        },
    }
    manager.atomic_write(slot_dir / manager.BACKUP_NAME, manager.canonical_json(payload))


def check_backup_validation_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-mode-") as raw:
        target = Path(raw) / "target"
        make_private_dir(target)
        valid_files = {
            relative: None for relative in (*manager.KNOWN_MANAGED_FILES, manager.STAMP_NAME)
        }
        write_manual_backup(manager, target, 0, valid_files)
        manager.load_backup(target, 0)
        backup_file = manager.backup_slot_path(target, 0) / manager.BACKUP_NAME
        os.chmod(backup_file, 0o644)
        before = path_signature(manager, target)
        expect_manager_error(
            manager,
            errors,
            "backup validation unsafe envelope mode",
            lambda: manager.restore_target(target, 0),
            contains="mode 0600",
        )
        if path_signature(manager, target) != before:
            errors.append("backup validation unsafe envelope mode: target changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-base-") as raw:
        target = Path(raw) / "target"
        make_private_dir(target)
        valid_files = {
            relative: None for relative in (*manager.KNOWN_MANAGED_FILES, manager.STAMP_NAME)
        }
        write_manual_backup(manager, target, 0, valid_files)
        base = json.loads(
            (manager.backup_slot_path(target, 0) / manager.BACKUP_NAME).read_text(encoding="utf-8")
        )
        variants: list[tuple[str, dict[str, Any], str]] = []
        extra_key = dict(base)
        extra_key["extra"] = True
        variants.append(("extra envelope key", extra_key, "envelope"))
        missing_file = json.loads(json.dumps(base))
        missing_file["files"].pop("opencode.json")
        variants.append(("missing file key", missing_file, "known managed paths"))
        extra_file = json.loads(json.dumps(base))
        extra_file["files"]["../escape"] = None
        variants.append(("extra file key", extra_file, "known managed paths"))
        missing_sizes_key = json.loads(json.dumps(base))
        missing_sizes_key.pop("sizes")
        variants.append(("missing sizes key", missing_sizes_key, "envelope"))
        bad_digest_missing = json.loads(json.dumps(base))
        bad_digest_missing["files"]["opencode.json"] = "{}\n"
        bad_digest_missing["sizes"]["opencode.json"] = 3
        variants.append(("missing digest", bad_digest_missing, "exactly present files"))
        bad_size_missing = json.loads(json.dumps(base))
        bad_size_missing["files"]["opencode.json"] = "{}\n"
        bad_size_missing["digests"]["opencode.json"] = manager.sha256_bytes(b"{}\n")
        variants.append(("missing size", bad_size_missing, "exactly present files"))
        bad_size_extra = json.loads(json.dumps(base))
        bad_size_extra["sizes"]["opencode.json"] = 3
        variants.append(("extra absent size", bad_size_extra, "exactly present files"))
        bad_size_type = json.loads(json.dumps(base))
        bad_size_type["files"]["opencode.json"] = "{}\n"
        bad_size_type["sizes"]["opencode.json"] = "3"
        bad_size_type["digests"]["opencode.json"] = manager.sha256_bytes(b"{}\n")
        variants.append(("bad size type", bad_size_type, "size is invalid"))
        bad_size_value = json.loads(json.dumps(base))
        bad_size_value["files"]["opencode.json"] = "{}\n"
        bad_size_value["sizes"]["opencode.json"] = 4
        bad_size_value["digests"]["opencode.json"] = manager.sha256_bytes(b"{}\n")
        variants.append(("size mismatch", bad_size_value, "size mismatch"))
        bad_digest_extra = json.loads(json.dumps(base))
        bad_digest_extra["digests"]["opencode.json"] = "0" * 64
        variants.append(("extra absent digest", bad_digest_extra, "exactly present files"))
        bad_digest_value = json.loads(json.dumps(base))
        bad_digest_value["files"]["opencode.json"] = "{}\n"
        bad_digest_value["sizes"]["opencode.json"] = 3
        bad_digest_value["digests"]["opencode.json"] = "0" * 64
        variants.append(("corrupt digest", bad_digest_value, "digest mismatch"))
        bad_schema_type = json.loads(json.dumps(base))
        bad_schema_type["schema_version"] = "2"
        variants.append(("bad schema type", bad_schema_type, "identity or schema"))
        bad_slot_type = json.loads(json.dumps(base))
        bad_slot_type["slot"] = "0"
        variants.append(("bad slot type", bad_slot_type, "slot value"))
        bad_digest_type = json.loads(json.dumps(base))
        bad_digest_type["files"]["opencode.json"] = "{}\n"
        bad_digest_type["sizes"]["opencode.json"] = 3
        bad_digest_type["digests"]["opencode.json"] = 123
        variants.append(("bad digest type", bad_digest_type, "digest is invalid"))
        oversized_file = json.loads(json.dumps(base))
        oversized_file["files"]["opencode.json"] = "x" * (manager.MANAGED_PAYLOAD_MAX_BYTES + 1)
        oversized_file["sizes"]["opencode.json"] = len(
            oversized_file["files"]["opencode.json"].encode("utf-8")
        )
        oversized_file["digests"]["opencode.json"] = manager.sha256_bytes(
            oversized_file["files"]["opencode.json"].encode("utf-8")
        )
        variants.append(("oversized file value", oversized_file, "exceeds"))
    for label, template, expected in variants:
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-variant-") as raw:
            target = Path(raw) / "target"
            make_private_dir(target)
            valid_files = {
                relative: None for relative in (*manager.KNOWN_MANAGED_FILES, manager.STAMP_NAME)
            }
            write_manual_backup(manager, target, 0, valid_files)
            payload = json.loads(json.dumps(template))
            payload["canonical_target"] = str(target)
            manager.atomic_write(
                manager.backup_slot_path(target, 0) / manager.BACKUP_NAME,
                manager.canonical_json(payload),
            )
            before = path_signature(manager, target)
            expect_manager_error(
                manager,
                errors,
                f"backup validation {label}",
                lambda: manager.restore_target(target, 0),
                contains=expected,
            )
            if path_signature(manager, target) != before:
                errors.append(f"backup validation {label}: target changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-shape-") as raw:
        target = Path(raw) / "target"
        make_private_dir(target)
        valid_files = {
            relative: None for relative in (*manager.KNOWN_MANAGED_FILES, manager.STAMP_NAME)
        }
        write_manual_backup(manager, target, 0, valid_files)
        base = json.loads(
            (manager.backup_slot_path(target, 0) / manager.BACKUP_NAME).read_text(encoding="utf-8")
        )
        write_manual_backup(manager, target, 8, valid_files)
        extra = manager.backup_slot_path(target, 8) / "EXTRA"
        manager.atomic_write(extra, b"extra")
        expect_manager_error(
            manager,
            errors,
            "backup validation extra slot file",
            lambda: manager.restore_target(target, 8),
            contains="must contain exactly",
        )
        extra.unlink()
        invalid_slot = manager.backup_root(target) / "x"
        make_private_dir(invalid_slot)
        expect_manager_error(
            manager,
            errors,
            "backup validation invalid root slot",
            lambda: manager.backup_pool_payloads(target),
            contains="invalid slot",
        )
        invalid_slot.rmdir()
        gap_slot = manager.backup_root(target) / "6"
        make_private_dir(gap_slot)
        manager.atomic_write(
            gap_slot / manager.BACKUP_NAME, manager.canonical_json({**base, "slot": 6})
        )
        expect_manager_error(
            manager,
            errors,
            "backup validation slot gap",
            lambda: manager.backup_pool_payloads(target),
            contains="contiguous",
        )


def check_restore_transaction_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-restore-write-") as raw:
        root = Path(raw)
        target = root / "target"
        full_auto = manager.render_profile("full-auto")
        safe = manager.render_profile("safe")
        manager.install_or_switch(target, full_auto, operation="install")
        manager.install_or_switch(target, safe, operation="switch")
        backup = manager.load_backup(target, 0)
        write_points = [
            f"write:{relative}" for relative, value in backup["files"].items() if value is not None
        ]
        for point in write_points:
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, point)

            expect_manager_error(
                manager,
                errors,
                f"restore transaction {point}",
                lambda fault=fault: manager.restore_target(target, 0, fault_injection=fault),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"restore transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"restore transaction {point}: state changed after rollback")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-restore-remove-") as raw:
        root = Path(raw)
        target = root / "target"
        manager.install_or_switch(target, manager.render_profile("full-auto"), operation="install")
        absent_files = {
            relative: None for relative in (*manager.KNOWN_MANAGED_FILES, manager.STAMP_NAME)
        }
        write_manual_backup(manager, target, 0, absent_files)
        remove_points = [f"remove:{relative}" for relative in absent_files]
        for point in remove_points:
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, point)

            expect_manager_error(
                manager,
                errors,
                f"restore transaction {point}",
                lambda fault=fault: manager.restore_target(target, 0, fault_injection=fault),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"restore transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"restore transaction {point}: state changed after rollback")

    for rollback_point in (
        "rollback-managed:remove-new:opencode.json",
        "rollback-managed:restore:opencode.json",
        "rollback-managed:remove-stage",
        "rollback-managed:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-restore-rollback-write-") as raw:
            root = Path(raw)
            target = root / "target"
            manager.install_or_switch(
                target, manager.render_profile("full-auto"), operation="install"
            )
            manager.install_or_switch(target, manager.render_profile("safe"), operation="switch")
            before = state_bundle_signature(manager, root, target)
            before_identity = identity_mtime_signature(manager, target)
            fault, seen = one_shot_fault(manager, "write:opencode.json")
            rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
            expect_manager_error(
                manager,
                errors,
                f"restore rollback fault {rollback_point}",
                lambda fault=fault, rollback_fault=rollback_fault: manager.restore_target(
                    target,
                    0,
                    fault_injection=fault,
                    rollback_fault_injection=rollback_fault,
                ),
                contains="write:opencode.json",
            )
            if not seen["value"] or not rollback_seen["value"]:
                errors.append(f"restore rollback fault {rollback_point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"restore rollback fault {rollback_point}: state changed after retry")
            if identity_mtime_signature(manager, target) != before_identity:
                errors.append(f"restore rollback fault {rollback_point}: identity changed")


def check_managed_lifecycle_failure_smokes(manager: Any, errors: list[str]) -> None:
    for point in (
        "atomic:managed:opencode.json:temp-write",
        "atomic:managed:opencode.json:chmod",
        "atomic:managed:opencode.json:file-fsync",
        "atomic:managed:opencode.json:replace",
        "atomic:managed:opencode.json:parent-fsync",
        "atomic:managed:opencode.json:postcondition",
        "write:opencode.json",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-managed-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, point)
            expect_manager_error(
                manager,
                errors,
                f"managed install transaction {point}",
                lambda fault=fault: manager.install_or_switch(
                    target,
                    manager.render_profile("full-auto"),
                    operation="install",
                    fault_injection=fault,
                ),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"managed install transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"managed install transaction {point}: state changed after rollback")

    for rollback_point in (
        "rollback-managed:remove:opencode.json",
        "rollback-managed:remove-target",
        "rollback-managed:postcondition",
        "rollback-managed:remove-new:opencode.json",
        "rollback-managed:restore:opencode.json",
        "rollback-managed:remove-stage",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-managed-rollback-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            existing_target = rollback_point not in {
                "rollback-managed:remove:opencode.json",
                "rollback-managed:remove-target",
            }
            if existing_target:
                manager.install_or_switch(
                    target, manager.render_profile("full-auto"), operation="install"
                )
                before = state_bundle_signature(manager, root, target)
                before_identity = identity_mtime_signature(manager, target)
                original_fault, original_seen = one_shot_fault(manager, "write:opencode.json")
                rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
                operation = "switch"
                profile = manager.render_profile("safe")
            else:
                before = state_bundle_signature(manager, root, target)
                before_identity = identity_mtime_signature(manager, target)
                original_fault, original_seen = one_shot_fault(manager, "write:opencode.json")
                rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
                operation = "install"
                profile = manager.render_profile("full-auto")
            expect_manager_error(
                manager,
                errors,
                f"managed rollback fault {rollback_point}",
                lambda original_fault=original_fault, rollback_fault=rollback_fault, profile=profile, operation=operation: (
                    manager.install_or_switch(
                        target,
                        profile,
                        operation=operation,
                        fault_injection=original_fault,
                        rollback_fault_injection=rollback_fault,
                    )
                ),
                contains="write:opencode.json",
            )
            if not original_seen["value"] or not rollback_seen["value"]:
                errors.append(f"managed rollback fault {rollback_point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"managed rollback fault {rollback_point}: state changed after retry")
            if identity_mtime_signature(manager, target) != before_identity:
                errors.append(f"managed rollback fault {rollback_point}: identity changed")


def check_backup_transaction_smokes(manager: Any, errors: list[str]) -> None:
    for point in (
        "atomic:backup:0:temp-write",
        "atomic:backup:0:chmod",
        "atomic:backup:0:file-fsync",
        "atomic:backup:0:replace",
        "atomic:backup:0:parent-fsync",
        "atomic:backup:0:postcondition",
        "backup:prepare-slot:0",
        "backup:prepare-postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-prepare-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            manager.install_or_switch(
                target, manager.render_profile("full-auto"), operation="install"
            )
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, point)
            expect_manager_error(
                manager,
                errors,
                f"backup prepare transaction {point}",
                lambda fault=fault: manager.install_or_switch(
                    target,
                    manager.render_profile("safe"),
                    operation="switch",
                    backup_fault_injection=fault,
                ),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"backup prepare transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"backup prepare transaction {point}: state changed after rollback")

    for point in ("backup:move-old-root", "backup:replace-root", "backup:postcondition"):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-commit-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            manager.install_or_switch(
                target, manager.render_profile("full-auto"), operation="install"
            )
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, point)
            expect_manager_error(
                manager,
                errors,
                f"backup commit transaction {point}",
                lambda fault=fault: manager.install_or_switch(
                    target,
                    manager.render_profile("safe"),
                    operation="switch",
                    backup_fault_injection=fault,
                ),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"backup commit transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"backup commit transaction {point}: state changed after rollback")

    for rollback_point in (
        "rollback-backup:remove-new-root",
        "rollback-backup:restore-previous-root",
        "rollback-backup:remove-staging-root",
        "rollback-backup:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-rollback-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            manager.install_or_switch(
                target, manager.render_profile("full-auto"), operation="install"
            )
            before = state_bundle_signature(manager, root, target)
            original_point = (
                "backup:move-old-root"
                if rollback_point == "rollback-backup:remove-staging-root"
                else "backup:replace-root"
            )
            fault, seen = one_shot_fault(manager, original_point)
            rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
            expect_manager_error(
                manager,
                errors,
                f"backup rollback fault {rollback_point}",
                lambda fault=fault, rollback_fault=rollback_fault: manager.install_or_switch(
                    target,
                    manager.render_profile("safe"),
                    operation="switch",
                    backup_fault_injection=fault,
                    backup_rollback_fault_injection=rollback_fault,
                ),
                contains=original_point,
            )
            if not seen["value"] or not rollback_seen["value"]:
                errors.append(f"backup rollback fault {rollback_point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"backup rollback fault {rollback_point}: state changed after retry")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-cleanup-fault-") as raw:
        root = Path(raw)
        target = root / "target"
        manager.install_or_switch(target, manager.render_profile("full-auto"), operation="install")
        fault, seen = one_shot_fault(manager, "backup:cleanup-old-root")
        result = manager.install_or_switch(
            target,
            manager.render_profile("safe"),
            operation="switch",
            backup_fault_injection=fault,
        )
        if not seen["value"]:
            errors.append("backup old-root cleanup fault: fault point not reached")
        if result.get("changed") is not True or result.get("backup") != 0:
            errors.append("backup old-root cleanup fault: success payload mismatch")
        manager.backup_pool_payloads(target)
        residue = sorted(
            child.name
            for child in target.parent.iterdir()
            if child.name.startswith(f".{target.name}.nddev-opencode-backups-previous.")
            or child.name.startswith(f".{target.name}.nddev-opencode-backups-stage.")
        )
        if residue:
            errors.append(f"backup old-root cleanup fault: residue remains {residue}")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-backup-full-pool-") as raw:
        root = Path(raw)
        target = root / "target"
        manager.install_or_switch(target, manager.render_profile("full-auto"), operation="install")
        for index in range(manager.MAX_BACKUPS + 1):
            profile = manager.render_profile("safe" if index % 2 == 0 else "full-auto")
            manager.install_or_switch(target, profile, operation="switch")
        before = state_bundle_signature(manager, root, target)
        fault, seen = one_shot_fault(manager, "backup:replace-root")
        expect_manager_error(
            manager,
            errors,
            "backup full-pool failed replacement",
            lambda fault=fault: manager.install_or_switch(
                target,
                manager.render_profile("full-auto"),
                operation="switch",
                backup_fault_injection=fault,
            ),
            contains="backup:replace-root",
        )
        if not seen["value"]:
            errors.append("backup full-pool failed replacement: fault point not reached")
        if state_bundle_signature(manager, root, target) != before:
            errors.append("backup full-pool failed replacement: backup/target changed")


def check_plan_mutation_parity_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-plan-parity-") as raw:
        target = Path(raw) / "target"
        profile = manager.render_profile("full-auto")
        plan = manager.plan_payload(target, profile)
        result = manager.install_or_switch(target, profile, operation="install")
        if plan.get("changes") != result.get("changes"):
            errors.append("plan/install parity: initial changed list mismatch")
        repeat_plan = manager.plan_payload(target, profile)
        repeat_result = manager.install_or_switch(target, profile, operation="install")
        if (
            repeat_plan.get("changes") != repeat_result.get("changes")
            or repeat_plan.get("changes") != []
        ):
            errors.append("plan/install parity: no-op changed list mismatch")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-plan-drift-parity-") as raw:
        target = Path(raw) / "target"
        manager.install_or_switch(target, manager.render_profile("full-auto"), operation="install")
        opencode = target / "opencode.json"
        config = json.loads(opencode.read_text(encoding="utf-8"))
        config["model"] = "preserved-user-model"
        manager.atomic_write(opencode, manager.canonical_json(config))
        profile = manager.render_profile("safe")
        plan = manager.plan_payload(target, profile)
        result = manager.install_or_switch(target, profile, operation="install")
        if plan.get("changes") != result.get("changes"):
            errors.append("plan/install parity: config-drift changed list mismatch")
        final_config = json.loads(opencode.read_text(encoding="utf-8"))
        if final_config.get("model") != "preserved-user-model":
            errors.append("plan/install parity: unmanaged config key was not preserved")


def check_platform_preflight_smokes(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-platform-install-") as raw:
        root = Path(raw)
        target = root / "target"
        called = {"metadata": False, "download": False}

        def unsupported_host() -> dict[str, Any]:
            return manager.select_supported_host(
                system="linux",
                machine="x86_64",
                os_release={"ID": "debian"},
                libc=("glibc", "2.36"),
                avx2=True,
            )

        def metadata_fetcher() -> dict[str, Any]:
            called["metadata"] = True
            return {}

        def artifact_downloader(url: str, destination: Path, expected_size: int) -> None:
            called["download"] = True
            raise manager.ManagerError("download should not run")

        before = state_bundle_signature(manager, root, target)
        expect_manager_error(
            manager,
            errors,
            "platform install preflight",
            lambda: manager.install_cli(
                target,
                update=False,
                host_detector=unsupported_host,
                metadata_fetcher=metadata_fetcher,
                artifact_downloader=artifact_downloader,
            ),
            contains="non-ubuntu-linux",
        )
        if called["metadata"] or called["download"]:
            errors.append("platform install preflight: network/download callback ran")
        if state_bundle_signature(manager, root, target) != before:
            errors.append("platform install preflight: target/stage changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-platform-launch-") as raw:
        root = Path(raw)
        target = root / "target"
        before = (state_bundle_signature(manager, root, target), lock_signature(manager, target))
        original = manager.detect_supported_host

        def unsupported_launch_host() -> dict[str, Any]:
            raise manager.ManagerError(
                "unsupported product host (non-ubuntu-linux): validator smoke"
            )

        manager.detect_supported_host = unsupported_launch_host
        try:
            expect_manager_error(
                manager,
                errors,
                "platform launch preflight",
                lambda: manager.launch(target, []),
                contains="non-ubuntu-linux",
            )
        finally:
            manager.detect_supported_host = original
        after = (state_bundle_signature(manager, root, target), lock_signature(manager, target))
        if after != before:
            errors.append("platform launch preflight: target/lock changed")

    for category in (
        "windows",
        "non-ubuntu-linux",
        "linux-musl",
        "unsupported-architecture",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-cli-host-gate-") as raw:
            root = Path(raw)
            target = root / "target"
            called: list[str] = []

            def unsupported_cli_host(category: str = category) -> dict[str, Any]:
                raise manager.ManagerError(
                    f"unsupported product host ({category}): validator smoke"
                )

            def forbidden_resolve(raw_target: str | None) -> Path:
                called.append(f"resolve:{raw_target}")
                raise manager.ManagerError("resolve_target should not run")

            def forbidden_locks(target: Path, *, create_target: bool) -> Any:
                called.append(f"locks:{target}:{create_target}")
                raise manager.ManagerError("target_locks should not run")

            commands = [
                ["status", "--target", str(target), "--json"],
                ["software-status", "--target", str(target), "--json"],
                ["plan", "--target", str(target), "--json"],
                ["install", "--target", str(target), "--json"],
                ["update", "--target", str(target), "--json"],
                ["switch", "--target", str(target), "--json"],
                ["migrate", "--target", str(target), "--json"],
                ["restore", "--target", str(target), "--backup", "0", "--json"],
                ["remove", "--target", str(target), "--json"],
                ["remove-cli", "--target", str(target), "--json"],
                ["install-cli", "--target", str(target), "--json"],
                ["update-cli", "--target", str(target), "--json"],
                ["launch", "--target", str(target), "--", "--version"],
            ]
            original_detect = manager.detect_supported_host
            original_resolve = manager.resolve_target
            original_locks = manager.target_locks
            manager.detect_supported_host = unsupported_cli_host
            manager.resolve_target = forbidden_resolve
            manager.target_locks = forbidden_locks
            try:
                for argv in commands:
                    before = state_bundle_signature(manager, root, target)
                    stderr = io.StringIO()
                    with contextlib.redirect_stderr(stderr):
                        rc = manager.main(argv)
                    if rc != 2:
                        errors.append(f"CLI host gate {category} returned {rc}: {argv}")
                    if f"({category})" not in stderr.getvalue():
                        errors.append(f"CLI host gate {category} category missing: {argv}")
                    if called:
                        errors.append(f"CLI host gate {category} ran target operation: {called}")
                    if state_bundle_signature(manager, root, target) != before:
                        errors.append(f"CLI host gate {category} changed target/root: {argv}")
            finally:
                manager.detect_supported_host = original_detect
                manager.resolve_target = original_resolve
                manager.target_locks = original_locks


def check_lock_failure_cleanup_smokes(manager: Any, errors: list[str]) -> None:
    for create_target in (False, True):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-lock-cleanup-") as raw:
            root = Path(raw)
            target = root / "target"
            if not create_target:
                make_private_dir(target)
            before = (
                state_bundle_signature(manager, root, target),
                lock_signature(manager, target),
            )
            expect_manager_error(
                manager,
                errors,
                f"target lock cleanup create={create_target}",
                lambda create_target=create_target: _raise_inside_lock(
                    manager, target, create_target
                ),
                contains="validator lock failure",
            )
            after = (state_bundle_signature(manager, root, target), lock_signature(manager, target))
            if after != before:
                errors.append(f"target lock cleanup create={create_target}: state/lock changed")


def _raise_inside_lock(manager: Any, target: Path, create_target: bool) -> None:
    with manager.target_locks(target, create_target=create_target):
        raise manager.ManagerError("validator lock failure")


def check_software_transaction_smokes(manager: Any, errors: list[str]) -> None:
    fault_points = [
        "software:swap-current",
        "atomic:software:entrypoint:chmod",
        "atomic:software:entrypoint:file-fsync",
        "atomic:software:entrypoint:replace",
        "atomic:software:entrypoint:parent-fsync",
        "atomic:software:entrypoint:postcondition",
        "software:copy-entrypoint",
        "software:chmod-entrypoint",
        "atomic:software:stamp:chmod",
        "atomic:software:stamp:file-fsync",
        "atomic:software:stamp:replace",
        "atomic:software:stamp:parent-fsync",
        "atomic:software:stamp:postcondition",
        "software:stamp",
        "software:fsync",
    ]
    cases = [
        ("install-cli existing target", False, True),
        ("install-cli absent target", False, False),
        ("update-cli", True, True),
    ]
    for label, update, create_target in cases:
        for point in fault_points:
            with tempfile.TemporaryDirectory(prefix="nddev-opencode-software-fault-") as raw:
                root = Path(raw)
                target = root / "target"
                if create_target:
                    make_private_dir(target)
                if update:
                    seed_current_software(manager, target, current=False)
                before = state_bundle_signature(manager, root, target)
                fault, seen = one_shot_fault(manager, point)

                expect_manager_error(
                    manager,
                    errors,
                    f"{label} transaction {point}",
                    lambda fault=fault, update=update: manager.install_cli(
                        target,
                        update=update,
                        fault_injection=fault,
                        **fake_install_kwargs(manager),
                    ),
                    contains=point,
                )
                if not seen["value"]:
                    errors.append(f"software transaction {point}: fault point not reached")
                if state_bundle_signature(manager, root, target) != before:
                    errors.append(f"{label} transaction {point}: state changed after rollback")

    for rollback_point in (
        "rollback-software:remove-new-current",
        "rollback-software:restore-previous-current",
        "rollback-software:remove-new:OpenCode entrypoint",
        "rollback-software:restore:OpenCode entrypoint",
        f"rollback-software:remove-new:{manager.SOFTWARE_STAMP_NAME}",
        f"rollback-software:restore:{manager.SOFTWARE_STAMP_NAME}",
        "rollback-software:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-software-rollback-fault-") as raw:
            root = Path(raw)
            target = make_private_dir(root / "target")
            seed_current_software(manager, target, current=False)
            before = state_bundle_signature(manager, root, target)
            before_identity = software_identity_signature(manager, target)
            fault, seen = one_shot_fault(manager, "software:stamp")
            rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
            expect_manager_error(
                manager,
                errors,
                f"software rollback fault {rollback_point}",
                lambda fault=fault, rollback_fault=rollback_fault: manager.install_cli(
                    target,
                    update=True,
                    fault_injection=fault,
                    rollback_fault_injection=rollback_fault,
                    **fake_install_kwargs(manager),
                ),
                contains="software:stamp",
            )
            if not seen["value"] or not rollback_seen["value"]:
                errors.append(f"software rollback fault {rollback_point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(
                    f"software rollback fault {rollback_point}: state changed after retry"
                )
            if software_identity_signature(manager, target) != before_identity:
                errors.append(
                    f"software rollback fault {rollback_point}: identity changed after retry"
                )

    for rollback_point in (
        "rollback-software:unlink:OpenCode entrypoint",
        f"rollback-software:unlink:{manager.SOFTWARE_STAMP_NAME}",
        "rollback-software:remove-bin-dir",
        "rollback-software:remove-software-root",
        "rollback-software:remove-target",
        "rollback-software:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-software-fresh-rollback-") as raw:
            root = Path(raw)
            target = root / "target"
            before = state_bundle_signature(manager, root, target)
            fault, seen = one_shot_fault(manager, "software:stamp")
            rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
            expect_manager_error(
                manager,
                errors,
                f"fresh software rollback fault {rollback_point}",
                lambda fault=fault, rollback_fault=rollback_fault: manager.install_cli(
                    target,
                    update=False,
                    fault_injection=fault,
                    rollback_fault_injection=rollback_fault,
                    **fake_install_kwargs(manager),
                ),
                contains="software:stamp",
            )
            if not seen["value"] or not rollback_seen["value"]:
                errors.append(
                    f"fresh software rollback fault {rollback_point}: fault point not reached"
                )
            if state_bundle_signature(manager, root, target) != before:
                errors.append(
                    f"fresh software rollback fault {rollback_point}: state changed after retry"
                )

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-software-cleanup-fault-") as raw:
        target = make_private_dir(Path(raw) / "target")
        seed_current_software(manager, target, current=False)
        fault, seen = one_shot_fault(manager, "software:cleanup-previous-current")
        kwargs = fake_install_kwargs(manager)
        artifact = kwargs["artifact_resolver"]("linux-x64")
        original_artifact = manager.ARTIFACTS["linux-x64"]
        manager.ARTIFACTS["linux-x64"] = artifact
        try:
            result = manager.install_cli(
                target,
                update=True,
                fault_injection=fault,
                **kwargs,
            )
            status = manager.software_status_payload(target)
        finally:
            manager.ARTIFACTS["linux-x64"] = original_artifact
        if not seen["value"]:
            errors.append("software previous-current cleanup fault: fault point not reached")
        if result.get("changed") is not True:
            errors.append("software previous-current cleanup fault: success payload mismatch")
        if status.get("current") is not True:
            errors.append("software previous-current cleanup fault: status drifted")
        previous_residue = [
            child.name
            for child in (target / manager.SOFTWARE_DIR_NAME).iterdir()
            if child.name.startswith(".previous.")
        ]
        if previous_residue:
            errors.append("software previous-current cleanup fault: previous residue remains")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-software-noop-") as raw:
        target = make_private_dir(Path(raw) / "target")
        seed_current_software(manager, target, current=True)
        before = identity_mtime_signature(manager, target)
        called = {"metadata": False}

        def metadata_fetcher() -> dict[str, Any]:
            called["metadata"] = True
            raise manager.ManagerError("metadata should not be fetched for no-op")

        for update in (False, True):
            result = manager.install_cli(
                target,
                update=update,
                metadata_fetcher=metadata_fetcher,
                **{
                    key: value
                    for key, value in fake_install_kwargs(manager).items()
                    if key != "metadata_fetcher"
                },
            )
            if result.get("changed") is not False:
                errors.append(f"software no-op update={update}: expected changed=false")
        if called["metadata"]:
            errors.append("software no-op fetched metadata")
        if identity_mtime_signature(manager, target) != before:
            errors.append("software no-op changed inode/mtime/content")


def remove_cli_stage_residue(manager: Any, target: Path) -> list[str]:
    if not target.exists() or not target.is_dir():
        return []
    return sorted(
        child.name
        for child in target.iterdir()
        if child.name.startswith(".nddev-opencode-software-remove-stage.")
    )


def check_remove_cli_transaction_smokes(manager: Any, errors: list[str]) -> None:
    for point in (
        "remove-cli:unlink-stamp",
        "remove-cli:stamp-parent-fsync",
        "remove-cli:unlink-entrypoint",
        "remove-cli:entrypoint-parent-fsync",
        "remove-cli:tree-swap",
        "remove-cli:software-parent-fsync",
        "remove-cli:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-remove-cli-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            seed_current_software(manager, target, current=True)
            before = state_bundle_signature(manager, root, target)
            before_identity = software_identity_signature(manager, target)
            fault, seen = one_shot_fault(manager, point)
            expect_manager_error(
                manager,
                errors,
                f"remove-cli transaction {point}",
                lambda fault=fault: manager.remove_cli(target, fault_injection=fault),
                contains=point,
            )
            if not seen["value"]:
                errors.append(f"remove-cli transaction {point}: fault point not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(f"remove-cli transaction {point}: state changed after rollback")
            if software_identity_signature(manager, target) != before_identity:
                errors.append(
                    f"remove-cli transaction {point}: software identity changed after rollback"
                )
            if remove_cli_stage_residue(manager, target):
                errors.append(f"remove-cli transaction {point}: stage residue remains")

    for rollback_point in (
        "rollback-remove-cli:restore-software-root",
        "rollback-remove-cli:restore-entrypoint",
        "rollback-remove-cli:restore-stamp",
        "rollback-remove-cli:remove-stage",
        "rollback-remove-cli:postcondition",
    ):
        with tempfile.TemporaryDirectory(prefix="nddev-opencode-remove-cli-rollback-fault-") as raw:
            root = Path(raw)
            target = root / "target"
            seed_current_software(manager, target, current=True)
            before = state_bundle_signature(manager, root, target)
            before_identity = software_identity_signature(manager, target)
            fault, seen = one_shot_fault(manager, "remove-cli:postcondition")
            rollback_fault, rollback_seen = one_shot_fault(manager, rollback_point)
            expect_manager_error(
                manager,
                errors,
                f"remove-cli rollback fault {rollback_point}",
                lambda fault=fault, rollback_fault=rollback_fault: manager.remove_cli(
                    target,
                    fault_injection=fault,
                    rollback_fault_injection=rollback_fault,
                ),
                contains="remove-cli:postcondition",
            )
            if not seen["value"] or not rollback_seen["value"]:
                errors.append(f"remove-cli rollback fault {rollback_point}: fault not reached")
            if state_bundle_signature(manager, root, target) != before:
                errors.append(
                    f"remove-cli rollback fault {rollback_point}: state changed after retry"
                )
            if software_identity_signature(manager, target) != before_identity:
                errors.append(
                    f"remove-cli rollback fault {rollback_point}: software identity changed"
                )
            if remove_cli_stage_residue(manager, target):
                errors.append(f"remove-cli rollback fault {rollback_point}: stage residue remains")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-remove-cli-cleanup-fault-") as raw:
        root = Path(raw)
        target = root / "target"
        seed_current_software(manager, target, current=True)
        fault, seen = one_shot_fault(manager, "remove-cli:cleanup-stage")
        result = manager.remove_cli(target, fault_injection=fault)
        if not seen["value"]:
            errors.append("remove-cli cleanup fault: fault point not reached")
        if result.get("changed") is not True or result.get("removed") is not True:
            errors.append("remove-cli cleanup fault: success payload mismatch")
        if manager.software_presence(target):
            errors.append("remove-cli cleanup fault: software presence remains")
        if remove_cli_stage_residue(manager, target):
            errors.append("remove-cli cleanup fault: stage residue remains")
        if path_signature(manager, manager.backup_root(target)) is not None:
            errors.append("remove-cli cleanup fault: backup pool changed")

    with tempfile.TemporaryDirectory(prefix="nddev-opencode-remove-cli-noop-") as raw:
        root = Path(raw)
        target = make_private_dir(root / "target")
        before = state_bundle_signature(manager, root, target)
        before_identity = identity_mtime_signature(manager, target)
        result = manager.remove_cli(target)
        if result.get("changed") is not False or result.get("removed") is not False:
            errors.append("remove-cli no-op: expected changed=false removed=false")
        if state_bundle_signature(manager, root, target) != before:
            errors.append("remove-cli no-op: target/root/backup changed")
        if identity_mtime_signature(manager, target) != before_identity:
            errors.append("remove-cli no-op: target inode/mtime changed")


def check_json_parse_smokes(errors: list[str]) -> None:
    script = ROOT / "cli-tools" / "nddev_opencode.py"
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-json-parse-") as raw:
        target = Path(raw) / "target"
        commands = [
            python_cli_argv(script, "status", "--json"),
            python_cli_argv(script, "status", "--target", str(target), "--json", "--stale"),
            python_cli_argv(script, "status", "--target", "--json"),
            python_cli_argv(
                script, "plan", "--target", str(target), "--profile", "missing", "--json"
            ),
        ]
        for argv in commands:
            completed = subprocess.run(
                argv,
                env=subprocess_clean_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 2:
                errors.append(f"JSON parse smoke returned {completed.returncode}: {argv}")
                continue
            try:
                payload = json.loads(completed.stderr)
            except json.JSONDecodeError as exc:
                errors.append(f"JSON parse smoke emitted non-JSON stderr: {argv}: {exc}")
                continue
            if payload.get("ok") is not False or not isinstance(payload.get("error"), str):
                errors.append(f"JSON parse smoke payload mismatch: {argv}")
            if completed.stdout:
                errors.append(f"JSON parse smoke wrote stdout: {argv}")
        non_json = subprocess.run(
            python_cli_argv(script, "status"),
            env=subprocess_clean_env(),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if non_json.returncode != 2 or "usage:" not in non_json.stderr:
            errors.append("non-JSON argparse behavior changed unexpectedly")


def cleanup_validator_lock_file(manager: Any, path: Path, existed_before: bool) -> None:
    if existed_before or not (path.exists() or path.is_symlink()):
        return
    manager.require_regular_file(path, f"validator-owned lock {path}", private=True)
    path.unlink()
    manager.fsync_directory(path.parent)


def cleanup_validator_empty_lock_root(manager: Any, existed_before: bool) -> None:
    root = manager.system_lock_root()
    if existed_before or not (root.exists() or root.is_symlink()):
        return
    manager.require_real_private_directory(root, "validator-owned lock root")
    if any(root.iterdir()):
        return
    root.rmdir()
    manager.fsync_directory(root.parent)


def check_read_only_lock_smoke(manager: Any, errors: list[str]) -> None:
    script = ROOT / "cli-tools" / "nddev_opencode.py"
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-readonly-lock-") as raw:
        target = Path(raw) / "target"
        token = manager.sha256_bytes(str(target.resolve(strict=False)).encode("utf-8"))
        external_lock = manager.system_lock_root() / f"{token}.lock"
        coordination_lock = manager.coordination_lock_path()
        lock_root_existed = (
            manager.system_lock_root().exists() or manager.system_lock_root().is_symlink()
        )
        coordination_existed = coordination_lock.exists() or coordination_lock.is_symlink()
        external_existed = external_lock.exists() or external_lock.is_symlink()
        if external_lock.exists() or external_lock.is_symlink():
            errors.append(
                "read-only lock smoke: unique external lock unexpectedly exists before run"
            )
            return
        try:
            completed = subprocess.run(
                python_cli_argv(script, "status", "--target", str(target), "--json"),
                env=subprocess_clean_env(),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                check=False,
            )
            if completed.returncode != 0:
                errors.append(f"read-only lock smoke failed: {completed.stderr}")
                return
            if target.exists() or target.is_symlink():
                errors.append("read-only lock smoke: status created target")
            if not (external_lock.exists() or external_lock.is_symlink()):
                errors.append("read-only lock smoke: status did not create external lock")
        finally:
            try:
                cleanup_validator_lock_file(manager, external_lock, external_existed)
                cleanup_validator_lock_file(manager, coordination_lock, coordination_existed)
                cleanup_validator_empty_lock_root(manager, lock_root_existed)
            except BaseException as exc:  # noqa: BLE001 - cleanup failure is a validator failure.
                errors.append(f"read-only lock smoke cleanup failed: {exc}")


def check_lifecycle_order_smoke(manager: Any, errors: list[str]) -> None:
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-order-smoke-") as raw:
        target = Path(raw) / "target"
        events: list[str] = []
        original_detect = manager.detect_supported_host
        original_resolve = manager.resolve_target
        original_resolve_locked = manager.resolve_target_locked
        original_lock_file = manager.lock_file
        original_status = manager.current_status

        def traced_detect() -> dict[str, Any]:
            events.append("host")
            return fake_host()

        def traced_resolve(raw_target: str | None) -> Path:
            events.append("lexical-target")
            if events != ["host", "lexical-target"]:
                errors.append(f"lifecycle order: lexical target ran out of order: {events}")
            return original_resolve(raw_target)

        def traced_lock_file(path: Path) -> Any:
            events.append(f"lock:{path.name}")
            return original_lock_file(path)

        def traced_resolve_locked(path: Path) -> Path:
            events.append("locked-resolve")
            if "lock:.coordination.lock" not in events:
                errors.append(
                    "lifecycle order: locked target resolution preceded coordination lock"
                )
            return original_resolve_locked(path)

        def traced_status(path: Path) -> dict[str, Any]:
            events.append("status-read")
            external_seen = any(
                event.startswith("lock:")
                and event != "lock:.coordination.lock"
                and event.endswith(".lock")
                for event in events
            )
            if not external_seen:
                errors.append("lifecycle order: status read preceded canonical external lock")
            return original_status(path)

        lock_root_existed = (
            manager.system_lock_root().exists() or manager.system_lock_root().is_symlink()
        )
        coordination_lock = manager.coordination_lock_path()
        coordination_existed = coordination_lock.exists() or coordination_lock.is_symlink()
        token = manager.sha256_bytes(str(target.resolve(strict=False)).encode("utf-8"))
        external_lock = manager.system_lock_root() / f"{token}.lock"
        external_existed = external_lock.exists() or external_lock.is_symlink()
        manager.detect_supported_host = traced_detect
        manager.resolve_target = traced_resolve
        manager.resolve_target_locked = traced_resolve_locked
        manager.lock_file = traced_lock_file
        manager.current_status = traced_status
        try:
            with (
                contextlib.redirect_stdout(io.StringIO()),
                contextlib.redirect_stderr(io.StringIO()),
            ):
                rc = manager.main(["status", "--target", str(target), "--json"])
        finally:
            manager.detect_supported_host = original_detect
            manager.resolve_target = original_resolve
            manager.resolve_target_locked = original_resolve_locked
            manager.lock_file = original_lock_file
            manager.current_status = original_status
            try:
                cleanup_validator_lock_file(manager, external_lock, external_existed)
                cleanup_validator_lock_file(manager, coordination_lock, coordination_existed)
                cleanup_validator_empty_lock_root(manager, lock_root_existed)
            except BaseException as exc:  # noqa: BLE001 - cleanup failure is a validator failure.
                errors.append(f"lifecycle order smoke cleanup failed: {exc}")
        if rc != 0:
            errors.append("lifecycle order smoke failed")
            return
        expected_prefix = ["host", "lexical-target", "lock:.coordination.lock", "locked-resolve"]
        if events[:4] != expected_prefix:
            errors.append(f"lifecycle order prefix mismatch: {events}")
        if not events or events[-1] != "status-read":
            errors.append(f"lifecycle order status read missing or out of order: {events}")


def check_cli_failure_lock_cleanup_smoke(manager: Any, errors: list[str]) -> None:
    script = ROOT / "cli-tools" / "nddev_opencode.py"
    with tempfile.TemporaryDirectory(prefix="nddev-opencode-cli-error-lock-") as raw:
        root = Path(raw)
        target = make_private_dir(root / "target")
        manager.atomic_write(
            target / "opencode.json", manager.canonical_json({"permission": "ask"})
        )
        before = (state_bundle_signature(manager, root, target), lock_signature(manager, target))
        completed = subprocess.run(
            python_cli_argv(script, "install", "--target", str(target), "--json"),
            env=subprocess_clean_env({"HOME": str(root / "home")}),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            check=False,
        )
        if completed.returncode == 0:
            errors.append("CLI failure lock cleanup smoke unexpectedly succeeded")
        try:
            payload = json.loads(completed.stderr)
        except json.JSONDecodeError as exc:
            errors.append(f"CLI failure lock cleanup smoke emitted non-JSON stderr: {exc}")
        else:
            if payload.get("ok") is not False:
                errors.append("CLI failure lock cleanup smoke JSON payload mismatch")
        after = (state_bundle_signature(manager, root, target), lock_signature(manager, target))
        if after != before:
            errors.append("CLI failure lock cleanup smoke left target/backup/lock residue")


def check_adversarial_smokes(errors: list[str]) -> None:
    manager = load_manager(errors)
    if manager is None:
        return
    check_fsync_fail_closed_smokes(manager, errors)
    check_path_and_lock_smokes(manager, errors)
    check_download_smokes(manager, errors)
    check_noop_and_backup_smokes(manager, errors)
    check_backup_validation_smokes(manager, errors)
    check_restore_transaction_smokes(manager, errors)
    check_managed_lifecycle_failure_smokes(manager, errors)
    check_backup_transaction_smokes(manager, errors)
    check_plan_mutation_parity_smokes(manager, errors)
    check_platform_preflight_smokes(manager, errors)
    check_lifecycle_order_smoke(manager, errors)
    check_lock_failure_cleanup_smokes(manager, errors)
    check_software_transaction_smokes(manager, errors)
    check_remove_cli_transaction_smokes(manager, errors)
    check_json_parse_smokes(errors)
    check_read_only_lock_smoke(manager, errors)
    check_cli_failure_lock_cleanup_smoke(manager, errors)


def main() -> int:
    errors: list[str] = []
    version_file = ROOT / "VERSION"
    version_text = (
        version_file.read_text(encoding="utf-8").strip() if version_file.is_file() else ""
    )
    if version_text != VERSION_TEXT:
        errors.append("VERSION must be 0.2.0")

    version = load_json("build/version.json", errors)
    manifest = load_json("build/manifest.json", errors)
    contract = load_json("config/nddev-contract.json", errors)
    baseline = load_json("references/opencode-baseline.json", errors)

    check_release(version, baseline, errors)
    check_manifest(manifest, errors)
    check_contract(contract, errors)
    check_host_selection_smokes(errors)
    check_adversarial_smokes(errors)
    check_setup(errors)
    for profile_id in PROFILE_IDS:
        check_profile(profile_id, errors)
    for relative in (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "cli-tools/nddev_opencode.py",
        "cli-tools/validate_public_contracts.py",
        ".claude/CLAUDE.md",
    ):
        check_text(relative, errors)
    for executable in ("cli-tools/nddev_opencode.py", "cli-tools/validate_public_contracts.py"):
        check_executable(executable, errors)
    for workflow in WORKFLOWS:
        check_text(f".github/workflows/{workflow}", errors)
    check_no_forbidden_public_paths(errors)

    if errors:
        print(f"validate_public_contracts.py: FAIL ({len(errors)} error(s))")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
