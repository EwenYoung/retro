---
name: retro
description: Review the session at wrap-up and distill pitfalls and major discoveries into the project's .retro/ knowledge base, escalating the most important lessons into AGENTS.md for future sessions to reuse. Use when the user says 沉淀一下 / 复盘 / 总结经验 / 记一下这次的坑 / retro, or proactively when wrapping up a substantial task (fixed a tough bug, set up an environment, finished a long debugging session) — even if the user didn't ask. Skip for trivial sessions with nothing worth recording.
---

# Retro：会话经验沉淀

任务收尾时回顾本会话，把可复用的经验写入项目 `.retro/` 经验库，把客观状态存入项目事实库 `.retro/facts.json`；特别重要的经验升级到项目 `AGENTS.md`。经验库的读者是未来的会话（agent 和用户），不是当下——每一条都必须值得未来花时间读。

记账、去重、计数、升降级、事实状态迁移等确定性操作由 `retro.py` 完成（脚本拥有派生字段），本技能只负责判断与内容。脚本路径：

```
~/.agents/skills/retro/scripts/retro.py --root <项目目录> <子命令>
```

## 五条不变量

1. **log/ 是唯一真相源**：`.retro/log/*.md` 追加式记录各会话原始摘录，事后永不修改。
2. **entries 可由 log 重建**：`.retro/entries/*.md` 是结构化条目，损坏可据 log 段落手工重写（`index` 只补派生字段，不生成条目），代价只是「重新结构化」，不是记忆丢失。
3. **记账字段归脚本**：`first_seen/last_seen/seen_count/applied_count/applied_ok/status/escalated/superseded_by` 由脚本写入，禁止手写。
4. **只标记不删除；升降级必审计**：冲突用 `supersedes` 标记而非删除；每次 escalate/demote 都会写 `escalation.log.jsonl`。
5. **事实与经验分开存，事实过期不删值**：经验（怎么做事）进 entries，事实（是什么状态）进 `.retro/facts.json`；事实的 `status/first_seen/verified_at` 归脚本，验证失败标 stale、退役标 retired，值和历史永不删除，每次变更写 `facts.log.jsonl` 账本（含旧值）。

## 第一步：回顾本会话

只回顾当前会话，重点找五类信号：

- **失败的尝试**：排查了很久才解决的报错；走了弯路、被证明行不通的做法——「什么不行」和「什么行」同样有价值
- **用户的纠正**：用户指出做错了什么、要求改方向的地方
- **找了很久才发现的信息**：关键文件/配置的实际位置、文档没写的行为、隐藏的架构约束
- **被推翻的假设**：一开始以为是 X，后来发现其实是 Y
- **稳定奏效的策略**：多次验证有效的排查路径/修复模式/自验方法（如「改样式先查 transition」「DOM 重依赖用 jiti 自验」）——成功模式与失败模式同样值得沉淀，tag 打 `success` 而非 `pitfall`，audit 时不会进失效候选

信号里若含**当前状态型事实**（见「项目事实」一节的收录标准），在写经验条目之外同时登记 facts。

## 收录门槛

同时满足三条才收录：

1. **可复用** —— 未来在本项目遇到类似情况时用得上
2. **非显而易见** —— 操作性标准：「本项目内实际踩坑且被 review/用户纠正过」；不是看代码或文档就能直接得出的
3. **跨会话有效** —— 不是本会话一次性的事务细节

不收录：任务进度细节（那是 handoff 技能的职责）、代码里一眼可见的事实、未经本项目验证的泛泛最佳实践。**特别地：本会话中从 `.retro/` 旧条目、`facts.json` 或 AGENTS.md 规则读到的内容不算新经验**——那是已有知识的复用，写入 log 会污染「这条经验被踩过几次」的统计（WikiSkill 消融实验的教训：知识来源被污染会降低数据对技能开发的诊断价值）。但**重新实测确认某事实值未变**是有价值的：`fact add` 同 key 同值登记（touch）即可刷新 verified_at，不算新经验也不产生新条目。回顾完若没有值得沉淀的，直接告诉用户「本次没有值得沉淀的经验」，不要硬凑——这是正常结果，不是失败。

## 第二步：写入

### 追加到 log

在 `.retro/log/YYYY-MM-DD.md`（当天不存在则新建）追加一个会话段落，**只追加，不改已写行**：

```markdown
## s1 本次会话摘录标题

<原文摘录，尽可能保留可复用信息>

> entry: 20260824-001        # 本次会话产生了该条目（挂接条目 id）
> fact: build.test-cmd       # 本段落同时登记了该事实（可选，挂接 fact key）
> seen-again: 20260824-002   # 旧事重提（seen_count 的确定性依据）
> applied: 20260824-001 ok   # 本会话实际采用了该条目的解法且有效
```

