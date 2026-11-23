"""Context-asset drafts: structure and provenance only, never content.

Layout follows the RSCL four-piece contract so that publishing later is a copy
plus a PR, not a rewrite. See docs/ASSET-SCHEMA.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import anchors, reconcile, unitmatch
from .state import workspace_ref
from .workspace import LabError, load_unit, meta_dir, read_json, utc_stamp, write_json

ASSETS_DIR = "assets"
# 单槽——一个协作主题恒只保留一份四件套，固定槽 assets/current/，
# 旧 asset-<id> 多代目录机制退役（历史代由 migrate-singleton 收编，K4）。
CURRENT_SLOT = "current"


def _sha256(path: Path) -> str:
    # Not `digest.file_sha256`: that one returns "" for an unreadable file, and a
    # manifest hash must fail loudly rather than hash an absence.
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def session_id_of(item: dict) -> str:
    """The session's own id, from the same derivation the digest and vault use.

    Prefers the `session_key` the digest already measured; `Path(path).stem` would
    hand codex sessions their whole file name (`rollout-2026-08-10T17-34-36-019f…`),
    which is a capture timestamp wearing an identity.
    """
    return reconcile.identity_of(item)[1]


def _asset_id(ws_ref: str, keys: list[tuple[str, str]], title: str) -> str:
    payload = json.dumps([ws_ref, keys, title], ensure_ascii=False, sort_keys=True)
    return "asset-" + hashlib.sha256(payload.encode("utf-8")).hexdigest()[:12]


def _render_readme(asset: dict) -> str:
    lines = [
        "# %s" % asset["title"],
        "",
        "> 由 Groma 生成的**上下文资产草稿**（status: draft）。只包含结构与出处，不包含对话正文。",
        "",
        "- 工作区：`%s`（Unit `%s` ｜ %s）"
        % (asset["workspace_name"], asset.get("unit_id") or "—", asset["workspace_ref"]),
        "- 组织路径：`%s`" % (asset.get("org_path") or "未登记（运行 `groma unit link --org host/owner/repo`）"),
        "- 生成时间：%s" % asset["generated_at"],
        "- 依据：`%s`（sha256 `%s`）" % (asset["digest"], asset["digest_sha256"][:16]),
        "- 覆盖工作区：%d 个（并入关系只在本机，路径不写入资产）" % asset["workspace_count"],
        "- 会话 %d 个 · 事件 %d 条" % (asset["totals"]["sessions"], asset["totals"]["events"]),
        "",
        "## 依据的会话",
        "",
        "| Agent | 类型 | 会话 | 事件 | 时间范围 |",
        "|---|---|---|---|---|",
    ]
    for item in asset["sessions"]:
        span = "%s → %s" % ((item.get("first_ts") or "—")[:10], (item.get("last_ts") or "—")[:10])
        lines.append(
            "| %s | %s | `%s` | %d | %s |"
            % (item["agent_name"], item["agent_kind"], item["session_id"][:8], item["events"], span)
        )
    if asset.get("notes"):
        lines += ["", "## 备注", "", asset["notes"]]
    if asset.get("claims"):
        lines += [
            "",
            "## 引用的结论",
            "",
            "正文只随锚点逐条存放（见 asset.json 的 `claims[].quote` 与锚点哈希）；README 只列结论与出处：",
            "",
        ]
        for claim in asset["claims"]:
            state = "已核对" if claim.get("state") == "verified" else "未核对（stale）"
            if claim.get("material") and not claim.get("quote"):
                # 材料活引用：README 只列指针出处，不搬正文
                ref = ((claim.get("material") or {}).get("locator") or {}).get("ref") or "？"
                lines.append("- %s ｜ %s ｜ 材料 `%s`" % (claim["title"], state, ref))
                continue
            lines.append("- %s ｜ %s ｜ %s → %s ｜ sha256 `%s`"
                         % (claim["title"], state, claim["from_ts"], claim["to_ts"],
                            claim["quote_sha256"][:12]))
    lines += [
        "",
        "## 边界",
        "",
        "- 没有正文、没有 cwd、没有绝对路径；路径相对来源根。",
        "- 这是草稿，不是发布物；发布到共享仓前需要人工复核。",
        "",
    ]
    return "\n".join(lines)


def _render_validation(asset: dict) -> str:
    lines = [
        "# 验证状态",
        "",
        "- 生成：draft（由 Groma 自动生成）",
        "- 人工复核：未进行",
        "- 内容留存：无（只保存结构与出处）" if not asset.get("claims")
        else "- 内容留存：仅 claims[].quote（逐条带锚点）",
        "",
        "## 已具备",
        "",
        "- 来源会话可在本地 digest 中定位（见 asset.json 的 digest 与 sha256）",
        "- 每个文件有 sha256（见 manifest.json）",
    ]
    if asset.get("claims"):
        lines += ["", "## 锚点核对", ""]
        for claim in asset["claims"]:
            if claim.get("material") and not claim.get("quote"):
                # 材料活引用：核对口径是文件指纹（诚实账），不是会话锚点
                ref = ((claim.get("material") or {}).get("locator") or {}).get("ref") or "？"
                lines.append("- ✅ 材料 `%s`（快照指纹 `%s…`）"
                             % (ref, (claim["material"].get("snapshot_sha256") or "")[:12]))
                continue
            if claim.get("state") == "verified":
                lines.append("- ✅ `%s` %s → %s（sha256 `%s`）"
                             % (claim["session_key"], claim["from_ts"], claim["to_ts"],
                                claim["quote_sha256"][:12]))
            else:
                lines.append("- ⚠️ `%s` **未核对（stale）**：%s"
                             % (claim["session_key"], claim.get("stale_reason") or "源已漂移"))
    lines += [
        "",
        "## 未验证",
        "",
        "- 结论是否正确：未评估",
        "- 是否被 Agent 使用：未证明",
        "- 是否产生效果：未评估",
        "",
    ]
    return "\n".join(lines)


def _inherit_claims(prev: dict | None, root: Path) -> tuple[list[dict], dict[str, int]]:
    """claims 跨代继承——实质重铸覆写单槽前，把上一份资产的 claims
    逐条核对带入：冻结引文对 vault 会话分片 sha、材料活引用对文件指纹（现成核对件）。
    可核对的写入新四件套；失效的按原因分桶计数（诚实账，不静默丢）。
    无上一份资产（首次重铸）= 空继承。
    """
    from . import anchors, vault as vault_mod

    skipped: dict[str, int] = {}
    carried: list[dict] = []
    for claim in (prev or {}).get("claims") or []:
        material = claim.get("material")
        if material and not claim.get("quote"):
            ref = ((material.get("locator") or {}).get("ref") or "")
            expected = material.get("snapshot_sha256")
            try:
                snapshot = anchors.material_snapshot(Path(root), ref)
            except LabError:
                skipped["material_missing"] = skipped.get("material_missing", 0) + 1
                continue
            if snapshot != expected:
                skipped["material_drift"] = skipped.get("material_drift", 0) + 1
                continue
            carried.append(claim)
            continue
        try:
            found = anchors.locate(vault_mod.vault_root(), claim.get("session_key") or "",
                                   claim.get("from_ts") or "", claim.get("to_ts") or "")
        except LabError:
            skipped["quote_missing"] = skipped.get("quote_missing", 0) + 1
            continue
        if found.get("quote_sha256") != claim.get("quote_sha256"):
            skipped["quote_stale"] = skipped.get("quote_stale", 0) + 1
            continue
        carried.append(dict(claim, state="verified", stale_reason=None))
    return carried, skipped


def build_asset(root: Path, title: str, selection: list[tuple[str, str]], note: str = "",
                dry_run: bool = False) -> dict:
    """Cite sessions from anywhere in the Unit, but write the asset locally."""
    unit = load_unit(root)
    label = (title or "").strip()
    if not label:
        raise LabError("asset title must not be empty")
    if not selection:
        raise LabError("select at least one session")

    roots = unitmatch.unit_workspaces(root)
    paths = unitmatch.digest_paths_for(roots)
    if not paths:
        raise LabError("no digest yet; run `groma catchup` first")
    digest_sha = {path.name: _sha256(path) for path in paths}
    # 身份是会话（家族＋id），不是采集源的显示名：源改名后账本里的旧键仍要引得到，
    # 而引用里写的是 digest 现持的标签。派生只住 reconcile 一处。
    index = reconcile.citation_index_for([Path(item) for item in roots])

    keys = sorted(set(selection))
    chosen = []
    for key in keys:
        item = reconcile.resolve_citation(index, key)
        if item is None:
            raise LabError("session not in this Unit's digests: %s:%s" % key)
        chosen.append(item)

    ws_ref = unit.get("workspace_ref") or workspace_ref(unit.get("workspace_root") or root)
    asset_id = _asset_id(ws_ref, keys, label)
    used_digests = sorted({item["digest"] for item in chosen})
    fingerprint = hashlib.sha256(
        "".join(digest_sha[name] for name in used_digests).encode("utf-8")
    ).hexdigest()

    sessions = [
        {
            "source": item["source"],
            "path": item["path"],
            "session_id": session_id_of(item),
            "agent_id": item.get("agent_id"),
            "agent_name": item.get("agent_name"),
            "agent_kind": item.get("agent_kind"),
            "events": item.get("events", 0),
            "transcript": item.get("transcript", True),
            "first_ts": item.get("first_ts"),
            "last_ts": item.get("last_ts"),
            "digest": item["digest"],
        }
        for item in chosen
    ]
    by_agent: dict[str, dict] = {}
    for item in sessions:
        slot = by_agent.setdefault(
            item["agent_id"] or "unknown",
            {
                "agent_id": item["agent_id"],
                "name": item["agent_name"],
                "kind": item["agent_kind"],
                "sessions": 0,
                "events": 0,
            },
        )
        slot["sessions"] += 1
        slot["events"] += item["events"]

    asset = {
        "schema": 1,
        "kind": "GROMA_LOCAL_CONTEXT_ASSET",
        "asset_id": asset_id,
        "title": label,
        "status": "draft",
        "generated_at": utc_stamp(),
        "workspace_ref": ws_ref,
        "unit_id": unit.get("unit_id"),
        "org_path": unit.get("org_path"),
        "workspace_name": unit.get("name"),
        "digest": " + ".join(used_digests),
        "digest_sha256": fingerprint,
        "digests": [{"name": name, "sha256": digest_sha[name]} for name in used_digests],
        "workspace_count": len(roots),
        "agents": sorted(by_agent.values(), key=lambda item: (item["kind"] or "", item["name"] or "")),
        "sessions": sessions,
        "totals": {"sessions": len(sessions), "events": sum(item["events"] for item in sessions)},
        "content_stored": False,
        "profile": "index-only",
        "notes": note or "",
    }

    # 写固定槽；写前比对指纹——新算指纹与当前槽 asset.json.asset_id 相等
    # → 「没变不重铸」，不写盘、不追加账本（调用方按 reason 提示）。
    # CLI --check：算指纹+依据摘要后即返回，不写盘、不进账本。
    if dry_run:
        return {"planned": True, "asset_id": asset_id, "title": label,
                "sessions": len(sessions), "digest": asset["digest"],
                "selection": [[source, path] for source, path in keys]}
    target = current_asset_dir(root, unit.get("unit_id"))
    existing = target / "asset.json"
    prev = None
    if existing.is_file():
        try:
            prev = read_json(existing)
        except ValueError:
            prev = None
        if prev and prev.get("asset_id") == asset_id:
            out = dict(prev)
            out["unchanged"] = True
            out["reason"] = "没变不重铸（指纹同）"
            return out
    # 覆写前核对继承上一份资产的 claims（首次重铸=空继承）。
    carried, skipped_claims = _inherit_claims(prev, root)
    if carried:
        asset["claims"] = carried
        asset["profile"] = "claim-anchored"
    target.mkdir(parents=True, exist_ok=True)
    _write_folder(target, asset)
    asset["inherited_claims"] = len(carried)
    asset["skipped_claims"] = skipped_claims
    return asset


def _write_folder(folder: Path, asset: dict) -> None:
    """Write the four-piece folder from an asset dict. The manifest lists the
    other three files with their sha256, so it is always written last."""
    write_json(folder / "asset.json", asset)
    (folder / "README.md").write_text(_render_readme(asset), encoding="utf-8", newline="\n")
    (folder / "VALIDATION.md").write_text(_render_validation(asset), encoding="utf-8", newline="\n")
    files = []
    for name in ("asset.json", "README.md", "VALIDATION.md"):
        path = folder / name
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    write_json(folder / "manifest.json", {"schema": 1, "asset_id": asset["asset_id"], "files": files})


def current_asset_dir(root: Path, unit_id: str | None = None) -> Path:
    """单槽读取件：恒 <家>/.groma/assets/current/。

    `unit_id` 给了就按协作主题的家定位；不给才按 `root` 自己的 Unit 家——
    与 find_asset 的旧口径同一句话：成员区自带卡时这两种口径不是同一个集合。
    """
    home = None
    if unit_id:
        home = unitmatch.home_path(unit_id)
    if home is None:
        try:
            unit = load_unit(root)
            home = unitmatch.home_path(unit.get("unit_id"))
        except (LabError, OSError, ValueError):
            home = None
    base = meta_dir(Path(home)) if home else meta_dir(Path(root))
    return base / ASSETS_DIR / CURRENT_SLOT


def find_asset(root: Path, asset_id: str, unit_id: str | None = None) -> Path | None:
    """The folder of an asset inside this Unit, whichever workspace built it.

    单槽语义：`asset_id` 已从目录身份降为内容指纹——当前槽里的 asset.json
    指纹等于要找的 id 才算找到；找不到不再翻 asset-<id> 多代目录（已退役）。
    """
    slot = current_asset_dir(root, unit_id)
    path = slot / "asset.json"
    if not path.is_file():
        return None
    try:
        asset = read_json(path)
    except ValueError:
        return None
    return slot if asset.get("asset_id") == asset_id else None


def load_asset(root: Path, asset_id: str, unit_id: str | None = None) -> dict:
    folder = find_asset(root, asset_id, unit_id)
    if folder is None:
        raise LabError("asset not found: %s" % asset_id)
    return read_json(folder / "asset.json")


def list_assets(root: Path, unit_id: str | None = None) -> list[dict]:
    """Every asset of this Unit. 单例化后恒至多一份（当前槽）。"""
    slot = current_asset_dir(root, unit_id)
    path = slot / "asset.json"
    if not path.is_file():
        return []
    try:
        asset = read_json(path)
    except ValueError:
        return []
    return [asset]


def attach_claims(root: Path, asset_id: str, claims: list[dict], unit_id: str | None = None,
                  vault_root: Path | None = None) -> dict:
    """Upgrade an asset to `claim-anchored` with human-supplied claims.

    Each claim is `{title, session_key, from_ts, to_ts}`; this locates the span
    in the vault, hashes the quote, and writes the claim into `asset.json`.
    Verification happens for ALL claims before ANY byte is written: a batch with
    one bad window is refused whole (a half-verified asset is worse than a
    refused one), and the vault itself is never written to.

    Re-attaching replaces the claim with the same (session_key, from_ts, to_ts)
    and keeps the asset_id -- claims never participate in the id derivation, so
    adding a citation to a published asset is an upgrade, not a new asset.
    落点=当前槽（find_asset 按指纹寻址）。
    """
    from . import vault as vault_mod

    folder = find_asset(root, asset_id, unit_id)
    if folder is None:
        raise LabError("asset not found: %s" % asset_id)
    asset = read_json(folder / "asset.json")
    if not claims:
        raise LabError("没有给出任何 claim")
    vr = Path(vault_root) if vault_root else vault_mod.vault_root()
    located: list[dict] = []
    reasons: list[str] = []
    for claim in claims:
        has_quote_side = bool((claim.get("session_key") or "").strip()
                              and (claim.get("from_ts") or "").strip()
                              and (claim.get("to_ts") or "").strip())
        material = claim.get("material")
        try:
            item = None
            if has_quote_side:
                found = anchors.locate(vr, claim.get("session_key") or "",
                                       claim.get("from_ts") or "", claim.get("to_ts") or "")
                item = anchors.make_claim(found, claim.get("title") or "")
            elif not material:
                raise LabError("这条 claim 既没有引文区间也没有材料活引用，视为无效：%s"
                               % (claim.get("title") or "（无标题）"))
            if material:
                ref = anchors.validate_material_locator(material.get("locator"))
                snapshot = (material.get("snapshot_sha256") or "").strip()                     or anchors.material_snapshot(root, ref)
                entry = {"locator": {"scheme": "ws-rel", "ref": ref},
                         "role": str(material.get("role") or "").strip() or None,
                         "snapshot_sha256": snapshot}
                if item is None:
                    item = {"title": claim.get("title") or "", "material": entry,
                            "state": "verified", "stale_reason": None}
                else:
                    item["material"] = entry
            if item is not None:
                located.append(item)
        except LabError as exc:
            reasons.append(str(exc))
    if reasons:
        raise LabError("有 %d 条 claim 无法落锚，资产未改：\n%s" % (len(reasons), "\n".join("- " + r for r in reasons)))

    existing = asset.get("claims") or []
    key_of = lambda item: (item.get("session_key"), item.get("from_ts"), item.get("to_ts"))  # noqa: E731 # 材料活引用没有引文三键
    merged = [item for item in existing if key_of(item) not in {key_of(n) for n in located}]
    merged.extend(located)
    asset["claims"] = merged
    asset["profile"] = "claim-anchored"
    _write_folder(folder, asset)
    return {"asset_id": asset_id, "attached": len(located), "claims": len(merged)}


def verify_claims(root: Path, asset_id: str, unit_id: str | None = None,
                  vault_root: Path | None = None) -> dict:
    """Re-derive every claim against the vault as it exists now.

    A claim whose span no longer re-derives to the same quote -- source moved,
    shard pruned or drifted -- is degraded to `stale` (recorded in
    VALIDATION.md, never rendered again), not silently kept and not deleted.
    """
    from . import vault as vault_mod

    folder = find_asset(root, asset_id, unit_id)
    if folder is None:
        raise LabError("asset not found: %s" % asset_id)
    asset = read_json(folder / "asset.json")
    claims = asset.get("claims") or []
    if not claims:
        raise LabError("这份资产还没有引文可核对（先 attach_claims）")
    vr = Path(vault_root) if vault_root else vault_mod.vault_root()
    stale = []
    drift = []
    for claim in claims:
        if claim.get("material"):
            # 活引用核对：snapshot 对不上是**诚实账**（记 VALIDATION.md 口径，不阻断、不降级
            # ——文件被改反而该读新的，指针不搬运）。这一支不进引文 stale 判定。
            ref = ((claim.get("material") or {}).get("locator") or {}).get("ref") or ""
            expected = (claim.get("material") or {}).get("snapshot_sha256")
            path = root / ref
            if not expected or not path.is_file()                     or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                drift.append(claim.get("title") or ref)
            continue
        try:
            found = anchors.locate(vr, claim["session_key"], claim["from_ts"], claim["to_ts"])
            if found["quote_sha256"] == claim["quote_sha256"]:
                claim["state"] = "verified"
                claim["stale_reason"] = None
            else:
                claim["state"] = "stale"
                claim["stale_reason"] = "区间正文与锚点哈希不符（源已变化）"
                stale.append(claim["title"])
        except LabError as exc:
            claim["state"] = "stale"
            claim["stale_reason"] = str(exc)
            stale.append(claim["title"])
    asset["claims"] = claims
    _write_folder(folder, asset)
    return {"asset_id": asset_id, "verified": len(claims) - len(stale),
            "stale": stale, "material_drift": drift}


def _pick_base_generation(slot_dir: Path, generations: list[Path], payloads: dict[Path, dict],
                          preferred_asset_id: str | None) -> tuple[dict | None, str | None, str | None]:
    """基代优先级 当前槽 > preferred（账本最新笔）> generated_at 最新 > None（报错）。

    目录名是 asset_id 哈希、字母序与时间无关——绝不能拿 `generations[-1]` 当"最新"。
    """
    if (slot_dir / "asset.json").is_file():
        try:
            return read_json(slot_dir / "asset.json"), slot_dir.name, "current-slot"
        except ValueError:
            pass
    if preferred_asset_id:
        for folder in generations:
            data = payloads.get(folder)
            if data and (data.get("asset_id") == preferred_asset_id
                         or folder.name == preferred_asset_id):
                return data, folder.name, "reforge-ledger"
    dated = [(str((payloads.get(folder) or {}).get("generated_at") or ""), folder)
             for folder in generations if payloads.get(folder)]
    dated = [(stamp, folder) for stamp, folder in dated if stamp]
    if dated:
        dated.sort(key=lambda item: item[0])
        stamp, folder = dated[-1]
        return payloads[folder], folder.name, "newest-generated-at"
    return None, None, None


def migrate_singleton(root: Path, unit_id: str, apply: bool = False,
                      preferred_asset_id: str | None = None) -> dict:
    """历史代 asset-* 收编进单槽。

    各历史代 asset.json 的 claims 按 (session_key, sha256, material.ref) 三元组去重、
    逐条核对（现成核对件），可核对的合并进单槽 → 写单槽 → 删除全部 asset-* 目录 →
    打印报告（合并/失效/删除计数）。不带 --yes（apply=False）只打印将执行的动作清单。
    迁移后 reforges 账本不动（审计在账）。
    """
    import shutil

    home = unitmatch.home_path(unit_id)
    if not home:
        raise LabError("协作主题没有家目录：%s" % unit_id)
    base = meta_dir(Path(home)) / ASSETS_DIR
    generations = sorted(folder for folder in (base.glob("asset-*") if base.is_dir() else [])
                         if (folder / "asset.json").is_file())
    slot_dir = current_asset_dir(Path(home), unit_id)
    plan = {"unit_id": unit_id, "home": Path(home).as_posix(),
            "generations": [folder.name for folder in generations],
            "slot": slot_dir.name,
            "actions": []}

    # 1) 收集并去重 claims（三元组；只数不核对的部分留给报告）。
    seen: set[tuple] = set()
    candidates: list[dict] = []
    payloads: dict[Path, dict] = {}
    for folder in generations:
        try:
            data = read_json(folder / "asset.json")
        except ValueError:
            plan["actions"].append("跳过不可读代：%s" % folder.name)
            continue
        payloads[folder] = data
        for claim in data.get("claims") or []:
            material = claim.get("material") or {}
            # 三元组第三列取「内容 sha」：引文取 quote_sha256，材料取快照指纹——
            # 同一材料被改过前后的两条不是同一条 claim，不能合并。
            key = (claim.get("session_key") or "",
                   claim.get("quote_sha256") or material.get("snapshot_sha256") or "",
                   (material.get("locator") or {}).get("ref") or "")
            if key in seen:
                continue
            seen.add(key)
            candidates.append(claim)
    plan["candidate_claims"] = len(candidates)
    plan["delete_dirs"] = len(generations)
    plan["actions"].append("合并 %d 条去重 claims 进单槽" % len(candidates))
    plan["actions"].append("删除 %d 个历史代目录（%s）"
                           % (len(generations), "、".join(plan["generations"])
                              if plan["generations"] else "无"))
    if not generations:
        plan["applied"] = False
        plan["note"] = "没有历史代目录——单槽已是唯一形态，无需迁移"
        return plan
    # 基代按「当前槽 > 账本最新笔 > generated_at 最新」选，报告里写明
    # 选的是哪一代、凭什么（base_generation / base_reason）；预演也照样给出。
    base_asset, base_generation, base_reason = _pick_base_generation(
        slot_dir, generations, payloads, preferred_asset_id)
    plan["base_generation"] = base_generation
    plan["base_reason"] = base_reason
    if base_asset is not None:
        plan["actions"].append("基代=%s（%s）" % (base_generation, base_reason))
    if not apply:
        plan["applied"] = False
        plan["note"] = "只打印清单（未带 --yes）：一个字节都没动"
        return plan

    # 2) 逐条核对（冻结引文对 vault 分片 sha、材料对文件指纹）；失效分桶。
    carried: list[dict] = []
    skipped: dict[str, int] = {}
    for claim in candidates:
        one, bucket = _inherit_claims({"claims": [claim]}, Path(home))
        if one:
            carried.extend(one)
        else:
            for name, count in bucket.items():
                skipped[name] = skipped.get(name, 0) + count

    # 3) 写单槽：基代见上（绝不按目录名字母序取"最新"），claims=合并集。
    if base_asset is None:
        raise LabError("没有可作底的资产（当前槽与历史代都不可读）")
    base_asset.pop("inherited_claims", None)
    base_asset.pop("skipped_claims", None)
    base_asset["claims"] = carried
    if carried:
        base_asset["profile"] = "claim-anchored"
    slot_dir.mkdir(parents=True, exist_ok=True)
    _write_folder(slot_dir, base_asset)

    # 4) 删全部历史代（单槽不动）。
    removed = 0
    for folder in generations:
        shutil.rmtree(folder, ignore_errors=True)
        removed += 1
    plan.update({"applied": True, "merged_claims": len(carried),
                 "skipped_claims": skipped, "removed_dirs": removed,
                 "slot_asset_id": base_asset.get("asset_id"),
                 "base_generation": base_generation, "base_reason": base_reason,
                 "note": "已收编：%d 条 claims 进单槽，删除 %d 个历史代目录" % (len(carried), removed)})
    return plan


def delete_asset(root: Path, asset_id: str, unit_id: str | None = None) -> dict:
    """Delete a draft asset folder, whole. Only `status: draft` may be deleted.

    A serving asset is the injection source of some workspace -- removing it
    would leave the fan-out pointing at nothing, so the verb refuses loudly and
    the caller (UI or CLI) tells the user to retire it first. The folder is the
    whole asset: the four-piece contract means there is nothing else to clean.
    """
    import shutil

    folder = find_asset(root, asset_id, unit_id)
    if folder is None:
        raise LabError("asset not found: %s" % asset_id)
    asset = read_json(folder / "asset.json")
    if (asset.get("status") or "draft") != "draft":
        raise LabError("只有 draft 状态的草稿可以删除：%s 的状态是 %s" % (asset_id, asset.get("status")))
    shutil.rmtree(folder)
    return {"asset_id": asset_id, "removed": True, "path": folder.as_posix()}
