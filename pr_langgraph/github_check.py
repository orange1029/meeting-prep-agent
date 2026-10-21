#!/usr/bin/env python
"""
github_check.py — 推送前自检：一眼看出卡在哪一步

为什么需要这个？
  2026-10-21 排查「推不上去」时，发现有三层独立的障碍叠在一起，
  每层的表现都是「推不上去」，但修法完全不同：
    1. token 看不到你的仓库（404）→ 改 token 的 Repository access
    2. token 能看但没写权限（403） → 加 Contents/Pull requests 权限
    3. github.com 域名不通         → 走 API 降级通道（脚本已处理）

  之前靠一个个 curl 试，很慢。这个脚本一次跑完，给出结论。

用法：
  python github_check.py --token ghp_xxx
  python github_check.py                    # 不带 token 则只查网络和本机状态

只看结论不看细节？每个检查项开头有 [OK]/[!!] 标记。
"""

import argparse
import json
import os
import subprocess
import sys
import urllib.error
import urllib.request

API = "https://api.github.com"
OWNER = "orange1029"
REPO_NAME = "meeting-prep-agent"
UPSTREAM = "langchain-ai/langgraph"
PR_NUMBER = 9318
ISSUE_NUMBER = 9319

HERE = os.path.dirname(os.path.abspath(__file__))
PROJECT_ROOT = os.path.abspath(os.path.join(HERE, ".."))


def http(url, token=None, method="GET", timeout=15):
    """返回 (状态码, 响应体文本)。网络错误返回 (0, 错误信息)。"""
    req = urllib.request.Request(url, method=method)
    if token:
        req.add_header("Authorization", f"Bearer {token}")
    req.add_header("User-Agent", "meeting-prep-agent-check")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return resp.status, resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.read().decode("utf-8", "replace")
    except Exception as e:
        return 0, f"{type(e).__name__}: {e}"


