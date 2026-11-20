"""The Unit as an entity, and how local workspaces attach to it.

Policy (agreed 2026-11-03; scope re-based onto units.json on 2026-11-04):
- D1 remote+branch identical  -> HIGH, candidate, one confirmation (never silent).
- D2 remote only, branch differs -> MEDIUM, manual review.
- D3 a harness session whose cwd is the repo is folded in by the collector as
  evidence, NOT treated as a workspace to join; we only surface git workspaces.
- D4 name/time only, no git    -> LOW hint only, stay in General, do not join.
- D5 a workspace may be referenced by several Units (membership unit+binding).
- D6 joining never uploads or copies anything; it only records a relationship.
- D7 scan is narrow: registered workspaces + explicitly passed --scan paths.
- D8 dedup is by session id + content hash (handled in the digest layer).
- D9 Unit-scoped pages read the union of the Unit's workspaces, deduped by
  source+path; a Unit with one workspace therefore reads exactly as it used to.
- D10 scope resolves from `units.json` + `memberships.json`. A folder's
  `groma-unit.json` is an *export* of the Unit, so a clone re-attaches on another
  machine -- it is never the thing a page is scoped by, and opening or joining a
  folder rewrites nothing.
- D11 every Unit has exactly one home: the folder that holds its assets and
  exports. Moving the home keeps the previous folder as a member.
"""

from __future__ import annotations

import os
import re
import secrets
from pathlib import Path

from . import gitutil, state, unitlink
from .sources import load_sources
from .workspace import UNIT_FILE, LabError, ensure_unit, load_unit, meta_dir, write_json

ROLE_PRIMARY = "primary"
ROLE_EVIDENCE = "evidence"


def binding_id_for(root: str | Path) -> str:
    return state.workspace_ref(root)


def current_branch(root: str | Path) -> str | None:
    result = gitutil.run(["rev-parse", "--abbrev-ref", "HEAD"], cwd=root)
    if result is None or result.returncode != 0:
        return None
    branch = (result.stdout or "").strip()
    return branch or None


def repo_org_path(root: str | Path) -> str | None:
    link = unitlink.read_link(root)
    if link and link.get("org_path"):
        return link["org_path"]
    return unitlink.derive_org_path(root)


def _repo_name(org_path: str | None) -> str:
    if not org_path:
        return ""
    return org_path.rstrip("/").split("/")[-1].lower()


def _name_key(value: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (value or "").lower())


def classify(candidate_root: str | Path, target: dict) -> dict:
    """Score one candidate workspace against the target Unit identity.

    Branch is only probed when the remote already matches, to avoid one `git`
    call per unrelated folder (and the console flashes that come with them).
    """
    candidate = Path(candidate_root)
    link = unitlink.read_link(candidate)
    binding = binding_id_for(candidate)
    org = repo_org_path(candidate)  # reads groma-unit.json first; git only as a fallback

    base = {
        "binding_id": binding,
        "root": candidate.as_posix(),
        "org_path": org,
        "branch": None,
        "has_identity_file": link is not None,
    }

    if link and link.get("unit_id") == target["unit_id"]:
        return {**base, "role": ROLE_PRIMARY, "signal": "groma-unit.json", "confidence": 1.0, "suggest": "already_primary"}
    if org and org == target.get("org_path"):
        branch = current_branch(candidate)
        base["branch"] = branch
        if branch and target.get("branch") and branch == target["branch"]:
            return {**base, "role": ROLE_EVIDENCE, "signal": "remote+branch", "confidence": 0.98, "suggest": "join_needs_confirm"}
        return {**base, "role": ROLE_EVIDENCE, "signal": "remote-only", "confidence": 0.6, "suggest": "manual_review"}
    if not org:
        wanted = _repo_name(target.get("org_path"))
        if wanted and wanted in _name_key(candidate.name):
            return {**base, "role": ROLE_EVIDENCE, "signal": "name-only", "confidence": 0.2, "suggest": "general_only"}
    return {**base, "role": None, "signal": "none", "confidence": 0.0, "suggest": "not_a_candidate"}


def _read(path: Path) -> dict:
    import json

    return json.loads(path.read_text(encoding="utf-8"))


def newest(paths: list[Path]) -> Path | None:
    """The newest stamped artifact (`digest-<stamp>[-N].json`), by real order.

    Plain name sorting is wrong here: when two land in the same second the
    collision suffix `...Z-1.json` sorts *before* `...Z.json` because `-` < `.`,
    so `sorted()[-1]` hands back the older one.
    """
    if not paths:
        return None
    return max(paths, key=_stamp_key)


