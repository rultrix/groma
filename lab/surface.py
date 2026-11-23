"""把 Unit 的投影放到各 Harness 的自动装载面上，只负责"标记块"。

三条硬规矩，都有测试守着：

- 块外的字节永远不动（用户手写的内容不是我们的存储层）；
- 内容没变就不写（否则 mtime 抖动会让工作区的 git 一直显示 modified）；
- 块内只有 `unit_id` / `asset_id` 和内容本身。本机指纹（`ws_ref`、时间戳、
  前后 sha）留在 `.groma/injections/manifest.json` —— 那个目录被 gitignore 覆盖，
  而标记块待在 `CLAUDE.md` 这类会被提交的文件里。

哪个面"保证生效"是另一件事：本模块只保证写入正确，`TRUST` 里诚实标注实测状态。
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

from .contract import (
    LEGACY_MARKER_END,
    LEGACY_MARKER_START,
    MARKER_END,
    MARKER_START,
    RULE_FILE,
)
from .state import workspace_ref
from .workspace import LabError, meta_dir

INJECT_DIR = "injections"
MANIFEST_FILE = "manifest.json"
BACKUP_DIR = "backups"
PAYLOAD_FILE = "payload.md"

START_PREFIX = MARKER_START
END_MARK = MARKER_END

# 块面：会出现在项目里、可能被版本库跟踪的文件。写它们只写标记块。
SURFACES = {
    "qoder-rule": {
        "path": RULE_FILE,
        "project_file": False,
        "trust": "conditional",
        "note": "Qoder 规则文件按需加载（描述匹配才读），不保证进上下文",
    },
    "claude-md": {
        "path": "CLAUDE.md",
        "project_file": True,
        "trust": "verified-read",
        "note": "2026-11-04 本机 claude -p A/B 实测：项目根 CLAUDE.md 与 Groma 标记块都被读入",
    },
    "codex-agents": {
        "path": "AGENTS.md",
        "project_file": True,
        "trust": "unverified",
        "note": "Codex 走 desktop 启动，本机无法非交互复现；这一格仍是 NOT_VERIFIED",
    },
}

# Qoder 不靠块面：Groma 生成的 hook 直接读 payload，项目里不多写一个文件。
HOOK_HARNESSES = {"qoder-cn", "qoder"}

# 载荷引文的段级预算（先到先得，整条退出不截断）：
# 条数上限与字符上限都远小于 hook 的 60000 字符跳过线，是构造性保证。
CLAIM_MAX_ITEMS = 10
CLAIM_MAX_CHARS = 8000


def block_of(unit_id: str, asset_id: str, body: str) -> str:
    """渲染标记块。块内不写任何本机指纹，见模块 docstring。"""
    return "\n".join(
        [
            "<!-- groma:inject unit=%s asset=%s -->" % (unit_id, asset_id),
            "",
            body.strip(),
            "",
            END_MARK,
            "",
        ]
    )


def _slice(text: str) -> tuple[int, int] | None:
    """The byte range of our block, old marker included, or None when absent."""
    for start_prefix, end_mark in ((START_PREFIX, END_MARK), (LEGACY_MARKER_START, LEGACY_MARKER_END)):
        start = text.find(start_prefix)
        if start < 0:
            continue
        end_of_line = text.find("\n", start)
        if end_of_line < 0:
            return None
        end = text.find(end_mark, end_of_line)
        if end < 0:
            return None
        tail = text.find("\n", end)
        return (start, len(text) if tail < 0 else tail + 1)
    return None


def has_block(path: Path) -> bool:
    return path.is_file() and _slice(path.read_text(encoding="utf-8")) is not None


def _sha(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _atomic_write(path: Path, text: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(text, encoding="utf-8", newline="\n")
    os.replace(tmp, path)


def _backup(root: Path, target: Path, original: str) -> Path:
    """Keep the first-ever pre-edit bytes, so removal can be byte-exact."""
    slot = meta_dir(root) / INJECT_DIR / BACKUP_DIR / workspace_ref(target) / target.name
    if not slot.is_file():
        slot.parent.mkdir(parents=True, exist_ok=True)
        slot.write_text(original, encoding="utf-8", newline="\n")
    return slot


def apply_block(root: Path, relative: str, unit_id: str, asset_id: str, body: str) -> dict:
    """Write (or refresh) only our block inside `root/relative`."""
    target = root / relative
    existed = target.is_file()
    original = target.read_text(encoding="utf-8") if existed else ""
    block = block_of(unit_id, asset_id, body)
    span = _slice(original)

    if span is not None and original[span[0] : span[1]] == block:
        return {"path": relative, "changed": False, "created": False, "block_sha256": _sha(block)}

    if span is None:
        updated = block if not original.strip() else original.rstrip("\n") + "\n\n" + block
    else:
        updated = original[: span[0]] + block + original[span[1] :]

    backup = _backup(root, target, original)  # also when we are about to create the file
    _atomic_write(target, updated)
    return {
        "path": relative,
        "changed": True,
        "created": not existed,
        "block_sha256": _sha(block),
        "file_sha256_before": _sha(original),
        "file_sha256_after": _sha(updated),
        "untouched_outside": _outside_intact(original, updated, span),
        "backup": str(backup),
    }


def _outside_intact(original: str, updated: str, span: tuple[int, int] | None) -> bool:
    """Did we leave everything outside our block alone?

    The first write appends, so the surrounding text has to survive as a prefix;
    a refresh replaces in place, and then the outside must match byte for byte.
    """
    if span is None:
        return _outside(updated).rstrip("\n") == original.rstrip("\n")
    return _outside(updated) == _outside(original)


def _outside(text: str) -> str:
    """Everything that is not our block. Comparing it proves we did not touch the rest."""
    span = _slice(text)
    if span is None:
        return text
    return text[: span[0]] + text[span[1] :]


def remove_block(root: Path, relative: str) -> dict:
    """Take our block out again.

    When nobody edited the file besides us, restore the pre-edit bytes exactly;
    otherwise only cut the block, so a hand-written line is never overwritten.
    """
    target = root / relative
    if not target.is_file():
        return {"path": relative, "changed": False, "reason": "absent"}
    current = target.read_text(encoding="utf-8")
    span = _slice(current)
    slot = meta_dir(root) / INJECT_DIR / BACKUP_DIR / workspace_ref(target) / target.name
    if slot.is_file():
        original = slot.read_text(encoding="utf-8")
        # A backup that itself carries a block (an old marker, or a pre-rename
        # write) must not be restored block and all -- cut the block instead.
        if _slice(original) is None and (_outside(current) == original or not original.strip()):
            if not original.strip():
                target.unlink()
                return {"path": relative, "changed": True, "mode": "deleted", "reason": "we created it"}
            _atomic_write(target, original)
            return {"path": relative, "changed": True, "mode": "restored"}
    if span is None:
        return {"path": relative, "changed": False, "reason": "no block"}
    _atomic_write(target, _outside(current).rstrip("\n") + ("\n" if _outside(current).strip() else ""))
    return {"path": relative, "changed": True, "mode": "block-removed"}


def status(root: Path, relative: str) -> dict:
    target = root / relative
    if not target.is_file():
        return {"path": relative, "present": False, "file": False}
    text = target.read_text(encoding="utf-8")
    span = _slice(text)
    return {
        "path": relative,
        "present": span is not None,
        "file": True,
        "block_sha256": _sha(text[span[0] : span[1]]) if span else None,
        "file_sha256": _sha(text),
    }


def payload_path(root: Path) -> Path:
    """The hook's source of truth: inside our own directory, never in the project."""
    return meta_dir(root) / INJECT_DIR / PAYLOAD_FILE