- 摘录写具体：命令/路径/参数，不写「注意配置正确」这类空话；说明原因和适用条件，让未来会话能判断是否适用。
- 若这次会话又踩了之前记录过的坑，加一行 `> seen-again: <旧条目 id>`，不要新开条目。触发条件：仅当同一坑再次被踩**且带来新信息**时加；纯重复没有新信息就不必再记。
- 若这次会话实际采用了某条经验并奏效，在该段落加一行 `> applied: <条目 id> ok`；采用了但没解决问题（经验过时/不适用）记 `fail`。同一 id 可多次记录，每行都是一次应用事件（`applied_count`/`applied_ok` 的确定性依据），ok 次数还会计入 escalate 门槛。
- 若这次会话发现某条旧经验已失效/被推翻，不要改旧条目，新建一条结论并在新条目里 `supersedes: <旧 id>`。

### 运行脚本

```bash
retro.py --root <项目目录> index   # 补齐派生字段 + 重新生成 INDEX.md
retro.py --root <项目目录> check   # 校验条目一致性，0 error 为验收线
```

### 条目 schema（entries/{id}.md）

内容字段由本技能撰写，其余由脚本维护：

```yaml
---
id: "20260824-001"       # YYYYMMDD-NNN（当日已有最大序号+1）
title: "Git Bash 路径必须用正斜杠"   # 一句话结论，≤60 字
scope: "Git Bash / wsl 下"   # 可选：该经验何时适用，≤60 字；确无适用条件限制时可省略
tags: [paths]            # 受控词表：paths|env|tooling|workflow|domain|pitfall|success|patterns
confidence: high         # low|medium|high
raw_ref: ["log/2026-08-24.md#s1"]   # 溯源到 log 段落
supersedes: null         # 或旧条目 id（本条推翻了旧条目）
---
正文：结论先行，≤10 行；说明症状/原因/解法/适用条件。
```

**书写规范**（脚本是严格 YAML 子集解析，写错会直接判语法错误）：

- frontmatter 字符串字段一律双引号包裹（`id`/`title`/`confidence`/`raw_ref` 元素等）。
- `tags` 用内联数组 `["a", "b"]`，元素必须加引号。
- 未加引号的标量若含空格/冒号/括号等符号会被脚本判为语法错误；拿不准就全用双引号。

脚本自动补全：`id 校验`、`first_seen/last_seen/seen_count`、`applied_count/applied_ok`、`status`（new→verified/needs_review/superseded）、`escalated`、`superseded_by`。

## 项目事实（facts）

经验回答「怎么做事、为什么」；事实回答「现在是什么状态」——构建/测试命令、关键路径、类名/选择器、端口、版本、数字限制。事实存 `.retro/facts.json`，未来会话**精确查询一步取值，零推理**。

### 收录标准

- **收**：agent 未来需要用「当前值」的客观状态——每次干活都可能查的（测试命令）、文档没写或翻了很久才发现的（隐藏类名、可达域名、有效期数字）。
- **不收**：历史测量型数字（如「实测 0/1074 放对」）——那是论证证据，留在经验条目正文里；一眼可查且不变的（入口文件名）；一次性事务细节。
- 同一发现可以「一鱼两吃」：经验条目记判断和解法，事实库记状态值；条目正文引用 key 而非写死值（如「活跃 tab 类名见 fact ui.class.active-tab」），避免值过期拖累经验条目。

### key 规范

至少两段、全小写字母/数字/连字符，按领域取前缀：`build.` `env.` `ui.` `paths.` `api.` `data.`（前缀是建议非强制，保证互不覆盖即可）。

### 登记 / 更新

```bash
retro.py --root <项目目录> fact add <key> <value> [--source "来源"] [--verify-cmd "断言命令"] [--ref log/YYYY-MM-DD.md#sN] [--reason "变更理由"]
```

- `fact add` 是新增+更新合一：key 已存在则更新，旧值自动入账本。
- `--verify-cmd` 尽量配：一条**只读、可重跑、退出码 0 表通过**的断言（如 grep 文件内容、检查端口、比对版本）。check 时脚本自动执行：失败标 stale（值仍可查但带过期警告），恢复自动转回 active。写不出的留给审计轮人工复核。
- 退役（功能下线/不再相关）：`fact retire <key> --reason "..."`，值不删除。

### 查询（干活时用，非沉淀时）

```bash
retro.py --root <项目目录> fact get <key>     # 精确取值；stale 会给过期警告
retro.py --root <项目目录> fact list [--prefix build.] [--status active]
```

INDEX.md 头部有 facts 概况行提示库里有什么。agent 在本项目中需要客观状态时，先查 facts 再去翻源文件——尤其找得很久才发现过的信息。

## 迁移旧结构到 log/entries

