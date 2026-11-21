"""Put the Unit's background where the Harnesses actually pick it up.

Two jobs, deliberately kept apart:

- `export_context`: write a draft asset out as `CONTEXT.md` under `.groma/exports`.
  Passive: nothing outside our own directory is touched.
- `inject_unit`: fan that same background out to the workspace's auto-loaded
  faces, as a marked block -- and only as a marked block (see `lab.surface`).

Whether a face really enters the session is a separate, measured question
(`lab.surface.SURFACES[*]["trust"]`); writing a file is never proof of injection.
"""

from __future__ import annotations

import hashlib
from pathlib import Path

from . import hooks, surface, unitmatch
from .asset import find_asset, load_asset
from .workspace import LabError, load_unit, meta_dir, read_json, utc_stamp

EXPORTS_DIR = "exports"


def _render(asset: dict, content_body: str | None = None) -> tuple[str, str]:
    if content_body is not None:
        body = content_body.rstrip() + "\n"
    else:
        body = _render_index(asset)
    digest = hashlib.sha256(body.encode("utf-8")).hexdigest()
    header = "<!-- groma:context asset=%s sha256=%s -->\n" % (asset["asset_id"], digest)
    return header + body, digest


def _render_index(asset: dict) -> str:
    count = asset.get("workspace_count", 1)
    provenance = "> 工作区 `%s` ｜ 依据 `%s`（sha256 `%s`）" % (
        asset["workspace_ref"],
        asset["digest"],
        asset["digest_sha256"][:16],
    )
    if count > 1:
        provenance += " ｜ 覆盖 %d 个工作区" % count
    lines = [
        "# 上下文资产：%s" % asset["title"],
        "",
        "> 由 Groma 从 %d 个会话生成。只包含结构与出处，**不包含对话正文**。" % asset["totals"]["sessions"],
        provenance,
        "",
        "## 这份资产能回答什么",
        "",
        "- 哪些 Agent 参与了这件事，各自做了多少",
        "- 覆盖的时间范围",
        "- 每条依据可以在哪个会话里核对",
        "",
        "## 依据",
        "",
        "| Agent | 类型 | 会话 | 事件 | 时间范围 |",
        "|---|---|---|---|---|",
    ]
    for item in asset["sessions"]:
        span = "%s → %s" % ((item.get("first_ts") or "—")[:10], (item.get("last_ts") or "—")[:10])
        lines.append(
            "| %s | %s | `%s` | %d | %s |"
            % (item["agent_name"], item["agent_kind"], item["session_id"], item["events"], span)
        )
    if asset.get("notes"):
        lines += ["", "## 备注", "", asset["notes"]]
    if asset.get("claims"):
        # claim-anchored 档：导出随带逐条带锚点的引文；stale 的不渲染（未核对不装有效）
        from . import anchors

        rendered, omitted = anchors.render_claims(asset["claims"])
        if rendered:
            lines += ["", "## 结论与引文", ""] + rendered
            if omitted:
                lines += ["", "另见：还有 %d 条未核对（stale）的结论，见 VALIDATION.md 的「锚点核对」。" % omitted]
    lines += [
        "",
        "## 使用边界",
        "",
        "- 这是 **draft**，未经人工复核；引用前请核对原始会话。",
        "- 不含正文、不含 cwd、不含绝对路径。",
        "- 需要细节时，按上表的会话 id 回到原始 Harness 记录。",
        "",
    ]
    return "\n".join(lines)


def export_context(root: Path, asset_id: str, out: Path | None = None) -> dict:
    asset = load_asset(root, asset_id)
    content_body = None
    folder = find_asset(root, asset_id)
    if asset.get("content_stored") and folder is not None:
        readme = folder / "README.md"
        if readme.is_file():
            content_body = readme.read_text(encoding="utf-8")
    text, digest = _render(asset, content_body)
    target = Path(out) if out else (meta_dir(root) / EXPORTS_DIR / ("%s-CONTEXT.md" % asset_id))
    if out is not None and target.suffix.lower() not in (".md", ".markdown"):
        raise LabError("output must be a .md file: %s" % target)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8", newline="\n")
    return {
        "asset_id": asset_id,
        "context": str(target),
        "sha256": digest,
        "bytes": len(text.encode("utf-8")),
    }


