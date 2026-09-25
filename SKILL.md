---
name: github-push-fallback
description: Use when `git push` / `git fetch` fails against GitHub with network errors ("Connection was reset", "Failed to connect to github.com:443", 502 via proxy, "CONNECT tunnel failed", HTTP 000) while the `gh` CLI still works. Pushes local commits to GitHub through the `gh api` Git Data channel instead, preserving real commit history. Also use when a GitHub push must be verified byte-for-byte (remote tree vs local tree), or when remote changes must land without a working `git fetch`.
agent_created: true
---

# GitHub 推送兜底（git push 不通时走 gh api）

## 何时用

`git push` / `git fetch` 撞网络错误，但 `gh api` 可用时。典型报错：

- `fatal: unable to access 'https://github.com/...': Recv failure: Connection was reset`
- `fatal: Failed to connect to github.com:443 after 21142 ms: Could not connect to server`
- 代理隧道 `CONNECT tunnel failed, response 502`、`HTTP 000`

先确认 `gh` 通道是好的——这一步决定走哪条路：

```bash
gh auth status                                        # 应为 Logged in
gh api repos/<owner>/<repo> --jq .full_name           # 能通说明 gh 通道可用
```

若 `gh api` 也失败，本技能不适用，需排查网络或代理本身。

## 首选：一条命令

```bash
python <skill>/scripts/gh_push.py <repo_dir>
```

脚本自动完成：差异识别 → 建 blob → 组装 tree → 建 commit → 更新 ref → 校验。

```bash
python gh_push.py <repo_dir> --dry-run           # 只看差异，不上传
python gh_push.py <repo_dir> --branch main       # 指定分支
python gh_push.py <repo_dir> --remote origin     # 指定远端名
```

**先跑 `--dry-run`**，确认差异文件列表符合预期再实推。若列表里有没预期的文件
（尤其是行尾相关的整文件重写），先查清原因。

退出码 0 表示「每个 blob SHA 都与本地 git blob 相符，且远端 tree == 本地 tree」。

## 必须知道的八个坑

这八条都实际踩过，脚本已自动处理，但排查问题时需要理解。

### ① `parents` 必须是完整 40 位 SHA

传短 SHA 会得到 `HTTP 422 Each SHA in the 'parents' parameter must be exactly 40 characters`。
脚本用 `gh api repos/.../commits/<branch> --jq .sha` 取全量 SHA。
（同类坑：`gh release create <tag> --target <sha>` 的 sha 也必须是完整 40 位。）

### ② 不能用工作区文件建 blob——行尾会污染

工作区文件是 **CRLF**，git 仓库内是 **LF**。直接 base64 上传工作区文件，远端会拿到 CRLF 版本，
表现为：blob 上传"成功"但 **tree SHA 与本地不一致**。

正确做法是取仓库规范化内容：

```bash
git cat-file blob <commit>:<path>      # 仓库内形态（LF）
git rev-parse <commit>:<path>          # 期望的 blob SHA，用于逐文件校验
```

脚本对每个文件都做这个比对，不符即报错并返回退出码 1。

### ③ `gh api --jq .content` 读文件会被 base64 换行截断

读远端文件内容改用 raw：

```bash
gh api repos/<owner>/<repo>/contents/README.md -H "Accept: application/vnd.github.raw"
```

### ④ 无 fetch 时如何算差异

`git push` 不通通常意味着 `git fetch` 也不通，远端 commit 不在本地对象库，
`git diff <remote_sha> HEAD` 直接失败。脚本有两套算法：

- **A（快）**：远端 commit 在本地对象库 → `git diff --name-status`
- **B（兜底）**：不在 → 递归拉取远端 tree 建 `{path: blob_sha}` 索引，
  与本地 `git ls-tree -r` 逐文件比 **blob SHA**（不是比文件 md5！），得出 A/M/D 差异。
  代价是文件多时慢，但不依赖 fetch。

### ⑤ `gh api -f force=true` 会把布尔传成字符串

用 `-f` 传 `force=true` 会得到：

```
For 'properties/force', "true" is not a boolean. (HTTP 422)
```

`gh api` 的 `-f` 一律当字符串。布尔/数字字段必须走 JSON body：

```bash
# 写成 json 文件，用 --input 传
echo '{"sha":"<40位SHA>","force":true}' > ref.json
gh api --method PATCH repos/<owner>/<repo>/git/refs/heads/<branch> --input ref.json
```

（同一坑：`gh api ... --jq '.content'` 读文件会被 base64 换行截断，见 ③。）

### ⑥ 空仓库：整个 Git Data API 都被禁用（409）

**全新的空仓库连 `git/blobs` 都会返回 409**，不只是读提交失败：