def _stamp_key(path: Path) -> tuple[str, int]:
    body = path.stem.split("-", 1)[-1]
    stamp, _, tail = body.partition("-")
    return (stamp, int(tail) if tail.isdigit() else 0)


def _latest_counts(root: Path) -> dict:
    folder = meta_dir(root) / "digests"
    newest_digest = newest(sorted(folder.glob("digest-*.json")) if folder.is_dir() else [])
    if newest_digest is None:
        return {"sessions": None, "events": None, "outside_cwds": []}
    try:
        data = _read(newest_digest)
    except (OSError, ValueError):
        return {"sessions": None, "events": None, "outside_cwds": []}
    return {
        "sessions": data.get("file_count"),
        "events": data.get("event_count"),
        "outside_cwds": [
            {**item, "name": Path(item.get("cwd") or "").name or "(无 cwd)"}
            for item in data.get("outside_cwds", [])
        ],
    }


def members_detail(unit_id: str) -> list[dict]:
    """Active memberships for a Unit, each resolved to a local path + counts."""
    out = []
    for membership in state.list_memberships(unit_id):
        local = state.workspace_path(membership["binding_id"])
        entry = dict(membership)
        entry["path"] = local
        if local:
            entry.update(_latest_counts(Path(local)))
        out.append(entry)
    return out


def describe(root: str | Path) -> dict:
    """What `groma unit show` reports: the Unit this folder stands for. Works without org_path."""
    root = Path(root)
    unit_id = load_unit(root)["unit_id"]
    record = resolve_unit(unit_id)
    return {
        "unit_id": unit_id,
        "name": record.get("name"),
        "org_path": record.get("org_path"),
        "created_at": record.get("created_at"),
        "workspace_ref": binding_id_for(root),
        "home": home_path(unit_id),
        "identity_file": unitlink.LINK_FILE if unitlink.read_link(root) else None,
        "local_paths": workspaces_for_unit(unit_id, fallback=root),
        "members": members_detail(unit_id),
    }


def _member_paths(unit_id: str) -> list[str]:
    paths = []
    for membership in state.list_memberships(unit_id):
        local = state.workspace_path(membership["binding_id"])
        if local:
            paths.append(local)
    return paths


def _card_created(path: str) -> str:
    """When this folder first became a Unit's, for picking a home deterministically."""
    local = meta_dir(Path(path)) / UNIT_FILE
    try:
        return _read(local).get("created_at") or ""
    except (OSError, ValueError):
        return ""


def sync_units() -> dict:
    """Fold identity cards into `units.json`. One-time per unit_id it has not seen.

    A card is how a folder says "I am about Unit X" -- on another machine, or in a
    second clone. Importing it is what makes that show up as one Unit without the
    human re-joining every folder. After the record exists, only explicit joins
    add folders, so a `leave` is never quietly undone by a stale card.
    """
    known = {item["unit_id"] for item in state.list_units()}
    groups: dict[str, list[dict]] = {}
    for record in state.list_workspaces():
        link = unitlink.read_link(record["path"]) or {}
        unit_id = link.get("unit_id") or record.get("unit_id")
        if unit_id:
            groups.setdefault(unit_id, []).append({**record, "link": link})
    created: list[str] = []
    for unit_id, items in sorted(groups.items()):
        if unit_id in known:
            continue
        home = sorted(items, key=lambda item: (_card_created(item["path"]) or "9", item["path"]))[0]
        name = next((item["link"].get("name") for item in items if item["link"].get("name")), None)
        org = next((item["link"].get("org_path") for item in items if item["link"].get("org_path")), None)
        state.upsert_unit(unit_id, name=name or Path(home["path"]).name, org_path=org, home_binding=home["ref"])
        for item in items:
            if item["ref"] != home["ref"]:
                state.add_membership(unit_id, item["ref"], role=ROLE_EVIDENCE, signal="identity-card", source="import")
        created.append(unit_id)
    for membership in state.list_memberships():
        unit_id = membership["unit_id"]
        if unit_id in known or unit_id in created:
            continue
        local = state.workspace_path(membership["binding_id"])
        state.upsert_unit(
            unit_id,
            name=Path(local).name if local else unit_id,
            home_binding=membership["binding_id"] if membership.get("role") == ROLE_PRIMARY else None,
        )
        created.append(unit_id)
    return {"units": state.list_units(), "created": created}


