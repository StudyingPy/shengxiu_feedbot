# Agent 指南：版本与发布规范

本文档面向参与本项目的 AI agent（含 Claude Code）。**用户不需要读这个文件**——它是给我未来"自己看"的工作守则。

## 仓库布局

单 git 仓库，远端 `https://github.com/StudyingPy/shengxiu_feedbot`，主分支 `main`。版本通过 git tag 标记（如 `v0.4.2`），不再用兄弟目录或独立 zip 包。

历史遗留：项目搬到 git 之前，每个版本曾是 `feed-bot vX.Y.Z/` 兄弟目录加同名 zip。如果在 `d:\个人文件\制作\shengxiu_feedbot\` 还能看到那些目录，那是只读历史，不要动也不要按那种方式建新版本。

## 何时切新版本

只在用户**明确要求**或**累计了多处独立改动**时切。判断标准：

| 改动类型 | 处理方式 |
| --- | --- |
| 修小 bug、调阈值、改文案 | 直接 commit 到 main，**不**打新 tag（除非用户明确要回退点） |
| 新增独立功能（如本次 zip2tph） | **打 minor tag**（v0.X.0 → v0.X+1.0） |
| 涉及部署流程/配置结构变化 | **打 minor tag**，CHANGELOG 写迁移步骤，回复里单独列出新增 config key |
| 单独的小修复，但用户希望保留回退点 | **打 patch tag**（v0.X.Y → v0.X.Y+1） |

用户说"先不动了，下次再改"或"想保留回退点"——这是打 tag 的强信号，主动建议或直接执行（仍要先经用户确认 push）。

## 版本号规则

`vMAJOR.MINOR.PATCH`（git tag 形式，不带后缀）。

- **MAJOR**：架构级重构（用户表态前别动）。
- **MINOR**：新增功能 / 配置不兼容 / 部署流程变化。
- **PATCH**：纯 bug 修复、文案、阈值微调。
- **topic-slug**（可选）：3~5 个英文词描述本版主题（kebab-case），写进 tag annotation / commit message / release notes 标题里，**不进 tag 名**（保持 `git checkout v0.4.2` 干净）。

例：
- tag `v0.4.0`，annotation `v0.4.0: archive-zip-progress`
- tag `v0.4.1`，annotation `v0.4.1: archive-multithread-download`

## 切新版本的标准流程

```bash
# 1. 在 main 上把改动 commit 完，确认工作树干净
git status

# 2. 更新 pyproject.toml 的 version 与 CHANGELOG.md 顶部条目（日期用 Today's date）
# 3. commit 版本元数据
git add pyproject.toml CHANGELOG.md
git commit -m "release: v0.4.3 <topic-slug>"

# 4. 打 annotated tag（用 git show 能看到日期与说明）
git tag -a v0.4.3 -m "v0.4.3: <topic-slug>"

# 5. 推送 commit 和 tag
git push
git push origin v0.4.3
```

可选——如果想要 GitHub Releases 页面（自动 zip 附件 + RSS 订阅）：

```bash
gh release create v0.4.3 --notes-from-tag
```

不强制：只打 tag 已经够用，`/tags` 页面每个 tag 自带 "Source code (zip)" 下载链接。

## 部署指南格式

服务器侧的更新流程已写进 [README.md](README.md) 的"更新部署"段。每次发新版只需告诉用户：

```bash
cd /opt/pixiv-feed-bot
systemctl stop pixiv-feed-bot
git pull
pip install -e . --quiet     # 仅 pyproject.toml 改动时
systemctl start pixiv-feed-bot
journalctl -u pixiv-feed-bot -n 50 --no-pager
```

或者切到特定 tag：`git fetch --tags && git checkout v0.4.3`。

需要"定制化"补的，只剩两件：
- 配置结构变化（新增/重命名 key）：文末用一行列出新增 key 让用户去改 `/etc/pixiv-feed-bot/config.yaml`。**不要**自动写"编辑 config.yaml"那段——用户会自己改。
- 要求重启某个外部服务（telegram-bot-api、nginx）：单独提醒。

旧的"unzip → /tmp/feedbotfix → 选择性 cp → 备份 .bak.$(date +%Y%m%d-%H%M%S)"流程已废弃，不要再给。

## CHANGELOG 写法

`CHANGELOG.md` 顶部新加段，倒序：

```markdown
## v0.4.1 — YYYY-MM-DD

