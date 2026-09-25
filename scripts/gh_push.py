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

空仓库（首次推送）是单独一支：树不加 base_tree、提交不带 parent、ref 走 POST 创建。

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
    ap.add_argument("--branch", default=None,
                    help="目标分支，默认取本地当前分支（不是硬编码 main —— "
                         "本地是 master 时推 main 会推错分支）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    R = os.path.abspath(args.repo_dir)
    if not os.path.isdir(os.path.join(R, ".git")):
        print("!! 不是 git 仓库: %s" % R)
        return 1

    # 分支：默认跟随本地当前分支
    if not args.branch:
        try:
            args.branch = run(["git", "-C", R, "branch", "--show-current"]) or "main"
        except RuntimeError:
            args.branch = "main"

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

    # 只推**已提交**的内容（本脚本推送的是 HEAD 这棵树）。
    # 未提交的改动必须显式告知 —— 否则用户改完文件直接跑，会被静默忽略、
    # 还误以为"推上去了"（本坑实际踩过：dry-run 报 0 差异，实推却 422）。
    dirty = run(["git", "-C", R, "status", "--porcelain"])
    if dirty:
        n = len([l for l in dirty.split("\n") if l.strip()])
        print("!! 注意    : 工作区有 %d 处未提交改动，**不会被推送**（本脚本只推 HEAD）。" % n)
        print("             如需推送请先 git add + git commit，再重跑。")
        for l in dirty.split("\n")[:8]:
            if l.strip():
                print("             %s" % l)

    # 远端当前状态
    # 空仓库（还没任何提交）时**整个 Git Data API 都被禁用**（git/blobs 也返回 409）。
    # 解法：先用 Contents API PUT 一个文件做出"种子提交"，仓库随即变为非空，
    # 剩余文件再走 Git Data API。最终内容仍以 tree 比对为准。
    empty_repo = False
    try:
        remote_commit = gh_json(["repos/%s/commits/%s" % (slug, args.branch)])
    except RuntimeError as e:
        msg = str(e)
        if "409" in msg or "Git Repository is empty" in msg:
            empty_repo = True
            print("远端 HEAD : （空仓库，需先落种子提交）")
        else:
            print("!! 读远端 %s 失败：%s" % (args.branch, e))
            return 1

    if empty_repo:
        # 挑一个文件做种子：优先 README.md / SKILL.md，让首屏有内容
        local_paths = [p for p in
                       run(["git", "-C", R, "ls-tree", "-r", "--name-only", local_head]).split("\n") if p]
        if not local_paths:
            print("!! 本地仓库没有任何文件，无法推送。")
            return 1
        pick = next((x for x in ("README.md", "SKILL.md") if x in local_paths), local_paths[0])
        seed_data = run_bytes(["git", "-C", R, "cat-file", "blob", "%s:%s" % (local_head, pick)])
        # 注意：空仓库还没有任何分支，payload 里**不能带 branch**——
        # 带上会得到 HTTP 404（分支不存在）。省略 branch 时 GitHub 会自建默认分支。
        seed_payload = {
            "message": "chore: 仓库初始化（gh_push.py 种子提交）",
            "content": base64.b64encode(seed_data).decode(),
        }
        fp = os.path.join(tempfile.mkdtemp(prefix="ghpush_seed_"), "seed.json")
        with open(fp, "w", encoding="utf-8") as f:
            json.dump(seed_payload, f)
        gh(["repos/%s/contents/%s" % (slug, pick)], payload_path=fp, jq=".commit.sha")
        print("种子提交  : %s（Contents API，空仓库唯一可用通道）" % pick)
        # 仓库已非空，重新读取远端状态
        remote_commit = gh_json(["repos/%s/commits/%s" % (slug, args.branch)])
        empty_repo = False

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
        # 注意：空树（4b825dc6…）在 GitHub 上是不存在的对象，查它会 404。
        # 远端只剩空目录时就会走到这条路径，必须当作"没有任何文件"处理。
        EMPTY_TREE = "4b825dc642cb6eb9a060e54bf8d69288fbee4904"
        remote_files = {}
        def walk(tree_sha, prefix=""):
            if tree_sha == EMPTY_TREE:
                return
            try:
                data = gh_json(["repos/%s/git/trees/%s" % (slug, tree_sha)])
            except RuntimeError as e:
                if "404" in str(e):
                    return          # 空树 / 已不存在的对象，视为无内容
                raise
            for e in data.get("tree", []):
                p = prefix + e["path"]
                if e["type"] == "tree":
                    if e["sha"] != EMPTY_TREE:
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

    print("\n提示：本地与远端 SHA 不同是正常的（Git Data API 建的提交，committer 信息不同）。")
    print("     判断是否成功看 tree，不要比 commit SHA。")
    print("     网络恢复后对齐本地跟踪引用（在此之前 fetch 不通，那条命令也跑不了）：")
    print("       git fetch %s && git reset --soft %s/%s" % (args.remote, args.remote, args.branch))

    return 0 if (all_ok and new_tree == local_tree) else 1


if __name__ == "__main__":
    sys.exit(main())
