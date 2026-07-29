#!/usr/bin/env python3
"""Validate the public OpenCode module using static, archive-local inputs only."""

from __future__ import annotations

import ast
import json
import re
import stat
import sys
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
SETUP_IDS = ["nddev-builder"]
PROFILE_IDS = ["full-auto", "safe"]
DEFAULT_PROFILE = "full-auto"
CONTENT_FILES = [
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "skills/nddev-builder/references/native-surfaces.md",
    "skills/nddev-builder/references/security-boundary.md",
    "agents/nddev-builder.md",
    "commands/nddev-orient.md",
    "commands/nddev-validate.md",
]
MANAGED_FILES = ["opencode.json", *CONTENT_FILES]
SUPPORTED_ARTIFACTS = {
    "darwin-arm64": (".zip", "macos-arm64", False),
    "darwin-x64": (".zip", "macos-x64", False),
    "darwin-x64-baseline": (".zip", "macos-x64", True),
    "linux-arm64": (".tar.gz", "ubuntu-glibc-arm64", False),
    "linux-x64": (".tar.gz", "ubuntu-glibc-x64", False),
    "linux-x64-baseline": (".tar.gz", "ubuntu-glibc-x64", True),
}
SUPPORTED_HOSTS = [
    "macos-arm64",
    "macos-x64",
    "ubuntu-glibc-arm64",
    "ubuntu-glibc-x64",
]
UNSUPPORTED_HOSTS = [
    "windows",
    "non-ubuntu-linux",
    "linux-musl",
    "unsupported-architecture",
]
SOURCE_USED_RUNTIME_FLAGS = {
    "OPENCODE_DISABLE_PROJECT_CONFIG",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS",
    "OPENCODE_DISABLE_CLAUDE_CODE",
    "OPENCODE_DISABLE_SHARE",
}
WORKFLOWS = {
    "actionlint.yml",
    "codeql.yml",
    "dependency-review.yml",
    "release.yml",
    "scorecard.yml",
    "secret-scan.yml",
    "zizmor.yml",
}
FORBIDDEN_PUBLIC_PARTS = {
    "__pycache__",
    "benchmarks",
    "evidence",
    "fixtures",
    "memories",
    "tests",
    "validation",
}
SHA256_RE = re.compile(r"[0-9a-f]{64}")
HEX_COMMIT_RE = re.compile(r"[0-9a-f]{40}")


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
    elif not (path.stat().st_mode & stat.S_IXUSR):
        errors.append(f"{relative}: owner executable bit required")


def resolve_ref(document: dict[str, Any], reference: object) -> object:
    if not isinstance(reference, str) or ":" not in reference:
        return None
    _path, selector = reference.split(":", 1)
    value: object = document
    for part in selector.split("."):
        if not isinstance(value, dict) or part not in value:
            return None
        value = value[part]
    return value


def check_baseline_release(
    baseline: dict[str, Any],
    runtime: str,
    release: object,
    errors: list[str],
) -> None:
    baseline_release = baseline.get("release")
    if not isinstance(release, dict) or not isinstance(baseline_release, dict):
        errors.append("references/opencode-baseline.json: release object required")
        return
    expected = {
        "github_release": (f"https://github.com/anomalyco/opencode/releases/tag/v{runtime}"),
        "github_release_api": release.get("api"),
        "tag": release.get("tag"),
        "release_id": release.get("id"),
        "immutable": release.get("immutable"),
        "published_at": release.get("published_at"),
        "tag_ref": release.get("tag_ref"),
        "target_commitish": release.get("target_commitish"),
    }
    for key, value in expected.items():
        if baseline_release.get(key) != value:
            errors.append(
                "references/opencode-baseline.json: "
                f"release.{key} disagrees with build/version.json"
            )
    if baseline_release.get("draft") is not False:
        errors.append("references/opencode-baseline.json: release must not be a draft")
    if baseline_release.get("prerelease") is not False:
        errors.append("references/opencode-baseline.json: release must not be a prerelease")
    if baseline_release.get("cli_signature") is not None:
        errors.append("references/opencode-baseline.json: unsupported CLI signature claim")
    if not str(baseline_release.get("cli_signature_note", "")).strip():
        errors.append("references/opencode-baseline.json: CLI signature observation required")


