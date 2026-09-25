#!/usr/bin/env python3
"""git push 网关不通时，经 gh api 的 Git Data API 把本地提交推上 GitHub。

为什么需要它：本机 git 走 https 常撞网关 `Connection was reset` / 502，
但 `gh api` 走另一条通道稳定可用。

用法：
    python gh_push.py <repo_dir> [--remote origin] [--branch main] [--dry-run]

行为：
  1. 找出「本地 HEAD 与远端 branch」的差异文件（git diff --name-status）
  2. 用 `git cat-file blob <sha>:<path>` 取**仓库规范化内容**（LF 行尾）建 blob
  3. 组装 tree（base_tree = 远端当前 tree）→ commit（parent = 远端当前 commit，**完整 40 位**）
  4. PATCH 更新 refs/heads/<branch>（默认 force=false，即要求快进）
  5. 校验：逐个比对「上传的 blob SHA」与「本地 git blob SHA」，再比对远端 tree 与本地 tree

退出码：0 成功（含校验一致），1 失败。
"""
import argparse
import base64
import json
import subprocess
import sys
import tempfile
import os


def run(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True, text=True)
    if check and p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s%s" % (" ".join(cmd), p.stdout, p.stderr))
    return p.stdout.strip()


def run_bytes(cmd, cwd=None, check=True):
    p = subprocess.run(cmd, cwd=cwd, capture_output=True)
    if check and p.returncode != 0:
        raise RuntimeError("命令失败: %s\n%s" % (" ".join(cmd), p.stderr.decode(errors="replace")))
    return p.stdout


def gh(args, payload_path=None, jq=None):
    """调用 gh api。payload_path 为 JSON 文件路径（--input）。"""
    cmd = ["gh", "api"] + args
    if payload_path:
        cmd += ["--input", payload_path]
    if jq:
        cmd += ["--jq", jq]
    out = run(cmd)
    return out


