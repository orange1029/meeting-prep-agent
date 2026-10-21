#!/usr/bin/env python
"""
push_all.py — 一键把本地提交推上去 + 更新 LangGraph PR

为什么需要这个？
  2026-10-21 那次收尾时，本地 3 个提交都做好了，但推不上去——
  Git 凭据里的 OAuth token（gho_ 前缀）过期了，
  而 GCM 要访问 github.com 才能刷新，那个域名在当时的网络下被代理掐断
  （502 错误的网关 / could not read Username）。

  结论是：**技术上完全没障碍，只差一个有效 token**。
  这个脚本把「找 token → 推代码 → 更新 PR → 在 issue 留言」串成一条命令，
  避免下次再手动分步做。

用法（三选一，token 来源不同）：
  python push_all.py --token ghp_xxx
  set GITHUB_TOKEN=ghp_xxx && python push_all.py
  # 或在项目根目录建 .github_token 文件，内容就是 token

加 --dry-run 只看要做什么，不真执行。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
REPO = "langchain-ai/langgraph"
PR_NUMBER = 9318
ISSUE_NUMBER = 9319

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))
PR_BODY_PATH = os.path.join(HERE, "PR_DESCRIPTION.md")


def api(method, path, token, body=None):
    url = f"{API}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    req = urllib.request.Request(url, data=data, method=method)
    req.add_header("Authorization", f"token {token}")
    req.add_header("Accept", "application/vnd.github+json")
    req.add_header("X-GitHub-Api-Version", "2022-11-28")
    req.add_header("User-Agent", "meeting-prep-agent-push-all")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
            return json.loads(raw) if raw else {}
    except urllib.error.HTTPError as e:
        detail = e.read().decode("utf-8", "replace")
        raise SystemExit(f"✗ API {e.code}: {method} {path}\n  {detail[:300]}")


def verify(token):
    if not token:
        return None
    try:
        req = urllib.request.Request(f"{API}/user")
        req.add_header("Authorization", f"token {token}")
        req.add_header("User-Agent", "meeting-prep-agent-push-all")
        with urllib.request.urlopen(req, timeout=15) as resp:
            return json.loads(resp.read().decode("utf-8")).get("login")
    except Exception:
        return None


def find_token(explicit):
    cands = []
    if explicit:
        cands.append(("命令行参数", explicit))
    for env in ("GITHUB_TOKEN", "GH_TOKEN"):
        v = os.environ.get(env, "").strip()
        if v:
            cands.append((f"环境变量 {env}", v))
    f = os.path.join(PROJECT_ROOT, ".github_token")
    if os.path.exists(f):
        try:
            with open(f, "r", encoding="utf-8-sig") as fh:
                v = fh.read().strip()
            if v:
                cands.append((f"文件 {f}", v))
        except OSError:
            pass

    print("查找 token…")
    for src, tok in cands:
        login = verify(tok)
        if login:
            print(f"  ✓ {src} 可用（{login}）")
            return tok
        print(f"  ✗ {src} 无效或已过期")
    print("  ✗ 没有可用 token")
    return None


def git(*args, timeout=120):
    p = subprocess.run(["git", *args], cwd=PROJECT_ROOT,
                       capture_output=True, text=True, timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="GitHub personal access token")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--skip-push", action="store_true",
                    help="只更新 PR，不推代码")
    args = ap.parse_args()

    token = find_token(args.token)
    if not token:
        raise SystemExit(
            "\n✗ 没有可用 token。三种办法任选一个：\n"
            "  1) python push_all.py --token ghp_你的token\n"
            "  2) set GITHUB_TOKEN=ghp_你的token\n"
            "  3) 在项目根目录建 .github_token 文件，内容就是 token\n"
            "  token 生成：https://github.com/settings/tokens （勾 public_repo）\n"
        )

    # ---- 1. 推代码 ----
    if not args.skip_push:
        rc, out = git("status", "--short")
        dirty = [ln for ln in out.splitlines()
                 if ln.strip() and not ln.startswith("??")]
        if dirty:
            print(f"\n⚠ 有未提交的改动，先提交再推：")
            for ln in dirty:
                print(f"    {ln}")
            raise SystemExit("  （或用 git add + git commit 后重跑）")

        rc, out = git("log", "--oneline", "origin/master..master")
        n = len([ln for ln in out.splitlines() if ln.strip()])
        print(f"\n[1/3] 推送代码（{n} 个待推提交）")
        if n == 0:
            print("  已是最新，跳过")
        else:
            for ln in out.splitlines():
                if ln.strip():
                    print(f"    {ln}")
            if args.dry_run:
                print("  DRY RUN：跳过实际 push")
            else:
                # 把 token 拼进 URL 推送——凭据管理器那条路现在不通
                url = (f"https://orange1029:{token}@github.com/"
                       f"orange1029/meeting-prep-agent.git")
                rc, out = git("push", url, "master")
                # 清掉 URL 里的 token（remote 里不留明文）
                git("remote", "set-url", "origin",
                    "https://github.com/orange1029/meeting-prep-agent.git")
                if rc != 0:
                    print(out[-800:])
                    raise SystemExit("✗ push 失败")
                print("  ✓ 推送成功")

    # ---- 2. 更新 PR 正文 ----
    print("\n[2/3] 更新 LangGraph PR 正文")
    with open(PR_BODY_PATH, "r", encoding="utf-8") as fh:
        body = fh.read()
    pr = api("GET", f"/repos/{REPO}/pulls/{PR_NUMBER}", token)
    print(f"  PR #{PR_NUMBER} 当前 state={pr.get('state')}")
    if body.strip() == (pr.get("body") or "").strip():
        print("  已是最新，跳过")
    elif args.dry_run:
        print(f"  DRY RUN：将更新 {len(body)} 字符")
    else:
        api("PATCH", f"/repos/{REPO}/pulls/{PR_NUMBER}", token, {"body": body})
        print("  ✓ 已更新")

    # ---- 3. 在 issue 留言 ----
    print("\n[3/3] 在 issue #9319 留言请求 assign")
    comments = api("GET", f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments", token)
    marker = "the clearer error for unknown keys is tracked in #9319"
    if any(marker in (c.get("body") or "") for c in comments):
        print("  已存在，跳过（幂等）")
    else:
        issue = api("GET", f"/repos/{REPO}/issues/{ISSUE_NUMBER}", token)
        assignees = [a.get("login") for a in (issue.get("assignees") or [])]
        print(f"  issue assignees={assignees or '无'}")
        if args.dry_run:
            print("  DRY RUN：跳过留言")
        else:
            import submit_pr_update as spu
            api("POST", f"/repos/{REPO}/issues/{ISSUE_NUMBER}/comments",
                token, {"body": spu.ISSUE_COMMENT})
            print("  ✓ 已留言")

    print("\n" + "=" * 60)
    print("  完成")
    print("=" * 60)
    print(f"  项目  https://github.com/orange1029/meeting-prep-agent")
    print(f"  PR    https://github.com/{REPO}/pull/{PR_NUMBER}")
    print(f"  Issue https://github.com/{REPO}/issues/{ISSUE_NUMBER}")
    print("\n  下一步：等维护者 assign，机器人会自动重开 PR。")


if __name__ == "__main__":
    sys.path.insert(0, HERE)
    main()
