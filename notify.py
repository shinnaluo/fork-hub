#!/usr/bin/env python3
"""每次自检 / 更新结束后，把结果主动推送给你。

设计原则：同步逻辑与通知解耦 —— 通知失败绝不影响 fork 同步结果。
只使用 Actions 自动提供的 GITHUB_TOKEN（权限仅限本仓库），不接触任何 fork。

三条输出通道：
  1. GitHub 状态 Issue 的新评论（由 github-actions[bot] 发出并 @mention 仓库 owner）
     -> 触发 GitHub 站内通知与邮件通知，同时留下一条按时间排列的自检日志。
  2. 可选的 NOTIFY_WEBHOOK（仓库 secret，一个 URL 即可），自动适配：
     Server酱 / Bark / 企业微信机器人 / 钉钉机器人 / 飞书机器人 / 通用 JSON。
     不配置就什么都不做，不影响其他通道。
  3. GITHUB_STEP_SUMMARY，Actions 页面上直接可见。

环境变量（由工作流注入）：
  MODE=check|apply|release  本次是自检、更新还是打包发布
  DATA_PATH=plan.json|result.json|release.json
  JOB_STATUS                ${{ job.status }}
  APPLY_DISPATCHED          check 是否已自动派发 apply（用于避免重复通知）
  NOTIFY_SCOPE=final|both   final（默认）：已派发 apply 时 check 不推，只推最终结果一条
                            both：check 与 apply 各推一条
  NOTIFY_LEVEL=all|changes  all（默认）：每次自检都推；changes：只在有变化/异常时推
  NOTIFY_MENTION            @谁，默认仓库 owner；填 none/false 关闭 @
  NOTIFY_WEBHOOK            可选，推送 URL（含密钥，脚本不会打印完整 URL）
"""
import json
import os
import re
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

API = "https://api.github.com"
TOKEN = os.environ.get("GH_TOKEN") or os.environ.get("GITHUB_TOKEN")
REPO = os.environ.get("GITHUB_REPOSITORY", "")
OWNER = REPO.split("/")[0] if "/" in REPO else ""
MODE = os.environ.get("MODE", "check").strip().lower()
DATA_PATH = os.environ.get("DATA_PATH", "plan.json")
JOB_STATUS = os.environ.get("JOB_STATUS", "unknown")
RUN_ID = os.environ.get("GITHUB_RUN_ID", "")
RUN_NUMBER = os.environ.get("GITHUB_RUN_NUMBER", "")
RUN_ATTEMPT = os.environ.get("GITHUB_RUN_ATTEMPT", "")
SERVER = os.environ.get("GITHUB_SERVER_URL", "https://github.com")
APPLY_DISPATCHED = os.environ.get("APPLY_DISPATCHED", "")
SCOPE = (os.environ.get("NOTIFY_SCOPE") or "final").strip().lower()
LEVEL = (os.environ.get("NOTIFY_LEVEL") or "all").strip().lower()
WEBHOOK = os.environ.get("NOTIFY_WEBHOOK", "").strip()
MENTION = os.environ.get("NOTIFY_MENTION", "").strip() or OWNER
LABEL = "fork-sync"

UPDATED_PREFIX = ("已更新", "已按 upstream", "已对齐")
PENDING_CATEGORIES = ("fast_forward", "diverged", "upstream_rewritten")


def api(method, path, payload=None):
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(
        API + path, data=data, method=method,
        headers={"Authorization": "Bearer " + (TOKEN or ""),
                 "Accept": "application/vnd.github+json",
                 "Content-Type": "application/json; charset=utf-8",
                 "User-Agent": "fork-hub-notify"})
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


def load_data():
    if not os.path.exists(DATA_PATH):
        return None, "未找到 %s（上游步骤可能失败，请查看 Actions 日志）" % DATA_PATH
    try:
        with open(DATA_PATH, encoding="utf-8") as f:
            d = json.load(f)
        if not isinstance(d, dict) or not (
                isinstance(d.get("forks"), list) or isinstance(d.get("releases"), list)):
            return None, "%s 格式不正确（缺少 forks / releases 数组）" % DATA_PATH
        return d, ""
    except Exception as e:
        return None, "读取 %s 失败：%s" % (DATA_PATH, e)


