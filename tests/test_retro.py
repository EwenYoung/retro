"""retro.py 集成测试（方案 C）。

通过 importlib 加载 scripts/retro.py，用 tmp_path 构造最小 fixture
（.retro/log/YYYY-MM-DD.md + .retro/entries/xxx.md + AGENTS.md 标记区），
经 retro.main() 做集成调用。
"""

import datetime
import importlib.util
import json
import os
import sys

import pytest

SCRIPTS_RETRO = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "scripts", "retro.py"
)
_spec = importlib.util.spec_from_file_location("retro_skill", SCRIPTS_RETRO)
retro = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(retro)

DATE = "2026-08-24"
EID = "20260824-001"


# ---------------------------------------------------------------- fixture 构造


def write_log(root, sections):
    """sections: [str] 每个元素是一个完整段落（含 `## sN` 标题）。"""
    log_dir = root / ".retro" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    content = "# %s 会话摘录\n\n" % DATE + "\n\n".join(sections) + "\n"
    (log_dir / ("%s.md" % DATE)).write_text(content, encoding="utf-8")


def write_log_at(root, date_str, sections):
    """按指定日期写 log 文件（write_log 固定用模块级 DATE）。"""
    log_dir = root / ".retro" / "log"
    log_dir.mkdir(parents=True, exist_ok=True)
    content = "# %s 会话摘录\n\n" % date_str + "\n\n".join(sections) + "\n"
    (log_dir / ("%s.md" % date_str)).write_text(content, encoding="utf-8")


def write_entry(root, fm, body):
    """fm: dict，字段会被 _serialize_value 序列化；body 以换行结尾。"""
    entries_dir = root / ".retro" / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    eid = fm["id"]
    keys = ["id", "title", "scope", "tags", "confidence", "raw_ref", "supersedes"]
    if "escalated" in fm:
        keys.append("escalated")
    lines = ["---"]
    for k in keys:
        if k in fm:
            lines.append("%s: %s" % (k, retro._serialize_value(fm[k])))
    lines.append("---")
    (entries_dir / ("%s.md" % eid)).write_text(
        "\n".join(lines) + "\n" + body, encoding="utf-8"
    )


def write_agents(root, rules=None):
    """rules: [str]，每项是规则行正文（不含 "- " 前缀与 id 后缀）。"""
    content = "# AGENTS.md\n\n## 经验教训\n\n<!-- retro-managed-start -->\n"
    for r in rules or []:
        content += "- %s\n" % r
    content += "<!-- retro-managed-end -->\n"
    (root / "AGENTS.md").write_text(content, encoding="utf-8")


def run(root, *args):
    return retro.main(["--root", str(root)] + list(args))


def capture(root, capsys, *args):
    rc = run(root, *args)
    return rc, capsys.readouterr().out


def base_fm(title="测试结论标题"):
    return {
        "id": EID,
        "title": title,
        "tags": ["domain"],
        "confidence": "high",
        "raw_ref": ["log/%s.md#s1" % DATE],
        "supersedes": None,
    }


# ---------------------------------------------------------------- D3