def identity_of(unit_id: str) -> dict:
    record = resolve_unit(unit_id)
    return {
        "unit_id": unit_id,
        "name": record.get("name"),
        "org_path": record.get("org_path"),
        "created_at": record.get("created_at"),
    }


def _oldest_card(unit_id: str) -> tuple[dict, str | None]:
    """The card that has existed longest among the folders naming this Unit."""
    best: dict = {}
    best_path: str | None = None
    stamp: str | None = None
    for path in _declared_paths(unit_id):
        folder = Path(path)
        if not folder.is_dir():
            continue
        link = unitlink.read_link(folder)
        if not link:
            continue
        created = _card_created(path) or "9"
        if stamp is None or created < stamp:
            stamp, best, best_path = created, link, path
    return best, best_path


def resolve_unit(unit_id: str) -> dict:
    """The Unit as this machine knows it: `units.json` first, a card as fallback.

    `groma init` gives a folder a card before any registry record exists, so the
    page must still be able to name the Unit and find its home without syncing.
    """
    record = dict(state.unit_record(unit_id) or {})
    card, card_path = _oldest_card(unit_id)
    record["unit_id"] = unit_id
    if not record.get("name"):
        record["name"] = card.get("name") or unit_id
    if not record.get("org_path"):
        record["org_path"] = card.get("org_path")
    if not record.get("home_binding") and card_path:
        record["home_binding"] = binding_id_for(card_path)
    if not record.get("created_at"):
        record["created_at"] = card.get("created_at")
    return record


def materialise(unit_id: str) -> dict:
    """Write a registry record for a Unit that only exists as cards so far."""
    if state.unit_record(unit_id) is None:
        record = resolve_unit(unit_id)
        if not record.get("home_binding"):
            raise LabError(
                "Unit %s 在本机既没有注册记录也没有任何卡片，无法确定家目录；"
                "先运行 `groma unit home` 指定一个，再改名或设置组织路径。" % unit_id
            )
        state.upsert_unit(
            unit_id,
            name=record["name"],
            org_path=record.get("org_path"),
            home_binding=record["home_binding"],
            created_at=record.get("created_at"),
        )
    return state.unit_record(unit_id)


def home_path(unit_id: str) -> str | None:
    """The folder this Unit's settings, assets and exports live in."""
    record = resolve_unit(unit_id)
    home = state.workspace_path(record.get("home_binding") or "")
    if home and Path(home).is_dir():
        return home
    paths = [path for path in _member_paths(unit_id) if Path(path).is_dir()]
    return paths[0] if paths else None


def workspaces_for_unit(unit_id: str, fallback: str | Path | None = None) -> list[str]:
    paths = []
    home = home_path(unit_id)
    if home:
        paths.append(home)
    for path in _member_paths(unit_id):
        if path not in paths:
            paths.append(path)
    if not paths and fallback is not None:
        paths = [Path(fallback).as_posix()]
    return paths


def scope_workspaces(root: str | Path | None, unit_id: str | None) -> list[str]:
    """跨区寻址的范围由"正在操作哪个协作主题"决定，不由落点目录的卡片决定。

    成员区自带一张身份卡时，`unit_workspaces(成员)` 返回的是**它自己的**主题，
    主题的家因此看不见（真机 0.55：扇出报 `asset not found`）。
    不给 unit_id 时保持旧口径——CLI 问的永远是"这个目录所属的 Unit"。
    """
    if not unit_id:
        return unit_workspaces(root)
    paths = workspaces_for_unit(unit_id)
    if not paths and root is not None:
        return unit_workspaces(root)
    return paths


def unit_workspaces(root: str | Path) -> list[str]:
    """The workspaces behind the Unit that `root` stands for, the standing folder first.

    Everything Unit-scoped (Discover/Graph/Activity/Assets) reads this set, so a
    join shows up as more sessions instead of a silent no-op.
    """
    root = Path(root)
    unit = load_unit(root)
    standing = Path(unit.get("workspace_root") or root).as_posix()
    paths = workspaces_for_unit(unit["unit_id"], fallback=standing)
    if standing not in paths:
        paths.insert(0, standing)
    return paths


def apply_card(root: str | Path, identity: dict) -> dict:
    """Export the Unit's identity into its home folder.

    Only Groma's own `groma-unit.json` and `.groma/unit.json` change; no project file is
    touched and nothing is uploaded. Members are deliberately never written to --
    joining records a relationship, nothing more.
    """
    root = Path(root)
    link = unitlink.adopt_link(root, identity)
    local = meta_dir(root) / UNIT_FILE
    if local.is_file():
        payload = _read(local)
        payload["unit_id"] = link["unit_id"]
        payload["name"] = link.get("name") or root.name
        write_json(local, payload)
    state.register_workspace(root, root.name)
    return link