```
{"message":"Git Repository is empty.","status":"409"}
```

所以"首次推送"不能直接走 Git Data，必须先用 **Contents API** 落一个种子文件：

```bash
# payload：{"message":"...","content":"<base64>"}  —— 注意**不要带 branch 字段**
gh api --method PUT repos/<o>/<r>/contents/README.md --input seed.json
```

**为什么不能带 `branch`**：空仓库还没有任何分支，指定 `branch: main` 会得到
`HTTP 404 Not Found`。省略 branch 时 GitHub 会自建默认分支。

种子落完仓库即非空，其余文件再走 Git Data API。脚本已内置这条分支。

### ⑦ 空树对象（`4b825dc6…`）查不到，会 404

远端最后一个文件被删掉后，远端 tree 会变成 git 的空树 SHA
`4b825dc642cb6eb9a060e54bf8d69288fbee4904`。**这个对象在 GitHub 上不存在**，
递归查它的内容会 404。脚本把 404 和空树 SHA 都当"没有任何文件"处理。

### ⑧ 默认分支不能硬编码 `main`

本地 Git 默认分支可能是 `master`（`git init` 在部分配置下就是这样）。
硬编码推 `main` 会把内容推到错误的分支上，且**远端那边看起来"推成功了"**，
不会报错——最难查的一类错误。脚本默认取 `git branch --show-current`。

### ⑨ 只推已提交的内容，未提交改动会被静默忽略

脚本推的是 **HEAD 那棵树**，不看工作区。改完文件没 commit 就跑，会出现最迷惑的组合：

- `--dry-run` 报 **0 差异**（HEAD 与远端确实一致）
- 实推时 `git/trees` 返回 **422 Invalid tree info**（blob 是旧内容的 SHA）

本坑实际踩过。脚本现在会显式打印「工作区有 N 处未提交改动，不会被推送」。
**改完文件请先 `git add && git commit`，再跑本脚本。**

## 推完之后的本地状态

Git Data API 建的提交，committer/时间戳与本地不同，所以**远端 SHA ≠ 本地 SHA**（内容相同）。
这是正常的：判断推送是否成功要看 **tree SHA 是否一致**，不要比 commit SHA。

网络恢复后对齐本地跟踪引用：

```bash
git fetch origin && git reset --soft origin/main
```

## 能力边界

- **只能快进**：默认 `force: false`。远端有本地没有的提交时会失败——那说明别人推过东西，
  应先把远端内容取回本地合并，再走本流程。别为了图快改 `force: true`。
- **不适合超大改动**：每个文件一次 API 调用（本地不产生对应提交对象）。
  改动上百个文件时优先想办法恢复 `git push`，本方案用于「必须把这次改动送上去」的场合。
- **删除文件**：`git/trees` 的语义是「base_tree + 覆盖项」，无法直接表达删除。改用 Contents API：

  ```bash
  gh api repos/<owner>/<repo>/contents/<path> --jq .sha        # 取 blob sha
  # body.json: {"message":"...","sha":"<blob sha>","branch":"main"}
  gh api --method DELETE repos/<owner>/<repo>/contents/<path> --input body.json
  ```

## 验证脚本自身

改完脚本后用真实场景验证，别只看 dry-run：

```bash
# 1. 造一个待推提交
echo "selftest" > _selftest.txt && git add _selftest.txt && git commit -m "chore: selftest"
# 2. 跑脚本（应识别 1 个新增文件，实推后校验通过）
python gh_push.py <repo_dir>
# 3. 清理：本地回退 + 远端删文件（见上文 Contents API 删法）
git reset --hard <原 HEAD>
```

理想的自检信号：**拿一个已知内容与远端一致的仓库跑 `--dry-run`，差异文件数应为 0**。
若不为 0，说明内容比对算法有偏差，先别推。

### 自检千万别留在远端历史里

实推自检会产生两个提交：自检提交 + revert 提交。它们**会永久留在远端历史**，
让 `git log` 变脏。清理办法（内容零变化，只动 ref）：

```bash
# 1. 确认目标提交的 tree 与当前一致（关键！不一致就是真丢改动）
gh api repos/<o>/<r>/commits/<目标SHA> --jq .commit.tree.sha

# 2. 把分支指回去（force 必须走 JSON body，见坑 ⑤）
gh api --method PATCH repos/<o>/<r>/git/refs/heads/<branch> --input ref.json

# 3. 校验远端 tree 仍等于本地 tree
```

**更省事的做法：别在真实仓库上实推自检。** 复制一份仓库到临时目录当靶场，
或直接拿「已知与远端一致的仓库」跑 `--dry-run`（差异数应为 0）——
`--dry-run` 已经覆盖差异算法这个最容易错的环节，实推步骤风险低得多。