def check_baseline(
    baseline: dict[str, Any],
    version: dict[str, Any],
    runtime: str,
    release: object,
    artifacts: object,
    host_map: object,
    errors: list[str],
) -> None:
    if baseline.get("schema_version") != 2:
        errors.append("references/opencode-baseline.json: schema_version must be 2")
    if resolve_ref(version, baseline.get("verified_version_ref")) != runtime:
        errors.append("references/opencode-baseline.json: verified_version_ref mismatch")
    runtime_contract = baseline.get("runtime")
    if not isinstance(runtime_contract, dict):
        errors.append("references/opencode-baseline.json: runtime object required")
    else:
        if runtime_contract.get("product") != "OpenCode":
            errors.append("references/opencode-baseline.json: runtime product mismatch")
        if runtime_contract.get("command") != "opencode":
            errors.append("references/opencode-baseline.json: runtime command mismatch")
        for key in ("tested_version_ref", "minimum_version_ref"):
            if resolve_ref(version, runtime_contract.get(key)) is None:
                errors.append(f"references/opencode-baseline.json: runtime.{key} is invalid")
    check_baseline_release(baseline, runtime, release, errors)
    reference_values = {
        "artifacts_ref": artifacts,
        "artifact_product_host_map_ref": host_map,
        "supported_product_hosts_ref": version.get("supported_product_hosts"),
        "unsupported_product_hosts_ref": version.get("unsupported_product_hosts"),
        "upstream_distribution_observation_ref": version.get("upstream_distribution_observation"),
    }
    for key, expected in reference_values.items():
        if resolve_ref(version, baseline.get(key)) != expected:
            errors.append(f"references/opencode-baseline.json: {key} mismatch")
    if set(baseline.get("source_verified_runtime_flags") or []) != SOURCE_USED_RUNTIME_FLAGS:
        errors.append("references/opencode-baseline.json: runtime flags mismatch")
    host_scope = baseline.get("product_host_scope")
    if not isinstance(host_scope, dict):
        errors.append("references/opencode-baseline.json: product_host_scope required")
    else:
        if host_scope.get("supported") != SUPPORTED_HOSTS:
            errors.append("references/opencode-baseline.json: supported hosts mismatch")
        if host_scope.get("unsupported") != UNSUPPORTED_HOSTS:
            errors.append("references/opencode-baseline.json: unsupported hosts mismatch")
        unsupported_ref = host_scope.get("product_unsupported_cli_assets_ref")
        expected_unsupported = (
            (version.get("upstream_distribution_observation") or {}).get(
                "product_unsupported_cli_assets"
            )
            if isinstance(version.get("upstream_distribution_observation"), dict)
            else None
        )
        if resolve_ref(version, unsupported_ref) != expected_unsupported:
            errors.append("references/opencode-baseline.json: unsupported asset ref mismatch")
    configuration = baseline.get("configuration")
    if not isinstance(configuration, dict):
        errors.append("references/opencode-baseline.json: configuration object required")
    else:
        if configuration.get("config_file") != "opencode.json":
            errors.append("references/opencode-baseline.json: config file mismatch")
        if configuration.get("native_builder_projection") != CONTENT_FILES[1:]:
            errors.append("references/opencode-baseline.json: native builder projection mismatch")
        if configuration.get("marketplace") is not None:
            errors.append("references/opencode-baseline.json: OpenCode marketplace must be null")
    permissions = baseline.get("permissions")
    if not isinstance(permissions, dict):
        errors.append("references/opencode-baseline.json: permissions object required")
    elif (
        permissions.get("current_key") != "permission"
        or permissions.get("legacy_tools_config_used") is not False
        or permissions.get("actions") != ["allow", "ask", "deny"]
        or permissions.get("full_auto_shape") != {"permission": "allow"}
    ):
        errors.append("references/opencode-baseline.json: permissions shape mismatch")