def folder_unit(root: str | Path) -> str | None:
    """Which Unit this folder's own card (or local metadata) claims, if any."""
    root = Path(root)
    link = unitlink.read_link(root)
    if link:
        return link["unit_id"]
    local = meta_dir(root) / UNIT_FILE
    if local.is_file():
        return _read(local).get("unit_id")
    return None


def _declared_paths(unit_id: str) -> list[str]:
    """Folders whose registry entry says they belong to this Unit."""
    return [record["path"] for record in state.find_unit(unit_id) if record.get("unit_id") == unit_id]


def create_unit(name: str, home: str | Path, org_path: str | None = None) -> dict:
    """Stand up a Unit around one folder, which becomes its home."""
    name = (name or "").strip()
    if not name:
        raise LabError("Unit 名称不能为空")
    home = Path(home)
    if not home.is_dir():
        raise LabError("目录不存在：%s" % home.as_posix())
    org = unitlink.normalise_org_path(org_path) if org_path else None
    binding = binding_id_for(home)
    claimed = folder_unit(home)
    if claimed:
        others = [path for path in _declared_paths(claimed) if Path(path) != home]
        others += [item["binding_id"] for item in state.list_memberships(claimed) if item["binding_id"] != binding]
        if others:
            raise LabError("%s 还带着另一个 Unit 的身份，那边另有工作区；先在那一页处理" % home.name)
        # Alone in its Unit: rename and re-home that Unit instead of minting a
        # second id, because every digest and asset it wrote is stamped with it.
        state.upsert_unit(claimed, name=name, org_path=org, home_binding=binding)
        ensure_unit(home)
        apply_card(home, identity_of(claimed))
        return state.unit_record(claimed)
    unit_id = "unit_" + secrets.token_hex(8)
    state.upsert_unit(unit_id, name=name, org_path=org, home_binding=binding)
    ensure_unit(home)
    apply_card(home, identity_of(unit_id))
    return state.unit_record(unit_id)


def set_home(unit_id: str, target: str | Path) -> dict:
    """Move the Unit's home; the folder it leaves stays a member."""
    target = Path(target)
    if not target.is_dir():
        raise LabError("目录不存在：%s" % target.as_posix())
    materialise(unit_id)
    binding = binding_id_for(target)
    previous = resolve_unit(unit_id).get("home_binding")
    ensure_unit(target)
    apply_card(target, identity_of(unit_id))
    if previous and previous != binding and state.workspace_path(previous):
        state.add_membership(unit_id, previous, role=ROLE_EVIDENCE, signal="was-home", source="human")
    return state.upsert_unit(unit_id, home_binding=binding)


def rename_unit(unit_id: str, name: str) -> dict:
    """Name the Unit. Applies to every folder that carries its card, not one."""
    name = (name or "").strip()
    if not name:
        raise LabError("Unit 名称不能为空")
    materialise(unit_id)
    record = state.upsert_unit(unit_id, name=name)
    _refresh_cards(unit_id)
    return record


def set_org_path(unit_id: str, org_path: str) -> dict:
    try:
        org = unitlink.normalise_org_path(org_path)
    except ValueError as exc:
        raise LabError(str(exc))
    materialise(unit_id)
    record = state.upsert_unit(unit_id, org_path=org)
    _refresh_cards(unit_id)
    return record


def _refresh_cards(unit_id: str) -> None:
    """Re-export the identity into folders that *are* this Unit's own repository.

    A second clone of the home repo carries the same committed card and must
    follow a rename. A joined member only carries a relationship: writing the
    home's org_path into another repository's folder makes that clone auto-attach
    to this Unit on every machine, under an org path that is not its own.
    """
    identity = identity_of(unit_id)
    # The *declared* home only. `home_path()` falls back to the first member when
    # nothing is declared, and refreshing through that would promote a member
    # folder into the Unit's home — writing the identity card into another repo,
    # which is the exact harm the rest of this function exists to prevent.
    declared = (state.unit_record(unit_id) or {}).get("home_binding")
    home = state.workspace_path(declared) if declared else None
    if home and not Path(home).is_dir():
        home = None
    home_org = unitlink.derive_org_path(home) if home else None
    for path in _declared_paths(unit_id):
        folder = Path(path)
        if not folder.is_dir():
            continue
        if home and state.normalise_path(folder) == state.normalise_path(home):
            apply_card(folder, identity)
            continue
        if not unitlink.read_link(folder):
            continue  # never mint a card for a folder that has none
        org = unitlink.derive_org_path(folder)
        if org is not None and org == home_org:
            # An unknown org path is a reason to stop, not to guess: a folder with
            # no remote (or a local-path remote) would otherwise keep absorbing the
            # home's org_path on every rename.
            apply_card(folder, identity)


