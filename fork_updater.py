#!/usr/bin/env python3
"""GitHub fork 一键检查与更新工具（仅用 Python 标准库）。

功能：
  1. 自动发现当前 GitHub 账号下的所有 fork 及其上游仓库（无需手写清单）；
  2. 比对每个 fork 默认分支与上游默认分支，得出 ahead / behind；
  3. 给出「该如何更新」的计划，默认只预览，不写任何东西；
  4. --apply 按同样的规则执行：
       - 无自有提交 + 上游有更新  -> 直接快进（merge-upstream）
       - 有自有提交 + 上游有更新  -> 先建备份分支，再合并上游，保留你的改动
       - 上游重写历史，fork 独有提交全是被上游放弃的旧提交 -> 备份后对齐上游
       - 上游无更新 / 已同步      -> 完全不动
     默认除「对齐上游」外永不使用 force push，永不删除分支或提交。
  5. 冲突处理（见下方「关于冲突」）：预检风险文件、409 冲突时安全停下并给出
     可复制的解决命令；--on-conflict 可选 keep / skip / upstream 三种策略。
  6. 生成 HTML 报告、冲突处置指引与可选 JSON 结果。

关于上游重写历史：
  fork 独有提交不再一律当成「我的改动」。脚本会逐个比对提交作者与上游仓库
  最近 100 个提交的作者身份（login / author email / committer email），并把
  GitHub 自动生成的 Sync fork 合并提交识别为同步噪声：
    - 独有提交全部来自上游作者或是同步噪声 -> 判定「上游已放弃」，备份后对齐上游；
    - 只要有一个提交不是上游作者写的（你本人、第三方贡献者、本地提交）->
      一律视为你的功能，按原有规则保留；混合情况不会自动强推，会在报告里提示。
  这样上游 force push / rebase / 回退掉的旧提交不会在 fork 里变成永久累赘，
  而你真正改过的功能永远不会被丢掉。备份分支始终保留，可随时恢复。

关于冲突：
  GitHub 的 merge-upstream 在内容冲突时返回 409 且不会改动 fork，因此脚本永远
  不会把你的提交冲掉。但「哪一行该保留」只有你能决定 —— 任何工具都无法自动
  猜测业务意图，所以脚本选择：停下 + 备份 + 报告 + 给出解决步骤，而不是蛮干。
  实测案例：HA-Housekeeper 双方改过 8 个同名文件，服务端仍自动合并成功；
  所以「改了同一个文件」不等于冲突，预检结果只是风险提示，不是判定。

凭据：优先读环境变量 GITHUB_TOKEN / GH_TOKEN；否则通过 git credential fill
     从本机 Git Credential Manager 读取，脚本不保存、不打印令牌。

用法：
  python fork_updater.py                      # 检查并生成报告（dry-run）
  python fork_updater.py --apply             # 列出计划，确认后执行更新
  python fork_updater.py --apply --yes       # 无人值守执行（适合定时任务）
  python fork_updater.py --only BiliPai,ha-lixiang
  python fork_updater.py --exclude BiliPai
  python fork_updater.py --json plan.json --no-report
  python fork_updater.py --apply --on-conflict skip      # 预检有风险则不动，等人工处理
  python fork_updater.py --apply --on-conflict upstream  # 危险：备份后强制对齐上游，丢弃自有改动
  python fork_updater.py --on-rewrite keep              # 兼容旧行为：不清理被上游放弃的历史

--on-rewrite 策略（默认 align）：
  align    上游重写历史且 fork 独有提交不是你的功能时，先备份再把分支对齐上游，
           同步放弃那些已被上游剔除的旧提交。交互模式下会额外确认一次。
  keep     保留旧行为：任何 fork 独有提交都当作需要保留，不去对齐。

--on-conflict 策略：
  keep     （默认）先备份，再尝试合并；409 冲突时 fork 保持原样，输出解决指引。
  skip     预检发现双方改过同名文件时直接跳过该仓库，完全不写入。
  upstream 仅在 409 冲突时生效：在已建备份分支的前提下，把分支强制对齐到上游，
           彻底丢弃自有提交（可从备份分支恢复）。必须显式指定，定时任务慎用。
"""

import argparse
import concurrent.futures
import html
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime

API = "https://api.github.com"
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
DEFAULT_REPORT = os.path.join(BASE_DIR, "fork-report.html")
TIMEOUT = 45
RETRIES = 2

# 分类
IN_SYNC = "in_sync"          # 与上游完全一致
LOCAL_ONLY = "local_only"    # 仅有自己的提交，上游没有更新
FAST_FORWARD = "fast_forward"  # 无自有提交，上游有更新 -> 直接快进
DIVERGED = "diverged"        # 双方都有提交 -> 备份后合并
REWRITE = "upstream_rewritten"  # 上游重写历史，fork 独有提交全是被上游放弃的旧提交
ARCHIVED = "archived"        # fork 已归档，只读
UNKNOWN = "unknown"          # 上游不可访问 / 比对失败

# 遇到内容冲突时的策略
CONFLICT_KEEP = "keep"        # 默认：停下、备份、给指引
CONFLICT_SKIP = "skip"        # 预检有风险就跳过
CONFLICT_UPSTREAM = "upstream"  # 显式授权：备份后强制对齐上游
CONFLICT_POLICIES = (CONFLICT_KEEP, CONFLICT_SKIP, CONFLICT_UPSTREAM)
DEFAULT_GUIDE = os.path.join(BASE_DIR, "conflict-guide.md")

# 上游历史被重写时的策略
REWRITE_ALIGN = "align"       # 默认：备份后对齐上游，放弃被上游剔除的旧提交
REWRITE_KEEP = "keep"         # 兼容旧行为：一律保留
REWRITE_POLICIES = (REWRITE_ALIGN, REWRITE_KEEP)

# 这些身份是 GitHub 平台自己，不能用来判定「上游作者」
GENERIC_EMAILS = {"noreply@github.com", "noreply@users.noreply.github.com",
                  "web-flow@users.noreply.github.com", ""}
SYNC_MERGE_RE = re.compile(
    r"^Merge (branch|pull request|remote-tracking branch|upstream)\b", re.I)

ORDER = {REWRITE: -1, DIVERGED: 0, FAST_FORWARD: 1, UNKNOWN: 2,
         LOCAL_ONLY: 3, ARCHIVED: 4, IN_SYNC: 5}
