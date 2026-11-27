#!/usr/bin/env python3
"""检查并同步发布目标清单：把账号下尚未加入的 fork 自动补进 release-targets.json。

设计要点：
  1. 只新增，不删除。原创仓库（如 ha-chuanglvjia）和手动加入的目标不会被移除；
     如需让某 fork 永不自动加入，写进配置的 ignore 列表。
  2. 类型自动识别：module.prop -> module；custom_components/<domain>/manifest.json
     -> ha-integration；其余 -> software。默认分支取 GitHub 上的实际值。
  3. 写回仓库用 GITHUB_TOKEN 走 Contents API —— 该令牌的 push 不会触发新的
     workflow run，避免「检查 -> 写清单 -> 又触发检查」的死循环。
  4. --dry-run 只报告与输出 JSON，不写本地文件、不提交，可离线验收。

用法：
  python sync_targets.py                         # 同步并提交（workflow 内）
  python sync_targets.py --dry-run               # 只检查，不写入
  python sync_targets.py --dry-run --json out.json
"""
from __future__ import annotations

import argparse
import base64
import json
import os
import sys
from datetime import datetime
from pathlib import Path

from fork_updater import GitHub, discover_forks, get_token

BASE_DIR = Path(__file__).resolve().parent
CONFIG_PATH = BASE_DIR / "release-targets.json"

KIND_HA = "ha-integration"
KIND_MODULE = "module"
KIND_SOFTWARE = "software"


def log(message: str) -> None:
    print("[sync-targets] " + message, flush=True)


def load_config(path: Path) -> dict:
    if not path.is_file():
        raise SystemExit("缺少 %s" % path)
    with path.open(encoding="utf-8") as handle:
        config = json.load(handle)
    if not isinstance(config.get("targets"), list):
        raise SystemExit("%s 的 targets 不是数组" % path)
    return config


def probe_kind(gh: GitHub, owner: str, repo: str, branch: str) -> tuple[str, str]:
    """用 Contents API 探测仓库类型，返回 (kind, path)。"""
    base = "/repos/%s/%s/contents" % (owner, repo)
    suffix = "?ref=" + branch

    status, _ = gh.get(base + "/module.prop" + suffix)
    if status == 200:
        return KIND_MODULE, "."

    status, entries = gh.get(base + "/custom_components" + suffix)
    if status == 200 and isinstance(entries, list):
        for item in entries:
            if item.get("type") != "dir":
                continue
            domain = str(item.get("name") or "")
            status, _ = gh.get(base + "/custom_components/" + domain + "/manifest.json" + suffix)
            if status == 200:
                return KIND_HA, "custom_components/" + domain

    return KIND_SOFTWARE, "."


def write_config_file(path: Path, config: dict) -> None:
    with path.open("w", encoding="utf-8") as handle:
        json.dump(config, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


def commit_config(gh: GitHub, owner: str, repo: str, branch: str,
                  path: str, content: str, message: str) -> tuple[int, dict]:
    """通过 Contents API 更新文件；需要 file sha。"""
    status, current = gh.get("/repos/%s/%s/contents/%s?ref=%s" % (owner, repo, path, branch))
    payload = {
        "message": message,
        "content": base64.b64encode(content.encode("utf-8")).decode("ascii"),
        "branch": branch,
    }
    if status == 200 and isinstance(current, dict) and current.get("sha"):
        payload["sha"] = current["sha"]
    return gh.request("PUT", "/repos/%s/%s/contents/%s" % (owner, repo, path), payload)


def main() -> int:
    parser = argparse.ArgumentParser(description="把新 fork 自动同步到发布目标清单")
    parser.add_argument("--dry-run", action="store_true", help="只检查并输出，不写文件、不提交")
    parser.add_argument("--config", default=str(CONFIG_PATH), help="清单路径")
    parser.add_argument("--json", default="", help="同步结果 JSON 输出路径")
    parser.add_argument("--no-commit", action="store_true", help="写本地文件但不提交（本地调试用）")
    args = parser.parse_args()

    config_path = Path(args.config).resolve()
    config = load_config(config_path)
    owner = str(config.get("owner") or "").strip()

    read_token = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN") or get_token()
    read_gh = GitHub(read_token)

    login, forks = discover_forks(read_gh)
    if not owner:
        owner = login
        config["owner"] = owner
    log("账号 %s：发现 %d 个 fork" % (login, len(forks)))

    existing = {str(t.get("repo") or "") for t in config["targets"] if t.get("repo")}
    ignored = {str(x).strip() for x in config.get("ignore", []) if str(x).strip()}
    stale = sorted(name for name in existing if name not in {f["name"] for f in forks})
    added = []
    skipped = []

    for fork in sorted(forks, key=lambda f: f["name"].lower()):
        name = str(fork["name"])
        if name in existing or name in ignored:
            continue
        branch = str(fork.get("default_branch") or config.get("branch") or "main")
        try:
            kind, path = probe_kind(read_gh, owner, name, branch)
        except Exception as error:  # noqa: BLE001
            skipped.append({"repo": name, "reason": "类型识别失败: %s" % error})
            log("%s: 类型识别失败，跳过（%s）" % (name, error))
            continue
        config["targets"].append({
            "repo": name, "kind": kind, "path": path, "branch": branch,
        })
        added.append({"repo": name, "kind": kind, "path": path, "branch": branch})
        log("%s: 加入清单（%s, %s, %s）" % (name, kind, path, branch))

    result = {
        "owner": owner,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "fork_count": len(forks),
        "target_count": len(config["targets"]),
        "added": added,
        "skipped": skipped,
        "stale": stale,
        "committed": False,
        "dry_run": bool(args.dry_run),
    }

    if added and not args.dry_run:
        write_config_file(config_path, config)
        commit_message = "chore: 同步新 fork 到发布目标清单（%s）" % ", ".join(x["repo"] for x in added)
        if args.no_commit:
            log("本地已更新清单，跳过提交（--no-commit）")
        else:
            write_token = os.environ.get("WRITE_TOKEN") or read_token
            write_gh = GitHub(write_token)
            host_repo = os.environ.get("GITHUB_REPOSITORY", "%s/fork-hub" % owner)
            host_owner, _, host_name = host_repo.partition("/")
            branch = os.environ.get("GITHUB_REF_NAME") or str(config.get("branch") or "main")
            rel_path = config_path.relative_to(BASE_DIR).as_posix()
            status, response = commit_config(
                write_gh, host_owner, host_name, branch, rel_path,
                config_path.read_text(encoding="utf-8"), commit_message)
            if status not in (200, 201):
                raise SystemExit("提交 %s 失败：HTTP %s %s" % (rel_path, status, response))
            result["committed"] = True
            log("已提交 %s（HTTP %s）" % (rel_path, status))
    elif not added:
        log("没有新 fork 需要同步，清单保持不变")
    else:
        log("dry-run：发现 %d 个待加入目标，未写入" % len(added))

    if stale:
        log("提示：清单中 %d 个目标已不是 fork（未自动移除）：%s" % (len(stale), ", ".join(stale)))

    if args.json:
        output = Path(args.json)
        output.parent.mkdir(parents=True, exist_ok=True)
        output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
        log("结果 JSON：%s" % output)

    return 0


if __name__ == "__main__":
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    raise SystemExit(main())
