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
from typing import Any, NoReturn, Optional

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
CLEANUP_PARENT_NAME = ".nddev-opencode-cleanup-pending"
CLEANUP_JOURNAL_NAME = "NDDEV-OPENCODE-CLEANUP.json"
CLEANUP_PREPARE_NAME = "NDDEV-OPENCODE-CLEANUP.prepare.json"
STAMP_SCHEMA = 2
BACKUP_SCHEMA = 3
SOFTWARE_STAMP_SCHEMA = 2
CLEANUP_JOURNAL_SCHEMA = 1
CLEANUP_PREPARE_SCHEMA = 1
MAX_BACKUPS = 10
MAX_CLEANUP_PATHS = 8
MAX_CLEANUP_OBJECTS = 512
OWNER_FILE_MODE = 0o600
OWNER_EXEC_MODE = 0o700
OWNER_DIR_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
CLEANUP_JOURNAL_MAX_BYTES = 1024 * 1024
CLEANUP_PREPARE_MAX_BYTES = 2 * 1024 * 1024
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
TreeIdentityRow = tuple[str, str, int, tuple[int, int], int, int, Optional[str]]
BackupObjectGraphRow = tuple[str, str, int, tuple[int, int], int, int, Optional[bytes]]

ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
ANCHOR_TEMP_RANDOM_PATTERN = re.compile(r"[A-Za-z0-9_]{8}\Z")
ANCHOR_PARENT_SCAN_LIMIT = 4096
READ_ONLY_RETRY_ATTEMPTS = 4

OPENCODE_VERSION = "1.18.13"
OPENCODE_RELEASE_TAG = "v1.18.13"
OPENCODE_RELEASE_API = "https://api.github.com/repos/anomalyco/opencode/releases/tags/v1.18.13"
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
        "name": "opencode-darwin-arm64.zip",
        "size": 45253324,
        "sha256": "6a85ae6de1aeb8e39ae4d977337b03f49168c2a827ee37b6f82c39471d711c63",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-darwin-arm64.zip",
        "format": "zip",
    },
    "darwin-x64": {
        "name": "opencode-darwin-x64.zip",
        "size": 47484318,
        "sha256": "5a119461c6ba265a9406bad616e2f845ee66ab8d004be5b5336547af3415c3fe",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-darwin-x64.zip",
        "format": "zip",
    },
    "darwin-x64-baseline": {
        "name": "opencode-darwin-x64-baseline.zip",
        "size": 47484318,
        "sha256": "71fd8020dec05f15241788b819944fb2c8e43504b93b53077d632c19fbad624a",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-darwin-x64-baseline.zip",
        "format": "zip",
    },
    "linux-arm64": {
        "name": "opencode-linux-arm64.tar.gz",
        "size": 59424771,
        "sha256": "dd4ac8c2167a8338caf296b002c955141d52a2e9c95ee0a95f4ae9939c293ab0",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-linux-arm64.tar.gz",
        "format": "tar.gz",
    },
    "linux-x64": {
        "name": "opencode-linux-x64.tar.gz",
        "size": 59609644,
        "sha256": "8d500b20fed2d26e537e221895b1a575476571b4f0089bb29fb13eeb8eb9e937",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-linux-x64.tar.gz",
        "format": "tar.gz",
    },
    "linux-x64-baseline": {
        "name": "opencode-linux-x64-baseline.tar.gz",
        "size": 59609645,
        "sha256": "d4471cc260d246b9b2df237ac03fafa75b6723306a6bc306e54dce6e9aa824db",
        "url": "https://github.com/anomalyco/opencode/releases/download/v1.18.13/opencode-linux-x64-baseline.tar.gz",
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
HOST_PRECHECK_COMMANDS = {
    "status",
    "software-status",
    "plan",
    "install",
    "update",
    "switch",
    "migrate",
    "restore",
    "remove",
    "install-cli",
    "update-cli",
    "remove-cli",
    "launch",
}


class ManagerError(Exception):
    """A user-facing lifecycle failure."""


class ConcurrentTargetChange(ManagerError):
    """A target changed while the manager was validating or mutating it."""


class CleanupJournalPublishedPending(ManagerError):
    """Cleanup journal final path is visible; later cleanup must drain it."""


class CleanupJournalPublishedInvalid(ManagerError):
    """Cleanup journal final path is visible but cannot prove valid pending state."""


class CleanupPreparePublishedPending(ManagerError):
    """Cleanup prepare intent final path is visible; later recovery must complete it."""


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
    mode: int | None = None
    identity: tuple[int, int] | None = None
    mtime_ns: int | None = None
    size: int | None = None


@dataclass(frozen=True)
class DirectorySnapshotEntry:
    mode: int
    identity: tuple[int, int]
    atime_ns: int
    mtime_ns: int
    size: int


@dataclass(frozen=True)
class ManagedStateSnapshot:
    target_existed: bool
    target_parent: DirectorySnapshotEntry | None
    files: dict[str, SnapshotEntry]
    directories: dict[str, DirectorySnapshotEntry]


@dataclass(frozen=True)
class ManagedMutationTransaction:
    target: Path
    stage_root: Path
    undo_root: Path
    snapshot: ManagedStateSnapshot
    expected: dict[str, SnapshotEntry] | None


@dataclass(frozen=True)
class CleanupPromotionResult:
    payload: dict[str, Any]
    publication_pending: bool


@dataclass(frozen=True)
class BinaryFileSnapshot:
    content: bytes | None
    mode: int | None
    identity: tuple[int, int] | None
    mtime_ns: int | None
    size: int | None


@dataclass(frozen=True)
class SoftwareStateSnapshot:
    target_existed: bool
    target_parent: DirectorySnapshotEntry | None
    software_root_existed: bool
    current_existed: bool
    current_tree_digest: str | None
    current_tree_identity: tuple[TreeIdentityRow, ...] | None
    bin_dir_existed: bool
    entrypoint: BinaryFileSnapshot
    stamp: BinaryFileSnapshot
    directories: dict[str, DirectorySnapshotEntry]


@dataclass(frozen=True)
class SoftwareRemoveTransaction:
    target: Path
    stage_root: Path
    software_root_stage: Path
    entrypoint_stage: Path
    stamp_stage: Path
    snapshot: SoftwareStateSnapshot


@dataclass(frozen=True)
class BackupObjectGraphSnapshot:
    existed: bool
    rows: tuple[BackupObjectGraphRow, ...]


@dataclass(frozen=True)
class BackupTransaction:
    root: Path
    staging_root: Path
    previous_root: Path
    root_existed: bool
    payloads_before: list[dict[str, Any]]
    object_graph_before: BackupObjectGraphSnapshot


@dataclass(frozen=True)
class LockHandle:
    fd: int
    path: Path
    parent_existed: bool
    file_existed: bool
    parent_snapshot: DirectorySnapshotEntry | None
    container_snapshot: DirectorySnapshotEntry | None
    file_identity: tuple[int, int] | None


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


def snapshot_private_directory_metadata(path: Path, label: str) -> DirectorySnapshotEntry | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    require_current_user_owner(info, label)
    if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
        fail(f"{label} must have mode 0700")
    return DirectorySnapshotEntry(
        mode=stat.S_IMODE(info.st_mode),
        identity=identity_of(info),
        atime_ns=info.st_atime_ns,
        mtime_ns=info.st_mtime_ns,
        size=info.st_size,
    )


def restore_private_directory_metadata(
    path: Path, snapshot: DirectorySnapshotEntry, label: str
) -> None:
    info = require_real_private_directory(path, label)
    if identity_of(info) != snapshot.identity:
        raise ConcurrentTargetChange(f"{label} identity changed during cleanup")
    os.chmod(path, snapshot.mode)
    os.utime(path, ns=(snapshot.atime_ns, snapshot.mtime_ns))
    fsync_directory(path)


def assert_private_directory_metadata(
    path: Path, snapshot: DirectorySnapshotEntry, label: str
) -> None:
    info = require_real_private_directory(path, label)
    if identity_of(info) != snapshot.identity:
        fail(f"{label} rollback postcondition identity mismatch")
    if stat.S_IMODE(info.st_mode) != snapshot.mode:
        fail(f"{label} rollback postcondition mode mismatch")
    if info.st_size != snapshot.size:
        fail(f"{label} rollback postcondition size mismatch")
    if info.st_mtime_ns != snapshot.mtime_ns:
        fail(f"{label} rollback postcondition mtime mismatch")


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
    allow_hardlink_aliases: bool = False,
) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1 and not allow_hardlink_aliases:
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
    allow_hardlink_aliases: bool = False,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(
        path,
        label,
        private=private,
        executable=executable,
        allow_hardlink_aliases=allow_hardlink_aliases,
    )
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
    final = require_regular_file(
        path,
        label,
        private=private,
        executable=executable,
        allow_hardlink_aliases=allow_hardlink_aliases,
    )
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
    target = Path(raw_target)
    if not target.is_absolute():
        fail("--target must be an absolute path")
    if target == Path(target.anchor):
        fail("filesystem root cannot be a target")
    return target


def resolve_target_locked(lexical_target: Path) -> Path:
    raw_info = stat_optional(lexical_target, "--target")
    if raw_info is not None and not stat.S_ISDIR(raw_info.st_mode):
        fail("--target must be a real directory")
    target = lexical_target.resolve(strict=False)
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
    for relative in set[str](LEGACY_MANAGED_FILES) - set[str](MANAGED_FILES):
        if target_file_exists(target, relative):
            drift.append(f"stale:{relative}")
    return sorted(drift)


def managed_parent_relatives() -> list[str]:
    relatives = {"."}
    for name in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        parent = safe_relative_path(name).parent
        if str(parent) == ".":
            continue
        current = Path()
        for part in parent.parts:
            current = current / part
            relatives.add(current.as_posix())
    return sorted(relatives, key=lambda value: (value.count("/"), value))


def snapshot_known_files(target: Path) -> dict[str, SnapshotEntry]:
    result: dict[str, SnapshotEntry] = {}
    target_exists = ensure_target_directory(target, create=False)
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        if target_exists and target_file_exists(target, relative):
            content, info = read_regular_file(
                target_path(target, relative),
                f"managed path {relative}",
                max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
            )
            text = content.decode("utf-8")
            result[relative] = SnapshotEntry(
                text=text,
                digest=sha256_bytes(content),
                mode=stat.S_IMODE(info.st_mode),
                identity=identity_of(info),
                mtime_ns=info.st_mtime_ns,
                size=info.st_size,
            )
        else:
            result[relative] = SnapshotEntry(text=None, digest=None)
    return result


def snapshot_managed_directories(target: Path) -> dict[str, DirectorySnapshotEntry]:
    result: dict[str, DirectorySnapshotEntry] = {}
    if not ensure_target_directory(target, create=False):
        return result
    for relative in managed_parent_relatives():
        path = target if relative == "." else target / safe_relative_path(relative)
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent must be a real directory: {path}")
        require_current_user_owner(info, f"managed parent {path}")
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"managed parent must have mode 0700: {path}")
        result[relative] = DirectorySnapshotEntry(
            mode=stat.S_IMODE(info.st_mode),
            identity=identity_of(info),
            atime_ns=info.st_atime_ns,
            mtime_ns=info.st_mtime_ns,
            size=info.st_size,
        )
    return result


def snapshot_managed_state(target: Path) -> ManagedStateSnapshot:
    return ManagedStateSnapshot(
        target_existed=ensure_target_directory(target, create=False),
        target_parent=snapshot_private_directory_metadata(target.parent, "target parent"),
        files=snapshot_known_files(target),
        directories=snapshot_managed_directories(target),
    )


def assert_snapshot(
    target: Path, snapshot: dict[str, SnapshotEntry] | ManagedStateSnapshot
) -> None:
    files = snapshot.files if isinstance(snapshot, ManagedStateSnapshot) else snapshot
    for relative, expected in files.items():
        exists = ensure_target_directory(target, create=False) and target_file_exists(
            target, relative
        )
        if expected.digest is None:
            if exists:
                raise ConcurrentTargetChange(f"managed path changed concurrently: {relative}")
            continue
        if not exists:
            raise ConcurrentTargetChange(f"managed path changed concurrently: {relative}")
        content, info = read_regular_file(
            target_path(target, relative),
            f"managed path {relative}",
            max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
        )
        if (
            sha256_bytes(content) != expected.digest
            or stat.S_IMODE(info.st_mode) != expected.mode
            or identity_of(info) != expected.identity
            or info.st_mtime_ns != expected.mtime_ns
            or info.st_size != expected.size
        ):
            raise ConcurrentTargetChange(f"managed path changed concurrently: {relative}")


def assert_managed_snapshot(target: Path, snapshot: ManagedStateSnapshot) -> None:
    if not snapshot.target_existed:
        if target.exists() or target.is_symlink():
            fail("managed target rollback postcondition expected absence")
        if snapshot.target_parent is not None:
            assert_private_directory_metadata(
                target.parent, snapshot.target_parent, "target parent"
            )
        return
    require_real_private_directory(target, "target")
    for relative, expected in snapshot.files.items():
        path = target_path(target, relative)
        if expected.digest is None:
            if path.exists() or path.is_symlink():
                fail(f"managed path rollback postcondition expected absence: {relative}")
            continue
        content, info = read_regular_file(
            path,
            f"managed path {relative}",
            max_bytes=MANAGED_PAYLOAD_MAX_BYTES,
        )
        if sha256_bytes(content) != expected.digest:
            fail(f"managed path rollback postcondition digest mismatch: {relative}")
        if stat.S_IMODE(info.st_mode) != expected.mode:
            fail(f"managed path rollback postcondition mode mismatch: {relative}")
        if identity_of(info) != expected.identity:
            fail(f"managed path rollback postcondition identity mismatch: {relative}")
        if info.st_mtime_ns != expected.mtime_ns or info.st_size != expected.size:
            fail(f"managed path rollback postcondition stat mismatch: {relative}")
    for relative in reversed(managed_parent_relatives()):
        path = target if relative == "." else target / safe_relative_path(relative)
        if relative not in snapshot.directories:
            if path.exists() or path.is_symlink():
                fail(f"managed directory rollback postcondition expected absence: {relative}")
            continue
        expected_dir = snapshot.directories[relative]
        info = require_real_private_directory(path, f"managed parent {relative}")
        if identity_of(info) != expected_dir.identity:
            fail(f"managed directory rollback postcondition identity mismatch: {relative}")
        if stat.S_IMODE(info.st_mode) != expected_dir.mode:
            fail(f"managed directory rollback postcondition mode mismatch: {relative}")
        if info.st_mtime_ns != expected_dir.mtime_ns:
            fail(f"managed directory rollback postcondition mtime mismatch: {relative}")
    if snapshot.target_parent is not None:
        assert_private_directory_metadata(target.parent, snapshot.target_parent, "target parent")


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


def prune_empty_managed_directories(target: Path) -> None:
    """Remove only empty parent chains of paths owned by the setup contract."""
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        remove_empty_managed_parents(target, relative)


def ensure_private_directory_chain(root: Path, relative: Path, label: str) -> None:
    require_real_private_directory(root, label)
    current = root
    for part in relative.parts:
        current = current / part
        ensure_real_private_directory(current, f"{label} {current.relative_to(root)}", create=True)


def managed_stage_path(root: Path, relative: str) -> Path:
    safe = safe_relative_path(relative)
    if str(safe.parent) != ".":
        ensure_private_directory_chain(root, safe.parent, "managed undo root")
    return root / safe


def prepare_managed_transaction(
    target: Path,
    expected: dict[str, SnapshotEntry] | None,
    snapshot: ManagedStateSnapshot,
) -> ManagedMutationTransaction:
    stage_root = Path(
        tempfile.mkdtemp(
            prefix=f".{target.name}.nddev-opencode-managed-stage.",
            dir=str(target.parent),
        )
    )
    try:
        os.chmod(stage_root, OWNER_DIR_MODE)
        require_real_private_directory(stage_root, "managed stage root")
        undo_root = stage_root / "undo"
        undo_root.mkdir(mode=OWNER_DIR_MODE)
        os.chmod(undo_root, OWNER_DIR_MODE)
        require_real_private_directory(undo_root, "managed undo root")
        fsync_directory(stage_root)
        fsync_directory(stage_root.parent)
    except BaseException:
        cleanup_private_tree_required(stage_root, "managed stage root")
        if snapshot.target_parent is not None:
            restore_private_directory_metadata(
                target.parent, snapshot.target_parent, "target parent"
            )
        raise
    return ManagedMutationTransaction(
        target=target,
        stage_root=stage_root,
        undo_root=undo_root,
        snapshot=snapshot,
        expected=expected,
    )