LABEL = {
    IN_SYNC: "已同步",
    LOCAL_ONLY: "仅你有改动",
    FAST_FORWARD: "上游有更新 · 可快进",
    DIVERGED: "双方均有改动 · 需保留后合并",
    REWRITE: "上游已重写历史 · 需对齐",
    ARCHIVED: "已归档 · 只读",
    UNKNOWN: "无法比对",
}
ORIGIN_LABEL = {
    "discarded_upstream": "上游作者提交（已被上游放弃）",
    "discarded_sync": "GitHub 自动同步合并提交",
    "mine": "你的功能提交（保留）",
}

ACTION = {
    IN_SYNC: "无需操作",
    LOCAL_ONLY: "上游无新提交，你的改动原样保留，不动",
    FAST_FORWARD: "直接快进到上游最新提交",
    DIVERGED: "先备份你的分支，再合并上游；能自动合并就保留全部自有改动，真冲突则安全停下并给解决指引",
    REWRITE: "先建备份分支，再对齐上游：同步放弃被上游剔除的旧提交（不含你的功能改动）",
    ARCHIVED: "仓库已归档，跳过更新",
    UNKNOWN: "需人工检查上游是否改名、删除或转为私有",
}


def get_token():
    for key in ("GITHUB_TOKEN", "GH_TOKEN"):
        if os.environ.get(key):
            return os.environ[key]
    env = {**os.environ, "GIT_TERMINAL_PROMPT": "0", "GCM_INTERACTIVE": "Never"}
    try:
        p = subprocess.run(
            ["git", "credential", "fill"],
            input="protocol=https\nhost=github.com\n\n",
            capture_output=True, text=True, timeout=60, env=env,
        )
        if p.returncode == 0:
            fields = dict(x.split("=", 1) for x in p.stdout.strip().splitlines() if "=" in x)
            if fields.get("password"):
                return fields["password"]
    except Exception as e:
        print("读取本机 Git 凭据失败:", e, file=sys.stderr)
    raise SystemExit(
        "未找到 GitHub 凭据。请设置环境变量 GITHUB_TOKEN（需要 repo 权限），\n"
        "或先在 Git Credential Manager 中登录 github.com。"
    )


class GitHub:
    def __init__(self, token):
        self.token = token

    def request(self, method, path, payload=None, retries=RETRIES):
        url = API + path
        data = json.dumps(payload).encode() if payload is not None else None
        last = None
        for attempt in range(retries + 1):
            req = urllib.request.Request(
                url, data=data, method=method,
                headers={
                    "Authorization": "Bearer " + self.token,
                    "Accept": "application/vnd.github+json",
                    "Content-Type": "application/json",
                    "User-Agent": "fork-updater",
                },
            )
            try:
                with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                    body = r.read().decode()
                    return r.status, (json.loads(body) if body else {})
            except urllib.error.HTTPError as e:
                body = e.read().decode()
                try:
                    body = json.loads(body)
                except Exception:
                    pass
                return e.code, body
            except Exception as e:
                last = e
                if attempt < retries:
                    time.sleep(1.5 * (attempt + 1))
        return -1, {"error": str(last)}

    def get(self, path):
        return self.request("GET", path)


def discover_forks(gh):
    st, user = gh.get("/user")
    if st != 200:
        raise SystemExit("无法获取 GitHub 账号信息: %s %s" % (st, user))
    login = user["login"]
    repos, page = [], 1
    while True:
        # 注意：GitHub 不允许同时传 type 与 affiliation，这里用 affiliation=owner
        st, d = gh.get("/user/repos?per_page=100&page=%d&affiliation=owner" % page)
        if st != 200 or not isinstance(d, list):
            raise SystemExit("无法列出仓库: %s %s" % (st, d))
        repos.extend(d)
        if len(d) < 100:
            break
        page += 1
    forks = [r for r in repos if r.get("fork")]
    return login, forks


def compare_path(parent, parent_branch, login, fork_branch):
    base = urllib.parse.quote(parent_branch, safe="")
    head = urllib.parse.quote(login + ":" + fork_branch, safe=":")
    # per_page=1 只限制 commits 列表长度，status/ahead_by/behind_by 仍是完整统计
    return "/repos/%s/compare/%s...%s?per_page=1" % (parent, base, head)


def analyze(gh, login, fork):
    full = fork["full_name"]
    rec = {
        "fork": full, "name": fork["name"], "branch": fork.get("default_branch") or "main",
        "upstream": "", "upstream_branch": "", "ahead_by": 0, "behind_by": 0,
        "category": UNKNOWN, "error": "", "archived": bool(fork.get("archived")),
        "head_before": "", "head_after": "", "result": "", "backup_ref": "",
        "behind_after": None, "ahead_after": None, "extra_branches": [],
        "merge_base": "", "risk_files": [], "risk_checked": False, "risk_error": "",
        "upstream_sha": "", "conflict": False, "conflict_message": "",
        "conflict_guide": "",
        "ownership_checked": False, "ownership_error": "", "commit_origins": [],
        "mine_commits": 0, "discarded_commits": 0,
    }
    st, detail = gh.get("/repos/" + full)
    if st != 200:
        rec["error"] = "repo detail HTTP %s" % st
        return rec
    parent = detail.get("parent") or {}
    rec["upstream"] = parent.get("full_name", "")
    rec["upstream_branch"] = parent.get("default_branch") or rec["branch"]
    rec["branch"] = detail.get("default_branch") or rec["branch"]
    rec["archived"] = bool(detail.get("archived"))

    if not rec["upstream"]:
        rec["error"] = "找不到上游仓库（可能已被删除或转为私有）"
        return rec
    if rec["archived"]:
        rec["category"] = ARCHIVED
        return rec

    st, cmp = gh.get(compare_path(rec["upstream"], rec["upstream_branch"], login, rec["branch"]))
    if st != 200:
        rec["error"] = "compare HTTP %s: %s" % (st, (cmp or {}).get("message", ""))
        return rec
    rec["ahead_by"] = cmp.get("ahead_by") or 0
    rec["behind_by"] = cmp.get("behind_by") or 0
    rec["head_before"] = ((cmp.get("head_commit") or {}).get("sha") or "")
    rec["merge_base"] = ((cmp.get("merge_base_commit") or {}).get("sha") or "")
    status = cmp.get("status")
    if status == "identical":
        rec["category"] = IN_SYNC
    elif status == "ahead":
        rec["category"] = LOCAL_ONLY
    elif status == "behind":
        rec["category"] = FAST_FORWARD
    elif status == "diverged":
        rec["category"] = DIVERGED
    else:
        rec["error"] = "未知 compare 状态: %s" % status
    return rec


def current_head(gh, rec):
    st, d = gh.get("/repos/%s/git/ref/heads/%s" % (
        rec["fork"], urllib.parse.quote(rec["branch"], safe="")))
    if st == 200:
        return (d.get("object") or {}).get("sha", "")
    return ""