def harnesses_here(root: Path) -> set[str]:
    """Which Harnesses actually ran inside this workspace, from its own digest.

    This is what keeps the fan-out honest: we never create an `AGENTS.md` for a
    tool that never had a session here.
    """
    paths = unitmatch.digest_paths_for([Path(root).as_posix()])
    if not paths:
        return set()
    try:
        digest = read_json(paths[-1])
    except (OSError, ValueError):
        return set()
    return {item.get("harness") for item in digest.get("files", []) if item.get("harness")}


def inject_unit(root: Path, asset_id: str, faces: list[str] | None = None,
                allow_project_files: bool = False, unit_id: str | None = None) -> dict:
    """Write this Unit's background into the workspace's auto-loaded faces.

    `unit_id` 是"正在操作的那个协作主题"。不给就是旧的 CLI 语义——
    落点目录自己的卡片；给了，资产寻址与工作区表都按那个主题走。
    """
    root = Path(root)
    unit = unitmatch.resolve_unit(unit_id) if unit_id else load_unit(root)
    asset = load_asset(root, asset_id, unit_id)
    rows = unitmatch.unit_rows(unit["unit_id"])
    seen = harnesses_here(root)
    chosen = faces or surface.surfaces_for(seen, allow_project_files)
    unknown = [name for name in chosen if name not in surface.SURFACES]
    if unknown:
        raise LabError("未知的面：%s" % "、".join(unknown))
    blocked = [name for name in chosen if surface.SURFACES[name]["project_file"] and not allow_project_files]
    if blocked:
        raise LabError("%s 会被版本库跟踪，需要显式 --allow-project-file 才写" % "、".join(blocked))
    body = surface.project_text(unit, rows, asset, root.as_posix())
    # 载荷永远先写：它是 hook 的读取源，待在 .groma/ 里，项目目录因此少一个被跟踪文件
    payload = surface.write_payload(root, body)
    results = []
    for name in chosen:
        face = surface.SURFACES[name]
        record = surface.apply_block(root, face["path"], unit["unit_id"], asset_id, body)
        results.append({**record, "face": name, "trust": face["trust"], "note": face["note"]})

    book = surface.read_manifest(root)
    stamp = utc_stamp()
    known = {item.get("path"): item for item in book["surfaces"]}
    for item in results:
        known[item["path"]] = {
            "face": item["face"],
            "path": item["path"],
            "unit_id": unit["unit_id"],
            "asset_id": asset_id,
            "block_sha256": item["block_sha256"],
            "file_sha256": item.get("file_sha256_after"),
            "created_by_us": bool(item.get("created")),
            "backup": item.get("backup"),
            "written_at": stamp,
            "ws_ref": unit.get("workspace_ref") or _ref(root),
        }
    book["surfaces"] = [known[key] for key in sorted(known)]
    book["payload"] = {"sha256": payload.get("sha256"), "written_at": stamp}
    # 注入源是人点定的：自动刷新只更新投影，永不换源；换源必须再来一次人工确认
    book["source"] = {
        "asset_id": asset_id,
        "unit_id": unit["unit_id"],
        "title": asset.get("title"),
        "generated_at": asset.get("generated_at"),
        "set_at": stamp,
    }
    book["gate"] = gate_state(unit["unit_id"], asset)
    surface.write_manifest(root, book)
    return {
        "unit_id": unit["unit_id"],
        "asset_id": asset_id,
        "viewed_from": root.as_posix(),
        "payload": payload,
        "surfaces": results,
        "hook_serves": sorted(name for name in ("qoder-cn", "qoder") if name in seen),
        "hook_registered": bool(hooks.registered(root)),
        "hint": None if results or hooks.registered(root) else
        "载荷已写入，但没有装载面在用：Qoder 需要装 hook，Claude/Codex 的面需要 --allow-project-file",
        "registration": "首次需要确认（hook 注册不由这一步触碰）",
    }


def _ref(root: Path) -> str:
    from .state import workspace_ref

    return workspace_ref(root)


