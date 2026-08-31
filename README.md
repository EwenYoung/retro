# retro

> 把 Agent 会话经验编译成持久知识，再蒸馏为可复用的规则——一个带验证门控的 Agent 记忆进化系统。

`retro` 是一个 [Agent Skill](https://agent-skills.dev)（SKILL.md + 确定性脚本），解决一个具体问题：**Agent 每次会话踩过的坑，下次会话还得再踩一遍**。

灵感来自 Google Research 的 [WikiSkill](https://arxiv.org/abs/2608.27454)（arXiv:2608.27454）：在「原始经验」和「可执行规则」之间插入一个持久知识层，让经验被持续编译、沉淀，支撑规则的复利式进化。

## 核心思想

三层架构 + 双向流动：

```
会话结束 ──沉淀──▶  .retro/log/        原始摘录（唯一真相源，只追加）
                        │  index
                        ▼
                   .retro/entries/     结构化知识（症状/原因/解法/适用条件）
                        │  escalate（验证门控）
                        ▼
                   AGENTS.md 规则区    常驻前台（≤12 条，每个会话都加载）

会话进行中 ──applied: ok/fail──▶ 逆向反馈：哪条经验真的管用？
定期 ──audit──▶ 全库审计：升级/降级/失效/合并/驳回
```

与 WikiSkill 的机制对应：

| WikiSkill | retro |
|---|---|
| raw 层（执行轨迹） | `.retro/log/` 会话摘录 |
| wiki 层（持久知识） | `.retro/entries/` 结构化条目 |
| skills 层（可执行规则） | `AGENTS.md` 规则区 |
| skill-impact.md（干预审计） | `> applied: <id> ok/fail` 引用行 + `audit.log.jsonl` |
| 验证门控 | `applied_ok≥1` 或 `seen_count≥2` 才能升级 |
| 提案者 + 回滚 | `audit` 六区块报告 + 用户确认后执行 |

## 快速开始

```bash
# 1. 在目标项目安装技能目录（SKILL.md + scripts/retro.py）

# 2. 会话收尾时，让 agent 沉淀经验（触发词：沉淀一下 / 复盘 / retro）
#    agent 会写 .retro/log/ + .retro/entries/，然后：

retro.py --root <项目目录> index    # 补齐派生字段、重建索引
retro.py --root <项目目录> check    # 校验一致性，0 error 为验收线

# 3. 查看哪些经验值得常驻前台
retro.py --root <项目目录> escalate          # 干跑：列升级候选+理由
retro.py --root <项目目录> escalate --apply <id>   # 确认后升级到 AGENTS.md

# 4. 定期审计（≥7 天或新增 ≥10 条或规则区 ≥10/12）
retro.py --root <项目目录> audit             # 六区块只读报告
retro.py --root <项目目录> audit --close "总结" --dismissed <id,id>  # 落账
```

## 子命令

| 子命令 | 作用 | 是否写 |
|---|---|---|
| `index` | 补齐派生字段、重建 INDEX.md | 写 |
| `check` | 校验条目一致性；含 error 退出 1 | 写 status |
| `stats` | 统计（条目/状态/applied/tag/规则数），零 LLM | 只读 |
| `escalate` | 列候选 / `--apply` 升级 / `--demote` 降级 / `--force` | 写 |
| `reconcile` | 检查 AGENTS.md 与条目 escalated 的漂移 | 只读 |
| `audit` | 审计轮：六区块只读报告 / `--close` 落账 | 只读/追加 |

## 设计原则

**分工**：LLM 只做判断与内容（写了什么经验、值不值得升级），脚本做全部确定性账务（计数、去重、升降级、审计日志）——计数永远不出错，内容永远有人负责。

**只追加，不删除**：log 和两条审计日志（escalation/audit）只追加；冲突用 `supersedes` 标记；降级只是退出前台，条目永回档案库。旧经验被推翻不会丢失——它会告诉你「什么曾经是对的」。

**验证门控**：升级到 AGENTS.md 需要证据——要么被重复踩过（`seen_count≥2`），要么被实际应用且有效（`applied_ok≥1`）。没有证据的经验留在档案库里，不占常驻前台的名额。

**门控永远在人手里**：脚本是干跑报告 + 确认执行两段式；`--apply`/`--demote`/`--close` 都需要用户显式确认；一切升降级有备份、有审计日志。

**防污染**：会话中从旧条目读来的内容不算新经验——复用不是新知，写进去会污染「这条经验被踩过几次」的统计。

## 数据结构

```
<项目>/
├── AGENTS.md                  # 规则区（retro-managed 标记区，≤12 条）
└── .retro/
    ├── log/YYYY-MM-DD.md      # 会话原始摘录（唯一真相源）
    ├── entries/YYYYMMDD-NNN.md  # 结构化条目（YAML frontmatter + 正文）
    ├── INDEX.md               # 脚本生成的索引（含 seen/applied/escalated）
    ├── escalation.log.jsonl   # 升降级审计
    └── audit.log.jsonl        # 审计轮次记录（含 dismissed 驳回记忆）
```

条目 frontmatter：内容字段（`id/title/scope/tags/confidence/raw_ref/supersedes`）由 agent 撰写，派生字段（`first_seen/last_seen/seen_count/applied_count/applied_ok/status/escalated/superseded_by`）由脚本写入。

## 实战规模

首个生产项目（Chrome MV3 扩展，活跃开发中）：25 条经验、10 条常驻规则、首轮审计 24/25 条补齐 scope、5 条降级候选经人工复核全部保留、驳回记忆正常静默。

## 测试

```bash
uv run pytest tests/ -q
```

24 个集成测试覆盖：log 解析、派生字段、词表校验、升级门槛与评分、scope 拼接、audit 六区块、dismissed 静默/浮出、只读纪律（文件 hash 比对）、真实 stdout 编码（subprocess 回归）。

## License

MIT