def write_payload(root: Path, text: str) -> dict:
    """Idempotent payload write -- an unchanged body touches no bytes."""
    path = payload_path(root)
    body = text.strip() + "\n"
    previous = path.read_text(encoding="utf-8") if path.is_file() else None
    if previous == body:
        return {"path": path.as_posix(), "changed": False, "sha256": _sha(body)}
    _atomic_write(path, body)
    return {
        "path": path.as_posix(),
        "changed": True,
        "sha256": _sha(body),
        "previous_sha256": _sha(previous) if previous is not None else None,
    }


def remove_payload(root: Path) -> bool:
    path = payload_path(root)
    if path.is_file():
        path.unlink()
        return True
    return False


def manifest_path(root: Path) -> Path:
    return meta_dir(root) / INJECT_DIR / MANIFEST_FILE


def read_manifest(root: Path) -> dict:
    path = manifest_path(root)
    if not path.is_file():
        return {"schema": 1, "surfaces": []}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except ValueError:
        return {"schema": 1, "surfaces": []}


def write_manifest(root: Path, payload: dict) -> None:
    _atomic_write(manifest_path(root), json.dumps(payload, ensure_ascii=False, indent=2, sort_keys=True) + "\n")


def surfaces_for(harnesses: set[str], allow_project_files: bool = False) -> list[str]:
    """Pick the block faces this workspace actually used, and that we may write.

    Qoder is deliberately absent: its path is the hook reading `.groma/injections/payload.md`,
    so no extra file appears in the project. `harnesses` comes from the digest rows
    (cwd inside this workspace), so we never create an `AGENTS.md` for a tool that
    never ran here.
    """
    chosen = []
    mapping = {
        "claude-code": ["claude-md"],
        "codex": ["codex-agents"],
    }
    for harness in sorted(harnesses):
        for name in mapping.get(harness, []):
            if SURFACES[name]["project_file"] and not allow_project_files:
                continue
            if name not in chosen:
                chosen.append(name)
    return chosen


def uses_hook(harnesses: set[str]) -> bool:
    """True when a Harness in this workspace is served by the Groma hook."""
    return bool(harnesses & HOOK_HARNESSES)


