#!/usr/bin/env python
"""
gql_write.py — 用 GraphQL 通道更新上游 PR 正文 / 发 issue 评论

为什么要有这个脚本（2026-10-21 实测记录）
  REST API 对上游 langchain-ai/langgraph 写入 → 403
  GraphQL 通道 → 能通，且如实报告 viewerPermission: READ

  结论：403 不是 token 的问题，是**账号**在上游仓库只有 READ 权限。
  GitHub「PR 作者可编辑自己提的 PR」是账号级规则，
  fine-grained PAT 不继承（PAT 严格按自身授权走）。

  所以这个脚本的价值是：
    1. 万一以后 PAT 加了上游仓库的授权，这里能直接干活
    2. 现在就能明确报出「为什么不行」，不用再猜

用法：
  python gql_write.py --token ghp_xxx --pr          # 更新 PR 正文
  python gql_write.py --token ghp_xxx --comment      # 发 issue 评论
  python gql_write.py --token ghp_xxx --check        # 只查权限
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/graphql"
OWNER = "langchain-ai"
NAME = "langgraph"
PR_NUMBER = 9318
ISSUE_NUMBER = 9319

HERE = os.path.dirname(os.path.abspath(__file__))
PR_BODY_PATH = os.path.join(HERE, "PR_DESCRIPTION.md")


def gql(token, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(API, data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "meeting-prep-agent-gql-write")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"http_error": e.code,
                "body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


Q_NODE = """
query($owner:String!, $name:String!, $prNum:Int!, $issueNum:Int!) {
  repository(owner:$owner, name:$name) {
    pullRequest(number:$prNum) { id body title state }
    issue(number:$issueNum) { id title state assignees(first:5){nodes{login}} }
  }
}
"""

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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--pr", action="store_true", help="更新 PR #9318 正文")
    ap.add_argument("--comment", action="store_true", help="在 issue #9319 留言")
    ap.add_argument("--check", action="store_true", help="只查权限")
    args = ap.parse_args()

    # ---- 权限 ----
    r = gql(args.token, Q_PERM, {"owner": OWNER, "name": NAME})
    perm = r.get("data", {}).get("repository", {}).get("viewerPermission")
    print(f"账号在 {OWNER}/{NAME} 的权限: {perm}")
    if perm == "READ":
        print("  → 只读。写入操作会被拒。")
        print("  → 要解决：把该仓库加进 PAT 的 Repository access，")
        print("    并勾 Pull requests / Issues = Read and write。")

    # ---- node id ----
    r2 = gql(args.token, Q_NODE,
             {"owner": OWNER, "name": NAME,
              "prNum": PR_NUMBER, "issueNum": ISSUE_NUMBER})
    repo = r2.get("data", {}).get("repository", {})
    pr = repo.get("pullRequest") or {}
    issue = repo.get("issue") or {}
    print(f"\nPR #{PR_NUMBER}: {pr.get('state')} — {pr.get('title')}")
    print(f"Issue #{ISSUE_NUMBER}: {issue.get('state')}, "
          f"assignees={[a['login'] for a in (issue.get('assignees') or {}).get('nodes', [])]}")

    if args.check or not (args.pr or args.comment):
        return

    if args.pr:
        with open(PR_BODY_PATH, "r", encoding="utf-8") as f:
            body = f.read()
        if body.strip() == (pr.get("body") or "").strip():
            print("\nPR 正文已是最新，跳过")
        else:
            r3 = gql(args.token, M_PR, {"id": pr.get("id"), "body": body})
            ok = r3.get("data", {}).get("updatePullRequest")
            if ok:
                print(f"\n✓ PR #{PR_NUMBER} 正文已更新（{len(body)} 字符）")
            else:
                print("\n✗ 更新失败：")
                print("  ", json.dumps(r3.get("errors", r3), ensure_ascii=False)[:300])

    if args.comment:
        sys.path.insert(0, HERE)
        import submit_pr_update as spu
        r4 = gql(args.token, M_COMMENT,
                 {"subject": issue.get("id"), "body": spu.ISSUE_COMMENT})
        ok = r4.get("data", {}).get("addComment")
        if ok:
            print(f"\n✓ 已在 issue #{ISSUE_NUMBER} 留言")
        else:
            print("\n✗ 留言失败：")
            print("  ", json.dumps(r4.get("errors", r4), ensure_ascii=False)[:300])


if __name__ == "__main__":
    main()