def beijing_time():
    return (datetime.now(timezone.utc) + timedelta(hours=8)).strftime("%Y-%m-%d %H:%M")


def run_url():
    return "%s/%s/actions/runs/%s" % (SERVER, REPO, RUN_ID) if RUN_ID else ""


def stats(d):
    data = d or {}
    releases = data.get("releases")
    if isinstance(releases, list):
        failed = [r for r in releases if r.get("status") != "success"]
        return {
            "mode": "release",
            "releases": releases,
            "failed": failed,
            "total": len(releases),
            "pending": [],
            "updated": [r for r in releases if r.get("status") == "success"],
            "conflicts": failed,
            "local_only": [],
            "in_sync": [],
            "risky": [],
        }
    forks = data.get("forks", [])
    return {
        "forks": forks,
        "total": len(forks),
        "pending": [r for r in forks if r.get("category") in PENDING_CATEGORIES],
        "updated": [r for r in forks if r.get("result", "").startswith(UPDATED_PREFIX)],
        "conflicts": [r for r in forks if r.get("conflict")],
        "local_only": [r for r in forks if r.get("category") == "local_only"],
        "in_sync": [r for r in forks if r.get("category") == "in_sync"],
        "risky": [r for r in forks if r.get("risk_files")],
    }


def build_release_message(d, err):
    """打包发布专用消息：只汇总 Release 结果，不套用 fork 同步的统计口径。"""
    data = d or {}
    rows = data.get("releases", [])
    success = sum(1 for r in rows if r.get("status") == "success")
    failed = len(rows) - success
    bad = bool(err) or JOB_STATUS != "success" or bool(failed)
    version = data.get("version", "")
    dry_run = bool(data.get("dry_run"))

    if bad:
        title = "Fork 打包发布异常：%d 个失败" % failed
        conclusion = "部分目标未发布成功，请以 Actions 日志与 release.json 为准。"
    else:
        title = "Fork 打包发布完成：%d 个成功" % success
        conclusion = ("dry-run 已完成，仅在 Artifact 里生成资产，未写入任何仓库。"
                      if dry_run else "Release 与资产已发布到对应 fork。")

    mention = "@%s " % MENTION if MENTION and MENTION.lower() not in ("none", "false", "0") else ""
    lines = [
        "### Fork 打包发布结果 · %s（北京时间）" % beijing_time(),
        "",
        "%s本次由 `release forks` 工作流推送。" % mention,
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        "| 运行状态 | %s |" % ("成功" if JOB_STATUS == "success" else JOB_STATUS),
        "| 目标数 | %d |" % len(rows),
        "| 成功发布 | %d |" % success,
        "| 失败 | %d |" % failed,
        "| 版本 | `%s` |" % version,
        "| 模式 | %s |" % ("dry-run" if dry_run else "正式发布"),
        "",
        "结论：%s" % conclusion,
        "",
    ]
    if err:
        lines += ["> 数据读取异常：%s" % err, ""]
    if rows:
        lines += ["| 仓库 | 类型 | 标签 | 资产 | 状态 |",
                  "| --- | --- | --- | --- | --- |"]
        for item in rows:
            lines.append("| `%s` | %s | `%s` | %s | %s |" % (
                item.get("repo", ""), item.get("kind", "-"),
                item.get("tag", "-") or "-",
                ", ".join(item.get("assets", [])) or "-",
                "成功" if item.get("status") == "success" else
                "失败：%s" % str(item.get("error", ""))[:120]))
        lines.append("")
    if run_url():
        lines += ["[查看本次运行日志](%s) ｜ [状态 Issue](%s/%s/issues?q=label%%3A%s)" % (
            run_url(), SERVER, REPO, LABEL), ""]
    lines += ["<sub>由 fork-hub 自动推送 · run #%s · attempt %s · 通知级别 %s</sub>" % (
        RUN_NUMBER or "?", RUN_ATTEMPT or "?", LEVEL)]
    markdown = "\n".join(lines)
    plain = re.sub(r"[|#*`\[\]<>]", "", markdown)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return title, markdown, plain