def check_release(
    version_text: str,
    version: dict[str, Any] | None,
    baseline: dict[str, Any] | None,
    errors: list[str],
) -> None:
    if version is None:
        return
    if version.get("schema_version") != 2:
        errors.append("build/version.json: schema_version must be 2")
    if version.get("build_version") != version_text:
        errors.append("VERSION and build/version.json:build_version disagree")
    runtime = version.get("opencode_version")
    if not isinstance(runtime, str) or not runtime:
        errors.append("build/version.json: opencode_version must be non-empty")
        runtime = ""
    release = version.get("release")
    if not isinstance(release, dict):
        errors.append("build/version.json: release object required")
    else:
        if release.get("tag") != f"v{runtime}":
            errors.append("build/version.json: release tag must match opencode_version")
        if not isinstance(release.get("id"), int) or release.get("id", 0) <= 0:
            errors.append("build/version.json: release id must be a positive integer")
        if release.get("immutable") is not True:
            errors.append("build/version.json: release must be immutable")
        for key in ("tag_ref", "target_commitish"):
            if HEX_COMMIT_RE.fullmatch(str(release.get(key, ""))) is None:
                errors.append(f"build/version.json: release.{key} must be a commit SHA")
        expected_api = f"https://api.github.com/repos/anomalyco/opencode/releases/tags/v{runtime}"
        if release.get("api") != expected_api:
            errors.append("build/version.json: release.api must match the pinned tag")

    artifacts = version.get("artifacts")
    host_map = version.get("artifact_product_host_map")
    if not isinstance(artifacts, dict) or set(artifacts) != set(SUPPORTED_ARTIFACTS):
        errors.append("build/version.json: supported artifact set mismatch")
        artifacts = {}
    if not isinstance(host_map, dict) or set(host_map) != set(SUPPORTED_ARTIFACTS):
        errors.append("build/version.json: artifact_product_host_map set mismatch")
        host_map = {}
    for artifact_id, (suffix, product_host, baseline_x64) in SUPPORTED_ARTIFACTS.items():
        artifact = artifacts.get(artifact_id)
        context = f"build/version.json:artifacts.{artifact_id}"
        if not isinstance(artifact, dict):
            errors.append(f"{context}: object required")
            continue
        name = f"opencode-{artifact_id}{suffix}"
        if artifact.get("name") != name:
            errors.append(f"{context}: canonical filename mismatch")
        if not isinstance(artifact.get("id"), int) or artifact.get("id", 0) <= 0:
            errors.append(f"{context}: id must be a positive integer")
        if not isinstance(artifact.get("size"), int) or artifact.get("size", 0) <= 0:
            errors.append(f"{context}: size must be a positive integer")
        if SHA256_RE.fullmatch(str(artifact.get("sha256", ""))) is None:
            errors.append(f"{context}: sha256 must be 64 lowercase hex characters")
        expected_url = f"https://github.com/anomalyco/opencode/releases/download/v{runtime}/{name}"
        if artifact.get("url") != expected_url:
            errors.append(f"{context}: URL must match pinned release and filename")
        if host_map.get(artifact_id) != {
            "product_host": product_host,
            "x64_baseline": baseline_x64,
        }:
            errors.append(f"{context}: product host mapping mismatch")

    if list((version.get("supported_product_hosts") or {}).keys()) != SUPPORTED_HOSTS:
        errors.append("build/version.json: supported product host IDs mismatch")
    if version.get("unsupported_product_hosts") != UNSUPPORTED_HOSTS:
        errors.append("build/version.json: unsupported product host categories mismatch")
    observation = version.get("upstream_distribution_observation")
    if not isinstance(observation, dict):
        errors.append("build/version.json: upstream distribution observation required")
    else:
        unsupported_assets = observation.get("product_unsupported_cli_assets")
        if not isinstance(unsupported_assets, dict) or set(unsupported_assets) != {
            "windows",
            "linux-musl",
        }:
            errors.append("build/version.json: unsupported upstream asset groups mismatch")
    if version.get("python_requires") != ">=3.9":
        errors.append("build/version.json: python_requires must remain >=3.9")

    if baseline is None:
        return
    check_baseline(
        baseline,
        version,
        runtime,
        release,
        artifacts,
        host_map,
        errors,
    )


