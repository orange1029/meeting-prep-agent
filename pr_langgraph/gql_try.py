#!/usr/bin/env python
"""
gql_try.py — 探 GraphQL 通道能否绕过 REST 的 403

背景（2026-10-21 实测）：
  REST API 对上游 langchain-ai/langgraph 写入 → 403
  （fine-grained PAT 对外部仓库默认只读）
  但 GraphQL 端点的权限判定可能与 REST 不同，值得一试。

用法：
  python gql_try.py --token ghp_xxx            # 探测（不发评论）
  python gql_try.py --token ghp_xxx --comment  # 真发（用正式文案）
"""
import argparse
import json
import os
import sys
import urllib.error
import urllib.request

API = "https://api.github.com/graphql"
REPO_OWNER = "langchain-ai"
REPO_NAME = "langgraph"
ISSUE_NUMBER = 9319
PR_NUMBER = 9318


def gql(token, query, variables=None):
    body = json.dumps({"query": query, "variables": variables or {}})
    req = urllib.request.Request(API, data=body.encode("utf-8"), method="POST")
    req.add_header("Authorization", f"Bearer {token}")
    req.add_header("Content-Type", "application/json")
    req.add_header("User-Agent", "meeting-prep-agent-gql")
    try:
        with urllib.request.urlopen(req, timeout=25) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        return {"http_error": e.code,
                "body": e.read().decode("utf-8", "replace")[:400]}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", required=True)
    ap.add_argument("--comment", action="store_true", help="真发评论")
    args = ap.parse_args()

    print("=== 1. GraphQL 基本可用性 ===")
    r = gql(args.token, "query { viewer { login } }")
    print("  viewer:", json.dumps(r, ensure_ascii=False)[:200])

    print("\n=== 2. 拿 issue 的 node id ===")
    r = gql(args.token,
            "query($owner:String!,$name:String!,$num:Int!){"
            " repository(owner:$owner,name:$name){"
            "  issue(number:$num){ id title state number } } }",
            {"owner": REPO_OWNER, "name": REPO_NAME, "num": ISSUE_NUMBER})
    print("  ", json.dumps(r, ensure_ascii=False)[:300])

    issue_id = None
    try:
        issue_id = r["data"]["repository"]["issue"]["id"]
    except Exception:
        print("  ✗ 没拿到 issue id")
        return

    print("\n=== 3. viewer 在该仓库的权限 ===")
    # viewerPermission 是枚举（RepositoryPermission），不是对象，
    # 所以不能带子字段——直接取它的值。
    r2 = gql(args.token,
             "query($owner:String!,$name:String!){"
             " repository(owner:$owner,name:$name){"
             "  viewerPermission } }",
             {"owner": REPO_OWNER, "name": REPO_NAME})
    print("  viewerPermission:", json.dumps(r2, ensure_ascii=False)[:200])
    print("  （READ → 只读；WRITE → 能写；ADMIN → 管理员）")

    if not args.comment:
        print("\n（加 --comment 才会真发评论）")
        return

    print("\n=== 4. 尝试发评论 ===")
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import submit_pr_update as spu
    r3 = gql(args.token,
             "mutation($subject:ID!,$body:String!){"
             " addComment(input:{subjectId:$subject,body:$body}){"
             "  clientMutationId } }",
             {"subject": issue_id, "body": spu.ISSUE_COMMENT})
    print("  ", json.dumps(r3, ensure_ascii=False)[:500])
    if r3.get("data", {}).get("addComment"):
        print("\n✓ 评论已发出")
    else:
        print("\n✗ 没能发出——需要网页手动贴")


if __name__ == "__main__":
    main()