def unit_rows(unit_id: str) -> list[dict]:
    """The Unit's own table: one row per workspace, home first, with its state.

    `sources` is what a folder actually collects from, and it is still stored
    per folder (P3 moves that onto the Unit), so a member that declares nothing
    shows zero sessions honestly instead of pretending to be aggregated.
    """
    home = resolve_unit(unit_id).get("home_binding")
    members = {item["binding_id"]: item for item in state.list_memberships(unit_id)}
    rows = []
    for path in workspaces_for_unit(unit_id):
        folder = Path(path)
        ref = binding_id_for(folder)
        initialised = (meta_dir(folder) / UNIT_FILE).is_file()
        rows.append(
            {
                "path": folder.as_posix(),
                "name": folder.name,
                "ref": ref,
                "is_home": ref == home,
                "membership": members.get(ref),
                "initialised": initialised,
                "sources": [item["name"] for item in load_sources(folder)] if initialised else [],
                **_latest_counts(folder),
            }
        )
    rows.sort(key=lambda item: (not item["is_home"], item["path"]))
    return rows


def unit_list() -> list[dict]:
    """Every Unit this machine can see: registry records, folder cards, memberships."""
    ids = {item["unit_id"] for item in state.list_units()}
    ids |= {record["unit_id"] for record in state.list_workspaces() if record.get("unit_id")}
    ids |= {item["unit_id"] for item in state.list_memberships()}
    out = []
    for unit_id in sorted(ids):
        roots = workspaces_for_unit(unit_id)
        out.append({**resolve_unit(unit_id), "workspaces": roots, "workspace_count": len(roots)})
    return out


def digest_paths_for(roots: list[str]) -> list[Path]:
    """The newest digest file of each workspace, in the same order as `roots`."""
    found: list[Path] = []
    for path in roots:
        folder = meta_dir(Path(path)) / "digests"
        latest = newest(sorted(folder.glob("digest-*.json")) if folder.is_dir() else [])
        if latest:
            found.append(latest)
    return found


def digest_records_for(roots: list[str]) -> list[dict]:
    """The newest digest of each workspace, primary first.

    A member that has never been collected has no digest, which is honest:
    joining records the relationship, the collector fills in the evidence.
    """
    records = []
    for path in digest_paths_for(roots):
        try:
            records.append({**_read(path), "digest_name": path.name})
        except (OSError, ValueError):
            continue
    return records


def unit_files(records: list[dict]) -> list[dict]:
    """Session rows for the whole Unit, deduped by source+path.

    Each workspace's digest only carries sessions whose cwd is inside it, so a
    workspace nested under another can report the same session twice.
    """
    seen: set[tuple[str, str]] = set()
    out: list[dict] = []
    for record in records:
        for item in record.get("files", []):
            key = (item.get("source"), item.get("path"))
            if key in seen:
                continue
            seen.add(key)
            out.append(item)
    return out


def unit_digest_union(root: str | Path) -> dict:
    """One digest-shaped record for the Unit, for the pages that show counts."""
    roots = unit_workspaces(root)
    records = digest_records_for(roots)
    files = unit_files(records)
    stamps = [item.get("created_at") for item in records if item.get("created_at")]
    return {
        "files": files,
        "file_count": len(files),
        "event_count": sum(int(item.get("events") or 0) for item in files),
        "unattributed_outside": sum(int(item.get("unattributed_outside") or 0) for item in records),
        "created_at": max(stamps) if stamps else None,
        "workspaces": roots,
    }


def scan_paths_for(roots: list[str]) -> list[Path]:
    """Every scan record written by these workspaces, primary first."""
    paths: list[Path] = []
    for path in roots:
        folder = meta_dir(Path(path)) / "scans"
        paths.extend(sorted(folder.glob("scan-*.json")) if folder.is_dir() else [])
    return paths


def asset_dirs_for(roots: list[str]) -> list[Path]:
    paths: list[Path] = []
    for path in roots:
        folder = meta_dir(Path(path)) / "assets"
        paths.extend(sorted(folder.glob("asset-*")) if folder.is_dir() else [])
    return paths