### 新增 / 变更 / 修复
- 一句话描述，带必要的为什么。
- 具体文件位置用 markdown 链接：`[handlers.py:1232](pixivfeed/channel/telegram/handlers.py#L1232)`

### 改动文件
- `pixivfeed/.../foo.py`
- `pixivfeed/.../bar.py`
```

旧条目原样保留，**不要删旧版本号**。

## Commit 文本规范

每条 commit message 第一行是 `<type>[(<scope>)]: <subject>`（conventional commits 简化版）。

类型（type）：

| type | 何时用 |
| --- | --- |
| `feat` | 新功能 / 用户可感知的能力 |
| `fix` | bug 修复 |
| `docs` | README / CHANGELOG / AGENTS 等纯文档 |
| `refactor` | 仅代码重排，行为不变 |
| `chore` | 版本号 bump、.gitignore、依赖收紧、杂项维护 |
| `release` | 发版 commit；subject 必须形如 `vX.Y.Z <topic-slug>` |

scope（可选）：标 affected area，如 `fix(novel):` / `feat(setting):` / `fix(archive):`。改动横跨多块时省略 scope。

subject：

- **英文 imperative**，全小写，无句号、无 emoji。"add"/"fix"/"rewrite"，不是 "added"/"fixes"。
- ≤ 70 字符。超出的细节丢 commit body，**别**塞进第一行。
- 不要把改动文件列表写进 message —— `git diff --stat` 就是源数据，详细列表写在 CHANGELOG.md。
- 不要写 `Co-Authored-By: Claude` 之类的痕迹。

正文 body（可选，超过单行才需要）：

- 用空行与 subject 隔开。
- 解释 **why**（动机、约束、坑），不是 **what**（diff 自己会说）。
- **统一用中文**。项目里 README / AGENTS / CHANGELOG 都是中文，body 也跟着用中文，整段不混英文（专有名词、函数名、文件路径例外）。历史上 v0.4.3、v0.5.0 等 release 留下的英文 body 不动；新 commit 一律中文。

主要参照已有 commit：`fix(novel): prevent CONTENT_TOO_BIG by tightening char limit + byte-aware bisect`、`feat(setting): ...`、`docs: mention /wiki feature and credit bot-rs`、`release: v0.5.0 wiki-search-inline`。历史里有些 bare 风格（`Remove pixiv inline commands section from README`）—— 那是早期遗留，新 commit 一律按上面格式。

## 提交前自检（避免重复回归）

加新 try/except 或 fallback 路径时，**先 grep 一下涉及的异常类型**，看：
1. 它是不是某个已被广泛 catch 的基类的子类？
2. 周围有没有 `except <baseclass>: ... continue` / `except <baseclass>: pass` 把它默默吞掉？

例：本项目 `ArchiveLockedError(ArchiveError)`。任何 `except ArchiveError` 都会先吃掉 LockedError。如果你想让 LockedError 跳过 fallback 直接向上抛，必须显式 `except ArchiveLockedError: raise` **写在** `except ArchiveError` **之前**。

类似的现成"陷阱"：
- `EHGalleryUnavailable(EHError)`
- `PixivAuthError` / `PixivNotFoundError` / `PixivAPIError`（看实现确认继承关系）

新增异常或新增 `except` 块前花 30 秒 grep 一下，能避免一整轮"用户测试失败 → 你查 log → 发现是异常被吞了"的循环。

## 何时主动建议切版本

主动开口的几个时机：
- 一组改动跑通用户验收过了，用户说"先这样吧"
- 改动涉及配置结构（新增 key、重命名 key）
- 改动会影响部署流程或外部服务（telegram-bot-api、nginx）
- 用户说"想保留可以回退"或"先不动了"

"主动建议"措辞示例：
> 这次改动跑通了，建议打个 v0.4.1 tag 作为回退点（commit + tag + push）。要我现在做吗？

不要不打招呼就 tag 并 push——push 到远端是公开动作，用户应该先认可。

## 不要在用户回复里写的事

- 不要重复声明"我已经切了新版本"如果上下文已经显示了。
- 不要把这份 AGENTS 文档贴回给用户当部署说明用。
- 不要在每次回答都重复版本规则——用户读过一次了。