旧结构（按主题拆分文件、手工维护 INDEX）升级到本结构的步骤：

1. 逐条把旧经验写入 `.retro/log/YYYY-MM-DD.md` 的 `## sN` 段落（N 从 1 递增），保留原文可复用信息。
2. 按 schema 手工撰写 `.retro/entries/YYYYMMDD-NNN.md`（log 段落 → 结构化条目）。
3. 保证 `raw_ref` 锚点与 log 段落一一对应（`log/YYYY-MM-DD.md#sN` ↔ `## sN`），段落内写 `> entry: <id>` 挂接条目。
4. 旧主题文件归档到 `.retro/legacy/` 或删除。
5. 跑 `retro.py index` → `check` 验收（0 error）。

id 分配规则：`YYYYMMDD-NNN`，NNN 为当日已有最大序号 +1（3 位补零）。

## 第三步：子命令速查

| 子命令 | 作用 | 是否写 |
|---|---|---|
| `index` | 补齐派生字段、重建 INDEX.md（含 facts 概况行） | 写 |
| `check` | 校验一致性（含状态回归；facts 的 verify_cmd 机械验证，失败标 stale）；含 error 退出 1 | 写 status/facts |
| `stats` | 统计（条目/状态/seen_top/tag/applied/INDEX 体量/规则数/facts），零 LLM | 只读 |
| `escalate` | 列候选（硬门槛：seen_count≥2 或 applied_ok≥1）/ `--apply ID...` 升级 / `--force` 绕过 seen_count≥2 门槛（仅限迁移/紧急场景，审计记录 force:true）/ `--demote ID...` 降级 | 写 |
| `reconcile` | 检查 AGENTS.md 与条目 escalated 的漂移 | 只读 |
| `fact` | 项目事实存取：`add` 登记/更新（旧值入账本）/ `get` 精确查询 / `list` 过滤列表 / `retire --reason` 退役 | 写（add/retire） |
| `audit` | 审计轮：六区块只读报告（健康/升级/降级/失效/合并候选/审计状态）；`--close "总结" [--dismissed id1,id2]` 落账本轮审计 | 只读/追加 |

> `check` 会自动写回 status（new→verified/needs_review/superseded 回归），这是脚本管辖派生字段的正常行为，**不写审计日志**。审计日志（`escalation.log.jsonl`）只记录人为升降级决策（`escalate --apply`/`--demote`）。

## 第四步：升级重要经验到 AGENTS.md

AGENTS.md 每个会话都会加载，是「常驻前台」；`.retro/` 是按需查阅的「档案库」。是否值得升级由本技能判断，但**执行 `--apply` 前需显式确认**：先向用户说明候选与理由。

升级目标区块：默认升级到 AGENTS.md 的「## 经验教训」标记区；其他区块不纳入脚本管理（脚本只认一个 retro-managed 标记区）。

### 自动门槛

不带参数的 `escalate` 是干跑，列出候选与逐项命中理由：

- 硬门槛：`status=verified`、`seen_count≥2 或 applied_ok≥1`、非 superseded、未 escalated。
- 评分：`seen_count×2 + applied_ok×2` + 命中全局 tag（env/tooling/workflow）+1 + 正文≤3 行 +1。

## AGENTS.md 标记区约束

- 全文件只允许一个 `<!-- retro-managed-start/end -->` 标记区（脚本 `find` 只认第一个）。
- 规则行格式：`- <正文/标题（可含适用条件 scope），≤120 字符> [YYYYMMDD-NNN]`。
- 上限 12 条，满了用 `escalate --demote` 降级最旧条目。

### 12 条上限与降级

- AGENTS.md 标记区规则 ≥12 条时 `--apply` 会被拒绝，并给出降级候选（按 last_seen 升序）。
- 降级用 `escalate --demote <id>`，只回 `.retro/` 不移除条目，写审计日志。降级候选排序由脚本实现：按 last_seen 升序，同 last_seen 再按 seen_count 升序。

## 第五步：审计轮

经验库定期审计，是「知识精修时刻」：把高频经验补上 scope、清理失效/重复候选。`retro.py audit` 干跑是**只读报告，不代写任何状态**；只有 `--close` 会追加一行 `audit.log.jsonl`（审计记录只追加、不删除，同 escalation.log）。

### 触发条件（三选一，满足即建议）

- 距上次审计 ≥ 7 天（`audit.log.jsonl` 最近一条记录的 ts）
- 期间新增条目 ≥ 10 条
- 规则区 ≥ 10/12（接近 12 条上限）

满足时在任务收尾**建议**用户跑一轮审计（建议不是执行；`--close` 落账前需用户确认）。

### 运行与六区块怎么读

```bash
retro.py --root <项目目录> audit    # 干跑：六区块只读报告
retro.py --root <项目目录> audit --close "本轮总结" [--dismissed id1,id2]   # 落账
```