def build_message(mode, d, err, s):
    """返回 (标题, Markdown 正文, 纯文本正文)。"""
    if mode == "release":
        return build_release_message(d, err)
    bad = JOB_STATUS != "success" or bool(err)
    label = "自检" if mode == "check" else "更新"
    n_pending, n_updated, n_conflict = len(s["pending"]), len(s["updated"]), len(s["conflicts"])

    if bad:
        title = "Fork %s异常：需要查看运行日志" % label
        conclusion = "本次运行未成功完成，结果与日志以 Actions 页面为准；fork 未被强制改动。"
    elif mode == "apply":
        if n_conflict:
            title = "Fork 更新需人工处理：%d 个冲突，%d 个已更新" % (n_conflict, n_updated)
            conclusion = "冲突仓库保持更新前状态，自有提交都在备份分支里，解决步骤见状态 Issue。"
        else:
            title = "Fork 更新完成：已更新 %d 个，冲突 0" % n_updated
            conclusion = "已追平上游；有自有改动的仓库先建备份分支再合并，提交完整保留。"
    else:
        if n_pending:
            title = "Fork 自检完成：发现 %d 个待更新" % n_pending
            conclusion = ("已自动派发更新（全自动模式），最终结果由 apply 工作流再推一条。"
                          if APPLY_DISPATCHED else "待更新清单已写入状态 Issue，等待执行更新。")
        else:
            title = "Fork 自检完成：全部已同步（%d 个 fork）" % s["total"]
            conclusion = "所有仓库已与上游一致，或仅含你的改动而上游无新提交，本次无写操作。"

    # apply 的 result.json 里 category 仍是更新前的分类，直接显示「待更新 2」会让人
    # 误以为没更新完；这里显示真正还没处理完的数量。
    remain = [r for r in s["pending"]
              if not r.get("result", "").startswith(UPDATED_PREFIX) and not r.get("conflict")]
    pending_label = "待更新（本次检出）" if mode == "check" else "更新后仍待更新"
    pending_value = n_pending if mode == "check" else len(remain)

    mention = "@%s " % MENTION if MENTION and MENTION.lower() not in ("none", "false", "0") else ""
    lines = [
        "### Fork %s结果 · %s（北京时间）" % (label, beijing_time()),
        "",
        "%s本次由 `%s` 工作流推送。" % (mention, "check forks" if mode == "check" else "apply fork updates"),
        "",
        "| 项目 | 数值 |",
        "| --- | --- |",
        "| 运行状态 | %s |" % ("成功" if JOB_STATUS == "success" else JOB_STATUS),
        "| fork 总数 | %d |" % s["total"],
        "| %s | %d |" % (pending_label, pending_value),
        "| 本次已更新 | %d |" % n_updated,
        "| 需人工处理冲突 | %d |" % n_conflict,
        "| 仅含自有改动（未动） | %d |" % len(s["local_only"]),
        "| 已与上游一致 | %d |" % len(s["in_sync"]),
        "",
        "结论：%s" % conclusion,
        "",
    ]
    if err:
        lines += ["> 数据读取异常：%s" % err, ""]

    if mode == "apply" and s["updated"]:
        lines += ["**本次更新的仓库**", "",
                  "| 仓库 | 处理方式 | 备份分支 | 复核落后上游 | 自有提交 |",
                  "| --- | --- | --- | --- | --- |"]
        for r in sorted(s["updated"], key=lambda x: x.get("name", "").lower()):
            kind = ("直接快进" if r.get("category") == "fast_forward"
                    else "对齐上游（放弃废弃提交）"
                    if r.get("category") == "upstream_rewritten"
                    else "保留改动后合并")
            behind = r.get("behind_after")
            verify = "0（已追平）" if behind == 0 else str(behind)
            lines.append("| `%s` | %s | %s | %s | %s |" % (
                r.get("name", ""), kind, r.get("backup_ref") or "—",
                verify, r.get("ahead_after") if r.get("ahead_after") is not None else "—"))
        lines.append("")
    elif mode == "check" and s["pending"]:
        lines += ["**待更新清单**", "",
                  "| 仓库 | 上游新增 | 自有提交 | 处理方式 |",
                  "| --- | --- | --- | --- |"]
        for r in sorted(s["pending"], key=lambda x: x.get("name", "").lower()):
            kind = ("直接快进" if r.get("category") == "fast_forward"
                    else "备份后对齐上游" if r.get("category") == "upstream_rewritten"
                    else "先备份再合并")
            lines.append("| `%s` | %s | %s | %s |" % (
                r.get("name", ""), r.get("behind_by"), r.get("ahead_by"), kind))
        lines.append("")

    if mode == "check":
        sync_path = os.environ.get("SYNC_PATH", "targets-sync.json")
        if os.path.exists(sync_path):
            try:
                with open(sync_path, encoding="utf-8") as f:
                    sync = json.load(f)
                synced = sync.get("added", [])
                if synced:
                    tag = "已提交" if sync.get("committed") else "待提交"
                    lines += ["**新纳入发布清单的 fork（%s）**" % tag, ""]
                    lines += ["- `%s` → `%s`（%s，分支 `%s`）" % (
                        x["repo"], x.get("path", "."), x.get("kind", ""), x.get("branch", ""))
                        for x in synced]
                    lines.append("")
            except (OSError, ValueError):
                pass

    if s["conflicts"]:
        lines += ["**需人工处理**", ""]
        for r in s["conflicts"]:
            lines.append("- `%s`：%s（备份 `%s`）" % (
                r.get("name", ""), r.get("result", ""), r.get("backup_ref") or "无"))
        lines.append("")
    if s["risky"] and mode == "check":
        lines.append("风险提示：%d 个仓库双方改过同名文件（不等同于冲突）。" % len(s["risky"]))
        lines.append("")

    if run_url():
        lines += ["[查看本次运行日志](%s) ｜ [状态 Issue](%s/%s/issues?q=label%%3A%s)" % (
            run_url(), SERVER, REPO, LABEL), ""]
    lines += ["<sub>由 fork-hub 自动推送 · run #%s · attempt %s · 通知级别 %s</sub>" % (
        RUN_NUMBER or "?", RUN_ATTEMPT or "?", LEVEL)]

    markdown = "\n".join(lines)
    plain = re.sub(r"[|#*`\[\]<>]", "", markdown)
    plain = re.sub(r"\n{3,}", "\n\n", plain).strip()
    return title, markdown, plain


