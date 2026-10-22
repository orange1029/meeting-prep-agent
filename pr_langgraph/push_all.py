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
import base64
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

OWNER = "orange1029"
REPO_NAME = "meeting-prep-agent"

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


def diagnose_repo_access(token, owner, name):
    """诊断对某个仓库的访问权限，区分 403 和 404。

    这个区分很关键，两种情况的原因完全不同：

    · **403**（Resource not accessible by personal access token）
      仓库**存在且可见**，只是没给写权限。
      修法：给 token 加 Contents: Read and write / Pull requests: Read and write。

    · **404**（Not Found）
      仓库**完全不可见**。私有仓库在 fine-grained PAT（细粒度令牌）
      下如果没在创建时勾选，就是完全不可见——连存在都查不到。
      修法：去 token 的设置页把该仓库加进「Repository access」。

    2026-10-21 实测记录：
      同一个 token，对 fde-checklist 报 403（可见无写权限），
      对 meeting-prep-agent 报 404（完全不可见）——
      说明那个 PAT 的仓库授权清单里没勾 meeting-prep-agent。
    """
    print(f"\n  诊断 {owner}/{name} 的访问权限：")
    try:
        api("GET", f"/repos/{owner}/{name}", token)
        print("    读：✓ 可见")
    except SystemExit as e:
        msg = str(e)
        if "404" in msg:
            print("    读：✗ 404 —— 仓库不可见")
            print("      （私有仓库 + fine-grained PAT 未勾选该仓库 = 完全不可见）")
        else:
            print(f"    读：✗ {msg.splitlines()[0]}")
        return "404"

    # 可见，写权限单独测（用一个必定失败的 sha，能区分 403 和 404）
    try:
        api("PUT", f"/repos/{owner}/{name}/contents/.perm_probe.txt", token,
            {"message": "probe", "content": "cHJvYmU=", "branch": "main",
             "sha": "0" * 40})
        print("    写：✓ 有写权限")
    except SystemExit as e:
        msg = str(e)
        if "403" in msg:
            print("    写：✗ 403 —— 可见但无写权限")
            print("      （token 需要加 Contents: Read and write 权限）")
        elif "404" in msg:
            print("    写：✗ 404 —— 分支或路径不存在（可能正常）")
        else:
            print(f"    写：? {msg.splitlines()[0]}")
    return "403"


def git(*args, timeout=120):
    """跑一条 git 命令。

    ★ 关键：加 `-c core.quotepath=false`。
      git 默认（quotepath=true）会把非 ASCII 文件名转义成八进制，
      也就是「交付工程说明.md」会变成
      "\"\\344\\272\\244\\344\\272\\244...\""
      后面 `git show master:<那个路径>` 就找不到文件了——
      中文文件名的文件会**静默推不上去**，而且不报错。

      这个坑是 2026-10-22 实测发现的：
      降级推送后 tree 校验不一致，排查半天才发现是中文文件名。
    """
    p = subprocess.run(["git", "-c", "core.quotepath=false", *args],
                       cwd=PROJECT_ROOT, capture_output=True, text=True,
                       timeout=timeout)
    return p.returncode, (p.stdout or "") + (p.stderr or "")


# ============ Git Data API 通道（github.com 不通时的唯一出路）============
# 网络事实（2026-10-21 实测）：
#   api.github.com  → 200，1.2s   ✓
#   github.com      → 000，10s 超时 ✗
# 代理只放行了前者。而 git push / git credential fill / GCM 刷新
# 全都走 github.com，所以在当前网络下普通 push 一定失败。
#
# Git Data API 走 api.github.com，能完成同样的事：
#   blob → tree → commit → PATCH ref
# 这是 2026-10-15 那次就用成功过的路径（见当日记忆）。

def api_blob(token, content_bytes):
    b64 = base64.b64encode(content_bytes).decode("ascii")
    r = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/blobs", token,
            {"content": b64, "encoding": "base64"})
    return r["sha"]


def api_tree(token, base_tree, entries):
    """entries: [{"path": ..., "mode": ..., "type": "blob", "sha": ...}]"""
    r = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/trees", token,
            {"base_tree": base_tree, "tree": entries})
    if r.get("truncated"):
        raise SystemExit("✗ tree 被截断，文件太多，需要分批提交")
    return r["sha"]


def api_commit(token, tree_sha, message, parents):
    r = api("POST", f"/repos/{OWNER}/{REPO_NAME}/git/commits", token, {
        "message": message,
        "tree": tree_sha,
        "parents": parents,
    })
    return r["sha"]


def api_update_ref(token, sha, ref="heads/master"):
    r = api("PATCH", f"/repos/{OWNER}/{REPO_NAME}/git/refs/{ref}", token,
            {"sha": sha, "force": False})
    return r.get("object", {}).get("sha")