def verify_after(gh, login, rec):
    """更新后复核：落后上游数应归零，对齐后不应再有任何独有提交。"""
    rec["head_after"] = current_head(gh, rec)
    st, cmp = gh.get(compare_path(rec["upstream"], rec["upstream_branch"],
                                  login, rec["branch"]))
    if st != 200:
        return
    behind = cmp.get("behind_by") or 0
    rec["behind_after"] = behind
    rec["ahead_after"] = cmp.get("ahead_by") or 0
    if rec["result"].startswith(("已更新", "已按 upstream", "已对齐")):
        rec["result"] += " · 复核落后 %d" % behind
    if rec["category"] == REWRITE and rec["ahead_after"]:
        rec["result"] += " · 注意仍有 %d 个独有提交" % rec["ahead_after"]


def apply_updates(gh, login, records, stamp, policy=CONFLICT_KEEP,
                  rewrite_policy=REWRITE_ALIGN):
    todo = [r for r in records if r["category"] in (FAST_FORWARD, DIVERGED, REWRITE)]
    for rec in todo:
        repo = rec["fork"]
        branch = rec["branch"]

        # 上游重写历史：独有提交里没有你的功能，备份后对齐上游，同步放弃废弃提交
        if rec["category"] == REWRITE:
            if rewrite_policy == REWRITE_KEEP:
                rec["result"] = "按 --on-rewrite keep 保留历史，未写入"
                print("  [保留] %s -> %s" % (rec["name"], rec["result"]))
                continue
            sha = current_head(gh, rec) or rec["head_before"]
            up_sha = rec["upstream_sha"] or upstream_head(gh, rec)
            if not (sha and up_sha):
                rec["result"] = "中止：无法读取 fork / 上游 HEAD，未写入"
                continue
            backup = "backup/%s-before-rewrite-%s" % (branch, stamp)
            st, d = gh.request("POST", "/repos/%s/git/refs" % repo,
                               {"ref": "refs/heads/" + backup, "sha": sha})
            if st not in (200, 201):
                rec["result"] = "中止：备份分支创建失败（HTTP %s %s），未写入" % (
                    st, (d or {}).get("message", ""))
                continue
            rec["backup_ref"] = backup
            print("  [备份] %s -> %s (%s)" % (rec["name"], backup, sha[:8]))
            st2, d2 = gh.request("PATCH", "/repos/%s/git/refs/heads/%s" % (
                repo, urllib.parse.quote(branch, safe="")),
                {"sha": up_sha, "force": True})
            if st2 == 200:
                rec["result"] = ("已对齐上游：放弃 %d 个被上游剔除的提交；备份 %s 可恢复"
                                 % (rec["discarded_commits"], backup))
            else:
                rec["result"] = ("失败：对齐上游 HTTP %s %s（fork 未改动，备份 %s）"
                                 % (st2, (d2 or {}).get("message", ""), backup))
            print("  [对齐] %s -> %s" % (rec["name"], rec["result"]))
            verify_after(gh, login, rec)
            print("     -> %s" % rec["result"])
            continue

        # skip 策略：预检发现双方改过同名文件时完全不写入，等人工决定
        if policy == CONFLICT_SKIP and rec["category"] == DIVERGED and rec["risk_files"]:
            rec["result"] = ("已跳过：预检发现双方改过 %d 个同名文件，按 skip 策略留待人工处理"
                             % len(rec["risk_files"]))
            print("  [跳过] %s -> %s" % (rec["name"], rec["result"]))
            continue

        if rec["category"] == DIVERGED:
            backup = "backup/%s-before-upstream-%s" % (branch, stamp)
            sha = current_head(gh, rec) or rec["head_before"]
            if not sha:
                rec["result"] = "跳过：无法读取当前 HEAD"
                continue
            st, d = gh.request("POST", "/repos/%s/git/refs" % repo,
                               {"ref": "refs/heads/" + backup, "sha": sha})
            if st not in (200, 201):
                rec["result"] = "中止：备份分支创建失败（HTTP %s %s）" % (
                    st, (d or {}).get("message", ""))
                continue
            rec["backup_ref"] = backup
            print("  [备份] %s -> %s (%s)" % (rec["name"], backup, sha[:8]))

        st, d = gh.request("POST", "/repos/%s/merge-upstream" % repo, {"branch": branch})
        print("  [同步] %s merge-upstream -> HTTP %s %s" % (
            rec["name"], st, (d or {}).get("merge_type", (d or {}).get("message", ""))))

        if st == 200:
            rec["result"] = "已更新（%s）" % (d.get("merge_type") or "merge")
        elif st in (409, 422) and rec["category"] == FAST_FORWARD:
            # 快进型仓库的兜底：直接把分支引用更新到上游 SHA（非强制，GitHub 只允许快进）
            st2, d2 = gh.request("PATCH", "/repos/%s/git/refs/heads/%s" % (
                repo, urllib.parse.quote(branch, safe="")),
                {"sha": upstream_head(gh, rec), "force": False})
            rec["result"] = ("已更新（引用快进）" if st2 == 200
                             else "失败：引用更新 HTTP %s %s" % (st2, (d2 or {}).get("message", "")))
        elif st == 409:
            rec["conflict_message"] = (d or {}).get("message", "")
            up_sha = rec["upstream_sha"] or upstream_head(gh, rec)
            if policy == CONFLICT_UPSTREAM and rec["backup_ref"] and up_sha:
                # 唯一会写入 force 的地方：显式授权 + 已备份 + 确有冲突
                st2, d2 = gh.request("PATCH", "/repos/%s/git/refs/heads/%s" % (
                    repo, urllib.parse.quote(branch, safe="")),
                    {"sha": up_sha, "force": True})
                if st2 == 200:
                    rec["result"] = ("已按 upstream 策略强制对齐上游：丢弃 %d 个自有提交；"
                                     "备份 %s 可恢复" % (rec["ahead_by"], rec["backup_ref"]))
                else:
                    rec["conflict"] = True
                    rec["conflict_guide"] = build_conflict_guide(rec)
                    rec["result"] = ("失败：强制对齐 HTTP %s %s（fork 未改动，需人工处理）"
                                     % (st2, (d2 or {}).get("message", "")))
                print("  [强制对齐] %s -> %s" % (rec["name"], rec["result"]))
            else:
                rec["conflict"] = True
                rec["conflict_guide"] = build_conflict_guide(rec)
                rec["result"] = ("合并冲突，需人工处理（fork 保持原样；备份 %s 已保留你的改动）"
                                 % (rec["backup_ref"] or "无"))
                print("  [冲突] %s 已安全停止，处置步骤已写入报告与指引文件" % rec["name"])
        elif st == 422:
            rec["result"] = ("失败：上游同步被拒绝（HTTP 422 %s），常见于分支保护或冲突，fork 未改动"
                             % (d or {}).get("message", ""))
        else:
            rec["result"] = "失败：HTTP %s %s" % (st, (d or {}).get("message", ""))

        verify_after(gh, login, rec)
        print("     -> %s" % rec["result"])
    return todo


