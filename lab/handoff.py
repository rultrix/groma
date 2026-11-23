"""接力简报 · 同工作流。

触发 = digest 硬事实：同一工作区，上一场会话 harness=A、新一场 harness=B 且 A≠B
（零推断、零额度）。触发时**只铸简报、不静默投**：界面给「现在投」/「忽略这一次」，
简报只投接棒 harness 的装载面；一次性（同一时刻只记一笔），不进 gate 的 stale 判定。
命名避开 `relay.py`（交棒候选边）与 `trigger.py`（logon 启动项）。
"""

from __future__ import annotations

from pathlib import Path

from .workspace import LabError

# 接力窗口 N=5：同工作区最近 5 场会话、跨 harness 都算；模块常量可调。
WINDOW = 5


def _session_rows(workspace: Path) -> list[dict]:
    """本工作区最新一份 digest 里的会话行，按末次活跃升序（旧→新）。"""
    from .unitmatch import digest_records_for

    records = digest_records_for([workspace.as_posix()])
    files = [item for record in records for item in record.get("files") or []]
    return sorted(files, key=lambda item: item.get("last_ts") or "")


def detect_handoff(workspace: Path) -> dict | None:
    """检出接力时刻：最近两场会话 harness 不同 ⇒ 一棒；相同或不足两场 ⇒ 无。

    一次性：同一工作区同一「新会话时刻」在全量账本里已有**任意 status** 的记录
    （待投/已投/已忽略/回放 observed）就不再检出——不依赖
    `latest_handoff`（它只回最近一条；中间被别的行隔着就漏）。
    """
    from . import topics

    rows = _session_rows(workspace)
    if len(rows) < 2:
        return None
    prev, newest = rows[-2], rows[-1]
    from_harness = str(prev.get("source") or "")
    to_harness = str(newest.get("source") or "")
    if not from_harness or not to_harness or from_harness == to_harness:
        return None
    at = str(newest.get("last_ts") or "")
    for item in topics.handoff_history(workspace.as_posix(), limit=10 ** 9):
        if item.get("at") == at:
            return None
    return {"workspace": workspace.as_posix(), "from_harness": from_harness,
            "to_harness": to_harness, "at": at}


def build_brief(workspace: Path, topic_id: str | None, baton: dict | None = None) -> dict:
    """铸简报（N=5 窗口，四段）：进行到哪 / 最近决策 claims / 开放问题 / 交接材料。

    配了角色表时带棒位：简报按下一棒投对应视角（任务书视角给施工方 /
    产出偏差视角给验收方——投影的简报侧）。
    """
    from . import surface as surface_mod
    from . import topics

    rows = _session_rows(workspace)[-WINDOW:]
    progress = [{"harness": str(item.get("source") or ""), "path": str(item.get("path") or ""),
                 "events": item.get("events"), "last_ts": item.get("last_ts")}
                for item in rows]
    claims, open_items = [], []
    serving = None
    book = surface_mod.read_manifest(workspace)
    asset_id = (book.get("source") or {}).get("asset_id")
    if asset_id:
        # 单槽读件：读当前槽 asset.json（指纹不符=偏旧语义，claims 照读不造假）。
        from .asset import current_asset_dir
        from .workspace import read_json as _read_json

        slot = current_asset_dir(Path(workspace))
        if (slot / "asset.json").is_file():
            try:
                serving = _read_json(slot / "asset.json")
            except ValueError:
                serving = None
    for claim in (serving or {}).get("claims") or []:
        if claim.get("state") == "verified":
            claims.append({"title": claim.get("title"), "quote": claim.get("quote")})
        else:
            open_items.append({"title": claim.get("title"), "state": claim.get("state")})
    materials = []
    if serving:
        materials.append({"asset_id": serving.get("asset_id"), "title": serving.get("title"),
                          "generated_at": serving.get("generated_at")})
        # 交接材料增列服役资产 claims 里的 material.locator（有则列，无则不显示）。
        for claim in serving.get("claims") or []:
            locator = (claim.get("material") or {}).get("locator")
            if locator:
                materials.append({"kind": "material_locator", "locator": locator})
    last_reforge = topics.last_reforge(topic_id) if topic_id else None
    if last_reforge:
        materials.append({"asset_id": last_reforge.get("asset_id"), "kind": "last_reforge",
                          "at": last_reforge.get("at")})
    return {"topic_id": topic_id, "window": WINDOW, "progress": progress,
            "claims": claims, "open": open_items, "materials": materials,
            "baton": baton}