def detect(root: str | Path, scan_paths: list[str] | None = None) -> dict:
    """Read-only: candidates and the Unit's own rows, for the Unit page.

    `root` is only where we stand to ask; the answer is about the Unit. org_path is
    a convenience signal, never a precondition: without a git remote you can still
    merge local folders, via an explicit manual join.
    """
    root = Path(root)
    unit_id = load_unit(root)["unit_id"]
    record = resolve_unit(unit_id)
    home = home_path(unit_id) or root.as_posix()
    target = {"unit_id": unit_id, "org_path": record.get("org_path"), "branch": current_branch(Path(home))}

    primary_binding = binding_id_for(root)
    active = {m["binding_id"] for m in state.list_memberships(unit_id)}

    seen = {primary_binding}
    pool: list[Path] = []
    for record in state.list_workspaces():
        path = Path(record["path"])
        if record["ref"] not in seen and path.is_dir():
            seen.add(record["ref"])
            pool.append(path)
    for extra in scan_paths or []:
        path = Path(extra).expanduser()
        ref = binding_id_for(path)
        if ref not in seen and path.is_dir():
            seen.add(ref)
            pool.append(path)

    candidates = []
    for path in pool:
        outcome = classify(path, target)
        if outcome["suggest"] == "not_a_candidate":
            continue
        outcome["is_member"] = outcome["binding_id"] in active
        outcome["path"] = path.as_posix()
        candidates.append(outcome)

    roots = workspaces_for_unit(unit_id, fallback=root)
    candidates.sort(key=lambda item: (-item["confidence"], item["path"]))
    return {
        "unit_id": unit_id,
        "name": record.get("name"),
        "org_path": record.get("org_path"),
        "has_org_path": bool(record.get("org_path")),
        "branch": target["branch"],
        "home": home,
        "primary": {"binding_id": binding_id_for(Path(home)), "path": home, **_latest_counts(Path(home))},
        "rows": unit_rows(unit_id),
        "members": members_detail(unit_id),
        "workspaces": roots,
        "workspace_counts": [{"path": path, **_latest_counts(Path(path))} for path in roots],
        "candidates": candidates,
        "note": "无 git remote 也能并入：在下方手动填目录即可（signal=manual）。有 remote 时会自动给出高置信候选。",
    }


def join_unit(unit_id: str, candidate_root: str | Path, role: str | None = None,
              authorized_by: str | None = None) -> dict:
    """Attach one local folder to a Unit. Records a relationship, nothing else.

    Nothing is written into the folder -- no `.groma/`, no `groma-unit.json`, no upload.
    Its evidence appears the next time the collector runs in it.
    """
    record = resolve_unit(unit_id)
    home = home_path(unit_id)
    target = {
        "unit_id": unit_id,
        "org_path": record.get("org_path"),
        "branch": current_branch(Path(home)) if home else None,
    }
    outcome = classify(candidate_root, target)
    forced = outcome["suggest"] == "not_a_candidate"
    if forced and role is None:
        raise LabError("该目录与本 Unit 无自动信号；确认属于同一件事时再用 --role 强制并入（不会移动文件）")
    signal = "manual" if forced else outcome["signal"]
    confidence = 1.0 if forced else outcome["confidence"]  # a human explicitly decided
    candidate_path = Path(outcome["root"])
    state.register_workspace(candidate_path, candidate_path.name)  # local path only; touches no project files
    return state.add_membership(
        unit_id=unit_id,
        binding_id=outcome["binding_id"],
        role=role or outcome["role"] or ROLE_EVIDENCE,
        signal=signal,
        source="manual",
        confidence=confidence,
        authorized_by=authorized_by or os.environ.get("USERNAME") or os.environ.get("USER"),
    )


def join(root: str | Path, target_unit_id: str | None, candidate_root: str | Path,
         role: str | None = None, authorized_by: str | None = None) -> dict:
    """CLI shape: the Unit is the folder's own, unless another one is named."""
    unit_id = target_unit_id or load_unit(Path(root))["unit_id"]
    return join_unit(unit_id, candidate_root, role=role, authorized_by=authorized_by)


def leave_unit(unit_id: str, candidate_root: str | Path) -> bool:
    return state.remove_membership(unit_id, binding_id_for(candidate_root))


def leave(root: str | Path, candidate_root: str | Path, unit_id: str | None = None) -> bool:
    return leave_unit(unit_id or load_unit(Path(root))["unit_id"], candidate_root)
