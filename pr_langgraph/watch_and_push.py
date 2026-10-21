#!/usr/bin/env python
"""
watch_and_push.py — 等待上游写权限就绪，然后自动完成剩余动作

背景（2026-10-21）：
  账号在 langchain-ai/langgraph 只有 READ 权限，
  fine-grained PAT 不继承「PR 作者可编辑自己提的 PR」这条账号级规则，
  所以 API 改不了上游 PR 正文、也发不了 issue 评论。

  用户去开权限了。这个脚本轮询等权限到位，一到位就自动：
    1. 更新 PR #9318 的正文（加上「上游已指派」那一节）
    2. 删掉重复的 issue 评论（保留完整那条）
    3. 在 issue #9319 留言请求 assign
    4. 核验结果

用法：
  python watch_and_push.py --token <token> --wait 900
  python watch_and_push.py --token <token>          # 不等待，直接试一次

token 来源优先级：--token 参数 > GITHUB_TOKEN/GH_TOKEN 环境变量 > .github_token 文件
"""

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"
GRAPHQL = "https://api.github.com/graphql"
OWNER = "langchain-ai"
NAME = "langgraph"
PR_NUMBER = 9318
ISSUE_NUMBER = 9319

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
PR_BODY_PATH = os.path.join(HERE, "PR_DESCRIPTION.md")

# 重复评论的判定：含关键引用但长度偏短的那条
DUP_MARKER = "the clearer error for unknown keys is tracked in #9319"


def rest(method, path, token, body=None):
    url = f"{API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "meeting-prep-agent-watcher")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            raw = resp.read().decode("utf-8")
            return resp.status, (json.loads(raw) if raw else {})
    except urllib.error.HTTPError as e:
        body_txt = e.read().decode("utf-8", "replace")
        try:
            return e.code, json.loads(body_txt)
        except Exception:
            return e.code, {"raw": body_txt[:300]}
    except Exception as e:
        return 0, {"error": str(e)}


