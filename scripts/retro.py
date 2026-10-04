#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""retro.py — retro 记忆系统「方案 C」确定性脚本。

两阶段模式的 Phase 2：index / check / stats / escalate / reconcile 全部由本脚本
确定性地完成；log/ 为唯一真相源，entries 永远可由 log 重建。

用法:  python retro.py --root <项目目录> <subcommand>
       python retro.py --root <项目目录> index|check|stats|reconcile|audit
       python retro.py --root <项目目录> escalate [--dry-run | --apply ID... | --demote ID...]
       python retro.py --root <项目目录> audit [--close "总结" [--dismissed ID1,ID2]]
       python retro.py --root <项目目录> fact add KEY VALUE [--source ...] [--verify-cmd ...] [--ref ...]
       python retro.py --root <项目目录> fact get KEY | fact list [--prefix P] [--status S]
       python retro.py --root <项目目录> fact retire KEY --reason "..."

退出码: 0=通过; 1=check 发现 errors 或脚本内部错误; 2=用法错误。
所有输出均为中文。所有文件读写显式 encoding="utf-8"。
"""

import argparse
import json
import os
import re
import subprocess
import sys
import tempfile
import datetime

# ---------------------------------------------------------------- 常量

LLM_KEYS = ["id", "title", "scope", "tags", "confidence", "raw_ref", "supersedes"]
SCRIPT_KEYS = [
    "first_seen", "last_seen", "seen_count", "applied_count", "applied_ok",
    "status", "escalated", "superseded_by",
]
FIELD_ORDER = LLM_KEYS + SCRIPT_KEYS
ALLOWED_KEYS = set(LLM_KEYS) | set(SCRIPT_KEYS)

VALID_TAGS = {"paths", "env", "tooling", "workflow", "domain", "pitfall", "success", "patterns"}
VALID_STATUS = {"new", "verified", "needs_review", "superseded"}
VALID_CONFIDENCE = {"low", "medium", "high"}

ID_RE = re.compile(r"^\d{8}-(\d{3})$")

DIR_LOG = ".retro/log"
DIR_ENTRIES = ".retro/entries"
FILE_INDEX = ".retro/INDEX.md"
FILE_ESCALATION = ".retro/escalation.log.jsonl"
FILE_AUDIT = ".retro/audit.log.jsonl"
FILE_FACTS = ".retro/facts.json"
FILE_FACTS_LOG = ".retro/facts.log.jsonl"
FILE_AGENTS = "AGENTS.md"

AGENTS_START = "<!-- retro-managed-start -->"
AGENTS_END = "<!-- retro-managed-end -->"

# 审计轮阈值
AUDIT_INTERVAL_DAYS = 7            # 审计轮最小间隔（驳回记忆复用同一阈值）
AUDIT_NEW_ENTRIES_TRIGGER = 10     # 新增条数触发阈值
AUDIT_RULES_WARN_THRESHOLD = 10    # 规则区预警阈值（上限 12）
AUDIT_STALE_DAYS = 30              # 失效候选「疑似档」老条目阈值

NEG_WORDS = ("不能", "不能用", "不要", "避免", "失败")
POS_WORDS = ("可以", "能行", "可行", "推荐", "成功")
ESCALATE_TAG_BONUS = {"env", "tooling", "workflow"}

# facts（项目事实）约束
FACT_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9-]*(\.[a-z0-9][a-z0-9-]*)+$")
VALID_FACT_STATUS = {"active", "stale", "retired"}
FACT_VERIFY_TIMEOUT_SEC = 10

# ---------------------------------------------------------------- 小工具


class RetroError(Exception):
    """脚本内部/数据层面的错误。"""


def _now():
    return datetime.datetime.now()


def _today_str():
    return _now().strftime("%Y-%m-%d")


def _stamp():
    return _now().strftime("%Y%m%dT%H%M%S")


def _stdout(msg=""):
    try:
        print(msg, flush=True)
    except UnicodeEncodeError:
        # Windows 本地控制台编码（如 GBK）可能无法编码部分字符，降级为 replace
        enc = sys.stdout.encoding or "utf-8"
        sys.stdout.buffer.write((str(msg) + "\n").encode(enc, "replace"))
        sys.stdout.flush()


def _is_valid_id(s):
    if not isinstance(s, str):
        return False
    return bool(ID_RE.match(s))


# ---------------------------------------------------------------- YAML 子集解析与序列化


def _escape_dq(s):
    out = []
    for c in s:
        if c in ('"', "\\"):
            out.append("\\" + c)
        elif c == "\n":
            out.append("\\n")
        elif c == "\t":
            out.append("\\t")
        elif c == "\r":
            out.append("\\r")
        else:
            out.append(c)
    return "".join(out)


def _parse_double_quoted(text):
    body = text[1:-1]
    out = []
    i = 0
    escapes = {"n": "\n", "t": "\t", "r": "\r", "\\": "\\", '"': '"', "/": "/", "0": "\0"}
    while i < len(body):
        c = body[i]
        if c == "\\":
            i += 1
            if i >= len(body):
                raise SyntaxError("转义符后无字符")
            e = body[i]
            if e not in escapes:
                raise SyntaxError("未知转义序列: \\" + e)
            out.append(escapes[e])
        elif c == '"':
            raise SyntaxError("字符串中存在未转义的双引号")
        else:
            out.append(c)
        i += 1
    return "".join(out)


def _split_top_level(text, sep=","):
    """在顶层按分隔符切分，尊重单/双引号包裹以及方括号嵌套。"""
    parts = []
    buf = []
    depth = 0
    in_single = False
    in_double = False
    i = 0
    while i < len(text):
        c = text[i]
        if in_double:
            if c == "\\":
                buf.append(c)
                i += 1
                if i < len(text):
                    buf.append(text[i])
                i += 1
                continue
            if c == '"':
                in_double = False
            buf.append(c)
        elif in_single:
            if c == "'":
                in_single = False
            buf.append(c)
        else:
            if c == '"':
                in_double = True
                buf.append(c)
            elif c == "'":
                in_single = True
                buf.append(c)
            elif c == "[":
                depth += 1
                buf.append(c)
            elif c == "]":
                depth -= 1
                buf.append(c)
            elif c == sep and depth == 0:
                parts.append("".join(buf))
                buf = []
            else:
                buf.append(c)
        i += 1
    parts.append("".join(buf))
    return parts


def _parse_scalar(text):
    text = text.strip()
    if text == "":
        raise SyntaxError("空标量")
    if text in ("null", "Null", "NULL", "~"):
        return None
    if text in ("true", "True", "TRUE"):
        return True
    if text in ("false", "False", "FALSE"):
        return False
    if text.startswith('"'):
        if len(text) < 2 or not text.endswith('"'):
            raise SyntaxError("双引号字符串未闭合")
        return _parse_double_quoted(text)
    if text.startswith("'"):
        if len(text) < 2 or not text.endswith("'"):
            raise SyntaxError("单引号字符串未闭合")
        return text[1:-1]
    # 纯标量：数字或普通字符串
    if re.fullmatch(r"[-+]?\d+", text):
        return int(text)
    if re.fullmatch(r"[-+]?\d*\.\d+", text):
        return float(text)
    if re.search(r"[\s:,\[\]{}]", text):
        # 含空白/保留符号的未引号标量视为语法错误（严格）
        raise SyntaxError("未引号标量含非法字符")
    return text


def _parse_value(text):
    text = text.strip()
    if text == "":
        raise SyntaxError("值缺失")
    if text.startswith("["):
        if not text.endswith("]"):
            raise SyntaxError("内联数组未闭合")
        inner = text[1:-1].strip()
        if inner == "":
            return []
        items = []
        for piece in _split_top_level(inner, ","):
            items.append(_parse_scalar(piece.strip()))
        return items
    return _parse_scalar(text)


def _serialize_value(v):
    if v is None:
        return "null"
    if v is True:
        return "true"
    if v is False:
        return "false"
    if isinstance(v, int):
        return str(v)
    if isinstance(v, float):
        return str(v)
    if isinstance(v, str):
        return '"' + _escape_dq(v) + '"'
    if isinstance(v, list):
        return "[" + ", ".join(_serialize_value(x) for x in v) + "]"
    raise RetroError("无法序列化: %r" % (v,))


def _parse_frontmatter(path):
    """返回 (data:dict, body_lines:list)。失败抛 RetroError/ValueError。"""
    with open(path, "r", encoding="utf-8", newline="") as f:
        raw = f.read()
    if raw.startswith("\ufeff"):
        raw = raw[1:]  # 容忍 UTF-8 BOM
    lines = raw.split("\n")
    if not lines or lines[0].strip() != "---":
        raise ValueError("缺少 frontmatter 起始行 '---'")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip().rstrip() == "---":
            end = i
            break
    if end is None:
        raise ValueError("缺少 frontmatter 结束行 '---'")
    data = {}
    for ln, rawline in enumerate(lines[1:end], start=2):
        s = rawline.strip()
        if not s or s.startswith("#"):
            continue
        m = re.match(r"^([A-Za-z_][A-Za-z0-9_.-]*):(.*)$", s)
        if not m:
            raise ValueError("frontmatter 第 %d 行语法错误: %s" % (ln, rawline))
        key = m.group(1)
        valtext = m.group(2)
        try:
            val = _parse_value(valtext)
        except SyntaxError as e:
            raise ValueError("frontmatter 第 %d 行 (%s) 值解析错误: %s" % (ln, key, e))
        data[key] = val
    body_lines = lines[end + 1:]
    return data, body_lines


def _rebuild_content(raw, data):
    """用固定字段顺序重建 frontmatter，保留正文原样。"""
    had_bom = raw.startswith("\ufeff")
    if had_bom:
        raw = raw[1:]
    lines = raw.split("\n")
    end = None
    for i in range(1, len(lines)):
        if lines[i].strip().rstrip() == "---":
            end = i
            break
    if end is None:
        raise RetroError("无法定位 frontmatter 结束行")
    parts = ["---"]
    for key in FIELD_ORDER:
        if key in data:
            parts.append("%s: %s" % (key, _serialize_value(data[key])))
    parts.append("---")
    body_lines = lines[end + 1:]
    result = "\n".join(parts) + "\n" + "\n".join(body_lines)
    if had_bom:
        result = "\ufeff" + result
    return result


def _atomic_write(path, content):
    d = os.path.dirname(path) or "."
    fd, tmp = tempfile.mkstemp(dir=d, suffix=".tmp", prefix=".retro-")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="") as f:
            f.write(content)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            try:
                os.remove(tmp)
            except OSError:
                pass
        raise


# ---------------------------------------------------------------- 日志扫描


def _scan_logs(root):
    """扫描 .retro/log/*.md，返回 {date:{file,anchors,seen_again,entries,applied,applied_bad}}。"""
    log_dir = os.path.join(root, DIR_LOG)
    meta = {}
    if not os.path.isdir(log_dir):
        return meta
    for fn in sorted(os.listdir(log_dir)):
        if not fn.endswith(".md"):
            continue
        m = re.match(r"^(\d{4}-\d{2}-\d{2})\.md$", fn)
        if not m:
            continue
        date = m.group(1)
        anchors = set()
        seen_again = set()
        entries = {}
        applied = []  # (anchor, id, result) 合法 applied 行，按行计数不去重
        applied_bad = []  # (anchor, 原行) 格式非法的 applied 行
        current_anchor = None
        path = os.path.join(log_dir, fn)
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                ls = line.strip()
                am = re.match(r"^## s(\d+)\b", ls)
                if am:
                    current_anchor = "s" + am.group(1)
                    anchors.add(current_anchor)
                    continue
                sm = re.match(r"^>\s*seen-again:\s*(\S+)", ls)
                if sm:
                    seen_again.add(sm.group(1).strip())
                    continue
                em = re.match(r"^>\s*entry:\s*(\S+)", ls)
                if em and current_anchor:
                    entries[current_anchor] = em.group(1).strip()
                    continue
                arm = re.match(r"^>\s*applied:\s*(.*)$", ls)
                if arm:
                    rest = arm.group(1).strip()
                    pm = re.match(r"^(\S+)\s+(\S+)$", rest)
                    if pm and pm.group(2) in ("ok", "fail") and _is_valid_id(pm.group(1)):
                        applied.append((current_anchor, pm.group(1), pm.group(2)))
                    else:
                        applied_bad.append((current_anchor, ls))
        meta[date] = {"file": fn, "anchors": anchors, "seen_again": seen_again,
                      "entries": entries, "applied": applied, "applied_bad": applied_bad}
    return meta


def _compute_seen(entry, log_meta):
    """返回 (first_seen, last_seen, seen_count, raw_ref_issues:list)。"""
    issues = []
    raw_ref_dates = []
    raw_refs = entry.get("raw_ref")
    if not isinstance(raw_refs, list) or len(raw_refs) == 0:
        issues.append("raw_ref 为空")
    else:
        for r in raw_refs:
            if not isinstance(r, str):
                issues.append("raw_ref 元素非字符串")
                continue
            m = re.match(r"^log/(\d{4}-\d{2}-\d{2})\.md(#s(\d+))?$", r)
            if not m:
                issues.append("raw_ref 格式非法: %s" % r)
                continue
            date = m.group(1)
            anchor = ("s" + m.group(3)) if m.group(3) else None
            raw_ref_dates.append(date)
            # 文件存在性 + 锚点存在性由调用方（check）单独校验，这里只收集日期
    first_seen = min(raw_ref_dates) if raw_ref_dates else None
    eid = entry.get("id")
    seen_again_dates = []
    if _is_valid_id(eid):
        for date, info in log_meta.items():
            if eid in info["seen_again"]:
                seen_again_dates.append(date)
    all_dates = set(raw_ref_dates) | set(seen_again_dates)
    last_seen = max(all_dates) if all_dates else None
    seen_count = 1 + len(set(seen_again_dates)) if raw_ref_dates else (len(set(seen_again_dates)) if seen_again_dates else 0)
    return first_seen, last_seen, seen_count, issues


def _compute_applied(entry, log_meta):
    """返回 (applied_count, applied_ok)。

    applied_count=该 id 所有 applied 行总数（按行计数，不去重，每行一次应用事件）；
    applied_ok=其中 result==ok 的行数。
    """
    eid = entry.get("id")
    if not _is_valid_id(eid):
        return 0, 0
    total = 0
    ok = 0
    for info in log_meta.values():
        for _anchor, aid, result in info["applied"]:
            if aid == eid:
                total += 1
                if result == "ok":
                    ok += 1
    return total, ok


# ---------------------------------------------------------------- 文本分析（重复/矛盾）

_NORM_PUNCT = re.compile(r"[\s\.\-\+,，。、！!？?；;：:()（）\[\]【】\"'“”‘’<>《》/\\|=*_~`^#]+")


def _normalize_title(t):
    if not isinstance(t, str):
        return ""
    return _NORM_PUNCT.sub("", t.lower())


def _bigrams(s):
    return set(s[i:i + 2] for i in range(len(s) - 1))


def _jaccard(a, b):
    if not a or not b:
        return 0.0
    inter = len(a & b)
    union = len(a | b)
    if union == 0:
        return 0.0
    return inter / union


def _body_line_count(body_lines):
    return sum(1 for ln in body_lines if ln.strip())


# ---------------------------------------------------------------- 分析核心（不写文件）

ANALYSIS_NOINDEX = "__no_index__"


def _analyze(root):
    """加载并重算全部条目，返回分析字典。不做任何文件写入。

    返回 keys:
      entries: {id: {...}}           该 id 的所有最终判定
      error_list: [(id_or_file, reason)]
      warn_list:  [(id_or_file, reason)]
      index_needs_update: bool
      log_meta, dup_pairs, contra_pairs
    """
    log_meta = _scan_logs(root)
    entries_dir = os.path.join(root, DIR_ENTRIES)

    # 1) 读取所有条目文件
    raw = []
    id_map = {}
    if os.path.isdir(entries_dir):
        for fn in sorted(os.listdir(entries_dir)):
            if not fn.endswith(".md"):
                continue
            path = os.path.join(entries_dir, fn)
            try:
                data, body_lines = _parse_frontmatter(path)
            except Exception as e:
                raw.append({"path": path, "fn": fn, "data": None,
                            "body_lines": [], "parse_err": str(e)})
                continue
            unknown = [k for k in data if k not in ALLOWED_KEYS]
            raw.append({"path": path, "fn": fn, "data": data,
                        "body_lines": body_lines, "parse_err": None, "unknown": unknown})

    error_list = []
    warn_list = []
    entries = {}

    # 2) 先校验 id 唯一性/合法性
    for item in raw:
        if item["parse_err"] is not None:
            error_list.append((item["fn"], "frontmatter 解析失败: " + item["parse_err"]))
            continue
        data = item["data"]
        if data.get("unknown"):
            error_list.append((data.get("id") or item["fn"],
                               "frontmatter 存在未知 key: " + ", ".join(data.get("unknown"))))
        eid = data.get("id")
        if not _is_valid_id(eid):
            error_list.append((data.get("id") if data.get("id") is not None else item["fn"],
                               "id 缺失或格式非法（应为 YYYYMMDD-NNN）: %r" % (eid,)))
    # 重复 id
    seen_ids = {}
    for item in raw:
        if item["parse_err"] is not None or not item.get("data"):
            continue
        eid = item["data"].get("id")
        if _is_valid_id(eid):
            seen_ids.setdefault(eid, []).append(item)
    for eid, items in seen_ids.items():
        if len(items) > 1:
            error_list.append((eid, "id 重复，出现于: " + ", ".join(i["fn"] for i in items)))

    # 3) 逐条核心判定
    for item in raw:
        if item["parse_err"] is not None:
            continue
        data = item["data"]
        eid = data.get("id") if _is_valid_id(data.get("id")) else None
        rec = {"path": item["path"], "fn": item["fn"], "data": data,
               "body_lines": item["body_lines"], "eid": eid}
        if eid is None:
            # 无有效 id 的条目不进入索引，但错误已上报
            self_errors = list(item.get("unknown", []))
            entries["%s__no_id" % item["fn"]] = rec
            rec["fatal"] = True
            continue
        rec["fatal"] = False
        entries[eid] = rec

    # 4) 计算 seen
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        data = rec["data"]
        first_seen, last_seen, seen_count, seen_issues = _compute_seen(data, log_meta)
        rec["first_seen"] = first_seen
        rec["last_seen"] = last_seen
        rec["seen_count"] = seen_count
        for iss in seen_issues:
            if iss == "raw_ref 为空":
                error_list.append((eid, "raw_ref 为空"))
            else:
                error_list.append((eid, iss))
        # raw_ref 指向的文件 / 锚点存在性
        raw_refs = data.get("raw_ref")
        if isinstance(raw_refs, list) and raw_refs:
            for r in raw_refs:
                if not isinstance(r, str):
                    continue
                m = re.match(r"^log/(\d{4}-\d{2}-\d{2})\.md(#s(\d+))?$", r)
                if not m:
                    continue  # 格式非法已上报
                date = m.group(1)
                anchor = ("s" + m.group(3)) if m.group(3) else None
                if date not in log_meta:
                    error_list.append((eid, "raw_ref 指向不存在的 log 文件: %s" % r))
                elif anchor and anchor not in log_meta[date]["anchors"]:
                    error_list.append((eid, "raw_ref 锚点 %s 在 %s 中不存在" % (anchor, date)))

    # 4b) 计算 applied（log 引用行统计，按行计数不去重）
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        rec["applied_count"], rec["applied_ok"] = _compute_applied(rec["data"], log_meta)

    # 5) 计算 supersede 图谱
    superseder_of = {}  # old_id -> new_id
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        data = rec["data"]
        if data.get("supersedes") is not None:
            old = data.get("supersedes")
            if not _is_valid_id(old):
                error_list.append((eid, "supersedes 指向非法 id: %r" % (old,)))
                continue
            if old not in entries or entries.get(old, {}).get("fatal"):
                error_list.append((eid, "supersedes 指向不存在的条目: %s" % old))
                continue
            # 允许一个旧条目只有一个 superseder，后写的覆盖先写的
            superseder_of[old] = eid

    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        rec["is_superseded"] = eid in superseder_of
        rec["superseded_by"] = superseder_of.get(eid)

    # 6) 逐条 warning 判定（标题/正文/词表）
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        data = rec["data"]
        title = data.get("title")
        if not isinstance(title, str) or title == "":
            error_list.append((eid, "缺 title"))
        tags = data.get("tags")
        if not isinstance(tags, list) or len(tags) == 0:
            warn_list.append((eid, "缺 tags"))
        else:
            bad_tags = [t for t in tags if t not in VALID_TAGS]
            if bad_tags:
                warn_list.append((eid, "tags 含词表外标签: " + ", ".join(bad_tags)))
        conf = data.get("confidence")
        if conf not in VALID_CONFIDENCE:
            warn_list.append((eid, "confidence 缺失或不在词表(low|medium|high): %r" % (conf,)))
        if isinstance(title, str) and len(title) > 60:
            warn_list.append((eid, "title 超过 60 字符 (%d)" % len(title)))
        scope = data.get("scope")
        if scope is not None and not isinstance(scope, str):
            warn_list.append((eid, "scope 应为字符串（可选）: %r" % (scope,)))
        elif isinstance(scope, str) and len(scope) > 60:
            warn_list.append((eid, "scope 超过 60 字符 (%d)" % len(scope)))
        body_n = _body_line_count(rec["body_lines"])
        if body_n > 10:
            warn_list.append((eid, "正文超过 10 行 (%d)" % body_n))
        rec["body_lines_count"] = body_n

    # 6b) log 段落 entry 行校验（仅 warning，不升级为 error）
    # ① log 中 entry 行指向的 id 在 entries 中不存在
    for date, info in log_meta.items():
        for anchor, log_id in info["entries"].items():
            if log_id not in entries:
                warn_list.append((info["file"] + "#" + anchor,
                                  "log 段落 entry 行指向不存在的条目 id: %s" % log_id))
    # ② 条目 raw_ref 指向的段落若有 entry 行，其指向 id 必须与当前条目一致
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        raw_refs = rec["data"].get("raw_ref")
        if isinstance(raw_refs, list):
            for r in raw_refs:
                if not isinstance(r, str):
                    continue
                m = re.match(r"^log/(\d{4}-\d{2}-\d{2})\.md(#s(\d+))?$", r)
                if not m:
                    continue
                date = m.group(1)
                anchor = ("s" + m.group(3)) if m.group(3) else None
                if anchor and date in log_meta and anchor in log_meta[date]["entries"]:
                    log_entry_id = log_meta[date]["entries"][anchor]
                    if log_entry_id != eid:
                        warn_list.append((eid, "raw_ref %s 对应段落 entry 行指向 %s，与条目 id %s 不一致"
                                          % (r, log_entry_id, eid)))

    # 6c) log 段落 applied 行校验（仅 warning，不升级为 error；挂在 log 文件#锚点上）
    for date, info in log_meta.items():
        for anchor, aid, _result in info["applied"]:
            if aid not in entries:
                loc = info["file"] + ("#" + anchor if anchor else "")
                warn_list.append((loc, "log 段落 applied 行指向不存在的条目 id: %s" % aid))
        for anchor, raw in info["applied_bad"]:
            loc = info["file"] + ("#" + anchor if anchor else "")
            warn_list.append((loc, "applied 行格式非法（应为 `> applied: <id> <ok|fail>`）: %s" % raw))

    # 7) 重复候选 / 矛盾候选（跨条目）
    dup_pairs = []
    contra_pairs = []
    valid_titles = []
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        t = rec["data"].get("title")
        if isinstance(t, str) and t:
            valid_titles.append((eid, t))
    norm_cache = {eid: _bigrams(_normalize_title(t)) for eid, t in valid_titles}
    for i in range(len(valid_titles)):
        for j in range(i + 1, len(valid_titles)):
            eid1, t1 = valid_titles[i]
            eid2, t2 = valid_titles[j]
            b1 = norm_cache[eid1]
            b2 = norm_cache[eid2]
            jac = _jaccard(b1, b2)
            if jac >= 0.7:
                name1 = entries[eid1]["fn"]
                name2 = entries[eid2]["fn"]
                dup_pairs.append((eid1, eid2, jac))
                warn_list.append((eid1, "与其他条目是重复候选（bigram Jaccard=%.2f，%s/%s)建议合并/去重" % (jac, name1, name2)))
                warn_list.append((eid2, "与条目 %s 是重复候选（bigram Jaccard=%.2f)建议合并/去重" % (eid1, jac)))
            elif jac >= 0.5:
                t1tags = entries[eid1]["data"].get("tags") or []
                t2tags = entries[eid2]["data"].get("tags") or []
                if isinstance(t1tags, list) and isinstance(t2tags, list) and (set(t1tags) & set(t2tags)):
                    body1 = " ".join(entries[eid1]["body_lines"])
                    body2 = " ".join(entries[eid2]["body_lines"])
                    for eid in (eid1, eid2):
                        body = body1 if eid == eid1 else body2
                        has_neg = any(w in body for w in NEG_WORDS)
                        has_pos = any(w in body for w in POS_WORDS)
                        if has_neg and has_pos:
                            contra_pairs.append((eid1, eid2, jac))
                            warn_list.append((eid, "与条目 %s 是矛盾候选（同 tag 且正文既有否定词也有肯定词)建议人工仲裁或 supersedes 标记" % (eid1 if eid == eid2 else eid2,)))
                            break

    # 8) 计算每个条目的最终状态（由 index 与 check 共用）
    for eid, rec in entries.items():
        if rec.get("fatal"):
            rec["status"] = None
            continue
        rec["has_error"] = any(er[0] == eid for er in error_list)
        rec["has_warning"] = any(wr[0] == eid for wr in warn_list)
        data = rec["data"]
        stored_status = data.get("status")
        if rec["is_superseded"] and not rec["has_error"]:
            intended = "superseded"
        elif rec["has_error"]:
            intended = None  # 有错不强行改状态
        elif rec["has_warning"]:
            intended = "needs_review"
        else:
            intended = "verified"
        rec["intended_status"] = intended
        # 用于 index 落地 status 的取值（有错时保留原值或 new）
        if intended is not None:
            rec["write_status"] = intended
        elif stored_status in VALID_STATUS:
            rec["write_status"] = stored_status
        else:
            rec["write_status"] = "new"
        rec["stored_status"] = stored_status

    return {
        "entries": entries,
        "error_list": error_list,
        "warn_list": warn_list,
        "log_meta": log_meta,
        "dup_pairs": dup_pairs,
        "contra_pairs": contra_pairs,
        "superseder_of": superseder_of,
        "index_needs_update": False,
    }


# ---------------------------------------------------------------- 条目重写（index 用）


def _rewrite_derived(rec, log_meta, superseder_of):
    """为单个有效条目构建要写入的完整字段 dict（LLM 字段原样 + 派生字段）。"""
    data = rec["data"]
    new_data = {}
    for key in LLM_KEYS:
        if key in data:
            new_data[key] = data[key]
    # 派生字段
    new_data["first_seen"] = rec["first_seen"]
    new_data["last_seen"] = rec["last_seen"]
    new_data["seen_count"] = rec["seen_count"]
    # applied 两个字段始终写入（0 也写——「从未被用过」本身是信息）
    new_data["applied_count"] = rec.get("applied_count", 0)
    new_data["applied_ok"] = rec.get("applied_ok", 0)
    new_data["status"] = rec["write_status"]
    new_data["escalated"] = (data.get("escalated") is True)
    new_data["superseded_by"] = rec["superseded_by"]
    return new_data


# ---------------------------------------------------------------- 子命令：index


def _cmd_index(root):
    log_meta = _scan_logs(root)
    analysis = _analyze(root)
    entries = analysis["entries"]
    errors = analysis["error_list"]

    entries_dir = os.path.join(root, DIR_ENTRIES)
    has_any = os.path.isdir(entries_dir) and any(
        fn.endswith(".md") for fn in os.listdir(entries_dir))

    # 写回派生字段（保持 LLM 字段原样）
    written = 0
    for eid, rec in entries.items():
        if rec.get("fatal"):
            continue
        # 重新读取原始内容以精确重建 frontmatter
        with open(rec["path"], "r", encoding="utf-8") as f:
            raw = f.read()
        new_data = _rewrite_derived(rec, log_meta, analysis["superseder_of"])
        try:
            content = _rebuild_content(raw, new_data)
        except RetroError as e:
            errors.append((eid, "索引重建失败: " + str(e)))
            continue
        if content != raw:
            _atomic_write(rec["path"], content)
            written += 1

    # 重新生成 INDEX.md（含最终 status）
    facts, _ferr = _load_facts(root)
    _write_index(root, analysis, facts)

    if not has_any:
        _stdout("未发现条目，INDEX.md 已生成（空索引）。")
    for err in errors:
        _stdout("[index:error] %s: %s" % (err[0], err[1]))
    _stdout("index 完成：处理 %d 个条目，更新派生字段 %d 个文件，发现 %d 个错误。"
            % (sum(1 for e in entries.values() if not e.get("fatal")), written, len(errors)))
    return 0


def _write_index(root, analysis, facts=None):
    """用分析结果写 INDEX.md。"""
    index_path = os.path.join(root, FILE_INDEX)
    valid = []
    for eid, rec in analysis["entries"].items():
        if rec.get("fatal"):
            continue
        data = rec["data"]
        title = data.get("title")
        if not isinstance(title, str) or title == "":
            continue
        valid.append(rec)
    valid.sort(key=lambda r: (r.get("last_seen") or "", r["fn"]), reverse=True)
    lines = ["<!-- generated by retro.py index -->"]
    facts_line = _facts_summary_line(facts) if facts else None
    if facts_line:
        lines.append(facts_line)
    for rec in valid:
        eid = rec["eid"]
        data = rec["data"]
        title = data.get("title", "")
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tag_str = ",".join(str(t) for t in tags)
        status = rec["write_status"] or rec.get("stored_status") or "new"
        line = "- [%s] %s | %s | seen×%s | %s" % (eid, title, tag_str, rec["seen_count"], status)
        if rec.get("applied_count", 0) > 0:
            line += " | applied×%d(ok %d)" % (rec["applied_count"], rec.get("applied_ok", 0))
        if data.get("escalated") is True:
            line += " | ⬆"
        lines.append(line)
    # 写到 .retro/ 下，目录保证存在
    os.makedirs(os.path.dirname(index_path), exist_ok=True)
    _atomic_write(index_path, "\n".join(lines) + "\n")


# ---------------------------------------------------------------- 子命令：check


def _norm_lines(s):
    return [ln.rstrip("\r") for ln in s.split("\n")]


def _read_index_lines(root):
    index_path = os.path.join(root, FILE_INDEX)
    if not os.path.isfile(index_path):
        return None
    with open(index_path, "r", encoding="utf-8") as f:
        return f.read()


def _build_expected_index(analysis, facts=None):
    lines = ["<!-- generated by retro.py index -->"]
    facts_line = _facts_summary_line(facts) if facts else None
    if facts_line:
        lines.append(facts_line)
    valid = []
    for eid, rec in analysis["entries"].items():
        if rec.get("fatal"):
            continue
        data = rec["data"]
        title = data.get("title")
        if not isinstance(title, str) or title == "":
            continue
        valid.append(rec)
    valid.sort(key=lambda r: (r.get("last_seen") or "", r["fn"]), reverse=True)
    for rec in valid:
        eid = rec["eid"]
        data = rec["data"]
        title = data.get("title", "")
        tags = data.get("tags") or []
        if not isinstance(tags, list):
            tags = []
        tag_str = ",".join(str(t) for t in tags)
        status = rec["write_status"] or rec.get("stored_status") or "new"
        line = "- [%s] %s | %s | seen×%s | %s" % (eid, title, tag_str, rec["seen_count"], status)
        if rec.get("applied_count", 0) > 0:
            line += " | applied×%d(ok %d)" % (rec["applied_count"], rec.get("applied_ok", 0))
        if data.get("escalated") is True:
            line += " | ⬆"
        lines.append(line)
    return "\n".join(lines) + "\n"


def _cmd_check(root, write_status=True, quiet=False):
    """check 主流程。返回 (errors:list, warnings:list, analysis, status_updates)。"""
    analysis = _analyze(root)
    entries = analysis["entries"]
    errors = list(analysis["error_list"])
    warnings = list(analysis["warn_list"])
    status_updates = []

    # facts：结构校验 + verify_cmd 机械验证（验证先于 INDEX 比较——status 影响概况行）
    facts, ferr = _load_facts(root)
    if ferr is not None:
        errors.append(("facts.json", ferr))
        facts = None
    elif facts:
        for key, rec in facts.items():
            _check_fact_record(key, rec, errors)
        fact_warnings = _verify_facts(root, facts)
        warnings.extend(fact_warnings)

    # reconcile 漂移作为 warning 附带上报
    drifts = _reconcile_drifts(root, analysis)
    for d in drifts:
        warnings.append(d)

    # D6：AGENTS.md 规则行长度校验（strip 后全文 >120 字符 → warning；上限含 scope 拼接）
    agents_status, agents_rules = _read_agents_rules(root)
    if agents_status is None:
        for _rid, line in agents_rules:
            stripped = line.strip()
            if len(stripped) > 120:
                warnings.append(("reconcile", "AGENTS.md 规则行超过 120 字符（%d 字符）: %s…"
                                  % (len(stripped), stripped[:60])))

    # 状态转换直接写回（只有状态字段变化，原子写）
    if write_status:
        for eid, rec in entries.items():
            if rec.get("fatal") or rec["intended_status"] is None:
                continue
            if rec["intended_status"] != rec["stored_status"]:
                with open(rec["path"], "r", encoding="utf-8") as f:
                    raw = f.read()
                data = dict(rec["data"])
                data["status"] = rec["intended_status"]
                # 保持 LLM 字段与其它派生字段不变，仅替换 status
                data = {k: (rec["data"][k] if k in rec["data"] else (data.get(k))) for k in FIELD_ORDER}
                data["status"] = rec["intended_status"]
                try:
                    content = _rebuild_content(raw, data)
                except RetroError as e:
                    errors.append((eid, "status 写回失败: " + str(e)))
                    continue
                _atomic_write(rec["path"], content)
                status_updates.append((eid, rec["stored_status"], rec["intended_status"]))
                rec["stored_status"] = rec["intended_status"]

    # INDEX 一致性比较（用最终 status）
    expected = _build_expected_index(analysis, facts)
    # 由于上面写回只改 status，期望 INDEX 依据最终状态；与磁盘 INDEX 比较
    disk_raw = _read_index_lines(root)
    if disk_raw is None:
        errors.append(("INDEX.md", "INDEX.md 不存在，请运行 retro.py index"))
    else:
        if _norm_lines(disk_raw) != _norm_lines(expected):
            errors.append(("INDEX.md", "INDEX.md 与重算结果不一致，请运行 retro.py index"))

    return errors, warnings, analysis, status_updates


def _reconcile_drifts(root, analysis):
    drifts = []
    agents_path = os.path.join(root, FILE_AGENTS)
    rule_ids = set()
    if os.path.isfile(agents_path):
        status, rules = _read_agents_rules(root)
        if status == "markers_missing":
            drifts.append(("reconcile", "AGENTS.md 缺少 retro-managed 标记区（或标记被破坏）"))
        elif status is None:
            rule_ids = {i for i, _ in rules}
    for eid, rec in analysis["entries"].items():
        if rec.get("fatal"):
            continue
        if rec["data"].get("escalated") is True and eid not in rule_ids:
            drifts.append(("reconcile", "条目 %s escalated=true 但 AGENTS.md 无该 id 行" % eid))
    for rid in rule_ids:
        if rid not in analysis["entries"] or analysis["entries"][rid].get("fatal"):
            continue
        if analysis["entries"][rid]["data"].get("escalated") is not True:
            drifts.append(("reconcile", "AGENTS.md 含 %s 但条目 escalated=false" % rid))
    return drifts


# ---------------------------------------------------------------- AGENTS.md 规则读写


def _read_agents_rules(root):
    """返回 (status, rules)。status: None=正常; 'missing'|'markers_missing'。
    rules: [(id, full_line)]。"""
    path = os.path.join(root, FILE_AGENTS)
    if not os.path.isfile(path):
        return "missing", []
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    si = content.find(AGENTS_START)
    ei = content.find(AGENTS_END)
    if si == -1 or ei == -1 or ei < si:
        return "markers_missing", []
    region = content[si + len(AGENTS_START): ei]
    rules = []
    for line in region.split("\n"):
        ls = line.strip()
        if ls.startswith("- "):
            m = re.search(r"\[(\d{8}-\d{3})\](\s*)$", ls) or re.search(r"\[(\d{8}-\d{3})\]", ls)
            if m:
                rules.append((m.group(1), line))
    return None, rules


def _insert_rules_content(content, rule_lines):
    """在标记区内追加规则行。rule_lines: [(id, rule_text)]。返回新内容。"""
    si = content.find(AGENTS_START)
    ei = content.find(AGENTS_END)
    if si == -1 or ei == -1 or ei < si:
        raise RetroError("未找到 AGENTS.md 标记区")
    block = ""
    for _id, text in rule_lines:
        block += "\n- %s [%s]\n" % (text, _id)
    # 保持标记区可读：区块前留一行空行
    between = content[si + len(AGENTS_START): ei]
    between = between.rstrip("\n")
    new_between = between + block
    return content[:si + len(AGENTS_START)] + new_between + content[ei:]


def _remove_rules_content(content, ids):
    """从标记区删除含这些 id 的规则行。返回新内容。"""
    si = content.find(AGENTS_START)
    ei = content.find(AGENTS_END)
    if si == -1 or ei == -1 or ei < si:
        raise RetroError("未找到 AGENTS.md 标记区")
    head = content[:si + len(AGENTS_START)]
    region = content[si + len(AGENTS_START): ei]
    tail = content[ei:]
    out_lines = []
    remove_match = re.compile(r"\[(\d{8}-\d{3})\]")
    for line in region.split("\n"):
        ls = line.strip()
        if ls.startswith("- "):
            m = remove_match.search(line)
            if m and m.group(1) in ids:
                continue
        out_lines.append(line)
    # 清理因删除产生的连续空行过多
    cleaned = "\n".join(out_lines).rstrip("\n")
    return head + "\n" + cleaned + "\n" + tail if tail else head + "\n" + cleaned


# ---------------------------------------------------------------- 子命令：stats


def _cmd_stats(root):
    analysis = _analyze(root)
    entries = analysis["entries"]
    valid = [e for e in entries.values() if not e.get("fatal")]

    total = len(valid)
    active = sum(1 for e in valid if e["intended_status"] != "superseded" and not (e["is_superseded"]))
    superseded = sum(1 for e in valid if e["intended_status"] == "superseded" or e["is_superseded"])
    escalated = sum(1 for e in valid if e["data"].get("escalated") is True)
    needs_review = sum(1 for e in valid if e["intended_status"] == "needs_review")
    # 本周新增（周一为起点）
    today = datetime.date.today()
    monday = today - datetime.timedelta(days=today.weekday())
    monday_str = monday.strftime("%Y-%m-%d")
    this_week = sum(1 for e in valid if e.get("first_seen") and e["first_seen"] >= monday_str)

    # seen_count Top5
    top5 = sorted(valid, key=lambda e: (e["seen_count"], e.get("last_seen") or ""), reverse=True)[:5]

    # tag 分布
    tag_dist = {}
    for e in valid:
        tags = e["data"].get("tags")
        if isinstance(tags, list):
            for t in tags:
                tag_dist[t] = tag_dist.get(t, 0) + 1

    # INDEX.md 行数/字符数（token 估算）
    index_path = os.path.join(root, FILE_INDEX)
    index_lines = 0
    index_chars = 0
    if os.path.isfile(index_path):
        with open(index_path, "r", encoding="utf-8") as f:
            icontent = f.read()
        index_lines = icontent.count("\n")
        index_chars = len(icontent)
    token_est = int(index_chars / 1.5)

    # AGENTS.md 规则区条数
    _, rules = _read_agents_rules(root)
    agents_count = len(rules)

    # facts 统计
    facts, _ferr = _load_facts(root)
    facts_counts = {"active": 0, "stale": 0, "retired": 0}
    facts_total = 0
    facts_unverified = 0
    if facts:
        for rec in facts.values():
            if not isinstance(rec, dict):
                continue
            facts_total += 1
            if rec.get("status") in facts_counts:
                facts_counts[rec["status"]] += 1
            if rec.get("status") == "active" and not (isinstance(rec.get("verify_cmd"), str) and rec["verify_cmd"].strip()):
                facts_unverified += 1

    _stdout("=== retro stats ===")
    _stdout("条目总数: %d" % total)
    _stdout("活动数（非 superseded）: %d" % active)
    _stdout("superseded: %d" % superseded)
    _stdout("escalated: %d" % escalated)
    _stdout("needs_review: %d" % needs_review)
    _stdout("本周新增（>=%s）: %d" % (monday_str, this_week))
    # applied 统计
    applied_total = sum(e.get("applied_count", 0) for e in valid)
    applied_ok_total = sum(e.get("applied_ok", 0) for e in valid)
    applied_entries = sum(1 for e in valid if e.get("applied_count", 0) > 0)
    _stdout("applied 总次数: %d" % applied_total)
    _stdout("applied ok 次数: %d" % applied_ok_total)
    _stdout("applied 成功率: %s" % ("%.1f%%" % (applied_ok_total * 100.0 / applied_total) if applied_total else "-"))
    _stdout("被应用过的条目数: %d" % applied_entries)
    _stdout("seen_count Top5:")
    for e in top5:
        _stdout("  %s  %s  seen×%d" % (e["eid"], e["data"].get("title", ""), e["seen_count"]))
    _stdout("tag 分布:")
    if tag_dist:
        for t, c in sorted(tag_dist.items(), key=lambda kv: -kv[1]):
            _stdout("  %s: %d" % (t, c))
    else:
        _stdout("  （无）")
    _stdout("INDEX.md:")
    _stdout("  行数: %d" % index_lines)
    _stdout("  字符数: %d（token 估算 ≈ %.1f，估算值）" % (index_chars, len(icontent) / 1.5 if os.path.isfile(index_path) else 0))
    _stdout("AGENTS.md 规则区条数: %d" % agents_count)
    if facts is not None:
        _stdout("facts: %d 条（active %d / stale %d / retired %d）" % (
            facts_total, facts_counts["active"], facts_counts["stale"], facts_counts["retired"]))
        _stdout("facts 无机械验证（无 verify_cmd）: %d 条" % facts_unverified)
    return 0


# ---------------------------------------------------------------- 子命令：escalate


def _escalate_candidates(analysis, force=False):
    cands = []
    min_seen = 1 if force else 2
    for eid, rec in analysis["entries"].items():
        if rec.get("fatal"):
            continue
        status = rec["intended_status"]
        seen = rec["seen_count"]
        applied_ok = rec.get("applied_ok", 0)
        escalated = rec["data"].get("escalated") is True
        # 硬门槛：seen 达标 或 被成功应用过（force 只放宽 seen 一侧）
        if status == "verified" and (seen >= min_seen or applied_ok >= 1) and not escalated and not rec["is_superseded"]:
            tags = rec["data"].get("tags") or []
            if not isinstance(tags, list):
                tags = []
            tag_hit = bool(set(tags) & ESCALATE_TAG_BONUS)
            body_n = rec["body_lines_count"]
            body_hit = body_n <= 3
            score = seen * 2 + applied_ok * 2 + (1 if tag_hit else 0) + (1 if body_hit else 0)
            reasons = ["seen_count=%d → %d 分" % (seen, seen * 2),
                       "applied_ok=%d → %d 分" % (applied_ok, applied_ok * 2)]
            if tag_hit:
                reasons.append("命中可升级 tag(%s) → +1" % ",".join(sorted(set(tags) & ESCALATE_TAG_BONUS)))
            else:
                reasons.append("未命中可升级 tag → +0")
            if body_hit:
                reasons.append("正文≤3行 → +1")
            else:
                reasons.append("正文>3行 → +0")
            cands.append({"id": eid, "title": rec["data"].get("title", ""),
                          "score": score, "reasons": reasons, "last_seen": rec.get("last_seen"), "rec": rec})
    cands.sort(key=lambda c: (-c["score"], -c["rec"]["seen_count"], c["id"]))
    return cands


def _print_candidate_lines(cands):
    """逐条打印候选（id/title/评分/逐项理由），不带区块头与执行提示。"""
    for c in cands:
        _stdout("[%s] %s  评分=%d" % (c["id"], c["title"], c["score"]))
        for r in c["reasons"]:
            _stdout("    - %s" % r)


def _print_candidates(cands):
    _stdout("=== escalate 候选 ===")
    if not cands:
        _stdout("无满足升级门槛的候选（需 status=verified、seen_count≥2 或 applied_ok≥1、非 superseded、未 escalated）。")
        return
    _print_candidate_lines(cands)
    if cands:
        _stdout("如需执行请显式确认后：escalate --apply %s" % " ".join(c["id"] for c in cands))


def _cmd_escalate(root, dry_run, apply_ids, demote_ids, force=False):
    # 参数互斥
    if (apply_ids and demote_ids) or (dry_run and (apply_ids or demote_ids)):
        _stdout("用法错误：--dry-run 与 --apply/--demote 互斥，且 --apply/--demote 不能同时使用。")
        return 2

    analysis = _analyze(root)
    if demote_ids:
        return _escalate_demote(root, demote_ids)
    if apply_ids:
        return _escalate_apply(root, apply_ids, force)
    # 默认 / dry-run：只打印候选
    cands = _escalate_candidates(analysis)
    _print_candidates(cands)
    return 0


def _escalate_apply(root, ids, force=False):
    # 1) 先跑 check，必须 0 error
    errors, warnings, analysis, _ = _cmd_check(root, write_status=False)
    if errors:
        _stdout("apply 被拒绝：check 存在 %d 个 error，需先修复。详情：" % len(errors))
        for er in errors[:20]:
            _stdout("  [error] %s: %s" % (er[0], er[1]))
        return 1

    # 2) 校验候选/id 都存在
    entries = analysis["entries"]
    missing = [i for i in ids if i not in entries or entries[i].get("fatal")]
    if missing:
        _stdout("apply 被拒绝：存在未知条目 id: %s" % ", ".join(missing))
        return 1
    cands = _escalate_candidates(analysis, force=force)
    cand_map = {c["id"]: c for c in cands}
    not_cands = [i for i in ids if i not in cand_map]
    if not_cands:
        _stdout("apply 被拒绝：以下条目不满足升级门槛: %s" % ", ".join(not_cands))
        return 1

    agents_path = os.path.join(root, FILE_AGENTS)
    status, rules_ = _read_agents_rules(root)
    if status == "missing":
        _stdout("apply 被拒绝：AGENTS.md 不存在。")
        return 1
    if status == "markers_missing":
        _stdout("apply 被拒绝：AGENTS.md 缺少 retro-managed 标记区。")
        return 1
    rule_count = len(rules_)
    if rule_count >= 12:
        _stdout("apply 被拒绝：AGENTS.md 规则已满 %d 条（上限 12）。降级候选（按 last_seen 升序，先 --demote 再升）：" % rule_count)
        esc_entries = [e for e in entries.values() if not e.get("fatal") and e["data"].get("escalated") is True]
        esc_entries.sort(key=lambda e: (e.get("last_seen") or "", e["eid"]))
        for e in esc_entries:
            _stdout("  [%s] %s  last_seen=%s" % (e["eid"], e["data"].get("title", ""), e.get("last_seen") or "-"))
        return 1

    # 3) 生成规则行：优先取 frontmatter title（title 为空才回退正文第一非空行）
    rule_lines = []
    for i in ids:
        c = cand_map[i]
        rec = c["rec"]
        title = rec["data"].get("title")
        if isinstance(title, str) and title.strip():
            rule_text = title.strip()
        else:
            rule_text = next((ln.strip() for ln in rec["body_lines"] if ln.strip()), "")
        # 有 scope 时规则行保留适用条件：- <title>（<scope>） [id]
        scope = rec["data"].get("scope")
        if isinstance(scope, str) and scope.strip():
            rule_text += "（%s）" % scope.strip()
        rule_lines.append((i, rule_text))
    reasons = "; ".join("%s(%d分)" % (c["id"], c["score"]) for c in cand_map.values() if c["id"] in ids)
    prev_count = rule_count

    # 4) 顺序：备份 → 改条目(escalated=true) → 改 AGENTS.md → 写日志
    if not os.path.isfile(agents_path):
        _stdout("apply 被拒绝：AGENTS.md 不存在。")
        return 1
    backup = "AGENTS.md.bak." + _stamp()
    backup_path = os.path.join(root, backup)
    with open(agents_path, "r", encoding="utf-8") as f:
        agents_content = f.read()
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(agents_content)

    # 改条目 escalated=true
    for i in ids:
        rec = entries[i]
        with open(rec["path"], "r", encoding="utf-8") as f:
            raw = f.read()
        data = dict(rec["data"])
        data["escalated"] = True
        # 固定顺序重建
        new_data = {}
        for k in FIELD_ORDER:
            if k in data:
                new_data[k] = data[k]
            elif k == "status":
                new_data["status"] = rec["write_status"]
            elif k == "first_seen":
                new_data["first_seen"] = rec["first_seen"]
            elif k == "last_seen":
                new_data["last_seen"] = rec["last_seen"]
            elif k == "seen_count":
                new_data["seen_count"] = rec["seen_count"]
            elif k == "superseded_by":
                new_data["superseded_by"] = rec["superseded_by"]
        content = _rebuild_content(raw, new_data)
        _atomic_write(rec["path"], content)

    # 改 AGENTS.md
    new_agents = _insert_rules_content(agents_content, rule_lines)
    _atomic_write(agents_path, new_agents)

    # 追加日志
    esc_log = os.path.join(root, FILE_ESCALATION)
    os.makedirs(os.path.dirname(esc_log), exist_ok=True)
    payload = {
        "ts": _now().isoformat(timespec="seconds"),
        "op": "escalate",
        "ids": ids,
        "actor": "session",
        "reason": reasons,
        "prev_count": prev_count,
        "backup": backup,
    }
    if force:
        payload["force"] = True
    with open(esc_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    _stdout("已升级 %d 条规则到 AGENTS.md（备份: %s）。" % (len(ids), backup))
    for i in ids:
        _stdout("  + - %s [%s]" % (cand_map[i]["title"], i))
    return 0


def _escalate_demote(root, ids):
    analysis = _analyze(root)
    entries = analysis["entries"]
    missing = [i for i in ids if i not in entries or entries[i].get("fatal")]
    if missing:
        _stdout("demote 被拒绝：存在未知条目 id: %s" % ", ".join(missing))
        return 1

    agents_path = os.path.join(root, FILE_AGENTS)
    status, rules_ = _read_agents_rules(root)
    if status in ("missing", "markers_missing"):
        _stdout("demote 被拒绝：AGENTS.md 标记区不可用。")
        return 1
    rule_map = dict(rules_)
    not_in_rules = [i for i in ids if i not in rule_map]
    if not_in_rules:
        _stdout("demote 被拒绝：以下 id 不在 AGENTS.md 规则区: %s" % ", ".join(not_in_rules))
        return 1

    # 备份 → 改条目(escalated=false) → 改 AGENTS.md → 写日志
    backup = "AGENTS.md.bak." + _stamp()
    backup_path = os.path.join(root, backup)
    with open(agents_path, "r", encoding="utf-8") as f:
        agents_content = f.read()
    with open(backup_path, "w", encoding="utf-8") as f:
        f.write(agents_content)

    for i in ids:
        rec = entries[i]
        with open(rec["path"], "r", encoding="utf-8") as f:
            raw = f.read()
        data = dict(rec["data"])
        data["escalated"] = False
        new_data = {}
        for k in FIELD_ORDER:
            if k in data:
                new_data[k] = data[k]
            elif k == "status":
                new_data["status"] = rec["write_status"]
            elif k == "first_seen":
                new_data["first_seen"] = rec["first_seen"]
            elif k == "last_seen":
                new_data["last_seen"] = rec["last_seen"]
            elif k == "seen_count":
                new_data["seen_count"] = rec["seen_count"]
            elif k == "superseded_by":
                new_data["superseded_by"] = rec["superseded_by"]
        content = _rebuild_content(raw, new_data)
        _atomic_write(rec["path"], content)

    new_agents = _remove_rules_content(agents_content, ids)
    _atomic_write(agents_path, new_agents)

    esc_log = os.path.join(root, FILE_ESCALATION)
    os.makedirs(os.path.dirname(esc_log), exist_ok=True)
    payload = {
        "ts": _now().isoformat(timespec="seconds"),
        "op": "demote",
        "ids": ids,
        "actor": "session",
        "reason": "手动降级",
        "prev_count": len(rules_),
        "backup": backup,
    }
    with open(esc_log, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    _stdout("已降级 %d 条规则（备份: %s）。" % (len(ids), backup))
    for i in ids:
        _stdout("  - 移除规则 [%s]" % i)
    return 0


# ---------------------------------------------------------------- 子命令：reconcile


def _cmd_reconcile(root):
    analysis = _analyze(root)
    drifts = _reconcile_drifts(root, analysis)
    if drifts:
        for d in drifts:
            _stdout("[reconcile] %s" % d[1])
        _stdout("检测到 %d 处漂移。" % len(drifts))
        return 1
    _stdout("无漂移")
    return 0


# ---------------------------------------------------------------- 子命令：fact（项目事实）


def _load_facts(root):
    """读取 facts.json。返回 (facts:dict|None, error:str|None)；文件不存在返回 (None, None)。"""
    path = os.path.join(root, FILE_FACTS)
    if not os.path.isfile(path):
        return None, None
    try:
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
    except (OSError, ValueError) as e:
        return None, "facts.json 读取/解析失败: %s" % e
    if not isinstance(data, dict) or not isinstance(data.get("facts"), dict):
        return None, 'facts.json 结构非法：顶层应为 {"facts": {key: record}}'
    return data["facts"], None


def _save_facts(root, facts):
    payload = {"facts": {k: facts[k] for k in sorted(facts)}}
    _atomic_write(os.path.join(root, FILE_FACTS),
                  json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def _append_fact_log(root, **payload):
    log_path = os.path.join(root, FILE_FACTS_LOG)
    os.makedirs(os.path.dirname(log_path), exist_ok=True)
    payload = dict(payload)
    payload["ts"] = _now().isoformat(timespec="seconds")
    payload["actor"] = "session"
    with open(log_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")


def _check_fact_record(key, rec, errors):
    """结构校验单条 fact，错误追加到 errors。"""
    where = "facts/%s" % key
    if not isinstance(key, str) or not FACT_KEY_RE.match(key):
        errors.append((where, "key 语法非法（应为小写字母/数字/连字符，至少两段，如 build.test-cmd）"))
        return
    if not isinstance(rec, dict):
        errors.append((where, "记录应为对象"))
        return
    if not isinstance(rec.get("value"), str) or rec["value"].strip() == "":
        errors.append((where, "value 缺失或为空"))
    if rec.get("status") not in VALID_FACT_STATUS:
        errors.append((where, "status 非法: %r（应为 active/stale/retired）" % (rec.get("status"),)))
    for opt in ("provenance", "verify_cmd"):
        v = rec.get(opt)
        if v is not None and not isinstance(v, str):
            errors.append((where, "%s 应为字符串或 null" % opt))
    rr = rec.get("raw_ref")
    if rr is not None:
        if not isinstance(rr, list) or not all(isinstance(x, str) for x in rr):
            errors.append((where, "raw_ref 应为字符串数组"))
        else:
            for r in rr:
                if not re.match(r"^log/\d{4}-\d{2}-\d{2}\.md(#s\d+)?$", r):
                    errors.append((where, "raw_ref 格式非法: %s" % r))


def _run_verify_cmd(root, cmd):
    """在项目根目录执行 verify_cmd 断言。返回 (ok:bool, detail:str)。"""
    try:
        proc = subprocess.run(cmd, shell=True, cwd=root, capture_output=True,
                              timeout=FACT_VERIFY_TIMEOUT_SEC)
    except subprocess.TimeoutExpired:
        return False, "超时（>%ds）" % FACT_VERIFY_TIMEOUT_SEC
    except OSError as e:
        return False, "执行失败: %s" % e
    if proc.returncode == 0:
        return True, ""
    err = (proc.stderr or b"").decode("utf-8", "replace").strip()
    detail = "退出码 %d" % proc.returncode
    if err:
        detail += "：" + err[:120]
    return False, detail


def _verify_facts(root, facts):
    """check 集成：对带 verify_cmd 的 active/stale 事实跑断言，维护 status/verified_at。

    失败 → stale；恢复 → active（verified_at 刷新）；状态迁移写账本。
    verified_at 单纯刷新不记账（避免账本膨胀）。返回 warnings 列表。
    """
    warnings = []
    changed = False
    today = _today_str()
    for key, rec in facts.items():
        if not isinstance(rec, dict) or rec.get("status") == "retired":
            continue
        cmd = rec.get("verify_cmd")
        if not isinstance(cmd, str) or not cmd.strip():
            continue  # 无断言命令的事实不参与机械验证（审计轮人工复核）
        ok, detail = _run_verify_cmd(root, cmd)
        if ok:
            if rec.get("status") == "stale":
                _append_fact_log(root, op="reactivate", key=key,
                                 old_status="stale", new_status="active")
                rec["status"] = "active"
                changed = True
                warnings.append(("facts/%s" % key, "verify_cmd 通过，stale -> active（已恢复）"))
            if rec.get("verified_at") != today:
                rec["verified_at"] = today
                changed = True
        else:
            if rec.get("status") != "stale":
                _append_fact_log(root, op="stale", key=key,
                                 old_status=rec.get("status"), new_status="stale",
                                 detail=detail)
                rec["status"] = "stale"
                changed = True
                warnings.append(("facts/%s" % key,
                                 "verify_cmd 失败（%s），已标 stale：值可能已过期，勿直接采信" % detail))
    if changed:
        _save_facts(root, facts)
    return warnings


def _facts_summary_line(facts):
    """INDEX.md 头部的 facts 概况行；无事实返回 None。"""
    if not facts:
        return None
    counts = {"active": 0, "stale": 0, "retired": 0}
    for rec in facts.values():
        if isinstance(rec, dict) and rec.get("status") in counts:
            counts[rec["status"]] += 1
    return ("<!-- facts: %d active / %d stale / %d retired —— retro.py fact list -->"
            % (counts["active"], counts["stale"], counts["retired"]))


def _cmd_fact_add(root, facts, args):
    key = args.key
    if not FACT_KEY_RE.match(key):
        _stdout("[retro error] key 语法非法: %s（应为小写字母/数字/连字符，至少两段，如 build.test-cmd）" % key)
        return 1
    if not args.value or not args.value.strip():
        _stdout("[retro error] value 不能为空")
        return 1
    today = _today_str()
    old = facts.get(key) if isinstance(facts.get(key), dict) else None

    def _opt(new, field):
        """显式传参优先（空串归一为 None 以支持清除），否则保留旧值。"""
        if new is not None:
            return new.strip() or None
        return (old or {}).get(field)

    rec = {
        "value": args.value,
        "provenance": _opt(args.source, "provenance"),
        "verify_cmd": _opt(args.verify_cmd, "verify_cmd"),
        "raw_ref": (args.ref if args.ref else (old or {}).get("raw_ref")),
        "status": "active",  # 登记即当前有效；retire 仅由显式命令设置
        "first_seen": (old or {}).get("first_seen") or today,
        "verified_at": today,  # 登记即视为本会话已验证（值来自本次实测/读取）
    }
    facts[key] = rec
    os.makedirs(os.path.join(root, ".retro"), exist_ok=True)
    _save_facts(root, facts)
    if old is None:
        _append_fact_log(root, op="add", key=key, new_value=rec["value"])
        _stdout("已登记事实 %s = %s（active）" % (key, rec["value"]))
    else:
        old_value = old.get("value")
        if old.get("status") != "active":
            # 人工复核恢复（stale/retired → active）也是状态迁移，必须记账
            _append_fact_log(root, op="reactivate", key=key,
                             old_status=old.get("status"), new_status="active")
            _stdout("事实 %s 已从 %s 重新激活为 active（本会话重新确认）。"
                    % (key, old.get("status")))
        if old_value == rec["value"]:
            _append_fact_log(root, op="touch", key=key, new_value=rec["value"],
                             reason=args.reason or "")
            _stdout("事实 %s 已存在且值未变，已刷新 verified_at 与元数据。" % key)
        else:
            _append_fact_log(root, op="update", key=key, old_value=old_value,
                             new_value=rec["value"], reason=args.reason or "")
            _stdout("已更新事实 %s：%s -> %s（旧值已入账本 facts.log.jsonl）" % (key, old_value, rec["value"]))
    return 0


def _cmd_fact_get(facts, key):
    rec = facts.get(key)
    if not isinstance(rec, dict):
        _stdout("[retro error] 事实不存在: %s（retro.py fact list 查看全部）" % key)
        return 1
    _stdout("key:     %s" % key)
    _stdout("value:   %s" % rec.get("value"))
    _stdout("status:  %s" % rec.get("status"))
    if rec.get("provenance"):
        _stdout("source:  %s" % rec["provenance"])
    if rec.get("verify_cmd"):
        _stdout("verify:  %s" % rec["verify_cmd"])
    if rec.get("raw_ref"):
        _stdout("ref:     %s" % ", ".join(rec["raw_ref"]))
    _stdout("first_seen: %s   verified_at: %s" % (rec.get("first_seen"), rec.get("verified_at")))
    if rec.get("status") == "stale":
        _stdout("⚠ stale：上次机械验证失败，此值可能已过期；修复后跑 retro.py check 自动复验。")
    elif rec.get("status") == "retired":
        _stdout("已退役：仅作历史记录，勿再采信。")
    return 0


def _cmd_fact_list(facts, prefix, status):
    rows = []
    for key in sorted(facts):
        rec = facts[key]
        if not isinstance(rec, dict):
            continue
        if prefix and not key.startswith(prefix):
            continue
        st = rec.get("status", "?")
        if status != "all" and st != status:
            continue
        val = str(rec.get("value", ""))
        if len(val) > 60:
            val = val[:57] + "..."
        rows.append((key, st, val))
    if not rows:
        _stdout("（无匹配事实：prefix=%s status=%s）" % (prefix or "*", status))
        return 0
    for key, st, val in rows:
        _stdout("%-32s %-8s %s" % (key, st, val))
    _stdout("共 %d 条。查询单条: retro.py fact get <key>" % len(rows))
    return 0


def _cmd_fact_retire(root, facts, key, reason):
    rec = facts.get(key)
    if not isinstance(rec, dict):
        _stdout("[retro error] 事实不存在: %s" % key)
        return 1
    old_status = rec.get("status")
    rec["status"] = "retired"
    _save_facts(root, facts)
    _append_fact_log(root, op="retire", key=key, old_status=old_status,
                     new_status="retired", reason=reason)
    _stdout("已退役 %s（原因: %s）。历史值仍在账本 facts.log.jsonl 可查。" % (key, reason))
    return 0


def _cmd_fact(root, args):
    facts, err = _load_facts(root)
    if err is not None:
        _stdout("[retro error] " + err)
        return 1
    facts = facts if facts is not None else {}
    if args.fact_cmd == "add":
        return _cmd_fact_add(root, facts, args)
    if args.fact_cmd == "get":
        return _cmd_fact_get(facts, args.key)
    if args.fact_cmd == "list":
        return _cmd_fact_list(facts, args.prefix, args.status)
    if args.fact_cmd == "retire":
        return _cmd_fact_retire(root, facts, args.key, args.reason)
    _stdout("未知 fact 子命令: %s" % args.fact_cmd)
    return 2


# ---------------------------------------------------------------- 子命令：audit（审计轮）

_RULE_ID_RE = re.compile(r"\[(\d{8}-\d{3})\]")


def _parse_date(s):
    """解析 'YYYY-MM-DD' 前缀日期，解析失败返回 None。"""
    if not isinstance(s, str):
        return None
    try:
        return datetime.datetime.strptime(s[:10], "%Y-%m-%d").date()
    except ValueError:
        return None


def _count_rule_blank_drift(root):
    """标记区内「规则行→空行→规则行」计数；无标记区返回 None。"""
    path = os.path.join(root, FILE_AGENTS)
    if not os.path.isfile(path):
        return None
    with open(path, "r", encoding="utf-8") as f:
        content = f.read()
    si = content.find(AGENTS_START)
    ei = content.find(AGENTS_END)
    if si == -1 or ei == -1 or ei < si:
        return None

    def is_rule(ln):
        ls = ln.strip()
        return ls.startswith("- ") and bool(_RULE_ID_RE.search(ls))

    region = content[si + len(AGENTS_START): ei]
    lines = region.split("\n")
    n = 0
    for i in range(len(lines) - 2):
        if is_rule(lines[i]) and not lines[i + 1].strip() and is_rule(lines[i + 2]):
            n += 1
    return n


def _read_audit_records(root):
    """读取全部审计记录（追加式文件，坏行跳过）。文件不存在 → []。"""
    records = []
    path = os.path.join(root, FILE_AUDIT)
    if os.path.isfile(path):
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    records.append(json.loads(line))
                except ValueError:
                    continue
    return records


def _read_dismissal_memory(records):
    """汇总所有审计记录的驳回名单：{id: 最近一次驳回轮次 ts}。"""
    mem = {}
    for rec in records:
        for did in rec.get("dismissed") or []:
            if isinstance(did, str) and did:
                mem[did] = rec.get("ts", "")
    return mem


def _dismiss_info(eid, dismiss_mem, today):
    """驳回记忆查询。返回 (silent:bool, marker:str)。

    silent=True → 该 id 处于静默期，应从候选剔除；
    marker 非空 → 驳回已超期，照常输出并在行尾标注请复查。
    """
    ts = dismiss_mem.get(eid)
    if not ts:
        return False, ""
    d = _parse_date(ts)
    if d is None:
        return False, ""
    days = (today - d).days
    if days < AUDIT_INTERVAL_DAYS:
        return True, ""
    return False, "（历史驳回 ≥%d天，请复查）" % days


def _escalation_logged_ids(root):
    """escalation.log.jsonl 中所有 escalate op 的 ids 并集（覆盖率分子用）。"""
    ids = set()
    path = os.path.join(root, FILE_ESCALATION)
    if not os.path.isfile(path):
        return ids
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if rec.get("op") == "escalate" and isinstance(rec.get("ids"), list):
                for i in rec["ids"]:
                    if isinstance(i, str):
                        ids.add(i)
    return ids


def _cmd_audit(root, summary=None, dismissed=None, close=False):
    """审计轮：无参干跑输出六区块只读报告；--close 落账（写 audit.log.jsonl，追加式）。"""
    if close:
        if not summary or not summary.strip():
            _stdout("用法错误：--close 需要本轮总结文本。")
            return 2
        return _cmd_audit_close(root, summary.strip(), dismissed or [])
    if dismissed:
        _stdout("用法错误：--dismissed 仅配合 --close 使用。")
        return 2

    # 只读复用：_cmd_check(write_status=False) 内部这一次 _analyze 供六个区块共享
    errors, warnings, analysis, _updates = _cmd_check(root, write_status=False)
    today = datetime.date.today()
    records = _read_audit_records(root)
    dismiss_mem = _read_dismissal_memory(records)

    _stdout("=== audit ===")

    # ① 健康检查
    _stdout("① 健康检查")
    _stdout("[errors] %d" % len(errors))
    _stdout("[warnings] %d" % len(warnings))
    agents_status, rules = _read_agents_rules(root)
    if agents_status is not None:
        rules = []
        why = "文件缺失" if agents_status == "missing" else "标记被破坏"
        _stdout("AGENTS.md 无可用标记区（%s），规则相关区块仅供参考" % why)
    else:
        _stdout("规则区格式漂移（规则行→空行→规则行）: %d 处" % _count_rule_blank_drift(root))
        _stdout("规则区规则数: %d/12" % len(rules))
        if len(rules) >= AUDIT_RULES_WARN_THRESHOLD:
            _stdout("! 规则区 %d/12，剩余 %d 空位，接近上限" % (len(rules), 12 - len(rules)))
    if errors:
        _stdout("存在 %d 个 check error，请先运行 check 修复数据（本次退出码 1）。" % len(errors))
    rule_ids = {i for i, _ in rules}

    # ② 升级候选（与 escalate 干跑同源）
    _stdout("② 升级候选")
    cands = _escalate_candidates(analysis)
    if not cands:
        _stdout("无满足升级门槛的候选（需 status=verified、seen_count≥2 或 applied_ok≥1、非 superseded、未 escalated）。")
    else:
        _print_candidate_lines(cands)
        _stdout("如需执行请显式确认后：escalate --apply %s" % " ".join(c["id"] for c in cands))

    # ③ 降级候选：已升级且在规则区的条目，(last_seen 升序, seen_count 升序, applied_count 升序) top 5
    _stdout("③ 降级候选")
    demote_pool = [e for e in analysis["entries"].values()
                   if not e.get("fatal") and e["data"].get("escalated") is True and e["eid"] in rule_ids]
    demote_pool.sort(key=lambda e: (e.get("last_seen") or "", e["seen_count"],
                                    e.get("applied_count", 0)))
    demote_kept = []
    for e in demote_pool:
        silent, marker = _dismiss_info(e["eid"], dismiss_mem, today)
        if not silent:
            demote_kept.append((e, marker))
    demote_top = demote_kept[:5]
    if len(rule_ids) < AUDIT_RULES_WARN_THRESHOLD:
        _stdout("规则区 %d/12，未到预警线，仅供参考" % len(rule_ids))
    if not demote_pool:
        _stdout("（无降级候选：已升级且仍在规则区的条目为空）")
    elif not demote_top:
        _stdout("（降级候选均处于历史驳回静默期，%d 天内不重复浮出）" % AUDIT_INTERVAL_DAYS)
    for e, marker in demote_top:
        _stdout("[%s] %s | last_seen=%s | seen×%s | applied×%s(ok %s)%s"
                % (e["eid"], e["data"].get("title", ""), e.get("last_seen") or "-",
                   e["seen_count"], e.get("applied_count", 0), e.get("applied_ok", 0), marker))

    # ④ 失效候选（两档）
    _stdout("④ 失效候选")
    fail_cands = []
    stale_cands = []
    for e in analysis["entries"].values():
        if e.get("fatal"):
            continue
        ac = e.get("applied_count", 0)
        if ac > 0 and e.get("applied_ok", 0) == 0:
            fail_cands.append(e)
        elif ac == 0 and e.get("seen_count") == 1:
            ls = _parse_date(e.get("last_seen"))
            if ls is not None and (today - ls).days >= AUDIT_STALE_DAYS:
                stale_cands.append(e)
    fail_cands.sort(key=lambda e: (e.get("last_seen") or "", e["eid"]))
    stale_cands.sort(key=lambda e: (e.get("last_seen") or "", e["eid"]))
    fail_kept = []
    for e in fail_cands:
        silent, marker = _dismiss_info(e["eid"], dismiss_mem, today)
        if not silent:
            fail_kept.append((e, marker))
    stale_kept = []
    for e in stale_cands:
        silent, marker = _dismiss_info(e["eid"], dismiss_mem, today)
        if not silent:
            stale_kept.append((e, marker))
    stale_top = stale_kept[:5]
    _stdout("证据档（applied_count>0 且 applied_ok==0，fail 由 count-ok 现算）:")
    if not fail_kept:
        _stdout("  （无）")
    for e, marker in fail_kept:
        _stdout("  [%s] %s | last_seen=%s | seen×%s | applied×%s(ok 0)%s"
                % (e["eid"], e["data"].get("title", ""), e.get("last_seen") or "-",
                   e["seen_count"], e["applied_count"], marker))
    _stdout("疑似档（无 applied 数据 × seen×1 × ≥%d 天未现，供 LLM 复核）:" % AUDIT_STALE_DAYS)
    if not stale_top:
        _stdout("  （无）")
    for e, marker in stale_top:
        _stdout("  [%s] %s | last_seen=%s | seen×%s%s"
                % (e["eid"], e["data"].get("title", ""), e.get("last_seen") or "-",
                   e["seen_count"], marker))

    # ⑤ 重复/合并候选
    _stdout("⑤ 重复/合并候选")
    pair_out = []
    for eid1, eid2, jac in analysis["dup_pairs"]:
        silent1, marker1 = _dismiss_info(eid1, dismiss_mem, today)
        silent2, marker2 = _dismiss_info(eid2, dismiss_mem, today)
        if silent1 or silent2:
            continue
        pair_out.append((eid1, eid2, jac, marker1 or marker2))
    if not pair_out:
        if analysis["dup_pairs"]:
            _stdout("（重复候选均处于历史驳回静默期，%d 天内不重复浮出）" % AUDIT_INTERVAL_DAYS)
        else:
            _stdout("（无）")
    for eid1, eid2, jac, marker in pair_out:
        _stdout("[%s] <-> [%s] Jaccard=%.2f%s" % (eid1, eid2, jac, marker))

    # ⑥ 审计状态
    _stdout("⑥ 审计状态")
    if not records:
        _stdout("从未审计")
    else:
        last_ts = records[-1].get("ts") or ""
        last_date = _parse_date(last_ts)
        if last_date is None:
            _stdout("上次审计 ts 无法解析: %r" % last_ts)
        else:
            days = (today - last_date).days
            _stdout("上次审计: %s（距今 %d 天）" % (last_ts[:10], days))
            new_count = sum(1 for e in analysis["entries"].values()
                            if not e.get("fatal") and e.get("first_seen")
                            and e["first_seen"] > last_ts[:10])
            _stdout("期间新增条目: %d 条" % new_count)
            if new_count >= AUDIT_NEW_ENTRIES_TRIGGER:
                _stdout("期间新增 ≥%d 条，达到触发阈值" % AUDIT_NEW_ENTRIES_TRIGGER)
            left = AUDIT_INTERVAL_DAYS - days
            if left > 0:
                _stdout("下次建议审计: 还剩 %d 天（建议 %s）"
                        % (left, (last_date + datetime.timedelta(days=AUDIT_INTERVAL_DAYS)).isoformat()))
            else:
                _stdout("下次建议审计: 已到期 %d 天" % (-left))
    esc_ids = _escalation_logged_ids(root)
    if rule_ids:
        covered = len(rule_ids & esc_ids)
        _stdout("escalation 覆盖: 规则区 %d 条中 %d 条有 escalate 记录（%d/%d）"
                % (len(rule_ids), covered, covered, len(rule_ids)))
    else:
        _stdout("escalation 覆盖: 规则区为空")
    if dismiss_mem:
        for did, ts in sorted(dismiss_mem.items()):
            _stdout("历史驳回: %s（最近驳回 %s）" % (did, ts[:10] if ts else "-"))
    else:
        _stdout("历史驳回名单: （无）")

    return 1 if errors else 0


def _cmd_audit_close(root, summary, dismissed):
    """审计落账：先校验（check 0 error + dismissed id 存在），再追加一行 audit.log.jsonl。"""
    errors, _w, analysis, _u = _cmd_check(root, write_status=False)
    if errors:
        _stdout("审计落账被拒绝：check 存在 %d 个 error，需先修复。详情：" % len(errors))
        for er in errors[:20]:
            _stdout("  [error] %s: %s" % (er[0], er[1]))
        return 1
    entries = analysis["entries"]
    bad = [i for i in dismissed if i not in entries or entries[i].get("fatal")]
    if bad:
        _stdout("审计落账被拒绝：--dismissed 存在未知条目 id: %s" % ", ".join(bad))
        return 1
    n_entries = sum(1 for e in entries.values() if not e.get("fatal"))
    _s, rules = _read_agents_rules(root)
    n_rules = len(rules)
    n_esc = sum(1 for e in entries.values() if not e.get("fatal") and e["data"].get("escalated") is True)

    audit_path = os.path.join(root, FILE_AUDIT)
    os.makedirs(os.path.dirname(audit_path), exist_ok=True)
    payload = {
        "ts": _now().isoformat(timespec="seconds"),
        "stats": {"entries": n_entries, "rules": n_rules, "escalated": n_esc},
        "summary": summary,
        "dismissed": dismissed,
    }
    with open(audit_path, "a", encoding="utf-8") as f:
        f.write(json.dumps(payload, ensure_ascii=False) + "\n")

    next_date = datetime.date.today() + datetime.timedelta(days=AUDIT_INTERVAL_DAYS)
    _stdout("审计落账成功。")
    if dismissed:
        _stdout("本轮驳回 %d 条: %s" % (len(dismissed), ", ".join(dismissed)))
    else:
        _stdout("本轮驳回 0 条。")
    _stdout("下轮建议审计日期: %s（%d 天后）" % (next_date.isoformat(), AUDIT_INTERVAL_DAYS))
    return 0


# ---------------------------------------------------------------- 入口


def _build_parser():
    parser = argparse.ArgumentParser(
        prog="retro.py",
        description="retro 记忆系统方案 C：确定性脚本（index/check/stats/escalate/reconcile）。",
    )
    parser.add_argument("--root", default=".", help="项目根目录（默认当前目录）")
    subs = parser.add_subparsers(dest="subcommand", required=True)

    subs.add_parser("index", help="重算派生字段并重新生成 INDEX.md")
    subs.add_parser("check", help="校验条目一致性（只读+状态回归），exit 1 表有 error")
    subs.add_parser("stats", help="统计信息")
    p = subs.add_parser("escalate", help="候选/升级/降级规则到 AGENTS.md")
    p.add_argument("--dry-run", action="store_true", help="仅打印候选与理由")
    p.add_argument("--apply", nargs="+", metavar="ID", help="将指定条目升级为 AGENTS.md 规则")
    p.add_argument("--force", action="store_true", help="绕过 seen_count≥2 门槛（仅限迁移/紧急场景，审计记录 force:true）")
    p.add_argument("--demote", nargs="+", metavar="ID", help="将指定条目从 AGENTS.md 规则降级")
    subs.add_parser("reconcile", help="检查 AGENTS.md 与条目 escalated 状态的漂移")
    p = subs.add_parser("fact", help="项目事实存取：add / get / list / retire")
    fsubs = p.add_subparsers(dest="fact_cmd", required=True)
    pa = fsubs.add_parser("add", help="登记/更新一条事实（key 已存在则更新，旧值入账本）")
    pa.add_argument("key", help="事实 key，至少两段小写字母/数字/连字符，如 build.test-cmd")
    pa.add_argument("value", help="事实值（当前状态，如命令/路径/类名/数字）")
    pa.add_argument("--source", help="来源说明（哪个文件/哪次实测得出）")
    pa.add_argument("--verify-cmd", dest="verify_cmd",
                    help="可重跑的只读断言命令，check 时自动验证；传空串可清除")
    pa.add_argument("--ref", action="append",
                    help="溯源 log 段落 log/YYYY-MM-DD.md#sN，可多次")
    pa.add_argument("--reason", help="值变化时的更新理由")
    pg = fsubs.add_parser("get", help="精确查询一条事实（stale 会给过期警告）")
    pg.add_argument("key")
    pl = fsubs.add_parser("list", help="列出事实（可按前缀/状态过滤）")
    pl.add_argument("--prefix", default="", help="key 前缀过滤，如 build.")
    pl.add_argument("--status", choices=["active", "stale", "retired", "all"], default="all")
    pr = fsubs.add_parser("retire", help="退役一条事实（功能下线/不再相关，不删除）")
    pr.add_argument("key")
    pr.add_argument("--reason", required=True, help="退役原因")
    p = subs.add_parser("audit", help="审计轮：六区块只读报告（干跑）/ --close 落账")
    p.add_argument("--close", metavar="SUMMARY", help="落账本轮审计总结（与干跑互斥，必填总结文本）")
    p.add_argument("--dismissed", metavar="ID1,ID2", help="本轮驳回的条目 id，逗号分隔（仅配合 --close）")
    return parser


def _check_echo_out(errors, warnings, title="check", quiet_errors=False):
    _stdout("=== %s ===" % title)
    _stdout("[errors] %d" % len(errors))
    for er in errors:
        _stdout("  - %s: %s" % (er[0], er[1]))
    _stdout("[warnings] %d" % len(warnings))
    for wr in warnings:
        _stdout("  - %s: %s" % (wr[0], wr[1]))
    if any(e[0] == "INDEX.md" for e in errors):
        _stdout("提示：INDEX.md 已过期，请运行 retro.py index")
    if not errors and not warnings:
        _stdout("CHECK PASSED")


def main(argv=None):
    parser = _build_parser()
    args = parser.parse_args(argv)
    root = os.path.abspath(args.root)
    sub = args.subcommand

    try:
        if sub == "index":
            return _cmd_index(root)
        if sub == "check":
            errors, warnings, analysis, updates = _cmd_check(root, write_status=True)
            _check_echo_out(errors, warnings, "check")
            for eid, old, new in updates:
                _stdout("已更新 status（%s）: %s -> %s" % (eid, old, new))
            return 1 if errors else 0
        if sub == "stats":
            return _cmd_stats(root)
        if sub == "escalate":
            return _cmd_escalate(root, args.dry_run, args.apply, args.demote, args.force)
        if sub == "reconcile":
            return _cmd_reconcile(root)
        if sub == "fact":
            return _cmd_fact(root, args)
        if sub == "audit":
            dismissed_list = None
            if args.dismissed:
                dismissed_list = [s.strip() for s in args.dismissed.split(",") if s.strip()]
            return _cmd_audit(root, summary=args.close, dismissed=dismissed_list,
                              close=args.close is not None)
        _stdout("未知子命令: %s" % sub)
        return 2
    except RetroError as e:
        _stdout("[retro error] " + str(e))
        return 1
    except Exception as e:  # 防御性兜底
        import traceback
        _stdout("[retro 内部错误] " + str(e))
        traceback.print_exc(file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
