#!/usr/bin/env python3
"""fork-hub 自动打包发布：把配置的 fork 打包成 Release 资产。

设计原则：
  1. 不改任何 fork 的分支/提交，只在目标 fork 上创建 Release 与资产；
  2. 只使用 Python 标准库，复用 fork_updater.py 的令牌读取与 API 层；
  3. HA 集成的 zip 符合 HACS 结构（<domain>/manifest.json），
     软件 / Magisk 模块保持仓库根目录结构；
  4. --dry-run 只打包到本地 dist/，不写任何仓库，可离线验收配置。

用法：
  python release_builder.py --dry-run --only ha-chuanglvjia --version 1.2.3
  python release_builder.py --only ha-chuanglvjia,magisk-tailscaled
  python release_builder.py --only ha-chuanglvjia --force   # 覆盖同名旧资产
"""

from __future__ import annotations

import argparse
import fnmatch
import hashlib
import io
import json
import os
import re
import shutil
import sys
import tarfile
import tempfile
import urllib.error
import urllib.parse
import urllib.request
import zipfile
from datetime import datetime, timezone
from pathlib import Path

from fork_updater import API, GitHub, get_token

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "release-targets.json"
DIST_DIR = BASE_DIR / "dist"
TIMEOUT = 60

DEFAULT_EXCLUDES = [
    ".git",
    "**/.git/**",
    ".github",
    "**/.github/**",
    "__pycache__",
    "**/__pycache__/**",
    "*.pyc",
    "*.pyo",
    ".DS_Store",
    "Thumbs.db",
    ".venv",
    "venv",
    "node_modules",
    "**/node_modules/**",
    "*.log",
    "dist",
]

KIND_HA = "ha-integration"
KIND_MODULE = "module"
KIND_SOFTWARE = "software"


def log(message: str) -> None:
    print("[release] " + message, flush=True)


def warn(message: str) -> None:
    print("[release] 警告: " + message, file=sys.stderr, flush=True)


def load_config() -> dict:
    if not CONFIG_PATH.is_file():
        raise SystemExit("缺少 %s，请创建发布目标配置" % CONFIG_PATH)
    with CONFIG_PATH.open(encoding="utf-8") as handle:
        config = json.load(handle)
    targets = config.get("targets")
    if not isinstance(targets, list) or not targets:
        raise SystemExit("release-targets.json 的 targets 不能为空")
    return config