def test_d3_apply_rule_text_uses_title(tmp_path):
    """seen_count=2（raw_ref + 同日期 seen-again）时 apply 规则行 = title。"""
    root = tmp_path
    log = [
        "## s1 段落标题\n\n> entry: %s\n" % EID,
        "## s2 同坑再现\n\n> seen-again: %s\n" % EID,
    ]
    write_log(root, log)
    title = "元素级 CSS 变量声明优先于继承"
    write_entry(root, base_fm(title), "正文第一非空行，不是标题。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    assert run(root, "check") == 0

    assert run(root, "escalate", "--apply", EID) == 0
    content = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "- %s [%s]" % (title, EID) in content
    # 规则行取自 title，而不是正文第一行
    assert "正文第一非空行，不是标题。" not in content


# ---------------------------------------------------------------- D5


def test_d5_force_bypasses_seen_threshold(tmp_path):
    """seen=1 时普通 --apply 被拒；--force 成功且审计日志含 force:true。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    assert run(root, "check") == 0

    # 普通 apply（seen=1）被拒
    assert run(root, "escalate", "--apply", EID) == 1
    # --force 成功
    assert run(root, "escalate", "--force", "--apply", EID) == 0
    content = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "- 测试结论标题 [%s]" % EID in content
    records = (root / ".retro" / "escalation.log.jsonl").read_text(encoding="utf-8").splitlines()
    last = json.loads(records[-1])
    assert last["op"] == "escalate"
    assert last["force"] is True
    assert last["ids"] == [EID]


# ---------------------------------------------------------------- D4


def test_d4_log_entry_line_missing_id_warns(tmp_path, capsys):
    """log 段落 entry 行指向不存在的条目 id → check 有 warning。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: 20260824-999\n"]
    write_log(root, log)
    fm = base_fm()
    fm["raw_ref"] = ["log/%s.md" % DATE]  # 文件级 ref，隔离②号校验
    write_entry(root, fm, "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert rc == 0  # warning 不升级为 error
    assert "指向不存在的条目 id: 20260824-999" in out


# ---------------------------------------------------------------- D6


def test_d6_long_rule_line_warns(tmp_path, capsys):
    """AGENTS.md 规则行 strip 后 >120 字符 → check 有 warning。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    fm = base_fm()
    fm["escalated"] = True  # 避免 reconcile 漂移 warning 干扰
    write_entry(root, fm, "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    long_text = "超长规则行" * 30  # 150 字符
    write_agents(root, ["%s [%s]" % (long_text, EID)])
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert rc == 0
    assert "超过 120 字符" in out


# ---------------------------------------------------------------- D8


def test_d8_reconcile_exit_1_on_drift(tmp_path, capsys):
    """条目 escalated=true 但 AGENTS.md 无该行 → reconcile 返回 1。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    fm = base_fm()
    fm["escalated"] = True
    write_entry(root, fm, "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root, [])  # 标记区存在但无规则行

    rc, out = capture(root, capsys, "reconcile")
    assert rc == 1
    assert "漂移" in out


# ---------------------------------------------------------------- D9


def test_d9_title_55_chars_no_warning(tmp_path, capsys):
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm("T" * 55), "结论句。\n\n- **症状**：x\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert rc == 0
    assert "title 超过 60 字符" not in out


def test_d9_title_61_chars_warns(tmp_path, capsys):
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm("T" * 61), "结论句。\n\n- **症状**：x\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert "title 超过 60 字符 (61)" in out
    # 状态回归为 needs_review 后重跑 index，check 应为 0 error（仅 warning）
    assert run(root, "index") == 0
    rc, out = capture(root, capsys, "check")
    assert rc == 0
    assert "title 超过 60 字符 (61)" in out


# ---------------------------------------------------------------- applied 追踪


def test_applied_lines_parsed_into_derived_fields(tmp_path):
    """log 中 applied 引用行 → index 后条目 frontmatter 出现 applied_count/applied_ok。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n> applied: %s ok\n" % (EID, EID)]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    content = (root / ".retro" / "entries" / ("%s.md" % EID)).read_text(encoding="utf-8")
    assert "applied_count: 1" in content
    assert "applied_ok: 1" in content


def test_applied_ok_meets_escalate_threshold(tmp_path, capsys):
    """seen_count=1 但 applied_ok=1 → 条目出现在 escalate 候选列表（门槛放宽生效）。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n> applied: %s ok\n" % (EID, EID)]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    assert run(root, "check") == 0

    rc, out = capture(root, capsys, "escalate")
    assert rc == 0
    assert "[%s]" % EID in out
    assert "applied_ok=1 → 2 分" in out


def test_applied_ok_fail_mix_counts(tmp_path):
    """同一 id 两行 applied（1 ok + 1 fail）→ applied_count=2、applied_ok=1。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n> applied: %s ok\n> applied: %s fail\n" % (EID, EID, EID)]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    content = (root / ".retro" / "entries" / ("%s.md" % EID)).read_text(encoding="utf-8")
    assert "applied_count: 2" in content
    assert "applied_ok: 1" in content


def test_applied_line_missing_id_warns(tmp_path, capsys):
    """log 段落 applied 行指向不存在的条目 id → check 输出 warning（且不是 error）。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n> applied: 20260824-999 ok\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert rc == 0  # warning 不升级为 error
    assert "applied 行指向不存在的条目 id: 20260824-999" in out


# ---------------------------------------------------------------- scope 字段


def test_scope_included_in_applied_rule_line(tmp_path):
    """有 scope 的条目 escalate --apply 后，AGENTS.md 规则行含（scope）且行尾 [id] 正确。"""
    root = tmp_path
    log = [
        "## s1 段落标题\n\n> entry: %s\n" % EID,
        "## s2 同坑再现\n\n> seen-again: %s\n" % EID,
    ]
    write_log(root, log)
    fm = base_fm()
    fm["scope"] = "Git Bash / wsl 下"
    write_entry(root, fm, "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0
    assert run(root, "check") == 0

    assert run(root, "escalate", "--apply", EID) == 0
    content = (root / "AGENTS.md").read_text(encoding="utf-8")
    assert "- 测试结论标题（Git Bash / wsl 下） [%s]" % EID in content


def test_scope_over_60_chars_warns(tmp_path, capsys):
    """scope 超过 60 字符 → check 报 warning（不是 error）。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    fm = base_fm()
    fm["scope"] = "适" * 61
    write_entry(root, fm, "结论句。\n\n- **症状**：x\n- **解法**：y\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "check")
    assert rc == 0
    assert "scope 超过 60 字符 (61)" in out


# ---------------------------------------------------------------- 审计轮（报告）

def test_audit_six_blocks_empty_repo(tmp_path, capsys):
    """空库（无 log/entries/AGENTS.md）跑 audit：不崩、六区块标题齐全、退出码 0。"""
    root = tmp_path
    assert run(root, "index") == 0  # 生成空 INDEX.md，check 才 0 error
    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    for title in ("① 健康检查", "② 升级候选", "③ 降级候选",
                  "④ 失效候选", "⑤ 重复/合并候选", "⑥ 审计状态"):
        assert title in out
    assert "从未审计" in out


def test_audit_rule_blank_drift_detected(tmp_path, capsys):
    """标记区内「规则行→空行→规则行」被计为格式漂移。"""
    root = tmp_path
    log = [
        "## s1 段落标题\n\n> entry: %s\n" % EID,
        "## s2 段落标题\n\n> entry: 20260824-002\n",
    ]
    write_log(root, log)
    fm = base_fm()
    fm["escalated"] = True
    write_entry(root, fm, "结论句。\n")
    fm2 = base_fm()
    fm2["id"] = "20260824-002"
    fm2["title"] = "第二个条目结论标题"
    fm2["escalated"] = True
    write_entry(root, fm2, "结论句。\n")
    # 标记区内空行分隔规则行 → 漂移 1 处
    content = ("# AGENTS.md\n\n## 经验教训\n\n<!-- retro-managed-start -->\n"
               "\n- 规则一 [%s]\n\n- 规则二 [20260824-002]\n\n"
               "<!-- retro-managed-end -->\n" % EID)
    (root / "AGENTS.md").write_text(content, encoding="utf-8")
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    assert "规则区格式漂移" in out
    assert "1 处" in out


def test_audit_rules_warn_threshold(tmp_path, capsys):
    """规则区条数 ≥ AUDIT_RULES_WARN_THRESHOLD → 输出预警行。"""
    root = tmp_path
    assert run(root, "index") == 0
    rule_lines = ["- 规则%02d [20260101-%03d]" % (i + 1, i + 1) for i in range(10)]
    content = ("# AGENTS.md\n\n## 经验教训\n\n<!-- retro-managed-start -->\n"
               + "\n".join(rule_lines) + "\n<!-- retro-managed-end -->\n")
    (root / "AGENTS.md").write_text(content, encoding="utf-8")

    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    assert "! 规则区 10/12，剩余 2 空位，接近上限" in out


def test_audit_demote_candidates_sorted(tmp_path, capsys):
    """降级候选按 (last_seen 升序, seen_count 升序, applied_count 升序) 输出。"""
    root = tmp_path
    d_old = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
    d_recent = (datetime.date.today() - datetime.timedelta(days=20)).strftime("%Y-%m-%d")
    ids = ["20260801-001", "20260810-001", "20260810-002", "20260810-003"]
    specs = [
        (ids[0], "最老的降级候选条目", ["log/%s.md#s1" % d_old], 1, 0),
        (ids[1], "稍新的降级候选条目", ["log/%s.md#s1" % d_recent], 1, 0),
        (ids[2], "同日期多次出现条目", ["log/%s.md#s2" % d_recent], 2, 0),
        (ids[3], "同日期被应用条目", ["log/%s.md#s3" % d_recent], 2, 1),
    ]
    for eid, title, raw_ref, _seen, _applied in specs:
        write_entry(root, {"id": eid, "title": title, "tags": ["domain"],
                           "confidence": "high", "raw_ref": raw_ref,
                           "supersedes": None, "escalated": True}, "结论句。\n")
    write_log_at(root, d_old, ["## s1 段落标题\n\n> entry: %s\n" % ids[0]])
    write_log_at(root, d_recent, [
        "## s1 段落标题\n\n> entry: %s\n" % ids[1],
        "## s2 段落标题\n\n> entry: %s\n> seen-again: %s\n" % (ids[2], ids[2]),
        "## s3 段落标题\n\n> entry: %s\n> seen-again: %s\n> applied: %s ok\n" % (ids[3], ids[3], ids[3]),
    ])
    rule_lines = ["- 规则%s [%s]" % (eid[-3:], eid) for eid in ids]
    content = ("# AGENTS.md\n\n## 经验教训\n\n<!-- retro-managed-start -->\n"
               + "\n".join(rule_lines) + "\n<!-- retro-managed-end -->\n")
    (root / "AGENTS.md").write_text(content, encoding="utf-8")
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    seg = out.split("③ 降级候选")[1].split("④ 失效候选")[0]
    order = [i for i in ids if ("[%s]" % i) in seg]
    assert order == ids
    assert "last_seen=%s | seen×1 | applied×0(ok 0)" % d_old in seg


def test_audit_stale_and_fail_only_candidates(tmp_path, capsys):
    """失效候选两档：applied 全 fail → 证据档；无 applied + 老 last_seen + seen×1 → 疑似档。"""
    root = tmp_path
    d_old = (datetime.date.today() - datetime.timedelta(days=40)).strftime("%Y-%m-%d")
    write_log_at(root, d_old, ["## s1 段落标题\n\n> entry: 20260701-001\n"])
    write_log(root, ["## s1 段落标题\n\n> entry: %s\n> applied: %s fail\n> applied: %s fail\n"
                     % (EID, EID, EID)])
    write_entry(root, base_fm(), "结论句。\n")
    fm = base_fm()
    fm["id"] = "20260701-001"
    fm["title"] = "很久未现的疑似失效条目"
    fm["raw_ref"] = ["log/%s.md#s1" % d_old]
    write_entry(root, fm, "结论句。\n")
    write_agents(root)
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    ev = out.split("证据档")[1].split("疑似档")[0]
    assert "[%s]" % EID in ev
    assert "applied×2(ok 0)" in ev
    sus = out.split("疑似档")[1].split("⑤ 重复/合并候选")[0]
    assert "[20260701-001]" in sus
    assert "供 LLM 复核" in sus


def test_audit_read_only_no_writes(tmp_path, capsys):
    """只读纪律：audit 干跑不改 entries / INDEX.md / AGENTS.md。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n\n- **症状**：x\n")
    write_agents(root, ["测试结论标题 [%s]" % EID])
    assert run(root, "index") == 0

    targets = [root / ".retro" / "entries" / ("%s.md" % EID),
               root / ".retro" / "INDEX.md",
               root / "AGENTS.md"]
    before = [p.read_text(encoding="utf-8") for p in targets]
    rc, _out = capture(root, capsys, "audit")
    assert rc == 0
    after = [p.read_text(encoding="utf-8") for p in targets]
    assert before == after


def test_audit_exit_code_on_check_errors(tmp_path, capsys):
    """① 区块有 check error 时退出 1；修复后退出 0。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    write_entry(root, base_fm(), "结论句。\n")
    write_agents(root)

    # 未生成 INDEX.md → check error → audit 退出 1
    rc, out = capture(root, capsys, "audit")
    assert rc == 1
    assert "① 健康检查" in out

    # 修复（index 重建）后 → 0
    assert run(root, "index") == 0
    rc, _out = capture(root, capsys, "audit")
    assert rc == 0


# ---------------------------------------------------------------- 审计轮（落账与驳回记忆）

def test_audit_close_landing_and_dismissed_validation(tmp_path, capsys):
    """--close 落账一行合法 JSON、stats 正确；--dismissed 指向不存在 id → 拒绝退出 1。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    fm = base_fm()
    fm["escalated"] = True
    write_entry(root, fm, "结论句。\n")
    write_agents(root, ["测试结论标题 [%s]" % EID])
    assert run(root, "index") == 0

    rc, out = capture(root, capsys, "audit", "--close", "本轮总结文本")
    assert rc == 0
    assert "落账成功" in out
    assert "0 条" in out  # 本轮驳回数
    path = root / ".retro" / "audit.log.jsonl"
    lines = path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["ts"]
    assert rec["stats"] == {"entries": 1, "rules": 1, "escalated": 1}
    assert rec["summary"] == "本轮总结文本"
    assert rec["dismissed"] == []

    # --dismissed 指向不存在的 id → 拒绝，不追加记录
    rc, out = capture(root, capsys, "audit", "--close", "x", "--dismissed", "20260824-999")
    assert rc == 1
    assert "存在未知条目 id" in out
    assert len(path.read_text(encoding="utf-8").splitlines()) == 1

    # --dismissed 不与 --close 搭配 → 用法错误
    assert run(root, "audit", "--dismissed", EID) == 2


def test_audit_dismissed_silent_then_resurface(tmp_path, capsys):
    """驳回记忆：7 天内静默剔除；≥7 天重新浮出并标注请复查。"""
    root = tmp_path
    log = ["## s1 段落标题\n\n> entry: %s\n" % EID]
    write_log(root, log)
    fm = base_fm()
    fm["escalated"] = True
    write_entry(root, fm, "结论句。\n")
    write_agents(root, ["测试结论标题 [%s]" % EID])
    assert run(root, "index") == 0

    # 落账本轮审计并驳回 EID
    assert run(root, "audit", "--close", "总结x", "--dismissed", EID) == 0

    # 立即再审计：降级候选中 EID 被静默剔除
    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    seg3 = out.split("③ 降级候选")[1].split("④ 失效候选")[0]
    assert "[%s]" % EID not in seg3

    # 把该轮 ts 改成 8 天前 → 重新浮出，标注请复查
    path = root / ".retro" / "audit.log.jsonl"
    rec = json.loads(path.read_text(encoding="utf-8").splitlines()[0])
    rec["ts"] = (datetime.date.today() - datetime.timedelta(days=8)).isoformat() + "T12:00:00"
    path.write_text(json.dumps(rec, ensure_ascii=False) + "\n", encoding="utf-8")

    rc, out = capture(root, capsys, "audit")
    assert rc == 0
    seg3 = out.split("③ 降级候选")[1].split("④ 失效候选")[0]
    assert "[%s]" % EID in seg3
    assert "请复查" in seg3


def test_audit_stdout_encoding_safe_subprocess(tmp_path):
    """subprocess 用真实 stdout 编码（Windows GBK 等），audit 不应因 emoji 崩溃。

    回归 U+26A0（⚠）/ U+2194（↔）在 GBK 控制台 UnicodeEncodeError 的 bug：
    capsys 重定向 stdout 走不到真实编码路径，只有 subprocess 能复现。
    """
    import subprocess
    root = tmp_path
    # 两条标题相同的升级条目 → 触发 ⑤ 重复候选（验证 ↔ 替代写法安全）
    fm = {"id": "20260101-001", "title": "完全相同的标题甲", "tags": ["domain"],
          "confidence": "high", "raw_ref": ["log/2026-01-01.md#s1"],
          "supersedes": None, "escalated": True}
    write_entry(root, fm, "结论句。\n")
    fm2 = dict(fm, id="20260101-002", raw_ref=["log/2026-01-01.md#s2"])
    write_entry(root, fm2, "结论句。\n")
    write_log_at(root, "2026-01-01",
                 ["## s1 段落甲\n\n> entry: 20260101-001\n",
                  "## s2 段落乙\n\n> entry: 20260101-002\n"])
    # 10 条规则（含 001/002 两条真实升级条目）→ 触发阈值预警（验证 ⚠ 替代写法安全）
    rule_lines = ["- 规则%02d [20260101-%03d]" % (i + 1, i + 1) for i in range(10)]
    content = ("# AGENTS.md\n\n## 经验教训\n\n<!-- retro-managed-start -->\n"
               + "\n".join(rule_lines) + "\n<!-- retro-managed-end -->\n")
    (root / "AGENTS.md").write_text(content, encoding="utf-8")
    assert run(root, "index") == 0  # entries 写完再 index，避免 INDEX 过期 error

    proc = subprocess.run(
        [sys.executable, SCRIPTS_RETRO, "--root", str(root), "audit"],
        capture_output=True, text=True,
    )
    assert proc.returncode == 0, proc.stderr
    assert "! 规则区 10/12" in proc.stdout
    assert "<->" in proc.stdout  # ⑤ 区块格式