def upstream_head(gh, rec):
    st, d = gh.get("/repos/%s/git/ref/heads/%s" % (
        rec["upstream"], urllib.parse.quote(rec["upstream_branch"], safe="")))
    if st == 200:
        return (d.get("object") or {}).get("sha", "")
    return ""


def full_compare_path(rec, login):
    """不带 per_page=1，用于拿到 fork 独有提交的完整列表。"""
    base = urllib.parse.quote(rec["upstream_branch"], safe="")
    head = urllib.parse.quote(login + ":" + rec["branch"], safe=":")
    return "/repos/%s/compare/%s...%s?per_page=100" % (rec["upstream"], base, head)


def upstream_author_set(gh, rec, login):
    """上游默认分支最近 100 个提交的作者身份；读不到就返回 None（保守保留）。"""
    st, d = gh.get("/repos/%s/commits?sha=%s&per_page=100" % (
        rec["upstream"], urllib.parse.quote(rec["upstream_branch"], safe="")))
    if st != 200 or not isinstance(d, list) or not d:
        return None
    ids = set()
    for c in d:
        a = c.get("author") or {}
        if a.get("login"):
            ids.add("login:" + a["login"].lower())
        for g in ((c.get("commit") or {}).get("author") or {},
                  (c.get("commit") or {}).get("committer") or {}):
            e = (g.get("email") or "").strip().lower()
            if e not in GENERIC_EMAILS:
                ids.add("email:" + e)
    # 本账号身份绝不能被当作「上游作者」，否则会把自己的改动误判成可丢弃
    ids.discard("login:" + login.lower())
    return ids


def classify_commit_origin(c, upstream_ids):
    """判定 fork 独有提交能不能被放弃。

    只有两类允许放弃：GitHub 自动生成的同步合并提交，以及能确认由上游作者
    写的提交。其余（本人、第三方贡献者、本地提交、身份无法识别）一律视为
    需要保留的 fork 自有功能 —— 宁可多留，绝不误删。
    """
    commit = c.get("commit") or {}
    msg = (commit.get("message") or "").splitlines()
    first = (msg[0] if msg else "").strip()
    ga = commit.get("author") or {}
    gc = commit.get("committer") or {}
    parents = c.get("parents") or []
    if (len(parents) >= 2
            and (gc.get("email") or "").strip().lower() in
            ("noreply@github.com", "web-flow@users.noreply.github.com")
            and SYNC_MERGE_RE.match(first)):
        return "discarded_sync"
    ids = set()
    a = c.get("author") or {}
    if a.get("login"):
        ids.add("login:" + a["login"].lower())
    for g in (ga, gc):
        e = (g.get("email") or "").strip().lower()
        if e not in GENERIC_EMAILS:
            ids.add("email:" + e)
    if ids & upstream_ids:
        return "discarded_upstream"
    return "mine"


def analyze_ownership(gh, login, records):
    """对所有 ahead>0 的仓库判断 fork 独有提交来源。纯只读，失败一律保守保留。"""
    targets = [r for r in records
               if r["ahead_by"] and r["category"] in (LOCAL_ONLY, DIVERGED)]
    if not targets:
        return

    def work(rec):
        st, cmp = gh.get(full_compare_path(rec, login))
        if st != 200:
            rec["ownership_error"] = "compare HTTP %s，保守保留" % st
            return rec
        commits = cmp.get("commits") or []
        if len(commits) < (rec["ahead_by"] or 0):
            rec["ownership_error"] = "独有提交超过 compare 上限，保守保留"
            return rec
        ids = upstream_author_set(gh, rec, login)
        if ids is None:
            rec["ownership_error"] = "无法读取上游提交作者，保守保留"
            return rec
        origins = []
        for c in commits:
            text = ((c.get("commit") or {}).get("message") or "").splitlines()
            origins.append({
                "sha": (c.get("sha") or "")[:10],
                "origin": classify_commit_origin(c, ids),
                "message": (text[0] if text else "")[:80],
            })
        rec["commit_origins"] = origins
        rec["mine_commits"] = sum(1 for x in origins if x["origin"] == "mine")
        rec["discarded_commits"] = sum(1 for x in origins
                                       if x["origin"].startswith("discarded"))
        rec["ownership_checked"] = True
        # 只有「一个需保留的提交都没有」时才允许对齐；混合情况绝不自动强推
        if rec["mine_commits"] == 0 and rec["discarded_commits"] > 0:
            rec["category"] = REWRITE
        return rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, targets))


def changed_file_set(gh, repo, base, head):
    """返回 compare 中的改动文件名集合；失败返回 None（区别于空集合）。"""
    st, d = gh.get("/repos/%s/compare/%s...%s?per_page=1" % (repo, base, head))
    if st != 200:
        return None
    return {f.get("filename") for f in (d or {}).get("files", []) if f.get("filename")}


def precheck_conflicts(gh, records):
    """对分叉仓库做「风险预检」：双方自 merge_base 起改动过的同名文件交集。

    重要：文件重叠不等于冲突（同文件不同行可自动合并），删除/重命名/二进制文件
    也可能漏报，且 compare 最多返回 300 个文件。这里只作提示，绝不用于判定。
    """
    targets = [r for r in records if r["category"] == DIVERGED]
    if not targets:
        return

    def work(rec):
        base = rec["merge_base"]
        head = rec["head_before"] or current_head(gh, rec)
        up = upstream_head(gh, rec)
        rec["upstream_sha"] = up
        if not (base and head and up):
            rec["risk_error"] = "缺少 merge_base / HEAD，无法预检"
            return rec
        a = changed_file_set(gh, rec["upstream"], base, up)
        b = changed_file_set(gh, rec["fork"], base, head)
        if a is None or b is None:
            rec["risk_error"] = "compare 不可用，无法预检"
            return rec
        rec["risk_checked"] = True
        rec["risk_files"] = sorted(a & b)
        return rec

    with concurrent.futures.ThreadPoolExecutor(max_workers=6) as ex:
        list(ex.map(work, targets))