def move_managed_original_to_undo(
    transaction: ManagedMutationTransaction,
    relative: str,
    path: Path,
    *,
    fault_injection: FaultInjector | None,
) -> Path:
    undo = managed_stage_path(transaction.undo_root, relative)
    if undo.exists() or undo.is_symlink():
        fail(f"managed undo path already exists: {relative}")
    require_regular_file(path, f"managed path {relative}")
    os.replace(path, undo)
    fsync_directory(path.parent)
    fsync_directory(undo.parent)
    maybe_inject_fault(fault_injection, f"managed:move-original:{relative}")
    return undo


def replace_managed_state(
    target: Path,
    desired: dict[str, bytes | None],
    expected: dict[str, SnapshotEntry] | None,
    *,
    transaction: ManagedMutationTransaction,
    fault_injection: FaultInjector | None = None,
) -> None:
    ensure_target_directory(target, create=True)
    if transaction.expected is not expected:
        fail("managed transaction expected snapshot mismatch")
    if transaction.expected is not None:
        assert_snapshot(target, transaction.expected)
    for relative in KNOWN_MANAGED_FILES:
        path = target_path(target, relative)
        content = desired.get(relative)
        if content is None:
            info = stat_optional(path, f"managed path {relative}")
            if info is not None:
                move_managed_original_to_undo(
                    transaction, relative, path, fault_injection=fault_injection
                )
                maybe_inject_fault(fault_injection, f"remove:{relative}")
            continue
        ensure_target_private_parent(target, path, f"managed path {relative}", create=True)
        if stat_optional(path, f"managed path {relative}") is not None:
            move_managed_original_to_undo(
                transaction, relative, path, fault_injection=fault_injection
            )
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
            move_managed_original_to_undo(
                transaction, STAMP_NAME, stamp_path(target), fault_injection=fault_injection
            )
            maybe_inject_fault(fault_injection, f"remove:{STAMP_NAME}")
    else:
        ensure_target_private_parent(target, stamp_path(target), STAMP_NAME, create=True)
        if stat_optional(stamp_path(target), STAMP_NAME) is not None:
            move_managed_original_to_undo(
                transaction, STAMP_NAME, stamp_path(target), fault_injection=fault_injection
            )
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


def restore_managed_directory_metadata(
    target: Path,
    snapshot: ManagedStateSnapshot,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    for relative in reversed(managed_parent_relatives()):
        expected = snapshot.directories.get(relative)
        if expected is None:
            continue
        path = target if relative == "." else target / safe_relative_path(relative)
        restore_private_directory_metadata(path, expected, f"managed parent {relative}")
        maybe_inject_fault(fault_injection, f"rollback-managed:restore-dir:{relative}")


def rollback_managed_transaction_once(
    transaction: ManagedMutationTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    target = transaction.target
    snapshot = transaction.snapshot
    if target.exists() or target.is_symlink():
        require_real_private_directory(target, "target")
    for relative in (*KNOWN_MANAGED_FILES, STAMP_NAME):
        path = target_path(target, relative)
        undo = transaction.undo_root / safe_relative_path(relative)
        expected = snapshot.files[relative]
        if undo.exists() or undo.is_symlink():
            require_regular_file(undo, f"managed undo path {relative}")
            if path.exists() or path.is_symlink():
                require_regular_file(path, f"managed path {relative}")
                path.unlink()
                fsync_directory(path.parent)
                maybe_inject_fault(fault_injection, f"rollback-managed:remove-new:{relative}")
            ensure_target_private_parent(target, path, f"managed path {relative}", create=True)
            os.replace(undo, path)
            fsync_directory(path.parent)
            fsync_directory(undo.parent)
            maybe_inject_fault(fault_injection, f"rollback-managed:restore:{relative}")
        elif expected.digest is None and (path.exists() or path.is_symlink()):
            require_regular_file(path, f"managed path {relative}")
            path.unlink()
            fsync_directory(path.parent)
            maybe_inject_fault(fault_injection, f"rollback-managed:remove:{relative}")
    for relative in reversed(managed_parent_relatives()):
        if relative == ".":
            continue
        if relative in snapshot.directories:
            continue
        path = target if relative == "." else target / safe_relative_path(relative)
        if path.exists() or path.is_symlink():
            require_real_private_directory(path, f"managed parent {relative}")
            try:
                path.rmdir()
            except OSError as exc:
                fail(f"managed directory rollback postcondition expected empty: {relative}: {exc}")
            fsync_directory(path.parent)
            maybe_inject_fault(fault_injection, f"rollback-managed:remove-dir:{relative}")
    if not snapshot.target_existed and (target.exists() or target.is_symlink()):
        require_real_private_directory(target, "target")
        target.rmdir()
        fsync_directory(target.parent)
        maybe_inject_fault(fault_injection, "rollback-managed:remove-target")
    if snapshot.target_existed:
        restore_managed_directory_metadata(target, snapshot, fault_injection=fault_injection)
    cleanup_private_tree_required(
        transaction.stage_root,
        "managed stage root",
        fault_injection=fault_injection,
        fault_point="rollback-managed:remove-stage",
    )
    if snapshot.target_parent is not None:
        restore_private_directory_metadata(target.parent, snapshot.target_parent, "target parent")
        maybe_inject_fault(fault_injection, "rollback-managed:restore-target-parent")
    assert_managed_snapshot(target, snapshot)
    maybe_inject_fault(fault_injection, "rollback-managed:postcondition")


def rollback_managed_transaction(
    transaction: ManagedMutationTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        rollback_managed_transaction_once(transaction, fault_injection=fault_injection)
    except BaseException:
        rollback_managed_transaction_once(transaction)


def commit_managed_transaction(
    transaction: ManagedMutationTransaction,
    *,
    fault_injection: FaultInjector | None = None,
) -> bool:
    return finish_deferred_cleanup(
        transaction.target,
        [(transaction.stage_root, "managed:cleanup-stage")],
        fault_injection=fault_injection,
    )


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


def snapshot_backup_object_graph(root: Path) -> BackupObjectGraphSnapshot:
    if not root.exists() and not root.is_symlink():
        return BackupObjectGraphSnapshot(existed=False, rows=())
    require_real_private_directory(root, "backup root")
    rows: list[BackupObjectGraphRow] = []
    total = 0
    entries = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    for item in entries:
        relative = "." if item == root else item.relative_to(root).as_posix()
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"backup object graph must not contain symlinks: {relative}")
        if stat.S_ISDIR(info.st_mode):
            require_current_user_owner(info, f"backup object {relative}")
            if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
                fail(f"backup object graph directory must have mode 0700: {relative}")
            rows.append(
                (
                    relative,
                    "dir",
                    stat.S_IMODE(info.st_mode),
                    identity_of(info),
                    info.st_mtime_ns,
                    0,
                    None,
                )
            )
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"backup object graph entry must be a regular file: {relative}")
        content, final = read_regular_file(
            item,
            f"backup object {relative}",
            private=True,
            max_bytes=METADATA_MAX_BYTES,
        )
        total += len(content)
        if total > MAX_BACKUPS * METADATA_MAX_BYTES:
            fail("backup object graph exceeds size limit")
        rows.append(
            (
                relative,
                "file",
                stat.S_IMODE(final.st_mode),
                identity_of(final),
                final.st_mtime_ns,
                final.st_size,
                content,
            )
        )
    return BackupObjectGraphSnapshot(existed=True, rows=tuple(rows))


def assert_backup_object_graph(root: Path, expected: BackupObjectGraphSnapshot) -> None:
    actual = snapshot_backup_object_graph(root)
    if actual != expected:
        fail("backup object graph rollback postcondition mismatch")


def backup_graph_path(root: Path, relative: str) -> Path:
    return root if relative == "." else root / safe_relative_path(relative)


def restore_backup_object_graph_metadata(
    root: Path,
    snapshot: BackupObjectGraphSnapshot,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    if not snapshot.existed:
        return
    rows = sorted(snapshot.rows, key=lambda row: (row[0].count("/"), row[0]), reverse=True)
    file_rows = [row for row in rows if row[1] == "file"]
    directory_rows = [row for row in rows if row[1] == "dir"]
    for relative, _kind, mode, identity, mtime_ns, _size, _content in file_rows:
        path = backup_graph_path(root, relative)
        info = require_regular_file(path, f"backup object {relative}", private=True)
        if identity_of(info) != identity:
            raise ConcurrentTargetChange(
                f"backup object identity changed during rollback: {relative}"
            )
        os.chmod(path, mode)
        current = path.lstat()
        os.utime(path, ns=(current.st_atime_ns, mtime_ns))
        maybe_inject_fault(fault_injection, f"rollback-backup:restore-object:{relative}")
    for relative, _kind, mode, identity, mtime_ns, _size, _content in directory_rows:
        path = backup_graph_path(root, relative)
        info = require_real_private_directory(path, f"backup object {relative}")
        if identity_of(info) != identity:
            raise ConcurrentTargetChange(
                f"backup object identity changed during rollback: {relative}"
            )
        os.chmod(path, mode)
        current = path.lstat()
        os.utime(path, ns=(current.st_atime_ns, mtime_ns))
        maybe_inject_fault(fault_injection, f"rollback-backup:restore-object:{relative}")


def prepare_backup_transaction(
    target: Path,
    operation: str,
    *,
    fault_injection: FaultInjector | None = None,
) -> BackupTransaction:
    existing = backup_pool_payloads(target)
    snapshot = snapshot_known_files(target)
    root = backup_root(target)
    object_graph_before = snapshot_backup_object_graph(root)
    target_parent = snapshot_private_directory_metadata(target.parent, "target parent")
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
        cleanup_private_tree_required(staging, "backup staging root")
        if target_parent is not None:
            restore_private_directory_metadata(target.parent, target_parent, "target parent")
        raise
    return BackupTransaction(
        root=root,
        staging_root=staging,
        previous_root=previous,
        root_existed=object_graph_before.existed,
        payloads_before=existing,
        object_graph_before=object_graph_before,
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
    assert_backup_object_graph(transaction.root, transaction.object_graph_before)
    if transaction.object_graph_before.existed:
        if backup_pool_payloads(target) != transaction.payloads_before:
            fail("backup pool rollback postcondition mismatch")
    elif transaction.root.exists() or transaction.root.is_symlink():
        fail("backup pool rollback postcondition expected absence")
    if transaction.previous_root.exists() or transaction.previous_root.is_symlink():
        fail("backup previous root residue remains after rollback")
    if transaction.staging_root.exists() or transaction.staging_root.is_symlink():
        fail("backup staging root residue remains after rollback")


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
    restore_backup_object_graph_metadata(
        root,
        transaction.object_graph_before,
        fault_injection=fault_injection,
    )
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
        **cleanup_pending_state(target),
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
    cleanup = cleanup_pending_state(target)
    desired = desired_state_with_stamp(target, profile)
    changes = state_delta(target, desired)
    if cleanup["cleanup_pending"] and "cleanup-pending" not in changes:
        changes = ["cleanup-pending", *changes]
    return {
        "setup_id": CONTENT_SETUP_ID,
        "profile_id": profile.profile_id,
        "target": str(target),
        "mutates": False,
        "changed": bool(changes),
        "changes": changes,
        **cleanup,
    }


def lifecycle_noop_payload(
    target: Path, profile: Profile, *, operation: str
) -> dict[str, Any] | None:
    cleanup = cleanup_pending_state(target)
    if cleanup["cleanup_pending"]:
        return None
    desired = desired_state_with_stamp(target, profile)
    changes = state_delta(target, desired)
    if changes:
        return None
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


def current_update_profile(target: Path) -> Profile:
    stamp = load_stamp(target)
    if stamp is None:
        fail("update requires a current managed schema-2 target")
    if stamp.get("schema_version") != STAMP_SCHEMA:
        fail("update requires a current managed schema-2 target; use migrate for legacy state")
    drift = detect_drift(target, stamp)
    if drift:
        fail(f"update requires a clean managed target: {drift}")
    return render_profile(str(stamp["profile_id"]))


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
    cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
    drain_cleanup_pending(target, allow_pending=False)
    rollback_snapshot = snapshot_managed_state(target)
    if operation != "install" and not ensure_target_directory(target, create=False):
        fail(f"{operation} requires an existing target")
    existing = load_stamp(target)
    if operation == "install":
        preflight_unmanaged_target(target)
    elif operation == "migrate":
        if existing is None or existing.get("schema_version") != 1:
            fail("migrate requires a legacy managed schema-1 target")
    elif existing is None or existing.get("schema_version") != STAMP_SCHEMA:
        fail(f"{operation} requires a current managed schema-2 target")
    elif detect_drift(target, existing):
        fail(f"{operation} requires a clean managed target")
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
            "changed": cleanup_drained,
            "changes": ["cleanup-pending"] if cleanup_drained else [],
            "backup": None,
            "cleanup_pending": False,
        }
    expected = snapshot_known_files(target)
    backup_transaction: BackupTransaction | None = None
    managed_transaction: ManagedMutationTransaction | None = None
    try:
        backup_transaction = prepare_backup_transaction(
            target,
            operation,
            fault_injection=backup_fault_injection,
        )
        managed_transaction = prepare_managed_transaction(target, expected, rollback_snapshot)
        replace_managed_state(
            target,
            desired,
            expected,
            transaction=managed_transaction,
            fault_injection=fault_injection,
        )
        assert_desired_managed_state(target, desired)
        backup = commit_backup_transaction(
            target,
            backup_transaction,
            fault_injection=backup_fault_injection,
            rollback_fault_injection=backup_rollback_fault_injection,
        )
        cleanup_pending = finish_deferred_cleanup(
            target,
            [
                (
                    backup_transaction.previous_root,
                    "backup:cleanup-old-root",
                    backup_fault_injection,
                ),
                (managed_transaction.stage_root, "managed:cleanup-stage", fault_injection),
            ],
        )
    except (CleanupJournalPublishedInvalid, CleanupPreparePublishedPending):
        raise
    except BaseException:
        if backup_transaction is not None:
            cleanup_backup_transaction(
                target,
                backup_transaction,
                fault_injection=backup_rollback_fault_injection,
            )
        if managed_transaction is not None:
            rollback_managed_transaction(
                managed_transaction,
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
        "cleanup_pending": cleanup_pending,
    }


def migrate_target(target: Path, profile_id: str | None) -> dict[str, Any]:
    cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
    drain_cleanup_pending(target, allow_pending=False)
    stamp = load_stamp_any(target)
    if stamp is None:
        fail("migrate requires a legacy managed target")
    if stamp.get("schema_version") == STAMP_SCHEMA:
        return {
            "ok": True,
            "operation": "migrate",
            "already_current": True,
            "target": str(target),
            "changed": cleanup_drained,
            "changes": ["cleanup-pending"] if cleanup_drained else [],
            "cleanup_pending": False,
        }
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
    result = install_or_switch(target, profile, operation="migrate")
    if cleanup_drained:
        result["changed"] = True
        changes = list(result.get("changes", []))
        if "cleanup-pending" not in changes:
            result["changes"] = ["cleanup-pending", *changes]
    return result


def restore_target(
    target: Path,
    slot: int,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
    drain_cleanup_pending(target, allow_pending=False)
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
    if not changes:
        return {
            "ok": True,
            "operation": "restore",
            "target": str(target),
            "backup": slot,
            "changed": cleanup_drained,
            "changes": ["cleanup-pending"] if cleanup_drained else [],
            "cleanup_pending": False,
        }
    transaction = prepare_managed_transaction(target, snapshot.files, snapshot)
    try:
        replace_managed_state(
            target,
            desired,
            snapshot.files,
            transaction=transaction,
            fault_injection=fault_injection,
        )
        assert_desired_managed_state(target, desired)
        cleanup_pending = commit_managed_transaction(transaction, fault_injection=fault_injection)
    except (CleanupJournalPublishedInvalid, CleanupPreparePublishedPending):
        raise
    except BaseException:
        rollback_managed_transaction(transaction, fault_injection=rollback_fault_injection)
        raise
    return {
        "ok": True,
        "operation": "restore",
        "target": str(target),
        "backup": slot,
        "changed": bool(changes),
        "cleanup_pending": cleanup_pending,
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
    cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
    drain_cleanup_pending(target, allow_pending=False)
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
        prune_empty_managed_directories(target)
        return {
            "ok": True,
            "operation": "remove",
            "target": str(target),
            "removed": False,
            "changed": cleanup_drained,
            "changes": ["cleanup-pending"] if cleanup_drained else [],
            "backup": None,
            "cleanup_pending": False,
        }
    expected = snapshot_known_files(target)
    backup_transaction: BackupTransaction | None = None
    managed_transaction: ManagedMutationTransaction | None = None
    try:
        backup_transaction = prepare_backup_transaction(
            target,
            "remove",
            fault_injection=backup_fault_injection,
        )
        managed_transaction = prepare_managed_transaction(target, expected, rollback_snapshot)
        replace_managed_state(
            target,
            desired,
            expected,
            transaction=managed_transaction,
            fault_injection=fault_injection,
        )
        assert_desired_managed_state(target, desired)
        backup = commit_backup_transaction(
            target,
            backup_transaction,
            fault_injection=backup_fault_injection,
            rollback_fault_injection=backup_rollback_fault_injection,
        )
        cleanup_pending = finish_deferred_cleanup(
            target,
            [
                (
                    backup_transaction.previous_root,
                    "backup:cleanup-old-root",
                    backup_fault_injection,
                ),
                (managed_transaction.stage_root, "managed:cleanup-stage", fault_injection),
            ],
        )
        if not cleanup_pending:
            prune_empty_managed_directories(target)
    except (CleanupJournalPublishedInvalid, CleanupPreparePublishedPending):
        raise
    except BaseException:
        if backup_transaction is not None:
            cleanup_backup_transaction(
                target,
                backup_transaction,
                fault_injection=backup_rollback_fault_injection,
            )
        if managed_transaction is not None:
            rollback_managed_transaction(
                managed_transaction,
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
        "cleanup_pending": cleanup_pending,
    }


def software_root(target: Path) -> Path:
    return target / SOFTWARE_DIR_NAME


def software_current(target: Path) -> Path:
    return software_root(target) / SOFTWARE_CURRENT_NAME


def software_stamp_path(target: Path) -> Path:
    return target / SOFTWARE_STAMP_NAME


def software_entrypoint(target: Path) -> Path:
    return target / "bin" / OPENCODE_COMMAND


def software_directory_paths(target: Path) -> dict[str, Path]:
    return {
        ".": target,
        "bin": target / "bin",
        SOFTWARE_DIR_NAME: software_root(target),
        f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}": software_current(target),
        f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}/bin": software_current(target) / "bin",
    }


def tree_identity_signature(root: Path) -> tuple[TreeIdentityRow, ...]:
    require_directory(root, "software tree", private=True)
    rows: list[TreeIdentityRow] = []
    total = 0
    entries = [root, *sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())]
    for item in entries:
        info = item.lstat()
        relative = "." if item == root else item.relative_to(root).as_posix()
        mode = stat.S_IMODE(info.st_mode)
        identity = identity_of(info)
        if stat.S_ISLNK(info.st_mode):
            fail(f"software tree must not contain symlinks: {relative}")
        if stat.S_ISDIR(info.st_mode):
            require_current_user_owner(info, relative)
            rows.append((relative, "dir", mode, identity, info.st_mtime_ns, 0, None))
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"software tree entry must be a regular file: {relative}")
        if info.st_nlink != 1:
            fail(f"software tree entry must not be a hardlink: {relative}")
        content = read_regular_file(item, relative, max_bytes=SOFTWARE_MAX_BYTES)[0]
        total += len(content)
        if total > SOFTWARE_MAX_BYTES:
            fail("software tree is too large")
        rows.append(
            (
                relative,
                "file",
                mode,
                identity,
                info.st_mtime_ns,
                info.st_size,
                sha256_bytes(content),
            )
        )
    return tuple(rows)


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


