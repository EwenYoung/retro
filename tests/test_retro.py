"""retro.py 集成测试（方案 C）。

通过 importlib 加载 scripts/retro.py，用 tmp_path 构造最小 fixture
（.retro/log/YYYY-MM-DD.md + .retro/entries/xxx.md + AGENTS.md 标记区），
经 retro.main() 做集成调用。
"""

import importlib.util
import json
import os

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


def write_entry(root, fm, body):
    """fm: dict，字段会被 _serialize_value 序列化；body 以换行结尾。"""
    entries_dir = root / ".retro" / "entries"
    entries_dir.mkdir(parents=True, exist_ok=True)
    eid = fm["id"]
    keys = ["id", "title", "tags", "confidence", "raw_ref", "supersedes"]
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
    """AGENTS.md 规则行 strip 后 >100 字符 → check 有 warning。"""
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
    assert "超过 100 字符" in out


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