1. **① 健康检查**：check 的 errors/warnings 数、规则区格式漂移、规则数预警。有 error 时审计退出码 1——先修数据再审计。
2. **② 升级候选**：与 `escalate` 干跑同源（硬门槛：seen_count≥2 或 applied_ok≥1），逐条确认后走 `escalate --apply`。
3. **③ 降级候选**：已升级且在规则区、按旧度排序 top 5。读正文确认仍有效，无效才 `escalate --demote`；只是参考列表，不是命令。
4. **④ 失效候选**：证据档（applied 全 fail）看失败上下文判断是经验过时还是当时误用；疑似档（无 applied × seen×1 × ≥30 天未现）供 LLM 复核。
5. **⑤ 重复/合并候选**：按「主体/解法/升级去向任一不同可各留一条」仲裁（例：ui-theme-vars 的 001「元素级 CSS 变量声明」与 003「官方新组件元素级变量」不合并）。
6. **⑥ 审计状态**：上次审计距今、期间新增、下次建议日期、escalation 覆盖、历史驳回名单——判断触发条件与体检节奏。

### scope 回填（核心动作）

审计时对**无 scope 的升级候选与高频条目**（seen×≥2 或 applied×≥1）回填适用条件：能写出「何时该用/何时别用」就写进 `scope`（规则行会带 `（scope）`），写不出说明经验本身不聚焦，考虑合并。这是审计轮作为「知识精修时刻」的核心动作。

### 决策清单

升 / 降 / 合并 / supersedes（新条目推翻了旧经验）/ 驳回（暂不处理），**经用户确认后**走既有命令执行：

- 升：`escalate --apply <id>`；降：`escalate --demote <id>`；合并与 supersedes：手工改 entries 后 `index`。
- 驳回的 id 记入 `--dismissed`，7 天内不再浮出（静默剔除），之后重新浮出并标注「请复查」。

### 收尾

1. 干跑读六区块，拿 ③④⑤ 做语义判断，scope 回填，决策清单跟用户确认后执行。
2. 必跑 `audit --close "本轮总结" [--dismissed ...]` 落账（check 有 error 会被拒绝）。
3. 最后 `retro.py check` 验收 0 error。

原则句：**audit 只读报告，不代写任何状态**（不修格式漂移、不写 entries/INDEX/AGENTS.md——report-only）；驳回过的候选 7 天内静默、之后重新浮出。

## 第六步：冲突处理约定

- **重复/近似重复**：check 会按标题相似度给出合并建议，上报后由本技能合并（保留 raw_ref 与 seen_count）。同原理多条的合并标准：主体/解法/升级去向任一不同可各留一条（例：ui-theme-vars 的 001「元素级 CSS 变量声明」是自研主题写法约束、003「官方新组件元素级变量」是覆盖官方组件的对抗规则，两者不合并）。
- **内容矛盾**：用 `supersedes` 标记（新条目指向旧条目 id），脚本自动给旧条目 `superseded_by`。绝不删除旧条目。
- **账务级/升降级操作**：脚本只出建议与理由，执行须显式确认；`--apply`/`--demote` 前会备份 AGENTS.md。

## 需要外包的情况

以下情况直接派临时 subagent 处理：

- `check` error 数 > 5，需要批量修复
- 批量重建 INDEX 与派生字段——注意 `retro.py index` 只补派生字段 + 重建 INDEX.md，**没有从 log 生成 entries 的命令**；entries 必须按 schema 手工撰写（log 段落 → 结构化条目），量大时适合外包批量写
- 批量 schema 迁移
- 冲突集中仲裁（需读几十上百条旧条目做裁决）

验收标准：重跑 `check` 全绿 + `escalation.log.jsonl` 有完整审计记录。

## 条目格式（正文写作要求）

```markdown
Git Bash 路径必须用正斜杠。

- **症状**：bash 脚本里写 D:\chat\file.txt 报 file not found
- **原因**：bash 把反斜杠当转义字符处理
- **解法**：一律写成 /d/chat/file.txt；脚本内可用 cygpath -u 转换
- **适用条件**：在 Git Bash / wsl 下
```

写法要求（受众是未来的 agent）：解法具体到可直接执行；说明原因和适用条件；一条经验一个条目，标题直接写结论。

「标题即结论」的操作化：结论在前半句即合规；日期/探测等后缀放条目正文；**正文第一非空行保持纯文本结论句，不要用 `##` 标题**（title 缺失时规则行提取逻辑会回退到它）。

## 最后：向用户汇报

一两句话说明：沉淀了几条、合并还是新建了哪些主题、登记/更新了哪些事实（有 stale 的要点名提醒）、是否有条目升级到了 AGENTS.md。不要贴出全部文件内容。
