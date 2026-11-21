"""S1 web surface: six nav pages, server-rendered, no JavaScript required (概览/发现/活动/资产/主题/观测).

Destructive actions are two submissions -- a confirm page that lists the cost,
then a POST the server refuses without `confirm=1`; nothing depends on an
inline handler. 关系图 is gone (K17): the same questions are answered by
`/evidence` (哪几段证据) and `/gaps` (漏了哪些，为什么).
See docs/TASK-ui-quickwins-2026-11-10.md 刀 5 / 刀 7.

The active workspace can change at runtime; every handler reads it through
`current()` instead of a fixed root.
"""

from __future__ import annotations

import json
import socket
import sys
import time
from pathlib import Path
from urllib.parse import quote

from flask import Flask, redirect, render_template, request, Response, url_for

from lab import __version__
from lab import hooks
from lab import reconcile, topics, unitmatch
from lab.agents import sync_agents
from lab.asset import attach_claims, build_asset, list_assets, verify_claims
from lab.collect import catchup, catchup_all, last_run
from lab.control import is_paused, machine_state, pause_machine, resume_machine
from lab.discovery import discover
from lab.inject import (drop_unit, export_context, forget_workspace, injection_summary, inject_unit,
                          refresh as refresh_payload, uninject)
from lab.observe_this_machine import digest_rows_from_index, generate_observation_page
from lab.settings import load_settings, save_settings
from lab.sources import add_source, load_sources, remove_source
from lab.state import (
    ephemeral_root,
    first_run,
    list_memberships,
    list_workspaces,
    mark_first_run,
    normalise_path,
    orphan_units_if_removed,
    register_workspace,
    workspace_ref,
)
from lab.summary import build_summary
from lab.timeformat import local_text
from lab.unitlink import link_path
from lab.workspace import LabError, ensure_unit, load_unit, meta_dir


# 观测页是静态产物：reindex 时生成并落盘，/observe 只读文件。
# 一次生成实测 52.4 秒（本机 1.8 GB / 249 分片），不能每次打开页面都付这个成本。
_OBSERVE_HTML_FILE = "observe.html"


def _observe_html_path():
    from lab.state import state_root
    return state_root() / _OBSERVE_HTML_FILE


# 产物里没有任何链接（lab/observe_this_machine.py 的 `<a`/`href` 各 0 命中，2026-11-10 实测），
# 所以"打开观测页回不来"只能在**供给层**修：把返回条插进响应，磁盘那份仍是可独立存在的快照。
# 自标身份（meta）让"这条是 Groma 加的"可被机器判定——否则响应 == 文件 会被当成不变量。
_OBSERVE_RETURN_MARK = '<meta name="groma-observe-return-bar" content="1">'
_OBSERVE_RETURN_BAR = (
    _OBSERVE_RETURN_MARK
    + '<style>.groma-return-bar{position:sticky;top:0;z-index:10;background:#fff;'
      'border-bottom:1px solid #e2e8f0;padding:8px 16px;display:flex;gap:14px;align-items:center;'
      'font:13px/1.5 "Segoe UI","Microsoft YaHei",system-ui,sans-serif;color:#0f172a}'
      '.groma-return-bar a{color:#0f766e;font-weight:600;text-decoration:none;'
      'border:1px solid #e2e8f0;border-radius:8px;padding:4px 10px}</style>'
    + '<div class="groma-return-bar"><span>Groma · 单机观测产物（静态快照）</span>'
      '<a href="/observe">&larr; 返回观测台</a></div>')


def _with_observe_return_bar(html: str) -> str:
    """Insert the return bar after the artifact's opening `<body>`; idempotent, and a document with no `<body` is served byte-for-byte."""
    if 'name="groma-observe-return-bar"' in html:
        return html
    start = html.lower().find("<body")
    if start == -1:
        return html
    end = html.find(">", start)
    if end == -1:
        return html
    return html[:end + 1] + _OBSERVE_RETURN_BAR + html[end + 1:]


def _observe_stale_key():
    """Return the (vault_root, generated_at) tuple last baked into the file, or None."""
    try:
        import json as _json
        meta_path = _observe_html_path().with_suffix(".meta.json")
        if meta_path.is_file():
            payload = _json.loads(meta_path.read_text(encoding="utf-8"))
            return tuple(payload.get("key") or [])
    except (OSError, ValueError):
        pass
    return None


# K12: a reindex takes tens of seconds and JavaScript is not on the table, so the
# browser owns no control while the POST is in flight. A live progress bar is
# therefore impossible; what is possible and testable is a refusal to start a
# second one, a receipt with timestamps when it finishes, and a warning that
# names the wait before the click. The marker lives next to the artifact.
_OBSERVE_REINDEX_FILE = "observe_reindex.json"
# The task card said "7–20 秒". Measured on the real 1.8 GB vault at release
# time it is 52.4 秒 -- an estimate that is off by 3x is a second lie replacing
# the first, so the wait is no longer asserted: once a receipt exists the page
# quotes the machine's own last measured duration.
# 注意这里不能说「首次」：进行中的标记会把上一次完成的回执覆盖掉，
# 于是重入被拒时走的正是这条回落——那台机器往往已经跑过很多次了。
# 真机实测到的就是这个瑕疵（第二次点被拒，文案却写「首次重建要几十秒」）。
_OBSERVE_REINDEX_WAIT_FIRST = "重建要几十秒（vault 越大越久），这期间页面不会响应，别重复点"
# A crashed process would otherwise leave the marker unfinished forever and the
# button permanently refused. Past this many seconds the marker is treated as
# abandoned -- still shown as a receipt, no longer as a running job.
_OBSERVE_REINDEX_ABANDONED_AFTER = 300


def _observe_reindex_path():
    from lab.state import state_root
    return state_root() / _OBSERVE_REINDEX_FILE


def _observe_reindex_load() -> dict:
    """The last reindex marker: {started_at, finished_at, outcome, seconds}.

    Missing or unreadable means "no receipt", never "one is running" -- the
    page must not claim work it cannot prove.
    """
    try:
        if _observe_reindex_path().is_file():
            payload = json.loads(_observe_reindex_path().read_text(encoding="utf-8"))
            return payload if isinstance(payload, dict) else {}
    except (OSError, ValueError):
        pass
    return {}