def push_via_api(token, dry_run=False):
    """用 Git Data API 推代码。

    注意一个已知坑（2026-10-15 踩过）：
    API 建的 commit sha 和本地 sha 不一样——GitHub 会规范化 author 邮箱大小写
    （outlook.com → Outlook.com），commit 哈希因此不同。
    **内容正确 > sha 一致**。判断推成功与否，看 tree sha 是否相同，
    不要强行对齐 commit sha。

    而且 Git Data API 一次只能推一个 commit（一个 tree），
    所以这里把待推的多个 commit 压成**一个** commit——
    对「同步本地成果到远端」这个目的来说完全够用，
    本地的历史分支还留在本地 git 里。
    """
    print("\n  改用 Git Data API 推送（github.com 不通时的唯一出路）")

    # ⚠️ 先查远端是否已经同步了。
    # 这个检查是必须的：2026-10-21 实测踩过——
    # 普通 git push 成功了（但我误判成失败），接着又跑降级通道，
    # 于是同一个内容被推了两遍，远端多出一条内容完全相同的空转 commit。
    #
    # 判断标准：远端 master 的 tree == 本地 master 的 tree，
    # 说明内容已经一致，不需要再推。
    rc, local_tree = git("rev-parse", "master^{tree}")
    local_tree = local_tree.strip()
    try:
        ref = api("GET", f"/repos/{OWNER}/{REPO_NAME}/git/ref/heads/master",
                  token)
        remote_commit_sha = ref["object"]["sha"]
        remote_commit = api(
            "GET",
            f"/repos/{OWNER}/{REPO_NAME}/git/commits/{remote_commit_sha}",
            token)
        remote_tree_sha = remote_commit["tree"]["sha"]
    except SystemExit:
        remote_tree_sha = None
    except (KeyError, TypeError):
        remote_tree_sha = None

    if remote_tree_sha == local_tree:
        print(f"  ✓ 远端内容已与本地一致（tree {local_tree[:10]}），无需再推")
        return True
    if remote_tree_sha:
        print(f"  远端 tree {remote_tree_sha[:10]} ≠ 本地 {local_tree[:10]}"
              f"，需要推")

    # 待推的 commit 列表
    rc, out = git("rev-list", "--reverse", "origin/master..master")
    commits = [c for c in out.split() if c]
    if not commits and not remote_tree_sha:
        print("  没有待推的 commit")
        return True

    # ★ 收集要推的文件：直接跟**远端实际 tree** 对比，
    #   不用本地缓存的 origin/master 做基准。
    #
    #   为什么：origin/master 是 git fetch 时的快照，
    #   在 github.com 不通（fetch 失败）时它会一直停在旧值。
    #   2026-10-22 实测踩过：远端已经推了新 commit，本地 origin/master
    #   还是旧的，diff 出来的文件清单跟远端实际状态对不上，
    #   结果有个中文名文件没推上去，tree 校验不一致才发现。
    #
    #   正确做法：列出远端 tree 里的所有文件，逐个跟本地比对 blob sha。
    files = []
    if remote_tree_sha:
        # 远端 tree 的文件清单
        # 注意：api() 返回的是 dict，不是 (状态码, 内容) 元组
        tree_json = api(
            "GET",
            f"/repos/{OWNER}/{REPO_NAME}/git/trees/{remote_tree_sha}?recursive=1",
            token)
        if not isinstance(tree_json, dict):
            tree_json = {}
        远端文件 = {}
        for 项 in tree_json.get("tree", []):
            if 项.get("type") == "blob":
                远端文件[项["path"]] = 项["sha"]

        # 本地追踪的文件 + 它们的 blob sha
        rc, out = git("ls-files", "-s")
        本地文件 = {}
        for 行 in out.splitlines():
            段 = 行.split(None, 3)
            if len(段) >= 4 and 段[3].strip():
                本地文件[段[3].strip()] = 段[1]

        # 远端有本地没有的 → 本地删了，要同步删除
        for 路径 in sorted(set(远端文件) - set(本地文件)):
            files.append(("删除", 路径))

        # 本地有、blob sha 不同的 → 要推
        for 路径 in sorted(set(本地文件) - set(远端文件)):
            files.append(("新增", 路径))
        for 路径 in sorted(set(本地文件) & set(远端文件)):
            if 本地文件[路径] != 远端文件[路径]:
                files.append(("修改", 路径))
    else:
        # 拿不到远端 tree，退回用本地缓存
        rc, out = git("diff", "--name-only", "origin/master", "master")
        files = [("修改", f) for f in out.splitlines() if f.strip()]

    if not files:
        print("  没有文件变更")
        return True

    print(f"  {len(commits)} 个 commit，{len(files)} 个文件")
    for 动作, 路径 in files:
        print(f"    {动作} {路径}")

    if dry_run:
        return True

    # 远端当前 HEAD
    ref = api("GET", f"/repos/{OWNER}/{REPO_NAME}/git/ref/heads/master", token)
    base_commit_sha = ref["object"]["sha"]
    base_commit = api("GET",
                      f"/repos/{OWNER}/{REPO_NAME}/git/commits/{base_commit_sha}",
                      token)
    base_tree = base_commit["tree"]["sha"]

    # 逐个文件建 blob
    entries = []
    for 动作, path in files:
        if 动作 == "删除":
            # 删除：tree 条目里 sha 传 null 就是删除
            entries.append({"path": path, "mode": "100644", "type": "blob",
                            "sha": None})
            print(f"    删除 {path}")
            continue

        rc, content = git("show", f"master:{path}")
        if rc != 0:
            print(f"  ⚠ 跳过 {path}（读取失败）")
            continue
        # mode 要跟远端现有的一致；新文件用 100644
        mode = "100644"
        rc, ls = git("ls-tree", "master", "--", path)
        if rc == 0 and ls.strip():
            mode = ls.split()[0]
        sha = api_blob(token, content.encode("utf-8"))
        entries.append({"path": path, "mode": mode, "type": "blob", "sha": sha})
        print(f"    blob {path}")

    # 建 tree
    tree_sha = api_tree(token, base_tree, entries)

    # 建 commit：消息取本地最后一个 commit 的
    rc, msg = git("log", "-1", "--format=%B", "master")
    new_commit = api_commit(token, tree_sha, msg.strip() or "sync", [base_commit_sha])
    print(f"  commit {new_commit[:10]}（本地 sha 不同属正常："
          f"GitHub 会规范化邮箱大小写）")

    # 更新 ref
    new_ref = api_update_ref(token, new_commit)
    print(f"  ✓ 已推送，master 现在是 {new_ref[:10]}")

    # 验证：tree sha 相同即内容一致
    rc, local_tree = git("rev-parse", "master^{tree}")
    local_tree = local_tree.strip()
    ok = local_tree == tree_sha
    print(f"  内容校验：本地 tree {local_tree[:10]} vs 远端 {tree_sha[:10]}"
          f" → {'一致 ✓' if ok else '不一致 ✗'}")
    if not ok:
        print("  注意：tree 不一致说明有文件没推上去，需要核对")
    return ok


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
        elif args.dry_run:
            for ln in out.splitlines():
                if ln.strip():
                    print(f"    {ln}")
            print("  DRY RUN：跳过实际 push")
        else:
            # 先试普通 git push。走 github.com 域名，
            # 在代理只放行 api.github.com 的网络下会失败——
            # 失败就自动降级到 Git Data API。
            url = (f"https://orange1029:{token}@github.com/"
                   f"{OWNER}/{REPO_NAME}.git")
            rc, out = git("push", url, "master", timeout=90)
            # 无论成败都把 remote URL 还原，不留明文 token
            git("remote", "set-url", "origin",
                f"https://github.com/{OWNER}/{REPO_NAME}.git")

            if rc == 0:
                print("  ✓ git push 成功")
            else:
                head = (out or "").strip().splitlines()
                print(f"  ✗ git push 失败：{head[-1] if head else '未知错误'}")
                print("    （github.com 走不通是常见原因，"
                      "自动改用 Git Data API）")
                if not push_via_api(token, dry_run=args.dry_run):
                    raise SystemExit("✗ Git Data API 推送也失败了")

    # ---- 2. 更新 PR 正文 ----
    print("\n[2/3] 更新 LangGraph PR 正文")
    with open(PR_BODY_PATH, "r", encoding="utf-8") as fh:
        body = fh.read()
    pr = api("GET", f"/repos/{REPO}/pulls/{PR_NUMBER}", token)
    print(f"  PR #{PR_NUMBER} 当前 state={pr.get('state')}")

    try:
        if body.strip() == (pr.get("body") or "").strip():
            print("  已是最新，跳过")
        elif args.dry_run:
            print(f"  DRY RUN：将更新 {len(body)} 字符")
        else:
            api("PATCH", f"/repos/{REPO}/pulls/{PR_NUMBER}", token, {"body": body})
            print("  ✓ 已更新")
    except SystemExit as e:
        msg = str(e)
        if "403" in msg:
            print("  ✗ 403 —— token 没有改上游 PR 的权限")
            print("    上游 langchain-ai/langgraph 是**外部仓库**，")
            print("    fine-grained PAT 默认对它无写权限。")
            print("    这不是配置问题：改 PR 正文需要上游授予")
            print("    「Pull requests: Write」，而这只有在 PR 被重新打开、")
            print("    且你被 assign 到 #9319 之后才可能。")
            print("    → 先在 #9319 留言请求 assign（见第 3 步），")
            print("      PR 正文随之就能改。")
        else:
            raise

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

    # ---- 4. 诊断自己的仓库（推送代码需要）----
    if not args.skip_push:
        print("\n[附加] 诊断自己的仓库权限")
        diagnose_repo_access(token, OWNER, REPO_NAME)

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