def gh_json(args, payload_path=None):
    out = gh(args, payload_path, jq=".")
    return json.loads(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("repo_dir")
    ap.add_argument("--remote", default="origin")
    ap.add_argument("--branch", default="main")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    R = os.path.abspath(args.repo_dir)
    if not os.path.isdir(os.path.join(R, ".git")):
        print("!! 不是 git 仓库: %s" % R)
        return 1

    # 从 remote url 推断 owner/repo
    url = run(["git", "-C", R, "remote", "get-url", args.remote])
    slug = url.rstrip("/").removesuffix(".git")
    for sep in ("github.com/", "github.com:"):
        if sep in slug:
            slug = slug.split(sep, 1)[1]
            break
    if "/" not in slug:
        print("!! 无法从 remote url 推断 owner/repo: %s" % url)
        return 1
    print("仓库      : %s" % slug)
    print("分支      : %s" % args.branch)

    local_head = run(["git", "-C", R, "rev-parse", "HEAD"])
    local_short = run(["git", "-C", R, "rev-parse", "--short", "HEAD"])
    print("本地 HEAD : %s" % local_short)

    # 远端当前状态
    try:
        remote_commit = gh_json(["repos/%s/commits/%s" % (slug, args.branch)])
    except RuntimeError as e:
        print("!! 读远端 %s 失败：%s" % (args.branch, e))
        return 1
    remote_sha = remote_commit["sha"]          # 完整 40 位
    remote_tree = remote_commit["commit"]["tree"]["sha"]
    remote_msg = remote_commit["commit"]["message"].split("\n")[0]
    print("远端 HEAD : %s  %s" % (remote_sha[:7], remote_msg))

    if remote_sha == local_head:
        print("\n远端已与本地一致，无需推送。")
        return 0

    # 差异文件（本地 HEAD 相对远端 commit）
    # 两套算法：
    #   A. 远端 commit 已在本地对象库 → git diff 直接算（快、准）
    #   B. 不在（本机 git fetch 不通时的常态）→ 列出本地 HEAD 全部文件，
    #      与远端 tree 逐文件比 blob SHA。这是兜底路径，代价是文件多时较慢，
    #      但**不依赖 fetch**，正是这个脚本存在的前提。
    base = None
    try:
        run(["git", "-C", R, "cat-file", "-e", remote_sha + "^{commit}"])
        base = remote_sha
    except RuntimeError:
        print("说明      : 远端 commit 不在本地对象库（fetch 不通），改用逐文件比对远端 tree")

    if base:
        status = run(["git", "-C", R, "diff", "--name-status", base, local_head])
        if not status:
            print("!! 无文件差异（可能只是提交元信息不同）。如需强推请手工处理。")
            return 1
        files = []          # (status_char, path)
        for line in status.split("\n"):
            parts = line.split("\t")
            if len(parts) >= 2:
                files.append((parts[0][0], parts[-1]))
    else:
        # 递归拉取远端 tree → {path: blob_sha}
        remote_files = {}
        def walk(tree_sha, prefix=""):
            data = gh_json(["repos/%s/git/trees/%s" % (slug, tree_sha)])
            for e in data.get("tree", []):
                p = prefix + e["path"]
                if e["type"] == "tree":
                    walk(e["sha"], p + "/")
                elif e["type"] == "blob":
                    remote_files[p] = e["sha"]
        walk(remote_tree)

        local_paths = [p for p in
                       run(["git", "-C", R, "ls-tree", "-r", "--name-only", local_head]).split("\n") if p]
        files = []
        for p in local_paths:
            lsha = run(["git", "-C", R, "rev-parse", "%s:%s" % (local_head, p)])
            if remote_files.get(p) != lsha:
                files.append(("M" if p in remote_files else "A", p))
        for p in remote_files:
            if p not in local_paths:
                files.append(("D", p))

    print("差异文件  : %d 个" % len(files))

    if args.dry_run:
        for st, p in files:
            print("   %s  %s" % (st, p))
        print("\n[dry-run] 未执行上传。")
        return 0

    tmp = tempfile.mkdtemp(prefix="ghpush_")

    def post(path, payload):
        fp = os.path.join(tmp, "payload.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(payload, f)
        return gh_json(["repos/%s/%s" % (slug, path)], payload_path=fp)

    print("\n[1/4] 创建 blob（用 git 仓库规范化内容，避免 CRLF 污染）")
    tree = []
    all_ok = True
    for st, path in files:
        if st == "D":
            # 删除：tree 里以 sha=null 表示
            tree.append({"path": path, "mode": "100644", "type": "blob", "sha": None})
            print("   D  %s（删除）" % path)
            continue
        # 关键：用仓库内规范化内容（LF），不要读工作区文件（可能是 CRLF）
        data = run_bytes(["git", "-C", R, "cat-file", "blob", "%s:%s" % (local_head, path)])
        expect = run(["git", "-C", R, "rev-parse", "%s:%s" % (local_head, path)])
        blob = post("git/blobs",
                    {"content": base64.b64encode(data).decode(), "encoding": "base64"})
        sha = blob["sha"]
        mark = "OK" if sha == expect else "!! 与本地 blob 不符 期望 %s" % expect
        if sha != expect:
            all_ok = False
        print("   %s  %s -> %s  %s" % (st, path, sha[:10], mark))
        tree.append({"path": path, "mode": "100644", "type": "blob", "sha": sha})

    print("\n[2/4] 创建 tree")
    new_tree = post("git/trees", {"base_tree": remote_tree, "tree": tree})["sha"]
    local_tree = run(["git", "-C", R, "rev-parse", "%s^{tree}" % local_head])
    print("   远端新 tree: %s" % new_tree)
    print("   本地   tree: %s  %s" % (local_tree, "一致" if new_tree == local_tree else "不一致"))

    print("\n[3/4] 创建 commit")
    msg = run(["git", "-C", R, "log", "-1", "--pretty=%B"])
    commit = post("git/commits",
                  {"message": msg, "tree": new_tree, "parents": [remote_sha]})
    new_commit = commit["sha"]
    print("   新 commit: %s" % new_commit)
    print("   本地 HEAD: %s  %s" % (local_head, "SHA 一致" if new_commit == local_head else "SHA 不同（内容相同即可，API 建的提交不受本地 committer 影响）"))

    print("\n[4/4] 更新 refs/heads/%s" % args.branch)
    ref = post("git/refs/heads/%s" % args.branch, {"sha": new_commit, "force": False})
    print("   引用已指向: %s" % ref["object"]["sha"][:7])

    print("\n=== 校验 ===")
    check = gh_json(["repos/%s/commits/%s" % (slug, args.branch)])
    print("   远端 %s : %s  %s" % (args.branch, check["sha"][:7],
                                  check["commit"]["message"].split("\n")[0]))
    print("   远端 tree: %s" % check["commit"]["tree"]["sha"])
    print("   本地 tree: %s" % local_tree)
    if check["commit"]["tree"]["sha"] == local_tree:
        print("\n内容一致。")
    else:
        print("\n!! tree 不一致，请检查是否有行尾/过滤规则差异。")

    print("\n提示：本地与远端 SHA 不同是正常的（Git Data API 建的提交）。")
    print("如需本地跟踪引用对齐，网络恢复后执行：")
    print("  git fetch %s && git reset --soft %s/%s" % (args.remote, args.remote, args.branch))

    return 0 if (all_ok and new_tree == local_tree) else 1


if __name__ == "__main__":
    sys.exit(main())