def snapshot_software_directories(target: Path) -> dict[str, DirectorySnapshotEntry]:
    if not ensure_target_directory(target, create=False):
        return {}
    result: dict[str, DirectorySnapshotEntry] = {}
    for relative, path in software_directory_paths(target).items():
        try:
            info = path.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"software directory must be a real directory: {relative}")
        require_current_user_owner(info, f"software directory {relative}")
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"software directory must have mode 0700: {relative}")
        result[relative] = DirectorySnapshotEntry(
            mode=stat.S_IMODE(info.st_mode),
            identity=identity_of(info),
            atime_ns=info.st_atime_ns,
            mtime_ns=info.st_mtime_ns,
            size=info.st_size,
        )
    return result


def snapshot_binary_file(path: Path, label: str, *, max_bytes: int) -> BinaryFileSnapshot:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return BinaryFileSnapshot(
            content=None,
            mode=None,
            identity=None,
            mtime_ns=None,
            size=None,
        )
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    require_current_user_owner(info, label)
    mode = stat.S_IMODE(info.st_mode)
    content, final = read_regular_file(path, label, max_bytes=max_bytes)
    return BinaryFileSnapshot(
        content=content,
        mode=mode,
        identity=identity_of(final),
        mtime_ns=final.st_mtime_ns,
        size=final.st_size,
    )


def snapshot_software_state(target: Path) -> SoftwareStateSnapshot:
    preflight_software_paths(target)
    current_exists = software_current(target).exists() or software_current(target).is_symlink()
    return SoftwareStateSnapshot(
        target_existed=ensure_target_directory(target, create=False),
        target_parent=snapshot_private_directory_metadata(target.parent, "target parent"),
        software_root_existed=(
            software_root(target).exists() or software_root(target).is_symlink()
        ),
        current_existed=current_exists,
        current_tree_digest=tree_sha256(software_current(target)) if current_exists else None,
        current_tree_identity=(
            tree_identity_signature(software_current(target)) if current_exists else None
        ),
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
        directories=snapshot_software_directories(target),
    )


def remove_private_tree(path: Path, label: str) -> None:
    if path.exists() or path.is_symlink():
        require_real_private_directory(path, label)
        shutil.rmtree(path)
        fsync_directory(path.parent)
    if path.exists() or path.is_symlink():
        fail(f"{label} cleanup postcondition expected absence")


def cleanup_private_tree_required(
    path: Path,
    label: str,
    *,
    fault_injection: FaultInjector | None = None,
    fault_point: str | None = None,
) -> None:
    first_error: BaseException | None = None
    for _attempt in range(2):
        try:
            remove_private_tree(path, label)
            if fault_point is not None:
                maybe_inject_fault(fault_injection, fault_point)
            if path.exists() or path.is_symlink():
                fail(f"{label} cleanup postcondition expected absence")
            return
        except BaseException as exc:
            if first_error is None:
                first_error = exc
                continue
            raise
    if first_error is not None:
        raise first_error


def cleanup_parent(target: Path) -> Path:
    return target / CLEANUP_PARENT_NAME


def cleanup_journal_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_JOURNAL_NAME


def cleanup_prepare_path(target: Path) -> Path:
    return cleanup_parent(target) / CLEANUP_PREPARE_NAME


def cleanup_machine_source_category(target: Path, path: Path) -> str | None:
    parent_prefixes = {
        f".{target.name}.nddev-opencode-managed-stage.": "managed-stage",
        f".{target.name}.nddev-opencode-backups-stage.": "backup-stage",
        f".{target.name}.nddev-opencode-backups-previous.": "backup-previous",
    }
    for prefix, category in parent_prefixes.items():
        if path.parent == target.parent and path.name.startswith(prefix):
            return category
    target_prefixes = {
        ".nddev-opencode-software-stage.": "software-stage",
        ".nddev-opencode-software-undo.": "software-undo",
        ".nddev-opencode-software-remove-stage.": "software-remove-stage",
    }
    for prefix, category in target_prefixes.items():
        if path.parent == target and path.name.startswith(prefix):
            return category
    if path.parent == software_root(target) and path.name.startswith(".previous."):
        return "software-previous-current"
    return None


def cleanup_relative_name(index: int, category: str) -> str:
    if index < 0 or index >= MAX_CLEANUP_PATHS:
        fail("cleanup tombstone index is out of range")
    validate_id(category, "cleanup category")
    return f"{index}-{category}"


def cleanup_tombstone_path(target: Path, relative_name: str) -> Path:
    if not re.fullmatch(r"[0-7]-[a-z0-9]+(?:-[a-z0-9]+)*", relative_name):
        fail(f"cleanup tombstone name is invalid: {relative_name!r}")
    return cleanup_parent(target) / relative_name


def tree_total_size(path: Path) -> int:
    if not path.exists() and not path.is_symlink():
        return 0
    require_real_private_directory(path, "cleanup tombstone")
    total = 0
    for item in path.rglob("*"):
        info = item.lstat()
        if stat.S_ISLNK(info.st_mode):
            fail(f"cleanup tombstone must not contain symlinks: {item}")
        if stat.S_ISREG(info.st_mode):
            total += info.st_size
    return total


def cleanup_object_relative(root: Path, path: Path) -> str:
    if path == root:
        return "."
    relative = path.relative_to(root).as_posix()
    validate_cleanup_object_relative(relative)
    return relative


def validate_cleanup_object_relative(relative: str) -> None:
    if not isinstance(relative, str) or not relative:
        fail("cleanup object relative path must be a non-empty string")
    if relative == ".":
        return
    if relative.startswith("/") or relative.endswith("/") or "//" in relative:
        fail("cleanup object relative path is invalid")
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        fail("cleanup object relative path escapes the tombstone")
    if len(relative) > METADATA_MAX_BYTES // 4:
        fail("cleanup object relative path exceeds bounded length")


def cleanup_object_path(tombstone: Path, relative: str) -> Path:
    validate_cleanup_object_relative(relative)
    if relative == ".":
        return tombstone
    return tombstone.joinpath(*relative.split("/"))


def cleanup_object_record(root: Path, path: Path) -> dict[str, Any]:
    info = path.lstat()
    if stat.S_ISLNK(info.st_mode):
        fail(f"cleanup tombstone must not contain symlinks: {path}")
    require_current_user_owner(info, f"cleanup object {path}")
    mode = stat.S_IMODE(info.st_mode)
    common: dict[str, Any] = {
        "relative_path": cleanup_object_relative(root, path),
        "uid": info.st_uid,
        "mode": mode,
        "nlink": info.st_nlink,
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
    }
    if stat.S_ISDIR(info.st_mode):
        if mode != OWNER_DIR_MODE:
            fail(f"cleanup object directory must have mode 0700: {path}")
        return {**common, "kind": "directory", "sha256": None}
    if not stat.S_ISREG(info.st_mode):
        fail(f"cleanup object must be a directory or regular file: {path}")
    if info.st_nlink != 1:
        fail(f"cleanup object file must not be a hardlink: {path}")
    if mode not in {OWNER_FILE_MODE, OWNER_EXEC_MODE}:
        fail(f"cleanup object file must have owner-only mode: {path}")
    content = read_regular_file(path, str(path), max_bytes=SOFTWARE_MAX_BYTES)[0]
    return {**common, "kind": "file", "sha256": sha256_bytes(content)}


def cleanup_object_graph(root: Path) -> list[dict[str, Any]]:
    require_real_private_directory(root, "cleanup tombstone")
    objects = [cleanup_object_record(root, root)]
    for item in sorted(root.rglob("*"), key=lambda child: child.relative_to(root).as_posix()):
        if len(objects) >= MAX_CLEANUP_OBJECTS:
            fail("cleanup tombstone object graph exceeds bounded size")
        objects.append(cleanup_object_record(root, item))
    return objects


def validate_cleanup_object_shape(raw: Any) -> dict[str, Any]:
    keys = {
        "relative_path",
        "kind",
        "uid",
        "mode",
        "nlink",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "sha256",
    }
    if not isinstance(raw, dict) or set(raw) != keys:
        fail("cleanup journal object has invalid keys")
    validate_cleanup_object_relative(raw["relative_path"])
    if raw["kind"] not in {"directory", "file"}:
        fail("cleanup journal object kind is invalid")
    for key in ("uid", "mode", "nlink", "device", "inode", "size", "mtime_ns"):
        if not isinstance(raw[key], int) or raw[key] < 0:
            fail(f"cleanup journal object {key} must be a non-negative integer")
    digest = raw["sha256"]
    if raw["kind"] == "directory":
        if digest is not None:
            fail("cleanup journal directory object must not carry a digest")
    elif not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        fail("cleanup journal file object must carry a lowercase SHA-256 digest")
    return raw


def cleanup_entry_objects(entry: dict[str, Any]) -> dict[str, dict[str, Any]]:
    objects = entry.get("objects")
    if not isinstance(objects, list) or not objects or len(objects) > MAX_CLEANUP_OBJECTS:
        fail("cleanup journal object graph count is invalid")
    result: dict[str, dict[str, Any]] = {}
    for raw in objects:
        obj = validate_cleanup_object_shape(raw)
        relative = obj["relative_path"]
        if relative in result:
            fail("cleanup journal object graph has duplicate paths")
        result[relative] = obj
    if "." not in result or result["."]["kind"] != "directory":
        fail("cleanup journal object graph must include directory root")
    for relative in result:
        if relative == ".":
            continue
        parent = relative.rsplit("/", 1)[0] if "/" in relative else "."
        if parent not in result or result[parent]["kind"] != "directory":
            fail("cleanup journal object graph has missing parent")
    return result


def cleanup_entry_for_path(
    target: Path,
    source: Path,
    *,
    index: int,
) -> dict[str, Any]:
    if not source.is_absolute():
        fail("cleanup source must be absolute")
    category = cleanup_machine_source_category(target, source)
    if category is None:
        fail(f"cleanup source is outside declared machine cleanup namespaces: {source}")
    info = require_real_private_directory(source, f"cleanup source {source}")
    objects = cleanup_object_graph(source)
    return {
        "relative_name": cleanup_relative_name(index, category),
        "category": category,
        "kind": "directory",
        "uid": info.st_uid,
        "mode": stat.S_IMODE(info.st_mode),
        "nlink": info.st_nlink,
        "device": info.st_dev,
        "inode": info.st_ino,
        "size": info.st_size,
        "mtime_ns": info.st_mtime_ns,
        "tree_size": tree_total_size(source),
        "tree_sha256": tree_sha256(source),
        "objects": objects,
    }


def cleanup_source_relative_path(target: Path, source: Path) -> tuple[str, str]:
    roots = (
        ("software-root", software_root(target)),
        ("target", target),
        ("target-parent", target.parent),
    )
    for scope, root in roots:
        try:
            relative = source.relative_to(root).as_posix()
        except ValueError:
            continue
        validate_cleanup_object_relative(relative)
        if relative == "." or "/" in relative:
            fail("cleanup source relative path must be a bounded direct child")
        return scope, relative
    fail(f"cleanup source is outside declared source roots: {source}")


def cleanup_prepare_source_record(
    target: Path,
    source: Path,
    entry: dict[str, Any],
) -> dict[str, Any]:
    anchor, relative = cleanup_source_relative_path(target, source)
    return {
        "relative_name": entry["relative_name"],
        "source_kind": entry["category"],
        "anchor": anchor,
        "relative_path": relative,
    }


def cleanup_prepare_source_path(
    target: Path,
    record: dict[str, Any],
    entry: dict[str, Any],
) -> Path:
    keys = {"relative_name", "source_kind", "anchor", "relative_path"}
    if not isinstance(record, dict) or set(record) != keys:
        fail("cleanup prepare source record has invalid keys")
    if (
        record["relative_name"] != entry["relative_name"]
        or record["source_kind"] != entry["category"]
    ):
        fail("cleanup prepare source record does not match journal entry")
    anchor = record["anchor"]
    relative = record["relative_path"]
    if anchor not in {"target-parent", "target", "software-root"}:
        fail("cleanup prepare source anchor is invalid")
    if not isinstance(relative, str):
        fail("cleanup prepare source relative path must be a string")
    validate_cleanup_object_relative(relative)
    if relative == "." or "/" in relative:
        fail("cleanup prepare source relative path must be a bounded direct child")
    root = {
        "target-parent": target.parent,
        "target": target,
        "software-root": software_root(target),
    }[str(anchor)]
    path = root / relative
    if cleanup_machine_source_category(target, path) != entry["category"]:
        fail("cleanup prepare source category binding is invalid")
    return path