def api_request(method: str, path: str, token: str, payload=None, raw: bool = False,
                ctype: str = "application/json", host: str = API):
    """标准库 HTTP 请求；raw=True 时 body 用字节直接发送。"""
    if raw:
        data = payload
    else:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    headers = {
        "Accept": "application/vnd.github+json",
        "User-Agent": "fork-hub-release",
        "X-GitHub-Api-Version": "2022-11-28",
    }
    if token:
        headers["Authorization"] = "Bearer " + token
    if data is not None:
        headers["Content-Type"] = ctype
    req = urllib.request.Request(host + path, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as response:
            body = response.read()
            try:
                parsed = json.loads(body.decode("utf-8")) if body else {}
            except (UnicodeDecodeError, json.JSONDecodeError):
                parsed = body
            return response.status, parsed
    except urllib.error.HTTPError as error:
        body = error.read().decode("utf-8", "replace")
        try:
            parsed = json.loads(body)
        except json.JSONDecodeError:
            parsed = body
        return error.code, parsed


def get_login(gh: GitHub | None, token: str, config: dict) -> str:
    if gh is not None:
        status, user = gh.get("/user")
        if status == 200 and isinstance(user, dict):
            return str(user["login"])
    owner = str(config.get("owner") or "")
    if owner:
        return owner
    if token:
        warn("无法获取当前账号，使用配置中的 owner")
    return owner


def download_snapshot(owner: str, repo: str, branch: str, token: str, destination: Path) -> Path:
    """下载默认分支 tarball 并安全解压，返回源码根目录。"""
    path = "/repos/%s/%s/tarball/%s" % (
        urllib.parse.quote(owner, safe=""),
        urllib.parse.quote(repo, safe=""),
        urllib.parse.quote(branch, safe="/"),
    )
    url = API + path
    headers = {"Accept": "application/vnd.github+json", "User-Agent": "fork-hub-release"}
    if token:
        headers["Authorization"] = "Bearer " + token
    request = urllib.request.Request(url, headers=headers)
    with urllib.request.urlopen(request, timeout=120) as response:
        archive = response.read()
    if not archive:
        raise RuntimeError("下载到的源码为空")

    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(fileobj=io.BytesIO(archive), mode="r:gz") as tar:
        for member in tar.getmembers():
            parts = [p for p in Path(member.name).parts if p not in ("", ".", "..")]
            if not parts or Path(member.name).is_absolute() or ".." in Path(member.name).parts:
                continue
            target = destination.joinpath(*parts)
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                source = tar.extractfile(member)
                if source is None:
                    continue
                with source, target.open("wb") as output:
                    shutil.copyfileobj(source, output)

    children = [item for item in destination.iterdir() if item.is_dir()]
    files = [item for item in destination.iterdir() if item.is_file()]
    if len(children) == 1 and not files:
        return children[0]
    if files or children:
        return destination
    raise RuntimeError("源码包解压后没有文件")


def match_any(rel: str, patterns: list[str]) -> bool:
    for raw in patterns:
        pattern = str(raw).replace("\\", "/").strip("/")
        if not pattern:
            continue
        if fnmatch.fnmatch(rel, pattern) or rel.startswith(pattern + "/"):
            return True
        if pattern.startswith("**/") and fnmatch.fnmatch(rel, pattern[3:]):
            return True
        if "/" not in pattern and fnmatch.fnmatch(Path(rel).name, pattern):
            return True
    return False


def detect_kind(root: Path) -> tuple[str, str]:
    if (root / "module.prop").is_file():
        return KIND_MODULE, "."
    integrations = []
    components = root / "custom_components"
    if components.is_dir():
        for candidate in sorted(components.iterdir()):
            if candidate.is_dir() and (candidate / "manifest.json").is_file():
                integrations.append("custom_components/" + candidate.name)
    if len(integrations) == 1:
        return KIND_HA, integrations[0]
    if len(integrations) > 1:
        warn("发现多个 HA 集成，仅打包第一个 %s；如需全部请在配置里逐个声明" % integrations[0])
        return KIND_HA, integrations[0]
    return KIND_SOFTWARE, "."


def resolve_source(root: Path, target: dict) -> tuple[str, Path]:
    kind = str(target.get("kind") or "").strip()
    path = str(target.get("path") or "").strip()
    if not kind or not path:
        detected_kind, detected_path = detect_kind(root)
        kind = kind or detected_kind
        path = path or detected_path
    source = root / path
    if not source.is_dir():
        raise RuntimeError("配置的 path 不存在: %s" % path)
    return kind, source


def manifest_info(source: Path) -> tuple[str, dict | None]:
    manifest = source / "manifest.json"
    if not manifest.is_file():
        return source.name, None
    try:
        data = json.loads(manifest.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        warn("无法读取 manifest.json: %s" % error)
        return source.name, None
    domain = str(data.get("domain") or source.name)
    return domain, data


def stage_files(source: Path, staging: Path, target: dict, config: dict) -> int:
    excludes = list(DEFAULT_EXCLUDES)
    excludes += [str(x) for x in config.get("exclude", [])]
    excludes += [str(x) for x in target.get("exclude", [])]
    includes = [str(x) for x in target.get("include", config.get("include", []))]

    copied = 0
    for folder, dirs, files in os.walk(source):
        folder_path = Path(folder)
        dirs[:] = [
            name for name in dirs
            if not match_any((folder_path / name).relative_to(source).as_posix(), excludes)
        ]
        for name in files:
            item = folder_path / name
            rel = item.relative_to(source).as_posix()
            if match_any(rel, excludes):
                continue
            if includes and not match_any(rel, includes):
                continue
            out = staging / rel
            out.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(item, out)
            copied += 1
    if copied == 0:
        raise RuntimeError("没有可打包的文件，请检查 path / include / exclude")
    return copied


def write_archives(staging: Path, output_dir: Path, base: str) -> list[Path]:
    output_dir.mkdir(parents=True, exist_ok=True)
    for stale in output_dir.glob(base + ".*"):
        stale.unlink()
    zip_path = output_dir / (base + ".zip")
    tar_path = output_dir / (base + ".tar.gz")
    with zipfile.ZipFile(zip_path, "w", compression=zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                archive.write(item, item.relative_to(staging).as_posix())
    with tarfile.open(tar_path, "w:gz") as archive:
        for item in sorted(staging.rglob("*")):
            if item.is_file():
                archive.add(item, arcname=item.relative_to(staging).as_posix())
    checksum = output_dir / "SHA256SUMS"
    lines = []
    for path in (zip_path, tar_path):
        digest = hashlib.sha256(path.read_bytes()).hexdigest()
        lines.append("%s  %s" % (digest, path.name))
    checksum.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return [zip_path, tar_path, checksum]


def build_package(root: Path, target: dict, config: dict, version: str, repo: str) -> tuple[list[Path], str, str]:
    kind, source = resolve_source(root, target)
    with tempfile.TemporaryDirectory(prefix="fork-hub-pkg-") as tmp:
        package_root = Path(tmp) / "package"
        staging = package_root
        domain = ""
        manifest = None
        if kind == KIND_HA:
            domain, manifest = manifest_info(source)
            if target.get("wrap", True):
                staging = package_root / domain
        staging.mkdir(parents=True, exist_ok=True)
        copied = stage_files(source, staging, target, config)

        if kind == KIND_HA and manifest is not None and target.get("sync_manifest_version", True):
            manifest_path = staging / "manifest.json"
            try:
                data = json.loads(manifest_path.read_text(encoding="utf-8"))
                if str(data.get("version", "")) != version:
                    data["version"] = version
                    manifest_path.write_text(
                        json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            except (OSError, json.JSONDecodeError) as error:
                warn("同步 manifest 版本失败: %s" % error)

        base = (domain or repo) + "-" + version
        output_dir = DIST_DIR / repo
        assets = write_archives(package_root, output_dir, base)
        log("%s: %s -> %s（%d 个文件）" % (repo, kind, ", ".join(p.name for p in assets), copied))
        return assets, kind, domain


def release_body(repo: str, kind: str, version: str, tag: str, assets: list[Path], url: str = "") -> str:
    lines = [
        "## %s %s" % (repo, version),
        "",
        "- 打包类型：`%s`" % kind,
        "- 标签：`%s`" % tag,
        "- 由 fork-hub 自动打包发布，源文件来自本仓库默认分支。",
        "",
        "### 资产",
        "",
    ]
    lines += ["- `%s`" % item.name for item in assets]
    if url:
        lines += ["", "运行记录：%s" % url]
    return "\n".join(lines) + "\n"


def ensure_release(gh: GitHub, owner: str, repo: str, tag: str, branch: str,
                   body: str, force: bool) -> tuple[int, dict]:
    status, existing = gh.get("/repos/%s/%s/releases/tags/%s" % (owner, repo, urllib.parse.quote(tag, safe="")))
    if status == 200 and isinstance(existing, dict) and not force:
        return 200, existing
    payload = {
        "tag_name": tag,
        "target_commitish": branch,
        "name": tag,
        "body": body,
        "draft": False,
        "prerelease": False,
        "generate_release_notes": True,
    }
    if status == 200 and isinstance(existing, dict):
        release_id = existing.get("id")
        for asset in existing.get("assets", []):
            gh.request("DELETE", "/repos/%s/%s/releases/assets/%s" % (owner, repo, asset.get("id")))
        status, result = gh.request("PATCH", "/repos/%s/%s/releases/%s" % (owner, repo, release_id), payload)
    else:
        status, result = gh.request("POST", "/repos/%s/%s/releases" % (owner, repo), payload)
    return status, result


def content_type(path: Path) -> str:
    if path.suffix == ".zip":
        return "application/zip"
    if path.name.endswith(".tar.gz"):
        return "application/gzip"
    return "text/plain; charset=utf-8"


def upload_assets(token: str, owner: str, repo: str, release_id: int, assets: list[Path]) -> list[str]:
    uploaded = []
    for asset in assets:
        path = "/repos/%s/%s/releases/%s/assets?name=%s" % (
            owner, repo, release_id, urllib.parse.quote(asset.name, safe=""))
        status, result = api_request(
            "POST", path, token, asset.read_bytes(), raw=True,
            ctype=content_type(asset), host="https://uploads.github.com")
        if status not in (200, 201):
            raise RuntimeError("上传 %s 失败 HTTP %s: %s" % (asset.name, status, result))
        uploaded.append(asset.name)
    return uploaded


def main() -> int:
    parser = argparse.ArgumentParser(description="fork-hub 自动打包并发布 fork 的 Release")
    parser.add_argument("--only", default="", help="只处理指定仓库名，逗号分隔；留空处理配置里全部目标")
    parser.add_argument("--version", default="", help="版本号，如 1.2.3；留空使用 UTC 时间戳")
    parser.add_argument("--dry-run", action="store_true", help="只打包到本地 dist/，不创建 Release")
    parser.add_argument("--force", action="store_true", help="标签已存在时更新 Release 并覆盖同名资产")
    parser.add_argument("--config", default="", help="配置路径，默认 release-targets.json")
    parser.add_argument("--json", default="release.json", help="结果 JSON 输出路径")
    args = parser.parse_args()

    global CONFIG_PATH
    if args.config:
        CONFIG_PATH = Path(args.config).resolve()
    config = load_config()

    version = args.version.strip().lstrip("vV")
    if not version:
        version = datetime.now(timezone.utc).strftime("%Y%m%d.%H%M")
    if not re.fullmatch(r"[0-9A-Za-z][0-9A-Za-z.\-+_]*", version):
        raise SystemExit("版本号包含非法字符: %r" % version)

    token = "" if args.dry_run else get_token()
    gh = GitHub(token) if not args.dry_run else None
    owner = get_login(gh, token, config)
    if not owner:
        raise SystemExit("无法确定仓库 owner，请在配置中设置 owner")

    only = {name.strip() for name in args.only.split(",") if name.strip()}
    targets = [t for t in config["targets"] if t.get("repo")]
    if only:
        targets = [t for t in targets if t["repo"] in only]
        missing = only - {t["repo"] for t in targets}
        if missing:
            warn("配置中没有这些目标: %s" % ", ".join(sorted(missing)))
    if not targets:
        raise SystemExit("没有匹配到任何发布目标")

    tag_pattern = str(config.get("tag_pattern") or "{repo}-v{version}")
    run_url = "%s/%s/actions/runs/%s" % (
        os.environ.get("GITHUB_SERVER_URL", "https://github.com"),
        os.environ.get("GITHUB_REPOSITORY", ""),
        os.environ.get("GITHUB_RUN_ID", ""),
    ) if os.environ.get("GITHUB_RUN_ID") else ""

    results = []
    for target in targets:
        repo = str(target["repo"])
        record = {"repo": repo, "version": version, "kind": "", "tag": "",
                  "assets": [], "status": "success", "url": "", "error": ""}
        try:
            branch = str(target.get("branch") or config.get("branch") or "main")
            with tempfile.TemporaryDirectory(prefix="fork-hub-src-") as tmp:
                root = download_snapshot(owner, repo, branch, token, Path(tmp))
                assets, kind, domain = build_package(root, target, config, version, repo)
                record["kind"] = kind
                record["domain"] = domain
                record["assets"] = [item.name for item in assets]
                record["tag"] = tag_pattern.format(repo=repo, version=version, domain=domain, kind=kind)
                if args.dry_run:
                    log("%s: dry-run 完成，未写入 Release" % repo)
                else:
                    body = release_body(repo, kind, version, record["tag"], assets, run_url)
                    status, release = ensure_release(
                        gh, owner, repo, record["tag"], branch, body, args.force)
                    if status not in (200, 201) or not isinstance(release, dict) or not release.get("id"):
                        raise RuntimeError("创建 Release 失败 HTTP %s: %s" % (status, release))
                    record["assets"] = upload_assets(token, owner, repo, release["id"], assets)
                    record["url"] = str(release.get("html_url") or "")
                    log("%s: 已发布 %s（%d 个资产）" % (repo, record["tag"], len(record["assets"])))
        except Exception as error:  # noqa: BLE001
            record["status"] = "failed"
            record["error"] = str(error)
            warn("%s: %s" % (repo, error))
        results.append(record)

    payload = {
        "releases": results,
        "version": version,
        "dry_run": bool(args.dry_run),
        "owner": owner,
        "generated_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
    }
    output = Path(args.json)
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")

    successful = sum(1 for item in results if item["status"] == "success")
    failed = len(results) - successful
    log("完成：成功 %d，失败 %d，结果 -> %s" % (successful, failed, output))
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