def build_conflict_guide(rec):
    """生成针对该仓库、可直接复制执行的人工解决步骤。"""
    fork, name = rec["fork"], rec["name"]
    branch, up, up_branch = rec["branch"], rec["upstream"], rec["upstream_branch"]
    login, up_owner = fork.split("/")[0], up.split("/")[0]
    backup = rec["backup_ref"] or "(本次未创建备份分支，请先手动创建)"
    head = rec["head_before"][:10] or "HEAD"
    compare_url = "https://github.com/%s/compare/%s...%s:%s" % (
        fork, urllib.parse.quote(branch, safe=""), up_owner,
        urllib.parse.quote(up_branch, safe=""))
    return "\n".join([
        "## %s" % name,
        "",
        "- 你的 fork：%s（分支 %s，更新前 HEAD %s）" % (fork, branch, head),
        "- 上游：%s（分支 %s）" % (up, up_branch),
        "- 备份分支：%s ← 你的改动完整保存在这里，随时可恢复" % backup,
        "- GitHub 返回 409 merge conflict；fork 保持更新前状态，没有丢任何提交。",
        "",
        "### 方式 A：网页上把上游合进你的分支（不用装 git）",
        "打开：" + compare_url,
        "base 选 %s:%s，head 选 %s:%s；页面会列出冲突文件，简单冲突可直接在浏览器里编辑后提交。" % (
            login, branch, up_owner, up_branch),
        "若页面不允许从上游分支开 PR，改用方式 B。",
        "",
        "### 方式 B：本地命令行解决（复杂冲突用这个）",
        "```bash",
        "git clone https://github.com/%s.git" % fork,
        "cd %s" % name,
        "git remote add upstream https://github.com/%s.git" % up,
        "git fetch --no-tags upstream %s" % up_branch,
        "git checkout %s" % branch,
        "git merge upstream/%s        # 这一步会列出 CONFLICT 文件" % up_branch,
        "# 编辑冲突文件：保留你要的内容，删掉 <<<<<<< / ======= / >>>>>>> 标记",
        "git status                   # 确认没有 Unmerged paths",
        "git add -A",
        'git commit -m "merge upstream, keep my changes"',
        "git push origin %s" % branch,
        "```",
        "",
        "### 反悔 / 回退",
        "```bash",
        "git merge --abort                         # 放弃本次本地合并",
        "git reset --hard %s                      # 回到更新前" % head,
        "git checkout -b restore origin/%s        # 从备份分支恢复" % backup,
        "```",
        "",
        "### 若决定不要自己的改动、完全跟随上游（危险：丢弃 %d 个自有提交）" % rec["ahead_by"],
        "```bash",
        "git fetch --no-tags upstream %s" % up_branch,
        "git reset --hard upstream/%s" % up_branch,
        "git push --force-with-lease origin %s    # 仅在确认备份分支已存在后执行" % branch,
        "```",
        "或让脚本代做（同样只在已有备份时执行）：",
        "python fork_updater.py --apply --on-conflict upstream --only %s" % name,
        "",
        "处理完再跑一次 python fork_updater.py 复核：落后数应归零，自有提交仍在。",
        "",
    ])


def write_conflict_guide(records, path):
    """仅在本次运行真的遇到冲突时写出指引文件。"""
    conflicts = [r for r in records if r["conflict"]]
    if not conflicts:
        return ""
    parts = [
        "# Fork 合并冲突处置指引",
        "",
        "生成于 %s（本机时间）。本次有 %d 个仓库无法自动合并。" % (
            datetime.now().strftime("%Y-%m-%d %H:%M"), len(conflicts)),
        "脚本已停止写入，fork 保持更新前状态，你的改动都在备份分支里。",
        "",
        "> 内容冲突无法由工具自动决定该保留哪一行，请按下面步骤人工处理后，再跑一次检查。",
        "",
    ]
    for r in conflicts:
        parts.append(r["conflict_guide"])
    with open(path, "w", encoding="utf-8") as f:
        f.write("\n".join(parts))
    return path


def print_plan(records, applied):
    groups = {}
    for r in records:
        groups.setdefault(r["category"], []).append(r)
    title = "执行结果" if applied else "检查计划（未做任何改动）"
    print("\n=== %s ===" % title)
    for cat in (REWRITE, DIVERGED, FAST_FORWARD, UNKNOWN, LOCAL_ONLY, ARCHIVED, IN_SYNC):
        items = groups.get(cat, [])
        if not items:
            continue
        print("\n[%s] %d 个" % (LABEL[cat], len(items)))
        for r in items:
            extra = ""
            if r["error"]:
                extra = " | " + r["error"]
            if r["result"]:
                extra = " | " + r["result"]
            elif r["category"] == REWRITE:
                extra = " | 独有提交 %d 个均来自上游或同步噪声，无你的功能改动" % r["discarded_commits"]
            elif r["risk_files"]:
                extra = " | 提示：双方改过 %d 个同名文件（不等于冲突）" % len(r["risk_files"])
            elif r["discarded_commits"] and r["mine_commits"]:
                extra = " | 含 %d 个可放弃的同步/废弃提交，因有你的功能改动故保守保留" % r["discarded_commits"]
            print("  - %-42s 上游 %-40s 落后 %-4s 自有 %-4s  %s%s" % (
                r["name"], r["upstream"] or "-", r["behind_by"], r["ahead_by"],
                ACTION[cat], extra))
    n_ff = len(groups.get(FAST_FORWARD, []))
    n_dv = len(groups.get(DIVERGED, []))
    n_rw = len(groups.get(REWRITE, []))
    print("\n共 %d 个 fork；可直接快进 %d 个，需保留改动后合并 %d 个，"
          "需对齐上游 %d 个，其余 %d 个无需操作。" % (
              len(records), n_ff, n_dv, n_rw, len(records) - n_ff - n_dv - n_rw))
    if not applied and (n_ff or n_dv or n_rw):
        print("执行更新：python fork_updater.py --apply")
    if n_dv:
        print("分叉仓库若真发生内容冲突，默认安全停下并生成冲突处置指引；"
              "可用 --on-conflict skip / upstream 改变策略。")