def check_manifest(
    version_text: str,
    manifest: dict[str, Any] | None,
    version: dict[str, Any] | None,
    errors: list[str],
) -> None:
    if manifest is None:
        return
    if manifest.get("schema_version") != 2 or manifest.get("build_version") != version_text:
        errors.append("build/manifest.json: schema/build version mismatch")
    if manifest.get("setup_ids") != SETUP_IDS:
        errors.append("build/manifest.json: setup_ids mismatch")
    if manifest.get("profile_ids") != PROFILE_IDS:
        errors.append("build/manifest.json: profile_ids mismatch")
    if manifest.get("default_setup") != SETUP_IDS[0]:
        errors.append("build/manifest.json: default_setup mismatch")
    if manifest.get("default_profile") != DEFAULT_PROFILE:
        errors.append("build/manifest.json: default_profile mismatch")
    managed = manifest.get("managed_files")
    if not isinstance(managed, list) or managed[: len(MANAGED_FILES)] != MANAGED_FILES:
        errors.append("build/manifest.json: managed_files mismatch")
    builder = manifest.get("builder")
    if not isinstance(builder, dict):
        errors.append("build/manifest.json: builder object required")
    else:
        if builder.get("projection") != "native" or builder.get("marketplace") is not None:
            errors.append("build/manifest.json: native non-marketplace builder required")
        if builder.get("managed_files") != CONTENT_FILES[1:]:
            errors.append("build/manifest.json: builder managed_files mismatch")
    software = manifest.get("software_lifecycle")
    if not isinstance(software, dict):
        errors.append("build/manifest.json: software_lifecycle required")
    elif software.get("install_channel") != "official-github-release-asset":
        errors.append("build/manifest.json: official release asset channel required")
    launch = manifest.get("runtime_launch")
    if not isinstance(launch, dict):
        errors.append("build/manifest.json: runtime_launch required")
    else:
        forced = launch.get("forced_environment")
        if (
            not isinstance(forced, dict)
            or {key for key in forced if key.startswith("OPENCODE_DISABLE")}
            != SOURCE_USED_RUNTIME_FLAGS
        ):
            errors.append("build/manifest.json: forced runtime environment mismatch")
    if version is not None:
        if manifest.get("runtime_version_ref") and resolve_ref(
            version, manifest.get("runtime_version_ref")
        ) != version.get("opencode_version"):
            errors.append("build/manifest.json: runtime_version_ref mismatch")


def check_contract(
    contract: dict[str, Any] | None,
    manifest: dict[str, Any] | None,
    version: dict[str, Any] | None,
    errors: list[str],
) -> None:
    if contract is None:
        return
    expected_scalars = {
        "contract_version": 2,
        "product_name": "nddev-opencode-app",
        "github_repository": "NDDev-it-com/nddev-opencode-app",
        "license": "AGPL-3.0-or-later",
        "manifest_ref": "build/manifest.json",
        "version_ref": "build/version.json",
    }
    for key, expected in expected_scalars.items():
        if contract.get(key) != expected:
            errors.append(f"config/nddev-contract.json: {key} mismatch")
    for key in ("manifest_ref", "version_ref"):
        reference = contract.get(key)
        if not isinstance(reference, str) or not (ROOT / reference).is_file():
            errors.append(f"config/nddev-contract.json: invalid {key}")
    setup = contract.get("setup_system")
    if not isinstance(setup, dict):
        errors.append("config/nddev-contract.json: setup_system required")
    else:
        if setup.get("setup_ids") != SETUP_IDS or setup.get("profile_ids") != PROFILE_IDS:
            errors.append("config/nddev-contract.json: setup/profile IDs mismatch")
        if setup.get("default_setup") != SETUP_IDS[0]:
            errors.append("config/nddev-contract.json: default setup mismatch")
        if setup.get("default_profile") != DEFAULT_PROFILE:
            errors.append("config/nddev-contract.json: default profile mismatch")
    managed = contract.get("managed_state")
    if not isinstance(managed, dict) or managed.get("managed_files") != MANAGED_FILES:
        errors.append("config/nddev-contract.json: managed files disagree with manifest")
    builder = contract.get("builder")
    if not isinstance(builder, dict):
        errors.append("config/nddev-contract.json: builder required")
    else:
        if builder.get("projection") != "native" or builder.get("marketplace") is not None:
            errors.append("config/nddev-contract.json: builder identity mismatch")
        if builder.get("enabled_in_setups") != SETUP_IDS:
            errors.append("config/nddev-contract.json: builder setup enablement mismatch")
    compatibility = contract.get("runtime_compatibility")
    if not isinstance(compatibility, dict):
        errors.append("config/nddev-contract.json: runtime_compatibility required")
    else:
        version_ref = compatibility.get("version_ref")
        if not isinstance(version_ref, str) or not (ROOT / version_ref).is_file():
            errors.append("config/nddev-contract.json: runtime version ref mismatch")