def should_skip(mode, err, s):
    """返回 (是否跳过, 原因)。失败与冲突永远不静默。"""
    if mode == "release":
        if err or JOB_STATUS != "success" or s.get("failed"):
            return False, ""
        if LEVEL == "changes" and not s.get("releases"):
            return True, "本次没有发布目标，NOTIFY_LEVEL=changes 已静默"
        return False, ""
    bad = JOB_STATUS != "success" or bool(err) or bool(s["conflicts"])
    if SCOPE != "both" and mode == "check" and APPLY_DISPATCHED == "true" and not bad:
        return True, "已自动派发 apply，最终通知由 apply 工作流发出（NOTIFY_SCOPE=final）"
    if LEVEL == "changes" and not bad and not s["pending"] and not s["updated"]:
        return True, "本次无变化，NOTIFY_LEVEL=changes 已静默"
    return False, ""


def post_issue_comment(title, markdown):
    if not (TOKEN and REPO):
        print("跳过 Issue 评论：缺少 GH_TOKEN / GITHUB_REPOSITORY")
        return False
    st, issues = api("GET", "/repos/%s/issues?state=all&labels=%s&per_page=100" % (REPO, LABEL))
    existing = [i for i in issues if isinstance(i, dict) and not i.get("pull_request")] \
        if isinstance(issues, list) else []
    if not existing:
        print("跳过 Issue 评论：还没有带 %s 标签的状态 Issue（sync_issue.py 会创建）" % LABEL)
        return False
    issue = max(existing, key=lambda i: int(i.get("number") or 0))
    body = "#### %s\n\n%s" % (title, markdown)
    st, d = api("POST", "/repos/%s/issues/%d/comments" % (REPO, issue["number"]), {"body": body})
    if st in (200, 201):
        print("已发布通知评论到 Issue #%d（HTTP %s），@%s 将收到 GitHub 通知" % (
            issue["number"], st, MENTION or "(未启用)"))
        return True
    print("警告：Issue 评论发布失败 HTTP %s：%s" % (st, d))
    return False