def validate_cleanup_entry_shape(entry: Any) -> dict[str, Any]:
    keys = {
        "relative_name",
        "category",
        "kind",
        "uid",
        "mode",
        "nlink",
        "device",
        "inode",
        "size",
        "mtime_ns",
        "tree_size",
        "tree_sha256",
        "objects",
    }
    if not isinstance(entry, dict) or set(entry) != keys:
        fail("cleanup journal entry has invalid keys")
    if not isinstance(entry["relative_name"], str):
        fail("cleanup tombstone name must be a string")
    cleanup_tombstone_path(Path("/tmp/placeholder"), entry["relative_name"])
    if entry["kind"] != "directory":
        fail("cleanup tombstone kind must be directory")
    validate_id(str(entry["category"]), "cleanup category")
    if str(entry["relative_name"]).split("-", 1)[1] != str(entry["category"]):
        fail("cleanup tombstone name/category mismatch")
    for key in ("uid", "mode", "nlink", "device", "inode", "size", "mtime_ns", "tree_size"):
        if not isinstance(entry[key], int) or entry[key] < 0:
            fail(f"cleanup journal {key} must be a non-negative integer")
    digest = entry["tree_sha256"]
    if not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest):
        fail("cleanup journal tree_sha256 must be a lowercase SHA-256 digest")
    objects = cleanup_entry_objects(entry)
    root_object = objects["."]
    for key in ("uid", "mode", "nlink", "device", "inode", "size", "mtime_ns"):
        if root_object[key] != entry[key]:
            fail("cleanup journal root object does not match entry identity")
    return entry


def cleanup_object_depth(relative: str) -> int:
    return 0 if relative == "." else relative.count("/") + 1


def cleanup_deletion_order(objects: dict[str, dict[str, Any]]) -> list[str]:
    return sorted(
        objects,
        key=lambda relative: (
            cleanup_object_depth(relative),
            1 if objects[relative]["kind"] == "directory" else 0,
            relative,
        ),
        reverse=True,
    )


def cleanup_object_ancestors(relative: str) -> set[str]:
    if relative == ".":
        return set()
    parts = relative.split("/")
    ancestors = {"."}
    for index in range(1, len(parts)):
        ancestors.add("/".join(parts[:index]))
    return ancestors


def cleanup_declared_child_names(
    objects: dict[str, dict[str, Any]], remaining: set[str]
) -> dict[str, set[str]]:
    result: dict[str, set[str]] = {
        relative: set()
        for relative, obj in objects.items()
        if obj["kind"] == "directory" and relative in remaining
    }
    for relative in remaining:
        if relative == ".":
            continue
        parent = relative.rsplit("/", 1)[0] if "/" in relative else "."
        name = relative.rsplit("/", 1)[-1]
        result.setdefault(parent, set()).add(name)
    return result


def validate_cleanup_object_present(
    tombstone: Path,
    obj: dict[str, Any],
    *,
    exact: bool,
) -> None:
    path = cleanup_object_path(tombstone, obj["relative_path"])
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"cleanup tombstone object is missing before deletion: {obj['relative_path']}")
    if stat.S_ISLNK(info.st_mode):
        fail("cleanup tombstone object must not be a symlink")
    if obj["kind"] == "directory":
        if not stat.S_ISDIR(info.st_mode):
            fail("cleanup tombstone object kind changed")
    elif not stat.S_ISREG(info.st_mode):
        fail("cleanup tombstone object kind changed")
    require_current_user_owner(info, f"cleanup tombstone object {obj['relative_path']}")
    if (
        info.st_uid != obj["uid"]
        or stat.S_IMODE(info.st_mode) != obj["mode"]
        or info.st_dev != obj["device"]
        or info.st_ino != obj["inode"]
    ):
        fail(f"cleanup tombstone object stable identity changed: {obj['relative_path']}")
    if exact:
        if (
            info.st_nlink != obj["nlink"]
            or info.st_size != obj["size"]
            or info.st_mtime_ns != obj["mtime_ns"]
        ):
            fail(f"cleanup tombstone object identity changed: {obj['relative_path']}")
        if obj["kind"] == "file":
            content = read_regular_file(path, str(path), max_bytes=SOFTWARE_MAX_BYTES)[0]
            if sha256_bytes(content) != obj["sha256"]:
                fail(f"cleanup tombstone object content changed: {obj['relative_path']}")


def validate_cleanup_tombstone_state(
    target: Path,
    entry: dict[str, Any],
    *,
    require_full: bool,
) -> tuple[Path, list[str], int]:
    tombstone = cleanup_tombstone_path(target, entry["relative_name"])
    objects = cleanup_entry_objects(entry)
    order = cleanup_deletion_order(objects)
    present: set[str] = set()
    for relative in objects:
        path = cleanup_object_path(tombstone, relative)
        if path.exists() or path.is_symlink():
            present.add(relative)
    if not present:
        return tombstone, order, len(order)
    if "." not in present:
        fail("cleanup tombstone partial state is missing its root")
    missing_prefix = 0
    for relative in order:
        if relative not in present:
            missing_prefix += 1
        else:
            break
    if require_full and missing_prefix != 0:
        fail("cleanup tombstone must be complete before the first destructive step")
    for relative in order[missing_prefix:]:
        if relative not in present:
            fail("cleanup tombstone partial drain is not resumable")
    remaining = set(order[missing_prefix:])
    mutated_ancestors: set[str] = set()
    for relative in order[:missing_prefix]:
        mutated_ancestors.update(cleanup_object_ancestors(relative))
    declared_children = cleanup_declared_child_names(objects, remaining)
    for relative in remaining:
        obj = objects[relative]
        exact = require_full or relative not in mutated_ancestors
        if obj["kind"] == "directory":
            validate_cleanup_object_present(tombstone, obj, exact=False)
            path = cleanup_object_path(tombstone, relative)
            actual_children = {child.name for child in path.iterdir()}
            expected_children = declared_children.get(relative, set())
            if actual_children != expected_children:
                fail(f"cleanup tombstone directory child set changed: {relative}")
            if exact:
                info = path.lstat()
                if (
                    info.st_nlink != obj["nlink"]
                    or info.st_size != obj["size"]
                    or info.st_mtime_ns != obj["mtime_ns"]
                ):
                    fail(f"cleanup tombstone object identity changed: {relative}")
        else:
            validate_cleanup_object_present(tombstone, obj, exact=exact)
    return tombstone, order, missing_prefix


def validate_cleanup_tombstone_identity(target: Path, entry: dict[str, Any]) -> Path:
    path, _order, _missing_prefix = validate_cleanup_tombstone_state(
        target, entry, require_full=False
    )
    return path


def validate_cleanup_tombstone_complete(target: Path, entry: dict[str, Any]) -> Path:
    path, _order, _missing_prefix = validate_cleanup_tombstone_state(
        target, entry, require_full=True
    )
    if not (path.exists() or path.is_symlink()):
        return path
    info = require_real_private_directory(path, f"cleanup tombstone {entry['relative_name']}")
    if (
        info.st_uid != entry["uid"]
        or stat.S_IMODE(info.st_mode) != entry["mode"]
        or info.st_nlink != entry["nlink"]
        or info.st_dev != entry["device"]
        or info.st_ino != entry["inode"]
        or info.st_size != entry["size"]
        or info.st_mtime_ns != entry["mtime_ns"]
    ):
        fail("cleanup tombstone identity mismatch")
    if tree_total_size(path) != entry["tree_size"] or tree_sha256(path) != entry["tree_sha256"]:
        fail("cleanup tombstone content mismatch")
    return path


def validate_cleanup_directory_matches_entry(
    path: Path,
    entry: dict[str, Any],
    label: str,
) -> None:
    info = require_real_private_directory(path, label)
    objects = cleanup_entry_objects(entry)
    actual = {"."}
    for item in path.rglob("*"):
        actual.add(cleanup_object_relative(path, item))
    if actual != set(objects):
        fail(f"{label} object graph does not match cleanup entry")
    root_object = objects["."]
    if (
        info.st_uid != root_object["uid"]
        or stat.S_IMODE(info.st_mode) != root_object["mode"]
        or info.st_nlink != root_object["nlink"]
        or info.st_dev != root_object["device"]
        or info.st_ino != root_object["inode"]
        or info.st_size != root_object["size"]
        or info.st_mtime_ns != root_object["mtime_ns"]
    ):
        fail(f"{label} identity does not match cleanup entry")
    if tree_total_size(path) != entry["tree_size"] or tree_sha256(path) != entry["tree_sha256"]:
        fail(f"{label} content does not match cleanup entry")
    for obj in objects.values():
        validate_cleanup_object_present(path, obj, exact=True)


def cleanup_journal_payload(target: Path, entries: list[dict[str, Any]]) -> dict[str, Any]:
    if not entries or len(entries) > MAX_CLEANUP_PATHS:
        fail("cleanup journal entry count is invalid")
    payload = {
        "schema_version": CLEANUP_JOURNAL_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": CLEANUP_PARENT_NAME,
        "max_entries": MAX_CLEANUP_PATHS,
        "serialized_max_bytes": CLEANUP_JOURNAL_MAX_BYTES,
        "phase": "ready",
        "entries": entries,
    }
    cleanup_journal_bytes(payload)
    return payload


def cleanup_journal_bytes(payload: dict[str, Any]) -> bytes:
    content = canonical_json(payload)
    if len(content) > CLEANUP_JOURNAL_MAX_BYTES:
        fail("cleanup journal exceeds serialized size limit")
    return content


def cleanup_prepare_payload(
    target: Path,
    journal_payload: dict[str, Any],
    sources: list[dict[str, Any]],
) -> dict[str, Any]:
    payload = {
        "schema_version": CLEANUP_PREPARE_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": str(target),
        "cleanup_parent": CLEANUP_PARENT_NAME,
        "prepare_file": CLEANUP_PREPARE_NAME,
        "journal_file": CLEANUP_JOURNAL_NAME,
        "journal_schema": CLEANUP_JOURNAL_SCHEMA,
        "journal_serialized_max_bytes": CLEANUP_JOURNAL_MAX_BYTES,
        "serialized_max_bytes": CLEANUP_PREPARE_MAX_BYTES,
        "phase": "preparing",
        "journal_payload": journal_payload,
        "sources": sources,
    }
    cleanup_prepare_bytes(payload)
    return payload


def cleanup_prepare_bytes(payload: dict[str, Any]) -> bytes:
    content = canonical_json(payload)
    if len(content) > CLEANUP_PREPARE_MAX_BYTES:
        fail("cleanup prepare intent exceeds serialized size limit")
    return content


def validate_cleanup_journal_payload_shape(
    target: Path,
    payload: dict[str, Any],
) -> list[dict[str, Any]]:
    keys = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "max_entries",
        "serialized_max_bytes",
        "phase",
        "entries",
    }
    if set(payload) != keys:
        fail("cleanup journal has invalid keys")
    if (
        payload["schema_version"] != CLEANUP_JOURNAL_SCHEMA
        or payload["product_name"] != PRODUCT_NAME
        or payload["canonical_target"] != str(target)
        or payload["cleanup_parent"] != CLEANUP_PARENT_NAME
        or payload["max_entries"] != MAX_CLEANUP_PATHS
        or payload["serialized_max_bytes"] != CLEANUP_JOURNAL_MAX_BYTES
        or payload["phase"] != "ready"
    ):
        fail("cleanup journal identity or binding is invalid")
    entries = payload["entries"]
    if not isinstance(entries, list) or not entries or len(entries) > MAX_CLEANUP_PATHS:
        fail("cleanup journal entry count is invalid")
    seen: set[str] = set()
    result: list[dict[str, Any]] = []
    for index, raw_entry in enumerate(entries):
        entry = validate_cleanup_entry_shape(raw_entry)
        if entry["relative_name"] != cleanup_relative_name(index, str(entry["category"])):
            fail("cleanup journal tombstone order mismatch")
        if entry["relative_name"] in seen:
            fail("cleanup journal has duplicate tombstone names")
        seen.add(entry["relative_name"])
        result.append(entry)
    return result