def scope_phrase(unit: dict) -> str:
    """装载面上那句主语——词与值同源于"这块背景是谁的背景"。

    界面上只有「工作区」与「协作主题」两个词，`Unit` 是工程口径不许出现在装载面；
    而"算不算协作主题"只许问一处（`unitmatch.is_theme`），否则会出现
    「主题页已经不再列它、装载面还替它报协作主题」——退成纯工作区正是这个形状。
    是主题就读人建标签的名；不是才读登记名，并显式说是"本机登记"。
    """
    from . import topics, unitmatch

    unit_id = str(unit.get("unit_id") or "")
    record = topics.topic_record(unit_id) if unit_id else None
    if record and unitmatch.is_theme(unit):
        return "协作主题：%s" % (record.get("name") or unit.get("name") or unit_id)
    return "本机登记：%s" % (unit.get("name") or unit_id)


def project_text(unit: dict, rows: list[dict], asset: dict, viewed_from: str) -> str:
    """The per-workspace view: the whole Unit's map, plus what this folder carries.

    Only folder basenames appear -- never absolute paths, never content.
    """
    here = next((row for row in rows if row["path"] == viewed_from), None)
    lines = [
        "# 项目全景（Groma 生成，请勿手改本段）",
        "",
        "%s ｜ %d 个工作区 ｜ 会话 %d · 事件 %d"
        % (
            scope_phrase(unit),
            len(rows),
            asset.get("totals", {}).get("sessions", 0),
            asset.get("totals", {}).get("events", 0),
        ),
        "",
        "## 本目录承担的部分：%s" % (here["name"] if here else Path(viewed_from).name),
        "",
        "| 工作区 | 角色 | 会话 | 事件 | 已声明采集根 |",
        "|---|---|---|---|---|",
    ]
    for row in rows:
        role = "家" if row.get("is_home") else "成员"
        sessions = row.get("sessions")
        lines.append(
            "| %s | %s | %s | %s | %s |"
            % (
                row.get("name"),
                role,
                "未采集" if sessions is None else sessions,
                "未采集" if row.get("events") is None else row.get("events"),
                "是" if row.get("sources") else "否",
            )
        )
    lines += [
        "",
        "## 这份背景从哪来",
        "",
        "- 资产：%s（生成于 %s）" % (asset.get("title", "—"), (asset.get("generated_at") or "")[:10]),
        "- 依据摘要：%s" % asset.get("digest", "—"),
        "- 本机指纹（`ws_ref`、sha256、写入时间）保存在 `.groma/injections/manifest.json`，不进本段、不进版本库。",
        "",
    ]
    notes = (asset.get("notes") or "").strip()
    if notes:
        lines += ["## 备注", "", notes, ""]
    claims = asset.get("claims") or []
    if claims:
        # 引文有预算而不是截断：hook 对超长载荷是整段跳过（超 60000 字符什么都不投），
        # 一段塞爆等于把全部背景掐死。先到先得，放不下的整条退出，只留「另见」。
        from . import anchors

        rendered, omitted = anchors.render_claims(claims, max_items=CLAIM_MAX_ITEMS,
                                                  max_chars=CLAIM_MAX_CHARS)
        if rendered:
            lines += ["## 结论与引文", ""] + rendered
            if omitted:
                lines += ["", "另见 CONTEXT.md：还有 %d 条结论未入载荷（预算内放不下或未核对）。" % omitted]
    return "\n".join(lines)

# -- 载荷投影：载荷 = f(受众)，现算不是存储 -------------------------------

# 块面/载荷通道 → harness：投影按 harness 查角色表，通道本来就按 harness 分。
HARNESS_BY_FACE = {
    "claude-md": "claude-code",
    "codex-agents": "codex",
    "hook": "qoder-cn",
    "qoder-rule": "qoder-cn",
}

# 角色视角句（角色表的语义；未配表 = 不插视角句，零配置可用）。
ROLE_VIEWS = {
    "任务书": "本载荷按「任务书」视角渲染：范围与判据为先。",
    "施工": "本载荷按「施工」视角渲染：以任务书为先。",
    "验收": "本载荷按「验收」视角渲染：以施工产出与偏差为先。",
}

# 净增益阈值：某成员会话占比 ≥50% 且 条数 ≥3 时折叠为一句；
# 折叠不删证据（README/四件套不受投影影响）。
FOLD_MIN_COUNT = 3
FOLD_MIN_SHARE = 0.5


def project_text_for(unit: dict, rows: list[dict], asset: dict, viewed_from: str,
                     harness: str | None = None, role: str | None = None,
                     viewer_session_ids: set[str] | None = None) -> str:
    """project_text 的受众版：角色视角句 + 净增益折叠；不传受众参数 = 原样。

    asset.json 一个字节都不改——投影是现算的；四件套与对外发布物不受影响。
    """
    lines = project_text(unit, rows, asset, viewed_from).split("\n")
    view = ROLE_VIEWS.get(str(role or ""))
    if view:
        # 插在标题之后，只动投影产物，不动任何存储
        lines.insert(1, "")
        lines.insert(2, view)
    sessions = asset.get("sessions") or []
    if viewer_session_ids and sessions:
        total = len(sessions)
        mine = sum(1 for item in sessions if item.get("session_id") in viewer_session_ids)
        if mine >= FOLD_MIN_COUNT and total and mine / total >= FOLD_MIN_SHARE:
            lines += ["", "%d 条里 %d 条来自你——已折叠。" % (total, mine)]
    return "\n".join(lines)
