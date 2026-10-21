#!/usr/bin/env python
"""
submit_pr_update.py — 一键更新 LangGraph PR #9318 的描述 + 在 issue #9319 留言

为什么需要这个脚本？
  PR #9318 目前是 closed 状态（被 require_issue_link 机器人自动关闭，
  因为作者 orange1029 没被 assign 到关联 issue #9319）。
  机器人会在被 assign 后自动重开 PR。

  但有个新情况：2026-10-19 维护者 eliornl 关闭 #6877 时留言说
  "the clearer error for unknown keys is tracked in #9319"——
  上游已把 #9319 正式指认为「未知 key 更清晰报错」的唯一 tracking issue。
  这个信息必须让维护者看到，否则他们不知道 #9319 已经被认可。

  所以这个脚本做两件事：
    1. 更新 PR #9318 的正文，加上「上游已指派」那一节
    2. 在 issue #9319 下留言，把 eliornl 的话引出来，请求 assign

  为什么写成脚本而不是手动点？
  · API token 会过期，手动重贴容易漏改
  · 脚本是幂等的，重复跑不会刷屏（会检查内容是否已存在）
  · 出错时有明确的错误信息，不用猜

用法：
  python submit_pr_update.py --token ghp_xxx
  python submit_pr_update.py --token ghp_xxx --dry-run   # 只看要发什么

拿 token 的方式：
  · github.com/settings/tokens 生成 personal access token，
    勾 public_repo 权限即可（只读公开仓库其实不需要，但更新 PR 需要）
  · 或者本机已登录 GitHub 时：
      printf 'protocol=https\nhost=github.com\n\n' | git credential fill
"""

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "langchain-ai/langgraph"
PR_NUMBER = 9318
ISSUE_NUMBER = 9319

PR_BODY_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)),
                            "PR_DESCRIPTION.md")

ISSUE_COMMENT = """Hi @eliornl — following up on the assignment request above.

Since I filed this, the situation seems to have moved in the right direction:
in [#6877](https://github.com/langchain-ai/langgraph/pull/6877) you wrote on 2026-10-19:

> closing in favor of #6771, which makes the same fix for routing to `END` without a `path_map` entry. **the clearer error for unknown keys is tracked in #9319.**

So the two concerns are split upstream:

- **routing to `END` without a `path_map` entry** → #6771 (a behaviour change)
- **a clearer error for unknown keys** → **#9319, this PR**

`main` still raises a bare `KeyError` at the `path_map` lookup
(`self.ends[r]` in `Branch._finish`), and there's no `_resolve_destination`
symbol upstream, so the error-message half looks unaddressed so far.

I've updated the PR description to reflect this split and to spell out how it
differs from #6771 and from the earlier #6877 attempt (which introduced a
`_resolve_destination` helper too, but its message didn't list the valid
`path_map` keys and didn't say which side to fix).

Could you assign me (@orange1029) to this issue? The `require_issue_link`
gate needs the PR author to be assigned before it will reopen, and I can't
assign myself to an issue outside the repo.

The change is deliberately narrow: only the error message. It doesn't alter
which values are accepted, so it composes with #6771 rather than conflicting
with it. The PR carries 8 tests, and I verified the two error tests fail
against the unpatched `_branch.py` while the six routing tests pass in both
states, so the behavioural cases assert compatibility independently of the fix.

Thanks again for tracking this.
"""


def request(method, path, token, body=None):
    """调 GitHub API，返回解析后的 JSON"""
    url = f"{API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "meeting-prep-agent-pr-updater")
    if data:
        req.add_header("Content-Type", "application/json")

    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(
            f"✗ GitHub API 报错 {e.code}\n"
            f"  {method} {path}\n"
            f"  {detail[:500]}\n\n"
            f"  401 = token 无效或已过期（GitHub token 默认 8 小时或 90 天过期）。\n"
            f"  重新生成：https://github.com/settings/tokens"
        )
    except urllib.error.URLError as e:
        raise SystemExit(
            f"✗ 网络不通：{e}\n"
            f"  api.github.com 应该能通，github.com 可能被代理拦了。"
        )


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True, help="GitHub personal access token")
    ap.add_argument("--dry-run", action="store_true", help="只打印要发什么，不真发")
    args = ap.parse_args()

    if not os.path.exists(PR_BODY_PATH):
        raise SystemExit(f"✗ 找不到 PR 描述文件：{PR_BODY_PATH}")

    with open(PR_BODY_PATH, "r", encoding="utf-8") as f:
        pr_body = f.read()

    # ---- 先确认身份 ----
    user = request("GET", "/user", args.token)
    print(f"✓ 已认证为 {user.get('login')}")

    # ---- 检查 PR 当前状态 ----
    pr = request("GET", f"/repos/{REPO}/pulls/{PR_NUMBER}", args.token)
    print(f"  PR #{PR_NUMBER}: {pr.get('state')} "
          f"(head={pr.get('head', {}).get('ref')}, "
          f"updated={pr.get('updated_at')})")

    issue = request("GET", f"/repos/{REPO}/issues/{ISSUE_NUMBER}", args.token)
    assignees = [a.get("login") for a in (issue.get("assignees") or [])]
    print(f"  Issue #{ISSUE_NUMBER}: {issue.get('state')}, "
          f"assignees={assignees or '无'}")

    # ---- 检查 issue 下是否已有我们的留言（幂等）----
    comments = request("GET",
                       f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
                       args.token)
    marker = "the clearer error for unknown keys is tracked in #9319"
    already = any(marker in (c.get("body") or "") for c in comments)
    if already:
        print("  ✓ issue 下已有引用该表述的留言，跳过（不重复刷屏）")

    if args.dry_run:
        print("\n" + "=" * 60)
        print("  DRY RUN —— 以下内容会被发送")
        print("=" * 60)
        print(f"\n【更新 PR #{PR_NUMBER} 正文】共 {len(pr_body)} 字符")
        print(f"\n【在 issue #{ISSUE_NUMBER} 留言】"
              f"{'（已存在，跳过）' if already else '共 ' + str(len(ISSUE_COMMENT)) + ' 字符'}")
        print("\n" + "=" * 60)
        return

    # ---- 更新 PR 正文 ----
    request("PATCH", f"/repos/{REPO}/pulls/{PR_NUMBER}", args.token,
            {"body": pr_body})
    print(f"✓ 已更新 PR #{PR_NUMBER} 的描述")

    # ---- 在 issue 下留言 ----
    if not already:
        request("POST",
                f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
                args.token, {"body": ISSUE_COMMENT})
        print(f"✓ 已在 issue #{ISSUE_NUMBER} 下留言")
    else:
        print(f"- 跳过 issue 留言（已有）")

    print("\n下一步：等维护者 assign。机器人会自动重开 PR。")
    print(f"  PR    https://github.com/{REPO}/pull/{PR_NUMBER}")
    print(f"  Issue https://github.com/{REPO}/issues/{ISSUE_NUMBER}")


if __name__ == "__main__":
    main()