def check_setup_and_profiles(errors: list[str]) -> None:
    setup = load_json("setups/nddev-builder/setup.json", errors)
    if setup is not None:
        if setup.get("schema_version") != 2 or setup.get("id") != SETUP_IDS[0]:
            errors.append("setups/nddev-builder/setup.json: schema/id mismatch")
        if setup.get("content_files") != CONTENT_FILES:
            errors.append("setups/nddev-builder/setup.json: content_files mismatch")
        if setup.get("builder_enabled") is not True:
            errors.append("setups/nddev-builder/setup.json: builder must be enabled")
    for relative in CONTENT_FILES:
        check_text(f"setups/nddev-builder/{relative}", errors)
    for legacy in ("safe", "balanced", "full-auto"):
        if (ROOT / "setups" / legacy).exists():
            errors.append(f"legacy setup directory must be absent: setups/{legacy}")

    for profile_id in PROFILE_IDS:
        metadata = load_json(f"profiles/{profile_id}/profile.json", errors)
        config = load_json(f"profiles/{profile_id}/opencode.json", errors)
        if metadata is not None:
            if metadata.get("schema_version") != 2 or metadata.get("id") != profile_id:
                errors.append(f"profiles/{profile_id}/profile.json: schema/id mismatch")
            if metadata.get("managed_config_keys") != ["autoupdate", "share", "permission"]:
                errors.append(f"profiles/{profile_id}/profile.json: managed keys mismatch")
            if metadata.get("default") is not (profile_id == DEFAULT_PROFILE):
                errors.append(f"profiles/{profile_id}/profile.json: default mismatch")
        if config is None:
            continue
        if config.get("$schema") != "https://opencode.ai/config.json":
            errors.append(f"profiles/{profile_id}/opencode.json: schema mismatch")
        if config.get("autoupdate") is not False or config.get("share") != "disabled":
            errors.append(f"profiles/{profile_id}/opencode.json: runtime posture mismatch")
        if "tools" in config or "tool" in config:
            errors.append(f"profiles/{profile_id}/opencode.json: legacy tools key forbidden")
        permission = config.get("permission")
        if profile_id == "full-auto" and permission != "allow":
            errors.append("profiles/full-auto/opencode.json: permission must be allow")
        if profile_id == "safe":
            if not isinstance(permission, dict):
                errors.append("profiles/safe/opencode.json: permission object required")
            elif (
                permission.get("edit") != "deny"
                or permission.get("bash") != "ask"
                or permission.get("external_directory") != "ask"
                or (permission.get("skill") or {}).get("nddev-builder") != "allow"
                or (permission.get("task") or {}).get("nddev-builder") != "allow"
            ):
                errors.append("profiles/safe/opencode.json: permission posture mismatch")


def check_manager_source(errors: list[str]) -> None:
    relative = "cli-tools/nddev_opencode.py"
    source = check_text(relative, errors)
    if not source:
        return
    try:
        ast.parse(source, filename=relative)
    except SyntaxError as exc:
        errors.append(f"{relative}: invalid Python syntax: {exc}")


def check_public_tree(errors: list[str]) -> None:
    for path in ROOT.rglob("*"):
        if ".git" in path.parts:
            continue
        relative_path = path.relative_to(ROOT)
        relative = relative_path.as_posix()
        if FORBIDDEN_PUBLIC_PARTS & {part.lower() for part in relative_path.parts}:
            errors.append(f"forbidden public path: {relative}")
        if path.is_file() and path.suffix in {".pyc", ".pyo"}:
            errors.append(f"Python cache file is forbidden: {relative}")


def main() -> int:
    errors: list[str] = []
    version_text = check_text("VERSION", errors).strip()
    version = load_json("build/version.json", errors)
    manifest = load_json("build/manifest.json", errors)
    contract = load_json("config/nddev-contract.json", errors)
    baseline = load_json("references/opencode-baseline.json", errors)

    check_release(version_text, version, baseline, errors)
    check_manifest(version_text, manifest, version, errors)
    check_contract(contract, manifest, version, errors)
    check_setup_and_profiles(errors)
    check_manager_source(errors)
    for relative in (
        "README.md",
        "AGENTS.md",
        "CHANGELOG.md",
        "SECURITY.md",
        "cli-tools/validate_public_contracts.py",
        ".claude/CLAUDE.md",
    ):
        check_text(relative, errors)
    for executable in ("cli-tools/nddev_opencode.py", "cli-tools/validate_public_contracts.py"):
        check_executable(executable, errors)
    for workflow in WORKFLOWS:
        check_text(f".github/workflows/{workflow}", errors)
    check_public_tree(errors)

    if errors:
        print(f"validate_public_contracts.py: FAIL ({len(errors)} error(s))")
        for error in errors:
            print(f"  - {error}")
        return 1
    print("validate_public_contracts.py: PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