CSS = """
:root{--bg:#f5f6f8;--card:#fff;--text:#1c2026;--muted:#6b7280;--line:#e6e8ec;
--green:#0f7b4f;--greenbg:#e7f5ee;--blue:#1d4ed8;--bluebg:#e8effd;
--grey:#5b6472;--greybg:#eef0f3;--amber:#a15c00;--amberbg:#fff2dd;--red:#b42318;--redbg:#fdecea}
*{box-sizing:border-box}
body{margin:0;padding:36px 20px 60px;background:var(--bg);color:var(--text);
font:14px/1.6 -apple-system,"Segoe UI","Microsoft YaHei",sans-serif}
.wrap{max-width:1140px;margin:0 auto}
h1{font-size:26px;margin:0 0 6px;letter-spacing:-.4px}
.meta{color:var(--muted);font-size:13px;margin-bottom:24px}
.cards{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-bottom:24px}
.card{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}
.card .n{font-size:27px;font-weight:650;line-height:1.2}.card .l{color:var(--muted);font-size:12px;margin-top:4px}
.card.ff .n{color:var(--blue)}.card.dv .n{color:var(--amber)}.card.ok .n{color:var(--green)}
section{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:20px 22px;margin-bottom:20px}
h2{font-size:16px;margin:0 0 12px}
table{width:100%;border-collapse:collapse;font-size:13px}
th{text-align:left;color:var(--muted);font-weight:500;font-size:12px;padding:9px 10px;border-bottom:1px solid var(--line);white-space:nowrap}
td{padding:11px 10px;border-bottom:1px solid #f0f1f4;vertical-align:top}
tr:last-child td{border-bottom:none}
a{color:var(--blue);text-decoration:none;font-weight:550}a:hover{text-decoration:underline}
a.up{font-weight:400;color:#4b5563;font-size:12px}
.sub{color:var(--muted);font-size:11px;margin-top:3px;line-height:1.5}
.num{font-variant-numeric:tabular-nums;color:#374151}
.tag,.res{display:inline-block;padding:2px 9px;border-radius:20px;font-size:11px;white-space:nowrap}
.t-diverged{background:var(--amberbg);color:var(--amber)}
.t-fast_forward{background:var(--bluebg);color:var(--blue)}
.t-local_only,.t-in_sync,.t-archived{background:var(--greybg);color:var(--grey)}
.t-upstream_rewritten{background:var(--redbg);color:var(--red)}
.t-unknown{background:var(--redbg);color:var(--red)}
.res{background:var(--greenbg);color:var(--green);font-weight:600}
.res.bad{background:var(--redbg);color:var(--red)}
code{background:#f1f2f5;padding:1px 6px;border-radius:5px;font-size:12px}
ul{margin:6px 0 0;padding-left:20px}li{margin-bottom:5px;color:#39414d}
.foot{color:var(--muted);font-size:12px;margin-top:14px}
.row-diverged{background:#fffaf2}.row-fast_forward{background:#f7faff}
.row-upstream_rewritten{background:#fff5f3}
h3{font-size:13px;margin:18px 0 8px;color:#39414d}
pre{background:#f6f7f9;border:1px solid var(--line);border-radius:8px;padding:12px 14px;
overflow-x:auto;font:12px/1.6 Consolas,"Courier New",monospace;color:#26303d;white-space:pre}
details{margin:10px 0;border:1px solid var(--line);border-radius:8px;padding:10px 14px;background:#fff}
summary{cursor:pointer;font-weight:600;font-size:13px}
.risk{color:var(--amber);font-size:11px;margin-top:3px}
.warn{background:var(--amberbg);border-left:3px solid var(--amber);padding:10px 14px;
border-radius:6px;color:#7a4700;margin-bottom:12px}
.policy{background:var(--bluebg);color:var(--blue);padding:1px 6px;border-radius:4px;font-weight:600}
"""


