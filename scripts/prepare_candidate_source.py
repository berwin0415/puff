#!/usr/bin/env python3
"""导出固定版本的第三方源码，不使用宿主依赖或凭证。

这仅是宿主侧构建准备，不安装或运行候选。
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import subprocess
import tarfile
import tempfile
from datetime import datetime, timezone

import yaml


SCHEMA = "harness-lab-source-manifest/1"
EXCLUDED_PARTS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", ".pytest_cache",
    ".mypy_cache", ".ruff_cache", ".cache", "old", ".DS_Store",
}
PRIVATE_ROOTS = {"workspaces", "state", "runs", "sessions", ".openclaw", ".codex"}
PRIVATE_FILES = {".npmrc", ".netrc", "credentials.json", "auth.json", "auth-profiles.json"}
SAFE_ENV_FILES = {".env.example", ".env.sample", ".env.template"}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def allowed(relative: PurePosixPath) -> bool:
    """即使 Git 已跟踪，也排除宿主状态与凭证目录。"""
    if relative.is_absolute() or ".." in relative.parts or not relative.parts:
        return False
    if set(relative.parts) & EXCLUDED_PARTS or relative.parts[0] in PRIVATE_ROOTS:
        return False
    name = relative.name.lower()
    if name in PRIVATE_FILES or (name.startswith(".env") and name not in SAFE_ENV_FILES):
        return False
    if name.endswith((".pem", ".key", ".p12", ".pfx")) or name.startswith("id_rsa"):
        return False
    return True


def git(path: Path, *args: str) -> bytes:
    return subprocess.check_output(["git", "-C", str(path), *args], stderr=subprocess.PIPE)


def write_file(root: Path, relative: PurePosixPath, data: bytes, mode: int) -> None:
    target = root.joinpath(*relative.parts)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)
    target.chmod(mode & 0o777 & ~0o022)


def write_link(root: Path, relative: PurePosixPath, target: str) -> None:
    """仅保留解析后目标仍位于本次源码导出目录内的链接。"""
    link = root.joinpath(*relative.parts)
    resolved = (link.parent / target).resolve()
    if Path(target).is_absolute() or not resolved.is_relative_to(root.resolve()):
        raise ValueError(f"符号链接越出源码目录：{relative}")
    destination = PurePosixPath(resolved.relative_to(root.resolve()).as_posix())
    if not allowed(destination):
        raise ValueError(f"符号链接指向被排除的源码位置：{relative}")
    link.parent.mkdir(parents=True, exist_ok=True)
    link.symlink_to(target)


def export_git(source: dict, output: Path) -> dict:
    path = Path(source["path"]).expanduser().resolve()
    commit = git(path, "rev-parse", "HEAD").decode().strip()
    if commit != source["commit"]:
        raise ValueError(f"提交不匹配：{source['id']}")
    status = git(path, "status", "--porcelain", "--untracked-files=all")
    if status:
        raise ValueError(f"拒绝使用有未提交变更的源码：{source['id']} ({len(status.splitlines())} 个条目)")
    remote = git(path, "remote", "get-url", "origin").decode().strip()
    if remote.removesuffix(".git").rstrip("/") != source["official_url"].removesuffix(".git").rstrip("/"):
        raise ValueError(f"origin 不匹配：{source['id']}")
    submodules = git(path, "submodule", "status").decode().strip()
    if submodules:
        raise ValueError('子模块需要单独固定的源码条目')
    excluded = 0
    for raw in git(path, "ls-files", "-z").split(b"\0"):
        if not raw:
            continue
        relative = PurePosixPath(raw.decode())
        if not allowed(relative):
            excluded += 1
            continue
        item = path.joinpath(*relative.parts)
        if item.is_symlink():
            write_link(output, relative, os.readlink(item))
        elif item.is_file():
            write_file(output, relative, item.read_bytes(), item.stat().st_mode)
        else:
            raise ValueError(f"已跟踪源码不是普通文件：{relative}")
    return {"dirty": False, "status_sha256": sha256(status), "submodules": [],
            "excluded_entries": excluded, "observed_commit": commit, "observed_origin": remote}


def export_archive(source: dict, output: Path) -> dict:
    path = Path(source["path"]).expanduser().resolve()
    digest = sha256(path.read_bytes())
    if digest != source["sha256"]:
        raise ValueError(f"归档哈希不匹配：{source['id']}")
    excluded = 0
    seen: set[str] = set()
    with tarfile.open(path, "r:gz") as archive:
        for item in archive:
            full = PurePosixPath(item.name)
            if full.is_absolute() or ".." in full.parts:
                raise ValueError('归档路径不安全')
            if len(full.parts) < 2:
                continue
            if full.parts[0] != source["archive_prefix"]:
                raise ValueError('归档根目录与固定提交不匹配')
            relative = PurePosixPath(*full.parts[1:])
            if not allowed(relative):
                excluded += 1
                continue
            if item.isdir():
                continue
            if str(relative) in seen:
                raise ValueError('归档条目重复')
            seen.add(str(relative))
            if item.isfile():
                stream = archive.extractfile(item)
                if stream is None:
                    raise ValueError('归档文件没有内容')
                write_file(output, relative, stream.read(), item.mode)
            elif item.issym():
                write_link(output, relative, item.linkname)
            else:
                raise ValueError(f"不支持的归档条目：{relative}")
    return {"dirty": None, "dirty_reason": '直接从已核验哈希的归档导出；未使用工作区。',
            "observed_archive_sha256": digest, "excluded_entries": excluded, "submodules": 'GitHub 源码归档未包含此项'}


def inventory(root: Path) -> list[dict]:
    result = []
    for item in sorted(root.rglob("*")):
        if item.is_symlink():
            content = os.readlink(item).encode()
            result.append({"path": item.relative_to(root).as_posix(), "type": "symlink",
                           "target": os.readlink(item), "sha256": sha256(content)})
        elif item.is_file():
            data = item.read_bytes()
            result.append({"path": item.relative_to(root).as_posix(), "type": "file",
                           "size": len(data), "mode": item.stat().st_mode & 0o777, "sha256": sha256(data)})
    return result


def prepare(config_path: Path, destination_root: Path) -> dict:
    config_bytes = config_path.read_bytes()
    config = yaml.safe_load(config_bytes)
    candidate = config["candidate"]
    if not re.fullmatch(r"[a-z][a-z0-9_]*", candidate):
        raise ValueError('候选名称无效')
    destination_root = destination_root.resolve()
    destination_root.mkdir(parents=True, exist_ok=True)
    destination = destination_root / candidate
    if destination.exists():
        marker = destination / "SOURCE_MANIFEST.json"
        if not marker.is_file() or json.loads(marker.read_text()).get("schema") != SCHEMA:
            raise ValueError('拒绝替换非本脚本生成的目录')
    stage = Path(tempfile.mkdtemp(prefix=f".{candidate}-", dir=destination_root))
    try:
        sources = []
        for source in config["sources"]:
            if not re.fullmatch(r"[a-z][a-z0-9_-]*", source["id"]):
                raise ValueError('源码 ID 无效')
            output = stage / "sources" / source["id"]
            output.mkdir(parents=True)
            if source["kind"] == "git":
                observed = export_git(source, output)
            elif source["kind"] == "archive":
                observed = export_archive(source, output)
            else:
                raise ValueError('不支持的源码类型')
            sources.append({**source, **observed})
        files = inventory(stage)
        manifest = {"schema": SCHEMA, "candidate": candidate,
                    "created_at": datetime.now(timezone.utc).isoformat(),
                    "config_sha256": sha256(config_bytes), "sources": sources,
                    "export_sha256": sha256(json.dumps(files, sort_keys=True).encode()), "files": files}
        (stage / "SOURCE_MANIFEST.json").write_text(json.dumps(manifest, indent=2, ensure_ascii=False) + "\n")
        if destination.exists():
            shutil.rmtree(destination)
        stage.rename(destination)
        return {"candidate": candidate, "destination": str(destination),
                "files": len(files), "export_sha256": manifest["export_sha256"]}
    finally:
        if stage.exists():
            shutil.rmtree(stage)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, help='单个候选配置')
    parser.add_argument("--all", action="store_true", help='导出全部 configs/candidates/*.yaml')
    parser.add_argument("--destination-root", type=Path, default=Path("external/build-context"))
    args = parser.parse_args()
    if bool(args.config) == args.all:
        parser.error('必须且只能选择 --config 或 --all 之一')
    configs = sorted(Path("configs/candidates").glob("*.yaml")) if args.all else [args.config]
    for config in configs:
        print(json.dumps(prepare(config, args.destination_root), ensure_ascii=False))


if __name__ == "__main__":
    main()