def section(title):
    print(f"\n{'=' * 62}")
    print(f"  {title}")
    print("=" * 62)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--token", help="要检查的 token")
    args = ap.parse_args()

    token = args.token
    if not token:
        for env in ("GITHUB_TOKEN", "GH_TOKEN"):
            v = os.environ.get(env, "").strip()
            if v:
                token = v
                print(f"（用环境变量 {env} 的 token）")
                break

    ok_count = 0
    fail_count = 0

    # ========== 1. 本机状态 ==========
    section("1. 本机状态")

    def git(*a):
        p = subprocess.run(["git", *a], cwd=PROJECT_ROOT,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, (p.stdout or "") + (p.stderr or "")

    rc, out = git("log", "--oneline", "-1")
    if rc == 0:
        print(f"[OK] git 仓库正常，最新提交 {out.strip()}")
        ok_count += 1
    else:
        print(f"[!!] git 仓库异常：{out.strip()[:120]}")
        fail_count += 1

    rc, out = git("status", "--short")
    dirty = [ln for ln in out.splitlines() if ln.strip()]
    if dirty:
        print(f"[!!] 有未提交的改动（{len(dirty)} 项）—— 先提交再推")
        for ln in dirty[:8]:
            print(f"      {ln}")
        fail_count += 1
    else:
        print("[OK] 工作区干净，所有改动已提交")
        ok_count += 1

    rc, out = git("log", "--oneline", "origin/master..master")
    n = len([ln for ln in out.splitlines() if ln.strip()])
    rc2, out2 = git("diff", "--name-only", "origin/master", "master")
    nf = len([ln for ln in out2.splitlines() if ln.strip()])
    print(f"[..] 待推内容：{n} 个提交，{nf} 个文件")
    if n:
        for ln in out.splitlines():
            if ln.strip():
                print(f"      {ln}")

    # ========== 2. 网络 ==========
    section("2. 网络连通性")

    code, _ = http(f"{API}/rate_limit")
    if code == 200:
        print("[OK] api.github.com 通（git push 走不通时靠它）")
        ok_count += 1
    else:
        print(f"[!!] api.github.com 不通（{code}）—— API 通道不可用")
        fail_count += 1

    code, _ = http("https://github.com", timeout=20)
    if code == 200:
        print("[OK] github.com 通（普通 git push 可用）")
        ok_count += 1
    else:
        print(f"[!!] github.com 不通（{code or '超时'}）")
        print("     → git push 走不通，脚本会自动降级到 Git Data API")
        print("     → 这是网络/代理问题，不是代码或权限问题")

    # ========== 3. token 身份 ==========
    section("3. token 身份")
    if not token:
        print("[!!] 没提供 token，无法检查权限")
        print("     → 拿 token：https://github.com/settings/personal-access-tokens")
        fail_count += 1
    else:
        code, body = http(f"{API}/user", token)
        if code == 200:
            login = json.loads(body).get("login")
            print(f"[OK] token 有效，身份 {login}")
            ok_count += 1
        else:
            print(f"[!!] token 无效或已过期（{code}）")
            print("     → 重新生成：https://github.com/settings/personal-access-tokens")
            fail_count += 1
            token = None

    # ========== 4. 仓库可见性 ==========
    if token:
        section("4. 仓库访问权限")

        code, body = http(f"{API}/repos/{OWNER}/{REPO_NAME}", token)
        if code == 200:
            d = json.loads(body)
            print(f"[OK] 你的仓库可见：{d['full_name']}（private={d['private']}）")
            ok_count += 1
        elif code == 404:
            print("[!!] 你的仓库返回 404 —— 仓库对当前 token 完全不可见")
            print()
            print("     两种可能：")
            print("     A) token 的 Repository access 没勾它")
            print("        → https://github.com/settings/personal-access-tokens")
            print("          → Edit → Repository access → 加上 meeting-prep-agent")
            print("     B) 仓库被删了或改名了")
            print("        → https://github.com/orange1029?tab=repositories 确认")
            print()
            print("     顺带说明：403 和 404 是两回事——")
            print("       403 = 仓库可见但没写权限（加权限即可）")
            print("       404 = 仓库完全不可见（PAT 没勾选）")
            fail_count += 1
        else:
            print(f"[!!] 访问异常（{code}）：{body[:150]}")
            fail_count += 1

        # 写权限
        if code == 200:
            code2, body2 = http(
                f"{API}/repos/{OWNER}/{REPO_NAME}/contents/.probe.txt",
                token, method="PUT")
            if code2 in (403, 404):
                print("[!!] 没有写权限（Contents 权限需 Read and write）")
                print("     → token 设置里勾 Contents: Read and write")
                fail_count += 1
            else:
                print("[OK] 有写权限")

        # 可见仓库列表
        code3, body3 = http(
            f"{API}/user/repos?per_page=100&affiliation=owner", token)
        if code3 == 200:
            repos = [r["full_name"] for r in json.loads(body3)]
            print(f"\n     token 能看到的 owner 仓库（{len(repos)} 个）：")
            for r in repos:
                mark = " ← 目标" if REPO_NAME in r else ""
                print(f"       {r}{mark}")
            if not any(REPO_NAME in r for r in repos):
                print("     ⚠ 目标不在列表里 → 确认 PAT 的 Repository access")

        # ========== 5. 上游 PR ==========
        section("5. 上游 LangGraph PR")
        code, body = http(f"{API}/repos/{UPSTREAM}/pulls/{PR_NUMBER}", token)
        if code == 200:
            d = json.loads(body)
            print(f"[OK] PR #{PR_NUMBER} 可读，state={d['state']}")
            if d["state"] == "closed":
                print("     → 被 require_issue_link 机器人自动关闭")
                print("     → 被 assign 到 #9319 后会自动重开")
        else:
            print(f"[!!] 读 PR 失败（{code}）")

        code, body = http(f"{API}/repos/{UPSTREAM}/issues/{ISSUE_NUMBER}", token)
        if code == 200:
            d = json.loads(body)
            assignees = [a["login"] for a in (d.get("assignees") or [])]
            print(f"[OK] Issue #{ISSUE_NUMBER} 可读，assignees={assignees or '无'}")
            if not assignees:
                print("     → 还没被 assign，需要在 issue 下留言请求")

        # 留言写权限
        code, _ = http(f"{API}/repos/{UPSTREAM}/issues/{ISSUE_NUMBER}/comments",
                       token, method="POST")
        if code == 403:
            print("[!!] 对上游仓库无写权限（正常——外部仓库的 fine-grained PAT 默认只读）")
            print("     → 改 PR 正文/留言需要你的**账号**权限在网页上操作")
            print("     → 或把上游仓库也加进 PAT 的 Repository access")
        elif code == 200:
            print("[OK] 对上游有写权限")

    # ========== 总结 ==========
    section("总结")
    print(f"  通过 {ok_count} 项，问题 {fail_count} 项")
    print()
    if fail_count == 0:
        print("  所有检查通过，可以推送：")
        print('    python pr_langgraph/push_all.py --token <token>')
    else:
        print("  按上面的提示逐项处理。处理完重跑本脚本复查。")
        print()
        print("  快速手动方案（不改 token 也能做）：")
        print("    PR 正文   → pr_langgraph/PR_DESCRIPTION.md")
        print("    issue 留言 → pr_langgraph/submit_pr_update.py 的 ISSUE_COMMENT")
        print("    贴到网页评论区，用的是账号权限，不受 token 限制。")


if __name__ == "__main__":
    main()