def render_html(records, login, applied, path):
    esc = html.escape
    groups = {}
    for r in records:
        groups.setdefault(r["category"], []).append(r)
    ordered = sorted(records, key=lambda r: (ORDER[r["category"]], r["name"].lower()))
    tr = []
    for r in ordered:
        note = ACTION[r["category"]]
        if r["error"]:
            note = esc(r["error"])
        result = ""
        if r["result"]:
            bad = r["result"].startswith(("失败", "中止", "合并冲突", "跳过"))
            result = '<span class="res%s">%s</span>' % (" bad" if bad else "", esc(r["result"]))
        if r["backup_ref"]:
            result += '<div class="sub">备份 %s</div>' % esc(r["backup_ref"])
        if r["category"] == REWRITE:
            result += ('<div class="risk">独有 %d 个提交都不是你的功能；将备份后对齐上游</div>'
                       % r["discarded_commits"])
        if r["risk_files"]:
            result += ('<div class="risk">提示：双方改过 %d 个同名文件（不等于冲突）</div>'
                       % len(r["risk_files"]))
        tr.append(
            '<tr class="row-%s"><td><a href="https://github.com/%s" target="_blank">%s</a>'
            '<div class="sub">分支 %s</div></td>'
            '<td><a class="up" href="https://github.com/%s" target="_blank">%s</a></td>'
            '<td class="num">+%s</td><td class="num">+%s</td>'
            '<td><span class="tag t-%s">%s</span></td><td>%s<div class="sub">%s</div></td></tr>' % (
                esc(r["category"]), esc(r["fork"]), esc(r["name"]), esc(r["branch"]),
                esc(r["upstream"]) if r["upstream"] else "", esc(r["upstream"]) or "-",
                r["behind_by"], r["ahead_by"], esc(r["category"]), esc(LABEL[r["category"]]),
                esc(note), result))
    conflicts = [r for r in ordered if r["conflict"]]
    if conflicts:
        guides = "".join(
            '<details open><summary>%s · 解决步骤（可直接复制）</summary><pre>%s</pre></details>'
            % (esc(r["name"]), esc(r["conflict_guide"])) for r in conflicts)
    else:
        guides = '<p class="sub">本次运行没有需要人工处理的冲突。</p>'
    risks = [r for r in ordered if r["risk_files"]]
    if risks:
        risk_items = "".join(
            '<li><strong>%s</strong>：双方改过 %d 个同名文件 — %s</li>' % (
                esc(r["name"]), len(r["risk_files"]),
                esc(", ".join(r["risk_files"][:10]) +
                    (" 等" if len(r["risk_files"]) > 10 else "")))
            for r in risks)
        risk_html = '<ul>%s</ul>' % risk_items
    else:
        risk_html = '<p class="sub">本次没有分叉仓库，或预检未发现双方改过同名文件。</p>'
    conflict_html = """<section><h2>遇到冲突怎么办 / 本次风险预检</h2>
<div class="warn">内容冲突无法由脚本自动决定「保留哪一行」——这是 Git 的本质限制。
脚本的做法是：先备份 → 尝试服务端合并 → GitHub 返回 409 时立刻停下
（fork 保持原样，不会丢任何提交）→ 给出可直接复制的解决步骤。</div>
<ul>
<li><span class="policy">keep（默认）</span>：真冲突时 fork 不动，备份分支保存你的全部改动，报告给出网页 PR 与本地 git 两套解决命令；反悔、回退、放弃自有改动三种出口都写清楚。</li>
<li><span class="policy">skip</span>：<code>--on-conflict skip</code>。预检发现双方改过同名文件就跳过该仓库，完全不写入，适合无人值守时避免反复失败。</li>
<li><span class="policy">upstream</span>：<code>--on-conflict upstream</code>。仅在已建备份且确有冲突时，把分支强制对齐到上游，<strong>会丢弃全部自有提交</strong>（可从备份分支恢复）。危险，必须显式指定。</li>
<li>改过同一个文件 ≠ 冲突：不同行、不同区块通常能自动合并；预检还受 GitHub compare 最多 300 个文件的限制，所以只是提示，不是判定。</li>
<li>默认路径永不使用 force push、不删除分支或提交；仅 <code>upstream</code> 策略会强制更新分支引用。</li>
</ul>
<h3>本次冲突仓库与处置指引</h3>%s
<h3>风险预检（双方改动文件的交集）</h3>%s
</section>""" % (guides, risk_html)

    rewrite_items = []
    for r in [x for x in ordered if x["category"] == REWRITE]:
        bad = [x for x in r["commit_origins"] if x["origin"].startswith("discarded")]
        lis = "".join('<li><code>%s</code> %s · %s</li>' % (
            esc(x["sha"]), esc(ORIGIN_LABEL.get(x["origin"], x["origin"])),
            esc(x["message"])) for x in bad[:12])
        rewrite_items.append(
            '<li><strong>%s</strong>：%d 个可放弃提交、0 个你的功能提交，'
            '将备份后对齐 <code>%s:%s</code><ul>%s</ul></li>' % (
                esc(r["name"]), len(bad), esc(r["upstream"]),
                esc(r["upstream_branch"]), lis))
    mixed = [r for r in ordered if r["ownership_checked"]
             and r["category"] != REWRITE and r["discarded_commits"]
             and r["mine_commits"]]
    mixed_items = "".join(
        '<li><strong>%s</strong>：%d 个你的功能提交 + %d 个可放弃的同步/废弃提交。'
        '为不丢功能，本次不会自动强推；若要清理历史可本地执行 '
        '<code>git rebase --onto upstream/%s %s %s</code> 后 '
        '<code>git push --force-with-lease origin %s</code>。</li>' % (
            esc(r["name"]), r["mine_commits"], r["discarded_commits"],
            esc(r["upstream_branch"]), esc(r["merge_base"][:10] or "<共同祖先>"),
            esc(r["branch"]), esc(r["branch"])) for r in mixed)
    rewrite_html = """<section><h2>上游历史重写 · 提交归属判定</h2>
<div class="warn">fork 独有提交不再一律当成「我的改动」。脚本把上游作者写的提交、
以及 GitHub 自动生成的同步合并提交识别为「上游已放弃」；只要有一个提交不是上游作者写的
（你本人、第三方贡献者、本地提交），就一律当作你的功能保留。上游 force push / 回退掉的
旧提交因此不会永久堆在 fork 里，而你的功能绝不会被丢掉。</div>
<ul>
<li><span class="policy">align（默认）</span>：独有提交全部可放弃时，先建 <code>backup/&lt;分支&gt;-before-rewrite-&lt;时间戳&gt;</code> 备份分支，再把分支对齐到上游 HEAD；可从备份恢复。</li>
<li><span class="policy">keep</span>：<code>--on-rewrite keep</code>，兼容旧行为，不去对齐。</li>
<li>混合情况（既有你的功能又有废弃提交）永不自动强推，只在下方给出人工 rebase 命令。</li>
<li>身份判定基于上游默认分支最近 100 个提交的 login / author email / committer email；读不到上游历史时一律保守保留。</li>
</ul>
<h3>本次将对齐上游的仓库</h3>%s
<h3>混合情况（保守保留）</h3>%s
</section>""" % (
        "<ul>%s</ul>" % "".join(rewrite_items) if rewrite_items
        else '<p class="sub">本次没有需要对齐的仓库。</p>',
        "<ul>%s</ul>" % mixed_items if mixed_items
        else '<p class="sub">本次没有混合情况。</p>')

    cards = [
        ("", len(records), "fork 总数"),
        ("ff", len(groups.get(FAST_FORWARD, [])), "可直接快进"),
        ("dv", len(groups.get(DIVERGED, [])), "需保留改动后合并"),
        ("", len(groups.get(REWRITE, [])), "需对齐上游"),
        ("", len(groups.get(LOCAL_ONLY, [])) + len(groups.get(IN_SYNC, [])), "无需操作"),
        ("ok", sum(1 for r in records if r["result"].startswith(("已更新", "已按 upstream"))), "本次已更新"),
    ]
    card_html = "".join('<div class="card %s"><div class="n">%d</div><div class="l">%s</div></div>'
                        % (c, n, l) for c, n, l in cards)
    mode = "已执行更新" if applied else "仅检查 / 预览（未写入任何仓库）"
    now = datetime.now().strftime("%Y-%m-%d %H:%M")
    doc = """<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>GitHub Fork 检查与更新报告 · %s</title><style>%s</style></head><body><div class="wrap">
<h1>GitHub Fork 检查与更新报告</h1>
<div class="meta">账号 <strong>%s</strong> · %s · 生成于 %s（本机时间）· 比对各仓库默认分支与其上游默认分支</div>
<div class="cards">%s</div>
<section><h2>更新规则</h2><ul>
<li><strong>无自有提交、上游有更新</strong>：直接快进（GitHub Sync fork / merge-upstream），无需人工介入。</li>
<li><strong>有自有提交、上游也有更新</strong>：先创建 <code>backup/&lt;分支&gt;-before-upstream-&lt;时间戳&gt;</code> 备份分支，再合并上游生成合并提交，你的提交完整保留；若服务端合并冲突，fork 保持原样，保留备份并给出解决步骤。</li>
<li><strong>上游重写了历史</strong>：若 fork 独有提交全部是上游作者写的旧提交或 GitHub 自动同步合并提交，先备份再对齐上游，同步放弃这些已被上游剔除的提交；只要含你的功能提交就绝不自动强推。</li>
<li><strong>上游无更新或已同步</strong>：完全不动。已归档仓库跳过。</li>
<li>默认路径不使用 force push（仅 <code>--on-conflict upstream</code> 与「对齐上游」例外），不删除任何分支或提交；凭据仅在运行时从环境变量或 Git Credential Manager 读取。</li>
</ul></section>
%s
%s
<section><h2>逐仓库明细</h2><table><thead><tr><th>你的仓库</th><th>上游</th><th>上游新增</th><th>自有提交</th><th>结论</th><th>建议动作 / 结果</th></tr></thead>
<tbody>%s</tbody></table>
<p class="foot">「上游新增」「自有提交」为报告生成时的数值；执行更新后会复核落后数是否归零。</p></section>
<section><h2>命令行用法</h2><ul>
<li><code>python fork_updater.py</code> —— 只检查并生成本报告（默认）</li>
<li><code>python fork_updater.py --apply</code> —— 列出计划、确认后执行</li>
<li><code>python fork_updater.py --apply --yes</code> —— 无人值守执行（适合计划任务）</li>
<li><code>--only A,B</code> / <code>--exclude A,B</code> —— 限定或排除仓库；<code>--json plan.json</code> 导出机器可读结果</li>
<li><code>--on-conflict keep|skip|upstream</code> —— 冲突策略，默认 <code>keep</code>；发生冲突时会另外生成 <code>conflict-guide.md</code></li>
<li><code>--on-rewrite align|keep</code> —— 上游重写历史时的策略，默认 <code>align</code>（备份后对齐上游）</li>
</ul></section></div></body></html>""" % (
        esc(login), CSS, esc(login), esc(mode), now, card_html, conflict_html,
        rewrite_html, "\n".join(tr))
    with open(path, "w", encoding="utf-8") as f:
        f.write(doc)
    return path