def _observe_reindex_save(payload: dict) -> None:
    try:
        _observe_reindex_path().write_text(
            json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8", newline="\n")
    except OSError:
        pass  # 回执是辅助信息：写不下去不该把一次成功的重建报成失败


def _reindex_running(marker: dict) -> bool:
    """True when a marker was opened and not closed inside the live window."""
    started_at = marker.get("started_at")
    return bool(started_at) and not marker.get("finished_at") and _reindex_started_recent(started_at)


def _reindex_abandoned(marker: dict) -> bool:
    """Opened but never closed, and past the live window -- a dead run.

    R11: the template used to recompute "is it running" as "started and not
    finished", i.e. without the window the router applies. So the page kept
    promising a guard the server no longer enforced, and the promise was the
    one thing the operator was clicking by. Both verdicts come from here now.
    """
    return (bool(marker.get("started_at")) and not marker.get("finished_at")
            and not _reindex_running(marker))


def _reindex_started_recent(started_at: str) -> bool:
    from datetime import datetime, timezone

    try:
        then = datetime.fromisoformat(str(started_at).replace("Z", "+00:00"))
    except ValueError:
        return False
    return (datetime.now(timezone.utc) - then).total_seconds() <= _OBSERVE_REINDEX_ABANDONED_AFTER


def _reindex_wait_hint(marker: dict) -> str:
    """How long the next reindex will take, in the page's own words.

    Quoting the last measured run beats reciting a constant: the constant was
    wrong the moment the vault grew, and nobody would have noticed.
    """
    seconds = marker.get("seconds")
    if marker.get("outcome") == "ok" and isinstance(seconds, (int, float)):
        return "上次重建实测 %s 秒（vault 越大越久），这期间页面不会响应，别重复点" % seconds
    return _OBSERVE_REINDEX_WAIT_FIRST


def _observe_host() -> str:
    """The machine name baked into the observation page.

    `machine_state()` returns scope/paused/updated_at only -- it has no `host`
    key -- so the old `.get("host", "unknown")` landed on the placeholder every
    single time. The state lookup stays first on purpose: when the shell starts
    writing host and port into state (K18), this page follows without a revert.
    """
    return machine_state().get("host") or socket.gethostname()


def _bake_observe_html():
    """Read vault index, generate the observation HTML, persist to disk.

    Returns (html, error). On error html is None; on success error is None.
    """
    from lab import vault as vault_mod
    import json

    try:
        vault_root = vault_mod.vault_root()
        index_path = vault_root / "index.json"
        if not index_path.is_file():
            return None, "Vault 索引不存在，请先运行 `groma vault reindex`"
        index = json.loads(index_path.read_text(encoding="utf-8"))
        sessions = index.get("sessions", [])
        if not sessions:
            return None, "Vault 中没有会话数据，请先运行 `groma vault backfill-db` 或 `groma vault backfill-file`"

        digest_rows = digest_rows_from_index(sessions)

        html = generate_observation_page(
            vault_root, digest_rows, _observe_host()
        )
        if not html:
            return None, "生成观测页为空（vault 里没有可分析的事件）"

        _observe_html_path().write_text(html, encoding="utf-8", newline="\n")
        meta = {"key": [str(vault_root), index.get("generated_at")]}
        _observe_html_path().with_suffix(".meta.json").write_text(
            json.dumps(meta, ensure_ascii=False, indent=2),
            encoding="utf-8", newline="\n",
        )
        return html, None
    except Exception as exc:
        return None, "生成观测页失败：%s" % str(exc)


def _gap_reason(row: dict) -> str:
    """Why this session is on the list but not in the collection.

    A count without a reason is the thing the 关系图 used to be: numbers the
    reader cannot act on. Order matters -- the reassuring cases come first.
    """
    shapes = row.get("file_shapes") or []
    if row.get("in_db") and not row.get("in_files"):
        return "厂商清单里有，磁盘上没有转写文件（正文还没落到能扫的地方）"
    if "prefix8" in shapes:
        return "磁盘上只有 8 位前缀，锚不到完整会话 id —— 不替它认一个会话"
    if row.get("in_files") and not row.get("in_db"):
        return "磁盘有转写，厂商清单里没有这个会话（别的 Harness，或已删除）"
    if row.get("in_files") and row.get("in_db"):
        return "两边都有，但没采到：采集根没申报，或申报了还没扫"
    return "对账里有这一条，两边都找不到实物 —— 需要人工看一眼"


def _evidence_stats(path: Path) -> dict:
    meta = meta_dir(path)
    counts = {}
    for name in ("scans", "digests", "summaries", "receipts", "assets"):
        folder = meta / name
        counts[name] = len(list(folder.glob("*"))) if folder.is_dir() else 0
    size = 0
    if meta.is_dir():
        for item in meta.rglob("*"):
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    return {"counts": counts, "size_mb": round(size / 1048576, 1)}


def create_app(unit_root: Path, on_switch=None) -> Flask:
    app = Flask(__name__, template_folder="templates")

    @app.template_filter("local_ts")
    def local_ts_filter(value, fallback: str = "") -> str:
        """界面时刻：存 UTC、按本机时区显示并带该时刻的有效偏移。"""
        return local_text(value, fallback=fallback)

    @app.template_filter("local_date")
    def local_date_filter(value) -> str:
        """日期格：先换算再取日期——UTC 23:30 在本机可能已是次日。"""
        return local_text(value, date_only=True)

    boot_root = Path(unit_root)
    ensure_unit(boot_root)
    register_workspace(boot_root, boot_root.name)
    unitmatch.sync_units()  # folders that carry the same card become one Unit record
    active = {"unit": unitmatch.folder_unit(boot_root) or load_unit(boot_root)["unit_id"]}

    def target_unit() -> str:
        """Which theme a row-level action means; falls back to the app's own."""
        return (request.form.get("unit") or "").strip() or current_unit()

    def current_unit() -> str:
        """The Unit every page is scoped by. This is the axis, not a folder."""
        return active["unit"]

    def current() -> Path:
        """The Unit's home folder: where its settings, assets and exports land.

        Collection settings are still stored per folder (P3 moves them onto the
        Unit), so the home is what "改设置/建资产" acts on -- not what you see.
        """
        home = unitmatch.home_path(active["unit"])
        return Path(home) if home else boot_root

    def focus(unit_id: str) -> None:
        active["unit"] = unit_id
        home = unitmatch.home_path(unit_id)
        if home is not None and on_switch is not None:
            on_switch(Path(home))

    def adopt_home(path: Path) -> None:
        """A folder that just became the Unit's home: sync its agents, scan it once.

        Best-effort -- a folder whose harness data is missing should not fail the
        move; the collector sweeps it again on the next tick.
        """
        try:
            sync_agents(path)
            catchup(path, force=True, written_by="web:adopt-home")
        except LabError:
            pass

    def other_workspaces(unit_id: str) -> list[dict]:
        inside = set(unitmatch.workspaces_for_unit(unit_id))
        # 候选表与 registered_paths() 用同一把尺——目录已经不在的登记行
        # 不是"可并入的工作区"，它是"失效的登记"，去处在下面那一节。
        return [item for item in live_workspaces() if item["path"] not in inside]

    def live_workspaces() -> list[dict]:
        return [item for item in list_workspaces() if Path(item["path"]).is_dir()]

    def dead_workspaces() -> list[dict]:
        return [item for item in list_workspaces() if not Path(item["path"]).is_dir()]

    def registered_paths() -> list[Path]:
        return [Path(item["path"]) for item in live_workspaces()]

    def discover_target(mapping) -> Path | None:
        """The workspace the 发现 page is about -- for reading AND for writing.

        One function, called with `request.args` on GET and `request.form` on
        the two POSTs. That is the whole of "读写同源" (K9′): before this the
        table rendered `chosen` while 申报/移除 wrote to `current()`, so a user
        aimed at an empty folder and filled a different workspace's sources.json
        without the page ever saying so.
        """
        paths = registered_paths()
        wanted = normalise_path((mapping.get("ws") or "").strip())
        hit = next((path for path in paths if normalise_path(path) == wanted), None) if wanted else None
        if hit is not None:
            return hit
        # ② the scope the app is already writing to -- `current()` is some Unit's
        # home by construction, so the default page is never an empty scratch
        # folder. ③ only then fall back to the first registered row (K7).
        home = normalise_path(current())
        return next((path for path in paths if normalise_path(path) == home),
                    paths[0] if paths else None)

    def global_report() -> dict | None:
        """The machine-wide reconciliation: vendor lists vs what we collected.

        Read-only, and deliberately not cached across requests: a stale number
        on this line is worse than a slow page.
        """
        paths = registered_paths()
        if not paths:
            return None
        try:
            return reconcile.build_reconciliation_for(paths)
        except (LabError, OSError, ValueError):
            return None

    def first_run_view() -> dict:
        """The two first-run questions, answered once per render instead of per template.

        Both have a legacy escape hatch: a machine that already moved the vault
        (`set_at`, only ever written by an explicit set-root or a move) or that
        already holds a folder other than Groma's own boot directory agreed without
        being asked, and must not be asked again now.
        """
        from lab import vault as vault_mod

        stamps = first_run()
        vault_ok = bool(stamps["vault_confirmed_at"]) or bool(vault_mod.load_config().get("set_at"))
        workspace_ok = bool(stamps["workspace_settled_at"]) or any(
            normalise_path(item["path"]) != normalise_path(boot_root) for item in list_workspaces()
        )
        return {
            "vault_root": vault_mod.vault_root().as_posix(),
            "boot_root": boot_root.as_posix(),
            "vault_ok": vault_ok,
            "workspace_ok": workspace_ok,
            "first_run_needed": not (vault_ok and workspace_ok),
        }

    def context(**extra: object) -> dict:
        root = current()
        unit = unitmatch.resolve_unit(current_unit())
        data = {
            "app_version": __version__,
            "unit": unit,
            "unit_name": unit.get("name"),
            "current_unit_id": unit["unit_id"],
            "units": unitmatch.unit_list(),
            "themes": unitmatch.themes(),
            "topics": topics.list_topics(),
            "workspaces_all": list_workspaces(),
            "workspace_root": root.as_posix(),
            "paused": machine_state()["paused"],
            "machine": machine_state(),
            "registered": registered_paths(),
            "report": global_report(),
            "last": last_run(root),
            "sources": load_sources(root),
            "digest": unitmatch.unit_digest_union(root),
            "settings": load_settings(root),
            "error": request.args.get("error"),
            "notice": request.args.get("notice"),
            **first_run_view(),
        }
        data.update(extra)
        return data

    def back(endpoint: str, error: str | None = None, notice: str | None = None):
        target = url_for(endpoint)
        if error:
            target += "?error=" + quote(error)
        if notice:
            target += "?notice=" + quote(notice)
        return redirect(target, code=303)

    @app.get("/")
    def index():
        def collected_for(roots: list[str]) -> dict:
            try:
                records = unitmatch.digest_records_for(roots)
            except LabError:
                records = []
            stamps = [r.get("created_at") for r in records if r.get("created_at")]
            return {
                "counts": topics.classification_counts(unitmatch.unit_files(records)),
                "last_at": max(stamps, default=None),
                "latest": max(records, key=lambda r: r.get("created_at") or "", default=None),
            }

        # 这张表的行只认「人建过的协作主题」那一个谓词（`unitmatch.themes()`），不各页再写一份过滤。
        # 「上次写入」仍按整机口径扫所有 Unit 记录——那一行说的是这台机器最后一次写入，不是某个主题。
        overview = []
        for unit in unitmatch.themes():
            roots = unit["workspaces"]
            view = collected_for(roots)
            record = topics.topic_record(unit["unit_id"]) or {}
            overview.append({
                "unit_id": unit["unit_id"],
                "name": record.get("name") or unit.get("name") or unit["unit_id"],
                "org_path": record.get("org_path") or unit.get("org_path"),
                "workspaces": len(roots),
                "collected": view["counts"]["collected_sessions"],
                "unclassified": view["counts"]["unclassified"],
                "last_at": view["last_at"],
            })
        newest: dict | None = None
        for unit in unitmatch.unit_list():
            latest = collected_for(unit.get("workspaces") or [])["latest"]
            if latest is not None and (newest is None or (latest.get("created_at") or "") > (newest.get("created_at") or "")):
                newest = latest
        from lab.classify_run import STATE_TEXT, last_runs

        runs = last_runs(500)
        last_attempt = runs[-1] if runs else None
        last_success = next((row for row in reversed(runs) if row.get("state") == "applied"), None)
        classify_status = {
            "attempt": ((last_attempt or {}).get("topic"), (last_attempt or {}).get("at"),
                        STATE_TEXT.get((last_attempt or {}).get("state"),
                                       (last_attempt or {}).get("state"))),
            "success": ((last_success or {}).get("topic"), (last_success or {}).get("at")),
        }
        return render_template(
            "index.html",
            **context(overview=overview, last_write=newest, classify_status=classify_status),
        )

    @app.get("/discover")
    def discover_page():
        paths = registered_paths()
        chosen = discover_target(request.args)
        rows = []
        for path in paths:
            try:
                records = unitmatch.digest_records_for([path.as_posix()])
            except LabError:
                records = []
            record = records[0] if records else {}
            try:
                sources = load_sources(path)
            except LabError:
                sources = []
            rows.append({
                "path": path.as_posix(),
                "name": path.name,
                "collected": record.get("file_count"),
                "at": record.get("created_at"),
                "outside": record.get("unattributed_outside"),
                "outside_cwds": (record.get("outside_cwds") or [])[:3],
                "sources": [source.get("name") for source in sources],
                "selected": path == chosen,
                # Server-side so the label cannot drift from the comparison the
                # first-run card already makes (case and separators differ on Windows).
                "is_boot_root": normalise_path(path) == normalise_path(boot_root),
                # 判据与登记那一步共用同一个函数：亮 badge 的范围必须等于会被挡住的范围。
                "is_temp": ephemeral_root(path),
            })
        root_rows = []
        for entry in (reconcile.harness_roots_for([chosen]) if chosen is not None else []):
            files = sum(1 for _ in entry["path"].rglob("*.jsonl")) if entry["exists"] else 0
            root_rows.append({**entry, "files": files,
                              "declared_include": entry["declared"],
                              "home_path": entry["home"].as_posix()})
        declared = []
        candidates: list[dict] = []
        if chosen is not None:
            try:
                for source in load_sources(chosen):
                    declared.append({**source,
                                     "family": reconcile.family_of(source.get("harness") or source.get("name"))})
            except LabError:
                declared = []
            candidates = discover(chosen, [chosen.as_posix()])
        # `harness_roots_for` 只遍历**已申报源**，所以零源工作区会得到
        # 一张空的采集根表——而「加入」按钮已在 W16 删掉，页面上就只剩"没申报"和一个空指针。
        # 这里把"本机装着、但这个区没申报"的家族补成采集根行，让 B 表仍是每个家族唯一的申报口
        # （验收给的两条修法里选 A：候选来源补齐，而不是在 A 块再开一个按钮）。
        if chosen is not None:
            seen = {(row["family"], row["subroot"]) for row in root_rows}
            for candidate in candidates:
                family = reconcile.family_of(candidate.get("harness") or candidate.get("name"))
                subroot = candidate.get("include") or ""
                if not candidate.get("exists") or (family, subroot) in seen:
                    continue
                home = Path(candidate["root"])
                target = home / subroot if subroot else home
                root_rows.append({
                    "family": family,
                    "harness": candidate.get("harness") or family,
                    "source": candidate.get("name") or family,
                    "subroot": subroot,
                    "path": target,
                    "home": home,
                    "home_ref": workspace_ref(home),
                    "exists": True,
                    "declared": False,
                    "declared_include": False,
                    "files": sum(1 for _ in target.rglob("*.jsonl")) if target.is_dir() else 0,
                    "home_path": home.as_posix(),
                })
                seen.add((family, subroot))
            root_rows.sort(key=lambda row: (row["family"], row["subroot"]))
        # 切换器的 option：顺序跟着清单（拍板 7），但排序不钉进任何测试。
        # 同名工作区必须能区分（设计侧复核裁定 3）：显示名取目录名，重名时各多带一级父目录。
        names = [row["name"] for row in rows]
        duplicated = {name for name in names if names.count(name) > 1}
        scope_options = []
        for row in rows:
            label = row["name"]
            if label in duplicated:
                label = "%s/%s" % (Path(row["path"]).parent.name, label)
            scope_options.append({"path": row["path"], "label": label, "selected": row["selected"],
                                  "is_boot_root": row["is_boot_root"]})
        return render_template(
            "discover.html",
            **context(workspace_rows=rows, chosen=chosen.as_posix() if chosen else None,
                      chosen_name=chosen.name if chosen else None,
                      # pills 收成原生下拉；`scope_selected` 仍只在本页给。
                      scope_selected=chosen.as_posix() if chosen else None,
                      scope_options=scope_options,
                      root_rows=root_rows, declared=declared, candidates=candidates),
        )

    @app.get("/sources")
    def sources_page():
        return redirect(url_for("discover_page"), code=303)

    @app.get("/activity")
    def activity_page():
        sections = []
        for unit in unitmatch.themes():
            home = unitmatch.home_path(unit["unit_id"])
            if home is None:
                sections.append({"unit": unit, "home": None, "summary": None, "coverage": None})
                continue
            try:
                summary = build_summary(Path(home), 30)
            except LabError:
                summary = None
            sections.append({"unit": unit, "home": home, "summary": summary})
        return render_template("activity.html", **context(sections=sections))

    @app.get("/evidence")
    def evidence_page():
        """K17: 主题的证据清单。关系图回答的是"谁和谁有关系"，而没人这么问过。

        Data comes from `topics.active_evidence()` and the digests -- no new
        algorithm, the view is all that was missing.
        """
        wanted = (request.args.get("unit") or "").strip()
        unit = unitmatch.resolve_unit(wanted or current_unit())
        roots = unitmatch.workspaces_for_unit(unit["unit_id"])
        records = []
        for root in roots:
            try:
                records.extend(unitmatch.digest_records_for([root]))
            except LabError:
                continue
        files = unitmatch.unit_files(records)
        topic = topics.resolve_topic(unit["unit_id"])
        evidence = topics.active_evidence(topic["topic_id"]) if topic is not None else []
        latest = {(row.get("source"), row.get("path")): row for row in evidence}
        rows = []
        for key in sorted(latest):
            record = latest[key]
            match = next((item for item in files
                          if (item.get("source"), item.get("path")) == key), None)
            rows.append({
                "source": key[0],
                "path": key[1],
                "at": record.get("at"),
                "decided_by": record.get("decided_by"),
                "confidence": record.get("confidence"),
                "signals": record.get("signals") or [],
                "events": (match or {}).get("events"),
                "agent_name": (match or {}).get("agent_name"),
                "collected": match is not None,
            })
        return render_template(
            "evidence.html",
            **context(evidence_unit=unit, evidence_workspaces=roots,
                      evidence_sessions=len(files), evidence_rows=rows),
        )

    @app.get("/gaps")
    def gaps_page():
        """K17: 采集缺口清单。分母是厂商清单与磁盘转写，绝不是我们自己的 digest。"""
        paths = registered_paths()
        try:
            report = reconcile.build_reconciliation_for(paths)
        except LabError as exc:
            return back("index", "对账没跑成：%s" % exc)
        counts = report["counts"]
        examples = report["examples"]["missing_from_collection"]
        rows = [{**row, "why": _gap_reason(row)} for row in examples]
        return render_template(
            "gaps.html",
            **context(gap_counts=counts, gap_rows=rows, gap_roots=report["roots"],
                      gap_databases=report["databases"],
                      gap_truncated=max(0, counts["missing_from_collection"] - len(rows)),
                      gap_denominator=report["denominator_source"],
                      gap_generated_at=report["generated_at"]),
        )

    @app.get("/session")
    def session_page():
        source = request.args.get("source", "")
        rel = request.args.get("path", "")
        files = unitmatch.unit_digest_union(current())["files"]
        match = next(
            (item for item in files if item.get("source") == source and item.get("path") == rel),
            None,
        )
        if match is None:
            return back("evidence_page", "找不到这个会话：%s / %s" % (source, rel))
        return render_template("session.html", **context(session=match))

    @app.get("/workspaces")
    def workspaces_page():
        return redirect(url_for("units_page"), code=302)  # merged into the Unit page

    @app.post("/workspaces/add")
    def workspaces_add():
        """Register a folder to observe -- one axis only, no theme involved (裁定 4).

        Deliberately neither writes the scope (`?ws=`) nor injects anything: a themeless
        workspace is a valid state, while a workspace is what Groma actually needs.
        """
        raw = (request.form.get("path") or "").strip()
        origin = request.form.get("from") or "discover_page"
        target = Path(raw).expanduser()
        if not raw or not target.is_dir():
            return back(origin, "目录不存在：%s" % (raw or "（空）"))
        known = any(normalise_path(item["path"]) == normalise_path(target) for item in list_workspaces())
        register_workspace(target, target.name)
        if known:
            return back(origin, notice="它已经登记过了：%s" % target.name)
        return back(origin,
                    notice="已添加工作区：%s（只登记到本机，没挂协作主题；要挂协作主题去协作主题页）" % target.name)

    @app.get("/workspaces/remove")
    def workspace_remove_page():
        """确认页：会删什么，逐件列出来（裁定 2）。

        三份数据各只有一个来源：证据统计来自 `_evidence_stats()`、要摘的块面来自
        `surface.read_manifest()` 现读、要退役的主题来自 `state.orphan_units_if_removed()`
        ——与 `remove_workspace()` 同一套判据。页面不自己另算，免得说一套做一套。
        """
        from lab import surface as surface_mod

        raw = (request.args.get("path") or "").strip()
        target = Path(raw).expanduser()
        if not raw or not target.is_dir():
            return back("units_page", "目录不存在：%s" % (raw or "（空）"))
        card = link_path(target)
        return render_template(
            "workspace_remove.html",
            **context(
                target=target.as_posix(), target_name=target.name, stats=_evidence_stats(target),
                blocks=[item.get("path") for item in surface_mod.read_manifest(target).get("surfaces", [])],
                card_path=card.as_posix(), has_card=card.is_file(),
                orphans=orphan_units_if_removed(target),
            ),
        )

    @app.post("/workspaces/remove")
    def workspace_remove():
        """一次删干净 Groma 在这个区留下的全部关联（裁定 1/8）——项目文件一律不动。"""
        raw = (request.form.get("path") or "").strip()
        target = Path(raw).expanduser()
        # 红线 2：破坏性动作只从确认页来。顺序写死：确认门 → 启动目录 → 最后一个区 → 目录不存在。
        if (request.form.get("confirm") or "") != "1":
            return back("units_page", "移除要经过确认页：先看清会删什么，再提交")
        if not raw or not target.is_dir():
            return back("units_page", "目录不存在：%s" % (raw or "（空）"))
        unit_id = current_unit()
        ref = workspace_ref(target)
        was_home = unitmatch.resolve_unit(unit_id).get("home_binding") == ref
        # 两道护栏（启动目录 / 最后一个）**下沉到 `forget_workspace()`**，
        # 好让 `groma workspace remove` 自动继承——路由不再各写一遍。
        try:
            result = forget_workspace(target, boot_root)
        except LabError as exc:
            return back("units_page", str(exc))
        if was_home:
            remaining = [path for path in unitmatch.workspaces_for_unit(unit_id)
                         if Path(path).is_dir()]
            if remaining:
                unitmatch.set_home(unit_id, Path(remaining[0]))
                focus(unit_id)
            else:
                # 这个主题可能刚被退役（它只剩这一个区）⇒ 换到一个还活着的主题上，
                # 不能把 active 指到一个已经不在 units.json 里的 id。
                fallback = unitmatch.unit_list()
                if fallback:
                    focus(fallback[0]["unit_id"])
        bits = ["已移除工作区：%s" % target.name]
        if result["faces_removed"]:
            bits.append("摘掉 %d 处注入块" % result["faces_removed"])
        if result["card_removed"]:
            bits.append("身份卡已删")
        if result["retired_units"]:
            bits.append("协作主题已退役：%s" % "、".join(
                item.get("name") or item["unit_id"] for item in result["retired_units"]))
        return back("units_page", notice="；".join(bits))

    @app.get("/workspaces/forget/confirm")
    def workspace_forget_page():
        """死登记行的出口。

        「移除工作区」要求目录存在，而这类行的目录早就没了 ⇒ 界面上原本三条通道都够不着它。
        这一步只删注册表里的一行，不碰任何文件。
        """
        raw = (request.args.get("path") or "").strip()
        target = Path(raw).expanduser()
        if not raw or target.is_dir():
            return back("units_page", "只有目录已经不存在的登记行才走这一步")
        rows = [item for item in list_workspaces() if item["path"] == target.as_posix()]
        if not rows:
            return back("units_page", "本机没有这条登记：%s" % (raw or "（空）"))
        return render_template("workspace_forget.html", **context(
            target=target.as_posix(), target_name=target.name,
            registered_at=rows[0].get("first_seen"),
            unit_name=(unitmatch.resolve_unit(rows[0]["unit_id"]).get("name")
                       if rows[0].get("unit_id") else None)))

    @app.post("/workspaces/forget")
    def workspace_forget():
        raw = (request.form.get("path") or "").strip()
        target = Path(raw).expanduser()
        if (request.form.get("confirm") or "") != "1":
            return back("units_page", "清登记要经过确认页：先看清会删什么，再提交")
        if not raw or target.is_dir():
            return back("units_page", "这一步只处理目录已经不存在的登记行；这个区还在，请用「移除工作区」")
        try:
            forget_workspace(target, boot_root)
        except LabError as exc:
            return back("units_page", str(exc))
        return back("units_page", notice="已清掉这条登记：%s（只删登记行，没碰任何文件）" % target.name)

    @app.get("/units")
    def units_page():
        blocks = []
        themes = unitmatch.themes()
        from lab.classify_run import STATE_TEXT, last_runs, run_summary

        runs = last_runs(500)
        registry = list_workspaces()
        # 候选表那一格要报名字而不是短码，名字就地从这份取数里拿（0 新取数）。
        # 没通过谓词的 Unit 记录不进这张表 ⇒ 模板那边落到「没有协作主题」那一档，
        # 不许在模板里重算一遍判断。
        unit_names = {}
        for item in themes:
            record = topics.topic_record(item["unit_id"]) or {}
            unit_names[item["unit_id"]] = record.get("name") or item.get("name") or item["unit_id"]
        for unit in themes:
            home = unitmatch.home_path(unit["unit_id"])
            info, error, rows = None, None, []
            if home is not None:
                try:
                    info = unitmatch.detect(Path(home))
                    rows = info["rows"]
                except LabError as exc:
                    error = str(exc)
            topic = topics.resolve_topic(unit["unit_id"]) or topics.resolve_topic(unit.get("name") or "")
            chain = None
            if topic is not None:
                summary = run_summary(runs, topic.get("name"))
                reforged = topics.last_reforge(topic["topic_id"])
                own = set(topics.asset_ids_for(topic["topic_id"]))
                serving = [r["name"] for r in rows
                           if (injection_summary(unit["unit_id"]).get(r["path"]) or {}).get("asset_id") in own]
                chain = {"last_classify": summary["success_at"],
                         "last_success": summary["success_at"],
                         "last_attempt": summary["attempt_at"],
                         "last_attempt_state": summary["attempt_state"],
                         "last_attempt_text": STATE_TEXT.get(summary["attempt_state"], summary["attempt_state"]),
                         "last_reforge": (reforged or {}).get("at"),
                         "asset_id": (reforged or {}).get("asset_id"),
                         "evidence_count": len(topics.active_evidence(topic["topic_id"])),
                         "serving": serving}
            blocks.append({
                "unit": unit,
                "topic": topic,
                "chain": chain,
                "home": home,
                "unit_id": unit["unit_id"],
                "info": info,
                "detect_error": error,
                "rows": rows,
                "injection": injection_summary(unit["unit_id"]),
                "assets": list_assets(Path(home)) if home is not None else [],
                "others": other_workspaces(unit["unit_id"]),
                "lost": [item["path"] for item in registry
                         if item.get("unit_id") == unit["unit_id"] and item.get("path")
                         and not Path(item["path"]).is_dir()],
            })
        # Vault status: implemented by the vault session (2026-11-09), not the peer.
        from lab import vault as vault_mod
        try:
            vault_root = vault_mod.vault_root()
            vault_check = vault_mod.check_root(vault_root)
            try:
                vault_index = vault_mod.read_index(vault_root)
                vault_session_count = len(vault_index.get("sessions", []))
                # The index carries a top-level `shard_count` and no `shards` key at all
                # (lab/vault.py builds it as a sum over sessions). Reading `shards` here
                # silently yielded 0 while `groma vault status` reported hundreds.
                vault_shard_count = int(vault_index.get("shard_count", 0))
            except LabError:
                vault_session_count = 0
                vault_shard_count = 0
            vault_status = {
                "root": str(vault_root),
                "health": vault_check,
                "session_count": vault_session_count,
                "shard_count": vault_shard_count,
            }
        except Exception as exc:
            vault_status = {"error": str(exc)}
        # 新建卡改「依附工作区」下拉——只列已登记且目录存在的区；
        # 默认项 = 当前协作主题的家（归一化比较），家不在选项里就落占位项，绝不默认第一个区。
        home_options = [item for item in list_workspaces() if Path(item["path"]).is_dir()]
        current_home = unitmatch.home_path(current_unit())
        default_home = next(
            (item["path"] for item in home_options
             if current_home and normalise_path(item["path"]) == normalise_path(current_home)),
            None,
        )
        return render_template("units.html", **context(blocks=blocks, unit_names=unit_names,
                                                        vault=vault_status,
                                                        dead=dead_workspaces(),
                                                        home_options=home_options,
                                                        default_home=default_home))

    @app.get("/observe")
    def observe_page():
        """单机观测页：静态产物，只读磁盘。reindex 才会重建。"""
        from lab import vault as vault_mod
        import json

        html_path = _observe_html_path()
        receipt = _observe_reindex_load()
        wait = _reindex_wait_hint(receipt)
        # 在不在跑、是不是中断，只在这里判一次；模板不再自己算（R11）。
        running = _reindex_running(receipt)
        abandoned = _reindex_abandoned(receipt)
        if not html_path.is_file():
            return render_template("observe.html", **context(
                error="观测页尚未生成，请点击下方「重建索引」按钮",
                artifact=None, summary=None, reindex=receipt, reindex_wait=wait,
                running=running, abandoned=abandoned, reindex_window_min=_OBSERVE_REINDEX_ABANDONED_AFTER // 60,
            ))

        # 校验当前 index.generated_at 与产物是否一致；不一致则提示重建。
        try:
            vault_root = vault_mod.vault_root()
            index_path = vault_root / "index.json"
            if index_path.is_file():
                index = json.loads(index_path.read_text(encoding="utf-8"))
                baked_key = _observe_stale_key()
                current_key = (str(vault_root), index.get("generated_at"))
                if baked_key and baked_key != current_key:
                    return render_template("observe.html", **context(
                        error="Vault 索引已更新，观测页需要重建（点「重建索引」）",
                        artifact=None, summary=None, reindex=receipt, reindex_wait=wait,
                        running=running, abandoned=abandoned, reindex_window_min=_OBSERVE_REINDEX_ABANDONED_AFTER // 60,
                    ))
                summary = {
                    "sessions": int(index.get("session_count") or len(index.get("sessions", []))),
                    "shards": int(index.get("shard_count") or 0),
                    "gb": round(int(index.get("bytes_total") or 0) / 1073741824, 2),
                    "generated_at": index.get("generated_at") or "（索引没写生成时刻）",
                }
            else:
                summary = None
        except (OSError, ValueError):
            summary = None
        return render_template("observe.html", **context(
            error=None, artifact=url_for("observe_artifact"), summary=summary,
            reindex=receipt, reindex_wait=wait, running=running, abandoned=abandoned, reindex_window_min=_OBSERVE_REINDEX_ABANDONED_AFTER // 60,
        ))

    @app.get("/observe/artifact")
    def observe_artifact():
        """The baked observation page, served as the standalone document it is.

        `lab/observe_this_machine.py` writes a whole `<html>` with its own
        `<style>`. Nesting that inside a card leaked its rules into the shell
        and put two h1 on one page (K5) -- two口径 in one file.
        """
        html_path = _observe_html_path()
        if not html_path.is_file():
            return back("observe_page", "观测页还没有产物：先点「重建索引与观测页」")
        return Response(_with_observe_return_bar(html_path.read_text(encoding="utf-8")),
                        mimetype="text/html")

    @app.post("/observe/reindex")
    def observe_reindex():
        """重建 vault 索引 + 重新生成观测页（本机 1.8 GB 实测 52.4 秒）。

        K12 gives up the progress bar and keeps three provisable things:
        a refusal to start a second run, a receipt with timestamps, and a
        wait estimate shown before the click.
        """
        from lab import vault as vault_mod
        from lab.workspace import utc_stamp

        marker = _observe_reindex_load()
        if _reindex_running(marker):
            return back("observe_page", error="上一次重建仍在进行（始于 %s）。%s——"
                                              "再点一次不会并行跑，也不会覆盖上一次的结果。" % (
                local_text(marker.get("started_at")), _reindex_wait_hint(marker)))
        started_at = utc_stamp()
        clock = time.monotonic()
        _observe_reindex_save({"started_at": started_at, "finished_at": None,
                               "outcome": None, "seconds": None})
        try:
            vault_mod.reindex(vault_mod.vault_root())
            _html, err = _bake_observe_html()
        except Exception as exc:
            _observe_reindex_save({"started_at": started_at, "finished_at": utc_stamp(),
                                   "outcome": "error", "seconds": round(time.monotonic() - clock, 1),
                                   "detail": str(exc)})
            return back("observe_page", error="Reindex 失败：%s" % str(exc))
        seconds = round(time.monotonic() - clock, 1)
        if err:
            _observe_reindex_save({"started_at": started_at, "finished_at": utc_stamp(),
                                   "outcome": "error", "seconds": seconds, "detail": err})
            return back("observe_page", error="索引已重建，但观测页生成失败：%s" % err)
        _observe_reindex_save({"started_at": started_at, "finished_at": utc_stamp(),
                               "outcome": "ok", "seconds": seconds, "detail": None})
        return back("observe_page", notice="Vault 索引与观测页已重建：始于 %s，用时 %s 秒" % (local_text(started_at), seconds))

    @app.post("/unit/inject/apply")
    def unit_inject_apply():
        asset_id = (request.form.get("asset_id") or "").strip()
        raw = (request.form.get("path") or "").strip()
        if not asset_id:
            return back("units_page", "请选择要注入的资产")
        # 作用域由这一块正在操作的协作主题决定，不能靠"当前聚焦"推断，
        # 也不能交给落点目录自己的卡片——成员区自带一张卡时那是两个集合。
        scope = (request.form.get("unit") or "").strip() or target_unit()
        if (request.form.get("all") or "") == "1":
            targets = [Path(path) for path in unitmatch.workspaces_for_unit(scope)]
            targets = [path for path in targets if path.is_dir()]
        else:
            target = Path(raw).expanduser() if raw else current()
            if not target.is_dir():
                return back("units_page", "工作区不存在：%s" % raw)
            targets = [target]
        done = []
        for target in targets:
            try:
                inject_unit(target, asset_id, unit_id=scope)
            except LabError as exc:
                return back("units_page", "%s：%s" % (target.name, exc))
            done.append(target.name)
        return back(
            "units_page",
            notice="注入源已设为 %s，落到 %d 个工作区：%s" % (asset_id, len(done), "、".join(done)),
        )

    @app.post("/unit/inject/refresh")
    def unit_inject_refresh():
        raw = (request.form.get("path") or "").strip()
        if not raw or not Path(raw).expanduser().is_dir():
            return back("units_page", "工作区不存在：%s" % raw)
        outcome = refresh_payload(Path(raw).expanduser())
        verb = {"off": "这个工作区还没选注入源", "paused": "已停发（闸门判定过旧）",
                "stale": "已刷新，但标注为偏旧", "serving": "已刷新"}
        return back("units_page", notice="%s：%s" % (Path(raw).name, verb.get(outcome["state"], outcome["state"])))

    @app.post("/unit/inject/stop")
    def unit_inject_stop():
        raw = (request.form.get("path") or "").strip()
        if not raw or not Path(raw).expanduser().is_dir():
            return back("units_page", "工作区不存在：%s" % raw)
        uninject(Path(raw).expanduser())
        return back("units_page", notice="已停发并清除载荷：%s（hook 注册保留，需要时用 hook-remove 摘除）" % Path(raw).name)

    @app.post("/unit/inject/hook")
    def unit_inject_hook():
        raw = (request.form.get("path") or "").strip()
        target = Path(raw).expanduser()
        if not raw or not target.is_dir():
            return back("units_page", "工作区不存在：%s" % raw)
        removing = (request.form.get("off") or "") == "1"
        try:
            if removing:
                hooks.remove(target)
                notice = "已摘除 hook：%s（同一 IDE 进程可能仍会打一次 SessionStart，重启才算真停用）" % target.name
            else:
                if (request.form.get("confirm") or "") != "1":
                    return back("units_page", "首次注册需要确认：Groma 会写 .qoder/hooks/groma_inject.py 并注册到 .qoder/settings.local.json")
                hooks.install(target, confirmed=True)
                notice = "已注册 hook：%s（此后 Groma 只更新载荷，不再改动注册）" % target.name
        except LabError as exc:
            return back("units_page", str(exc))
        return back("units_page", notice=notice)

    @app.get("/unit/select")
    def unit_select():
        unit_id = (request.args.get("unit_id") or "").strip()
        # 作用域只能落在「人建过的协作主题」上：整机的 active 是行级动作的回落目标，
        # 指到一张自动折出来的身份卡上，等于把后面每个动作都接到一份没有主题的账里。
        if not any(item["unit_id"] == unit_id for item in unitmatch.themes()):
            return back("units_page", "没有这个协作主题：%s" % (unit_id or "（空）"))
        focus(unit_id)
        return redirect(url_for("units_page"), code=303)

    @app.post("/unit/create")
    def unit_create():
        name = (request.form.get("name") or "").strip()
        org = (request.form.get("org_path") or "").strip() or None
        raw = (request.form.get("home") or "").strip()
        if not name:
            return back("units_page", "协作主题名称不能为空")
        if not raw:
            return back("units_page", "请给新协作主题选一个家（本机工作区）")
        try:
            record = unitmatch.create_unit(name, Path(raw).expanduser(), org)
        except (LabError, ValueError) as exc:
            return back("units_page", str(exc))
        focus(record["unit_id"])
        adopt_home(Path(raw).expanduser())
        return back("units_page", notice="已新建协作主题：%s（家：%s，资产与设置以后写在这里）" % (
            record["name"], Path(raw).name))

    @app.post("/unit/home")
    def unit_home():
        raw = (request.form.get("path") or "").strip()
        if not raw:
            return back("units_page", "缺少路径")
        target = Path(raw).expanduser()
        try:
            # 这是人亲自指着这个区当家 ⇒ 记成人建的；内部那处换家（移除工作区之后）不带这个标记。
            unitmatch.set_home(target_unit(), target, by_human=True)
        except LabError as exc:
            return back("units_page", str(exc))
        focus(target_unit())
        adopt_home(target)
        return back("units_page", notice="已把家换到：%s（资产与设置以后写在这里）" % target.name)

    @app.post("/units/join")
    def units_join():
        raw = (request.form.get("path") or "").strip()
        role = (request.form.get("role") or "").strip() or "evidence"  # from the UI a join is always a human decision
        target = Path(raw).expanduser()
        origin = request.form.get("from") or "units_page"
        if not raw or not target.is_dir():
            return back(origin, "目录不存在：%s" % (raw or "（空）"))
        try:
            unitmatch.join_unit(target_unit(), target, role)
        except LabError as exc:
            return back(origin, str(exc))
        return back(origin, notice="已并入：%s（它自己的会话会被自动扫到）" % target.name)

    @app.post("/units/rename")
    def units_rename():
        name = (request.form.get("name") or "").strip()
        if not name:
            return back("units_page", "名称不能为空")
        try:
            unitmatch.rename_unit(target_unit(), name)
        except LabError as exc:
            return back("units_page", str(exc))
        return back("units_page", notice="协作主题已命名为：%s（对本协作主题的每个工作区生效）" % name)

    @app.post("/units/org")
    def units_org():
        org = (request.form.get("org_path") or "").strip()
        if not org:
            return back("units_page", "组织路径不能为空")
        try:
            unitmatch.set_org_path(target_unit(), org)
        except LabError as exc:
            return back("units_page", str(exc))
        return back("units_page", notice="组织路径已设为：%s" % org)

    @app.post("/units/intent")
    def units_intent():
        """The one-line definition a classifier needs. Empty clears it; past is untouched."""
        unit = (request.form.get("unit") or "").strip()
        intent = (request.form.get("intent") or "").strip()
        record = topics.resolve_topic(unit)
        if record is None and unit:
            record = topics.create_topic(unit)  # first save on this page creates the label row
        if record is None:
            return back("units_page", "协作主题不存在：%s" % (unit or "（空）"))
        try:
            topics.set_intent(record["topic_id"], intent, decided_by="human")
        except ValueError as exc:
            return back("units_page", str(exc))
        verb = "已写入说明" if intent else "已清空说明"
        return back("units_page", notice="%s：%s（只影响下一次归类，已入选的不变）" % (verb, record["name"]))

    @app.post("/units/leave")
    def units_leave():
        raw = (request.form.get("path") or "").strip()
        target = Path(raw).expanduser()
        if not raw:
            return back("units_page", "缺少路径")
        try:
            removed = unitmatch.leave_unit(target_unit(), target)
        except LabError as exc:
            return back("units_page", str(exc))
        if not removed:
            return back("units_page", notice="本就未并入：%s" % target.name)
        # 载荷还有没有来源，看这个区**还剩几个主题**——
        # 还剩 0 个 ⇒ 摘掉；还剩别的 ⇒ 一律不动（不误伤），并且要说清"载荷仍来自旧主题"。
        binding = unitmatch.binding_id_for(target)
        rest = [row for row in list_memberships() if row.get("binding_id") == binding]
        if not rest:
            uninject(target)
            return back("units_page", notice="已解除：%s；它不再属于任何协作主题，注入载荷已摘除" % target.name)
        names = {item["unit_id"]: (item.get("name") or item["unit_id"]) for item in unitmatch.unit_list()}
        holders = "、".join(names.get(row["unit_id"], row["unit_id"]) for row in rest)
        return back("units_page", notice=(
            "已解除：%s；它还在 %s 里，载荷仍来自旧协作主题，要换去协作主题页刷新" % (target.name, holders)))

    @app.get("/units/delete/confirm")
    def unit_delete_confirm():
        """确认页：删的是主题，不是工作区——把"会摘什么 / 会留什么"都写在明面上（裁定 2）。"""
        from lab import surface as surface_mod

        unit_id = (request.args.get("unit") or "").strip()
        record = next((item for item in unitmatch.unit_list() if item["unit_id"] == unit_id), None)
        if record is None:
            return back("units_page", "没有这个协作主题：%s" % (unit_id or "（空）"))
        workspaces = []
        for path in unitmatch.workspaces_for_unit(unit_id):
            target = Path(path)
            workspaces.append({
                "name": target.name,
                "path": target.as_posix(),
                "blocks": [item.get("path") for item in surface_mod.read_manifest(target).get("surfaces", [])],
            })
        blocked = ""
        if record.get("home_binding") == unitmatch.binding_id_for(boot_root):
            blocked = "它是 Groma 启动目录的协作主题，删了下次启动会以新身份回来。"
        elif len(unitmatch.unit_list()) <= 1:
            blocked = "这是本机最后一个协作主题，至少留一个。"
        return render_template(
            "unit_delete.html",
            **context(unit_id=unit_id, unit_name=record.get("name") or unit_id,
                      org_path=record.get("org_path"), workspaces=workspaces, blocked=blocked),
        )

    @app.post("/units/delete")
    def unit_delete():
        """删主题：摘注入、删身份卡、清注册表指针、忘掉主题——工作区与项目文件全留（裁定 7）。"""
        if (request.form.get("confirm") or "") != "1":
            return back("units_page", "删除协作主题要经过确认页：先看清会留下什么，再提交")
        unit_id = (request.form.get("unit") or "").strip()
        was_focused = unit_id == current_unit()
        try:
            result = drop_unit(unit_id, boot_root)
        except LabError as exc:
            return back("units_page", str(exc))
        if was_focused:
            # 不许留一个指向已删主题的 active["unit"]——current_unit() 被全站用。
            fallback = unitmatch.themes()
            if fallback:
                focus(fallback[0]["unit_id"])
        bits = ["已删除协作主题：%s" % result["name"]]
        faces = sum(item["faces_removed"] for item in result["workspaces"])
        if faces:
            bits.append("摘掉 %d 处注入块" % faces)
        if result["cards_removed"]:
            bits.append("%d 张身份卡已删" % result["cards_removed"])
        bits.append("它的 %d 个工作区都还在（现在没有协作主题）" % len(result["workspaces"]))
        return back("units_page", notice="；".join(bits))

    @app.get("/units/retire/confirm")
    def unit_retire_confirm():
        """确认页：退的是"这是不是一条协作主题"，工作区、身份卡与那句说明都留下。"""
        unit_id = (request.args.get("unit") or "").strip()
        record = next((item for item in unitmatch.themes() if item["unit_id"] == unit_id), None)
        if record is None:
            return back("units_page", "没有这个协作主题：%s" % (unit_id or "（空）"))
        return render_template(
            "unit_retire.html",
            **context(unit_id=unit_id, unit_name=record.get("name") or unit_id,
                      org_path=record.get("org_path"),
                      workspaces=unitmatch.workspaces_for_unit(unit_id),
                      intent=str((topics.topic_record(unit_id) or {}).get("intent") or "").strip(),
                      last_of_it=len(unitmatch.themes()) <= 1),
        )

    @app.post("/units/retire")
    def unit_retire():
        """退成纯工作区：把这条记录标成机器折的身份登记，其余一概不动。"""
        if (request.form.get("confirm") or "") != "1":
            return back("units_page", "退成纯工作区要经过确认页：先看清留下什么，再提交")
        unit_id = (request.form.get("unit") or "").strip()
        was_focused = unit_id == current_unit()
        try:
            record = unitmatch.retire_to_workspace(unit_id)
        except LabError as exc:
            return back("units_page", str(exc))
        if was_focused:
            fallback = unitmatch.themes()
            if fallback:
                focus(fallback[0]["unit_id"])
        return back("units_page", notice=(
            "已退成纯工作区：%s（它的 %d 个工作区、身份卡与那句说明都留着；"
            "夜里不会再为它跑归类，要重新认回来就在其中那个区上建一次主题）"
            % (record.get("name") or unit_id, len(record.get("workspaces") or [])
               or len(unitmatch.workspaces_for_unit(unit_id)))))

    @app.get("/assets")
    def assets_page():
        blocks = []
        for unit in unitmatch.themes():
            unit_id = unit["unit_id"]
            home = unitmatch.home_path(unit_id)
            items = []
            if home:
                try:
                    items = list_assets(Path(home))
                except LabError:
                    items = []
            seen, unique = set(), []
            for item in items:
                if item["asset_id"] in seen:
                    continue
                seen.add(item["asset_id"])
                unique.append(item)
            record = topics.topic_record(unit_id) or {}
            blocks.append({
                "topic": record.get("name") or unit.get("name") or unit_id,
                "topic_id": unit_id,
                "home": Path(home).as_posix() if home else "",
                "members": len(unit["workspaces"]),
                "assets": unique,
            })
        files = unitmatch.unit_digest_union(current())["files"]
        sessions = sorted(files, key=lambda item: item.get("last_ts") or "", reverse=True)
        home = unitmatch.home_path(current_unit())
        return render_template(
            "assets.html",
            **context(
                asset_blocks=blocks,
                sessions=sessions,
                unit_workspaces=unitmatch.unit_workspaces(current()),
                home_path=Path(home).as_posix() if home else "",
            ),
        )

    @app.post("/assets/build")
    def assets_build():
        title = (request.form.get("title") or "").strip()
        note = (request.form.get("note") or "").strip()
        selection = []
        for raw in request.form.getlist("session"):
            source, _, path = raw.partition(":")
            if source and path:
                selection.append((source, path))
        try:
            # 落点恒为本主题的家（一个 Unit 一个家），表单里的 path 不再参与。
            home = unitmatch.home_path(target_unit())
            if not home:
                return back("assets_page", "这个协作主题还没有家目录，先在主题页指定")
            allowed = {(item["source"], item["path"])
                       for record in unitmatch.digest_records_for(
                           [str(path) for path in unitmatch.unit_workspaces(home)])
                       for item in record.get("files") or []}
            missing = [key for key in selection if key not in allowed]
            if missing:
                return back("assets_page", "有 %d 条会话不在这个协作主题的证据里（可能已不在本机采集范围内）"
                            % len(missing))
            build_asset(Path(home), title, selection, note)
        except LabError as exc:
            return back("assets_page", str(exc))
        return back("assets_page", notice="已生成草稿《%s》，写在 %s。在资产列表可以看到。" % (
            title or "（未命名）", Path(home).as_posix()))

    @app.post("/topic/reforge")
    def topic_reforge():
        """主题级手动重铸：只调 topics.reforge，不另造第二条实现。"""
        landing = "assets_page" if (request.form.get("from") or "") == "assets" else "units_page"
        record = topics.resolve_topic(target_unit())
        if record is None:
            return back(landing, "没有这个协作主题：%s" % (target_unit() or "（空）"))
        home = unitmatch.home_path(record["topic_id"])
        if not home:
            return back(landing, "这个协作主题还没有家目录，先在主题页指定")
        served = topics.last_reforge(record["topic_id"])
        served_keys = {(str(item[0]), str(item[1])) for item in (served or {}).get("selection") or []}
        active_keys = set(topics.asset_selection(record["topic_id"]))
        if served is not None and served_keys == active_keys:
            return back(landing, notice="证据集与上次重铸一致，没有新建（护栏：没变不重铸）")
        try:
            built = topics.reforge(Path(home), record["topic_id"])
        except (LabError, ValueError) as exc:
            return back(landing, str(exc))
        skipped = int(built.get("skipped_stale") or 0)
        if built.get("asset_id") is None:
            return back(landing, notice="重铸没有新建：%s（跳过 %d 条）" % (
                built.get("reason") or "没有可用的证据", skipped))
        return back(landing, notice="已重铸《%s》，依据 %d 个会话%s" % (
            built["title"], built["sessions"],
            "（跳过 %d 条已不在采集范围）" % skipped if skipped else ""))

    @app.post("/assets/anchor")
    def assets_anchor():
        asset_id = (request.form.get("asset_id") or "").strip()
        raw = (request.form.get("path") or "").strip()
        root = Path(raw).expanduser() if raw else current()
        claim = {
            "title": (request.form.get("title") or "").strip(),
            "session_key": (request.form.get("session_key") or "").strip(),
            "from_ts": (request.form.get("from_ts") or "").strip(),
            "to_ts": (request.form.get("to_ts") or "").strip(),
        }
        try:
            result = attach_claims(root, asset_id, [claim])
        except LabError as exc:
            return back("assets_page", str(exc))
        return back("assets_page", notice="已为《%s》落锚 1 条引文（共 %d 条）；整条拒绝的核对在「验证状态」里。"
                    % (asset_id, result["claims"]))

    @app.post("/assets/verify")
    def assets_verify():
        asset_id = (request.form.get("asset_id") or "").strip()
        raw = (request.form.get("path") or "").strip()
        root = Path(raw).expanduser() if raw else current()
        try:
            result = verify_claims(root, asset_id)
        except LabError as exc:
            return back("assets_page", str(exc))
        if result["stale"]:
            return back("assets_page", "核对完成：%d 条有效，%d 条降级 stale（已记入 VALIDATION.md，导出不再渲染）"
                        % (result["verified"], len(result["stale"])))
        return back("assets_page", notice="核对完成：%d 条引文全部有效。" % result["verified"])

    @app.post("/assets/export")
    def assets_export():
        asset_id = (request.form.get("asset_id") or "").strip()
        raw = (request.form.get("path") or "").strip()
        root = Path(raw).expanduser() if raw else current()
        try:
            result = export_context(root, asset_id)
        except LabError as exc:
            return back("assets_page", str(exc))
        return back(
            "assets_page",
            notice="已导出 %s（sha256 %s）" % (result["context"], result["sha256"][:16]),
        )

    @app.post("/settings")
    def settings_save():
        # One handler, two independent requests (间隔 / 归类三项 / 自动化开关).
        # A field is only touched when the browser actually sent its name --
        # "value present" is not the same test as "value truthy", and the old
        # unconditional int() turned the classify form into a silent no-op (K1).
        # The classify checkbox pair in 刀 8 rides the same rule via a sentinel.
        classify_fields = ("classify_model", "classify_provider", "classify_signature")
        payload: dict = {}
        if "interval" in request.form:
            raw = (request.form.get("interval") or "").strip()
            try:
                payload["collect_interval_minutes"] = int(raw)
            except ValueError:
                return back("index", "采集间隔要填 1–1440 的整数")
        for field in classify_fields:
            if field in request.form:
                # Empty means empty: lab/settings.py collapses "" to None, and
                # None is exactly the "自动探测 / 用默认" the placeholder promises.
                payload[field] = (request.form.get(field) or "").strip()
        # 刀 8 · 自动化开关：HTML 不勾选就不发这个键，所以只认哨兵。带哨兵 = 无条件赋值。
        if "auto_form" in request.form:
            for field in ("auto_reforge", "auto_inject"):
                payload[field] = request.form.get(field) == "on"
        if not payload:
            return back("index", notice="没有需要保存的改动")
        try:
            saved = save_settings(current(), payload)
        except (ValueError, LabError) as exc:
            return back("index", "设置保存失败：%s" % exc)
        parts = []
        if "collect_interval_minutes" in payload:
            parts.append("采集间隔已设为 %d 分钟（整机生效）" % saved["collect_interval_minutes"])
        if any(field in payload for field in classify_fields):
            parts.append("归类设置已保存：模型 %s · CLI %s · 身份首行 %s（下一次归类生效）" % (
                saved.get("classify_model") or "（默认）",
                saved.get("classify_provider") or "自动探测",
                saved.get("classify_signature") or "（默认）",
            ))
        if "auto_form" in request.form:
            parts.append("自动化已%s：自动重铸资产 %s · 自动投放工作区 %s" % (
                "打开" if (saved.get("auto_reforge") or saved.get("auto_inject")) else "关闭",
                "开" if saved.get("auto_reforge") else "关",
                "开" if saved.get("auto_inject") else "关",
            ))
        return back("index", notice="；".join(parts))

    # K11: destructive actions are two submissions, not one JavaScript dialog.
    # The confirm page is what a no-JS reader gets; the server gate below is
    # what actually enforces it. Pattern copied from /workspaces/remove.
    @app.get("/vault/purge/confirm")
    def vault_purge_confirm():
        from lab import vault as vault_mod
        try:
            index = vault_mod.read_index(vault_mod.vault_root())
        except LabError as exc:
            return back("units_page", "读不到 vault 索引，先重建再清空：%s" % exc)
        sessions = int(index.get("session_count") or len(index.get("sessions", [])))
        shards = int(index.get("shard_count") or 0)
        total = int(index.get("bytes_total") or 0)
        return render_template(
            "confirm.html",
            **context(
                title="确认清空 vault",
                lines=[("会话", "%d 个会话" % sessions),
                       ("分片", "%d 个分片" % shards),
                       ("占用", "%.2f GB（%d 字节）" % (total / 1073741824, total)),
                       ("位置", str(vault_mod.vault_root()))],
                warning="不可恢复：会话正文分片与索引一并删除，之后只能重新回捞。",
                reassurance="资产与工作区里的 .groma 元数据不受影响；这一步只动 vault 正文。",
                action=url_for("vault_purge"),
                fields={"confirm": "1"},
                confirm_label="确认清空 vault",
                cancel=url_for("units_page"),
                danger=True,
            ),
        )

    @app.get("/vault/move/confirm")
    def vault_move_confirm():
        from lab import vault as vault_mod
        to_path = (request.args.get("to") or "").strip()
        if not to_path:
            return back("units_page", "要先填新路径才能打开确认页")
        return render_template(
            "confirm.html",
            **context(
                title="确认移动 vault",
                lines=[("当前路径", str(vault_mod.vault_root())),
                       ("目标路径", to_path)],
                warning="",
                reassurance="移动只换位置：不删任何分片，中断了可以按同一目标路径重跑。",
                action=url_for("vault_move"),
                fields={"to": to_path, "confirm": "1"},
                confirm_label="确认移动",
                cancel=url_for("units_page"),
                danger=False,
            ),
        )

    @app.get("/sources/remove/confirm")
    def sources_remove_confirm():
        name = (request.args.get("name") or "").strip()
        ws = (request.args.get("ws") or "").strip()
        target = discover_target(request.args)
        return render_template(
            "confirm.html",
            **context(
                title="确认移除这个采集根",
                lines=[("采集根", name),
                       ("所在工作区", (target.name if target is not None else "（未登记）")),
                       ("工作区路径", ws or (target.as_posix() if target is not None else ""))],
                warning="",
                reassurance="只删这一条申报，不会删除任何会话文件、不会删除已采到的摘要；"
                            "重新申报就又能扫到原来的东西。",
                action=url_for("sources_remove"),
                fields={"name": name, "ws": ws},
                confirm_label="确认移除",
                cancel=url_for("discover_page"),
                danger=True,
            ),
        )

    # Vault move/purge: implemented by the vault session (2026-11-09), not the peer.
    @app.post("/vault/move")
    def vault_move():
        from lab import vault as vault_mod
        to_path = (request.form.get("to") or "").strip()
        if not to_path:
            return back("units_page", "新路径不能为空")
        if request.form.get("confirm") != "1":
            return back("units_page", "移动 vault 需要确认：请先打开确认页看清当前路径与目标路径")
        try:
            result = vault_mod.move_vault(to_path)
            if result.get("moved"):
                mark_first_run("vault")  # 搬完了就是同意过：首启卡第 1 步不必再问一遍
                return back("units_page", notice="vault 已移至 %s（%d 字节）" % (result["new_root"], result["bytes_moved"]))
            return back("units_page", notice="vault 未移动：%s" % result.get("reason", "未知"))
        except LabError as exc:
            return back("units_page", "vault 移动失败：%s" % exc)

    @app.post("/firstrun/vault")
    def firstrun_vault():
        """First-run step 1: agree on where the session bodies live. One button, two shapes.

        Equal to the current root is a non-destructive stamp. Anything else is only a
        hand-off -- the move stays behind `/vault/move/confirm`, because a card on the
        overview must never relocate gigabytes in a single submission.
        """
        from lab import vault as vault_mod

        wanted = (request.form.get("path") or "").strip()
        if not wanted:
            return back("index", "要先填一个路径才能确认")
        if normalise_path(wanted) == normalise_path(vault_mod.vault_root()):
            mark_first_run("vault")
            return back("index", notice="正文仓库就用这里：%s" % vault_mod.vault_root().as_posix())
        return redirect(url_for("vault_move_confirm", to=wanted), code=303)

    @app.post("/firstrun/workspace")
    def firstrun_workspace():
        """First-run step 2, second shape: Groma's own boot folder really is the project."""
        mark_first_run("workspace")
        return back("index", notice="好，就看着 %s。还要并别的项目，卡片随时在概览上方。" % boot_root.as_posix())

    @app.post("/vault/purge")
    def vault_purge():
        from lab import vault as vault_mod
        confirm = request.form.get("confirm") == "1"
        if not confirm:
            return back("units_page", "purge 需要确认")
        try:
            result = vault_mod.purge_vault(confirm=True)
            if result.get("purged"):
                return back("units_page", notice="vault 已清空（%d 文件，%d 字节）" % (result["files_deleted"], result["bytes_deleted"]))
            return back("units_page", notice="vault 未清空：%s" % result.get("reason", "未知"))
        except LabError as exc:
            return back("units_page", "vault 清空失败：%s" % exc)

    @app.post("/refresh")
    def refresh():
        try:
            result = catchup_all(force=True, written_by="web:refresh")
        except LabError as exc:
            return back("index", str(exc))
        at = (last_run(current()) or {}).get("at") or "还没有跑过"
        return back("index", notice="已刷新 %d 个工作区（写入 %d 个），最近写入 %s。漏采与未分类看顶部状态线。" % (
            result.get("workspaces", 0), result.get("collected", 0), at))

    @app.post("/pause")
    def pause_route():
        pause_machine()
        return back("index", notice="已暂停：采集器不再写新摘要。定时触发器仍在，恢复按钮就在上方。")

    @app.post("/resume")
    def resume_route():
        resume_machine()
        minutes = load_settings(current())["collect_interval_minutes"]
        return back("index", notice="已恢复：下一轮按 %d 分钟自动采集。" % minutes)

    @app.post("/discover/add")
    def discover_add():
        name = (request.form.get("name") or "").strip()
        path = (request.form.get("path") or "").strip()
        include = (request.form.get("include") or "").strip()
        harness = (request.form.get("harness") or "unknown").strip()
        target = discover_target(request.form)
        if target is None:
            return back("discover_page", "本机还没有登记任何工作区——先在上方「我的工作区」里添加一个")
        try:
            add_source(target, name, path, harness, [include] if include else [])
        except LabError as exc:
            return back("discover_page", str(exc))
        return back("discover_page", notice="已申报 %s 到 %s（%s）。下一次采集就会扫它。" % (
            name or "（未命名）", target.name, target.as_posix()))

    @app.post("/sources/remove")
    def sources_remove():
        name = (request.form.get("name") or "").strip()
        target = discover_target(request.form)
        if target is None:
            return back("discover_page", "本机还没有登记任何工作区——先在上方「我的工作区」里添加一个")
        try:
            remove_source(target, name)
        except LabError as exc:
            return back("discover_page", str(exc))
        return back("discover_page", notice="已移除申报 %s（%s）。会话文件一个都没动，已采到的摘要也还在。" % (
            name, target.name))

    @app.get("/about")
    def about_page():
        """关于页：身份、本机路径、安全边界、更新状态。只读取磁盘，零 JS。

        路径一律现读，不写未现读的常量；会话正文、凭据、设备标识一律不上这一页（§6.2 的"不得显示"）。
        """
        from lab import update
        from lab.brand import icon_path
        from lab.state import state_root
        from lab.vault import vault_root

        status = update.load()
        try:
            brand = icon_path().as_posix()
        except FileNotFoundError:
            brand = None
        install_dir = Path(sys.executable).parent if getattr(sys, "frozen", False) \
            else Path(__file__).resolve().parents[1]
        return render_template("about.html", **context(
            identity_name="Groma",
            paths={"workspace": current().as_posix(), "state": state_root().as_posix(),
                   "vault": vault_root().as_posix(), "install": install_dir.as_posix()},
            brand_icon=brand, release_repo=update.RELEASE_REPO,
            update_status=status, update_text=update.describe(status),
            update_checked_at=local_text((status or {}).get("checked_at")),
            has_status=bool(status),
        ))

    @app.post("/about/check-update")
    def about_check_update():
        """用户点一下才查一次 GitHub Release。

        这一条路由是**唯一的网络出口触发点之一**（另一个是托盘「检查更新」）。启动路径、
        `create_app()`、其它任何 GET 都不许调到它——由 `test_startup_and_create_app_do_not_call_update_network` 钉。
        """
        from lab import update

        result = update.check_latest()
        update.save(current(), result)
        return redirect(url_for("about_page", notice=update.describe(result)), code=303)

    @app.post("/about/open-release")
    def about_open_release():
        """只在已经查到 `release_url` 时开系统浏览器；没查过就不开（§8.6）。

        不用 GET 触发网络或开浏览器；跳转目标是上一次检查里 GitHub 自己给的那个地址，
        本程序不自己拼 URL 去访问它。
        """
        import webbrowser

        from lab import update

        status = update.load() or {}
        url = status.get("release_url")
        if not isinstance(url, str) or not url:
            return redirect(url_for("about_page",
                                    error="还没有可打开的发布页：先点「检查更新」"), code=303)
        webbrowser.open(url)
        return redirect(url_for("about_page", notice="已在系统浏览器打开：%s" % url), code=303)

    @app.get("/favicon.ico")
    def favicon():
        """品牌图标：文件在就发，不在就 404——不伪造空响应。"""
        from lab.brand import icon_path

        try:
            blob = icon_path().read_bytes()
        except (FileNotFoundError, OSError):
            return Response("品牌图标未接入", status=404, mimetype="text/plain; charset=utf-8")
        return Response(blob, mimetype="image/vnd.microsoft.icon")

    @app.get("/healthz")
    def healthz():
        return {"ok": True, "version": __version__, "paused": is_paused(current()), "workspace": current().as_posix()}

    return app
