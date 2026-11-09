#!/usr/bin/env python3
"""本地脚本只负责「按按钮」：触发 / 查看 hub 仓库里的工作流。

真正的检查与更新都在 GitHub Actions 云端执行；本文件不写入任何 fork。
凭据：GH_TOKEN / GITHUB_TOKEN 环境变量，或本机 Git Credential Manager。
仓库：--repo 或环境变量 FORK_HUB_REPO（默认 shinnaluo/fork-hub）。

用法：
  python dispatch.py check                     # 触发一次检查
  python dispatch.py check --auto-apply        # 检查后若有待更新项，自动触发更新
  python dispatch.py apply                    # 触发更新（默认 keep 策略）
  python dispatch.py apply --only BiliPai --on-conflict skip
  python dispatch.py apply --fail-on-conflict # 真冲突时让 run 标红并给通知
  python dispatch.py runs                     # 查看两个工作流最近的运行

更新结果（是否保留你的提交、备份分支、复核落后数、冲突解决命令）
会自动写回 hub 仓库的「Fork 同步状态」Issue，不必下载 artifact 就能看到。
"""
import argparse
import json
import os
import urllib.error
import urllib.request

from fork_updater import get_token

API = "https://api.github.com"
CHECK = "check-forks.yml"
APPLY = "apply-forks.yml"
DEFAULT_REPO = os.environ.get("FORK_HUB_REPO", "shinnaluo/fork-hub")


def call(token, method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + token,
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "fork-hub-dispatch"})
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body


def dispatch(token, repo, workflow, ref, inputs):
    path = "/repos/%s/actions/workflows/%s/dispatches" % (repo, workflow)
    st, d = call(token, "POST", path, {"ref": ref, "inputs": inputs})
    if st in (200, 204):
        url = (d or {}).get("html_url") or \
              "https://github.com/%s/actions/workflows/%s" % (repo, workflow)
        print("已触发 %s → %s" % (workflow, url))
        return 0
    print("触发失败 HTTP %s：%s" % (st, (d or {}).get("message", d)))
    if st == 403:
        print("提示：令牌需要 Actions(write) 权限；classic PAT 需要 repo scope。")
    return 1


def show_runs(token, repo):
    for wf in (CHECK, APPLY):
        path = "/repos/%s/actions/workflows/%s/runs?per_page=5" % (repo, wf)
        st, d = call(token, "GET", path)
        print("\n=== %s ===" % wf)
        if st != 200 or not isinstance(d, dict):
            print("  查询失败 HTTP", st)
            continue
        for r in d.get("workflow_runs", []):
            print("  #%-5s %-14s %-12s %s  %s" % (
                r.get("run_number"), str(r.get("status")), str(r.get("conclusion")),
                r.get("created_at"), (r.get("html_url") or "")))
        if not d.get("workflow_runs"):
            print("  暂无运行记录（工作流需至少成功运行过一次才能通过 API 触发）")
    return 0


def main():
    ap = argparse.ArgumentParser(description="触发 fork-hub 云端工作流（本地不写仓库）")
    ap.add_argument("command", choices=["check", "apply", "runs"])
    ap.add_argument("--repo", default=DEFAULT_REPO, help="hub 仓库 owner/name")
    ap.add_argument("--ref", default="main", help="工作流所在分支，默认 main")
    ap.add_argument("--on-conflict", choices=["keep", "skip", "upstream"], default="keep")
    ap.add_argument("--only", default="", help="逗号分隔的仓库名")
    ap.add_argument("--auto-apply", action="store_true",
                    help="仅 check 有效：有待更新项时自动触发 apply")
    ap.add_argument("--fail-on-conflict", action="store_true",
                    help="仅 apply 有效：真冲突时让 run 标红（方便收到通知）")
    args = ap.parse_args()

    token = get_token()
    if args.command == "runs":
        return show_runs(token, args.repo)
    if args.command == "check":
        return dispatch(token, args.repo, CHECK, args.ref, {
            "only": args.only,
            "auto_apply": "true" if args.auto_apply else "false",
        })
    if args.on_conflict == "upstream":
        print("警告：upstream 策略会在冲突时丢弃你的自有提交（备份分支可恢复）。")
    return dispatch(token, args.repo, APPLY, args.ref, {
        "on_conflict": args.on_conflict,
        "only": args.only,
        "fail_on_conflict": "true" if args.fail_on_conflict else "false",
    })


if __name__ == "__main__":
    raise SystemExit(main())
