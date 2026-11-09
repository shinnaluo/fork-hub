#!/usr/bin/env python3
"""把检查结果 / 更新结果写成 GitHub 上一个自动维护的「Fork 同步状态」Issue。

只使用 Actions 自动提供的 GITHUB_TOKEN（权限仅限本仓库），
不接触任何 fork —— 跨仓库的读写全部交给 FORK_SYNC_PAT。

两种模式（环境变量 MODE）：
  plan   —— check-forks.yml 调用，读 dry-run 的 plan.json，列出待更新清单
  result —— apply-forks.yml 调用，读 result.json，回写「改动是否保留、
            备份分支、复核落后数、冲突与可直接复制的解决命令」

Issue 正文分成两个带标记的块，谁运行谁只替换自己的块，另一块原样保留，
因此手动触发 apply 之后清单不会停留在旧的「待更新」状态。
"""
import json
import os
import re
import urllib.error
import urllib.request
from datetime import datetime

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
PLAN = os.environ.get("PLAN_PATH", "plan.json")
MODE = os.environ.get("MODE", "plan").strip().lower()
LABEL = "fork-sync"
TITLE = "Fork 待更新清单（自动维护）"
ALERT_TITLE = "⚠️ Fork 同步需人工处理（%d 个冲突）"

BEGIN_PLAN = "<!-- BEGIN FORK_HUB_PLAN -->"
END_PLAN = "<!-- END FORK_HUB_PLAN -->"
BEGIN_LAST = "<!-- BEGIN FORK_HUB_LAST_RUN -->"
END_LAST = "<!-- END FORK_HUB_LAST_RUN -->"
CONFLICT_RE = re.compile(r"<!-- FORK_HUB_CONFLICTS=(\d+) -->")

HEADER = ("# Fork 同步状态\n\n"
          "由 `check-forks.yml` / `apply-forks.yml` 自动维护，请勿手动编辑。\n")


def call(method, path, payload=None):
    data = json.dumps(payload).encode() if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + (TOKEN or ""),
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json",
                 "User-Agent": "fork-hub"})
    try:
        with urllib.request.urlopen(req, timeout=40) as r:
            body = r.read().decode()
            return r.status, (json.loads(body) if body else {})
    except urllib.error.HTTPError as e:
        body = e.read().decode()
        try:
            body = json.loads(body)
        except Exception:
            pass
        return e.code, body


def stamp():
    return datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")


def block(begin, end, text):
    return begin + "\n" + text.strip() + "\n" + end


def extract_block(body, begin, end):
    """取出旧正文中某个标记块的内容（不含标记本身）。"""
    if begin in body and end in body:
        tail = body.split(begin, 1)[1]
        if end in tail:
            return tail.split(end, 1)[0].strip()
    return ""