def validate_cleanup_prepare_payload(
    target: Path,
    payload: dict[str, Any],
    *,
    expected_journal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    keys = {
        "schema_version",
        "product_name",
        "build_version",
        "canonical_target",
        "cleanup_parent",
        "prepare_file",
        "journal_file",
        "journal_schema",
        "journal_serialized_max_bytes",
        "serialized_max_bytes",
        "phase",
        "journal_payload",
        "sources",
    }
    if set(payload) != keys:
        fail("cleanup prepare intent has invalid keys")
    if (
        payload["schema_version"] != CLEANUP_PREPARE_SCHEMA
        or payload["product_name"] != PRODUCT_NAME
        or payload["canonical_target"] != str(target)
        or payload["cleanup_parent"] != CLEANUP_PARENT_NAME
        or payload["prepare_file"] != CLEANUP_PREPARE_NAME
        or payload["journal_file"] != CLEANUP_JOURNAL_NAME
        or payload["journal_schema"] != CLEANUP_JOURNAL_SCHEMA
        or payload["journal_serialized_max_bytes"] != CLEANUP_JOURNAL_MAX_BYTES
        or payload["serialized_max_bytes"] != CLEANUP_PREPARE_MAX_BYTES
        or payload["phase"] != "preparing"
    ):
        fail("cleanup prepare intent identity or binding is invalid")
    journal_payload = payload["journal_payload"]
    if not isinstance(journal_payload, dict):
        fail("cleanup prepare intent journal payload must be an object")
    cleanup_journal_bytes(journal_payload)
    if expected_journal is not None and cleanup_journal_bytes(
        journal_payload
    ) != cleanup_journal_bytes(expected_journal):
        fail("cleanup prepare intent does not match cleanup journal")
    entries = validate_cleanup_journal_payload_shape(target, journal_payload)
    sources = payload["sources"]
    if not isinstance(sources, list) or len(sources) != len(entries):
        fail("cleanup prepare intent source count is invalid")
    source_paths: list[Path] = []
    for raw_source, entry in zip(sources, entries):
        source_paths.append(cleanup_prepare_source_path(target, raw_source, entry))
    return journal_payload, source_paths


def read_cleanup_prepare_payload(
    target: Path,
    prepare: Path,
    *,
    expected_journal: dict[str, Any] | None = None,
) -> tuple[dict[str, Any], list[Path]]:
    content, _after = read_cleanup_prepare_payload_bytes(prepare)
    payload = parse_json_object(content, CLEANUP_PREPARE_NAME)
    cleanup_prepare_bytes(payload)
    return validate_cleanup_prepare_payload(
        target,
        payload,
        expected_journal=expected_journal,
    )


def require_cleanup_prepare_file(final: Path) -> os.stat_result:
    try:
        info = final.lstat()
    except FileNotFoundError:
        fail("cleanup prepare intent is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("cleanup prepare intent must be a regular non-symlink file")
    require_current_user_owner(info, CLEANUP_PREPARE_NAME)
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        fail("cleanup prepare intent must have mode 0600")
    if info.st_nlink not in {1, 2}:
        fail("cleanup prepare intent must not have unexpected hard-link aliases")
    if info.st_size > CLEANUP_PREPARE_MAX_BYTES:
        fail("cleanup prepare intent exceeds serialized size limit")
    return info


def read_cleanup_prepare_payload_bytes(
    prepare: Path,
    *,
    allow_hardlink_aliases: bool = False,
) -> tuple[bytes, os.stat_result]:
    return read_regular_file(
        prepare,
        CLEANUP_PREPARE_NAME,
        private=True,
        max_bytes=CLEANUP_PREPARE_MAX_BYTES,
        allow_hardlink_aliases=allow_hardlink_aliases,
    )


def require_cleanup_prepare_publication_alias(
    final: Path,
    temporary: Path,
    identity: tuple[int, int],
) -> None:
    if temporary.parent != final.parent or not temporary.name.startswith(
        f".{CLEANUP_PREPARE_NAME}."
    ):
        fail("cleanup prepare temporary alias is outside bounded publication namespace")
    suffix = temporary.name[len(f".{CLEANUP_PREPARE_NAME}.") :]
    if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(suffix):
        fail("cleanup prepare temporary alias name is not machine-owned")
    try:
        info = temporary.lstat()
    except FileNotFoundError:
        fail("cleanup prepare temporary alias is missing before validation")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("cleanup prepare temporary alias is not a regular file")
    require_current_user_owner(info, "cleanup prepare temporary alias")
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE or identity_of(info) != identity:
        fail("cleanup prepare temporary alias identity mismatch")


def validate_cleanup_prepare_publication(
    target: Path,
    final: Path,
    temporary: Path | None = None,
) -> None:
    before = require_cleanup_prepare_file(final)
    if temporary is not None:
        if before.st_nlink != 2:
            fail("cleanup prepare publication alias expected a two-link final")
        require_cleanup_prepare_publication_alias(final, temporary, identity_of(before))
    elif before.st_nlink != 1:
        fail("cleanup prepare recovery requires exclusive target coordination")
    content, _after = read_cleanup_prepare_payload_bytes(
        final,
        allow_hardlink_aliases=temporary is not None,
    )
    payload = parse_json_object(content, CLEANUP_PREPARE_NAME)
    cleanup_prepare_bytes(payload)
    validate_cleanup_prepare_payload(target, payload)


def recover_cleanup_prepare_temp_alias(final: Path, identity: tuple[int, int]) -> None:
    parent = final.parent
    prefix = f".{CLEANUP_PREPARE_NAME}."
    try:
        children = list(parent.iterdir())
    except OSError as exc:
        fail(f"cleanup prepare alias scan failed: {exc}")
    if len(children) > ANCHOR_PARENT_SCAN_LIMIT:
        fail("cleanup prepare parent scan exceeded bounded limit")
    matches: list[Path] = []
    for child in children:
        if child == final or not child.name.startswith(prefix):
            continue
        suffix = child.name[len(prefix) :]
        if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(suffix):
            fail(f"cleanup prepare temporary alias name is not machine-owned: {child.name}")
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail("cleanup prepare temporary alias is not a regular file")
        require_current_user_owner(info, "cleanup prepare temporary alias")
        if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE or identity_of(info) != identity:
            fail("cleanup prepare temporary alias identity mismatch")
        matches.append(child)
    if len(matches) != 1:
        fail("cleanup prepare intent has unexpected hard-link aliases")
    matches[0].unlink()
    fsync_directory(parent)


def cleanup_unpublished_prepare_or_journal_temps(parent: Path) -> bool:
    removed = False
    prefixes = (
        f".{CLEANUP_PREPARE_NAME}.",
        f".{CLEANUP_JOURNAL_NAME}.",
    )
    for child in list(parent.iterdir()):
        prefix = next((value for value in prefixes if child.name.startswith(value)), None)
        if prefix is None:
            continue
        suffix = child.name[len(prefix) :]
        if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(suffix):
            fail(f"cleanup temporary state is not machine-owned: {child.name}")
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail("cleanup temporary state must be a regular file")
        require_current_user_owner(info, "cleanup temporary state")
        if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE or info.st_nlink != 1:
            fail("cleanup temporary state identity is invalid")
        child.unlink()
        removed = True
        fsync_directory(parent)
    return removed


def publish_cleanup_prepare(target: Path, payload: dict[str, Any]) -> None:
    parent = cleanup_parent(target)
    final = cleanup_prepare_path(target)
    if final.exists() or final.is_symlink():
        fail("cleanup prepare intent already exists")
    content = cleanup_prepare_bytes(payload)
    fd = -1
    temporary: Path | None = None
    final_visible = False
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{CLEANUP_PREPARE_NAME}.", dir=str(parent))
        temporary = Path(temporary_name)
        os.set_inheritable(fd, False)
        os.fchmod(fd, OWNER_FILE_MODE)
        written = 0
        while written < len(content):
            count = os.write(fd, content[written:])
            if count <= 0:
                fail("cleanup prepare write made no progress")
            written += count
        fsync_file_descriptor(fd)
        try:
            os.close(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
            fail(f"cleanup prepare close failed before publication: {exc}")
        fd = -1
        os.link(temporary, final)
        final_visible = True
        validate_cleanup_prepare_publication(target, final, temporary)
        try:
            temporary.unlink()
            fsync_directory(parent)
            validate_cleanup_prepare_publication(target, final)
        except BaseException as exc:
            try:
                final_info = require_cleanup_prepare_file(final)
                validate_cleanup_prepare_publication(
                    target,
                    final,
                    temporary if temporary.exists() or temporary.is_symlink() else None,
                )
                if final_info.st_nlink == 2 and not (temporary.exists() or temporary.is_symlink()):
                    fail("cleanup prepare intent has an unknown hard-link alias")
            except BaseException as validation_exc:
                fail(
                    "cleanup prepare final validation failed after publication cleanup fault: "
                    f"{validation_exc}"
                )
            raise CleanupPreparePublishedPending(
                f"cleanup prepare intent is pending after final-path publication: {exc}"
            ) from exc
    except BaseException:
        if (
            not final_visible
            and temporary is not None
            and (temporary.exists() or temporary.is_symlink())
        ):
            temporary.unlink()
            fsync_directory(parent)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def publish_cleanup_journal(
    target: Path,
    payload: dict[str, Any],
    *,
    prepare_intent: Path | None = None,
) -> None:
    parent = cleanup_parent(target)
    ensure_target_private_directory(
        target,
        CLEANUP_PARENT_NAME,
        "cleanup pending parent",
        create=True,
    )
    final = cleanup_journal_path(target)
    if final.exists() or final.is_symlink():
        fail("cleanup journal already exists")
    content = cleanup_journal_bytes(payload)
    fd = -1
    temporary: Path | None = None
    final_visible = False
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{CLEANUP_JOURNAL_NAME}.", dir=str(parent))
        temporary = Path(temporary_name)
        os.set_inheritable(fd, False)
        os.fchmod(fd, OWNER_FILE_MODE)
        written = 0
        while written < len(content):
            count = os.write(fd, content[written:])
            if count <= 0:
                fail("cleanup journal write made no progress")
            written += count
        fsync_file_descriptor(fd)
        try:
            os.close(fd)
        except OSError as exc:
            try:
                os.close(fd)
            except OSError:
                pass
            fd = -1
            fail(f"cleanup journal close failed before publication: {exc}")
        fd = -1
        os.link(temporary, final)
        final_visible = True
        try:
            final_payload = read_cleanup_journal_payload_bytes(
                final,
                allow_hardlink_aliases=True,
            )
            validate_cleanup_journal_payload(
                target,
                final_payload,
                publication_alias=temporary,
                prepare_intent=prepare_intent,
            )
        except BaseException as exc:
            raise CleanupJournalPublishedInvalid(
                f"cleanup journal final validation failed after final-path publication: {exc}"
            ) from exc
        try:
            temporary.unlink()
            if prepare_intent is not None and (
                prepare_intent.exists() or prepare_intent.is_symlink()
            ):
                require_regular_file(prepare_intent, CLEANUP_PREPARE_NAME, private=True)
                prepare_intent.unlink()
            fsync_directory(parent)
            final_payload = read_cleanup_journal_payload(target, final)
            validate_cleanup_journal_payload(target, final_payload)
        except BaseException as exc:
            try:
                final_info = require_cleanup_journal_file(final)
                final_payload = read_cleanup_journal_payload_bytes(
                    final,
                    allow_hardlink_aliases=True,
                )
                validate_cleanup_journal_payload(
                    target,
                    final_payload,
                    publication_alias=temporary
                    if temporary.exists() or temporary.is_symlink()
                    else None,
                    prepare_intent=prepare_intent
                    if prepare_intent is not None
                    and (prepare_intent.exists() or prepare_intent.is_symlink())
                    else None,
                )
                if final_info.st_nlink == 2 and not (temporary.exists() or temporary.is_symlink()):
                    fail("cleanup journal has an unknown hard-link alias after publication")
            except BaseException as validation_exc:
                raise CleanupJournalPublishedInvalid(
                    "cleanup journal final validation failed after publication cleanup fault: "
                    f"{validation_exc}"
                ) from validation_exc
            raise CleanupJournalPublishedPending(
                f"cleanup journal is pending after final-path publication: {exc}"
            ) from exc
    except BaseException:
        if (
            not final_visible
            and temporary is not None
            and (temporary.exists() or temporary.is_symlink())
        ):
            temporary.unlink()
            fsync_directory(parent)
        raise
    finally:
        if fd >= 0:
            os.close(fd)


def recover_cleanup_journal_temp_alias(final: Path, identity: tuple[int, int]) -> None:
    parent = final.parent
    prefix = f".{CLEANUP_JOURNAL_NAME}."
    try:
        children = list(parent.iterdir())
    except OSError as exc:
        fail(f"cleanup journal alias scan failed: {exc}")
    if len(children) > ANCHOR_PARENT_SCAN_LIMIT:
        fail("cleanup journal parent scan exceeded bounded limit")
    matches: list[Path] = []
    for child in children:
        if child == final or not child.name.startswith(prefix):
            continue
        suffix = child.name[len(prefix) :]
        if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(suffix):
            fail(f"cleanup journal temporary alias name is not machine-owned: {child.name}")
        info = child.lstat()
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail("cleanup journal temporary alias is not a regular file")
        require_current_user_owner(info, "cleanup journal temporary alias")
        if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE or identity_of(info) != identity:
            fail("cleanup journal temporary alias identity mismatch")
        matches.append(child)
    if len(matches) != 1:
        fail("cleanup journal has unexpected hard-link aliases")
    matches[0].unlink()
    fsync_directory(parent)


def require_cleanup_journal_file(final: Path) -> os.stat_result:
    try:
        info = final.lstat()
    except FileNotFoundError:
        fail("cleanup journal is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("cleanup journal must be a regular non-symlink file")
    require_current_user_owner(info, CLEANUP_JOURNAL_NAME)
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        fail("cleanup journal must have mode 0600")
    if info.st_nlink not in {1, 2}:
        fail("cleanup journal must not have unexpected hard-link aliases")
    if info.st_size > CLEANUP_JOURNAL_MAX_BYTES:
        fail("cleanup journal exceeds serialized size limit")
    return info


def read_cleanup_journal_payload(target: Path, final: Path) -> dict[str, Any]:
    _ = target
    before = require_cleanup_journal_file(final)
    if before.st_nlink != 1:
        fail("cleanup journal recovery requires exclusive target coordination")
    return read_cleanup_journal_payload_bytes(final)


def read_cleanup_journal_payload_bytes(
    final: Path,
    *,
    allow_hardlink_aliases: bool = False,
) -> dict[str, Any]:
    content, _after = read_regular_file(
        final,
        CLEANUP_JOURNAL_NAME,
        private=True,
        max_bytes=CLEANUP_JOURNAL_MAX_BYTES,
        allow_hardlink_aliases=allow_hardlink_aliases,
    )
    payload = parse_json_object(content, CLEANUP_JOURNAL_NAME)
    cleanup_journal_bytes(payload)
    return payload


def require_cleanup_journal_publication_alias(
    final: Path,
    temporary: Path,
    identity: tuple[int, int],
) -> None:
    if temporary.parent != final.parent or not temporary.name.startswith(
        f".{CLEANUP_JOURNAL_NAME}."
    ):
        fail("cleanup journal temporary alias is outside bounded publication namespace")
    suffix = temporary.name[len(f".{CLEANUP_JOURNAL_NAME}.") :]
    if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(suffix):
        fail("cleanup journal temporary alias name is not machine-owned")
    try:
        info = temporary.lstat()
    except FileNotFoundError:
        fail("cleanup journal temporary alias is missing before validation")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail("cleanup journal temporary alias is not a regular file")
    require_current_user_owner(info, "cleanup journal temporary alias")
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE or identity_of(info) != identity:
        fail("cleanup journal temporary alias identity mismatch")


def validate_cleanup_journal_payload(
    target: Path,
    payload: dict[str, Any],
    *,
    publication_alias: Path | None = None,
    prepare_intent: Path | None = None,
) -> dict[str, Any]:
    parent = cleanup_parent(target)
    if publication_alias is not None:
        before = require_cleanup_journal_file(cleanup_journal_path(target))
        if before.st_nlink != 2:
            fail("cleanup journal publication alias expected a two-link final")
        require_cleanup_journal_publication_alias(
            cleanup_journal_path(target),
            publication_alias,
            identity_of(before),
        )
    entries = validate_cleanup_journal_payload_shape(target, payload)
    if prepare_intent is not None:
        read_cleanup_prepare_payload(target, prepare_intent, expected_journal=payload)
    seen: set[str] = set()
    for entry in entries:
        seen.add(entry["relative_name"])
        validate_cleanup_tombstone_identity(target, entry)
    allowed = {CLEANUP_JOURNAL_NAME, *seen}
    if publication_alias is not None:
        allowed.add(publication_alias.name)
    if prepare_intent is not None:
        allowed.add(CLEANUP_PREPARE_NAME)
    for child in parent.iterdir():
        if publication_alias is not None and child == publication_alias:
            continue
        if child.name.startswith(f".{CLEANUP_JOURNAL_NAME}."):
            fail("cleanup pending parent contains unpublished journal state")
        if child.name not in allowed:
            fail(f"cleanup pending parent contains unknown state: {child.name}")
    return payload


def recover_cleanup_preparation(target: Path) -> None:
    parent = cleanup_parent(target)
    require_real_private_directory(parent, "cleanup pending parent")
    final = cleanup_journal_path(target)
    if final.exists() or final.is_symlink():
        fail("cleanup preparation recovery requires absent final journal")
    prepare = cleanup_prepare_path(target)
    before = require_cleanup_prepare_file(prepare)
    if before.st_nlink == 2:
        recover_cleanup_prepare_temp_alias(prepare, identity_of(before))
    elif before.st_nlink != 1:
        fail("cleanup prepare intent must not have unexpected hard-link aliases")
    journal_payload, sources = read_cleanup_prepare_payload(target, prepare)
    entries = validate_cleanup_journal_payload_shape(target, journal_payload)
    allowed = {CLEANUP_PREPARE_NAME, *(entry["relative_name"] for entry in entries)}
    cleanup_unpublished_prepare_or_journal_temps(parent)
    for child in parent.iterdir():
        if child.name.startswith(f".{CLEANUP_JOURNAL_NAME}."):
            fail("cleanup pending parent contains unpublished journal state")
        if child.name not in allowed:
            fail(f"cleanup pending parent contains unknown state: {child.name}")
    for entry, source in zip(entries, sources):
        tombstone = cleanup_tombstone_path(target, entry["relative_name"])
        source_present = source.exists() or source.is_symlink()
        tombstone_present = tombstone.exists() or tombstone.is_symlink()
        if source_present and tombstone_present:
            fail("cleanup preparation recovery found both source and tombstone")
        if not source_present and not tombstone_present:
            fail("cleanup preparation recovery found neither source nor tombstone")
        if source_present:
            validate_cleanup_directory_matches_entry(
                source,
                entry,
                f"cleanup prepared source {source}",
            )
            os.replace(source, tombstone)
            fsync_directory(source.parent)
            fsync_directory(parent)
        else:
            validate_cleanup_directory_matches_entry(
                tombstone,
                entry,
                f"cleanup prepared tombstone {tombstone}",
            )
    try:
        publish_cleanup_journal(target, journal_payload, prepare_intent=prepare)
    except CleanupJournalPublishedPending:
        return


def load_cleanup_journal(target: Path, *, recover_aliases: bool = False) -> dict[str, Any] | None:
    parent = cleanup_parent(target)
    if not parent.exists() and not parent.is_symlink():
        return None
    require_real_private_directory(parent, "cleanup pending parent")
    final = cleanup_journal_path(target)
    prepare = cleanup_prepare_path(target)
    if not final.exists() and not final.is_symlink():
        if prepare.exists() or prepare.is_symlink():
            if not recover_aliases:
                fail("cleanup preparation recovery requires exclusive target coordination")
            recover_cleanup_preparation(target)
        else:
            if recover_aliases:
                cleanup_unpublished_prepare_or_journal_temps(parent)
                if not any(parent.iterdir()):
                    parent.rmdir()
                    fsync_directory(target)
                    return None
            elif not any(parent.iterdir()):
                fail("cleanup pending parent contains incomplete cleanup state")
            if any(parent.iterdir()):
                fail("cleanup pending parent contains unjournaled state")
            return None
    if not final.exists() and not final.is_symlink():
        if any(parent.iterdir()):
            fail("cleanup pending parent contains unjournaled state")
        return None
    if recover_aliases:
        before = require_cleanup_journal_file(final)
        if before.st_nlink == 2:
            recover_cleanup_journal_temp_alias(final, identity_of(before))
        elif before.st_nlink != 1:
            fail("cleanup journal must not have unexpected hard-link aliases")
        if prepare.exists() or prepare.is_symlink():
            prepare_info = require_cleanup_prepare_file(prepare)
            if prepare_info.st_nlink == 2:
                recover_cleanup_prepare_temp_alias(prepare, identity_of(prepare_info))
            elif prepare_info.st_nlink != 1:
                fail("cleanup prepare intent must not have unexpected hard-link aliases")
    payload = read_cleanup_journal_payload(target, final)
    return validate_cleanup_journal_payload(
        target,
        payload,
        prepare_intent=prepare if prepare.exists() or prepare.is_symlink() else None,
    )


def cleanup_pending_state(target: Path) -> dict[str, Any]:
    payload = load_cleanup_journal(target, recover_aliases=False)
    if payload is None:
        return {"cleanup_pending": False, "cleanup_pending_entries": []}
    return {
        "cleanup_pending": True,
        "cleanup_pending_entries": [
            {
                "relative_name": entry["relative_name"],
                "category": entry["category"],
                "kind": entry["kind"],
                "tree_size": entry["tree_size"],
            }
            for entry in payload["entries"]
        ],
    }


def promote_cleanup_tombstones(
    target: Path,
    sources: list[Path],
) -> CleanupPromotionResult | None:
    existing_sources = [source for source in sources if source.exists() or source.is_symlink()]
    if not existing_sources:
        return None
    if load_cleanup_journal(target, recover_aliases=True) is not None:
        fail("cleanup pending state must be drained before new cleanup work")
    directory_snapshots: dict[Path, tuple[str, DirectorySnapshotEntry | None]] = {}

    def remember_directory(path: Path, label: str) -> None:
        if path not in directory_snapshots:
            directory_snapshots[path] = (
                label,
                snapshot_private_directory_metadata(path, label),
            )

    remember_directory(target, "cleanup target")
    remember_directory(target.parent, "cleanup target parent")
    parent = cleanup_parent(target)
    parent_existed = parent.exists() or parent.is_symlink()
    if parent_existed:
        remember_directory(parent, "cleanup pending parent")
    for source in existing_sources:
        remember_directory(source.parent, f"cleanup source parent {source.parent}")
    entries: list[dict[str, Any]] = []
    source_records: list[dict[str, Any]] = []
    planned_moves: list[tuple[Path, Path]] = []
    for index, source in enumerate(existing_sources):
        entry = cleanup_entry_for_path(target, source, index=index)
        tombstone = cleanup_tombstone_path(target, entry["relative_name"])
        if tombstone.exists() or tombstone.is_symlink():
            fail(f"cleanup tombstone already exists: {entry['relative_name']}")
        entries.append(entry)
        source_records.append(cleanup_prepare_source_record(target, source, entry))
        planned_moves.append((source, tombstone))
    payload = cleanup_journal_payload(target, entries)
    ensure_target_private_directory(
        target,
        CLEANUP_PARENT_NAME,
        "cleanup pending parent",
        create=True,
    )
    if any(parent.iterdir()):
        fail("cleanup pending parent contains unjournaled state")
    moved: list[tuple[Path, Path]] = []
    prepare = cleanup_prepare_path(target)
    try:
        try:
            publish_cleanup_prepare(
                target,
                cleanup_prepare_payload(target, payload, source_records),
            )
        except CleanupPreparePublishedPending:
            try:
                recover_cleanup_preparation(target)
            except CleanupJournalPublishedInvalid:
                raise
            except BaseException as exc:
                raise CleanupPreparePublishedPending(
                    f"cleanup prepare recovery remains pending: {exc}"
                ) from exc
            return CleanupPromotionResult(payload=payload, publication_pending=False)
        for source, tombstone in planned_moves:
            os.replace(source, tombstone)
            moved.append((tombstone, source))
            fsync_directory(source.parent)
            fsync_directory(parent)
        try:
            publish_cleanup_journal(target, payload, prepare_intent=prepare)
        except CleanupJournalPublishedPending:
            return CleanupPromotionResult(payload=payload, publication_pending=True)
        return CleanupPromotionResult(payload=payload, publication_pending=False)
    except CleanupJournalPublishedInvalid:
        raise
    except CleanupPreparePublishedPending:
        raise
    except BaseException:
        for tombstone, source in reversed(moved):
            if tombstone.exists() or tombstone.is_symlink():
                if source.exists() or source.is_symlink():
                    fail("cleanup promotion rollback found occupied source path")
                os.replace(tombstone, source)
                fsync_directory(tombstone.parent)
                fsync_directory(source.parent)
        cleanup_unpublished_prepare_or_journal_temps(parent)
        if prepare.exists() or prepare.is_symlink():
            require_regular_file(prepare, CLEANUP_PREPARE_NAME, private=True)
            prepare.unlink()
            fsync_directory(parent)
        if not any(parent.iterdir()):
            if parent_existed:
                label, snapshot = directory_snapshots[parent]
                if snapshot is not None:
                    restore_private_directory_metadata(parent, snapshot, label)
                    assert_private_directory_metadata(parent, snapshot, label)
            else:
                parent.rmdir()
                fsync_directory(target)
        for path, (label, snapshot) in sorted(
            directory_snapshots.items(),
            key=lambda item: len(item[0].parts),
            reverse=True,
        ):
            if snapshot is None:
                if path.exists() or path.is_symlink():
                    fail(f"{label} rollback postcondition expected absence")
                continue
            if not (path.exists() or path.is_symlink()):
                fail(f"{label} rollback postcondition expected presence")
            restore_private_directory_metadata(path, snapshot, label)
            assert_private_directory_metadata(path, snapshot, label)
        raise


def drain_cleanup_pending(
    target: Path,
    *,
    allow_pending: bool,
    fault_points: dict[str, tuple[str, FaultInjector | None]] | None = None,
) -> bool:
    payload = load_cleanup_journal(target, recover_aliases=True)
    if payload is None:
        return False
    pending = False
    for entry in payload["entries"]:
        try:
            tombstone, order, missing_prefix = validate_cleanup_tombstone_state(
                target, entry, require_full=False
            )
            if missing_prefix >= len(order):
                continue
            if missing_prefix == 0:
                tombstone, order, _missing_prefix = validate_cleanup_tombstone_state(
                    target, entry, require_full=True
                )
            objects = cleanup_entry_objects(entry)
            for index, relative in enumerate(order):
                if index < missing_prefix:
                    continue
                tombstone, _order, observed_prefix = validate_cleanup_tombstone_state(
                    target, entry, require_full=False
                )
                if observed_prefix > index:
                    continue
                if observed_prefix != index:
                    fail("cleanup tombstone partial drain cursor is inconsistent")
                obj = objects[relative]
                path = cleanup_object_path(tombstone, relative)
                if obj["kind"] == "directory":
                    path.rmdir()
                else:
                    path.unlink()
                fsync_directory(path.parent)
            fault = (fault_points or {}).get(entry["relative_name"])
            if fault is not None:
                point, injector = fault
                maybe_inject_fault(injector, point)
        except BaseException:
            pending = True
            break
    if pending:
        if allow_pending:
            return True
        fail("cleanup pending state could not be drained")
    journal = cleanup_journal_path(target)
    prepare = cleanup_prepare_path(target)
    try:
        if prepare.exists() or prepare.is_symlink():
            read_cleanup_prepare_payload(target, prepare, expected_journal=payload)
            prepare.unlink()
            fsync_directory(cleanup_parent(target))
        require_regular_file(journal, CLEANUP_JOURNAL_NAME, private=True)
        journal.unlink()
        fsync_directory(cleanup_parent(target))
        cleanup_parent(target).rmdir()
        fsync_directory(target)
    except BaseException:
        if allow_pending:
            return True
        fail("cleanup pending state could not be cleared")
    return False


def finish_deferred_cleanup(
    target: Path,
    items: list[tuple[Path, str] | tuple[Path, str, FaultInjector | None]],
    *,
    fault_injection: FaultInjector | None = None,
) -> bool:
    category_faults: dict[str, tuple[str, FaultInjector | None]] = {}
    normalized: list[tuple[Path, str, FaultInjector | None]] = []
    for item in items:
        path, point = item[0], item[1]
        injector = item[2] if len(item) > 2 else fault_injection
        normalized.append((path, point, injector))
        if path.exists() or path.is_symlink():
            category = cleanup_machine_source_category(target, path)
            if category is not None:
                category_faults[category] = (point, injector)
    try:
        promotion = promote_cleanup_tombstones(
            target, [path for path, _point, _injector in normalized]
        )
    except CleanupJournalPublishedInvalid:
        raise
    except BaseException:
        if load_cleanup_journal(target, recover_aliases=True) is not None:
            return True
        raise
    if promotion is None:
        return False
    if promotion.publication_pending:
        return True
    fault_points = {
        str(entry["relative_name"]): category_faults[str(entry["category"])]
        for entry in promotion.payload["entries"]
    }
    return drain_cleanup_pending(
        target,
        allow_pending=True,
        fault_points=fault_points,
    )


def move_software_original_to_undo(
    path: Path,
    undo_path: Path,
    label: str,
    *,
    executable: bool = False,
    private: bool = False,
    fault_injection: FaultInjector | None = None,
) -> bool:
    info = stat_optional(path, label)
    if info is None:
        return False
    require_regular_file(path, label, executable=executable, private=private)
    if undo_path.exists() or undo_path.is_symlink():
        fail(f"software undo path already exists: {label}")
    require_real_private_directory(undo_path.parent, "OpenCode software undo")
    os.replace(path, undo_path)
    fsync_directory(path.parent)
    fsync_directory(undo_path.parent)
    maybe_inject_fault(fault_injection, f"software:move-original:{label}")
    return True


def binary_snapshot_matches(
    path: Path,
    snapshot: BinaryFileSnapshot,
    label: str,
    *,
    max_bytes: int,
) -> bool:
    if snapshot.content is None:
        return not (path.exists() or path.is_symlink())
    try:
        info = require_regular_file(path, label)
    except ManagerError:
        return False
    if (
        snapshot.identity is None
        or identity_of(info) != snapshot.identity
        or stat.S_IMODE(info.st_mode) != snapshot.mode
        or info.st_mtime_ns != snapshot.mtime_ns
        or info.st_size != snapshot.size
    ):
        return False
    return read_regular_file(path, label, max_bytes=max_bytes)[0] == snapshot.content


def restore_binary_file(
    path: Path,
    snapshot: BinaryFileSnapshot,
    label: str,
    *,
    original_path: Path | None = None,
    max_bytes: int,
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
    if binary_snapshot_matches(path, snapshot, label, max_bytes=max_bytes):
        return
    if original_path is not None and (original_path.exists() or original_path.is_symlink()):
        require_regular_file(original_path, f"original {label}")
        if path.exists() or path.is_symlink():
            require_regular_file(path, label)
            path.unlink()
            fsync_directory(path.parent)
            maybe_inject_fault(fault_injection, f"rollback-software:remove-new:{label}")
        make_parent_directories(path)
        os.replace(original_path, path)
        fsync_directory(path.parent)
        fsync_directory(original_path.parent)
        maybe_inject_fault(fault_injection, f"rollback-software:restore:{label}")
        if not binary_snapshot_matches(path, snapshot, label, max_bytes=max_bytes):
            fail(f"{label} rollback postcondition identity mismatch")
        return
    if snapshot.identity is not None:
        fail(f"{label} rollback requires the original file object")
    make_parent_directories(path)
    atomic_write(
        path,
        snapshot.content,
        mode=snapshot.mode or OWNER_FILE_MODE,
        fault_injection=fault_injection,
        fault_label=f"rollback-software:{label}",
    )


def restore_software_directory_metadata(
    target: Path,
    snapshot: SoftwareStateSnapshot,
    *,
    fault_injection: FaultInjector | None = None,
) -> None:
    if snapshot.target_existed:
        paths = software_directory_paths(target)
        for relative in sorted(
            snapshot.directories, key=lambda value: value.count("/"), reverse=True
        ):
            expected = snapshot.directories[relative]
            path = paths[relative]
            restore_private_directory_metadata(path, expected, f"software directory {relative}")
            maybe_inject_fault(fault_injection, f"rollback-software:restore-dir:{relative}")
    if snapshot.target_parent is not None:
        restore_private_directory_metadata(target.parent, snapshot.target_parent, "target parent")
        maybe_inject_fault(fault_injection, "rollback-software:restore-target-parent")


def restore_software_state(
    target: Path,
    snapshot: SoftwareStateSnapshot,
    *,
    previous_current: Path | None,
    original_entrypoint: Path | None = None,
    original_stamp: Path | None = None,
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
        original_path=original_entrypoint,
        max_bytes=SOFTWARE_MAX_BYTES,
        fault_injection=fault_injection,
    )
    restore_binary_file(
        software_stamp_path(target),
        snapshot.stamp,
        SOFTWARE_STAMP_NAME,
        original_path=original_stamp,
        max_bytes=METADATA_MAX_BYTES,
        fault_injection=fault_injection,
    )
    if not snapshot.bin_dir_existed:
        if (target / "bin").exists() or (target / "bin").is_symlink():
            require_real_private_directory(target / "bin", "software entrypoint parent")
            (target / "bin").rmdir()
            fsync_directory(target)
            maybe_inject_fault(fault_injection, "rollback-software:remove-bin-dir")
    if not snapshot.software_root_existed:
        if software_root(target).exists() or software_root(target).is_symlink():
            require_real_private_directory(software_root(target), SOFTWARE_DIR_NAME)
            software_root(target).rmdir()
            fsync_directory(target)
            maybe_inject_fault(fault_injection, "rollback-software:remove-software-root")
    if not snapshot.target_existed:
        if target.exists() or target.is_symlink():
            require_real_private_directory(target, "target")
            target.rmdir()
            fsync_directory(target.parent)
            maybe_inject_fault(fault_injection, "rollback-software:remove-target")
    restore_software_directory_metadata(target, snapshot, fault_injection=fault_injection)
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
    if snapshot.identity is not None and identity_of(info) != snapshot.identity:
        fail(f"{label} rollback postcondition identity mismatch")
    if stat.S_IMODE(info.st_mode) != snapshot.mode:
        fail(f"{label} rollback postcondition mode mismatch")
    if info.st_mtime_ns != snapshot.mtime_ns or info.st_size != snapshot.size:
        fail(f"{label} rollback postcondition stat mismatch")
    if read_regular_file(path, label, max_bytes=max_bytes)[0] != snapshot.content:
        fail(f"{label} rollback postcondition content mismatch")


def assert_software_snapshot(target: Path, snapshot: SoftwareStateSnapshot) -> None:
    if not snapshot.target_existed:
        if target.exists() or target.is_symlink():
            fail("software target rollback postcondition expected absence")
        if snapshot.target_parent is not None:
            assert_private_directory_metadata(
                target.parent, snapshot.target_parent, "target parent"
            )
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
    if (
        snapshot.current_tree_identity is not None
        and tree_identity_signature(software_current(target)) != snapshot.current_tree_identity
    ):
        fail("software current tree rollback postcondition identity mismatch")
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
    paths = software_directory_paths(target)
    for relative, expected in snapshot.directories.items():
        info = require_real_private_directory(paths[relative], f"software directory {relative}")
        if identity_of(info) != expected.identity:
            fail(f"software directory rollback postcondition identity mismatch: {relative}")
        if stat.S_IMODE(info.st_mode) != expected.mode:
            fail(f"software directory rollback postcondition mode mismatch: {relative}")
        if info.st_mtime_ns != expected.mtime_ns:
            fail(f"software directory rollback postcondition mtime mismatch: {relative}")
    if snapshot.target_parent is not None:
        assert_private_directory_metadata(target.parent, snapshot.target_parent, "target parent")


def rollback_software_state(
    target: Path,
    snapshot: SoftwareStateSnapshot,
    *,
    previous_current: Path | None,
    original_entrypoint: Path | None = None,
    original_stamp: Path | None = None,
    fault_injection: FaultInjector | None = None,
) -> None:
    try:
        restore_software_state(
            target,
            snapshot,
            previous_current=previous_current,
            original_entrypoint=original_entrypoint,
            original_stamp=original_stamp,
            fault_injection=fault_injection,
        )
    except BaseException:
        restore_software_state(
            target,
            snapshot,
            previous_current=previous_current,
            original_entrypoint=original_entrypoint,
            original_stamp=original_stamp,
        )


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
        # The probe binary runs under an isolated HOME it may write to
        # (e.g. ~/.cache). Force an owner-only umask so anything it creates
        # is already mode 0700/0600 and survives the cleanup-object mode
        # journaling that follows, regardless of the caller's ambient umask.
        preexec_fn=lambda: os.umask(0o077),
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
            "api": OPENCODE_RELEASE_API,
        },
        "artifact": {
            "platform": artifact_key,
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
        **cleanup_pending_state(target),
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
        for key in ("name", "size", "sha256", "url", "format"):
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
    restore_software_directory_metadata(
        target, transaction.snapshot, fault_injection=fault_injection
    )
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
    artifact_resolver: Callable[[str], dict[str, Any]] | None = None,
    artifact_downloader: Callable[[str, Path, int], None] = download_artifact,
    version_probe: Callable[[Path, Path], str] = run_version_probe,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    host = host_detector()
    key = str(host["artifact_platform"])
    cleanup_drained = False
    if ensure_target_directory(target, create=False):
        cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
        drain_cleanup_pending(target, allow_pending=False)
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
                "changed": cleanup_drained,
                "cleanup_pending": False,
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
    artifact = (artifact_resolver or ARTIFACTS.__getitem__)(key)
    root = software_root(target)
    stage_parent = target / f".nddev-opencode-software-stage.{os.getpid()}.{time.time_ns()}"
    undo_parent = target / f".nddev-opencode-software-undo.{os.getpid()}.{time.time_ns()}"
    stage_current = stage_parent / SOFTWARE_CURRENT_NAME
    archive = stage_parent / artifact["name"]
    previous_current = root / f".previous.{os.getpid()}.{time.time_ns()}"
    previous_current_moved = False
    original_entrypoint = undo_parent / "entrypoint"
    original_stamp = undo_parent / "software-stamp"
    original_entrypoint_moved = False
    original_stamp_moved = False
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
        ensure_target_private_directory(
            target,
            undo_parent.relative_to(target).as_posix(),
            "OpenCode software undo",
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
        original_entrypoint_moved = move_software_original_to_undo(
            software_entrypoint(target),
            original_entrypoint,
            "OpenCode entrypoint",
            executable=True,
            fault_injection=fault_injection,
        )
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
        original_stamp_moved = move_software_original_to_undo(
            software_stamp_path(target),
            original_stamp,
            SOFTWARE_STAMP_NAME,
            private=True,
            fault_injection=fault_injection,
        )
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
        cleanup_private_tree_required(stage_parent, "OpenCode stage")
        if (
            not snapshot.target_existed
            and not original_entrypoint_moved
            and not original_stamp_moved
        ):
            cleanup_private_tree_required(undo_parent, "OpenCode software undo")
        rollback_software_state(
            target,
            snapshot,
            previous_current=previous_current if previous_current_moved else None,
            original_entrypoint=original_entrypoint if original_entrypoint_moved else None,
            original_stamp=original_stamp if original_stamp_moved else None,
            fault_injection=rollback_fault_injection,
        )
        cleanup_private_tree_required(undo_parent, "OpenCode software undo")
        restore_software_directory_metadata(target, snapshot)
        assert_software_snapshot(target, snapshot)
        raise
    cleanup_pending = finish_deferred_cleanup(
        target,
        [
            (stage_parent, "software:cleanup-stage"),
            (previous_current, "software:cleanup-previous-current"),
            (undo_parent, "software:cleanup-undo"),
        ],
        fault_injection=fault_injection,
    )
    return {
        "ok": True,
        "operation": "update-cli" if update else "install-cli",
        "product_host": host["product_host"],
        "artifact": key,
        "target": str(target),
        "changed": True,
        "cleanup_pending": cleanup_pending,
    }


def remove_cli(
    target: Path,
    *,
    fault_injection: FaultInjector | None = None,
    rollback_fault_injection: FaultInjector | None = None,
) -> dict[str, Any]:
    if not ensure_target_directory(target, create=False):
        return {"ok": True, "operation": "remove-cli", "target": str(target), "removed": False}
    cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
    drain_cleanup_pending(target, allow_pending=False)
    preflight_software_paths(target)
    if not software_presence(target):
        return {
            "ok": True,
            "operation": "remove-cli",
            "target": str(target),
            "removed": False,
            "changed": cleanup_drained,
            "cleanup_pending": False,
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
    cleanup_pending = finish_deferred_cleanup(
        target,
        [(stage_root, "remove-cli:cleanup-stage")],
        fault_injection=fault_injection,
    )
    return {
        "ok": True,
        "operation": "remove-cli",
        "target": str(target),
        "removed": True,
        "changed": True,
        "cleanup_pending": cleanup_pending,
    }


def system_lock_root() -> Path:
    base = (
        Path("/private/tmp")
        if sys.platform == "darwin" and Path("/private/tmp").is_dir()
        else Path("/tmp")
    )
    uid = current_uid()
    return base / f"nddev-opencode-locks-{uid if uid is not None else 'nouid'}"


def coordination_lock_path() -> Path:
    return global_lock_path()


def global_lock_path() -> Path:
    return system_lock_root() / "global.lock"


def external_lock_path_for_digest(digest: str) -> Path:
    return system_lock_root() / f"{digest}.lock"


def lock_open_flags() -> int:
    flags = os.O_RDWR
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    return flags


def cleanup_unreturned_created_lock(
    path: Path,
    *,
    created_identity: tuple[int, int] | None,
    parent_existed: bool,
    parent_snapshot: DirectorySnapshotEntry | None,
    container_snapshot: DirectorySnapshotEntry | None,
) -> None:
    handle = LockHandle(
        fd=-1,
        path=path,
        parent_existed=parent_existed,
        file_existed=False,
        parent_snapshot=parent_snapshot,
        container_snapshot=container_snapshot,
        file_identity=created_identity,
    )
    cleanup_created_lock(handle)


def open_lock_file(
    path: Path,
    *,
    create: bool,
    lock_operation: int | None,
) -> LockHandle | None:
    parent_existed = path.parent.exists() or path.parent.is_symlink()
    file_existed = path.exists() or path.is_symlink()
    if not create and not parent_existed:
        return None
    parent_snapshot = (
        snapshot_private_directory_metadata(path.parent, f"lock parent {path.parent}")
        if parent_existed
        else None
    )
    container_snapshot = None
    if not parent_existed and (path.parent.parent.exists() or path.parent.parent.is_symlink()):
        try:
            container_snapshot = snapshot_private_directory_metadata(
                path.parent.parent,
                f"lock parent container {path.parent.parent}",
            )
        except ManagerError:
            container_snapshot = None
    ensure_real_private_directory(path.parent, f"lock parent {path.parent}", create=create)
    parent_before = require_real_private_directory(path.parent, f"lock parent {path.parent}")
    flags = lock_open_flags()
    fd: int | None = None
    created_file = False
    file_identity: tuple[int, int] | None = None
    try:
        try:
            if create:
                fd = os.open(path, flags | os.O_CREAT | os.O_EXCL, OWNER_FILE_MODE)
                created_file = True
                file_identity = identity_of(os.fstat(fd))
                os.fchmod(fd, OWNER_FILE_MODE)
            else:
                fd = os.open(path, flags)
        except FileExistsError:
            try:
                fd = os.open(path, flags)
            except OSError as exc:
                fail(f"cannot open lock file safely: {path}: {exc}")
        except FileNotFoundError:
            if not create:
                return None
            raise
        except OSError as exc:
            fail(f"cannot open lock file safely: {path}: {exc}")
        os.set_inheritable(fd, False)
        opened = os.fstat(fd)
        if file_identity is None:
            file_identity = identity_of(opened)
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
        if lock_operation is not None:
            fcntl.flock(fd, lock_operation | fcntl.LOCK_NB)
    except BlockingIOError:
        if fd is not None:
            os.close(fd)
            fd = None
        if created_file:
            try:
                cleanup_unreturned_created_lock(
                    path,
                    created_identity=file_identity,
                    parent_existed=parent_existed,
                    parent_snapshot=parent_snapshot,
                    container_snapshot=container_snapshot,
                )
            except BaseException as cleanup_exc:
                fail(f"cannot clean failed lock creation safely: {path}: {cleanup_exc}")
        fail(f"target is already locked: {path}")
    except BaseException as exc:
        if fd is not None:
            os.close(fd)
            fd = None
        if created_file:
            try:
                cleanup_unreturned_created_lock(
                    path,
                    created_identity=file_identity,
                    parent_existed=parent_existed,
                    parent_snapshot=parent_snapshot,
                    container_snapshot=container_snapshot,
                )
            except BaseException as cleanup_exc:
                fail(f"cannot clean failed lock creation safely: {path}: {cleanup_exc}")
        if isinstance(exc, ManagerError):
            raise
        fail(f"cannot open lock file safely: {path}: {exc}")
    assert fd is not None
    return LockHandle(
        fd=fd,
        path=path,
        parent_existed=parent_existed,
        file_existed=file_existed,
        parent_snapshot=parent_snapshot,
        container_snapshot=container_snapshot,
        file_identity=file_identity,
    )


def lock_file(path: Path) -> LockHandle:
    handle = open_lock_file(path, create=True, lock_operation=fcntl.LOCK_EX)
    assert handle is not None
    return handle


def lock_existing_file(path: Path, *, shared: bool) -> LockHandle | None:
    return open_lock_file(
        path,
        create=False,
        lock_operation=fcntl.LOCK_SH if shared else fcntl.LOCK_EX,
    )


def anchor_marker(*, anchor: str, target_digest: str | None = None) -> bytes:
    payload: dict[str, Any] = {
        "schema": 1,
        "product": PRODUCT_NAME,
        "anchor": anchor,
    }
    if target_digest is not None:
        payload["target_digest"] = target_digest
    return canonical_json(payload)


def expected_anchor_payload(*, anchor: str, target_digest: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "schema": 1,
        "product": PRODUCT_NAME,
        "anchor": anchor,
    }
    if target_digest is not None:
        payload["target_digest"] = target_digest
    return payload


def read_fd_bytes(fd: int, label: str) -> bytes:
    try:
        os.lseek(fd, 0, os.SEEK_SET)
        chunks: list[bytes] = []
        remaining = METADATA_MAX_BYTES + 1
        while remaining > 0:
            chunk = os.read(fd, min(remaining, 8192))
            if not chunk:
                break
            chunks.append(chunk)
            remaining -= len(chunk)
    except OSError as exc:
        fail(f"{label} cannot be read safely: {exc}")
    content = b"".join(chunks)
    if not content:
        fail(f"{label} is empty")
    if len(content) > METADATA_MAX_BYTES:
        fail(f"{label} exceeds metadata size limit")
    return content


def recover_anchor_temp_aliases(path: Path, identity: tuple[int, int]) -> None:
    parent = path.parent
    prefix = f".{path.name}."
    expected_random_start = len(prefix)
    try:
        children = list(parent.iterdir())
    except OSError as exc:
        fail(f"anchor temporary alias scan failed for {path}: {exc}")
    if len(children) > ANCHOR_PARENT_SCAN_LIMIT:
        fail(f"anchor parent scan exceeded bounded limit for {path}")
    matching_aliases: list[Path] = []
    for child in children:
        if child == path or not child.name.startswith(prefix):
            continue
        random_suffix = child.name[expected_random_start:]
        if not ANCHOR_TEMP_RANDOM_PATTERN.fullmatch(random_suffix):
            fail(f"anchor temporary alias name is not machine-owned: {child}")
        try:
            info = child.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
            fail(f"anchor temporary alias is not a regular file: {child}")
        require_current_user_owner(info, f"anchor temporary alias {child}")
        if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
            fail(f"anchor temporary alias must have mode 0600: {child}")
        if identity_of(info) != identity:
            fail(f"anchor temporary alias identity mismatch: {child}")
        matching_aliases.append(child)
    if len(matching_aliases) != 1:
        fail(f"anchor file has unexpected hard-link aliases: {path}")
    try:
        matching_aliases[0].unlink()
    except OSError as exc:
        fail(f"anchor temporary alias cleanup failed for {matching_aliases[0]}: {exc}")
    fsync_directory(parent)


def validate_anchor_handle(
    fd: int,
    path: Path,
    *,
    anchor: str,
    target_digest: str | None = None,
    recover_aliases: bool = True,
) -> tuple[int, int]:
    try:
        opened = os.fstat(fd)
        on_disk = path.lstat()
    except OSError as exc:
        fail(f"anchor binding cannot be validated: {path}: {exc}")
    if identity_of(opened) != identity_of(on_disk):
        raise ConcurrentTargetChange(f"anchor file identity changed while opening: {path}")
    if stat.S_ISLNK(opened.st_mode) or not stat.S_ISREG(opened.st_mode):
        fail(f"anchor file must be a regular non-symlink file: {path}")
    require_current_user_owner(opened, f"anchor file {path}")
    if stat.S_IMODE(opened.st_mode) != OWNER_FILE_MODE:
        fail(f"anchor file must have mode 0600: {path}")
    if opened.st_nlink == 2:
        if not recover_aliases:
            fail(f"anchor hard-link alias recovery requires exclusive coordination: {path}")
        recover_anchor_temp_aliases(path, identity_of(opened))
        try:
            opened = os.fstat(fd)
            on_disk = path.lstat()
        except OSError as exc:
            fail(f"anchor binding cannot be revalidated after recovery: {path}: {exc}")
        if identity_of(opened) != identity_of(on_disk):
            raise ConcurrentTargetChange(f"anchor file identity changed during recovery: {path}")
        if opened.st_nlink != 1:
            fail(f"anchor file must not have hard-link aliases: {path}")
    elif opened.st_nlink != 1:
        fail(f"anchor file must not have hard-link aliases: {path}")
    try:
        payload = json.loads(read_fd_bytes(fd, f"anchor file {path}").decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"anchor file is malformed: {path}: {exc}")
    expected = expected_anchor_payload(anchor=anchor, target_digest=target_digest)
    if payload != expected:
        fail(f"anchor file binding mismatch: {path}")
    return identity_of(opened)


def open_existing_anchor(
    path: Path,
    *,
    anchor: str,
    target_digest: str | None = None,
    shared: bool,
    recover_aliases: bool,
) -> LockHandle | None:
    parent_existed = path.parent.exists() or path.parent.is_symlink()
    if not parent_existed:
        return None
    parent_snapshot = snapshot_private_directory_metadata(path.parent, f"lock parent {path.parent}")
    flags = lock_open_flags()

    def open_locked(operation: int, *, recover_aliases: bool) -> tuple[int, tuple[int, int]]:
        try:
            descriptor = os.open(path, flags)
        except FileNotFoundError:
            return -1, (-1, -1)
        except OSError as exc:
            fail(f"cannot open anchor file safely: {path}: {exc}")
        try:
            os.set_inheritable(descriptor, False)
            fcntl.flock(descriptor, operation)
            identity = validate_anchor_handle(
                descriptor,
                path,
                anchor=anchor,
                target_digest=target_digest,
                recover_aliases=recover_aliases,
            )
        except BaseException:
            os.close(descriptor)
            raise
        return descriptor, identity

    try:
        fd, file_identity = open_locked(
            fcntl.LOCK_SH if shared and not recover_aliases else fcntl.LOCK_EX,
            recover_aliases=recover_aliases,
        )
        if fd < 0:
            return None
        if shared and recover_aliases:
            release_errors: list[BaseException] = []
            try:
                fcntl.flock(fd, fcntl.LOCK_UN)
            except OSError as exc:
                release_errors.append(exc)
            try:
                os.close(fd)
            except OSError as exc:
                release_errors.append(exc)
            if release_errors:
                fail(f"cannot release exclusive anchor recovery lock safely: {release_errors[0]}")
            fd, file_identity = open_locked(fcntl.LOCK_SH, recover_aliases=False)
            if fd < 0:
                raise ConcurrentTargetChange(f"anchor disappeared before shared reopen: {path}")
    except BaseException as exc:
        if isinstance(exc, ManagerError):
            raise
        fail(f"cannot open anchor file safely: {path}: {exc}")
    return LockHandle(
        fd=fd,
        path=path,
        parent_existed=True,
        file_existed=True,
        parent_snapshot=parent_snapshot,
        container_snapshot=None,
        file_identity=file_identity,
    )


def cleanup_unpublished_anchor(
    *,
    temporary: Path | None,
    parent: Path,
    parent_existed: bool,
    parent_snapshot: DirectorySnapshotEntry | None,
    container_snapshot: DirectorySnapshotEntry | None,
) -> None:
    cleanup_errors: list[BaseException] = []
    if temporary is not None and (temporary.exists() or temporary.is_symlink()):
        try:
            require_regular_file(temporary, f"unpublished anchor {temporary}", private=True)
            temporary.unlink()
            fsync_directory(parent)
        except BaseException as exc:
            cleanup_errors.append(exc)
    if parent_snapshot is not None:
        try:
            restore_private_directory_metadata(
                parent,
                parent_snapshot,
                f"lock parent {parent}",
            )
            assert_private_directory_metadata(
                parent,
                parent_snapshot,
                f"lock parent {parent}",
            )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if not parent_existed and (parent.exists() or parent.is_symlink()):
        try:
            require_real_private_directory(parent, f"lock parent {parent}")
            parent.rmdir()
            fsync_directory(parent.parent)
            if container_snapshot is not None:
                restore_private_directory_metadata(
                    parent.parent,
                    container_snapshot,
                    f"lock parent container {parent.parent}",
                )
                assert_private_directory_metadata(
                    parent.parent,
                    container_snapshot,
                    f"lock parent container {parent.parent}",
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        raise cleanup_errors[0]


def publish_anchor(
    path: Path,
    *,
    anchor: str,
    target_digest: str | None = None,
    shared: bool = False,
) -> LockHandle:
    parent_existed = path.parent.exists() or path.parent.is_symlink()
    parent_snapshot = (
        snapshot_private_directory_metadata(path.parent, f"lock parent {path.parent}")
        if parent_existed
        else None
    )
    container_snapshot = None
    if not parent_existed and (path.parent.parent.exists() or path.parent.parent.is_symlink()):
        try:
            container_snapshot = snapshot_private_directory_metadata(
                path.parent.parent,
                f"lock parent container {path.parent.parent}",
            )
        except ManagerError:
            container_snapshot = None
    ensure_real_private_directory(path.parent, f"lock parent {path.parent}", create=True)
    marker = anchor_marker(anchor=anchor, target_digest=target_digest)
    fd = -1
    temporary: Path | None = None
    linked_final = False
    durable = False
    try:
        fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
        temporary = Path(temporary_name)
        os.set_inheritable(fd, False)
        os.fchmod(fd, OWNER_FILE_MODE)
        written = 0
        while written < len(marker):
            written += os.write(fd, marker[written:])
        fsync_file_descriptor(fd)
        fcntl.flock(fd, fcntl.LOCK_EX)
        try:
            os.link(temporary, path)
            linked_final = True
        except FileExistsError:
            os.close(fd)
            fd = -1
            assert temporary is not None
            temporary.unlink()
            fsync_directory(path.parent)
            existing = open_existing_anchor(
                path,
                anchor=anchor,
                target_digest=target_digest,
                shared=shared,
                recover_aliases=True,
            )
            if existing is None:
                raise ConcurrentTargetChange(
                    f"anchor disappeared after existing publication: {path}"
                )
            return existing
        assert temporary is not None
        temporary.unlink()
        fsync_directory(path.parent)
        durable = True
        file_identity = validate_anchor_handle(
            fd,
            path,
            anchor=anchor,
            target_digest=target_digest,
        )
        if shared:
            fcntl.flock(fd, fcntl.LOCK_SH)
        return LockHandle(
            fd=fd,
            path=path,
            parent_existed=True,
            file_existed=True,
            parent_snapshot=snapshot_private_directory_metadata(
                path.parent,
                f"lock parent {path.parent}",
            ),
            container_snapshot=None,
            file_identity=file_identity,
        )
    except BaseException as exc:
        if fd >= 0:
            try:
                os.close(fd)
            except OSError:
                pass
        if not durable:
            try:
                cleanup_unpublished_anchor(
                    temporary=temporary,
                    parent=path.parent,
                    parent_existed=parent_existed,
                    parent_snapshot=None if linked_final else parent_snapshot,
                    container_snapshot=None if linked_final else container_snapshot,
                )
            except BaseException as cleanup_exc:
                fail(f"cannot clean unpublished anchor safely: {path}: {cleanup_exc}")
        if isinstance(exc, ManagerError):
            raise
        fail(f"cannot publish anchor safely: {path}: {exc}")


def acquire_anchor(
    path: Path,
    *,
    anchor: str,
    target_digest: str | None = None,
    shared: bool = False,
    create: bool,
) -> LockHandle | None:
    if create:
        existing = open_existing_anchor(
            path,
            anchor=anchor,
            target_digest=target_digest,
            shared=shared,
            recover_aliases=True,
        )
        if existing is not None:
            return existing
        return publish_anchor(
            path,
            anchor=anchor,
            target_digest=target_digest,
            shared=shared,
        )
    return open_existing_anchor(
        path,
        anchor=anchor,
        target_digest=target_digest,
        shared=shared,
        recover_aliases=False,
    )


def release_lock_handle(handle: LockHandle) -> list[BaseException]:
    errors: list[BaseException] = []
    try:
        fcntl.flock(handle.fd, fcntl.LOCK_UN)
    except OSError as exc:
        errors.append(exc)
    try:
        os.close(handle.fd)
    except OSError as exc:
        errors.append(exc)
    return errors


def cleanup_created_lock(handle: LockHandle) -> None:
    cleanup_errors: list[BaseException] = []
    if not handle.file_existed:
        try:
            if handle.path.exists() or handle.path.is_symlink():
                info = require_regular_file(handle.path, f"lock file {handle.path}", private=True)
                if handle.file_identity is not None and identity_of(info) != handle.file_identity:
                    raise ConcurrentTargetChange(f"lock file changed before cleanup: {handle.path}")
                handle.path.unlink()
                fsync_directory(handle.path.parent)
            if handle.parent_snapshot is not None:
                restore_private_directory_metadata(
                    handle.path.parent,
                    handle.parent_snapshot,
                    f"lock parent {handle.path.parent}",
                )
                assert_private_directory_metadata(
                    handle.path.parent,
                    handle.parent_snapshot,
                    f"lock parent {handle.path.parent}",
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if not handle.parent_existed and (
        handle.path.parent.exists() or handle.path.parent.is_symlink()
    ):
        try:
            require_real_private_directory(handle.path.parent, f"lock parent {handle.path.parent}")
            handle.path.parent.rmdir()
            fsync_directory(handle.path.parent.parent)
            if handle.container_snapshot is not None:
                restore_private_directory_metadata(
                    handle.path.parent.parent,
                    handle.container_snapshot,
                    f"lock parent container {handle.path.parent.parent}",
                )
                assert_private_directory_metadata(
                    handle.path.parent.parent,
                    handle.container_snapshot,
                    f"lock parent container {handle.path.parent.parent}",
                )
        except BaseException as exc:
            cleanup_errors.append(exc)
    if cleanup_errors:
        raise cleanup_errors[0]


@contextlib.contextmanager
def internal_target_lock(target: Path, *, create_target: bool) -> Iterator[Path]:
    internal_handle: LockHandle | None = None
    target_existed = target.exists() or target.is_symlink()
    target_parent_snapshot = snapshot_private_directory_metadata(target.parent, "target parent")
    success = False
    try:
        target_exists = ensure_target_directory(target, create=create_target)
        if target_exists:
            internal = target / ".nddev-opencode-lock" / "lock"
            internal_handle = lock_file(internal)
        yield target
        success = True
    finally:
        release_errors: list[BaseException] = []
        if internal_handle is not None:
            release_errors.extend(release_lock_handle(internal_handle))
        if not success:
            cleanup_errors: list[BaseException] = []
            if internal_handle is not None:
                try:
                    cleanup_created_lock(internal_handle)
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if not target_existed and (target.exists() or target.is_symlink()):
                try:
                    target.rmdir()
                    fsync_directory(target.parent)
                    if target_parent_snapshot is not None:
                        restore_private_directory_metadata(
                            target.parent, target_parent_snapshot, "target parent"
                        )
                except BaseException as exc:
                    cleanup_errors.append(exc)
            if cleanup_errors:
                raise cleanup_errors[0]
        if release_errors:
            fail(f"cannot release lock safely: {release_errors[0]}")


@contextlib.contextmanager
def target_locks(
    target: Path,
    *,
    create_target: bool,
    lock_internal: bool = True,
) -> Iterator[Path]:
    coordination_handle = acquire_anchor(
        coordination_lock_path(),
        anchor="product",
        create=True,
    )
    external_handle: LockHandle | None = None
    try:
        assert coordination_handle is not None
        canonical_target = resolve_target_locked(target)
        canonical = str(canonical_target)
        token = sha256_bytes(canonical.encode("utf-8"))
        external = external_lock_path_for_digest(token)
        external_handle = acquire_anchor(
            external,
            anchor="target",
            target_digest=token,
            create=True,
        )
        coordination_release_errors = release_lock_handle(coordination_handle)
        coordination_handle = None
        if coordination_release_errors:
            fail(f"cannot release product lock safely: {coordination_release_errors[0]}")
        if lock_internal:
            with internal_target_lock(canonical_target, create_target=create_target):
                yield canonical_target
        else:
            if create_target:
                fail("internal target lock is required before target creation")
            yield canonical_target
    finally:
        release_errors: list[BaseException] = []
        if external_handle is not None:
            release_errors.extend(release_lock_handle(external_handle))
        if coordination_handle is not None:
            release_errors.extend(release_lock_handle(coordination_handle))
        if release_errors:
            fail(f"cannot release lock safely: {release_errors[0]}")


def product_anchor_present() -> bool:
    root = system_lock_root()
    if not root.exists() and not root.is_symlink():
        return False
    require_real_private_directory(root, f"lock root {root}")
    path = coordination_lock_path()
    return path.exists() or path.is_symlink()


@contextlib.contextmanager
def read_only_target_locks(target: Path) -> Iterator[Path]:
    coordination_handle: LockHandle | None = None
    external_handle: LockHandle | None = None
    cold_no_anchor = False
    try:
        coordination_handle = acquire_anchor(
            coordination_lock_path(),
            anchor="product",
            shared=True,
            create=False,
        )
        if coordination_handle is None:
            cold_no_anchor = True
            canonical_target = resolve_target_locked(target)
            token = sha256_bytes(str(canonical_target).encode("utf-8"))
            orphan_target_anchor = external_lock_path_for_digest(token)
            if orphan_target_anchor.exists() or orphan_target_anchor.is_symlink():
                fail("orphan target anchor exists without product coordination anchor")
            yield canonical_target
        else:
            canonical_target = resolve_target_locked(target)
            token = sha256_bytes(str(canonical_target).encode("utf-8"))
            external_handle = acquire_anchor(
                external_lock_path_for_digest(token),
                anchor="target",
                target_digest=token,
                shared=True,
                create=False,
            )
            if external_handle is not None:
                release_errors = release_lock_handle(coordination_handle)
                coordination_handle = None
                if release_errors:
                    fail(f"cannot release product lock safely: {release_errors[0]}")
            yield canonical_target
    finally:
        release_errors: list[BaseException] = []
        if external_handle is not None:
            release_errors.extend(release_lock_handle(external_handle))
        if coordination_handle is not None:
            release_errors.extend(release_lock_handle(coordination_handle))
        if release_errors:
            fail(f"cannot release read-only lock safely: {release_errors[0]}")
        if cold_no_anchor and product_anchor_present():
            raise ConcurrentTargetChange("product anchor appeared during cold read")


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


def launch(target: Path, child_args: list[str], *, host: dict[str, Any] | None = None) -> int:
    _ = host if host is not None else detect_supported_host()
    with target_locks(target, create_target=False) as locked_target:
        target = locked_target
        load_cleanup_journal(target, recover_aliases=True)
        drain_cleanup_pending(target, allow_pending=False)
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

    for name in ("plan", "install", "switch"):
        p = sub.add_parser(name, help=f"{name} managed setup/profile")
        add_target(p)
        p.add_argument("--setup", default=CONTENT_SETUP_ID, choices=[CONTENT_SETUP_ID])
        p.add_argument("--profile", default=DEFAULT_PROFILE_ID, choices=PROFILE_IDS)
        add_json(p)

    p = sub.add_parser("update", help="refresh the installed managed setup/profile")
    add_target(p)
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


def require_prechecked_host(host: dict[str, Any] | None) -> dict[str, Any]:
    if host is None:
        fail("supported host precheck did not run")
    return host


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
        prechecked_host = detect_supported_host() if command in HOST_PRECHECK_COMMANDS else None
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
            return launch(target, child_args, host=prechecked_host)

        target = resolve_target(args.target) if hasattr(args, "target") else None
        assert target is not None
        json_output = bool(getattr(args, "json", False))

        if command in {"status", "software-status", "plan"}:
            payload = None
            for attempt in range(READ_ONLY_RETRY_ATTEMPTS):
                try:
                    with read_only_target_locks(target) as locked_target:
                        target = locked_target
                        if command == "status":
                            payload = current_status(target)
                        elif command == "software-status":
                            payload = software_status_payload(target)
                        else:
                            payload = plan_payload(target, render_profile(args.profile))
                    break
                except ConcurrentTargetChange:
                    if attempt + 1 < READ_ONLY_RETRY_ATTEMPTS:
                        continue
                    raise
            assert payload is not None
        elif command in {"install", "switch", "update"}:
            payload = None
            with target_locks(target, create_target=False, lock_internal=False) as locked_target:
                target = locked_target
                cleanup_drained = load_cleanup_journal(target, recover_aliases=True) is not None
                drain_cleanup_pending(target, allow_pending=False)
                profile = (
                    current_update_profile(target)
                    if command == "update"
                    else render_profile(args.profile)
                )
                payload = lifecycle_noop_payload(target, profile, operation=command)
                if payload is not None and cleanup_drained:
                    payload["changed"] = True
                    payload["changes"] = ["cleanup-pending"]
            if payload is None:
                if command == "update":
                    with target_locks(
                        target, create_target=False, lock_internal=False
                    ) as locked_target:
                        target = locked_target
                        with internal_target_lock(target, create_target=False):
                            cleanup_drained = (
                                load_cleanup_journal(target, recover_aliases=True) is not None
                            )
                            drain_cleanup_pending(target, allow_pending=False)
                            profile = current_update_profile(target)
                            payload = lifecycle_noop_payload(target, profile, operation="update")
                            if payload is not None and cleanup_drained:
                                payload["changed"] = True
                                payload["changes"] = ["cleanup-pending"]
                            if payload is None:
                                payload = install_or_switch(target, profile, operation=command)
                else:
                    with target_locks(
                        target,
                        create_target=command == "install",
                    ) as locked_target:
                        target = locked_target
                        payload = install_or_switch(
                            target,
                            render_profile(args.profile),
                            operation=command,
                        )
        else:
            create_for_command = command == "install-cli"
            with target_locks(target, create_target=create_for_command) as locked_target:
                target = locked_target
                if command == "migrate":
                    payload = migrate_target(target, args.profile)
                elif command == "restore":
                    payload = restore_target(target, args.backup)
                elif command == "remove":
                    payload = remove_target(target)
                elif command == "install-cli":
                    payload = install_cli(
                        target,
                        update=False,
                        host_detector=lambda host=prechecked_host: require_prechecked_host(host),
                    )
                elif command == "update-cli":
                    payload = install_cli(
                        target,
                        update=True,
                        host_detector=lambda host=prechecked_host: require_prechecked_host(host),
                    )
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