def gate_state(unit_id: str, asset: dict) -> dict:
    """Serving policy: 宁可不注入，也不把过期结论当成现况。

    两个信号，缺一不可地停：

    - `behind`：Unit 里有比这份资产更新的采集证据；
    - `narrow`：这份资产覆盖的会话数已经不到现况的一半。

    只 `behind` 不 `narrow` 说明视野还够，只是略旧 —— 继续投放但标注，
    否则一次普通采集就会把注入掐掉，那会让功能形同不可用。
    """
    from . import topics

    home = unitmatch.home_path(unit_id)
    live = 0
    newest = ""
    topic_id = topics.topic_of_asset(str(asset.get("asset_id") or ""))
    if topic_id:
        # A topic asset is a subset by design; measuring it against the whole
        # Unit means it can never be narrow -- and it would be paused forever.
        live = len(topics.active_evidence(topic_id))
    if home:
        union = unitmatch.unit_digest_union(home)
        if not topic_id:
            live = int(union.get("file_count") or 0)
        newest = union.get("created_at") or ""
    covered = int(asset.get("totals", {}).get("sessions") or 0)
    made = asset.get("generated_at") or ""
    behind = bool(newest and made and newest > made)
    narrow = bool(live) and covered * 2 < live
    # 分母口径只在这里判一次：文案由字段生成，拼句子时不再二次推断。
    evidence_scope = "topic" if topic_id else "unit"
    if behind and narrow:
        label = "本协作主题现况" if evidence_scope == "topic" else "本 Unit 现况"
        state, reason = "paused", "资产依据 %d 个会话，%s %d 个，且已有更新的采集证据" % (covered, label, live)
    elif behind:
        state, reason = "stale", "有比这份资产更新的证据（现况 %d 个会话）" % live
    else:
        state, reason = "serving", None
    return {"state": state, "reason": reason, "asset_sessions": covered, "live_sessions": live,
            "evidence_scope": evidence_scope}


def refresh(root: Path) -> dict:
    """Refresh (or pause) this workspace's payload from the human-chosen source.

    Called after collection. It never picks a different asset and never registers
    a hook: those are human decisions.
    """
    root = Path(root)
    book = surface.read_manifest(root)
    source = book.get("source") or {}
    if not source.get("asset_id"):
        return {"workspace": root.as_posix(), "state": "off", "changed": False}
    try:
        asset = load_asset(root, source["asset_id"], source.get("unit_id"))
    except LabError:
        book["gate"] = {"state": "paused", "reason": "注入源资产已不在本 Unit 可寻址范围内：%s" % source["asset_id"]}
        surface.remove_payload(root)
        surface.write_manifest(root, book)
        return {"workspace": root.as_posix(), "state": "paused", "changed": True}
    gate = gate_state(asset["unit_id"] if "unit_id" in asset else (book.get("source") or {}).get("unit_id"), asset)
    book["gate"] = gate
    if gate["state"] == "paused":
        existed = surface.remove_payload(root)
        surface.write_manifest(root, book)
        return {"workspace": root.as_posix(), "state": "paused", "reason": gate["reason"], "changed": existed}
    body = surface.project_text(unitmatch.resolve_unit(asset["unit_id"]), unitmatch.unit_rows(asset["unit_id"]),
                                asset, root.as_posix())
    payload = surface.write_payload(root, body)
    book["payload"] = {"sha256": payload.get("sha256"), "written_at": utc_stamp()}
    if payload.get("changed"):
        for item in book.get("surfaces", []):
            face = surface.SURFACES.get(item.get("face") or "")
            if not face:
                continue
            again = surface.apply_block(root, item["path"], asset["unit_id"], item["asset_id"], body)
            item["block_sha256"] = again["block_sha256"]
            item["file_sha256"] = again.get("file_sha256_after", item.get("file_sha256"))
            item["written_at"] = utc_stamp()
    surface.write_manifest(root, book)
    return {
        "workspace": root.as_posix(),
        "state": gate["state"],
        "reason": gate.get("reason"),
        "changed": bool(payload.get("changed")),
        "payload_sha256": payload.get("sha256"),
    }


def refresh_unit(unit_id: str) -> list[dict]:
    """Refresh every workspace of this Unit that has a chosen injection source."""
    out = []
    for path in unitmatch.workspaces_for_unit(unit_id):
        workspace = Path(path)
        if (meta_dir(workspace) / "injections" / "manifest.json").is_file():
            out.append(refresh(workspace))
    return out