def main():
    try:
        sys.stdout.reconfigure(encoding="utf-8")
    except Exception:
        pass
    ap = argparse.ArgumentParser(description="自动检查并更新 GitHub 账号下的所有 fork")
    ap.add_argument("--apply", action="store_true", help="实际执行更新（默认只预览）")
    ap.add_argument("--yes", action="store_true", help="跳过交互确认，配合 --apply 使用")
    ap.add_argument("--only", default="", help="只处理这些仓库名，逗号分隔")
    ap.add_argument("--exclude", default="", help="排除这些仓库名，逗号分隔")
    ap.add_argument("--report", default=DEFAULT_REPORT, help="HTML 报告输出路径")
    ap.add_argument("--no-report", action="store_true", help="不生成 HTML 报告")
    ap.add_argument("--json", default="", help="额外导出 JSON 结果到指定路径")
    ap.add_argument("--workers", type=int, default=6, help="并发请求数（默认 6）")
    ap.add_argument("--on-conflict", choices=CONFLICT_POLICIES, default=CONFLICT_KEEP,
                    help="冲突策略：keep 停下给指引（默认）/ skip 预检有风险就跳过 / "
                         "upstream 备份后强制对齐上游（丢弃自有改动，危险）")
    ap.add_argument("--no-precheck", action="store_true",
                    help="关闭分叉仓库的冲突风险预检（可少发几次 API 请求）")
    ap.add_argument("--on-rewrite", choices=REWRITE_POLICIES, default=REWRITE_ALIGN,
                    help="上游重写历史时的策略：align 备份后对齐上游、放弃被上游剔除的旧提交"
                         "（默认）/ keep 一律保留旧历史")
    ap.add_argument("--no-ownership", action="store_true",
                    help="关闭 fork 独有提交的来源分析（可少发 API 请求，此时不会做对齐）")
    ap.add_argument("--guide", default=DEFAULT_GUIDE, help="冲突处置指引输出路径")
    args = ap.parse_args()

    gh = GitHub(get_token())
    login, forks = discover_forks(gh)
    only = {x.strip().lower() for x in args.only.split(",") if x.strip()}
    exclude = {x.strip().lower() for x in args.exclude.split(",") if x.strip()}
    forks = [f for f in forks
             if (not only or f["name"].lower() in only) and f["name"].lower() not in exclude]
    print("账号 %s：发现 %d 个 fork，正在比对上游…" % (login, len(forks)))

    with concurrent.futures.ThreadPoolExecutor(max_workers=args.workers) as ex:
        records = list(ex.map(lambda f: analyze(gh, login, f), forks))
    records.sort(key=lambda r: (ORDER[r["category"]], r["name"].lower()))

    n_ahead = sum(1 for r in records if r["ahead_by"]
                  and r["category"] in (LOCAL_ONLY, DIVERGED))
    if n_ahead and not args.no_ownership:
        print("发现 %d 个仓库有 fork 独有提交，正在判定提交来源…" % n_ahead)
        analyze_ownership(gh, login, records)

    n_dv_pre = sum(1 for r in records if r["category"] == DIVERGED)
    if n_dv_pre and not args.no_precheck:
        print("发现 %d 个分叉仓库，正在预检双方改动文件…" % n_dv_pre)
        precheck_conflicts(gh, records)

    n_ff = sum(1 for r in records if r["category"] == FAST_FORWARD)
    n_dv = sum(1 for r in records if r["category"] == DIVERGED)
    n_rw = sum(1 for r in records if r["category"] == REWRITE)
    print_plan(records, applied=False)

    n_rw_action = n_rw if args.on_rewrite == REWRITE_ALIGN else 0
    if args.apply and (n_ff or n_dv or n_rw_action):
        if not args.yes:
            print("\n即将更新 %d 个仓库（快进 %d / 保留改动后合并 %d / 对齐上游 %d），冲突策略：%s。" % (
                n_ff + n_dv + n_rw_action, n_ff, n_dv, n_rw_action, args.on_conflict))
            if n_rw_action:
                print("注意：其中 %d 个仓库将先建备份分支，再对齐上游并放弃被上游剔除的旧提交"
                      "（已确认不含你的功能改动）。" % n_rw_action)
            if args.on_conflict == CONFLICT_UPSTREAM:
                print("⚠ upstream 策略会在冲突时丢弃全部自有提交（备份分支可恢复），请确认已知晓。")
                answer = input('确认请输入 UPSTREAM（区分大小写），否则取消: ').strip()
                if answer != "UPSTREAM":
                    print("已取消，未做任何改动。")
                    args.apply = False
            else:
                answer = input("确认执行？输入 y 继续 [y/N]: ").strip().lower()
                if answer not in ("y", "yes"):
                    print("已取消，未做任何改动。")
                    args.apply = False
        if args.apply:
            stamp = datetime.now().strftime("%Y%m%d-%H%M")
            print("\n=== 开始更新 ===")
            apply_updates(gh, login, records, stamp, args.on_conflict, args.on_rewrite)
            print_plan(records, applied=True)
            guide = write_conflict_guide(records, args.guide)
            if guide:
                print("冲突处置指引：%s" % guide)
    elif args.apply:
        print("\n没有需要更新的项目，--apply 未执行任何写操作。")

    if not args.no_report:
        path = render_html(records, login, bool(args.apply and (n_ff or n_dv)), args.report)
        print("HTML 报告：%s" % path)
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"login": login, "generated_at": datetime.now().isoformat(),
                       "applied": bool(args.apply and (n_ff or n_dv or n_rw_action)),
                       "on_conflict": args.on_conflict,
                       "on_rewrite": args.on_rewrite, "forks": records},
                      f, ensure_ascii=False, indent=2)
        print("JSON 结果：%s" % args.json)


if __name__ == "__main__":
    main()