def render_brief(brief: dict) -> str:
    """简报四段渲染成纯文本（不进 gate、不进资产四件套）。"""
    lines = ["## 接力简报（同工作流，N=%d）" % brief.get("window", WINDOW), ""]
    baton = brief.get("baton") or {}
    if baton.get("enabled") and baton.get("mapped") and baton.get("next_station"):
        views = {"施工": "任务书视角：范围与判据为先。", "验收": "产出/偏差视角：施工产出与偏差为先。",
                 "任务书": "范围判据视角。"}
        lines.append("下一棒：%s（%s）" % (baton["next_station"],
                                        views.get(baton["next_station"], "")))
        lines.append("")
    lines.append("### 进行到哪")
    for item in brief.get("progress") or []:
        lines.append("- %s · %s · %s 事件 · %s" % (
            item.get("harness") or "？", item.get("path") or "？",
            item.get("events") if item.get("events") is not None else "？",
            item.get("last_ts") or "？"))
    if not brief.get("progress"):
        lines.append("- （窗口里没有会话）")
    lines.append("")
    lines.append("### 最近决策（claims）")
    for item in brief.get("claims") or []:
        lines.append("- %s：%s" % (item.get("title") or "（无标题）", item.get("quote") or ""))
    if not brief.get("claims"):
        lines.append("- （无）")
    lines.append("")
    lines.append("### 开放问题")
    for item in brief.get("open") or []:
        lines.append("- %s（%s）" % (item.get("title") or "（无标题）", item.get("state") or "？"))
    if not brief.get("open"):
        lines.append("- （无）")
    lines.append("")
    lines.append("### 交接材料")
    for item in brief.get("materials") or []:
        if item.get("kind") == "material_locator":
            lines.append("- 材料：%s" % (item.get("locator") or "？"))
            continue
        lines.append("- %s（%s）" % (item.get("title") or item.get("asset_id") or "？",
                                     item.get("asset_id") or item.get("at") or ""))
    if not brief.get("materials"):
        lines.append("- （无）")
    return "\n".join(lines) + "\n"


def deliver_handoff(workspace: Path, record: dict, brief_text: str,
                    decided_by: str = "human") -> dict:
    """把简报投给**接棒 harness 的装载面**：hook 载荷追加一段，块面按 harness 映射投块。

    简报不进 gate、不进资产四件套；块标记形状不变（unit 不变、asset 标记沿用当前源）。
    """
    from . import surface as surface_mod, topics

    to_harness = str(record.get("to_harness") or "")
    harnesses = {to_harness}
    faces = surface_mod.surfaces_for(harnesses, allow_project_files=True)
    unit_id = (surface_mod.read_manifest(workspace).get("source") or {}).get("unit_id") or ""
    asset_id = (surface_mod.read_manifest(workspace).get("source") or {}).get("asset_id") or "handoff"
    written = []
    body = surface_mod.block_of(unit_id, asset_id, brief_text)
    for face in faces:
        spec = surface_mod.SURFACES[face]
        out = surface_mod.apply_block(workspace, spec["path"], unit_id, asset_id, body)
        written.append({"face": face, "path": out["path"]})
    if surface_mod.uses_hook(harnesses):
        current = ""
        payload = surface_mod.payload_path(workspace)
        if payload.is_file():
            current = payload.read_text(encoding="utf-8")
        surface_mod.write_payload(workspace, current.rstrip("\n") + "\n\n" + brief_text)
        written.append({"face": "hook", "path": surface_mod.payload_path(workspace).as_posix()})
    return topics.mark_handoff_delivered(record, faces=written, decided_by=decided_by)


def ignore_handoff(record: dict, decided_by: str = "human") -> dict:
    """「忽略这一次」：这笔不投，也不再检出（一次性）。"""
    from . import topics

    return topics.mark_handoff_ignored(record, decided_by=decided_by)


def replay_handoffs(workspace: Path) -> dict:
    """历史换棒回放：读全量 digest 行，逐对相邻 A→B（A≠B）入账。

    回放只入账不投递——一律 `status="observed"`、`decided_by="replay"`，
    绝不调用 `deliver_handoff` / `ignore_handoff`（「现在投 / 忽略这一次」只属于
    真实检出的 pending 时刻）。
    去重按全量账本查键 `(workspace, at)`（at=新会话 last_ts），任意 status
    已存在即跳过——不得依赖 `latest_handoff`（它只回最近一条，会漏）。
    """
    from . import topics

    workspace = Path(workspace)
    rows = _session_rows(workspace)
    # 全量账本取去重集（handoff_history 默认 limit=20 只回最近一段，会漏）。
    seen_at = {str(item.get("at") or "")
               for item in topics.handoff_history(workspace.as_posix(), limit=10 ** 9)}
    created = []
    for prev, newest in zip(rows, rows[1:]):
        from_harness = str(prev.get("source") or "")
        to_harness = str(newest.get("source") or "")
        if not from_harness or not to_harness or from_harness == to_harness:
            continue
        at = str(newest.get("last_ts") or "")
        if at in seen_at:
            continue
        seen_at.add(at)
        created.append(topics.record_handoff({
            "workspace": workspace.as_posix(),
            "from_harness": from_harness,
            "to_harness": to_harness,
            "at": at,
            "status": "observed",
            "decided_by": "replay",
        }))
    return {"workspace": workspace.as_posix(), "created": len(created)}


def replay_unit_handoffs(boot_root: Path) -> dict:
    """启动段调用（只在这里跑，不在任何 GET 页面路径里）：本主题每个成员区回放一遍。"""
    from . import unitmatch

    unit_id = unitmatch.folder_unit(Path(boot_root)) or ""
    created = 0
    workspaces = []
    if unit_id:
        for path in unitmatch.workspaces_for_unit(unit_id):
            folder = Path(path)
            if not folder.is_dir():
                continue
            out = replay_handoffs(folder)
            created += out["created"]
            workspaces.append(out["workspace"])
    return {"created": created, "workspaces": workspaces}