def _delivery_reconciliation(root: Path, book: dict) -> dict:
    """Reconcile the three sha256 values that describe a delivery.

    Three parties, each recorded somewhere else: the manifest (what we stored
    last), the payload file on disk (what the hook would serve now) and the
    newest `action=inject` receipt (what a host actually took). The payload is
    rewritten by every refresh while a delivery only happens when a session
    rebuilds, so "served the new one" and "served the old one" coexist for most
    of the day -- without this reconciliation the receipt cannot tell them apart.

    `none` = nothing delivered yet; `match` = the host took exactly what is on
    disk; `stale` = what was served is not what is stored now -- including the
    payload being gone (paused) while a receipt survives, which is NOT the same
    as never having delivered.
    """
    disk_sha = None
    path = surface.payload_path(root)
    if path.is_file():
        disk_sha = hashlib.sha256(path.read_bytes()).hexdigest()
    manifest_sha = (book.get("payload") or {}).get("sha256")
    delivered_sha = (hooks.last_delivery(root) or {}).get("sha256")
    if disk_sha is None:
        state = "stale" if delivered_sha else "none"
    elif delivered_sha is None:
        state = "none"
    else:
        state = "match" if delivered_sha == disk_sha else "stale"
    return {
        "state": state,
        "disk_matches_manifest": disk_sha is not None and manifest_sha is not None and disk_sha == manifest_sha,
        "manifest_sha256": manifest_sha,
        "disk_sha256": disk_sha,
        "delivered_sha256": delivered_sha,
    }


def inject_status(root: Path) -> dict:
    """What the manifest claims vs what is on disk now -- drift is visible, not assumed."""
    root = Path(root)
    book = surface.read_manifest(root)
    rows = []
    for item in book["surfaces"]:
        now = surface.status(root, item["path"])
        drift = now.get("block_sha256") != item.get("block_sha256")
        rows.append({**item, "on_disk": now, "drifted": drift})
    return {
        "surfaces": rows,
        "changed": [row["path"] for row in rows if row["drifted"]],
        "source": book.get("source"),
        "gate": book.get("gate"),
        "payload_present": surface.payload_path(root).is_file(),
        "delivery": _delivery_reconciliation(root, book),
    }


def injection_summary(unit_id: str) -> dict:
    """Per workspace: is it being injected, with what, and when did it last land.

    Built for the Unit page. Reading it touches only small local files: manifest,
    payload presence, hook registration, and the tail of the receipt log.
    """
    out = {}
    for path in unitmatch.workspaces_for_unit(unit_id):
        workspace = Path(path)
        book = surface.read_manifest(workspace)
        source = book.get("source") or {}
        gate = book.get("gate") or {}
        state = "off" if not source.get("asset_id") else (gate.get("state") or "serving")
        faces = sorted({item.get("face") for item in book["surfaces"] if item.get("face")})
        registered = bool(hooks.status(workspace)["registered"])
        out[workspace.as_posix()] = {
            "state": state,
            # 光有载荷不算"在注入"：必须有一个装载面真的在用它，否则界面就是假绿
            "serving": bool(source.get("asset_id"))
            and state != "paused"
            and (bool(faces) or registered),
            "hook_registered": registered,
            "reason": gate.get("reason"),
            "asset_id": source.get("asset_id"),
            "title": source.get("title"),
            "payload_present": surface.payload_path(workspace).is_file(),
            "payload_sha256": (book.get("payload") or {}).get("sha256"),
            "faces": faces,
            "last_delivery": hooks.last_delivery(workspace),
            "delivery": _delivery_reconciliation(workspace, book),
        }
    return out


def uninject(root: Path) -> dict:
    """Take our blocks back out, drop the payload, and forget the chosen source."""
    root = Path(root)
    book = surface.read_manifest(root)
    results = [surface.remove_block(root, item["path"]) for item in book["surfaces"]]
    surface.remove_payload(root)
    surface.write_manifest(root, {"schema": 1, "surfaces": []})
    return {"removed": [item for item in results if item.get("changed")], "restart_hint": True}