def adapt_webhook(url, title, markdown, plain):
    """返回 (是否表单编码, payload)。按 URL 自动适配常见推送服务。"""
    if "sctapi.ftqq.com" in url or "sc.ftqq.com" in url:
        return True, {"title": title, "desp": markdown}
    if "api.day.app" in url or "bark" in url.lower():
        return False, {"title": title, "body": plain, "group": "fork-hub"}
    if "qyapi.weixin.qq.com" in url:
        return False, {"msgtype": "markdown",
                       "markdown": {"content": "**%s**\n%s" % (title, markdown)}}
    if "oapi.dingtalk.com" in url:
        return False, {"msgtype": "markdown",
                       "markdown": {"title": title, "text": "### %s\n\n%s" % (title, markdown)}}
    if "open.feishu.cn" in url or "open.larksuite.com" in url:
        return False, {"msg_type": "text", "content": {"text": "%s\n\n%s" % (title, plain)}}
    return False, {"title": title, "body": plain, "markdown": markdown,
                   "repo": REPO, "run_url": run_url(), "status": JOB_STATUS}


def post_webhook(title, markdown, plain):
    if not WEBHOOK:
        return False
    host = urllib.parse.urlsplit(WEBHOOK).netloc or "(invalid url)"
    try:
        is_form, payload = adapt_webhook(WEBHOOK, title, markdown, plain)
        if is_form:
            data = urllib.parse.urlencode(payload).encode("utf-8")
            ctype = "application/x-www-form-urlencoded"
        else:
            data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
            ctype = "application/json; charset=utf-8"
        req = urllib.request.Request(
            WEBHOOK, data=data, method="POST",
            headers={"Content-Type": ctype, "User-Agent": "fork-hub-notify"})
        with urllib.request.urlopen(req, timeout=25) as r:
            resp = r.read().decode("utf-8", "replace")[:200]
            print("已推送 webhook -> %s，HTTP %s，响应：%s" % (host, r.status, resp))
            return 200 <= r.status < 300
    except Exception as e:
        print("警告：webhook 推送失败（%s）：%s" % (host, e))
        return False


def write_summary(title, markdown, skipped_reason, delivered):
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    lines = ["## 通知推送：%s" % title, "",
             "- Issue 评论 / @mention：%s" % ("已发送" if delivered else "未发送"),
             "- 外部 webhook：%s" % ("已配置" if WEBHOOK else "未配置"),
             "- 通知级别：`%s` ｜ 范围：`%s`" % (LEVEL, SCOPE), ""]
    if skipped_reason:
        lines += ["> 本次未推送：%s" % skipped_reason, ""]
    lines += [markdown]
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write("\n".join(lines) + "\n")
    except Exception as e:
        print("警告：写 Step Summary 失败：%s" % e)


def main():
    if MODE not in ("check", "apply", "release"):
        raise SystemExit("MODE 只能是 check / apply / release，当前为 %r" % MODE)
    d, err = load_data()
    s = stats(d)
    title, markdown, plain = build_message(MODE, d, err, s)
    skip, reason = should_skip(MODE, err, s)

    print("通知标题：%s" % title)
    print("运行状态：%s ｜ fork %d ｜ 待更新 %d ｜ 已更新 %d ｜ 冲突 %d" % (
        JOB_STATUS, s["total"], len(s["pending"]), len(s["updated"]), len(s["conflicts"])))
    if skip:
        print("按策略跳过推送：%s" % reason)
        write_summary(title, markdown, reason, False)
        return

    delivered = post_issue_comment(title, markdown)
    post_webhook(title, markdown, plain)
    write_summary(title, markdown, "", delivered)
    print("通知处理完成。")


if __name__ == "__main__":
    main()
