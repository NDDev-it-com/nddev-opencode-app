#!/usr/bin/env python3
"""Target-explicit OpenCode setup manager for NDDev.

The manager writes one selected setup into an explicit absolute OpenCode config
directory. It never infers or mutates the caller's live ``~/.config/opencode``.
Only the managed OpenCode config keys, ``AGENTS.md``, and the native NDDev
builder projection are owned; sibling config keys and unrelated files are
preserved.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import tempfile
import time
from collections.abc import Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any, NoReturn

ROOT = Path(__file__).resolve().parents[1]
CATALOG_ROOT = ROOT / "setups"
VERSION = (ROOT / "VERSION").read_text(encoding="ascii").strip()
PRODUCT_NAME = "nddev-opencode-app"
STAMP_NAME = "NDDEV-OPENCODE-SETUP.json"
BACKUP_NAME = "NDDEV-OPENCODE-BACKUP.json"
STAMP_SCHEMA = 1
BACKUP_SCHEMA = 1
MAX_BACKUPS = 10
OWNER_FILE_MODE = 0o600
OWNER_DIR_MODE = 0o700
METADATA_MAX_BYTES = 256 * 1024
MANAGED_PAYLOAD_MAX_BYTES = 1024 * 1024
SETUP_ID_PATTERN = re.compile(r"[a-z0-9]+(?:-[a-z0-9]+)*\Z")
SHA256_PATTERN = re.compile(r"[0-9a-f]{64}\Z")
MANAGED_FILES = (
    "opencode.json",
    "AGENTS.md",
    "plugins/nddev-builder.js",
    "skills/nddev-builder/SKILL.md",
    "agents/nddev-builder.md",
)
CONFIG_MANAGED_KEYS = ("autoupdate", "share", "permission")
PROVIDER_SECRET_NAMES = {
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "GOOGLE_API_KEY",
    "GOOGLE_APPLICATION_CREDENTIALS",
    "GROK_API_KEY",
    "XAI_API_KEY",
    "OPENROUTER_API_KEY",
    "AWS_ACCESS_KEY_ID",
    "AWS_SECRET_ACCESS_KEY",
    "AWS_SESSION_TOKEN",
}
PACKAGE_MANAGER_SECRET_NAMES = {
    "NODE_AUTH_TOKEN",
    "NPM_TOKEN",
    "BUN_AUTH_TOKEN",
    "npm_config_userconfig",
    "npm_config_prefix",
}
LAUNCH_BLOCKED_BOOLEAN_FLAGS = {
    "--auto": "permission auto-approval override",
    "--dangerously-skip-permissions": "permission bypass override",
    "--global": "global config scope override",
    "--pure": "native plugin projection override",
    "--yolo": "permission bypass override",
}
LAUNCH_BLOCKED_VALUE_FLAGS = {
    "--agent": "agent override",
    "--attach": "remote server scope override",
    "--cwd": "working-directory scope override",
    "--dir": "working-directory scope override",
    "--mode": "agent mode override",
    "--path": "agent path scope override",
    "--permissions": "agent permission override",
    "--tools": "agent permission override",
}
LAUNCH_BLOCKED_SHORT_FLAGS = {
    "-g": "global config scope override",
}
LAUNCH_BLOCKED_COMMANDS = {
    "upgrade": "target-owned software updates must go through update-cli",
}
OPENCODE_PACKAGE_NAME = "opencode-ai"
OPENCODE_PACKAGE_VERSION = "1.18.6"
OPENCODE_COMMAND = "opencode"
OPENCODE_PACKAGE_BIN = "bin/opencode.exe"
OPENCODE_POSTINSTALL_SCRIPT = "node ./postinstall.mjs"
OPENCODE_REGISTRY_INTEGRITY = "sha512-MKombYcQfUlBFa6bo5ikev4nPcmHo4rI8q8KWfqFZFmNNqEutaXFYJKQHiUKE6nPOXcTI7T4sK7hicEYwL3S1w=="
OPENCODE_REGISTRY_SHASUM = "de8f32d2328a3f07891f263ce7f2b6f790324e13"
BUN_INSTALL_ARGV = [
    "add",
    "--global",
    "--exact",
    "--trust",
    f"{OPENCODE_PACKAGE_NAME}@{OPENCODE_PACKAGE_VERSION}",
]
SOFTWARE_STAMP_NAME = "NDDEV-OPENCODE-SOFTWARE.json"
SOFTWARE_DIR_NAME = ".nddev-opencode-software"
SOFTWARE_CURRENT_NAME = "current"
SOFTWARE_STAGE_FRAGMENT = ".nddev-opencode-software-stage"
SOFTWARE_MAX_BYTES = 512 * 1024 * 1024
SOFTWARE_MAX_PATHS = 20000
PROCESS_OUTPUT_MAX_BYTES = 64 * 1024
BUN_INSTALL_TIMEOUT_SECONDS = 900
STAGED_VERSION_PROBE_TIMEOUT_SECONDS = 120
OPENCODE_BINARY_RELATIVE = "install/global/node_modules/opencode-ai/bin/opencode.exe"
SOFTWARE_STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "canonical_target",
    "package",
    "version",
    "command",
    "package_bin",
    "entrypoint",
    "entrypoint_kind",
    "entrypoint_main",
    "installed_tree",
    "manager",
    "entrypoint_sha256",
    "package_binary_sha256",
    "installed_tree_sha256",
    "registry",
    "version_probe",
    "official_package_scripts",
    "installer",
}
SOFTWARE_STAMP_REGISTRY_KEYS = {"integrity", "shasum"}
SOFTWARE_STAMP_PROBE_KEYS = {"argv", "environment", "stdout_stderr_sha256"}
SOFTWARE_STAMP_SCRIPT_KEYS = {"postinstall"}
SOFTWARE_STAMP_INSTALLER_KEYS = {"tool", "argv", "trust_reason", "env"}
SOFTWARE_STAMP_INSTALLER_ENV_KEYS = {
    "BUN_INSTALL_GLOBAL_DIR",
    "BUN_INSTALL_BIN",
    "BUN_INSTALL_CACHE_DIR",
    "HOME",
    "XDG_CONFIG_HOME",
    "TMPDIR",
}
STAMP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "setup_id",
    "canonical_target",
    "managed_files",
    "builder",
}
BACKUP_KEYS = {
    "schema_version",
    "product_name",
    "build_version",
    "slot",
    "canonical_target",
    "source_setup_id",
    "managed_files",
    "stamp_sha256",
}


class ManagerError(Exception):
    """A structured user-facing lifecycle failure."""


class ConcurrentTargetChange(ManagerError):
    """A fail-closed target race or identity change."""


@dataclass(frozen=True)
class Setup:
    setup_id: str
    description: str
    managed_files: tuple[str, ...]
    builder_enabled: bool
    files: dict[str, bytes]


@dataclass(frozen=True)
class FileSnapshot:
    content: bytes | None
    digest: str | None


def fail(message: str) -> NoReturn:
    raise ManagerError(message)


def canonical_json(value: Any) -> bytes:
    return (json.dumps(value, indent=2, sort_keys=True) + "\n").encode("utf-8")


def sha256_bytes(value: bytes) -> str:
    return hashlib.sha256(value).hexdigest()


def identity_of(info: os.stat_result) -> tuple[int, int]:
    return info.st_dev, info.st_ino


def owner_of(info: os.stat_result) -> int | None:
    return info.st_uid if hasattr(info, "st_uid") else None


def is_current_user_owner(info: os.stat_result) -> bool:
    if not hasattr(os, "geteuid"):
        return True
    return owner_of(info) == os.geteuid()


def require_current_user_owner(info: os.stat_result, label: str) -> None:
    if not is_current_user_owner(info):
        fail(f"{label} must be owned by the current user")


def is_owner_only_file(info: os.stat_result) -> bool:
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        return False
    if not is_current_user_owner(info):
        return False
    return True


def safe_relative_path(relative: str) -> Path:
    path = Path(relative)
    if path.is_absolute() or any(part in {"", ".", ".."} for part in path.parts):
        fail(f"managed path is not safe: {relative}")
    return path


def reject_symlink_ancestors(root: Path, relative: str) -> None:
    current = root
    parts = safe_relative_path(relative).parts
    for part in parts[:-1]:
        current = current / part
        try:
            info = current.lstat()
        except FileNotFoundError:
            return
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            fail(f"managed parent must be a real directory: {current}")


def require_directory(path: Path, label: str) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a real directory")
    return info


def require_regular_file(path: Path, label: str, *, owner_only: bool) -> os.stat_result:
    try:
        info = path.lstat()
    except FileNotFoundError:
        fail(f"{label} is missing")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"{label} must not have hard-link aliases")
    if owner_only and not is_owner_only_file(info):
        fail(f"{label} must be owned by the current user with mode 0600")
    return info


def read_regular_file(
    path: Path,
    label: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> tuple[bytes, os.stat_result]:
    before = require_regular_file(path, label, owner_only=owner_only)
    if before.st_size > max_bytes:
        fail(f"{label} exceeds the {max_bytes}-byte size limit")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(path, flags)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(before):
            raise ConcurrentTargetChange(f"{label} changed while it was opened")
        if opened.st_size > max_bytes:
            fail(f"{label} exceeds the {max_bytes}-byte size limit")
        blocks: list[bytes] = []
        total = 0
        while True:
            block = os.read(descriptor, 65536)
            if not block:
                break
            total += len(block)
            if total > max_bytes:
                fail(f"{label} exceeds the {max_bytes}-byte size limit")
            blocks.append(block)
        after = os.fstat(descriptor)
    finally:
        os.close(descriptor)
    final = require_regular_file(path, label, owner_only=owner_only)
    if identity_of(final) != identity_of(before) or identity_of(after) != identity_of(before):
        raise ConcurrentTargetChange(f"{label} changed while it was read")
    return b"".join(blocks), final


def parse_json_object(content: bytes, label: str) -> dict[str, Any]:
    try:
        value = json.loads(content.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        fail(f"cannot read valid JSON from {label}: {exc}")
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    return value


def read_json_file(path: Path, label: str, *, owner_only: bool = False) -> dict[str, Any]:
    content, _ = read_regular_file(
        path,
        label,
        owner_only=owner_only,
        max_bytes=METADATA_MAX_BYTES,
    )
    return parse_json_object(content, label)


def exact_keys(value: Any, expected: set[str], label: str) -> dict[str, Any]:
    if not isinstance(value, dict):
        fail(f"{label} must contain a JSON object")
    keys = set(value)
    if keys != expected:
        missing = sorted(expected - keys)
        extra = sorted(keys - expected)
        detail = []
        if missing:
            detail.append("missing " + ", ".join(missing))
        if extra:
            detail.append("extra " + ", ".join(extra))
        fail(f"{label} schema mismatch: {'; '.join(detail)}")
    return value


def stat_optional(path: Path, label: str) -> os.stat_result | None:
    try:
        info = path.lstat()
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        fail(f"{label} must not be a symlink")
    return info


def canonical_target_readonly(target: Path) -> str:
    info = stat_optional(target, "target")
    if info is not None and not stat.S_ISDIR(info.st_mode):
        fail("target must be a real directory")
    return str(target.resolve(strict=False))


def require_safe_partial_directory(path: Path, label: str) -> None:
    info = stat_optional(path, label)
    if info is None:
        return
    if not stat.S_ISDIR(info.st_mode):
        fail(f"{label} must be a directory")
    require_current_user_owner(info, label)
    if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
        fail(f"{label} must be private")


def require_safe_partial_file(path: Path, label: str, *, max_bytes: int) -> None:
    info = stat_optional(path, label)
    if info is None:
        return
    if not stat.S_ISREG(info.st_mode):
        fail(f"{label} must be a regular file")
    require_current_user_owner(info, label)
    if info.st_nlink != 1:
        fail(f"{label} must not be a hardlink")
    if info.st_size > max_bytes:
        fail(f"{label} is too large")


def file_sha256(path: Path, *, label: str) -> str:
    content, info = read_regular_file(path, label, owner_only=False, max_bytes=SOFTWARE_MAX_BYTES)
    require_current_user_owner(info, label)
    return sha256_bytes(content)


def tree_sha256(root: Path) -> str:
    root_info = require_directory(root, "software tree")
    if stat.S_IMODE(root_info.st_mode) != OWNER_DIR_MODE:
        fail("software tree root must be private")
    paths = sorted(root.rglob("*"), key=lambda item: item.relative_to(root).as_posix())
    if len(paths) > SOFTWARE_MAX_PATHS:
        fail("software tree has too many paths")
    digest = hashlib.sha256()
    total = 0
    for path in paths:
        relative = path.relative_to(root).as_posix()
        info = path.lstat()
        mode = stat.S_IMODE(info.st_mode)
        if stat.S_ISLNK(info.st_mode):
            fail(f"software tree must not contain symlinks: {relative}")
        digest.update(relative.encode("utf-8") + b"\0" + oct(mode).encode("ascii") + b"\0")
        if stat.S_ISDIR(info.st_mode):
            require_current_user_owner(info, relative)
            if mode != OWNER_DIR_MODE:
                fail(f"software tree directory must be private: {relative}")
            digest.update(b"dir\0")
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"software tree entry must be a regular file: {relative}")
        require_current_user_owner(info, relative)
        if info.st_nlink != 1:
            fail(f"software tree entry must not be a hardlink: {relative}")
        content, _ = read_regular_file(path, relative, max_bytes=SOFTWARE_MAX_BYTES)
        total += len(content)
        if total > SOFTWARE_MAX_BYTES:
            fail("software tree is too large")
        digest.update(b"file\0" + sha256_bytes(content).encode("ascii") + b"\0")
    return digest.hexdigest()


def validate_setup_id(setup_id: str) -> None:
    if not SETUP_ID_PATTERN.fullmatch(setup_id):
        fail(f"invalid setup id: {setup_id!r}")


def software_root(target: Path) -> Path:
    return target / SOFTWARE_DIR_NAME


def software_current(target: Path) -> Path:
    return software_root(target) / SOFTWARE_CURRENT_NAME


def software_stamp_path(target: Path) -> Path:
    return target / SOFTWARE_STAMP_NAME


def software_entrypoint(target: Path) -> Path:
    return target / "bin" / OPENCODE_COMMAND


def software_presence(target: Path) -> list[str]:
    labels = (
        (software_stamp_path(target), SOFTWARE_STAMP_NAME),
        (software_root(target), SOFTWARE_DIR_NAME),
        (software_current(target), f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}"),
        (software_entrypoint(target), "bin/opencode"),
    )
    present = [label for path, label in labels if path.exists() or path.is_symlink()]
    return sorted(present)


def validate_pre_network_software_target(target: Path) -> None:
    require_safe_partial_directory(target, "target")
    require_safe_partial_directory(software_entrypoint(target).parent, "bin")
    require_safe_partial_directory(software_root(target), "software root")
    require_safe_partial_directory(software_current(target), "current software tree")
    require_safe_partial_file(
        software_entrypoint(target), "OpenCode entrypoint", max_bytes=SOFTWARE_MAX_BYTES
    )
    require_safe_partial_file(
        software_stamp_path(target), SOFTWARE_STAMP_NAME, max_bytes=METADATA_MAX_BYTES
    )


def package_manifest_path(root: Path) -> Path:
    return root / "install" / "global" / "node_modules" / OPENCODE_PACKAGE_NAME / "package.json"


def package_binary_path(root: Path) -> Path:
    return root / OPENCODE_BINARY_RELATIVE


def load_package_manifest(root: Path) -> dict[str, Any]:
    manifest = read_json_file(package_manifest_path(root), "OpenCode package manifest")
    if manifest.get("name") != OPENCODE_PACKAGE_NAME:
        fail("OpenCode package manifest has unexpected package name")
    if manifest.get("version") != OPENCODE_PACKAGE_VERSION:
        fail("OpenCode package manifest has unexpected package version")
    if manifest.get("bin") not in (
        {OPENCODE_COMMAND: OPENCODE_PACKAGE_BIN},
        {OPENCODE_COMMAND: f"./{OPENCODE_PACKAGE_BIN}"},
    ):
        fail("OpenCode package manifest has unexpected bin mapping")
    scripts = manifest.get("scripts")
    if not isinstance(scripts, dict) or scripts.get("postinstall") != OPENCODE_POSTINSTALL_SCRIPT:
        fail("OpenCode package manifest must declare the official postinstall")
    return manifest


def is_relative_to(path: Path, parent: Path) -> bool:
    try:
        path.relative_to(parent)
    except ValueError:
        return False
    return True


def private_mode_for_source(info: os.stat_result) -> int:
    return 0o700 if stat.S_IMODE(info.st_mode) & 0o100 else OWNER_FILE_MODE


def read_staged_file(source: Path, label: str) -> tuple[bytes, os.stat_result]:
    info = source.lstat()
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"staged software entry must be a regular file: {label}")
    if info.st_size > SOFTWARE_MAX_BYTES:
        fail(f"staged software entry is too large: {label}")
    flags = os.O_RDONLY
    if hasattr(os, "O_CLOEXEC"):
        flags |= os.O_CLOEXEC
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor = os.open(source, flags)
    try:
        opened = os.fstat(descriptor)
        if identity_of(opened) != identity_of(info):
            raise ConcurrentTargetChange(f"staged software entry changed while opening: {label}")
        chunks: list[bytes] = []
        total = 0
        while True:
            chunk = os.read(descriptor, 65536)
            if not chunk:
                break
            total += len(chunk)
            if total > SOFTWARE_MAX_BYTES:
                fail(f"staged software entry is too large: {label}")
            chunks.append(chunk)
    finally:
        os.close(descriptor)
    return b"".join(chunks), info


def copy_file_private(source: Path, destination: Path, label: str) -> None:
    content, info = read_staged_file(source, label)
    destination.parent.mkdir(mode=OWNER_DIR_MODE, parents=True, exist_ok=True)
    with destination.open("xb") as target_handle:
        target_handle.write(content)
    destination.chmod(private_mode_for_source(info))


def materialized_source(path: Path, allowed_roots: tuple[Path, ...], label: str) -> Path:
    info = path.lstat()
    if not stat.S_ISLNK(info.st_mode):
        return path
    try:
        resolved = path.resolve(strict=True)
    except FileNotFoundError:
        fail(f"staged software symlink is broken: {label}")
    if not any(is_relative_to(resolved, root) for root in allowed_roots):
        fail(f"staged software symlink escapes persisted tree: {label}")
    resolved_info = resolved.lstat()
    if stat.S_ISLNK(resolved_info.st_mode):
        resolved = resolved.resolve(strict=True)
        resolved_info = resolved.lstat()
    if not stat.S_ISREG(resolved_info.st_mode):
        fail(f"staged software symlink must resolve to a regular file: {label}")
    return resolved


def copy_tree_sanitized(source: Path, destination: Path, allowed_roots: tuple[Path, ...]) -> None:
    require_directory(source, "staged software tree")
    paths = sorted(source.rglob("*"), key=lambda item: item.relative_to(source).as_posix())
    if len(paths) > SOFTWARE_MAX_PATHS:
        fail("staged software tree has too many paths")
    destination.mkdir(mode=OWNER_DIR_MODE)
    total = 0
    for path in paths:
        relative = path.relative_to(source)
        target_path = destination / relative
        info = path.lstat()
        if stat.S_ISDIR(info.st_mode):
            target_path.mkdir(mode=OWNER_DIR_MODE, exist_ok=True)
            continue
        if stat.S_ISLNK(info.st_mode):
            source_file = materialized_source(path, allowed_roots, relative.as_posix())
            source_info = source_file.lstat()
            if not stat.S_ISREG(source_info.st_mode):
                fail(
                    f"staged software symlink must resolve to a regular file: {relative.as_posix()}"
                )
            total += source_info.st_size
            if total > SOFTWARE_MAX_BYTES:
                fail("staged software tree is too large")
            copy_file_private(source_file, target_path, relative.as_posix())
            continue
        if not stat.S_ISREG(info.st_mode):
            fail(f"staged software entry must be a regular file: {relative.as_posix()}")
        total += info.st_size
        if total > SOFTWARE_MAX_BYTES:
            fail("staged software tree is too large")
        copy_file_private(path, target_path, relative.as_posix())


def materialize_persisted_install(stage_workspace: Path, stage_current: Path) -> None:
    allowed_roots = (
        (stage_workspace / "install" / "global").resolve(strict=False),
        (stage_workspace / "bin").resolve(strict=False),
    )
    stage_current.mkdir(mode=OWNER_DIR_MODE)
    (stage_current / "install").mkdir(mode=OWNER_DIR_MODE)
    copy_tree_sanitized(
        stage_workspace / "install" / "global",
        stage_current / "install" / "global",
        allowed_roots,
    )
    copy_tree_sanitized(stage_workspace / "bin", stage_current / "bin", allowed_roots)


def safe_bun_env(stage_workspace: Path) -> dict[str, str]:
    home = stage_workspace / "home"
    xdg_config = stage_workspace / "xdg-config"
    cache = stage_workspace / "cache"
    tmp = stage_workspace / "tmp"
    for directory in (
        home,
        xdg_config,
        cache,
        tmp,
        stage_workspace / "install" / "global",
        stage_workspace / "bin",
    ):
        directory.mkdir(mode=OWNER_DIR_MODE, parents=True, exist_ok=True)
    return {
        "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        "HOME": str(home),
        "XDG_CONFIG_HOME": str(xdg_config),
        "TMPDIR": str(tmp),
        "BUN_INSTALL_GLOBAL_DIR": str(stage_workspace / "install" / "global"),
        "BUN_INSTALL_BIN": str(stage_workspace / "bin"),
        "BUN_INSTALL_CACHE_DIR": str(cache),
    }


def read_process_output(handle: Any, label: str) -> str:
    handle.seek(0, os.SEEK_END)
    size = handle.tell()
    handle.seek(0)
    data = handle.read(PROCESS_OUTPUT_MAX_BYTES + 1)
    text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
    if size > PROCESS_OUTPUT_MAX_BYTES:
        return text[:PROCESS_OUTPUT_MAX_BYTES] + f"\n[{label} truncated]\n"
    return text


def run_bun_install(stage_workspace: Path) -> None:
    command = ["bun", *BUN_INSTALL_ARGV]
    env = safe_bun_env(stage_workspace)
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=BUN_INSTALL_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            fail("bun command was not found on PATH")
        except subprocess.TimeoutExpired:
            fail(f"bun install timed out after {BUN_INSTALL_TIMEOUT_SECONDS} seconds")
        if completed.returncode != 0:
            detail = (
                read_process_output(stderr, "stderr") or read_process_output(stdout, "stdout")
            ).strip()
            fail(f"bun install failed with exit code {completed.returncode}: {detail}")


def run_stage_version_probe(stage_current: Path, stage_workspace: Path) -> str:
    home = stage_workspace / "smoke-home"
    config_dir = stage_workspace / "smoke-opencode-config"
    tmp = stage_workspace / "smoke-tmp"
    for directory in (home, config_dir, tmp):
        directory.mkdir(mode=OWNER_DIR_MODE, parents=True, exist_ok=True)
    env = {
        "HOME": str(home),
        "OPENCODE_CONFIG": str(config_dir / "opencode.json"),
        "OPENCODE_CONFIG_DIR": str(config_dir),
        "PATH": "/usr/bin:/bin",
        "TMPDIR": str(tmp),
    }
    command = [str(stage_current / "bin" / OPENCODE_COMMAND), "--version"]
    with tempfile.TemporaryFile() as stdout, tempfile.TemporaryFile() as stderr:
        try:
            completed = subprocess.run(
                command,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=stdout,
                stderr=stderr,
                check=False,
                timeout=STAGED_VERSION_PROBE_TIMEOUT_SECONDS,
            )
        except FileNotFoundError:
            fail("staged opencode executable is missing")
        except subprocess.TimeoutExpired:
            fail(
                "staged opencode version probe timed out after "
                f"{STAGED_VERSION_PROBE_TIMEOUT_SECONDS} seconds"
            )
        output = (
            read_process_output(stdout, "stdout") + read_process_output(stderr, "stderr")
        ).strip()
        if completed.returncode != 0:
            fail(
                f"staged opencode version probe failed with exit code {completed.returncode}: {output}"
            )
        if OPENCODE_PACKAGE_VERSION not in output:
            fail("staged opencode version probe did not report the pinned release")
        return sha256_bytes(output.encode("utf-8"))


def render_setup(setup_id: str) -> Setup:
    validate_setup_id(setup_id)
    setup_root = CATALOG_ROOT / setup_id
    if not setup_root.is_dir() or setup_root.is_symlink():
        fail(f"unknown setup: {setup_id}")
    metadata = read_json_file(setup_root / "setup.json", f"setup {setup_id} metadata")
    expected_keys = {"schema_version", "id", "description", "managed_files", "builder_enabled"}
    if set(metadata) != expected_keys:
        fail(f"setup {setup_id} metadata has invalid keys")
    if metadata["schema_version"] != 1 or metadata["id"] != setup_id:
        fail(f"setup {setup_id} metadata identity or schema is invalid")
    if metadata["managed_files"] != list(MANAGED_FILES):
        fail(f"setup {setup_id} managed file declaration is invalid")
    if metadata["builder_enabled"] is not True:
        fail(f"setup {setup_id} must enable the native nddev-builder projection")
    if not isinstance(metadata["description"], str) or not metadata["description"].strip():
        fail(f"setup {setup_id} description must be non-empty")

    files: dict[str, bytes] = {}
    for relative in MANAGED_FILES:
        path = setup_root / safe_relative_path(relative)
        content, _ = read_regular_file(path, f"setup {setup_id}/{relative}")
        try:
            content.decode("utf-8")
        except UnicodeDecodeError as exc:
            fail(f"setup {setup_id}/{relative} must be UTF-8: {exc}")
        if not content or not content.endswith(b"\n") or b"\r" in content:
            fail(f"setup {setup_id}/{relative} must be non-empty LF-terminated text")
        files[relative] = content

    config = parse_json_object(files["opencode.json"], f"setup {setup_id}/opencode.json")
    if config.get("$schema") != "https://opencode.ai/config.json":
        fail(f"setup {setup_id}/opencode.json must use the current OpenCode schema")
    if "tools" in config or "tool" in config:
        fail(f"setup {setup_id}/opencode.json must not use legacy tools config")
    permission = config.get("permission")
    if not isinstance(permission, dict):
        fail(f"setup {setup_id}/opencode.json permission must be an object")
    if setup_id == "safe":
        if permission.get("edit") != "deny" or permission.get("bash") != "ask":
            fail("safe setup permission posture is invalid")
        if (permission.get("skill") or {}).get("nddev-builder") != "allow":
            fail("safe setup must allow the nddev-builder skill")
        if (permission.get("task") or {}).get("nddev-builder") != "allow":
            fail("safe setup must allow the nddev-builder subagent")
    elif setup_id == "balanced":
        if permission.get("edit") != "ask" or permission.get("bash") != "ask":
            fail("balanced setup permission posture is invalid")
        if permission.get("external_directory") != "ask" or permission.get("webfetch") != "ask":
            fail("balanced setup must gate external directories and web fetches")
        if (permission.get("skill") or {}).get("nddev-builder") != "allow":
            fail("balanced setup must allow the nddev-builder skill")
        if (permission.get("task") or {}).get("nddev-builder") != "allow":
            fail("balanced setup must allow the nddev-builder subagent")
    elif setup_id == "full-auto":
        if permission.get("edit") != "allow" or permission.get("bash") != "allow":
            fail("full-auto setup permission posture is invalid")
        if permission.get("skill") != {"*": "allow"}:
            fail("full-auto setup must allow native skills")
        if permission.get("task") != {"*": "allow"}:
            fail("full-auto setup must allow native subagents")
    else:
        fail(f"unsupported setup id: {setup_id}")
    return Setup(
        setup_id=setup_id,
        description=metadata["description"],
        managed_files=tuple(metadata["managed_files"]),
        builder_enabled=True,
        files=files,
    )


def list_setups() -> list[dict[str, Any]]:
    if not CATALOG_ROOT.is_dir() or CATALOG_ROOT.is_symlink():
        fail("setup catalog is missing or unsafe")
    result: list[dict[str, Any]] = []
    for candidate in sorted(CATALOG_ROOT.iterdir(), key=lambda item: item.name):
        if not candidate.is_dir() or candidate.is_symlink():
            fail(f"catalog entry must be a real directory: {candidate.name}")
        setup = render_setup(candidate.name)
        result.append(
            {
                "id": setup.setup_id,
                "description": setup.description,
                "managed_files": list(setup.managed_files),
                "builder_enabled": setup.builder_enabled,
            }
        )
    if not result:
        fail("setup catalog is empty")
    return result


def resolve_target(raw_target: str | None) -> Path:
    if not raw_target:
        fail("--target is required")
    expanded = Path(raw_target).expanduser()
    if not expanded.is_absolute():
        fail("--target must be an absolute path")
    try:
        raw_info = expanded.lstat()
    except FileNotFoundError:
        raw_info = None
    if raw_info is not None and stat.S_ISLNK(raw_info.st_mode):
        fail("--target must not be a symlink")
    target = expanded.resolve(strict=False)
    if target == Path(target.anchor):
        fail("filesystem root cannot be a target")
    parent = target.parent
    parent_info = require_directory(parent, "canonical --target parent")
    if stat.S_ISLNK(parent_info.st_mode):
        fail("canonical --target parent must be a real directory")
    if target.exists():
        target_info = require_directory(target, "--target")
        if stat.S_ISLNK(target_info.st_mode):
            fail("--target must not be a symlink")
    return target


def ensure_target_directory(target: Path, *, create: bool) -> bool:
    try:
        info = target.lstat()
    except FileNotFoundError:
        if not create:
            return False
        target.mkdir(mode=OWNER_DIR_MODE)
        os.chmod(target, OWNER_DIR_MODE)
        return True
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        fail("--target must be a real directory")
    return True


def target_path(target: Path, relative: str) -> Path:
    reject_symlink_ancestors(target, relative)
    return target / safe_relative_path(relative)


def target_file_exists(target: Path, relative: str) -> bool:
    path = target_path(target, relative)
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        fail(f"managed path {path} must be a regular non-symlink file")
    if info.st_nlink != 1:
        fail(f"managed path {path} must not have hard-link aliases")
    return True


def read_target_file(
    target: Path,
    relative: str,
    *,
    owner_only: bool = False,
    max_bytes: int = MANAGED_PAYLOAD_MAX_BYTES,
) -> bytes:
    path = target_path(target, relative)
    content, _ = read_regular_file(
        path,
        f"managed path {path}",
        owner_only=owner_only,
        max_bytes=max_bytes,
    )
    return content


def read_target_json_if_present(target: Path) -> dict[str, Any]:
    path = target_path(target, "opencode.json")
    if not path.exists():
        return {}
    content, _ = read_regular_file(
        path,
        f"OpenCode config {path}",
        owner_only=False,
        max_bytes=METADATA_MAX_BYTES,
    )
    return parse_json_object(content, f"OpenCode config {path}")


def managed_config_fragment(config: dict[str, Any]) -> dict[str, Any]:
    return {key: config[key] for key in CONFIG_MANAGED_KEYS if key in config}


def managed_digest(relative: str, content: bytes) -> str:
    if relative != "opencode.json":
        return sha256_bytes(content)
    config = parse_json_object(content, "managed opencode.json")
    return sha256_bytes(canonical_json(managed_config_fragment(config)))


def compose_config(current: dict[str, Any], setup_config: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    if "$schema" not in result:
        result["$schema"] = setup_config["$schema"]
    for key in CONFIG_MANAGED_KEYS:
        result[key] = setup_config[key]
    return result


def strip_managed_config(current: dict[str, Any]) -> dict[str, Any]:
    result = dict(current)
    for key in CONFIG_MANAGED_KEYS:
        result.pop(key, None)
    return result


def desired_for_setup(target: Path, setup: Setup) -> dict[str, bytes | None]:
    current = read_target_json_if_present(target) if target.exists() else {}
    setup_config = parse_json_object(setup.files["opencode.json"], "setup opencode.json")
    desired = dict(setup.files)
    desired["opencode.json"] = canonical_json(compose_config(current, setup_config))
    return desired


def stamp_payload(target: Path, setup_id: str, desired: dict[str, bytes | None]) -> dict[str, Any]:
    managed_files: dict[str, str | None] = {}
    for relative in MANAGED_FILES:
        content = desired.get(relative)
        managed_files[relative] = None if content is None else managed_digest(relative, content)
    return {
        "schema_version": STAMP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "setup_id": setup_id,
        "canonical_target": str(target),
        "managed_files": managed_files,
        "builder": {
            "projection": "native",
            "enabled": True,
            "files": [
                "plugins/nddev-builder.js",
                "skills/nddev-builder/SKILL.md",
                "agents/nddev-builder.md",
            ],
        },
    }


def validate_digest_map(value: Any, label: str) -> dict[str, str | None]:
    if not isinstance(value, dict) or set(value) != set(MANAGED_FILES):
        fail(f"{label} must declare exactly {list(MANAGED_FILES)}")
    result: dict[str, str | None] = {}
    for name in MANAGED_FILES:
        digest = value[name]
        if digest is not None and (
            not isinstance(digest, str) or not SHA256_PATTERN.fullmatch(digest)
        ):
            fail(f"{label}.{name} must be null or a lowercase SHA-256 digest")
        result[name] = digest
    return result


def load_stamp(target: Path) -> dict[str, Any] | None:
    if not ensure_target_directory(target, create=False):
        return None
    if not target_file_exists(target, STAMP_NAME):
        return None
    content = read_target_file(target, STAMP_NAME, max_bytes=METADATA_MAX_BYTES)
    stamp = parse_json_object(content, f"managed stamp {target / STAMP_NAME}")
    if set(stamp) != STAMP_KEYS:
        fail("managed stamp has invalid keys")
    if stamp["schema_version"] != STAMP_SCHEMA or stamp["product_name"] != PRODUCT_NAME:
        fail("managed stamp identity or schema is invalid")
    if stamp["canonical_target"] != str(target):
        fail("managed stamp is bound to a different canonical target")
    if not isinstance(stamp["setup_id"], str):
        fail("managed stamp setup_id must be a string")
    validate_setup_id(stamp["setup_id"])
    validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    builder = stamp["builder"]
    if not isinstance(builder, dict) or builder.get("projection") != "native":
        fail("managed stamp builder projection is invalid")
    if builder.get("enabled") is not True:
        fail("managed stamp builder projection must be enabled")
    return stamp


def detect_drift(target: Path, stamp: dict[str, Any]) -> list[str]:
    drift: list[str] = []
    expected = validate_digest_map(stamp["managed_files"], "managed stamp managed_files")
    for relative in MANAGED_FILES:
        if not target_file_exists(target, relative):
            drift.append(relative)
            continue
        content = read_target_file(target, relative, owner_only=True)
        if managed_digest(relative, content) != expected[relative]:
            drift.append(relative)
    return drift


def snapshot_managed_files(target: Path) -> dict[str, FileSnapshot]:
    snapshot: dict[str, FileSnapshot] = {}
    for relative in (*MANAGED_FILES, STAMP_NAME):
        if ensure_target_directory(target, create=False) and target_file_exists(target, relative):
            content = read_target_file(target, relative, owner_only=False)
            snapshot[relative] = FileSnapshot(content=content, digest=sha256_bytes(content))
        else:
            snapshot[relative] = FileSnapshot(content=None, digest=None)
    return snapshot


def assert_snapshot(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    for relative, expected in snapshot.items():
        exists = ensure_target_directory(target, create=False) and target_file_exists(
            target, relative
        )
        if not exists:
            actual = FileSnapshot(content=None, digest=None)
        else:
            content = read_target_file(target, relative, owner_only=False)
            actual = FileSnapshot(content=content, digest=sha256_bytes(content))
        if actual.digest != expected.digest:
            raise ConcurrentTargetChange(f"managed path changed concurrently: {relative}")


def preflight_unmanaged_target(target: Path) -> None:
    if not ensure_target_directory(target, create=False):
        return
    for relative in MANAGED_FILES:
        if relative == "opencode.json":
            continue
        if target_file_exists(target, relative):
            fail(f"unmanaged target already has managed path: {relative}")
    config_path = target_path(target, "opencode.json")
    if config_path.exists():
        config = read_target_json_if_present(target)
        managed = set(CONFIG_MANAGED_KEYS) & set(config)
        if managed:
            fail(f"unmanaged target already has managed OpenCode config keys: {sorted(managed)}")


def make_parent_directories(path: Path) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    try:
        os.chmod(path.parent, OWNER_DIR_MODE)
    except OSError:
        pass


def atomic_write(path: Path, content: bytes) -> None:
    make_parent_directories(path)
    fd, temporary_name = tempfile.mkstemp(prefix=f".{path.name}.", dir=str(path.parent))
    temporary = Path(temporary_name)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(content)
        os.chmod(temporary, OWNER_FILE_MODE)
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def remove_empty_managed_parents(target: Path, relative: str) -> None:
    path = target / safe_relative_path(relative)
    current = path.parent
    while current != target and current.exists():
        try:
            current.rmdir()
        except OSError:
            break
        current = current.parent


def replace_managed_state(
    target: Path,
    desired: dict[str, bytes | None],
    expected: dict[str, FileSnapshot] | None,
    *,
    remove_empty_parents: bool = True,
) -> None:
    ensure_target_directory(target, create=True)
    if expected is not None:
        assert_snapshot(target, expected)
    for relative in (*MANAGED_FILES, STAMP_NAME):
        path = target_path(target, relative)
        content = desired.get(relative)
        if content is None:
            if path.exists():
                require_regular_file(path, f"managed path {path}", owner_only=False)
                path.unlink()
                if remove_empty_parents:
                    remove_empty_managed_parents(target, relative)
            continue
        atomic_write(path, content)
    if expected is not None:
        for relative in (*MANAGED_FILES, STAMP_NAME):
            target_file_exists(target, relative)


def restore_snapshot(target: Path, snapshot: dict[str, FileSnapshot]) -> None:
    desired = {relative: item.content for relative, item in snapshot.items()}
    replace_managed_state(target, desired, None)


@contextlib.contextmanager
def target_lock(target: Path) -> Iterator[None]:
    lock = target.parent / f".{target.name}.nddev-opencode-lock"
    try:
        lock.mkdir(mode=OWNER_DIR_MODE)
    except FileExistsError:
        fail(f"target is already locked: {lock}")
    try:
        yield
    finally:
        with contextlib.suppress(FileNotFoundError):
            lock.rmdir()


def ensure_software_parent(path: Path, target: Path) -> None:
    relative_parent = path.relative_to(target).parent
    current = target
    for part in relative_parent.parts:
        current = current / part
        info = stat_optional(current, f"software parent {current}")
        if info is None:
            current.mkdir(mode=OWNER_DIR_MODE)
            continue
        if not stat.S_ISDIR(info.st_mode):
            fail(f"software parent is not a directory: {current}")
        require_current_user_owner(info, f"software parent {current}")
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"software parent must be private: {current}")


def software_stamp(
    target: Path,
    *,
    entrypoint_digest: str,
    installed_tree_digest: str,
    package_binary_digest: str,
    version_probe_digest: str,
) -> dict[str, Any]:
    return {
        "schema_version": 1,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "canonical_target": canonical_target_readonly(target),
        "package": OPENCODE_PACKAGE_NAME,
        "version": OPENCODE_PACKAGE_VERSION,
        "command": OPENCODE_COMMAND,
        "package_bin": OPENCODE_PACKAGE_BIN,
        "entrypoint": "bin/opencode",
        "entrypoint_kind": "bun-native-bin",
        "entrypoint_main": f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}/{OPENCODE_BINARY_RELATIVE}",
        "installed_tree": f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}",
        "manager": "cli-tools/nddev_opencode.py",
        "entrypoint_sha256": entrypoint_digest,
        "package_binary_sha256": package_binary_digest,
        "installed_tree_sha256": installed_tree_digest,
        "registry": {
            "integrity": OPENCODE_REGISTRY_INTEGRITY,
            "shasum": OPENCODE_REGISTRY_SHASUM,
        },
        "version_probe": {
            "argv": ["bin/opencode", "--version"],
            "environment": {
                "HOME": "<stage>/smoke-home",
                "OPENCODE_CONFIG": "<stage>/smoke-opencode-config/opencode.json",
                "OPENCODE_CONFIG_DIR": "<stage>/smoke-opencode-config",
                "PATH": "/usr/bin:/bin",
                "TMPDIR": "<stage>/smoke-tmp",
            },
            "stdout_stderr_sha256": version_probe_digest,
        },
        "official_package_scripts": {
            "postinstall": OPENCODE_POSTINSTALL_SCRIPT,
        },
        "installer": {
            "tool": "bun",
            "argv": BUN_INSTALL_ARGV,
            "trust_reason": "official package opencode-ai@1.18.6 declares postinstall=node ./postinstall.mjs and platform optional packages",
            "env": {
                "BUN_INSTALL_GLOBAL_DIR": "<stage>/install/global",
                "BUN_INSTALL_BIN": "<stage>/bin",
                "BUN_INSTALL_CACHE_DIR": "<stage>/cache",
                "HOME": "<stage>/home",
                "XDG_CONFIG_HOME": "<stage>/xdg-config",
                "TMPDIR": "<stage>/tmp",
            },
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
    if info.st_nlink != 1:
        fail("software stamp must not be a hardlink")
    if stat.S_IMODE(info.st_mode) != OWNER_FILE_MODE:
        fail("software stamp mode must be 0600")
    stamp = read_json_file(path, SOFTWARE_STAMP_NAME, owner_only=False)
    exact_keys(stamp, SOFTWARE_STAMP_KEYS, SOFTWARE_STAMP_NAME)
    exact_keys(stamp["registry"], SOFTWARE_STAMP_REGISTRY_KEYS, "software stamp registry")
    exact_keys(stamp["version_probe"], SOFTWARE_STAMP_PROBE_KEYS, "software stamp version_probe")
    exact_keys(
        stamp["official_package_scripts"],
        SOFTWARE_STAMP_SCRIPT_KEYS,
        "software stamp official_package_scripts",
    )
    installer = exact_keys(
        stamp["installer"], SOFTWARE_STAMP_INSTALLER_KEYS, "software stamp installer"
    )
    exact_keys(installer["env"], SOFTWARE_STAMP_INSTALLER_ENV_KEYS, "software stamp installer env")
    if stamp.get("product_name") != PRODUCT_NAME:
        fail("software stamp belongs to another product")
    if stamp.get("canonical_target") != canonical_target_readonly(target):
        fail("software stamp is bound to a different canonical target")
    return stamp


def software_status_payload(target: Path) -> dict[str, Any]:
    canonical = canonical_target_readonly(target)
    payload: dict[str, Any] = {
        "installed": False,
        "current": False,
        "package": OPENCODE_PACKAGE_NAME,
        "version": None,
        "expected_version": OPENCODE_PACKAGE_VERSION,
        "command": OPENCODE_COMMAND,
        "executable": str(software_entrypoint(target)),
        "installed_tree": str(software_current(target)),
        "drift": [],
        "present": False,
        "presence": [],
        "canonical_target": canonical,
    }
    if not target.exists():
        return payload
    target_info = require_directory(target, "target")
    require_current_user_owner(target_info, "target")
    if stat.S_IMODE(target_info.st_mode) != OWNER_DIR_MODE:
        fail("target must be private")
    presence = software_presence(target)
    payload["present"] = bool(presence)
    payload["presence"] = presence
    stamp = read_software_stamp(target)
    if stamp is None:
        return payload
    payload["installed"] = True
    payload["version"] = stamp.get("version")
    drift: list[str] = []
    try:
        root_info = stat_optional(software_root(target), "software root")
        if root_info is None or not stat.S_ISDIR(root_info.st_mode):
            drift.append(SOFTWARE_DIR_NAME)
        elif stat.S_IMODE(root_info.st_mode) != OWNER_DIR_MODE:
            drift.append("software_root_mode")
        current_info = stat_optional(software_current(target), "current software tree")
        if current_info is None or not stat.S_ISDIR(current_info.st_mode):
            drift.append(SOFTWARE_CURRENT_NAME)
        elif stat.S_IMODE(current_info.st_mode) != OWNER_DIR_MODE:
            drift.append("software_current_mode")
        entrypoint_info = require_regular_file(
            software_entrypoint(target), "OpenCode entrypoint", owner_only=False
        )
        require_current_user_owner(entrypoint_info, "OpenCode entrypoint")
        if stat.S_IMODE(entrypoint_info.st_mode) != 0o700:
            drift.append("entrypoint_mode")
        load_package_manifest(software_current(target))
        entrypoint_digest = file_sha256(software_entrypoint(target), label="OpenCode entrypoint")
        package_binary_digest = file_sha256(
            package_binary_path(software_current(target)), label="OpenCode package binary"
        )
        installed_tree_digest = tree_sha256(software_current(target))
        expected_env = {
            "BUN_INSTALL_GLOBAL_DIR": "<stage>/install/global",
            "BUN_INSTALL_BIN": "<stage>/bin",
            "BUN_INSTALL_CACHE_DIR": "<stage>/cache",
            "HOME": "<stage>/home",
            "XDG_CONFIG_HOME": "<stage>/xdg-config",
            "TMPDIR": "<stage>/tmp",
        }
        expected_probe_env = {
            "HOME": "<stage>/smoke-home",
            "OPENCODE_CONFIG": "<stage>/smoke-opencode-config/opencode.json",
            "OPENCODE_CONFIG_DIR": "<stage>/smoke-opencode-config",
            "PATH": "/usr/bin:/bin",
            "TMPDIR": "<stage>/smoke-tmp",
        }
        checks = {
            "schema_version": stamp.get("schema_version") == 1,
            "product_name": stamp.get("product_name") == PRODUCT_NAME,
            "build_version": stamp.get("build_version") == VERSION,
            "canonical_target": stamp.get("canonical_target") == canonical,
            "package": stamp.get("package") == OPENCODE_PACKAGE_NAME,
            "version": stamp.get("version") == OPENCODE_PACKAGE_VERSION,
            "command": stamp.get("command") == OPENCODE_COMMAND,
            "package_bin": stamp.get("package_bin") == OPENCODE_PACKAGE_BIN,
            "entrypoint": stamp.get("entrypoint") == "bin/opencode",
            "entrypoint_kind": stamp.get("entrypoint_kind") == "bun-native-bin",
            "entrypoint_main": stamp.get("entrypoint_main")
            == f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}/{OPENCODE_BINARY_RELATIVE}",
            "installed_tree": stamp.get("installed_tree")
            == f"{SOFTWARE_DIR_NAME}/{SOFTWARE_CURRENT_NAME}",
            "manager": stamp.get("manager") == "cli-tools/nddev_opencode.py",
            "entrypoint_sha256": stamp.get("entrypoint_sha256") == entrypoint_digest,
            "package_binary_sha256": stamp.get("package_binary_sha256") == package_binary_digest,
            "installed_tree_sha256": stamp.get("installed_tree_sha256") == installed_tree_digest,
        }
        for label, ok in checks.items():
            if not ok:
                drift.append(label)
        registry = stamp.get("registry")
        if (
            not isinstance(registry, dict)
            or registry.get("integrity") != OPENCODE_REGISTRY_INTEGRITY
            or registry.get("shasum") != OPENCODE_REGISTRY_SHASUM
        ):
            drift.append("registry")
        installer = stamp.get("installer")
        if (
            not isinstance(installer, dict)
            or installer.get("tool") != "bun"
            or installer.get("argv") != BUN_INSTALL_ARGV
            or installer.get("env") != expected_env
            or installer.get("trust_reason")
            != "official package opencode-ai@1.18.6 declares postinstall=node ./postinstall.mjs and platform optional packages"
        ):
            drift.append("installer")
        scripts = stamp.get("official_package_scripts")
        if (
            not isinstance(scripts, dict)
            or scripts.get("postinstall") != OPENCODE_POSTINSTALL_SCRIPT
        ):
            drift.append("official_package_scripts")
        probe = stamp.get("version_probe")
        if (
            not isinstance(probe, dict)
            or probe.get("argv") != ["bin/opencode", "--version"]
            or probe.get("environment") != expected_probe_env
            or not isinstance(probe.get("stdout_stderr_sha256"), str)
        ):
            drift.append("version_probe")
    except ManagerError as exc:
        drift.append(str(exc))
    payload["drift"] = drift
    payload["current"] = not drift and stamp.get("version") == OPENCODE_PACKAGE_VERSION
    return payload


def software_precondition_state(target: Path) -> dict[str, Any]:
    validate_pre_network_software_target(target)
    try:
        return software_status_payload(target)
    except ManagerError as exc:
        info = stat_optional(target, "target")
        if info is None or not stat.S_ISDIR(info.st_mode):
            raise
        presence = software_presence(target)
        if not presence:
            raise
        validate_pre_network_software_target(target)
        return {
            "installed": False,
            "current": False,
            "present": True,
            "presence": presence,
            "drift": [str(exc)],
            "package": OPENCODE_PACKAGE_NAME,
            "version": None,
            "expected_version": OPENCODE_PACKAGE_VERSION,
            "command": OPENCODE_COMMAND,
            "executable": str(software_entrypoint(target)),
            "installed_tree": str(software_current(target)),
            "canonical_target": canonical_target_readonly(target),
        }


def snapshot_software_file(
    path: Path, label: str, max_bytes: int
) -> tuple[bytes | None, int | None]:
    info = stat_optional(path, label)
    if info is None:
        return None, None
    if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        fail(f"{label} must be a regular non-hardlinked file")
    content, opened = read_regular_file(path, label, max_bytes=max_bytes)
    return content, stat.S_IMODE(opened.st_mode)


def restore_software_file(
    path: Path,
    target: Path,
    data: bytes | None,
    mode: int | None,
    *,
    remove_empty_parent: bool,
) -> None:
    if data is None:
        with contextlib.suppress(FileNotFoundError):
            path.unlink()
        if remove_empty_parent:
            with contextlib.suppress(OSError):
                path.parent.rmdir()
        return
    ensure_software_parent(path, target)
    temporary = path.with_name(f".{path.name}.nddev.tmp.{os.getpid()}.{time.time_ns()}")
    fd = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, mode or OWNER_FILE_MODE)
    try:
        with os.fdopen(fd, "wb") as handle:
            handle.write(data)
        os.replace(temporary, path)
        path.chmod(mode or OWNER_FILE_MODE)
    except BaseException:
        with contextlib.suppress(FileNotFoundError):
            temporary.unlink()
        raise


def remove_created_target_if_empty(target: Path) -> None:
    for candidate in (
        software_stamp_path(target),
        software_entrypoint(target),
    ):
        with contextlib.suppress(FileNotFoundError):
            candidate.unlink()
    for candidate in (
        software_entrypoint(target).parent,
        software_current(target),
        software_root(target),
        target,
    ):
        with contextlib.suppress(OSError):
            candidate.rmdir()


def write_software_entrypoint_from_stage(source: Path, destination: Path, target: Path) -> str:
    content, info = read_regular_file(
        source, "staged OpenCode entrypoint", max_bytes=SOFTWARE_MAX_BYTES
    )
    ensure_software_parent(destination, target)
    require_safe_partial_file(destination, "OpenCode entrypoint", max_bytes=SOFTWARE_MAX_BYTES)
    temporary = destination.with_name(
        f".{destination.name}.nddev.tmp.{os.getpid()}.{time.time_ns()}"
    )
    with temporary.open("xb") as handle:
        handle.write(content)
    temporary.chmod(private_mode_for_source(info))
    os.replace(temporary, destination)
    return file_sha256(destination, label="OpenCode entrypoint")


def install_or_update_software(target: Path, *, update: bool) -> dict[str, Any]:
    preflight = software_precondition_state(target)
    if preflight["current"]:
        return {
            "changed": False,
            "package": OPENCODE_PACKAGE_NAME,
            "version": OPENCODE_PACKAGE_VERSION,
            "command": OPENCODE_COMMAND,
            "executable": str(software_entrypoint(target)),
            "installed_tree": str(software_current(target)),
            "target": canonical_target_readonly(target),
        }
    if update and not preflight["present"]:
        fail("update-cli requires existing target-owned OpenCode software presence")
    if not update and preflight["present"]:
        fail(
            "install-cli found partial or non-current target-owned OpenCode software; use update-cli"
        )

    with target_lock(target):
        created_target = stat_optional(target, "target") is None
        try:
            if created_target:
                target.mkdir(mode=OWNER_DIR_MODE)
                os.chmod(target, OWNER_DIR_MODE)
            else:
                require_safe_partial_directory(target, "target")
            status = software_precondition_state(target)
            if status["current"]:
                return {
                    "changed": False,
                    "package": OPENCODE_PACKAGE_NAME,
                    "version": OPENCODE_PACKAGE_VERSION,
                    "command": OPENCODE_COMMAND,
                    "executable": str(software_entrypoint(target)),
                    "installed_tree": str(software_current(target)),
                    "target": canonical_target_readonly(target),
                }
            if update and not status["present"]:
                fail("update-cli requires existing target-owned OpenCode software presence")
            if not update and status["present"]:
                fail(
                    "install-cli found partial or non-current target-owned OpenCode software; use update-cli"
                )

            parent = target.parent
            with (
                tempfile.TemporaryDirectory(
                    prefix=f".{target.name}{SOFTWARE_STAGE_FRAGMENT}.", dir=str(parent)
                ) as stage_raw,
                tempfile.TemporaryDirectory(
                    prefix=f".{target.name}.nddev-opencode-software-rollback.", dir=str(parent)
                ) as rollback_raw,
            ):
                stage_root = Path(stage_raw)
                rollback_root = Path(rollback_raw)
                stage_install = stage_root / "install-output"
                stage_current = stage_root / SOFTWARE_CURRENT_NAME
                run_bun_install(stage_install)
                load_package_manifest(stage_install)
                materialize_persisted_install(stage_install, stage_current)
                staged_entrypoint = stage_current / "bin" / OPENCODE_COMMAND
                require_regular_file(
                    staged_entrypoint, "staged OpenCode entrypoint", owner_only=False
                )
                package_binary = package_binary_path(stage_current)
                require_regular_file(
                    package_binary, "staged OpenCode package binary", owner_only=False
                )
                version_probe_digest = run_stage_version_probe(stage_current, stage_root)
                package_binary_digest = file_sha256(
                    package_binary, label="staged OpenCode package binary"
                )
                installed_tree_digest = tree_sha256(stage_current)

                software_root_was_present = (
                    stat_optional(software_root(target), "software root") is not None
                )
                entrypoint_parent_was_present = (
                    stat_optional(software_entrypoint(target).parent, "bin") is not None
                )
                software_root(target).mkdir(mode=OWNER_DIR_MODE, exist_ok=True)
                os.chmod(software_root(target), OWNER_DIR_MODE)
                current = software_current(target)
                rollback_current = rollback_root / SOFTWARE_CURRENT_NAME
                previous_entrypoint, previous_entrypoint_mode = snapshot_software_file(
                    software_entrypoint(target), "OpenCode entrypoint", SOFTWARE_MAX_BYTES
                )
                previous_stamp, previous_stamp_mode = snapshot_software_file(
                    software_stamp_path(target), SOFTWARE_STAMP_NAME, METADATA_MAX_BYTES
                )
                current_moved = False
                new_current_installed = False
                try:
                    current_info = stat_optional(current, "current software tree")
                    if current_info is not None:
                        if not stat.S_ISDIR(current_info.st_mode):
                            fail("current software tree must be a directory")
                        current.rename(rollback_current)
                        current_moved = True
                    stage_current.rename(current)
                    new_current_installed = True
                    entrypoint_digest = write_software_entrypoint_from_stage(
                        current / "bin" / OPENCODE_COMMAND,
                        software_entrypoint(target),
                        target,
                    )
                    if os.environ.get("NDDEV_OPENCODE_TEST_FAIL_AFTER_ENTRYPOINT") == "1":
                        fail("injected software swap failure after entrypoint")
                    stamp = software_stamp(
                        target,
                        entrypoint_digest=entrypoint_digest,
                        installed_tree_digest=installed_tree_digest,
                        package_binary_digest=package_binary_digest,
                        version_probe_digest=version_probe_digest,
                    )
                    atomic_write(software_stamp_path(target), canonical_json(stamp))
                    verified = software_status_payload(target)
                    if not verified["current"]:
                        fail(
                            f"installed software failed status verification: {', '.join(verified['drift'])}"
                        )
                except BaseException:
                    if new_current_installed:
                        shutil.rmtree(current, ignore_errors=True)
                    if current_moved:
                        rollback_current.rename(current)
                    restore_software_file(
                        software_entrypoint(target),
                        target,
                        previous_entrypoint,
                        previous_entrypoint_mode,
                        remove_empty_parent=not entrypoint_parent_was_present,
                    )
                    restore_software_file(
                        software_stamp_path(target),
                        target,
                        previous_stamp,
                        previous_stamp_mode,
                        remove_empty_parent=False,
                    )
                    if not software_root_was_present:
                        with contextlib.suppress(OSError):
                            software_root(target).rmdir()
                    raise
                return {
                    "changed": True,
                    "package": OPENCODE_PACKAGE_NAME,
                    "version": OPENCODE_PACKAGE_VERSION,
                    "command": OPENCODE_COMMAND,
                    "executable": str(software_entrypoint(target)),
                    "installed_tree": str(software_current(target)),
                    "target": canonical_target_readonly(target),
                }
        except BaseException:
            if created_target:
                remove_created_target_if_empty(target)
            raise


def backup_pool(target: Path) -> Path:
    return target.parent / f".{target.name}.nddev-opencode-backups"


def choose_backup_slot(pool: Path) -> int:
    if not pool.exists():
        return 0
    slots = sorted(
        int(path.name)
        for path in pool.iterdir()
        if path.is_dir() and path.name.isdigit() and 0 <= int(path.name) < MAX_BACKUPS
    )
    if not slots:
        return 0
    return (slots[-1] + 1) % MAX_BACKUPS


def write_backup(target: Path, stamp: dict[str, Any]) -> int:
    pool = backup_pool(target)
    if pool.exists() and pool.is_symlink():
        fail("backup pool must not be a symlink")
    pool.mkdir(mode=OWNER_DIR_MODE, exist_ok=True)
    os.chmod(pool, OWNER_DIR_MODE)
    slot = choose_backup_slot(pool)
    slot_dir = pool / str(slot)
    if slot_dir.exists():
        shutil.rmtree(slot_dir)
    files_dir = slot_dir / "files"
    files_dir.mkdir(parents=True, mode=OWNER_DIR_MODE)
    managed_files: dict[str, str | None] = {}
    for relative in MANAGED_FILES:
        if target_file_exists(target, relative):
            content = read_target_file(target, relative, owner_only=False)
            backup_path = files_dir / safe_relative_path(relative)
            atomic_write(backup_path, content)
            managed_files[relative] = managed_digest(relative, content)
        else:
            managed_files[relative] = None
    stamp_content = read_target_file(target, STAMP_NAME, max_bytes=METADATA_MAX_BYTES)
    envelope = {
        "schema_version": BACKUP_SCHEMA,
        "product_name": PRODUCT_NAME,
        "build_version": VERSION,
        "slot": slot,
        "canonical_target": str(target),
        "source_setup_id": stamp["setup_id"],
        "managed_files": managed_files,
        "stamp_sha256": sha256_bytes(stamp_content),
    }
    atomic_write(slot_dir / BACKUP_NAME, canonical_json(envelope))
    return slot


def load_backup(target: Path, slot: int) -> tuple[dict[str, Any], dict[str, bytes | None]]:
    if slot < 0 or slot >= MAX_BACKUPS:
        fail("--backup must be between 0 and 9")
    slot_dir = backup_pool(target) / str(slot)
    envelope_path = slot_dir / BACKUP_NAME
    if envelope_path.is_symlink() or not envelope_path.is_file():
        fail(f"backup slot is missing: {slot}")
    envelope = read_json_file(envelope_path, f"backup slot {slot}", owner_only=False)
    if set(envelope) != BACKUP_KEYS:
        fail("backup envelope has invalid keys")
    if envelope["schema_version"] != BACKUP_SCHEMA or envelope["product_name"] != PRODUCT_NAME:
        fail("backup envelope identity or schema is invalid")
    if envelope["canonical_target"] != str(target):
        fail("backup belongs to a different canonical target")
    validate_digest_map(envelope["managed_files"], "backup managed_files")
    files: dict[str, bytes | None] = {}
    files_dir = slot_dir / "files"
    for relative in MANAGED_FILES:
        expected = envelope["managed_files"][relative]
        path = files_dir / safe_relative_path(relative)
        if expected is None:
            files[relative] = None
            continue
        content, _ = read_regular_file(path, f"backup file {relative}", owner_only=False)
        if managed_digest(relative, content) != expected:
            fail(f"backup file digest mismatch: {relative}")
        files[relative] = content
    stamp = stamp_payload(target, envelope["source_setup_id"], files)
    files[STAMP_NAME] = canonical_json(stamp)
    return envelope, files


def current_status(target: Path) -> dict[str, Any]:
    if not ensure_target_directory(target, create=False):
        return {
            "state": "missing",
            "target": str(target),
            "setup_id": None,
            "drift": [],
            "builder": {"projection": "native", "enabled": False},
        }
    stamp = load_stamp(target)
    if stamp is None:
        return {
            "state": "unmanaged",
            "target": str(target),
            "setup_id": None,
            "drift": [],
            "builder": {"projection": "native", "enabled": False},
        }
    drift = detect_drift(target, stamp)
    return {
        "state": "managed",
        "target": str(target),
        "setup_id": stamp["setup_id"],
        "build_version": stamp["build_version"],
        "drift": drift,
        "builder": {
            "projection": "native",
            "enabled": not any(
                item in drift
                for item in (
                    "plugins/nddev-builder.js",
                    "skills/nddev-builder/SKILL.md",
                    "agents/nddev-builder.md",
                )
            ),
        },
    }


def plan_setup(target: Path, setup_id: str) -> dict[str, Any]:
    render_setup(setup_id)
    status = current_status(target)
    if status["state"] == "missing":
        operation = "install"
        backup_required = False
    elif status["state"] == "unmanaged":
        operation = "install"
        backup_required = False
    elif status["setup_id"] == setup_id:
        operation = "update"
        backup_required = False
    else:
        operation = "switch"
        backup_required = True
    return {
        "operation": operation,
        "target": str(target),
        "setup_id": setup_id,
        "mutates": False,
        "backup_required": backup_required,
        "state": status["state"],
        "current_setup_id": status["setup_id"],
        "drift": status["drift"],
    }


def require_clean_managed(target: Path) -> dict[str, Any]:
    stamp = load_stamp(target)
    if stamp is None:
        fail("target is not managed")
    drift = detect_drift(target, stamp)
    if drift:
        fail(f"managed target has drift: {drift}")
    return stamp


def mutate_setup(target: Path, setup_id: str, action: str) -> dict[str, Any]:
    setup = render_setup(setup_id)
    with target_lock(target):
        ensure_target_directory(target, create=True)
        existing_stamp = load_stamp(target)
        if existing_stamp is None:
            if action == "switch":
                fail("switch requires a managed target")
            preflight_unmanaged_target(target)
        else:
            drift = detect_drift(target, existing_stamp)
            if drift:
                fail(f"managed target has drift: {drift}")
        backup_slot: int | None = None
        if existing_stamp is not None and existing_stamp["setup_id"] != setup_id:
            backup_slot = write_backup(target, existing_stamp)
        before = snapshot_managed_files(target)
        desired = desired_for_setup(target, setup)
        desired[STAMP_NAME] = canonical_json(stamp_payload(target, setup_id, desired))
        try:
            replace_managed_state(target, desired, before)
        except BaseException:
            restore_snapshot(target, before)
            raise
        changed = [
            relative
            for relative in MANAGED_FILES
            if before[relative].digest != sha256_bytes(desired[relative] or b"")
        ]
        return {
            "operation": "install" if existing_stamp is None else action,
            "target": str(target),
            "setup_id": setup_id,
            "changed": changed,
            "backup_slot": backup_slot,
            "builder": {"projection": "native", "enabled": True},
        }


def restore_backup(target: Path, slot: int) -> dict[str, Any]:
    with target_lock(target):
        stamp = require_clean_managed(target)
        _, files = load_backup(target, slot)
        backup_slot = write_backup(target, stamp)
        before = snapshot_managed_files(target)
        try:
            replace_managed_state(target, files, before)
        except BaseException:
            restore_snapshot(target, before)
            raise
        restored_stamp = load_stamp(target)
        assert restored_stamp is not None
        return {
            "operation": "restore",
            "target": str(target),
            "setup_id": restored_stamp["setup_id"],
            "backup_slot": backup_slot,
            "restored_backup": slot,
            "builder": {"projection": "native", "enabled": True},
        }


def remove_setup(target: Path) -> dict[str, Any]:
    with target_lock(target):
        stamp = require_clean_managed(target)
        backup_slot = write_backup(target, stamp)
        before = snapshot_managed_files(target)
        desired: dict[str, bytes | None] = {relative: None for relative in MANAGED_FILES}
        if target_file_exists(target, "opencode.json"):
            current = read_target_json_if_present(target)
            stripped = strip_managed_config(current)
            desired["opencode.json"] = canonical_json(stripped) if stripped else None
        desired[STAMP_NAME] = None
        try:
            replace_managed_state(target, desired, before)
        except BaseException:
            restore_snapshot(target, before)
            raise
        return {
            "operation": "remove",
            "target": str(target),
            "removed_setup_id": stamp["setup_id"],
            "backup_slot": backup_slot,
            "builder": {"projection": "native", "enabled": False},
        }


def ensure_private_launch_directory(target: Path, relative: str) -> Path:
    path = target
    for part in safe_relative_path(relative).parts:
        path = path / part
        label = f"launch runtime directory {path.relative_to(target)}"
        info = stat_optional(path, label)
        if info is None:
            path.mkdir(mode=OWNER_DIR_MODE)
            os.chmod(path, OWNER_DIR_MODE)
            info = require_directory(path, label)
        if not stat.S_ISDIR(info.st_mode):
            fail(f"{label} must be a real directory")
        require_current_user_owner(info, label)
        if stat.S_IMODE(info.st_mode) != OWNER_DIR_MODE:
            fail(f"{label} must be private")
    return path


def build_launch_env(target: Path) -> dict[str, str]:
    runtime_home = target / ".runtime-home"
    xdg_config = target / ".xdg" / "config"
    xdg_data = target / ".xdg" / "data"
    xdg_state = target / ".xdg" / "state"
    xdg_cache = target / ".xdg" / "cache"
    for relative in (
        ".runtime-home",
        ".xdg/config",
        ".xdg/data",
        ".xdg/state",
        ".xdg/cache",
    ):
        ensure_private_launch_directory(target, relative)
    return {
        "HOME": str(runtime_home),
        "OPENCODE_CONFIG": str((target / "opencode.json").resolve()),
        "OPENCODE_CONFIG_DIR": str(target),
        "XDG_CONFIG_HOME": str(xdg_config),
        "XDG_DATA_HOME": str(xdg_data),
        "XDG_STATE_HOME": str(xdg_state),
        "XDG_CACHE_HOME": str(xdg_cache),
        "PATH": "/usr/bin:/bin",
    }


def require_safe_launch_args(child_args: list[str]) -> None:
    first_command: str | None = None
    for token in child_args:
        if token == "--":
            return
        if not token.startswith("-"):
            if first_command is None:
                first_command = token
            continue
        if token in LAUNCH_BLOCKED_SHORT_FLAGS:
            fail(f"launch argument {token} is not allowed: {LAUNCH_BLOCKED_SHORT_FLAGS[token]}")
        flag = token.split("=", 1)[0]
        if flag in LAUNCH_BLOCKED_BOOLEAN_FLAGS:
            fail(f"launch argument {flag} is not allowed: {LAUNCH_BLOCKED_BOOLEAN_FLAGS[flag]}")
        if flag in LAUNCH_BLOCKED_VALUE_FLAGS:
            fail(f"launch argument {flag} is not allowed: {LAUNCH_BLOCKED_VALUE_FLAGS[flag]}")
    if first_command in LAUNCH_BLOCKED_COMMANDS:
        fail(
            f"launch command {first_command} is not allowed: {LAUNCH_BLOCKED_COMMANDS[first_command]}"
        )


def prepare_launch_invocation(
    target: Path, child_args: list[str]
) -> tuple[list[str], dict[str, str]]:
    with target_lock(target):
        require_clean_managed(target)
        software = software_status_payload(target)
        if not software["current"]:
            drift = software.get("drift") or ["target-owned OpenCode package is not installed"]
            fail(f"launch requires current target-owned OpenCode package: {', '.join(drift)}")
        executable = software_entrypoint(target)
        executable_info = require_regular_file(
            executable, "target-owned OpenCode executable", owner_only=False
        )
        require_current_user_owner(executable_info, "target-owned OpenCode executable")
        if stat.S_IMODE(executable_info.st_mode) != 0o700:
            fail("target-owned OpenCode executable must be private executable")
        require_safe_launch_args(child_args)
        env = build_launch_env(target)
        return [str(executable), *child_args], env


def launch(target: Path, child_args: list[str]) -> int:
    command, env = prepare_launch_invocation(target, child_args)
    try:
        completed = subprocess.run(command, env=env, check=False)
    except FileNotFoundError:
        fail("target-owned opencode executable is missing")
    return int(completed.returncode)


def emit(payload: dict[str, Any] | list[Any], *, as_json: bool) -> None:
    if as_json:
        print(json.dumps(payload, indent=2, sort_keys=True))
    else:
        print(json.dumps(payload, indent=2, sort_keys=True))


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    subparsers = parser.add_subparsers(dest="command", required=True)

    list_parser = subparsers.add_parser("list", help="list available setups")
    list_parser.add_argument("--json", action="store_true")

    for name in ("status", "remove"):
        command = subparsers.add_parser(name)
        command.add_argument("--target")
        command.add_argument("--json", action="store_true")

    for name in ("software-status", "install-cli", "update-cli"):
        command = subparsers.add_parser(name)
        command.add_argument("--target")
        command.add_argument("--json", action="store_true")

    for name in ("plan", "install", "apply", "switch"):
        command = subparsers.add_parser(name)
        command.add_argument("--setup", required=True)
        command.add_argument("--target")
        command.add_argument("--json", action="store_true")

    restore_parser = subparsers.add_parser("restore")
    restore_parser.add_argument("--backup", required=True, type=int)
    restore_parser.add_argument("--target")
    restore_parser.add_argument("--json", action="store_true")

    launch_parser = subparsers.add_parser("launch")
    launch_parser.add_argument("--target")
    launch_parser.add_argument("child_args", nargs=argparse.REMAINDER)
    return parser.parse_args(argv)


def wants_json(argv: list[str]) -> bool:
    return "--json" in argv


def main(argv: list[str] | None = None) -> int:
    raw_argv = list(sys.argv[1:] if argv is None else argv)
    try:
        args = parse_args(raw_argv)
        if args.command == "list":
            emit({"setups": list_setups()}, as_json=args.json)
            return 0
        if args.command == "status":
            emit(current_status(resolve_target(args.target)), as_json=args.json)
            return 0
        if args.command == "software-status":
            emit(software_status_payload(resolve_target(args.target)), as_json=args.json)
            return 0
        if args.command == "install-cli":
            emit(
                install_or_update_software(resolve_target(args.target), update=False),
                as_json=args.json,
            )
            return 0
        if args.command == "update-cli":
            emit(
                install_or_update_software(resolve_target(args.target), update=True),
                as_json=args.json,
            )
            return 0
        if args.command == "plan":
            emit(plan_setup(resolve_target(args.target), args.setup), as_json=args.json)
            return 0
        if args.command in {"install", "apply", "switch"}:
            action = "install" if args.command == "apply" else args.command
            emit(mutate_setup(resolve_target(args.target), args.setup, action), as_json=args.json)
            return 0
        if args.command == "restore":
            emit(restore_backup(resolve_target(args.target), args.backup), as_json=args.json)
            return 0
        if args.command == "remove":
            emit(remove_setup(resolve_target(args.target)), as_json=args.json)
            return 0
        if args.command == "launch":
            child_args = list(args.child_args)
            if child_args and child_args[0] == "--":
                child_args = child_args[1:]
            return launch(resolve_target(args.target), child_args)
        fail(f"unsupported command: {args.command}")
    except ManagerError as exc:
        if wants_json(raw_argv):
            print(json.dumps({"error": str(exc)}, indent=2, sort_keys=True), file=sys.stderr)
        else:
            print(f"nddev-opencode: {exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