def gql(token, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(GRAPHQL, data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "meeting-prep-agent-watcher")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:
        return {"error": str(e)}


Q_PERM = """
query($owner:String!, $name:String!) {
  repository(owner:$owner, name:$name) { viewerPermission }
}
"""

M_PR = """
mutation($id:ID!, $body:String!) {
  updatePullRequest(input:{pullRequestId:$id, body:$body}) {
    pullRequest { number body }
  }
}
"""

M_COMMENT = """
mutation($subject:ID!, $body:String!) {
  addComment(input:{subjectId:$subject, body:$body}) { clientMutationId }
}
"""

Q_IDS = """
query($owner:String!, $name:String!, $prNum:Int!, $issueNum:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$prNum) { id body state }
    issue(number:$issueNum) { id state comments(last:10) {
      nodes { id body author{login} createdAt } } }
  }
}
"""


def find_token(explicit=None):
    if explicit:
        return explicit
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(env, "").strip()
        if v:
            return v
    f = os.path.join(PROJECT_ROOT, ".github_token")
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                v = fh.read().strip()
            if v:
                return v
        except OSError:
            pass
    return None


def permission(token):
    r = gql(token, Q_PERM, {"owner": OWNER, "name": NAME})
    return r.get("data", {}).get("repository", {}).get("viewerPermission")


def try_write(token):
    """权限到位就执行三件事。返回 (成功项列表, 失败说明)"""
    done, notes = [], []

    r = gql(token, Q_IDS, {"owner": OWNER, "name": NAME,
                           "prNum": PR_NUMBER, "issueNum": ISSUE_NUMBER})
    repo = r.get("data", {}).get("repository", {})
    pr = repo.get("pullRequest") or {}
    issue = repo.get("issue") or {}
    pr_id = pr.get("id")
    issue_id = issue.get("id")

    # ---- 1. 更新 PR 正文 ----
    with open(PR_BODY_PATH, "r", encoding="utf-8") as f:
        want = f.read().strip()
    have = (pr.get("body") or "").strip()
    if pr_id and want != have:
        r2 = gql(token, M_PR, {"id": pr_id, "body": want})
        if r2.get("data", {}).get("updatePullRequest"):
            done.append(f"PR #{PR_NUMBER} 正文已更新（{len(want)} 字符）")
        else:
            errs = r2.get("errors", r2)
            notes.append(f"PR 正文更新失败：{json.dumps(errs, ensure_ascii=False)[:200]}")
    elif pr_id:
        done.append("PR 正文已是最新，跳过")

    # ---- 2. 清理重复评论 ----
    comments = (issue.get("comments") or {}).get("nodes") or []
    mine = [c for c in comments
            if (c.get("author") or {}).get("login") == "orange1029"
            and DUP_MARKER in (c.get("body") or "")]
    if len(mine) > 1:
        # 保留最长那条，删其余
        keep = max(mine, key=lambda c: len(c.get("body") or ""))
        for c in mine:
            if c["id"] == keep["id"]:
                continue
            code, _ = rest("DELETE",
                           f"/repos/{OWNER}/{NAME}/issues/comments/{c['id']}",
                           token)
            if code == 204:
                done.append(f"已删重复评论 {c['id'][:12]}（保留完整那条）")
            else:
                notes.append(f"删评论 {c['id'][:12]} 失败：HTTP {code}")
    elif len(mine) == 1:
        done.append("无重复评论")

    # ---- 3. 发留言（如果还没有）----
    if issue_id and not mine:
        sys.path.insert(0, HERE)
        import submit_pr_update as spu
        r3 = gql(token, M_COMMENT,
                 {"subject": issue_id, "body": spu.ISSUE_COMMENT.strip()})
        if r3.get("data", {}).get("addComment"):
            done.append(f"已在 issue #{ISSUE_NUMBER} 留言")
        else:
            notes.append(f"留言失败：{json.dumps(r3.get('errors', r3), ensure_ascii=False)[:200]}")
    elif mine:
        done.append(f"issue #{ISSUE_NUMBER} 已有留言（{len(mine)} 条），跳过")

    return done, notes


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token")
    ap.add_argument("--wait", type=int, default=0,
                    help="最多等待多少秒（默认 0 = 不等待，立刻试一次）")
    ap.add_argument("--interval", type=int, default=30, help="轮询间隔秒数")
    args = ap.parse_args()

    # 每次轮询都重新取 token。
    # 关键：用户在 GitHub 上点 Update token 后，**旧 token 立刻失效**，
    # 而新 token 字符串是变的。如果只在开头取一次，
    # 监听器会一直拿着已失效的旧 token，永远看不到权限变化。
    # 所以每轮重读 .github_token / 环境变量 / --token。
    explicit = args.token
    token = find_token(explicit)
    if not token:
        raise SystemExit(
            "✗ 没有 token。三种办法任选一个：\n"
            "  1) --token ghp_xxx\n"
            "  2) set GITHUB_TOKEN=ghp_xxx\n"
            "  3) 在项目根目录建 .github_token 文件，内容就是 token\n"
        )

    print("=" * 64)
    print(f"  监听上游写权限 | token={token[:12]}…")
    print(f"  每 {args.interval}s 重读一次 token（改权限后旧 token 会失效，"
          f"必须能读到新的）")
    print("=" * 64)

    deadline = time.time() + args.wait
    attempt = 0
    while True:
        attempt += 1
        # 每轮重读：用户可能刚换了新 token
        fresh = find_token(explicit)
        if fresh and fresh != token:
            print(f"\n[{attempt}] 检测到 token 已更新")
            token = fresh

        # token 没了（用户删了 .github_token 文件）→ 优雅退出，别空转
        if not token:
            print(f"\n[{attempt}] token 已不可用（.github_token 被删除？）")
            print("  → 退出监听。如果还想继续，把新 token 写回 .github_token 后重跑。")
            return

        perm = permission(token)
        print(f"[{attempt}] 权限: {perm}", end="")

        if perm in ("WRITE", "ADMIN"):
            print(" → 已就绪，开始执行")
            break

        print(" → 仍只读")
        if time.time() >= deadline:
            print(f"\n等待超时（{args.wait}s）。当前权限 {perm}")
            print("你可以手动跑一次，或提高 --wait 的秒数。")
            print("（若你已开了权限但这里还是 READ，"
                  "可能是 token 没复制新的——重新生成后 token 字符串会变。）")
            return

        wait = min(args.interval, max(0, deadline - time.time()))
        print(f"  {int(wait)}s 后重试…")
        time.sleep(wait)

    # ---- 执行 ----
    print("\n" + "=" * 64)
    print("  执行剩余动作")
    print("=" * 64)
    done, notes = try_write(token)

    print("\n【完成】")
    for d in done:
        print(f"  ✓ {d}")
    if notes:
        print("\n【未完成】")
        for n in notes:
            print(f"  ✗ {n}")

    # ---- 核验 ----
    print("\n" + "=" * 64)
    print("  核验")
    print("=" * 64)
    r = gql(token, Q_IDS, {"owner": OWNER, "name": NAME,
                           "prNum": PR_NUMBER, "issueNum": ISSUE_NUMBER})
    repo = r.get("data", {}).get("repository", {})
    pr = repo.get("pullRequest") or {}
    issue = repo.get("issue") or {}
    body = pr.get("body") or ""
    print(f"  PR #{PR_NUMBER}: {pr.get('state')}")
    print(f"    含 Status in upstream : {'Status in upstream' in body}")
    print(f"    含 eliornl 原话       : {DUP_MARKER in body}")
    print(f"    含与 #6771 的区别说明 : {'#6771' in body}")
    print(f"  Issue #{ISSUE_NUMBER}: {issue.get('state')}")
    comments = (issue.get("comments") or {}).get("nodes") or []
    mine = [c for c in comments
            if (c.get("author") or {}).get("login") == "orange1029"
            and DUP_MARKER in (c.get("body") or "")]
    print(f"    你的关键引用留言      : {len(mine)} 条")


if __name__ == "__main__":
    main()