def forget_workspace(root: Path, boot_root: Path | None = None) -> dict:
    """Remove every trace Groma left in a workspace, in the one order that stays consistent.

    Deliberately here and not inside `state.remove_workspace()`: `state` cannot
    import `inject` (circular), so the orchestration lives in the module that
    already owns both halves. Project files, harness databases and sessions are
    never touched -- only what Groma wrote.

    Both guards live here rather than in the route so `groma workspace remove`
    inherits them: ① the boot root cannot be removed (it is where
    this process lives) ② the last workspace cannot be removed.

    Order is fixed: take our blocks out first (they name a payload that is about
    to disappear), then the identity card (the thing `sync_units()` rebuilds a
    theme from), then the registry rows + `.groma/`.
    """
    from . import state, unitlink

    root = Path(root)
    ref = state.workspace_ref(root)
    if boot_root is not None and state.workspace_ref(boot_root) == ref:
        raise LabError("这是 Groma 的启动目录（这个进程自己的家），不能移除")
    if not [item for item in state.list_workspaces() if item["ref"] != ref]:
        raise LabError("这是本机最后一个工作区，先添加另一个再移除")
    size = 0
    meta = meta_dir(root)
    if meta.is_dir():
        for item in meta.rglob("*"):
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    taken_out = uninject(root)
    card_removed = unitlink.clear_link(root)
    gone = state.remove_workspace(root, delete_evidence=True)
    return {
        "path": root.as_posix(),
        "faces": [item.get("path") for item in taken_out["removed"]],
        "faces_removed": len(taken_out["removed"]),
        "card_removed": card_removed,
        "retired_units": gone.get("retired_units", []),
        "evidence_deleted": gone.get("evidence_deleted", False),
        "size_mb": round(size / 1048576, 1),
    }


def drop_unit(unit_id: str, boot_root: Path | None = None) -> dict:
    """Forget a theme and every pointer Groma left to it. Workspaces and files stay.

    Guards live here, not in the route, so `groma unit delete` inherits them (裁定 7):
    ② the last remaining theme cannot be dropped, and ① when the caller knows the
    Groma boot root, the theme that claims it cannot be dropped either -- its folder
    would mint a fresh identity on the next start.

    Order is fixed: take the payload out of every workspace, drop the cards (the
    other half of the revival path), clear the registry rows that point here, then
    the theme record itself.
    """
    from . import state, unitlink

    unit_id = (unit_id or "").strip()
    record = unitmatch.resolve_unit(unit_id)
    if not record.get("unit_id") or not any(item["unit_id"] == unit_id for item in unitmatch.unit_list()):
        raise LabError("没有这个主题：%s" % (unit_id or "（空）"))
    if boot_root is not None and record.get("home_binding") == state.workspace_ref(boot_root):
        raise LabError("它是 Groma 启动目录的主题，删了下次启动会以新身份回来")
    if len(unitmatch.unit_list()) <= 1:
        raise LabError("这是本机最后一个主题，至少留一个")
    # 一份注册表快照，paths 与 rows 都从它算：目录已经没了的登记行
    # 在 workspaces_for_unit 里是空表，但它恰恰是重启后把主题折回来的那一行。
    # 成员行的 unit_id 是它自己的卡给的，不指向本主题，所以按"路径集合"匹配，
    # 不按 unit_id 匹配——只按 unit_id 会把并入区漏掉（既有 A 类钉当场抓到）。
    snapshot = state.list_workspaces()
    home_and_members = {state.normalise_path(path)
                        for path in unitmatch.workspaces_for_unit(unit_id)}
    home_and_members |= {state.normalise_path(item["path"]) for item in snapshot
                         if item.get("unit_id") == unit_id and item.get("path")}
    rows_here = [item for item in snapshot
                 if item.get("path") and state.normalise_path(item["path"]) in home_and_members]
    paths = [Path(item["path"]) for item in rows_here if Path(item["path"]).is_dir()]
    taken, cards, rows = [], 0, 0
    for path in paths:
        out = uninject(path)
        taken.append({"path": path.as_posix(), "faces_removed": len(out["removed"])})
        if unitlink.clear_link(path):
            cards += 1
    for item in rows_here:
        if state.clear_workspace_unit(item["path"]):
            rows += 1
    removed = state.remove_unit(unit_id)
    return {
        "unit_id": unit_id,
        "name": record.get("name") or unit_id,
        "workspaces": taken,
        "cards_removed": cards,
        "rows_cleared": rows,
        "removed": removed,
    }
