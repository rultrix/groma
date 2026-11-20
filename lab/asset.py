"""Context-asset drafts: structure and provenance only, never content.

Layout follows the RSCL four-piece contract so that publishing later is a copy
plus a PR, not a rewrite. See docs/ASSET-SCHEMA.md.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from . import unitmatch
from .digest import normalise_session_id
from .state import workspace_ref
from .workspace import LabError, load_unit, meta_dir, read_json, utc_stamp, write_json

ASSETS_DIR = "assets"


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
    key = item.get("session_key") or ""
    if ":" in key:
        return key.split(":", 1)[1]
    return normalise_session_id(Path(item["path"]).name)


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
    lines += [
        "",
        "## 边界",
        "",
        "- 没有正文、没有 cwd、没有绝对路径；路径相对来源根。",
        "- 这是草稿，不是发布物；发布到共享仓前需要人工复核。",
        "",
    ]
    return "\n".join(lines)


def _render_validation() -> str:
    return "\n".join(
        [
            "# 验证状态",
            "",
            "- 生成：draft（由 Groma 自动生成）",
            "- 人工复核：未进行",
            "- 内容留存：无（只保存结构与出处）",
            "",
            "## 已具备",
            "",
            "- 来源会话可在本地 digest 中定位（见 asset.json 的 digest 与 sha256）",
            "- 每个文件有 sha256（见 manifest.json）",
            "",
            "## 未验证",
            "",
            "- 结论是否正确：未评估",
            "- 是否被 Agent 使用：未证明",
            "- 是否产生效果：未评估",
            "",
        ]
    )


def build_asset(root: Path, title: str, selection: list[tuple[str, str]], note: str = "") -> dict:
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
    records = unitmatch.digest_records_for(roots)
    digest_sha = {path.name: _sha256(path) for path in paths}

    index: dict[tuple[str, str], dict] = {}
    for record in records:
        for item in record.get("files", []):
            key = (item["source"], item["path"])
            index.setdefault(key, {**item, "digest": record["digest_name"]})

    keys = sorted(set(selection))
    chosen = []
    for key in keys:
        item = index.get(key)
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
        "notes": note or "",
    }

    target = meta_dir(root) / ASSETS_DIR / asset_id
    target.mkdir(parents=True, exist_ok=True)
    write_json(target / "asset.json", asset)
    (target / "README.md").write_text(_render_readme(asset), encoding="utf-8", newline="\n")
    (target / "VALIDATION.md").write_text(_render_validation(), encoding="utf-8", newline="\n")

    files = []
    for name in ("asset.json", "README.md", "VALIDATION.md"):
        path = target / name
        files.append({"path": name, "bytes": path.stat().st_size, "sha256": _sha256(path)})
    write_json(target / "manifest.json", {"schema": 1, "asset_id": asset_id, "files": files})
    return asset


def find_asset(root: Path, asset_id: str, unit_id: str | None = None) -> Path | None:
    """The folder of an asset inside this Unit, whichever workspace built it.

    `unit_id` 是"正在操作的协作主题"。给了就按它的成员集找，不给才退回
    `root` 自己的卡片——成员区自带一张卡时，这两种口径不是同一个集合。
    """
    roots = unitmatch.scope_workspaces(root, unit_id)
    for folder in unitmatch.asset_dirs_for(roots):
        if folder.name == asset_id and (folder / "asset.json").is_file():
            return folder
    return None


def load_asset(root: Path, asset_id: str, unit_id: str | None = None) -> dict:
    folder = find_asset(root, asset_id, unit_id)
    if folder is None:
        raise LabError("asset not found: %s" % asset_id)
    return read_json(folder / "asset.json")


def list_assets(root: Path) -> list[dict]:
    """Every asset of this Unit, newest first, one per asset id."""
    found: dict[str, dict] = {}
    for folder in unitmatch.asset_dirs_for(unitmatch.unit_workspaces(root)):
        path = folder / "asset.json"
        if not path.is_file():
            continue
        try:
            asset = read_json(path)
        except ValueError:
            continue
        current = found.get(asset["asset_id"])
        if current is None or (asset.get("generated_at") or "") > (current.get("generated_at") or ""):
            found[asset["asset_id"]] = asset
    return sorted(found.values(), key=lambda item: item.get("generated_at") or "", reverse=True)