def load_plan():
    """返回 (数据, 错误文本)。文件缺失时也要能写出可读的 Issue，不能让 job 静默。"""
    if not os.path.exists(PLAN):
        return None, "未找到 %s：工作流可能在上一步失败，请查看 Actions 日志。" % PLAN
    try:
        with open(PLAN, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not isinstance(d.get("forks"), list):
            return None, "%s 格式不正确（缺少 forks 数组）。" % PLAN
        return d, ""
    except Exception as e:
        return None, "读取 %s 失败：%s" % (PLAN, e)


def fmt(v):
    return "—" if v is None else str(v)


def pending_records(forks, mode):
    """plan 模式按分类统计；result 模式剔除本次已成功更新的，避免清单与结果互相矛盾。"""
    if mode == "plan":
        return [r for r in forks if r.get("category") in
                ("fast_forward", "diverged", "upstream_rewritten")]
    out = []
    for r in forks:
        if r.get("conflict") or r.get("category") in (
                "fast_forward", "diverged", "upstream_rewritten"):
            done = r.get("result", "").startswith(("已更新", "已按 upstream", "已对齐"))
            if done and not r.get("conflict"):
                continue
            out.append(r)
    return out


def render_plan(d, unresolved, todo, source):
    forks = d.get("forks", [])
    lines = [
        "## 当前检查结果",
        "",
        "%s：%s ｜ fork 总数：**%d** ｜ 待更新：**%d** ｜ 未解决冲突：**%d**" % (
            source, stamp(), len(forks), len(todo), unresolved),
        "",
    ]
    if unresolved:
        lines += [
            "> ⚠️ 上次更新有 **%d** 个仓库发生内容冲突，fork 保持原样、备份分支已保留你的改动。" % unresolved,
            "> 请展开下方「上次更新结果」里的解决步骤人工处理后，再触发一次更新。",
            "",
        ]
    if todo:
        lines += [
            "| 仓库 | 上游 | 上游新增 | 自有提交 | 结论 | 风险提示 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in sorted(todo, key=lambda x: x.get("name", "").lower()):
            kind = ("可快进（无自有改动）" if r.get("category") == "fast_forward"
                    else "备份后对齐上游（放弃被上游剔除的旧提交）"
                    if r.get("category") == "upstream_rewritten"
                    else "保留我的改动后合并")
            risk = ("双方改过 %d 个同名文件" % len(r["risk_files"])) \
                if r.get("risk_files") else "—"
            lines.append("| [%s](https://github.com/%s) | %s | %s | %s | %s | %s |" % (
                r.get("name", ""), r.get("fork", ""), r.get("upstream", ""),
                fmt(r.get("behind_by")), fmt(r.get("ahead_by")), kind, risk))
        lines += [
            "",
            "执行更新：",
            "- 网页：Actions → `apply fork updates` → Run workflow",
            "- 本地：`python dispatch.py apply`",
            "",
            "无自有改动 → 直接快进；有自有改动 → 先建备份分支再合并，提交完整保留。",
            "真冲突时安全停下（fork 不动），解决命令会写进下方「上次更新结果」与本轮 artifact。",
        ]
    else:
        lines.append(
            "当前没有待更新的 fork：所有仓库已与上游一致，或仅含你自己的改动而上游无新提交。")
    return "\n".join(lines)


def render_result(d, err):
    if err:
        return "\n".join([
            "## 上次更新结果",
            "",
            "执行时间：%s" % stamp(),
            "",
            "> ⚠️ 本次没有拿到结果文件：%s" % err,
            "",
            "<!-- FORK_HUB_CONFLICTS=0 -->",
        ])
    forks = d.get("forks", [])
    acted = [r for r in forks if r.get("result")]
    updated = [r for r in acted if r["result"].startswith(
        ("已更新", "已按 upstream", "已对齐"))]
    ff = [r for r in updated if r.get("category") == "fast_forward"]
    merged = [r for r in updated if r.get("category") == "diverged"]
    aligned = [r for r in updated if r.get("category") == "upstream_rewritten"]
    conflicts = [r for r in forks if r.get("conflict")]
    skipped = [r for r in acted if "跳过" in r["result"]]
    failed = [r for r in acted if not r.get("conflict")
              and ("失败" in r["result"] or "需人工" in r["result"])]

    lines = [
        "## 上次更新结果",
        "",
        "执行时间：%s ｜ 冲突策略：`%s`" % (stamp(), d.get("on_conflict") or "keep"),
        "",
        "已更新 **%d**（直接快进 %d / 保留改动后合并 %d / 对齐上游 %d）｜ 冲突 **%d** ｜ "
        "跳过 **%d** ｜ 其他失败 **%d**" % (
            len(updated), len(ff), len(merged), len(aligned),
            len(conflicts), len(skipped), len(failed)),
        "",
    ]
    if acted:
        lines += [
            "| 仓库 | 处理方式 | 结果 | 备份分支 | 复核落后上游 | 自有提交 |",
            "| --- | --- | --- | --- | --- | --- |",
        ]
        for r in sorted(acted, key=lambda x: x.get("name", "").lower()):
            kind = ("直接快进" if r.get("category") == "fast_forward"
                    else "对齐上游（放弃废弃提交）"
                    if r.get("category") == "upstream_rewritten"
                    else "保留改动后合并")
            behind = r.get("behind_after")
            verify = ("✅ 0" if behind == 0 else "⚠️ %s" % fmt(behind))
            backup = ("`%s`" % r["backup_ref"]) if r.get("backup_ref") else "—"
            lines.append("| [%s](https://github.com/%s) | %s | %s | %s | %s | %s |" % (
                r.get("name", ""), r.get("fork", ""), kind, r["result"],
                backup, verify, fmt(r.get("ahead_after"))))
        lines.append("")
        lines.append("「复核落后上游」为 0 表示已追平；「自有提交」不为 0 表示你的改动仍在。")
        lines.append("")
    else:
        lines += ["本次没有需要更新的仓库（无写操作）。", ""]

    if conflicts:
        lines += [
            "> ⚠️ 下面 %d 个仓库发生内容冲突：fork 保持更新前状态，**没有丢任何提交**，" % len(conflicts),
            "> 你的改动都在备份分支里。请任选一种方式处理，完成后再跑一次检查。",
            "",
        ]
        for r in conflicts:
            guide = r.get("conflict_guide") or "（本次未生成指引，请查看 artifact 中的 conflict-guide.md）"
            lines += [
                "<details><summary><strong>%s · 解决步骤（可直接复制）</strong></summary>" % r.get("name", ""),
                "",
                guide,
                "",
                "</details>",
                "",
            ]
        lines += ["完整命令同时保存在本轮 run 的 artifact `conflict-guide.md`（保留 30 天）。", ""]
    lines.append("<!-- FORK_HUB_CONFLICTS=%d -->" % len(conflicts))
    return "\n".join(lines)


def build_body(old, mode, d, err, todo):
    unresolved = 0
    m = CONFLICT_RE.search(old or "")
    if m:
        unresolved = int(m.group(1))
    if mode == "result":
        last_text = render_result(d or {}, err)
        # 本次结果覆盖旧的冲突计数
        mm = CONFLICT_RE.search(last_text)
        unresolved = int(mm.group(1)) if mm else 0
        plan_text = render_plan(d or {}, unresolved, todo, "根据上次执行结果推算") \
            if not err else extract_block(old or "", BEGIN_PLAN, END_PLAN) \
            or "尚未生成检查结果。"
    else:
        plan_text = render_plan(d or {}, unresolved, todo, "检查时间") if not err \
            else "\n".join(["## 当前检查结果", "", "> ⚠️ %s" % err, ""])
        last_text = extract_block(old or "", BEGIN_LAST, END_LAST) or \
            "尚未执行过更新；触发 `apply fork updates` 后这里会显示结果与备份分支。"
    return (HEADER + "\n" + block(BEGIN_PLAN, END_PLAN, plan_text)
            + "\n\n" + block(BEGIN_LAST, END_LAST, last_text) + "\n"), unresolved


def main():
    if not (TOKEN and REPO):
        raise SystemExit("缺少 GH_TOKEN / GITHUB_REPOSITORY 环境变量")
    if MODE not in ("plan", "result"):
        raise SystemExit("MODE 只能是 plan 或 result，当前为 %r" % MODE)

    d, err = load_plan()
    forks = (d or {}).get("forks", [])
    todo = pending_records(forks, MODE)

    st, _ = call("POST", "/repos/%s/labels" % REPO,
                 {"name": LABEL, "color": "0E8A16", "description": "fork sync plan"})
    if st not in (200, 201, 422):
        print("警告：标签创建返回 HTTP", st)

    st, issues = call("GET", "/repos/%s/issues?state=all&labels=%s&per_page=100" % (REPO, LABEL))
    existing = [i for i in issues if isinstance(i, dict) and not i.get("pull_request")] \
        if isinstance(issues, list) else []
    # 优先复用仍打开的那个；没有就复用最近关闭的那个
    existing.sort(key=lambda i: (i.get("state") != "open", -int(i.get("number") or 0)))
    cur = existing[0] if existing else None
    old_body = (cur or {}).get("body") or ""
    body, unresolved = build_body(old_body, MODE, d, err, todo)

    need_open = bool(todo) or unresolved > 0 or (MODE == "result" and err)
    title = ALERT_TITLE % unresolved if unresolved else TITLE

    if not need_open:
        # 即使没有待办，也要刷新正文里的「本次检查时间」，否则关闭后的 Issue
        # 会永远停在最后一次有待更新时的状态，看不出今天到底自检过没有。
        # PATCH body 不会重开 Issue；通知由 notify.py 的评论单独负责。
        if cur:
            if body.strip() == old_body.strip():
                print("无待更新项，Issue #%d 正文已是最新，无需写入" % cur["number"])
                return
            st, _ = call("PATCH", "/repos/%s/issues/%d" % (REPO, cur["number"]),
                         {"state": "closed", "title": title, "body": body})
            print("无待更新项，已刷新 Issue #%d 正文并保持关闭，HTTP %s" % (cur["number"], st))
        else:
            print("无待更新项，无需操作")
        return

    if cur:
        payload = {"title": title, "body": body, "state": "open"}
        st, _ = call("PATCH", "/repos/%s/issues/%d" % (REPO, cur["number"]), payload)
        print("已更新 Issue #%d，HTTP %s（模式 %s，未解决冲突 %d）" % (
            cur["number"], st, MODE, unresolved))
    else:
        st, _ = call("POST", "/repos/%s/issues" % REPO,
                     {"title": title, "body": body, "labels": [LABEL]})
        print("已创建状态 Issue，HTTP %s（模式 %s，未解决冲突 %d）" % (st, MODE, unresolved))


if __name__ == "__main__":
    main()
