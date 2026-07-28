#!/usr/bin/env python3
"""Target-explicit OpenCode setup/profile manager for NDDev.

This public manager owns only reusable OpenCode setup logic and public runtime
contracts. It never defaults to or mutates the caller's live home directory.
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import hashlib
import json
import os
import platform
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import urllib.request
import zipfile
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-opencode-app"
CONTENT_SETUP_ID = "nddev-builder"
PROFILE_IDS = ("full-auto", "safe")
DEFAULT_PROFILE_ID = "full-auto"
SETUP_ROOT = ROOT / "setups" / CONTENT_SETUP_ID
PROFILE_ROOT = ROOT / "profiles"

STAMP_NAME = "NDDEV-OPENCODE-SETUP.json"
BACKUP_NAME = "NDDEV-OPENCODE-BACKUP.json"
SOFTWARE_STAMP_NAME = "NDDEV-OPENCODE-SOFTWARE.json"
STAMP_SCHEMA = 2
BACKUP_SCHEMA = 3
SOFTWARE_STAMP_SCHEMA = 2
MAX_BACKUPS = 10
OWNER_FILE_MODE = 0o600
OWNER_EXEC_MODE = 0o700
OWNER_DIR_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 1024 * 1024
SOFTWARE_MAX_BYTES = 512 * 1024 * 1024
PROCESS_OUTPUT_MAX_BYTES = 64 * 1024
VERSION_PROBE_TIMEOUT_SECONDS = 120
DOWNLOAD_TIMEOUT_SECONDS = 900
DOWNLOAD_CHUNK_BYTES = 1024 * 1024

CONTENT_FILES = (
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "skills/nddev-builder/references/native-surfaces.md",
    "skills/nddev-builder/references/security-boundary.md",
    "agents/nddev-builder.md",
    "commands/nddev-orient.md",
    "commands/nddev-validate.md",
)
MANAGED_FILES = ("opencode.json", *CONTENT_FILES)
LEGACY_MANAGED_FILES = (
    "opencode.json",
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "agents/nddev-builder.md",
)
KNOWN_MANAGED_FILES = tuple(dict.fromkeys((*MANAGED_FILES, *LEGACY_MANAGED_FILES)))
CONFIG_MANAGED_KEYS = ("autoupdate", "share", "permission")
FaultInjector = Callable[[str], None]

ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")

OPENCODE_VERSION = "1.18.7"
OPENCODE_RELEASE_TAG = "v1.18.7"
OPENCODE_RELEASE_ID = 360254815
OPENCODE_RELEASE_IMMUTABLE = True
OPENCODE_TAG_REF = "02981844b88aed33f06f1527da6c58d137975069"
OPENCODE_TARGET_COMMIT = "35075bb46692a921ab36715e5e1f4bf7f2def494"
OPENCODE_RELEASE_API = "https://api.github.com/repos/anomalyco/opencode/releases/tags/v1.18.7"
OPENCODE_COMMAND = "opencode"
SOFTWARE_DIR_NAME = ".nddev-opencode-software"
SOFTWARE_CURRENT_NAME = "current"
SUPPORTED_PRODUCT_HOSTS = {
    "macos-arm64": {
        "system": "darwin",
        "architecture": "arm64",
        "artifact_platforms": ["darwin-arm64"],
    },
    "macos-x64": {
        "system": "darwin",
        "architecture": "x64",
        "artifact_platforms": ["darwin-x64", "darwin-x64-baseline"],
        "x64_baseline_selection": "x64 host without AVX2",
    },
    "ubuntu-glibc-arm64": {
        "system": "linux",
        "distribution_id": "ubuntu",
        "distribution_metadata_source": "platform.freedesktop_os_release",
        "libc": "glibc",
        "variants": ["desktop", "server"],
        "official_distribution_version_floor": None,
        "official_distribution_version_floor_note": "no-official-floor",
        "architecture": "arm64",
        "artifact_platforms": ["linux-arm64"],
    },
    "ubuntu-glibc-x64": {
        "system": "linux",
        "distribution_id": "ubuntu",
        "distribution_metadata_source": "platform.freedesktop_os_release",
        "libc": "glibc",
        "variants": ["desktop", "server"],
        "official_distribution_version_floor": None,
        "official_distribution_version_floor_note": "no-official-floor",
        "architecture": "x64",
        "artifact_platforms": ["linux-x64", "linux-x64-baseline"],
        "x64_baseline_selection": "x64 host without AVX2",
    },
}
UNSUPPORTED_PRODUCT_HOSTS = [
    "windows",
    "non-ubuntu-linux",
    "linux-musl",
    "unsupported-architecture",
]
ARTIFACT_PRODUCT_HOSTS = {
    "darwin-arm64": {"product_host": "macos-arm64", "x64_baseline": False},
    "darwin-x64": {"product_host": "macos-x64", "x64_baseline": False},
    "darwin-x64-baseline": {"product_host": "macos-x64", "x64_baseline": True},
    "linux-arm64": {"product_host": "ubuntu-glibc-arm64", "x64_baseline": False},
    "linux-x64": {"product_host": "ubuntu-glibc-x64", "x64_baseline": False},
    "linux-x64-baseline": {"product_host": "ubuntu-glibc-x64", "x64_baseline": True},
}

ARTIFACTS: dict[str, dict[str, Any]] = {
    "darwin-arm64": {
        "id": 491142136,
        "name": "opencode-darwin-arm64.zip",
        "size": 44941305,
        "sha256": "47efed233667713fd3e0603ddaea95d0ee2076ce00dc9faa7dbc9208aeb13505",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-darwin-arm64.zip",
        "format": "zip",
    },
    "darwin-x64": {
        "id": 491142140,
        "name": "opencode-darwin-x64.zip",
        "size": 47179820,
        "sha256": "feee11da7697a80e2fcf943ff9ca392d4e960c5ddabd918bdd6e4de790279b7e",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-darwin-x64.zip",
        "format": "zip",
    },
    "darwin-x64-baseline": {
        "id": 491142139,
        "name": "opencode-darwin-x64-baseline.zip",
        "size": 47179820,
        "sha256": "7b4d13a20d28ff6425deace63943d3e459c338cb7d26a0578bb489779b924749",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-darwin-x64-baseline.zip",
        "format": "zip",
    },
    "linux-arm64": {
        "id": 491142207,
        "name": "opencode-linux-arm64.tar.gz",
        "size": 59118379,
        "sha256": "6c791e453c2ca03ee3dea09ebd16bfdfac4837e45d344a1487cd196b80090fc7",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-linux-arm64.tar.gz",
        "format": "tar.gz",
    },
    "linux-x64": {
        "id": 491142237,
        "name": "opencode-linux-x64.tar.gz",
        "size": 59307429,
        "sha256": "cb5d9d6d2f8fbef0a9c975ed4494f73b2a62f4e4ffd508bcc3212da4fa76c3da",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-linux-x64.tar.gz",
        "format": "tar.gz",
    },
    "linux-x64-baseline": {
        "id": 491142193,
        "name": "opencode-linux-x64-baseline.tar.gz",
        "size": 59307516,
        "sha256": "96df9b0b4fcabb420c445dfdcf45d49570a57546603bbb4784593c6dfb098d7e",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.7/opencode-linux-x64-baseline.tar.gz",
        "format": "tar.gz",
    },
}

LAUNCH_FORCED_ENV = {
    "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
    "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
    "OPENCODE_DISABLE_CLAUDE_CODE": "1",
    "OPENCODE_DISABLE_SHARE": "1",
}
PROVIDER_SECRET_NAMES = {
    "ANTHROPIC_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "OPENAI_API_KEY",
    "OPENROUTER_API_KEY",
    "XAI_API_KEY",
}
PACKAGE_MANAGER_SECRET_NAMES = {
    "BUN_AUTH_TOKEN",
    "NODE_AUTH_TOKEN",
    "NPM_TOKEN",
    "npm_config_prefix",
    "npm_config_userconfig",
}
LAUNCH_BLOCKED_BOOLEAN_FLAGS = {
    "--auto": "permission auto-approval override",
    "--dangerously-skip-permissions": "permission bypass override",
    "--global": "global config scope override",
    "--pure": "external plugin override",
    "--share": "sharing side effect",
    "--yolo": "permission bypass override",
}
LAUNCH_BLOCKED_VALUE_FLAGS = {
    "--agent": "agent override",
    "--attach": "remote server scope override",
    "--config": "config override",
    "--cwd": "working-directory scope override",
    "--dir": "working-directory scope override",
    "--hostname": "network listener override",
    "--mode": "agent mode override",
    "--path": "agent path scope override",
    "--permissions": "agent permission override",
    "--port": "network listener override",
    "--tools": "agent permission override",
}
LAUNCH_BLOCKED_SHORT_FLAGS = {
    "-g": "global config scope override",
}
LAUNCH_BLOCKED_COMMANDS = {
    "plug": "external plugin mutation",
    "plugin": "external plugin mutation",
    "serve": "network listener side effect",
    "upgrade": "target-owned software updates must go through update-cli",
    "web": "network/browser side effect",
}


class ManagerError(Exception):
    """A user-facing lifecycle failure."""


class ConcurrentTargetChange(ManagerError):
    """A target changed while the manager was validating or mutating it."""


class ManagerCliParseError(Exception):
    """A parse-time CLI error that should be rendered by main()."""


PARSER_ARGV_FOR_JSON: tuple[str, ...] = ()


class ManagerArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> NoReturn:
        if "--json" in PARSER_ARGV_FOR_JSON:
            raise ManagerCliParseError(message)
        super().error(message)


@dataclass(frozen=True)
class ContentSetup:
    setup_id: str
    description: str
    files: dict[str, bytes]


@dataclass(frozen=True)
class Profile:
    profile_id: str
    description: str
    config: dict[str, Any]


@dataclass(frozen=True)
class SnapshotEntry:
    text: str | None
    digest: str | None


@dataclass(frozen=True)
class ManagedStateSnapshot:
    target_existed: bool
    files: dict[str, SnapshotEntry]


@dataclass(frozen=True)
class BinaryFileSnapshot:
    content: bytes | None
    mode: int | None


@dataclass(frozen=True)
class SoftwareStateSnapshot:
    target_existed: bool
    software_root_existed: bool
    current_existed: bool
    current_tree_digest: str | None
    bin_dir_existed: bool
    entrypoint: BinaryFileSnapshot
    stamp: BinaryFileSnapshot


@dataclass(frozen=True)
class SoftwareRemoveTransaction:
    target: Path
    stage_root: Path
    software_root_stage: Path
    entrypoint_stage: Path
    stamp_stage: Path
    snapshot: SoftwareStateSnapshot


@dataclass(frozen=True)
class BackupTransaction:
    root: Path
    staging_root: Path
    previous_root: Path
    root_existed: bool
    payloads_before: list[dict[str, Any]]


@dataclass(frozen=True)
class LockHandle:
    fd: int
    path: Path
    parent_existed: bool
    file_existed: bool


def fail(message: str) -> NoReturn:
    raise ManagerError(message)


def unsupported_product_host(category: str, detail: str) -> NoReturn:
    fail(f"unsupported product host ({category}): {detail}")


def maybe_inject_fault(fault_injection: FaultInjector | None, point: str) -> None:
    if fault_injection is not None:
        fault_injection(point)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def current_uid() -> int | None:
    return os.geteuid() if hasattr(os, "geteuid") else None


def fsync_directory(path: Path) -> None:
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    try:
        fd = os.open(path, flags)
    except OSError as exc:
        fail(f"directory fsync open failed for {path}: {exc}")
    try:
        os.set_inheritable(fd, False)
        try:
            os.fsync(fd)
        except OSError as exc:
            fail(f"directory fsync failed for {path}: {exc}")
    finally:
        os.close(fd)


def fsync_file_descriptor(fd: int) -> None:
    try:
        os.fsync(fd)
    except OSError as exc:
        fail(f"file fsync failed: {exc}")


def require_current_user_owner(info: os.stat_result, label: str) -> None:
    uid = current_uid()
    if uid is not None and info.st_uid != uid:
        fail(f"{label} must be owned by the current user")


def safe_relative_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"managed path is not safe: {relative}")
    return path


def reject_unsafe_archive_path(name: str, label: str) -> None:
    path = Path(name)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"{label} contains an unsafe archive path: {name}")


def require_directory(path: Path, label: str, *, private: bool = False) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    require_current_user_owner(info, label)
    if private and stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
        fail(f"{label} must have mode 0700")
    return info


def require_real_private_directory(path: Path, label: str) -> os.stat_result:
    info = require_directory(path, label, private=True)
    return info


def ensure_real_private_directory(path: Path, label: str, *, create: bool) -> bool:
    try:
        info = path.lstat()
    except FileNotFoundError:
        if not create:
            return False
        path.mkdir(mode=OWNER_DIR_MODE)
        os.chmod(path, OWNER_DIR_MODE)
        require_real_private_directory(path, label)
        return True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    require_current_user_owner(info, label)
    if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
        fail(f"{label} must have mode 0700")
    return True


def require_target_contained(target: Path, path: Path, label: str) -> Path:
    try:
        return path.relative_to(target)
    except ValueError:
        fail(f"{label} must stay inside the canonical target")


def ensure_target_private_directory(
    target: Path, relative: str, label: str, *, create: bool
) -> bool:
    require_real_private_directory(target, "target")
    safe = safe_relative_path(relative)
    current = target
    for part in safe.parts:
        current = current / part
        if not ensure_real_private_directory(
            current, f"{label} {current.relative_to(target)}", create=create
        ):
            return False
    return True


def ensure_target_private_parent(target: Path, path: Path, label: str, *, create: bool) -> bool:
    relative = require_target_contained(target, path, label)
    parent = relative.parent
    if str(parent) == ".":
        require_real_private_directory(target, "target")
        return True
    return ensure_target_private_directory(
        target, parent.as_posix(), f"{label} parent", create=create
    )


def stat_optional(path: Path, label: str) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    return info


def require_regular_file(
    path: Path,
    label: str,
    *,
    private: bool = False,
    executable: bool = False,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    require_current_user_owner(info, label)
    mode = stat.S_IMODE(info.st_mode)
    if private and mode != OWNER_FILE_MODE:
        fail(f"{label} must have mode 0600")
    if executable and mode != OWNER_EXEC_MODE:
        fail(f"{label} must have mode 0700")
    return info


def read_regular_file(
    path: Path,
    label: str,
    *,
    private: bool = False,
    executable: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(path, label, private=private, executable=executable)
    if before.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        os.set_inheritable(descriptor, False)
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            raise ConcurrentTargetChange(f"{label} changed while it was opened")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            chunks.append(chunk)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label, private=private, executable=executable)
    if identity_of(after) != identity_of(before) or identity_of(final) != identity_of(before):
        raise ConcurrentTargetChange(f"{label} changed while it was read")
    return b"".join(chunks), final


def parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def read_json_file(path: Path, label: str, *, private: bool = False) -> dict[str, Any]:
    content, _ = read_regular_file(path, label, private=private, max_bytes=METADATA_MAX_BYTES)
    return parse_json_object(content, label)


def validate_id(value: str, label: str) -> None:
    if not ID_PATTERN.fullmatch(value):
        fail(f"invalid {label}: {value!r}")


def target_path(target: Path, relative: str) -> Path:
    path = safe_relative_path(relative)
    current = target
    for part in path.parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent must be a real directory: {current}")
        require_current_user_owner(info, f"managed parent {current}")
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"managed parent must have mode 0700: {current}")
    return target / path


def resolve_target(raw_target: str | None) -> Path:
    if not raw_target:
        fail("--target is required")
    expanded = Path(raw_target).expanduser()
    if not expanded.is_absolute():
        fail("--target must be an absolute path")
    raw_info = stat_optional(expanded, "--target")
    if raw_info is not None and not stat.S_ISDIR(raw_info.st_mode):
        fail("--target must be a real directory")
    target = expanded.resolve(strict=False)
    if target == Path(target.anchor):
        fail("filesystem root cannot be a target")
    require_directory(target.parent, "canonical --target parent")
    if target.exists():
        require_real_private_directory(target, "--target")
    return target


def ensure_target_directory(target: Path, *, create: bool) -> bool:
    return ensure_real_private_directory(target, "target", create=create)


def read_target_file(
    target: Path,
    relative: str,
    *,
    private: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> bytes:
    return read_regular_file(
        target_path(target, relative),
        f"managed path {relative}",
        private=private,
        max_bytes=max_bytes,
    )[0]


def target_file_exists(target: Path, relative: str) -> bool:
    path = target_path(target, relative)
    info = stat_optional(path, f"managed path {relative}")
    if info is None:
        return False
    if not stat.S_ISREG(info.st_mode):
        fail(f"managed path {relative} must be a regular file")
    if info.st_nlink != 1:
        fail(f"managed path {relative} must not have hard-link aliases")
    return True


def read_target_json_if_present(target: Path) -> dict[str, Any]:
    path = target_path(target, "opencode.json")
    info = stat_optional(path, "opencode.json")
    if info is None:
        return {}
    if not stat.S_ISREG(info.st_mode):
        fail("opencode.json must be a regular file")
    content = read_regular_file(path, "opencode.json", max_bytes=METADATA_MAX_BYTES)[0]
    return parse_json_object(content, "opencode.json")


def managed_config_fragment(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in CONFIG_MANAGED_KEYS if key in config}


def managed_digest(relative: str, content: bytes) -> str:
    if relative != "opencode.json":
        return sha256_bytes(content)
    config = parse_json_object(content, "managed opencode.json")
    return sha256_bytes(canonical_json(managed_config_fragment(config)))


def render_content_setup() -> ContentSetup:
    metadata = read_json_file(SETUP_ROOT / "setup.json", "setups/nddev-builder/setup.json")
    expected_keys = {"schema_version", "id", "description", "content_files", "builder_enabled"}
    if set(metadata) != expected_keys:
        fail("setups/nddev-builder/setup.json has invalid keys")
    if metadata["schema_version"] != 2 or metadata["id"] != CONTENT_SETUP_ID:
        fail("setups/nddev-builder/setup.json identity or schema is invalid")
    if metadata["content_files"] != list(CONTENT_FILES):
        fail("setups/nddev-builder/setup.json content_files mismatch")
    if metadata["builder_enabled"] is not True:
        fail("nddev-builder setup must enable the native builder projection")
    files: dict[str, bytes] = {}
    for relative in CONTENT_FILES:
        content, _ = read_regular_file(SETUP_ROOT / safe_relative_path(relative), relative)
        if not content or not content.endswith(b"\n") or b"\r" in content:
            fail(f"{relative} must be non-empty LF-terminated text")
        content.decode("utf-8")
        files[relative] = content
    return ContentSetup(CONTENT_SETUP_ID, str(metadata["description"]), files)


def render_profile(profile_id: str) -> Profile:
    validate_id(profile_id, "profile id")
    if profile_id not in PROFILE_IDS:
        fail(f"unknown profile: {profile_id}")
    root = PROFILE_ROOT / profile_id
    metadata = read_json_file(root / "profile.json", f"profiles/{profile_id}/profile.json")
    expected_keys = {"schema_version", "id", "description", "default", "managed_config_keys"}
    if set(metadata) != expected_keys:
        fail(f"profiles/{profile_id}/profile.json has invalid keys")
    if metadata["schema_version"] != 2 or metadata["id"] != profile_id:
        fail(f"profiles/{profile_id}/profile.json identity or schema is invalid")
    if metadata["managed_config_keys"] != list(CONFIG_MANAGED_KEYS):
        fail(f"profiles/{profile_id}/profile.json managed_config_keys mismatch")
    config = read_json_file(root / "opencode.json", f"profiles/{profile_id}/opencode.json")
    if config.get("$schema") != "https://opencode.ai/config.json":
        fail(f"profiles/{profile_id}/opencode.json must use the current schema")
    if "tools" in config or "tool" in config:
        fail(f"profiles/{profile_id}/opencode.json must not use legacy tools config")
    if config.get("autoupdate") is not False:
        fail(f"profiles/{profile_id}/opencode.json must disable autoupdate")
    if config.get("share") != "disabled":
        fail(f"profiles/{profile_id}/opencode.json must disable sharing")
    if profile_id == "full-auto":
        if config.get("permission") != "allow":
            fail("full-auto profile must use exact official permission scalar 'allow'")
    else:
        permission = config.get("permission")
        if not isinstance(permission, dict):
            fail("safe profile permission must be an object")
        if permission.get("edit") != "deny" or permission.get("bash") != "ask":
            fail("safe profile must deny edits and ask for shell")
        if permission.get("external_directory") != "ask":
            fail("safe profile must ask for external_directory")
        if permission.get("share") is not None:
            fail("safe profile must not define non-official share permission")
        if (permission.get("skill") or {}).get("nddev-builder") != "allow":
            fail("safe profile must allow the nddev-builder skill")
        if (permission.get("task") or {}).get("nddev-builder") != "allow":
            fail("safe profile must allow the nddev-builder subagent")
    return Profile(profile_id, str(metadata["description"]), config)


def compose_config(current: dict[str, Any], profile: Profile) -> dict[str, Any]:
    result = dict(current)
    if "$schema" not in result:
        result["$schema"] = profile.config["$schema"]
    for key in CONFIG_MANAGED_KEYS:
        result[key] = profile.config[key]
    return result


def strip_managed_config(current: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key in CONFIG_MANAGED_KEYS:
        result.pop(key, None)
    if result == {"$schema": "https://opencode.ai/config.json"}:
        return {}
    return result


def desired_state(target: Path, setup: ContentSetup, profile: Profile) -> dict[str, bytes | None]:
    current = read_target_json_if_present(target) if target.exists() else {}
    desired: dict[str, bytes | None] = {relative: None for relative in KNOWN_MANAGED_FILES}
    desired["opencode.json"] = canonical_json(compose_config(current, profile))
    for relative, content in setup.files.items():
        desired[relative] = content
    return desired


def desired_state_with_stamp(target: Path, profile: Profile) -> dict[str, bytes | None]:
    setup = render_content_setup()
    desired = desired_state(target, setup, profile)
    desired[STAMP_NAME] = canonical_json(stamp_payload(target, profile, desired))
    return desired


def validate_digest_map(value: Any, label: str) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != set(MANAGED_FILES):
        fail(f"{label} must declare exactly current managed files")
    result: dict[str, str | None] = {}
    for relative in MANAGED_FILES:
        digest = value[relative]
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
        ):
            fail(f"{label}.{relative} must be null or a lowercase SHA-256 digest")
        result[relative] = digest
    return result


def stamp_payload(
    target: Path, profile: Profile, desired: dict[str, bytes | None]
) -> dict[str, Any]:
    managed_files = {
        relative: managed_digest(relative, desired[relative] or b"") for relative in MANAGED_FILES
    }
    return {
        "schema_version": STAMP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": CONTENT_SETUP_ID,
        "profile_id": profile.profile_id,
        "canonical_target": str(target),
        "managed_files": managed_files,
        "builder": {
            "projection": "native",
            "enabled": True,
            "files": [
                "plugins/nddev-builder.js",
                "skills/nddev-builder/SKILL.md",
                "skills/nddev-builder/references/native-surfaces.md",
                "skills/nddev-builder/references/security-boundary.md",
                "agents/nddev-builder.md",
                "commands/nddev-orient.md",
                "commands/nddev-validate.md",
            ],
        },
        "runtime_boundary": {
            "project_config_disabled_env": "OPENCODE_DISABLE_PROJECT_CONFIG",
            "external_skills_disabled_env": "OPENCODE_DISABLE_EXTERNAL_SKILLS",
            "claude_compat_disabled_env": "OPENCODE_DISABLE_CLAUDE_CODE",
            "share_disabled_env": "OPENCODE_DISABLE_SHARE",
        },
    }


def stamp_path(target: Path) -> Path:
    return target / STAMP_NAME


def load_stamp_any(target: Path) -> dict[str, Any] | None:
    if not ensure_target_directory(target, create=False):
        return None
    if not target_file_exists(target, STAMP_NAME):
        return None
    return parse_json_object(
        read_target_file(target, STAMP_NAME, max_bytes=METADATA_MAX_BYTES),
        STAMP_NAME,
    )


def load_stamp(target: Path) -> dict[str, Any] | None:
    stamp = load_stamp_any(target)
    if stamp is None:
        return None
    if stamp.get("schema_version") == 1:
        return stamp
    expected = {
        "schema_version",
        "product_name",
        "build_version",
        "setup_id",
        "profile_id",
        "canonical_target",
        "managed_files",
        "builder",
        "runtime_boundary",
    }
    if set(stamp) != expected:
        fail("managed stamp has invalid keys")
    if stamp["schema_version"] != STAMP_SCHEMA or stamp["product_name"] != PRODUCT_NAME:
        fail("managed stamp identity or schema is invalid")
    if stamp["canonical_target"] != str(target):
        fail("managed stamp is bound to a different canonical target")
    if stamp["setup_id"] != CONTENT_SETUP_ID:
        fail("managed stamp setup_id is invalid")
    if stamp["profile_id"] not in PROFILE_IDS:
        fail("managed stamp profile_id is invalid")
    validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    builder = stamp["builder"]
    if not isinstance(builder, dict) or builder.get("projection") != "native":
        fail("managed stamp builder projection is invalid")
    boundary = stamp["runtime_boundary"]
    if (
        not isinstance(boundary, dict)
        or boundary.get("share_disabled_env") != "OPENCODE_DISABLE_SHARE"
    ):
        fail("managed stamp runtime boundary is invalid")
    return stamp


def detect_drift(target: Path, stamp: dict[str, Any]) -> list[str]:
    if stamp.get("schema_version") == 1:
        return ["legacy_schema"]
    drift: list[str] = []
    expected = validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    for relative in MANAGED_FILES:
        if not target_file_exists(target, relative):
            drift.append(relative)
            continue
        content = read_target_file(target, relative, private=True)
        if managed_digest(relative, content) != expected[relative]:
            drift.append(relative)
    for relative in set(LEGACY_MANAGED_FILES) - set(MANAGED_FILES):
        if target_file_exists(target, relative):
            drift.append(f"stale:{relative}")
    return sorted(drift)


def snapshot_known_files(target: Path) -> dict[str, SnapshotEntry]:
    result: dict[str, SnapshotEntry] = {}
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        if ensure_target_directory(target, create=False) and target_file_exists(target, relative):
            content = read_target_file(target, relative, max_bytes=MANAGED_PAYLOAD_MAX_BYTES)
            text = content.decode("utf-8")
            result[relative] = SnapshotEntry(text=text, digest=sha256_bytes(content))
        else:
            result[relative] = SnapshotEntry(text=None, digest=None)
    return result


def snapshot_managed_state(target: Path) -> ManagedStateSnapshot:
    return ManagedStateSnapshot(
        target_existed=ensure_target_directory(target, create=False),
        files=snapshot_known_files(target),
    )


def assert_snapshot(
    target: Path, snapshot: dict[str, SnapshotEntry] | ManagedStateSnapshot
) -> None:
    files = snapshot.files if isinstance(snapshot, ManagedStateSnapshot) else snapshot
    for relative, expected in files.items():
        exists = ensure_target_directory(target, create=False) and target_file_exists(
            target, relative
        )
        actual_digest: str | None
        if exists:
            actual_digest = sha256_bytes(read_target_file(target, relative))
        else:
            actual_digest = None
        if actual_digest != expected.digest:
            raise ConcurrentTargetChange(f"managed path changed concurrently: {relative}")


def current_managed_digest(target: Path, relative: str) -> str | None:
    if not ensure_target_directory(target, create=False) or not target_file_exists(
        target, relative
    ):
        return None
    content = read_target_file(target, relative, private=True)
    if relative in MANAGED_FILES:
        return managed_digest(relative, content)
    return sha256_bytes(content)


def state_delta(target: Path, desired: dict[str, bytes | None]) -> list[dict[str, Any]]:
    changes: list[dict[str, Any]] = []
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        desired_content = desired.get(relative)
        current_digest = current_managed_digest(target, relative)
        desired_digest = None
        if desired_content is not None:
            desired_digest = (
                managed_digest(relative, desired_content)
                if relative in MANAGED_FILES
                else sha256_bytes(desired_content)
            )
        if current_digest != desired_digest:
            changes.append(
                {
                    "path": relative,
                    "action": "remove"
                    if desired_content is None
                    else ("create" if current_digest is None else "update"),
                }
            )
    return changes


def make_parent_directories(path: Path) -> None:
    require_real_private_directory(path.parent, f"{path} parent")


def atomic_write(
    path: Path,
    content: bytes,
    *,
    mode: int = OWNER_FILE_MODE,
    fault_injection: FaultInjector | None = None,
    fault_label: str | None = None,
) -> None:
    make_parent_directories(path)
    existing = stat_optional(path, str(path))
    if existing is not None:
        if not stat.S_ISREG(existing.st_mode):
            fail(f"{path} must be a regular file")
        if existing.st_nlink != 1:
            fail(f"{path} must not have hard-link aliases")
        require_current_user_owner(existing, str(path))
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        os.set_inheritable(fd, False)
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
            handle.flush()
            maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:temp-write")
            os.chmod(temporary, mode)
            maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:chmod")
            fsync_file_descriptor(handle.fileno())
            maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:file-fsync")
        os.replace(temporary, path)
        maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:replace")
        fsync_directory(path.parent)
        maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:parent-fsync")
        if mode == OWNER_EXEC_MODE:
            require_regular_file(path, str(path), executable=True)
        elif mode == OWNER_FILE_MODE:
            require_regular_file(path, str(path), private=True)
        else:
            require_regular_file(path, str(path))
        if read_regular_file(path, str(path), max_bytes=max(len(content), 1))[0] != content:
            fail(f"{path} content postcondition mismatch")
        maybe_inject_fault(fault_injection, f"atomic:{fault_label or path.name}:postcondition")
    finally:
        temporary.unlink(missing_ok=True)


def remove_empty_managed_parents(target: Path, relative: str) -> None:
    current = (target / safe_relative_path(relative)).parent
    while current != target:
        try:
            info = current.lstat()
        except FileNotFoundError:
            break
        if stat.S_ISLNK(info.st_mode):
            fail(f"managed parent must not be a symlink: {current}")
        if not stat.S_ISDIR(info.st_mode):
            break
        require_current_user_owner(info, f"managed parent {current}")
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"managed parent must have mode 0700: {current}")
        try:
            current.rmdir()
        except OSError:
            break
        fsync_directory(current.parent)
        current = current.parent


def replace_managed_state(
    target: Path,
    desired: dict[str, bytes | None],
    expected: dict[str, SnapshotEntry] | None,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    ensure_target_directory(target, create=True)
    if expected is not None:
        assert_snapshot(target, expected)
    for relative in KNOWN_MANAGED_FILES:
        path = target_path(target, relative)
        content = desired.get(relative)
        if content is None:
            info = stat_optional(path, f"managed path {relative}")
            if info is not None:
                require_regular_file(path, f"managed path {relative}")
                path.unlink()
                fsync_directory(path.parent)
                remove_empty_managed_parents(target, relative)
                maybe_inject_fault(fault_injection, f"remove:{relative}")
            continue
        ensure_target_private_parent(target, path, f"managed path {relative}", create=True)
        atomic_write(
            path,
            content,
            fault_injection=fault_injection,
            fault_label=f"managed:{relative}",
        )
        maybe_inject_fault(fault_injection, f"write:{relative}")
    stamp_content = desired.get(STAMP_NAME)
    if stamp_content is None:
        if stat_optional(stamp_path(target), STAMP_NAME) is not None:
            require_regular_file(stamp_path(target), STAMP_NAME)
            stamp_path(target).unlink()
            fsync_directory(stamp_path(target).parent)
            maybe_inject_fault(fault_injection, f"remove:{STAMP_NAME}")
    else:
        ensure_target_private_parent(target, stamp_path(target), STAMP_NAME, create=True)
        atomic_write(
            stamp_path(target),
            stamp_content,
            fault_injection=fault_injection,
            fault_label=f"managed:{STAMP_NAME}",
        )
        maybe_inject_fault(fault_injection, f"write:{STAMP_NAME}")


def assert_desired_managed_state(target: Path, desired: dict[str, bytes | None]) -> None:
    remaining = state_delta(target, desired)
    if remaining:
        fail(f"managed state postcondition mismatch: {remaining}")


def restore_managed_snapshot(
    target: Path,
    snapshot: ManagedStateSnapshot,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    desired = {
        name: None if entry.text is None else entry.text.encode("utf-8")
        for name, entry in snapshot.files.items()
    }
    replace_managed_state(target, desired, None, fault_injection=fault_injection)
    assert_desired_managed_state(target, desired)
    if not snapshot.target_existed:
        with contextlib.suppress(OSError):
            target.rmdir()
            fsync_directory(target.parent)
            maybe_inject_fault(fault_injection, "rollback-managed:remove-target")
    maybe_inject_fault(fault_injection, "rollback-managed:postcondition")


def rollback_managed_snapshot(
    target: Path,
    snapshot: ManagedStateSnapshot,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        restore_managed_snapshot(target, snapshot, fault_injection=fault_injection)
    except BaseException:
        restore_managed_snapshot(target, snapshot)


def backup_root(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-opencode-backups"


def backup_slot_path(target: Path, slot: int) -> Path:
    return backup_root(target) / str(slot)


def validate_backup_payload(target: Path, slot: int, payload: dict[str, Any]) -> dict[str, Any]:
    expected_keys = {
        "schema_version",
        "product_name",
        "build_version",
        "slot",
        "operation",
        "canonical_target",
        "files",
        "sizes",
        "digests",
    }
    if set(payload) != expected_keys:
        fail("backup envelope has invalid keys")
    if (
        payload.get("schema_version") != BACKUP_SCHEMA
        or payload.get("product_name") != PRODUCT_NAME
    ):
        fail("backup identity or schema is invalid")
    if not isinstance(payload.get("canonical_target"), str) or payload.get(
        "canonical_target"
    ) != str(target):
        fail("backup is bound to a different canonical target")
    if not isinstance(payload.get("slot"), int) or payload.get("slot") != slot:
        fail("backup slot value is invalid")
    if not isinstance(payload.get("build_version"), str) or payload.get("build_version") != VERSION:
        fail("backup build_version is invalid")
    if not isinstance(payload.get("operation"), str) or not payload["operation"]:
        fail("backup operation is invalid")
    files = payload.get("files")
    if not isinstance(files, dict):
        fail("backup files must be an object")
    expected_file_keys = set((*KNOWN_MANAGED_FILES, STAMP_NAME))
    if set(files) != expected_file_keys:
        fail("backup files must declare exactly known managed paths")
    for relative, value in files.items():
        safe_relative_path(relative)
        if value is not None and not isinstance(value, str):
            fail(f"backup file value must be text or null: {relative}")
        if value is not None and len(value.encode("utf-8")) > MANAGED_PAYLOAD_MAX_BYTES:
            fail(f"backup file value exceeds size limit: {relative}")
    digests = payload.get("digests")
    if not isinstance(digests, dict):
        fail("backup digests must be an object")
    present_keys = {relative for relative, value in files.items() if value is not None}
    sizes = payload.get("sizes")
    if not isinstance(sizes, dict):
        fail("backup sizes must be an object")
    if set(sizes) != present_keys:
        fail("backup sizes must declare exactly present files")
    for relative in present_keys:
        size = sizes[relative]
        if not isinstance(size, int) or size < 0:
            fail(f"backup size is invalid: {relative}")
        encoded = str(files[relative]).encode("utf-8")
        if len(encoded) != size:
            fail(f"backup size mismatch: {relative}")
    if set(digests) != present_keys:
        fail("backup digests must declare exactly present files")
    for relative in present_keys:
        digest = digests[relative]
        if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
            fail(f"backup digest is invalid: {relative}")
        encoded = str(files[relative]).encode("utf-8")
        if sha256_bytes(encoded) != digest:
            fail(f"backup digest mismatch: {relative}")
    return payload


def backup_payload_from_snapshot(
    target: Path, operation: str, snapshot: dict[str, SnapshotEntry], slot: int
) -> dict[str, Any]:
    payload = {
        "schema_version": BACKUP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": slot,
        "operation": operation,
        "canonical_target": str(target),
        "files": {name: entry.text for name, entry in snapshot.items()},
        "sizes": {
            name: len(entry.text.encode("utf-8"))
            for name, entry in snapshot.items()
            if entry.text is not None
        },
        "digests": {
            name: entry.digest for name, entry in snapshot.items() if entry.digest is not None
        },
    }
    return validate_backup_payload(target, slot, payload)


def write_backup_payload_file(
    target: Path,
    slot_dir: Path,
    slot: int,
    payload: dict[str, Any],
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    require_real_private_directory(slot_dir, f"backup slot {slot}")
    payload = validate_backup_payload(target, slot, payload)
    atomic_write(
        slot_dir / BACKUP_NAME,
        canonical_json(payload),
        fault_injection=fault_injection,
        fault_label=f"backup:{slot}",
    )
    entries = list(slot_dir.iterdir())
    if len(entries) != 1 or entries[0].name != BACKUP_NAME:
        fail(f"backup slot {slot} must contain exactly {BACKUP_NAME}")


def backup_pool_payloads(target: Path) -> list[dict[str, Any]]:
    root = backup_root(target)
    if not root.exists() and not root.is_symlink():
        return []
    require_real_private_directory(root, "backup root")
    slots: list[int] = []
    for child in root.iterdir():
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"backup root contains an invalid entry: {child.name}")
        if not child.name.isdigit():
            fail(f"backup root contains an invalid slot: {child.name}")
        slot = int(child.name)
        if slot < 0 or slot >= MAX_BACKUPS:
            fail(f"backup root contains an out-of-range slot: {child.name}")
        slots.append(slot)
    slots.sort()
    if slots != list(range(len(slots))):
        fail("backup slots must be contiguous from 0")
    return [load_backup(target, slot) for slot in slots]


def prepare_backup_transaction(
    target: Path,
    operation: str,
    *,
    fault_injection: FaultInjector | None = None,
) -> BackupTransaction:
    existing = backup_pool_payloads(target)
    snapshot = snapshot_known_files(target)
    root = backup_root(target)
    stage_name = tempfile.mkdtemp(
        prefix=f".{target.name}.nddev-opencode-backups-stage.",
        dir=str(target.parent),
    )
    staging = Path(stage_name)
    previous = (
        target.parent
        / f".{target.name}.nddev-opencode-backups-previous.{os.getpid()}.{time.time_ns()}"
    )
    try:
        os.chmod(staging, OWNER_DIR_MODE)
        require_real_private_directory(staging, "backup staging root")
        payloads: list[dict[str, Any]] = [
            backup_payload_from_snapshot(target, operation, snapshot, 0)
        ]
        for slot, payload in enumerate(existing[: MAX_BACKUPS - 1], start=1):
            shifted = dict(payload)
            shifted["slot"] = slot
            shifted["digests"] = {
                relative: sha256_bytes(value.encode("utf-8"))
                for relative, value in shifted["files"].items()
                if value is not None
            }
            shifted["sizes"] = {
                relative: len(value.encode("utf-8"))
                for relative, value in shifted["files"].items()
                if value is not None
            }
            payloads.append(validate_backup_payload(target, slot, shifted))
        for slot, payload in enumerate(payloads):
            slot_dir = staging / str(slot)
            slot_dir.mkdir(mode=OWNER_DIR_MODE)
            os.chmod(slot_dir, OWNER_DIR_MODE)
            write_backup_payload_file(
                target,
                slot_dir,
                slot,
                payload,
                fault_injection=fault_injection,
            )
            maybe_inject_fault(fault_injection, f"backup:prepare-slot:{slot}")
        backup_pool_payloads_from_root(target, staging)
        maybe_inject_fault(fault_injection, "backup:prepare-postcondition")
    except BaseException:
        with contextlib.suppress(ManagerError, OSError):
            remove_private_tree(staging, "backup staging root")
        raise
    return BackupTransaction(
        root=root,
        staging_root=staging,
        previous_root=previous,
        root_existed=root.exists() or root.is_symlink(),
        payloads_before=existing,
    )


def backup_pool_payloads_from_root(target: Path, root: Path) -> list[dict[str, Any]]:
    if not root.exists() and not root.is_symlink():
        return []
    require_real_private_directory(root, "backup root")
    slots: list[int] = []
    for child in root.iterdir():
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"backup root contains an invalid entry: {child.name}")
        if not child.name.isdigit():
            fail(f"backup root contains an invalid slot: {child.name}")
        slot = int(child.name)
        if slot < 0 or slot >= MAX_BACKUPS:
            fail(f"backup root contains an out-of-range slot: {child.name}")
        slots.append(slot)
    slots.sort()
    if slots != list(range(len(slots))):
        fail("backup slots must be contiguous from 0")
    payloads: list[dict[str, Any]] = []
    for slot in slots:
        slot_dir = root / str(slot)
        require_real_private_directory(slot_dir, f"backup slot {slot}")
        entries = list(slot_dir.iterdir())
        if len(entries) != 1 or entries[0].name != BACKUP_NAME:
            fail(f"backup slot {slot} must contain exactly {BACKUP_NAME}")
        payload = read_json_file(slot_dir / BACKUP_NAME, BACKUP_NAME, private=True)
        payloads.append(validate_backup_payload(target, slot, payload))
    return payloads


def assert_backup_pool_state(target: Path, transaction: BackupTransaction) -> None:
    if transaction.payloads_before:
        if backup_pool_payloads(target) != transaction.payloads_before:
            fail("backup pool rollback postcondition mismatch")
    elif transaction.root.exists() or transaction.root.is_symlink():
        fail("backup pool rollback postcondition expected absence")
    if transaction.previous_root.exists() or transaction.previous_root.is_symlink():
        fail("backup previous root residue remains after rollback")
    if transaction.staging_root.exists() or transaction.staging_root.is_symlink():
        fail("backup staging root residue remains after rollback")


def best_effort_remove_private_tree(
    path: Path,
    label: str,
    *,
    fault_injection: FaultInjector | None = None,
    fault_point: str | None = None,
) -> None:
    with contextlib.suppress(ManagerError, OSError):
        remove_private_tree(path, label)
        if fault_point is not None:
            maybe_inject_fault(fault_injection, fault_point)


def rollback_backup_transaction(
    target: Path,
    transaction: BackupTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    root = transaction.root
    previous = transaction.previous_root
    if previous.exists() or previous.is_symlink():
        if root.exists() or root.is_symlink():
            remove_private_tree(root, "backup root")
            maybe_inject_fault(fault_injection, "rollback-backup:remove-new-root")
        os.replace(previous, root)
        fsync_directory(root.parent)
        maybe_inject_fault(fault_injection, "rollback-backup:restore-previous-root")
    elif not transaction.root_existed and (root.exists() or root.is_symlink()):
        remove_private_tree(root, "backup root")
        maybe_inject_fault(fault_injection, "rollback-backup:remove-new-root")
    if transaction.staging_root.exists() or transaction.staging_root.is_symlink():
        remove_private_tree(transaction.staging_root, "backup staging root")
        maybe_inject_fault(fault_injection, "rollback-backup:remove-staging-root")
    assert_backup_pool_state(target, transaction)
    maybe_inject_fault(fault_injection, "rollback-backup:postcondition")


def cleanup_backup_transaction(
    target: Path,
    transaction: BackupTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        rollback_backup_transaction(target, transaction, fault_injection=fault_injection)
    except BaseException:
        rollback_backup_transaction(target, transaction)


def commit_backup_transaction(
    target: Path,
    transaction: BackupTransaction,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> int:
    root = transaction.root
    staging = transaction.staging_root
    previous = transaction.previous_root
    moved_previous = False
    committed = False
    try:
        backup_pool_payloads(target)
        backup_pool_payloads_from_root(target, staging)
        if previous.exists() or previous.is_symlink():
            fail("backup previous root already exists")
        if root.exists() or root.is_symlink():
            require_real_private_directory(root, "backup root")
            os.replace(root, previous)
            moved_previous = True
            fsync_directory(root.parent)
            maybe_inject_fault(fault_injection, "backup:move-old-root")
        os.replace(staging, root)
        committed = True
        fsync_directory(root.parent)
        maybe_inject_fault(fault_injection, "backup:replace-root")
        backup_pool_payloads(target)
        maybe_inject_fault(fault_injection, "backup:postcondition")
    except BaseException:
        cleanup_backup_transaction(
            target,
            transaction,
            fault_injection=rollback_fault_injection,
        )
        if committed or moved_previous:
            backup_pool_payloads(target)
        raise
    best_effort_remove_private_tree(
        previous,
        "previous backup root",
        fault_injection=fault_injection,
        fault_point="backup:cleanup-old-root",
    )
    return 0


def load_backup(target: Path, slot: int) -> dict[str, Any]:
    if slot < 0 or slot >= MAX_BACKUPS:
        fail("--backup must be in range 0..9")
    root = backup_root(target)
    require_real_private_directory(root, "backup root")
    slot_dir = backup_slot_path(target, slot)
    require_real_private_directory(slot_dir, f"backup slot {slot}")
    entries = list(slot_dir.iterdir())
    if len(entries) != 1 or entries[0].name != BACKUP_NAME:
        fail(f"backup slot {slot} must contain exactly {BACKUP_NAME}")
    payload = read_json_file(slot_dir / BACKUP_NAME, BACKUP_NAME, private=True)
    return validate_backup_payload(target, slot, payload)


def preflight_unmanaged_target(target: Path) -> None:
    stamp = load_stamp_any(target)
    if stamp is not None:
        return
    if not ensure_target_directory(target, create=False):
        return
    for relative in CONTENT_FILES:
        if target_file_exists(target, relative):
            fail(f"unmanaged target already has managed path: {relative}")
    config = read_target_json_if_present(target)
    managed = sorted(set(CONFIG_MANAGED_KEYS) & set(config))
    if managed:
        fail(f"unmanaged target already has managed OpenCode config keys: {managed}")


def current_status(target: Path) -> dict[str, Any]:
    canonical = str(target)
    payload: dict[str, Any] = {
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": canonical,
        "managed": False,
        "legacy": False,
        "migrate_required": False,
        "setup_id": None,
        "profile_id": None,
        "drift": [],
        "present": target.exists(),
    }
    stamp = load_stamp(target)
    if stamp is None:
        return payload
    if stamp.get("schema_version") == 1:
        payload.update(
            {
                "managed": True,
                "legacy": True,
                "migrate_required": True,
                "setup_id": stamp.get("setup_id"),
                "profile_id": None,
                "drift": ["legacy_schema"],
            }
        )
        return payload
    drift = detect_drift(target, stamp)
    payload.update(
        {
            "managed": True,
            "setup_id": stamp.get("setup_id"),
            "profile_id": stamp.get("profile_id"),
            "drift": drift,
            "current": not drift and stamp.get("build_version") == VERSION,
        }
    )
    return payload


def plan_payload(target: Path, profile: Profile) -> dict[str, Any]:
    desired = desired_state_with_stamp(target, profile)
    changes = state_delta(target, desired)
    return {
        "setup_id": CONTENT_SETUP_ID,
        "profile_id": profile.profile_id,
        "target": str(target),
        "mutates": False,
        "changed": bool(changes),
        "changes": changes,
    }


def install_or_switch(
    target: Path,
    profile: Profile,
    *,
    operation: str,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
    backup_fault_injection: FaultInjector | None = None,
    backup_rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    setup = render_content_setup()
    rollback_snapshot = snapshot_managed_state(target)
    if operation == "install":
        ensure_target_directory(target, create=True)
    elif not ensure_target_directory(target, create=False):
        fail(f"{operation} requires an existing target")
    existing = load_stamp(target)
    if operation == "install":
        preflight_unmanaged_target(target)
    elif operation == "migrate":
        if existing is None or existing.get("schema_version") != 1:
            fail("migrate requires a legacy managed schema-1 target")
    elif existing is None or existing.get("schema_version") != STAMP_SCHEMA:
        fail("switch requires a current managed schema-2 target")
    elif detect_drift(target, existing):
        fail("switch requires a clean managed target")
    desired = desired_state(target, setup, profile)
    desired[STAMP_NAME] = canonical_json(stamp_payload(target, profile, desired))
    changes = state_delta(target, desired)
    if not changes:
        return {
            "ok": True,
            "operation": operation,
            "setup_id": CONTENT_SETUP_ID,
            "profile_id": profile.profile_id,
            "target": str(target),
            "changed": False,
            "changes": [],
            "backup": None,
        }
    expected = snapshot_known_files(target)
    backup_transaction: BackupTransaction | None = None
    try:
        backup_transaction = prepare_backup_transaction(
            target,
            operation,
            fault_injection=backup_fault_injection,
        )
        replace_managed_state(target, desired, expected, fault_injection=fault_injection)
        assert_desired_managed_state(target, desired)
        backup = commit_backup_transaction(
            target,
            backup_transaction,
            fault_injection=backup_fault_injection,
            rollback_fault_injection=backup_rollback_fault_injection,
        )
    except BaseException:
        if backup_transaction is not None:
            cleanup_backup_transaction(
                target,
                backup_transaction,
                fault_injection=backup_rollback_fault_injection,
            )
        rollback_managed_snapshot(
            target,
            rollback_snapshot,
            fault_injection=rollback_fault_injection,
        )
        raise
    return {
        "ok": True,
        "operation": operation,
        "setup_id": CONTENT_SETUP_ID,
        "profile_id": profile.profile_id,
        "target": str(target),
        "changed": True,
        "changes": changes,
        "backup": backup,
    }


def migrate_target(target: Path, profile_id: str | None) -> dict[str, Any]:
    stamp = load_stamp_any(target)
    if stamp is None:
        fail("migrate requires a legacy managed target")
    if stamp.get("schema_version") == STAMP_SCHEMA:
        return {"ok": True, "operation": "migrate", "already_current": True, "target": str(target)}
    if stamp.get("schema_version") != 1 or stamp.get("product_name") != PRODUCT_NAME:
        fail("migrate requires a legacy nddev-opencode schema-1 stamp")
    legacy_setup = stamp.get("setup_id")
    if legacy_setup == "full-auto":
        selected_profile = profile_id or "full-auto"
    elif legacy_setup == "safe":
        selected_profile = profile_id or "safe"
    elif legacy_setup == "balanced":
        if profile_id is None:
            fail("legacy balanced migration requires explicit --profile safe|full-auto")
        selected_profile = profile_id
    else:
        fail(f"unsupported legacy setup id: {legacy_setup!r}")
    profile = render_profile(selected_profile)
    return install_or_switch(target, profile, operation="migrate")


def restore_target(
    target: Path,
    slot: int,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    payload = load_backup(target, slot)
    snapshot = snapshot_managed_state(target)
    desired: dict[str, bytes | None] = {
        relative: None for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME)
    }
    files = payload["files"]
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        value = files[relative]
        desired[relative] = None if value is None else str(value).encode("utf-8")
    changes = state_delta(target, desired)
    try:
        replace_managed_state(target, desired, snapshot.files, fault_injection=fault_injection)
        assert_desired_managed_state(target, desired)
    except BaseException:
        rollback_managed_snapshot(
            target,
            snapshot,
            fault_injection=rollback_fault_injection,
        )
        raise
    return {
        "ok": True,
        "operation": "restore",
        "target": str(target),
        "backup": slot,
        "changed": bool(changes),
    }


def remove_target(
    target: Path,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
    backup_fault_injection: FaultInjector | None = None,
    backup_rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    if not ensure_target_directory(target, create=False):
        return {"ok": True, "operation": "remove", "target": str(target), "removed": False}
    rollback_snapshot = snapshot_managed_state(target)
    desired: dict[str, bytes | None] = {
        relative: None for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME)
    }
    current = read_target_json_if_present(target)
    stripped = strip_managed_config(current)
    if stripped:
        desired["opencode.json"] = canonical_json(stripped)
    changes = state_delta(target, desired)
    if not changes:
        return {
            "ok": True,
            "operation": "remove",
            "target": str(target),
            "removed": False,
            "changed": False,
            "changes": [],
            "backup": None,
        }
    expected = snapshot_known_files(target)
    backup_transaction: BackupTransaction | None = None
    try:
        backup_transaction = prepare_backup_transaction(
            target,
            "remove",
            fault_injection=backup_fault_injection,
        )
        replace_managed_state(target, desired, expected, fault_injection=fault_injection)
        assert_desired_managed_state(target, desired)
        backup = commit_backup_transaction(
            target,
            backup_transaction,
            fault_injection=backup_fault_injection,
            rollback_fault_injection=backup_rollback_fault_injection,
        )
    except BaseException:
        if backup_transaction is not None:
            cleanup_backup_transaction(
                target,
                backup_transaction,
                fault_injection=backup_rollback_fault_injection,
            )
        rollback_managed_snapshot(
            target,
            rollback_snapshot,
            fault_injection=rollback_fault_injection,
        )
        raise
    return {
        "ok": True,
        "operation": "remove",
        "target": str(target),
        "removed": True,
        "changed": True,
        "changes": changes,
        "backup": backup,
    }


def software_root(target: Path) -> Path:
    return target / SOFTWARE_DIR_NAME


def software_current(target: Path) -> Path:
    return software_root(target) / SOFTWARE_CURRENT_NAME


def software_stamp_path(target: Path) -> Path:
    return target / SOFTWARE_STAMP_NAME


def software_entrypoint(target: Path) -> Path:
    return target / "bin" / OPENCODE_COMMAND


def preflight_software_paths(target: Path) -> None:
    if not ensure_target_directory(target, create=False):
        return
    for path, label in (
        (target / "bin", "software entrypoint parent"),
        (software_root(target), SOFTWARE_DIR_NAME),
        (software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"),
    ):
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"{label} must be a real directory")
        require_current_user_owner(info, label)
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"{label} must have mode 0700")


def snapshot_binary_file(path: Path, label: str, *, max_bytes: int) -> BinaryFileSnapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return BinaryFileSnapshot(content=None, mode=None)
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    require_current_user_owner(info, label)
    mode = stat.S_IMODE(info.st_mode)
    content = read_regular_file(path, label, max_bytes=max_bytes)[0]
    return BinaryFileSnapshot(content=content, mode=mode)


def snapshot_software_state(target: Path) -> SoftwareStateSnapshot:
    preflight_software_paths(target)
    current_exists = software_current(target).exists() or software_current(target).is_symlink()
    return SoftwareStateSnapshot(
        target_existed=ensure_target_directory(target, create=False),
        software_root_existed=(
            software_root(target).exists() or software_root(target).is_symlink()
        ),
        current_existed=current_exists,
        current_tree_digest=tree_sha256(software_current(target)) if current_exists else None,
        bin_dir_existed=((target / "bin").exists() or (target / "bin").is_symlink()),
        entrypoint=snapshot_binary_file(
            software_entrypoint(target),
            "OpenCode entrypoint",
            max_bytes=SOFTWARE_MAX_BYTES,
        ),
        stamp=snapshot_binary_file(
            software_stamp_path(target),
            SOFTWARE_STAMP_NAME,
            max_bytes=METADATA_MAX_BYTES,
        ),
    )


def remove_private_tree(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        require_real_private_directory(path, label)
        shutil.rmtree(path)
        fsync_directory(path.parent)


def restore_binary_file(
    path: Path,
    snapshot: BinaryFileSnapshot,
    label: str,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    if snapshot.content is None:
        info = stat_optional(path, label)
        if info is not None:
            require_regular_file(path, label)
            path.unlink()
            fsync_directory(path.parent)
            maybe_inject_fault(fault_injection, f"rollback-software:unlink:{label}")
        return
    make_parent_directories(path)
    atomic_write(
        path,
        snapshot.content,
        mode=snapshot.mode or OWNER_FILE_MODE,
        fault_injection=fault_injection,
        fault_label=f"rollback-software:{label}",
    )


def restore_software_state(
    target: Path,
    snapshot: SoftwareStateSnapshot,
    *,
    previous_current: Path | None,
    fault_injection: FaultInjector | None = None,
) -> None:
    if previous_current is not None and (
        previous_current.exists() or previous_current.is_symlink()
    ):
        require_real_private_directory(previous_current, "previous OpenCode software tree")
        remove_private_tree(
            software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"
        )
        maybe_inject_fault(fault_injection, "rollback-software:remove-new-current")
        ensure_real_private_directory(software_root(target), SOFTWARE_DIR_NAME, create=True)
        os.replace(previous_current, software_current(target))
        fsync_directory(software_root(target))
        maybe_inject_fault(fault_injection, "rollback-software:restore-previous-current")
    elif not snapshot.current_existed:
        remove_private_tree(
            software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"
        )
        maybe_inject_fault(fault_injection, "rollback-software:remove-new-current")
    if snapshot.entrypoint.content is not None:
        ensure_target_private_parent(
            target, software_entrypoint(target), "OpenCode entrypoint", create=True
        )
    if snapshot.stamp.content is not None:
        ensure_target_private_parent(
            target, software_stamp_path(target), SOFTWARE_STAMP_NAME, create=True
        )
    restore_binary_file(
        software_entrypoint(target),
        snapshot.entrypoint,
        "OpenCode entrypoint",
        fault_injection=fault_injection,
    )
    restore_binary_file(
        software_stamp_path(target),
        snapshot.stamp,
        SOFTWARE_STAMP_NAME,
        fault_injection=fault_injection,
    )
    if not snapshot.bin_dir_existed:
        with contextlib.suppress(OSError):
            (target / "bin").rmdir()
            fsync_directory(target)
            maybe_inject_fault(fault_injection, "rollback-software:remove-bin-dir")
    if not snapshot.software_root_existed:
        with contextlib.suppress(OSError):
            software_root(target).rmdir()
            fsync_directory(target)
            maybe_inject_fault(fault_injection, "rollback-software:remove-software-root")
    if not snapshot.target_existed:
        with contextlib.suppress(OSError):
            target.rmdir()
            fsync_directory(target.parent)
            maybe_inject_fault(fault_injection, "rollback-software:remove-target")
    assert_software_snapshot(target, snapshot)
    maybe_inject_fault(fault_injection, "rollback-software:postcondition")


def assert_binary_snapshot(
    path: Path, snapshot: BinaryFileSnapshot, label: str, *, max_bytes: int
) -> None:
    if snapshot.content is None:
        if path.exists() or path.is_symlink():
            fail(f"{label} rollback postcondition expected absence")
        return
    info = require_regular_file(path, label)
    if stat.S_IMODE(info.st_mode) != snapshot.mode:
        fail(f"{label} rollback postcondition mode mismatch")
    if read_regular_file(path, label, max_bytes=max_bytes)[0] != snapshot.content:
        fail(f"{label} rollback postcondition content mismatch")


def assert_software_snapshot(target: Path, snapshot: SoftwareStateSnapshot) -> None:
    if not snapshot.target_existed:
        if target.exists() or target.is_symlink():
            fail("software target rollback postcondition expected absence")
        return
    require_real_private_directory(target, "target")
    for path, existed, label in (
        (software_root(target), snapshot.software_root_existed, SOFTWARE_DIR_NAME),
        (
            software_current(target),
            snapshot.current_existed,
            f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}",
        ),
        (target / "bin", snapshot.bin_dir_existed, "software entrypoint parent"),
    ):
        present = path.exists() or path.is_symlink()
        if not existed and present:
            fail(f"{label} rollback postcondition expected absence")
        if existed:
            require_real_private_directory(path, label)
    if (
        snapshot.current_tree_digest is not None
        and tree_sha256(software_current(target)) != snapshot.current_tree_digest
    ):
        fail("software current tree rollback postcondition digest mismatch")
    assert_binary_snapshot(
        software_entrypoint(target),
        snapshot.entrypoint,
        "OpenCode entrypoint",
        max_bytes=SOFTWARE_MAX_BYTES,
    )
    assert_binary_snapshot(
        software_stamp_path(target),
        snapshot.stamp,
        SOFTWARE_STAMP_NAME,
        max_bytes=METADATA_MAX_BYTES,
    )


def rollback_software_state(
    target: Path,
    snapshot: SoftwareStateSnapshot,
    *,
    previous_current: Path | None,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        restore_software_state(
            target,
            snapshot,
            previous_current=previous_current,
            fault_injection=fault_injection,
        )
    except BaseException:
        restore_software_state(target, snapshot, previous_current=previous_current)


def file_sha256(path: Path, *, label: str, executable: bool = False) -> str:
    return sha256_bytes(
        read_regular_file(path, label, executable=executable, max_bytes=SOFTWARE_MAX_BYTES)[0]
    )


def tree_sha256(root: Path) -> str:
    require_directory(root, "software tree", private=True)
    digest = hashlib.sha256()
    total = 0
    for path in sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix()):
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"software tree must not contain symlinks: {relative}")
        digest.update(
            relative.encode("utf-8")
            + b"\0"
            + oct(stat.S_IMODE(info.st_mode)).encode("ascii")
            + b"\0"
        )
        if stat.S_ISDIR(info.st_mode):
            require_current_user_owner(info, relative)
            digest.update(b"dir\0")
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"software tree entry must be a regular file: {relative}")
        if info.st_nlink != 1:
            fail(f"software tree entry must not be a hardlink: {relative}")
        content = read_regular_file(path, relative, max_bytes=SOFTWARE_MAX_BYTES)[0]
        total += len(content)
        if total > SOFTWARE_MAX_BYTES:
            fail("software tree is too large")
        digest.update(b"file\0" + sha256_bytes(content).encode("ascii") + b"\0")
    return digest.hexdigest()


def has_avx2(machine: str | None = None) -> bool:
    machine = (machine or platform.machine()).lower()
    if machine not in {"x86_64", "amd64"}:
        return False
    if sys.platform.startswith("linux"):
        with contextlib.suppress(OSError):
            return (
                " avx2 "
                in f" {Path('/proc/cpuinfo').read_text(encoding='utf-8', errors='ignore').lower()} "
            )
    if sys.platform == "darwin":
        try:
            result = subprocess.run(
                ["sysctl", "-n", "machdep.cpu.leaf7_features"],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.DEVNULL,
                text=True,
                check=False,
                timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return False
        return "AVX2" in result.stdout.upper()
    return False


def freedesktop_os_release() -> dict[str, str]:
    reader = getattr(platform, "freedesktop_os_release", None)
    if reader is None:
        return {}
    try:
        return dict(reader())
    except OSError:
        return {}


def normalize_machine(machine: str) -> str | None:
    value = machine.lower()
    if value in {"arm64", "aarch64"}:
        return "arm64"
    if value in {"x86_64", "amd64"}:
        return "x64"
    return None


def select_supported_host(
    *,
    system: str,
    machine: str,
    os_release: dict[str, str] | None = None,
    libc: tuple[str, str] | None = None,
    avx2: bool | None = None,
) -> dict[str, Any]:
    """Select a supported product host and vendor artifact from structured data."""

    normalized_arch = normalize_machine(machine)
    system_value = system.lower()
    if system_value == "darwin":
        if normalized_arch == "arm64":
            return {
                "product_host": "macos-arm64",
                "system": "macos",
                "architecture": "arm64",
                "artifact_platform": "darwin-arm64",
                "x64_baseline": False,
            }
        if normalized_arch == "x64":
            baseline = not bool(avx2)
            return {
                "product_host": "macos-x64",
                "system": "macos",
                "architecture": "x64",
                "x64_baseline": baseline,
                "artifact_platform": "darwin-x64-baseline" if baseline else "darwin-x64",
            }
        unsupported_product_host(
            "unsupported-architecture", f"unsupported macOS architecture: {machine}"
        )

    if system_value.startswith("linux"):
        distro = {str(key).upper(): str(value).lower() for key, value in (os_release or {}).items()}
        libc_name = (libc or ("", ""))[0].lower()
        if libc_name != "glibc":
            unsupported_product_host(
                "linux-musl",
                f"Ubuntu desktop/server with glibc is required; libc is {libc_name or 'unknown'}",
            )
        distro_id = distro.get("ID")
        if distro_id != "ubuntu":
            if not distro_id:
                unsupported_product_host(
                    "non-ubuntu-linux",
                    "unknown or unreadable /etc/os-release; ID=ubuntu is required",
                )
            unsupported_product_host(
                "non-ubuntu-linux",
                f"distribution ID {distro_id} is outside scope; ID=ubuntu is required",
            )
        if normalized_arch == "arm64":
            return {
                "product_host": "ubuntu-glibc-arm64",
                "system": "ubuntu",
                "distribution": "ubuntu",
                "libc": "glibc",
                "architecture": "arm64",
                "artifact_platform": "linux-arm64",
                "x64_baseline": False,
                "official_distribution_version_floor": None,
                "official_distribution_version_floor_note": "no-official-floor",
            }
        if normalized_arch == "x64":
            baseline = not bool(avx2)
            return {
                "product_host": "ubuntu-glibc-x64",
                "system": "ubuntu",
                "distribution": "ubuntu",
                "libc": "glibc",
                "architecture": "x64",
                "x64_baseline": baseline,
                "artifact_platform": "linux-x64-baseline" if baseline else "linux-x64",
                "official_distribution_version_floor": None,
                "official_distribution_version_floor_note": "no-official-floor",
            }
        unsupported_product_host(
            "unsupported-architecture", f"unsupported Ubuntu architecture: {machine}"
        )

    if system_value.startswith(("win32", "cygwin", "msys")) or system_value == "windows":
        unsupported_product_host(
            "windows", "Windows is outside the nddev-opencode-app product scope"
        )
    unsupported_product_host(
        "unsupported-architecture", f"unsupported host platform: {system}/{machine}"
    )


def detect_supported_host() -> dict[str, Any]:
    return select_supported_host(
        system=sys.platform,
        machine=platform.machine(),
        os_release=freedesktop_os_release(),
        libc=platform.libc_ver(),
        avx2=has_avx2(),
    )


def platform_key() -> str:
    return str(detect_supported_host()["artifact_platform"])


def fetch_release_metadata() -> dict[str, Any]:
    with urllib.request.urlopen(OPENCODE_RELEASE_API, timeout=30) as response:
        return json.load(response)


def verify_release_metadata(data: dict[str, Any]) -> None:
    checks = {
        "tag_name": data.get("tag_name") == OPENCODE_RELEASE_TAG,
        "id": data.get("id") == OPENCODE_RELEASE_ID,
        "draft": data.get("draft") is False,
        "prerelease": data.get("prerelease") is False,
        "immutable": data.get("immutable") is OPENCODE_RELEASE_IMMUTABLE,
        "target_commitish": data.get("target_commitish") == OPENCODE_TARGET_COMMIT,
    }
    failed = [name for name, ok in checks.items() if not ok]
    if failed:
        fail(f"OpenCode release metadata mismatch: {failed}")
    assets = {
        asset.get("name"): asset for asset in data.get("assets", []) if isinstance(asset, dict)
    }
    for artifact in ARTIFACTS.values():
        found = assets.get(artifact["name"])
        if not found:
            fail(f"OpenCode release asset missing: {artifact['name']}")
        if found.get("id") != artifact["id"]:
            fail(f"OpenCode release asset id mismatch: {artifact['name']}")
        if found.get("size") != artifact["size"]:
            fail(f"OpenCode release asset size mismatch: {artifact['name']}")
        if found.get("digest") != f"sha256:{artifact['sha256']}":
            fail(f"OpenCode release asset digest mismatch: {artifact['name']}")
        if found.get("browser_download_url") != artifact["url"]:
            fail(f"OpenCode release asset URL mismatch: {artifact['name']}")


def response_content_length(response: Any) -> int:
    headers = getattr(response, "headers", None)
    raw: str | None = None
    if headers is not None:
        raw = headers.get("Content-Length")
    if raw is None and hasattr(response, "getheader"):
        raw = response.getheader("Content-Length")
    if raw is None:
        fail("OpenCode artifact response is missing Content-Length")
    try:
        value = int(str(raw))
    except ValueError:
        fail("OpenCode artifact response has malformed Content-Length")
    if value < 0:
        fail("OpenCode artifact response has malformed Content-Length")
    return value


def download_artifact(
    url: str,
    destination: Path,
    expected_size: int,
    *,
    opener: Callable[..., Any] = urllib.request.urlopen,
) -> None:
    make_parent_directories(destination)
    if stat_optional(destination, "downloaded OpenCode artifact") is not None:
        fail("downloaded OpenCode artifact destination already exists")
    request = urllib.request.Request(url, headers={"User-Agent": PRODUCT_NAME})
    temporary: Path | None = None
    try:
        with opener(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
            content_length = response_content_length(response)
            if content_length != expected_size:
                fail("OpenCode artifact Content-Length mismatch")
            fd, temporary_name = tempfile.mkstemp(
                prefix=f".{destination.name}.", dir=str(destination.parent)
            )
            temporary = Path(temporary_name)
            total = 0
            with os.fdopen(fd, "wb") as handle:
                os.set_inheritable(handle.fileno(), False)
                while True:
                    chunk = response.read(DOWNLOAD_CHUNK_BYTES)
                    if not chunk:
                        break
                    total += len(chunk)
                    if total > expected_size:
                        fail("downloaded OpenCode artifact exceeds pinned size")
                    handle.write(chunk)
                handle.flush()
                os.chmod(temporary, OWNER_FILE_MODE)
                fsync_file_descriptor(handle.fileno())
            if total != expected_size:
                fail("downloaded OpenCode artifact size mismatch")
            os.replace(temporary, destination)
            fsync_directory(destination.parent)
            temporary = None
            require_regular_file(destination, "downloaded OpenCode artifact", private=True)
    except BaseException:
        if temporary is not None:
            temporary.unlink(missing_ok=True)
        with contextlib.suppress(FileNotFoundError):
            destination.unlink()
            fsync_directory(destination.parent)
        raise


def archive_binary_from_zip(archive: Path) -> bytes:
    with zipfile.ZipFile(archive) as zf:
        candidates: list[zipfile.ZipInfo] = []
        for info in zf.infolist():
            reject_unsafe_archive_path(info.filename, "OpenCode zip asset")
            name = Path(info.filename)
            if info.is_dir():
                continue
            file_type = (info.external_attr >> 16) & 0o170000
            if file_type == stat.S_IFLNK:
                fail("OpenCode zip asset must not contain symlinks")
            if name.name == OPENCODE_COMMAND:
                candidates.append(info)
        if len(candidates) != 1:
            fail("OpenCode zip asset must contain exactly one opencode binary")
        data = zf.read(candidates[0])
    if len(data) > SOFTWARE_MAX_BYTES:
        fail("OpenCode binary exceeds size limit")
    return data


def archive_binary_from_tar(archive: Path) -> bytes:
    with tarfile.open(archive, "r:gz") as tf:
        candidates: list[tarfile.TarInfo] = []
        for member in tf.getmembers():
            reject_unsafe_archive_path(member.name, "OpenCode tar asset")
            if member.issym() or member.islnk() or member.isdev():
                fail("OpenCode tar asset must not contain links or device entries")
            if member.isfile() and Path(member.name).name == OPENCODE_COMMAND:
                candidates.append(member)
        if len(candidates) != 1:
            fail("OpenCode tar asset must contain exactly one opencode binary")
        extracted = tf.extractfile(candidates[0])
        if extracted is None:
            fail("OpenCode tar binary could not be read")
        data = extracted.read(SOFTWARE_MAX_BYTES + 1)
    if len(data) > SOFTWARE_MAX_BYTES:
        fail("OpenCode binary exceeds size limit")
    return data


def extract_single_binary(archive: Path, artifact: dict[str, Any], destination: Path) -> str:
    data = (
        archive_binary_from_zip(archive)
        if artifact["format"] == "zip"
        else archive_binary_from_tar(archive)
    )
    digest = sha256_bytes(data)
    make_parent_directories(destination)
    atomic_write(destination, data, mode=OWNER_EXEC_MODE)
    return digest


def run_version_probe(binary: Path, stage: Path) -> str:
    home = stage / "probe-home"
    config = stage / "probe-config"
    tmp = stage / "probe-tmp"
    require_real_private_directory(stage, "OpenCode stage")
    for relative in ("probe-home", "probe-config", "probe-tmp"):
        ensure_target_private_directory(stage, relative, f"OpenCode stage {relative}", create=True)
    env = {
        "HOME": str(home),
        "OPENCODE_CONFIG": str(config / "opencode.json"),
        "OPENCODE_CONFIG_DIR": str(config),
        "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
        "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
        "OPENCODE_DISABLE_CLAUDE_CODE": "1",
        "OPENCODE_DISABLE_SHARE": "1",
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp),
    }
    completed = subprocess.run(
        [str(binary), "--version"],
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        check=False,
        timeout=VERSION_PROBE_TIMEOUT_SECONDS,
    )
    output = (completed.stdout + completed.stderr).strip()
    if completed.returncode != 0:
        fail(
            f"OpenCode version probe failed with exit code {completed.returncode}: {output[:PROCESS_OUTPUT_MAX_BYTES]}"
        )
    if OPENCODE_VERSION not in output:
        fail("OpenCode version probe did not report the pinned version")
    return sha256_bytes(output.encode("utf-8"))


def software_stamp(
    target: Path,
    host: dict[str, Any],
    artifact_key: str,
    artifact: dict[str, Any],
    *,
    executable_digest: str,
    installed_tree_digest: str,
    version_probe_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": SOFTWARE_STAMP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "opencode_version": OPENCODE_VERSION,
        "product_host": host,
        "release": {
            "tag": OPENCODE_RELEASE_TAG,
            "id": OPENCODE_RELEASE_ID,
            "immutable": OPENCODE_RELEASE_IMMUTABLE,
            "tag_ref": OPENCODE_TAG_REF,
            "target_commitish": OPENCODE_TARGET_COMMIT,
            "api": OPENCODE_RELEASE_API,
        },
        "artifact": {
            "platform": artifact_key,
            "id": artifact["id"],
            "name": artifact["name"],
            "size": artifact["size"],
            "sha256": artifact["sha256"],
            "url": artifact["url"],
            "format": artifact["format"],
        },
        "entrypoint": "bin/opencode",
        "installed_tree": f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}",
        "executable_sha256": executable_digest,
        "installed_tree_sha256": installed_tree_digest,
        "version_probe": {
            "argv": ["bin/opencode", "--version"],
            "environment": {
                "HOME": "<stage>/probe-home",
                "OPENCODE_CONFIG": "<stage>/probe-config/opencode.json",
                "OPENCODE_CONFIG_DIR": "<stage>/probe-config",
                "OPENCODE_DISABLE_PROJECT_CONFIG": "1",
                "OPENCODE_DISABLE_EXTERNAL_SKILLS": "1",
                "OPENCODE_DISABLE_CLAUDE_CODE": "1",
                "OPENCODE_DISABLE_SHARE": "1",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "<stage>/probe-tmp",
            },
            "stdout_stderr_sha256": version_probe_digest,
        },
        "manager": "cli-tools/nddev_opencode.py",
        "provenance": {
            "mechanism": "official-github-release-asset",
            "fail_closed_on_missing_digest": True,
            "cli_signature": None,
            "cli_signature_note": "Official CLI zip/tar assets expose GitHub release asset SHA-256 digests but no PGP/cosign signature was published for the CLI assets.",
        },
    }


def read_software_stamp(target: Path) -> dict[str, Any] | None:
    path = software_stamp_path(target)
    info = stat_optional(path, SOFTWARE_STAMP_NAME)
    if info is None:
        return None
    if not stat.S_ISREG(info.st_mode):
        fail("software stamp must be a regular file")
    require_current_user_owner(info, SOFTWARE_STAMP_NAME)
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        fail("software stamp must have mode 0600")
    stamp = read_json_file(path, SOFTWARE_STAMP_NAME)
    if stamp.get("product_name") != PRODUCT_NAME:
        fail("software stamp belongs to another product")
    if stamp.get("canonical_target") != str(target):
        fail("software stamp is bound to a different canonical target")
    return stamp


def software_presence(target: Path) -> list[str]:
    labels = (
        (software_stamp_path(target), SOFTWARE_STAMP_NAME),
        (software_root(target), SOFTWARE_DIR_NAME),
        (software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"),
        (software_entrypoint(target), "bin/opencode"),
    )
    return sorted(label for path, label in labels if path.exists() or path.is_symlink())


def software_status_payload(target: Path) -> dict[str, Any]:
    preflight_software_paths(target)
    payload: dict[str, Any] = {
        "installed": False,
        "current": False,
        "legacy": False,
        "expected_version": OPENCODE_VERSION,
        "version": None,
        "executable": str(software_entrypoint(target)),
        "installed_tree": str(software_current(target)),
        "canonical_target": str(target),
        "status_executes_binary": False,
        "presence": software_presence(target) if target.exists() else [],
        "drift": [],
    }
    if not target.exists():
        return payload
    stamp = read_software_stamp(target)
    if stamp is None:
        return payload
    if stamp.get("schema_version") == 1:
        payload.update(
            {
                "installed": True,
                "legacy": True,
                "version": stamp.get("version"),
                "drift": ["legacy_schema"],
            }
        )
        return payload
    drift: list[str] = []
    if stamp.get("schema_version") != SOFTWARE_STAMP_SCHEMA:
        drift.append("schema_version")
    if stamp.get("opencode_version") != OPENCODE_VERSION:
        drift.append("opencode_version")
    artifact = stamp.get("artifact")
    if not isinstance(artifact, dict) or artifact.get("platform") not in ARTIFACTS:
        drift.append("artifact")
    else:
        expected = ARTIFACTS[artifact["platform"]]
        for key in ("id", "name", "size", "sha256", "url", "format"):
            if artifact.get(key) != expected[key]:
                drift.append(f"artifact.{key}")
    try:
        executable_digest = file_sha256(
            software_entrypoint(target), label="OpenCode entrypoint", executable=True
        )
        installed_tree_digest = tree_sha256(software_current(target))
        if stamp.get("executable_sha256") != executable_digest:
            drift.append("executable_sha256")
        if stamp.get("installed_tree_sha256") != installed_tree_digest:
            drift.append("installed_tree_sha256")
    except ManagerError as exc:
        drift.append(str(exc))
    payload.update(
        {
            "installed": True,
            "current": not drift,
            "version": stamp.get("opencode_version"),
            "drift": sorted(set(drift)),
        }
    )
    return payload


def software_stamp_matches_host(
    stamp: dict[str, Any], host: dict[str, Any], artifact_key: str
) -> bool:
    artifact = stamp.get("artifact")
    product_host = stamp.get("product_host")
    if not isinstance(artifact, dict) or not isinstance(product_host, dict):
        return False
    return (
        artifact.get("platform") == artifact_key
        and product_host.get("product_host") == host.get("product_host")
        and product_host.get("x64_baseline") == host.get("x64_baseline")
    )


def assert_intended_software_state(
    target: Path,
    *,
    entrypoint_content: bytes,
    stamp_content: bytes,
    executable_digest: str,
    installed_tree_digest: str,
) -> None:
    actual_entrypoint = read_regular_file(
        software_entrypoint(target),
        "OpenCode entrypoint",
        executable=True,
        max_bytes=SOFTWARE_MAX_BYTES,
    )[0]
    if (
        actual_entrypoint != entrypoint_content
        or sha256_bytes(actual_entrypoint) != executable_digest
    ):
        fail("OpenCode entrypoint postcondition mismatch")
    if tree_sha256(software_current(target)) != installed_tree_digest:
        fail("OpenCode installed tree postcondition mismatch")
    actual_stamp = read_regular_file(
        software_stamp_path(target),
        SOFTWARE_STAMP_NAME,
        private=True,
        max_bytes=METADATA_MAX_BYTES,
    )[0]
    if actual_stamp != stamp_content:
        fail("OpenCode software stamp postcondition mismatch")


def assert_removed_software_state(target: Path, transaction: SoftwareRemoveTransaction) -> None:
    _ = transaction
    for path, label in (
        (software_stamp_path(target), SOFTWARE_STAMP_NAME),
        (software_entrypoint(target), "OpenCode entrypoint"),
        (software_root(target), SOFTWARE_DIR_NAME),
    ):
        if path.exists() or path.is_symlink():
            fail(f"remove-cli postcondition expected absence: {label}")


def rollback_remove_cli_transaction(
    transaction: SoftwareRemoveTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    target = transaction.target
    if transaction.software_root_stage.exists() or transaction.software_root_stage.is_symlink():
        if software_root(target).exists() or software_root(target).is_symlink():
            remove_private_tree(software_root(target), SOFTWARE_DIR_NAME)
            maybe_inject_fault(fault_injection, "rollback-remove-cli:remove-new-software-root")
        os.replace(transaction.software_root_stage, software_root(target))
        fsync_directory(target)
        fsync_directory(transaction.stage_root)
        maybe_inject_fault(fault_injection, "rollback-remove-cli:restore-software-root")
    if transaction.entrypoint_stage.exists() or transaction.entrypoint_stage.is_symlink():
        ensure_target_private_directory(target, "bin", "OpenCode entrypoint parent", create=True)
        os.replace(transaction.entrypoint_stage, software_entrypoint(target))
        fsync_directory(software_entrypoint(target).parent)
        fsync_directory(transaction.stage_root)
        maybe_inject_fault(fault_injection, "rollback-remove-cli:restore-entrypoint")
    if transaction.stamp_stage.exists() or transaction.stamp_stage.is_symlink():
        os.replace(transaction.stamp_stage, software_stamp_path(target))
        fsync_directory(target)
        fsync_directory(transaction.stage_root)
        maybe_inject_fault(fault_injection, "rollback-remove-cli:restore-stamp")
    if transaction.stage_root.exists() or transaction.stage_root.is_symlink():
        remove_private_tree(transaction.stage_root, "remove-cli stage")
        maybe_inject_fault(fault_injection, "rollback-remove-cli:remove-stage")
    assert_software_snapshot(target, transaction.snapshot)
    maybe_inject_fault(fault_injection, "rollback-remove-cli:postcondition")


def cleanup_remove_cli_transaction(
    transaction: SoftwareRemoveTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        rollback_remove_cli_transaction(transaction, fault_injection=fault_injection)
    except BaseException:
        rollback_remove_cli_transaction(transaction)


def install_cli(
    target: Path,
    *,
    update: bool,
    host_detector: Callable[[], dict[str, Any]] = detect_supported_host,
    metadata_fetcher: Callable[[], dict[str, Any]] = fetch_release_metadata,
    release_verifier: Callable[[dict[str, Any]], None] = verify_release_metadata,
    artifact_resolver: Callable[[str], dict[str, Any]] | None = None,
    artifact_downloader: Callable[[str, Path, int], None] = download_artifact,
    version_probe: Callable[[Path, Path], str] = run_version_probe,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    host = host_detector()
    key = str(host["artifact_platform"])
    snapshot = snapshot_software_state(target)
    if update:
        ensure_target_directory(target, create=False)
    else:
        ensure_target_directory(target, create=True)
    existing = software_status_payload(target)
    if existing["current"]:
        existing_stamp = read_software_stamp(target)
        if existing_stamp is not None and software_stamp_matches_host(existing_stamp, host, key):
            return {
                "ok": True,
                "operation": "update-cli" if update else "install-cli",
                "product_host": host["product_host"],
                "artifact": key,
                "target": str(target),
                "changed": False,
            }
    if not update and existing["presence"] and not existing["current"]:
        fail("install-cli refuses partial or drifted software; use update-cli")
    if update and not existing["presence"]:
        fail("update-cli requires existing target-owned software presence")
    mapping = ARTIFACT_PRODUCT_HOSTS[key]
    if mapping["product_host"] != host["product_host"] or mapping["x64_baseline"] != host.get(
        "x64_baseline"
    ):
        fail("internal product host to artifact mapping mismatch")
    data = metadata_fetcher()
    release_verifier(data)
    artifact = (artifact_resolver or ARTIFACTS.__getitem__)(key)
    root = software_root(target)
    stage_parent = target / f".nddev-opencode-software-stage.{os.getpid()}.{time.time_ns()}"
    stage_current = stage_parent / SOFTWARE_CURRENT_NAME
    archive = stage_parent / artifact["name"]
    previous_current = root / f".previous.{os.getpid()}.{time.time_ns()}"
    previous_current_moved = False
    status: dict[str, Any] | None = None
    try:
        ensure_target_private_directory(
            target,
            stage_parent.relative_to(target).as_posix(),
            "OpenCode stage",
            create=True,
        )
        ensure_target_private_directory(
            target,
            stage_current.relative_to(target).as_posix(),
            "OpenCode staged current",
            create=True,
        )
        ensure_target_private_directory(
            target,
            (stage_current / "bin").relative_to(target).as_posix(),
            "OpenCode staged bin",
            create=True,
        )
        artifact_downloader(artifact["url"], archive, artifact["size"])
        info = require_regular_file(archive, "downloaded OpenCode artifact")
        if info.st_size != artifact["size"]:
            fail("downloaded OpenCode artifact size mismatch")
        if file_sha256(archive, label="downloaded OpenCode artifact") != artifact["sha256"]:
            fail("downloaded OpenCode artifact digest mismatch")
        executable_digest = extract_single_binary(
            archive, artifact, stage_current / "bin" / OPENCODE_COMMAND
        )
        version_probe_digest = version_probe(stage_current / "bin" / OPENCODE_COMMAND, stage_parent)
        installed_tree_digest = tree_sha256(stage_current)
        ensure_target_private_directory(target, SOFTWARE_DIR_NAME, SOFTWARE_DIR_NAME, create=True)
        if software_current(target).exists() or software_current(target).is_symlink():
            require_real_private_directory(
                software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"
            )
            os.replace(software_current(target), previous_current)
            previous_current_moved = True
        os.replace(stage_current, software_current(target))
        fsync_directory(root)
        maybe_inject_fault(fault_injection, "software:swap-current")
        ensure_target_private_directory(target, "bin", "OpenCode entrypoint parent", create=True)
        entrypoint_content = read_regular_file(
            software_current(target) / "bin" / OPENCODE_COMMAND,
            "staged OpenCode entrypoint",
            executable=True,
            max_bytes=SOFTWARE_MAX_BYTES,
        )[0]
        atomic_write(
            software_entrypoint(target),
            entrypoint_content,
            mode=OWNER_EXEC_MODE,
            fault_injection=fault_injection,
            fault_label="software:entrypoint",
        )
        maybe_inject_fault(fault_injection, "software:copy-entrypoint")
        maybe_inject_fault(fault_injection, "software:chmod-entrypoint")
        stamp = software_stamp(
            target,
            host,
            key,
            artifact,
            executable_digest=executable_digest,
            installed_tree_digest=installed_tree_digest,
            version_probe_digest=version_probe_digest,
        )
        ensure_target_private_parent(
            target, software_stamp_path(target), SOFTWARE_STAMP_NAME, create=True
        )
        stamp_content = canonical_json(stamp)
        atomic_write(
            software_stamp_path(target),
            stamp_content,
            fault_injection=fault_injection,
            fault_label="software:stamp",
        )
        maybe_inject_fault(fault_injection, "software:stamp")
        maybe_inject_fault(fault_injection, "software:fsync")
        fsync_directory(target)
        assert_intended_software_state(
            target,
            entrypoint_content=entrypoint_content,
            stamp_content=stamp_content,
            executable_digest=executable_digest,
            installed_tree_digest=installed_tree_digest,
        )
        status = software_status_payload(target)
        if not status["current"]:
            fail(f"installed OpenCode software is not current: {status['drift']}")
    except BaseException:
        with contextlib.suppress(ManagerError, OSError):
            remove_private_tree(stage_parent, "OpenCode stage")
        rollback_software_state(
            target,
            snapshot,
            previous_current=previous_current if previous_current_moved else None,
            fault_injection=rollback_fault_injection,
        )
        raise
    finally:
        with contextlib.suppress(ManagerError, OSError):
            remove_private_tree(stage_parent, "OpenCode stage")
    best_effort_remove_private_tree(
        previous_current,
        "previous OpenCode software tree",
        fault_injection=fault_injection,
        fault_point="software:cleanup-previous-current",
    )
    return {
        "ok": True,
        "operation": "update-cli" if update else "install-cli",
        "product_host": host["product_host"],
        "artifact": key,
        "target": str(target),
        "changed": True,
    }


def remove_cli(
    target: Path,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    if not ensure_target_directory(target, create=False):
        return {"ok": True, "operation": "remove-cli", "target": str(target), "removed": False}
    preflight_software_paths(target)
    if not software_presence(target):
        return {
            "ok": True,
            "operation": "remove-cli",
            "target": str(target),
            "removed": False,
            "changed": False,
        }
    snapshot = snapshot_software_state(target)
    stage_root = target / f".nddev-opencode-software-remove-stage.{os.getpid()}.{time.time_ns()}"
    transaction = SoftwareRemoveTransaction(
        target=target,
        stage_root=stage_root,
        software_root_stage=stage_root / "software-root",
        entrypoint_stage=stage_root / "entrypoint",
        stamp_stage=stage_root / "software-stamp",
        snapshot=snapshot,
    )
    try:
        ensure_target_private_directory(
            target,
            stage_root.relative_to(target).as_posix(),
            "remove-cli stage",
            create=True,
        )
        if software_stamp_path(target).exists() or software_stamp_path(target).is_symlink():
            require_regular_file(software_stamp_path(target), SOFTWARE_STAMP_NAME, private=True)
            os.replace(software_stamp_path(target), transaction.stamp_stage)
            maybe_inject_fault(fault_injection, "remove-cli:unlink-stamp")
            fsync_directory(target)
            fsync_directory(stage_root)
            maybe_inject_fault(fault_injection, "remove-cli:stamp-parent-fsync")
        if software_entrypoint(target).exists() or software_entrypoint(target).is_symlink():
            require_regular_file(
                software_entrypoint(target), "OpenCode entrypoint", executable=True
            )
            os.replace(software_entrypoint(target), transaction.entrypoint_stage)
            maybe_inject_fault(fault_injection, "remove-cli:unlink-entrypoint")
            fsync_directory(software_entrypoint(target).parent)
            fsync_directory(stage_root)
            maybe_inject_fault(fault_injection, "remove-cli:entrypoint-parent-fsync")
        if software_root(target).exists() or software_root(target).is_symlink():
            require_real_private_directory(software_root(target), SOFTWARE_DIR_NAME)
            os.replace(software_root(target), transaction.software_root_stage)
            maybe_inject_fault(fault_injection, "remove-cli:tree-swap")
            fsync_directory(target)
            fsync_directory(stage_root)
            maybe_inject_fault(fault_injection, "remove-cli:software-parent-fsync")
        assert_removed_software_state(target, transaction)
        maybe_inject_fault(fault_injection, "remove-cli:postcondition")
    except BaseException:
        cleanup_remove_cli_transaction(
            transaction,
            fault_injection=rollback_fault_injection,
        )
        raise
    best_effort_remove_private_tree(
        stage_root,
        "remove-cli stage",
        fault_injection=fault_injection,
        fault_point="remove-cli:cleanup-stage",
    )
    with contextlib.suppress(ManagerError, OSError):
        (target / "bin").rmdir()
        fsync_directory(target)
    return {
        "ok": True,
        "operation": "remove-cli",
        "target": str(target),
        "removed": True,
        "changed": True,
    }


def system_lock_root() -> Path:
    base = (
        Path("/private/tmp")
        if sys.platform == "darwin" and Path("/private/tmp").is_dir()
        else Path("/tmp")
    )
    uid = current_uid()
    return base / f"nddev-opencode-locks-{uid if uid is not None else 'nouid'}"


def lock_file(path: Path) -> LockHandle:
    parent_existed = path.parent.exists() or path.parent.is_symlink()
    file_existed = path.exists() or path.is_symlink()
    ensure_real_private_directory(path.parent, f"lock parent {path.parent}", create=True)
    parent_before = require_real_private_directory(path.parent, f"lock parent {path.parent}")
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    try:
        fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
        os.fchmod(fd, OWNER_FILE_MODE)
    except FileExistsError:
        try:
            fd = os.open(path, flags)
        except OSError as exc:
            fail(f"cannot open lock file safely: {path}: {exc}")
    except OSError as exc:
        fail(f"cannot open lock file safely: {path}: {exc}")
    try:
        os.set_inheritable(fd, False)
        opened = os.fstat(fd)
        on_disk = path.lstat()
        parent_after = require_real_private_directory(path.parent, f"lock parent {path.parent}")
        if identity_of(parent_before) != identity_of(parent_after):
            raise ConcurrentTargetChange(f"lock parent changed while opening: {path.parent}")
        if identity_of(opened) != identity_of(on_disk):
            raise ConcurrentTargetChange(f"lock file identity changed while opening: {path}")
        if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
            fail(f"lock file must be a regular non-symlink file: {path}")
        require_current_user_owner(opened, f"lock file {path}")
        if stat.S_IMODE(opened.st_mode) != OWNER_FILE_MODE:
            fail(f"lock file must have mode 0600: {path}")
        if opened.st_nlink != 1:
            fail(f"lock file must not have hard-link aliases: {path}")
    except BaseException:
        os.close(fd)
        raise
    try:
        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        os.close(fd)
        fail(f"target is already locked: {path}")
    return LockHandle(fd=fd, path=path, parent_existed=parent_existed, file_existed=file_existed)


def cleanup_created_lock(handle: LockHandle) -> None:
    cleanup_errors: list[BaseException] = []
    if not handle.file_existed and (handle.path.exists() or handle.path.is_symlink()):
        try:
            require_regular_file(handle.path, f"lock file {handle.path}", private=True)
            handle.path.unlink()
            fsync_directory(handle.path.parent)
        except BaseException as exc:
            cleanup_errors.append(exc)
    if not handle.parent_existed and (
        handle.path.parent.exists() or handle.path.parent.is_symlink()
    ):
        try:
            require_real_private_directory(handle.path.parent, f"lock parent {handle.path.parent}")
            handle.path.parent.rmdir()
            fsync_directory(handle.path.parent.parent)
        except OSError:
            pass
        except BaseException as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        raise cleanup_errors[0]


@contextlib.contextmanager
def target_locks(target: Path, *, create_target: bool) -> Iterator[None]:
    canonical = str(target)
    token = sha256_bytes(canonical.encode("utf-8"))
    external = system_lock_root() / f"{token}.lock"
    target_existed = target.exists() or target.is_symlink()
    external_handle = lock_file(external)
    internal_handle: LockHandle | None = None
    success = False
    try:
        target_exists = ensure_target_directory(target, create=create_target)
        if target_exists:
            internal = target / ".nddev-opencode-lock" / "lock"
            internal_handle = lock_file(internal)
        yield
        success = True
    finally:
        if internal_handle is not None:
            with contextlib.suppress(OSError):
                fcntl.flock(internal_handle.fd, fcntl.LOCK_UN)
                os.close(internal_handle.fd)
        with contextlib.suppress(OSError):
            fcntl.flock(external_handle.fd, fcntl.LOCK_UN)
            os.close(external_handle.fd)
        if not success:
            if internal_handle is not None:
                with contextlib.suppress(ManagerError, OSError):
                    cleanup_created_lock(internal_handle)
            with contextlib.suppress(ManagerError, OSError):
                cleanup_created_lock(external_handle)
            if not target_existed and (target.exists() or target.is_symlink()):
                with contextlib.suppress(OSError):
                    target.rmdir()
                    fsync_directory(target.parent)


def validate_launch_args(args: list[str], profile_id: str) -> None:
    index = 0
    while index < len(args):
        item = args[index]
        if item == "--":
            index += 1
            continue
        if item in LAUNCH_BLOCKED_BOOLEAN_FLAGS:
            fail(f"launch argument is blocked ({LAUNCH_BLOCKED_BOOLEAN_FLAGS[item]}): {item}")
        if item in LAUNCH_BLOCKED_SHORT_FLAGS:
            fail(f"launch argument is blocked ({LAUNCH_BLOCKED_SHORT_FLAGS[item]}): {item}")
        if item in LAUNCH_BLOCKED_VALUE_FLAGS:
            fail(f"launch argument is blocked ({LAUNCH_BLOCKED_VALUE_FLAGS[item]}): {item}")
        if any(item.startswith(flag + "=") for flag in LAUNCH_BLOCKED_VALUE_FLAGS):
            flag = item.split("=", 1)[0]
            fail(f"launch argument is blocked ({LAUNCH_BLOCKED_VALUE_FLAGS[flag]}): {flag}")
        if item in LAUNCH_BLOCKED_COMMANDS:
            fail(f"launch command is blocked ({LAUNCH_BLOCKED_COMMANDS[item]}): {item}")
        index += 1
    if profile_id == "safe" and any(
        item in {"--auto", "--yolo", "--dangerously-skip-permissions"} for item in args
    ):
        fail("safe profile cannot use permission bypass flags")


def runtime_dirs(target: Path) -> dict[str, Path]:
    return {
        "HOME": target / ".runtime-home",
        "XDG_CONFIG_HOME": target / ".xdg" / "config",
        "XDG_DATA_HOME": target / ".xdg" / "data",
        "XDG_STATE_HOME": target / ".xdg" / "state",
        "XDG_CACHE_HOME": target / ".xdg" / "cache",
        "TMPDIR": target / ".tmp",
    }


def launch_env(target: Path) -> dict[str, str]:
    dirs = runtime_dirs(target)
    for directory in dirs.values():
        ensure_target_private_directory(
            target,
            require_target_contained(target, directory, "runtime directory").as_posix(),
            "runtime directory",
            create=True,
        )
    env = {
        "PATH": "/usr/bin:/bin",
        "OPENCODE_CONFIG": str(target / "opencode.json"),
        "OPENCODE_CONFIG_DIR": str(target),
        **{name: str(path) for name, path in dirs.items()},
        **LAUNCH_FORCED_ENV,
    }
    return env


def validate_executable_for_launch(target: Path, stamp: dict[str, Any]) -> dict[str, Any]:
    executable = software_entrypoint(target)
    before = require_regular_file(executable, "OpenCode entrypoint", executable=True)
    digest = file_sha256(executable, label="OpenCode entrypoint", executable=True)
    after = require_regular_file(executable, "OpenCode entrypoint", executable=True)
    if identity_of(before) != identity_of(after):
        raise ConcurrentTargetChange("OpenCode entrypoint identity changed before launch")
    if stamp.get("executable_sha256") != digest:
        fail("OpenCode entrypoint digest does not match software stamp")
    return {"device": before.st_dev, "inode": before.st_ino, "sha256": digest}


def launch(target: Path, child_args: list[str]) -> int:
    detect_supported_host()
    with target_locks(target, create_target=False):
        status = current_status(target)
        if not status.get("managed") or status.get("legacy") or status.get("drift"):
            fail("launch requires a current clean managed setup target")
        software_status = software_status_payload(target)
        if not software_status["current"]:
            fail(
                f"launch requires current target-owned OpenCode software: {software_status['drift']}"
            )
        stamp = read_software_stamp(target)
        if stamp is None:
            fail("software stamp is missing")
        validate_launch_args(child_args, str(status["profile_id"]))
        validate_executable_for_launch(target, stamp)
        process = subprocess.Popen(
            [str(software_entrypoint(target)), *child_args],
            env=launch_env(target),
            close_fds=True,
        )
        return process.wait()


def emit(payload: Any, *, json_output: bool) -> None:
    if json_output:
        print(json.dumps(payload, indent=2, sort_keys=True))
        return
    if isinstance(payload, dict):
        for key, value in payload.items():
            print(f"{key}: {value}")
    else:
        print(payload)


def add_target(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--target", required=True, help="absolute isolated OpenCode target")


def add_json(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--json", action="store_true", help="emit JSON")


def build_parser() -> argparse.ArgumentParser:
    parser = ManagerArgumentParser(description="NDDev OpenCode setup/profile manager")
    sub = parser.add_subparsers(dest="command", required=True, parser_class=ManagerArgumentParser)

    p = sub.add_parser("list", help="list available setup/profile choices")
    add_json(p)

    for name in ("status", "software-status", "remove", "remove-cli"):
        p = sub.add_parser(name, help=f"{name} target state")
        add_target(p)
        add_json(p)

    for name in ("plan", "install", "apply", "switch"):
        p = sub.add_parser(name, help=f"{name} managed setup/profile")
        add_target(p)
        p.add_argument("--setup", default=CONTENT_SETUP_ID, choices=[CONTENT_SETUP_ID])
        p.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=PROFILE_IDS)
        add_json(p)

    p = sub.add_parser("migrate", help="migrate a legacy schema-1 target")
    add_target(p)
    p.add_argument("--profile", choices=PROFILE_IDS)
    add_json(p)

    p = sub.add_parser("restore", help="restore a managed backup")
    add_target(p)
    p.add_argument("--backup", type=int, required=True)
    add_json(p)

    for name in ("install-cli", "update-cli"):
        p = sub.add_parser(name, help=f"{name} target-owned OpenCode software")
        add_target(p)
        add_json(p)

    p = sub.add_parser("launch", help="launch target-owned OpenCode")
    add_target(p)
    p.add_argument("args", nargs=argparse.REMAINDER)
    return parser


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    global PARSER_ARGV_FOR_JSON
    actual = tuple(sys.argv[1:] if argv is None else argv)
    previous = PARSER_ARGV_FOR_JSON
    PARSER_ARGV_FOR_JSON = actual
    try:
        return build_parser().parse_args(list(actual))
    finally:
        PARSER_ARGV_FOR_JSON = previous


def main(argv: list[str] | None = None) -> int:
    try:
        args = parse_args(argv)
    except ManagerCliParseError as exc:
        print(
            json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr
        )
        return 2
    try:
        command = args.command
        if command == "list":
            payload = {
                "setup_ids": [CONTENT_SETUP_ID],
                "profile_ids": list(PROFILE_IDS),
                "default_setup": CONTENT_SETUP_ID,
                "default_profile": DEFAULT_PROFILE_ID,
                "setups": [
                    {
                        "id": CONTENT_SETUP_ID,
                        "description": render_content_setup().description,
                        "managed_files": list(MANAGED_FILES),
                    }
                ],
                "profiles": [
                    {"id": profile_id, "description": render_profile(profile_id).description}
                    for profile_id in PROFILE_IDS
                ],
            }
            emit(payload, json_output=args.json)
            return 0

        if command == "launch":
            target = resolve_target(args.target)
            child_args = list(args.args)
            if child_args and child_args[0] == "--":
                child_args = child_args[1:]
            return launch(target, child_args)

        target = resolve_target(args.target) if hasattr(args, "target") else None
        assert target is not None
        json_output = bool(getattr(args, "json", False))

        if command == "status":
            payload = current_status(target)
        elif command == "software-status":
            payload = software_status_payload(target)
        elif command == "plan":
            payload = plan_payload(target, render_profile(args.profile))
        else:
            create_for_command = command in {"install", "apply", "install-cli"}
            with target_locks(target, create_target=create_for_command):
                if command in {"install", "apply"}:
                    payload = install_or_switch(
                        target, render_profile(args.profile), operation="install"
                    )
                elif command == "switch":
                    payload = install_or_switch(
                        target, render_profile(args.profile), operation="switch"
                    )
                elif command == "migrate":
                    payload = migrate_target(target, args.profile)
                elif command == "restore":
                    payload = restore_target(target, args.backup)
                elif command == "remove":
                    payload = remove_target(target)
                elif command == "install-cli":
                    payload = install_cli(target, update=False)
                elif command == "update-cli":
                    payload = install_cli(target, update=True)
                elif command == "remove-cli":
                    payload = remove_cli(target)
                else:
                    fail(f"unknown command: {command}")
        emit(payload, json_output=json_output)
        return 0
    except ManagerError as exc:
        if getattr(args, "command", None) != "launch" and getattr(args, "json", False):
            print(
                json.dumps({"ok": False, "error": str(exc)}, indent=2, sort_keys=True),
                file=sys.stderr,
            )
        else:
            print(f"nddev-opencode: error: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())
