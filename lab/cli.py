"""Groma: the L1 command surface. Local only, no network, no credentials.

Exit codes are part of the contract:
  0  PASS (or command succeeded)
  2  usage / local IO problem (bad args, missing receipt, not initialised)
  3  FAIL: integrity broken (missing / size changed / sha256 changed / extra file)
  4  NOT_PROVEN: there was nothing to verify (empty receipt)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from . import __version__, schedule as schedule_mod
from . import classify, classify_run, hooks, provider, reconcile, topics
from . import trigger as trigger_mod
from . import unitlink, unitmatch
from .asset import attach_claims, build_asset, list_assets, migrate_singleton, verify_claims
from . import inject as inject_mod
from .inject import export_context, inject_status, inject_unit, refresh as refresh_payload, uninject
from .agents import sync_agents
from .collect import catchup, catchup_all
from .coverage import build_coverage
from .digest import build_digest, save_digest
from .receipt import build_receipt, save_receipt, verify_receipt
from .scan import build_scan, save_scan
from .settings import load_settings, save_settings
from .sources import add_source, load_sources, remove_source
from .state import find_unit, register_workspace, workspace_ref
from .summary import build_summary, render_text, save_summary
from .workspace import (
    LabError,
    attach_agent,
    ensure_unit,
    init_workspace,
    load_unit,
    meta_dir,
    root_path,
    utc_stamp,
)
# Vault CLI: implemented by the vault session (2026-11-09), not the peer.
from . import vault as vault_mod
from . import qoder_db

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_FAIL = 3
EXIT_NOT_PROVEN = 4

VERDICT_CODES = {"PASS": EXIT_OK, "FAIL": EXIT_FAIL, "NOT_PROVEN": EXIT_NOT_PROVEN}


def _err(message: str) -> None:
    # A windowed exe has no stderr either.
    if sys.stderr is not None:
        sys.stderr.write(message)


def emit(payload: dict) -> None:
    # A windowed exe has no stdout; printing must not crash the command.
    if sys.stdout is None:
        return
    # ensure_ascii keeps Windows consoles (cp936) from mangling non-ASCII values.
    sys.stdout.write(json.dumps(payload, ensure_ascii=True, indent=2, sort_keys=True) + "\n")


def _add_root(parser: argparse.ArgumentParser, default: object = ".") -> None:
    """--root is accepted before or after the subcommand; SUPPRESS keeps the outer value."""
    parser.add_argument(
        "--root",
        default=default,
        help="workspace root (default: current directory)",
    )


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="groma",
        description="Local runtime spike (L1). No network, no secrets.",
    )
    parser.add_argument("--version", action="version", version="groma %s" % __version__)
    _add_root(parser)
    sub = parser.add_subparsers(dest="command", required=True)

    init_cmd = sub.add_parser("init", help="create a local unit")
    _add_root(init_cmd, default=argparse.SUPPRESS)
    init_cmd.add_argument("--name")
    init_cmd.add_argument("--owner")

    attach_cmd = sub.add_parser("attach", help="record an agent on this unit")
    _add_root(attach_cmd, default=argparse.SUPPRESS)
    attach_cmd.add_argument("name", help="agent name")
    attach_cmd.add_argument("--harness", default="unknown", help="executor type")

    receipt_cmd = sub.add_parser("receipt", help="hash the unit's files into a receipt")
    _add_root(receipt_cmd, default=argparse.SUPPRESS)
    receipt_cmd.add_argument("--note", default="")
    receipt_cmd.add_argument(
        "--allow-empty",
        action="store_true",
        help="attest an empty unit on purpose (verify will still answer NOT_PROVEN)",
    )

    verify_cmd = sub.add_parser("verify", help="recompute and compare against a receipt")
    _add_root(verify_cmd, default=argparse.SUPPRESS)
    verify_cmd.add_argument("receipt", help="receipt path, relative to --root")

    sources_cmd = sub.add_parser("sources", help="declare read-only input roots")
    _add_root(sources_cmd, default=argparse.SUPPRESS)
    sources_sub = sources_cmd.add_subparsers(dest="sources_command", required=True)

    sources_add = sources_sub.add_parser("add", help="declare one input root")
    _add_root(sources_add, default=argparse.SUPPRESS)
    sources_add.add_argument("name", help="label for this root")
    sources_add.add_argument("--path", required=True, help="absolute path to the root")
    sources_add.add_argument("--harness", default="unknown", help="executor type")
    sources_add.add_argument(
        "--include",
        action="append",
        default=[],
        metavar="PREFIX",
        help="session sub-path inside this root (repeatable)",
    )

    sources_remove = sources_sub.add_parser("remove", help="undeclare a root")
    _add_root(sources_remove, default=argparse.SUPPRESS)
    sources_remove.add_argument("name")

    sources_list = sources_sub.add_parser("list", help="show declared input roots")
    _add_root(sources_list, default=argparse.SUPPRESS)

    scan_cmd = sub.add_parser("scan", help="metadata-only scan of declared roots")
    _add_root(scan_cmd, default=argparse.SUPPRESS)

    digest_cmd = sub.add_parser("digest", help="structural digest of session files (never stores content)")
    _add_root(digest_cmd, default=argparse.SUPPRESS)
    digest_cmd.add_argument(
        "--include",
        action="append",
        default=None,
        metavar="PREFIX",
        help="only files under this sub-path (repeatable); defaults to the declared includes",
    )

    summary_cmd = sub.add_parser("summary", help="one-page rollup of the newest digest")
    _add_root(summary_cmd, default=argparse.SUPPRESS)
    summary_cmd.add_argument("--days", type=int, default=7, help="window in days (default 7)")
    summary_cmd.add_argument("--json", action="store_true", help="print JSON instead of text")

    catchup_cmd = sub.add_parser("catchup", help="scan + digest once, rate-limited (for triggers)")
    _add_root(catchup_cmd, default=argparse.SUPPRESS)
    catchup_cmd.add_argument("--min-interval", type=int, default=30, help="skip if run this recently")
    catchup_cmd.add_argument("--force", action="store_true", help="ignore the interval")

    catchup_all_cmd = sub.add_parser("catchup-all", help="collect every registered workspace")
    _add_root(catchup_all_cmd, default=argparse.SUPPRESS)

    coverage_cmd = sub.add_parser("coverage", help="coverage over active hours, not wall clock")
    _add_root(coverage_cmd, default=argparse.SUPPRESS)
    coverage_cmd.add_argument("--days", type=int, default=7)

    trigger_cmd = sub.add_parser("trigger", help="logon trigger (user Startup folder)")
    _add_root(trigger_cmd, default=argparse.SUPPRESS)
    trigger_sub = trigger_cmd.add_subparsers(dest="trigger_command", required=True)
    for name, help_text in (("install", "add the Startup entry"), ("status", "show it"), ("remove", "delete it")):
        child = trigger_sub.add_parser(name, help=help_text)
        _add_root(child, default=argparse.SUPPRESS)

    web_cmd = sub.add_parser("web", help="serve the local UI (needs Flask)")
    _add_root(web_cmd, default=argparse.SUPPRESS)
    web_cmd.add_argument("--host", default="127.0.0.1")
    web_cmd.add_argument("--port", type=int, default=8787)

    shell_cmd = sub.add_parser("shell", help="desktop shell: window + tray")
    _add_root(shell_cmd, default=argparse.SUPPRESS)
    shell_cmd.add_argument("--port", type=int, default=8787)

    asset_cmd = sub.add_parser("asset", help="context-asset drafts")
    _add_root(asset_cmd, default=argparse.SUPPRESS)
    asset_sub = asset_cmd.add_subparsers(dest="asset_command", required=True)
    asset_build = asset_sub.add_parser("build", help="build/reproduce the single-slot asset from selected sessions")
    _add_root(asset_build, default=argparse.SUPPRESS)
    asset_build.add_argument("--title", required=True)
    asset_build.add_argument("--session", action="append", default=[], metavar="SOURCE:PATH")
    asset_build.add_argument("--note", default="")
    # 两模式——默认覆写单槽（同指纹提示跳过）、--check 预演不落盘。
    asset_build.add_argument("--check", action="store_true",
                             help="dry-run: compute the fingerprint and evidence summary, write nothing")
    asset_migrate = asset_sub.add_parser(
        "migrate-singleton",
        help="fold historical asset-* generations into the single slot, then delete them")
    _add_root(asset_migrate, default=argparse.SUPPRESS)
    asset_migrate.add_argument("--unit", required=True, help="topic/unit id whose home holds the assets")
    asset_migrate.add_argument("--yes", action="store_true",
                               help="apply the merge and deletions; without it only prints the plan")
    asset_list = asset_sub.add_parser("list", help="list drafts")
    _add_root(asset_list, default=argparse.SUPPRESS)
    asset_export = asset_sub.add_parser("export", help="export a CONTEXT.md")
    _add_root(asset_export, default=argparse.SUPPRESS)
    asset_export.add_argument("asset_id")
    asset_export.add_argument("--out", default=None, help="target .md path")
    asset_anchor = asset_sub.add_parser("anchor", help="attach a human-supplied claim (title + session + ts window)")
    _add_root(asset_anchor, default=argparse.SUPPRESS)
    asset_anchor.add_argument("asset_id")
    asset_anchor.add_argument("--title", required=True, help="结论句")
    asset_anchor.add_argument("--session-key", required=True)
    asset_anchor.add_argument("--from-ts", required=True)
    asset_anchor.add_argument("--to-ts", required=True)
    asset_verify = asset_sub.add_parser("verify", help="re-derive every claim against the vault now")
    _add_root(asset_verify, default=argparse.SUPPRESS)
    asset_verify.add_argument("asset_id")

    topic_cmd = sub.add_parser("topic", help="topics: candidate packs, selection events, re-forge")
    _add_root(topic_cmd, default=argparse.SUPPRESS)
    topic_sub = topic_cmd.add_subparsers(dest="topic_command", required=True)
    for name, help_text in (
        ("list", "list topics with live evidence counts"),
        ("coverage", "reconcile our collection against the vendor's own lists"),
    ):
        _add_root(topic_sub.add_parser(name, help=help_text), default=argparse.SUPPRESS)
    topic_create = topic_sub.add_parser("create", help="name a topic (a label; touches no session identity)")
    _add_root(topic_create, default=argparse.SUPPRESS)
    topic_create.add_argument("name")
    topic_create.add_argument("--intent", default=None, help="one line saying what this topic means")
    topic_candidates = topic_sub.add_parser("candidates", help="export a metadata-only candidate pack")
    _add_root(topic_candidates, default=argparse.SUPPRESS)
    topic_candidates.add_argument("--out", default=None, help="target .json path")
    topic_apply = topic_sub.add_parser("apply", help="record chosen tokens as selection events")
    _add_root(topic_apply, default=argparse.SUPPRESS)
    topic_apply.add_argument("--topic", required=True)
    topic_apply.add_argument("--from", dest="pack", required=True, help="candidate pack .json")
    topic_apply.add_argument("--token", action="append", default=[], help="chosen token (repeat)")
    topic_apply.add_argument("--decided-by", default="human")
    topic_undo = topic_sub.add_parser("undo", help="undo selection (an event, never a deletion)")
    _add_root(topic_undo, default=argparse.SUPPRESS)
    topic_undo.add_argument("--topic", required=True)
    topic_undo.add_argument("--from", dest="pack", required=True, help="candidate pack .json")
    topic_undo.add_argument("--token", action="append", default=[], required=True)
    topic_undo.add_argument("--decided-by", default="human")
    topic_undo.add_argument("--reason", default=None)
    topic_reforge = topic_sub.add_parser("reforge", help="rebuild the asset from live evidence only")
    _add_root(topic_reforge, default=argparse.SUPPRESS)
    topic_reforge.add_argument("--topic", required=True)
    topic_reforge.add_argument("--title", default=None)
    topic_reforge.add_argument("--note", default="")
    topic_intent = topic_sub.add_parser("intent", help="read or rewrite the one-line definition")
    _add_root(topic_intent, default=argparse.SUPPRESS)
    topic_intent.add_argument("--topic", required=True)
    topic_intent.add_argument("--set", dest="intent", default=None,
                              help="new definition; pass an empty string to clear it")
    topic_run = topic_sub.add_parser("classify-once", help="one classification pass through the harness")
    _add_root(topic_run, default=argparse.SUPPRESS)
    topic_run.add_argument("--topic", required=True)
    topic_run.add_argument("--model", default=classify_run.DEFAULT_MODEL)
    topic_run.add_argument("--dry-run", action="store_true", help="show what it would pick; write nothing")
    topic_run.add_argument("--provider", default=None, help="explicit harness path (default: probe)")
    topic_run.add_argument("--limit", type=int, default=classify_run.PROMPT_LIMIT)
    topic_maintain = topic_sub.add_parser("maintain", help="classify, re-forge when evidence moved, re-point injection")
    _add_root(topic_maintain, default=argparse.SUPPRESS)
    topic_maintain.add_argument("--model", default=None)
    topic_maintain.add_argument("--provider", default=None)
    topic_maintain.add_argument("--limit", type=int, default=classify_run.PROMPT_LIMIT)
    topic_run_all = topic_sub.add_parser("classify-all", help="one pass for every topic that has a definition")
    _add_root(topic_run_all, default=argparse.SUPPRESS)
    topic_run_all.add_argument("--model", default=classify_run.DEFAULT_MODEL)
    topic_run_all.add_argument("--dry-run", action="store_true")
    topic_run_all.add_argument("--provider", default=None)
    topic_run_all.add_argument("--limit", type=int, default=classify_run.PROMPT_LIMIT)
    topic_run.add_argument("--intent", default=None, help="one line saying what this topic means")

    provider_cmd = sub.add_parser("provider", help="the harness groma shells out to (identity, not credentials)")
    _add_root(provider_cmd, default=argparse.SUPPRESS)
    provider_sub = provider_cmd.add_subparsers(dest="provider_command", required=True)
    provider_check = provider_sub.add_parser("check", help="locate it and prove which binary it is")
    _add_root(provider_check, default=argparse.SUPPRESS)
    provider_check.add_argument("--path", default=None, help="check this path instead of probing")
    for name, help_text in (
        ("models", "list the model names this account may pin with -m"),
        ("status", "login state read from the harness itself (no model call)"),
    ):
        sub_parser = provider_sub.add_parser(name, help=help_text)
        _add_root(sub_parser, default=argparse.SUPPRESS)
        sub_parser.add_argument("--path", default=None)

    workspace_cmd = sub.add_parser("workspace", help="工作区：登记一个要观测的目录，或把 Groma 的关联一次删干净")
    _add_root(workspace_cmd, default=argparse.SUPPRESS)
    workspace_sub = workspace_cmd.add_subparsers(dest="workspace_command", required=True)
    workspace_add = workspace_sub.add_parser("add", help="登记一个本地目录（只登记，不挂主题）")
    _add_root(workspace_add, default=argparse.SUPPRESS)
    workspace_add.add_argument("path", help="目录的本地绝对路径")
    workspace_add.add_argument("--allow-ephemeral", action="store_true",
                         help="允许登记系统临时目录下的路径（默认拒绝：那通常是脚本的沙箱）")
    workspace_remove = workspace_sub.add_parser(
        "remove", help="移除工作区：摘注入、删身份卡、删 .groma/（项目文件与 Harness 会话不动）")
    _add_root(workspace_remove, default=argparse.SUPPRESS)
    workspace_remove.add_argument("path", help="要移除的目录")
    workspace_remove.add_argument("--yes", action="store_true", help="真的执行；不带它只报告会删什么")

    unit_cmd = sub.add_parser("unit", help="Unit identity: local path <-> org path")
    _add_root(unit_cmd, default=argparse.SUPPRESS)
    unit_sub = unit_cmd.add_subparsers(dest="unit_command", required=True)
    unit_show = unit_sub.add_parser("show", help="show one Unit by id, org path, ref or local path")
    _add_root(unit_show, default=argparse.SUPPRESS)
    unit_show.add_argument("value", nargs="?", default=None)
    unit_link = unit_sub.add_parser("link", help="set the org path on this workspace")
    _add_root(unit_link, default=argparse.SUPPRESS)
    unit_link.add_argument("--org", required=True, help="host/owner/repo or a remote URL")
    unit_list = unit_sub.add_parser("list", help="list known Units")
    _add_root(unit_list, default=argparse.SUPPRESS)
    unit_detect = unit_sub.add_parser("detect", help="read-only: candidate workspaces that may belong to this Unit")
    _add_root(unit_detect, default=argparse.SUPPRESS)
    unit_detect.add_argument("--scan", action="append", default=[], metavar="PATH", help="extra dir to consider (repeatable)")
    unit_join = unit_sub.add_parser("join", help="给主题加一个工作区（只记关系，不移动/复制文件，也不自动投放资产）")
    _add_root(unit_join, default=argparse.SUPPRESS)
    unit_join.add_argument("--root-of", dest="root_of", default=None, help="a workspace already on this Unit (defaults to --root)")
    unit_join.add_argument("--unit", default=None, help="target unit_id (defaults to this workspace's Unit)")
    unit_join.add_argument("--path", required=True, help="workspace to join")
    unit_join.add_argument("--role", choices=["primary", "evidence"], default=None)
    unit_leave = unit_sub.add_parser("leave", help="unbind a workspace from a Unit (keeps its files)")
    _add_root(unit_leave, default=argparse.SUPPRESS)
    unit_leave.add_argument("--unit", default=None)
    unit_leave.add_argument("--path", required=True, help="workspace to unbind")
    unit_rename = unit_sub.add_parser("rename", help="give this Unit a human name")
    _add_root(unit_rename, default=argparse.SUPPRESS)
    unit_rename.add_argument("--name", required=True)
    unit_create = unit_sub.add_parser("create", help="stand up a new Unit around one folder")
    _add_root(unit_create, default=argparse.SUPPRESS)
    unit_create.add_argument("--name", required=True)
    unit_create.add_argument("--home", default=None, help="folder that becomes its home (defaults to --root)")
    unit_create.add_argument("--org", default=None, help="optional host/owner/repo")
    unit_delete = unit_sub.add_parser("delete", help="删除一个主题：工作区与它们的文件都留下，只忘主题")
    _add_root(unit_delete, default=argparse.SUPPRESS)
    unit_delete.add_argument("unit", help="主题的 unit_id、名字或 org path")
    unit_delete.add_argument("--yes", action="store_true", help="真的执行；不带它只报告会摘什么")
    unit_home = unit_sub.add_parser("home", help="move this Unit's home to another workspace")
    _add_root(unit_home, default=argparse.SUPPRESS)
    unit_home.add_argument("--path", required=True, help="workspace to make the home")
    unit_retire = unit_sub.add_parser(
        "retire", help="把一条协作主题退成纯工作区（留身份卡与说明，只不再当主题）")
    _add_root(unit_retire, default=argparse.SUPPRESS)
    unit_retire.add_argument("unit", help="主题的 unit_id、名字或 org path")

    inject_cmd = sub.add_parser("inject", help="fan the Unit's background out to harness faces")
    _add_root(inject_cmd, default=argparse.SUPPRESS)
    inject_sub = inject_cmd.add_subparsers(dest="inject_command", required=True)
    inject_apply = inject_sub.add_parser("apply", help="write the marked block into this workspace's faces")
    _add_root(inject_apply, default=argparse.SUPPRESS)
    inject_apply.add_argument("--asset", required=True, help="asset id to project")
    inject_apply.add_argument("--surface", action="append", default=[],
                              help="face name, repeatable (defaults to the harnesses seen here)")
    inject_apply.add_argument("--allow-project-file", action="store_true",
                              help="permit faces git tracks, such as CLAUDE.md / AGENTS.md")
    inject_status = inject_sub.add_parser("status", help="manifest vs disk: which faces drifted")
    _add_root(inject_status, default=argparse.SUPPRESS)
    inject_remove = inject_sub.add_parser("remove", help="take our blocks back out (restart the Host to drop hooks)")
    _add_root(inject_remove, default=argparse.SUPPRESS)
    hook_install = inject_sub.add_parser("hook-install", help="generate and register the Qoder injection hook")
    _add_root(hook_install, default=argparse.SUPPRESS)
    hook_install.add_argument("--confirm", action="store_true", help="explicit approval to edit .qoder/settings.local.json")
    inject_sub.add_parser("refresh", help="refresh this workspace's payload from the chosen source")
    inject_sub.add_parser("hook-status", help="script, registration and last receipts")
    inject_sub.add_parser("hook-remove", help="delete our script and unhook it (a Host restart still may be needed)")

    settings_cmd = sub.add_parser("settings", help="unit settings")
    _add_root(settings_cmd, default=argparse.SUPPRESS)
    settings_sub = settings_cmd.add_subparsers(dest="settings_command", required=True)
    settings_get = settings_sub.add_parser("get", help="show settings")
    _add_root(settings_get, default=argparse.SUPPRESS)
    settings_set = settings_sub.add_parser("set", help="change settings")
    settings_set.add_argument("--model", default=None, help="classifier model name (e.g. Qwen3.8-Flash)")
    settings_set.add_argument("--provider", default=None, help="absolute path to the harness executable")
    settings_set.add_argument("--signature", default=None, help="first-line identity the harness must print")
    _add_root(settings_set, default=argparse.SUPPRESS)
    settings_set.add_argument("--interval", type=int, required=True, help="collect every N minutes (1-1440)")

    schedule_cmd = sub.add_parser("schedule", help="run scan on an OS schedule")
    _add_root(schedule_cmd, default=argparse.SUPPRESS)
    schedule_sub = schedule_cmd.add_subparsers(dest="schedule_command", required=True)

    schedule_install = schedule_sub.add_parser("install", help="create the OS task")
    _add_root(schedule_install, default=argparse.SUPPRESS)
    schedule_install.add_argument("--period", type=int, default=60, help="minutes between runs")
    schedule_install.add_argument("--kind", choices=["scan", "classify"], default="scan")

    schedule_status = schedule_sub.add_parser("status", help="show the OS task")
    _add_root(schedule_status, default=argparse.SUPPRESS)
    schedule_status.add_argument("--kind", choices=["scan", "classify"], default=None)

    schedule_remove = schedule_sub.add_parser("remove", help="delete the OS task")
    _add_root(schedule_remove, default=argparse.SUPPRESS)
    schedule_remove.add_argument("--kind", choices=["scan", "classify"], default="scan")

    # Vault subcommand — context storage with immutable shards.
    # Implemented by the vault session (2026-11-09), not the peer.
    vault_cmd = sub.add_parser("vault", help="context vault: immutable shard storage")
    _add_root(vault_cmd, default=argparse.SUPPRESS)
    vault_sub = vault_cmd.add_subparsers(dest="vault_command", required=True)

    vault_init = vault_sub.add_parser("init", help="initialize the vault root")
    _add_root(vault_init, default=argparse.SUPPRESS)
    vault_init.add_argument("--vault-root", dest="vault_root_path", default=None,
                            help="absolute path for the vault root (default: per-user app data)")

    vault_status = vault_sub.add_parser("status", help="show vault config and health")
    _add_root(vault_status, default=argparse.SUPPRESS)

    vault_reindex = vault_sub.add_parser("reindex", help="rebuild index.json from shards")
    _add_root(vault_reindex, default=argparse.SUPPRESS)

    vault_backfill_file = vault_sub.add_parser("backfill-file",
                                                help="extract one session from a transcript file")
    _add_root(vault_backfill_file, default=argparse.SUPPRESS)
    vault_backfill_file.add_argument("--session", required=True, help="session key (harness:id)")
    vault_backfill_file.add_argument("--path", required=True, help="absolute path to transcript file")
    vault_backfill_file.add_argument("--harness", default="qoder-cn", help="harness name")
    vault_backfill_file.add_argument("--rel", default=None, help="relative path for source tracking")

    vault_backfill_db = vault_sub.add_parser("backfill-db",
                                              help="extract one session from the Qoder DB")
    _add_root(vault_backfill_db, default=argparse.SUPPRESS)
    vault_backfill_db.add_argument("--session", required=True, help="session key (harness:id)")
    vault_backfill_db.add_argument("--harness", default="qoder-cn", help="harness name")
    vault_backfill_db.add_argument("--db", default=None, help="path to local.db (default: probe)")

    vault_inspect = vault_sub.add_parser("inspect", help="show vault index or session details")
    _add_root(vault_inspect, default=argparse.SUPPRESS)
    vault_inspect.add_argument("--session", default=None, help="session key to inspect")

    vault_move = vault_sub.add_parser("move", help="move the vault root to a new location")
    _add_root(vault_move, default=argparse.SUPPRESS)
    vault_move.add_argument("--to", dest="to_path", required=True, help="new vault root path")

    vault_purge = vault_sub.add_parser("purge", help="delete all vault content (dangerous!)")
    _add_root(vault_purge, default=argparse.SUPPRESS)
    vault_purge.add_argument("--confirm", action="store_true", help="required to confirm the operation")

    # Observe subcommand — single-machine observation page.
    migrate_cmd = sub.add_parser(
        "migrate", help="move wb-era files to Groma names (preview by default)"
    )
    migrate_cmd.add_argument("--yes", action="store_true", help="apply the plan; without it nothing is written")

    observe_cmd = sub.add_parser("observe", help="generate single-machine observation page")
    _add_root(observe_cmd, default=argparse.SUPPRESS)
    observe_cmd.add_argument("--out", default=None, help="output HTML file path (default: print to stdout)")
    observe_cmd.add_argument("--host", default=None, help="host name to display (default: machine hostname)")

    return parser


def _resolve_receipt(root: Path, value: str) -> Path:
    candidate = Path(value)
    if not candidate.is_absolute():
        candidate = root / candidate
    if not candidate.is_file():
        raise LabError("receipt not found: %s" % value)
    return candidate


def _read_pack(value: str) -> dict:
    target = Path(value)
    if not target.is_file():
        raise LabError("候选包不存在：%s" % value)
    try:
        return json.loads(target.read_text(encoding="utf-8"))
    except ValueError as exc:
        raise LabError("候选包不是合法 JSON：%s（%s）" % (value, exc))


def _pack_tokens(pack: dict) -> list[str]:
    tokens = pack.get("tokens")
    if isinstance(tokens, list):
        return [str(item) for item in tokens]
    return [str(item["token"]) for item in pack.get("items") or [] if item.get("token")]


def _topic(root: Path, args: argparse.Namespace) -> dict:
    if args.topic_command == "list":
        records = unitmatch.digest_records_for(unitmatch.unit_workspaces(root))
        counts = topics.classification_counts(unitmatch.unit_files(records))
        rows = []
        for record in topics.list_topics():
            stale = topics.stale_after_undo(record["topic_id"])
            rows.append({
                "topic_id": record["topic_id"],
                "name": record["name"],
                "active": len(topics.active_evidence(record["topic_id"])),
                "undone_still_served": len(stale["dropped"]),
            })
        return {"topics": rows, "classification": counts}
    if args.topic_command == "create":
        return topics.create_topic(args.name, intent=args.intent)
    if args.topic_command == "intent":
        record = topics.resolve_topic(args.topic)
        if record is None:
            raise LabError("主题不存在：%s" % args.topic)
        if args.intent is None:
            return {"topic_id": record["topic_id"], "name": record["name"],
                    "intent": record.get("intent"),
                    "intent_by": record.get("intent_by"),
                    "intent_updated_at": record.get("intent_updated_at")}
        return topics.set_intent(args.topic, args.intent)
    if args.topic_command == "candidates":
        pack = classify.candidates(root)
        leaks = classify.assert_no_leak(pack)
        if leaks:
            raise LabError("候选包没有通过泄漏闸门：%s" % ", ".join(leaks))
        exported = {key: value for key, value in pack.items() if key != "_registry"}
        target = Path(args.out) if args.out else (
            meta_dir(root) / "topics" / ("candidates-%s.json" % utc_stamp())
        )
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps(exported, ensure_ascii=False, indent=2, sort_keys=True) + "\n",
                          encoding="utf-8", newline="\n")
        shown = target.relative_to(root).as_posix() if target.is_relative_to(root) else str(target)
        return {"pack": shown, "recall": pack["recall"], "leak_gate": "pass",
                "anchors_written": False}
    if args.topic_command == "apply":
        if not args.token:
            raise LabError("apply 需要至少一个 --token：整包入选不是本命令的默认，也不是任何人的默认")
        pack = _read_pack(args.pack)
        outside = [token for token in args.token if token not in set(_pack_tokens(pack))]
        if outside:
            raise LabError("这些 token 不在这个候选包里：%s" % ", ".join(outside[:3]))
        result = classify.apply(root, args.topic, args.token, decided_by=args.decided_by)
        result["pack_generated_at"] = pack.get("generated_at")
        return result
    if args.topic_command == "undo":
        live = classify.candidates(root)
        known, unknown = classify.resolve(live, args.token)
        if unknown:
            raise LabError("撤销拒绝部分执行：%s" % ", ".join(item["token"] for item in unknown))
        record = topics.resolve_topic(args.topic)
        if record is None:
            raise LabError("主题不存在：%s" % args.topic)
        keys = [(item["source"], item["path"]) for item in known]
        done = topics.undo(record["topic_id"], keys, decided_by=args.decided_by, reason=args.reason)
        stale = topics.stale_after_undo(record["topic_id"])
        return {"topic_id": record["topic_id"], "undone": done,
                "still_served_by_asset": stale.get("asset_id"), "dropped": len(stale["dropped"])}
    if args.topic_command == "reforge":
        built = topics.reforge(root, args.topic, title=args.title, note=args.note)
        return {"topic_id": built["topic_id"], "asset_id": built["asset_id"],
                "title": built["title"], "sessions": built["sessions"]}
    if args.topic_command == "coverage":
        return reconcile.build_reconciliation(root)
    if args.topic_command == "maintain":
        return classify_run.maintain(root, model=args.model, provider_path=args.provider, limit=args.limit)
    if args.topic_command == "classify-all":
        return classify_run.run_all(root, model=args.model, dry_run=args.dry_run,
                                    provider_path=args.provider, limit=args.limit)
    if args.topic_command == "classify-once":
        return classify_run.run_once(root, args.topic, model=args.model, dry_run=args.dry_run,
                                     provider_path=args.provider, limit=args.limit, intent=args.intent)
    raise LabError("未知的 topic 子命令：%s" % args.topic_command)


def _provider(args: argparse.Namespace) -> dict:
    if args.provider_command == "check":
        return provider.check(args.path)
    if args.provider_command == "models":
        return provider.models(args.path)
    if args.provider_command == "status":
        return provider.login_state(args.path)
    raise LabError("未知的 provider 子命令：%s" % args.provider_command)


# Vault CLI dispatch — implemented by the vault session (2026-11-09), not the peer.
def _vault(args: argparse.Namespace) -> dict:
    if args.vault_command == "init":
        target = Path(args.vault_root_path) if args.vault_root_path else vault_mod.default_root()
        return vault_mod.save_root(target)
    if args.vault_command == "status":
        root = vault_mod.vault_root()
        check = vault_mod.check_root(root)
        index = vault_mod.read_index(root)
        session_count = len(index.get("sessions", []))
        shard_count = sum(len(s.get("shards", [])) for s in index.get("sessions", []))
        return {
            "vault_root": str(root),
            "config": vault_mod.load_config(),
            "health": check,
            "session_count": session_count,
            "shard_count": shard_count,
        }
    if args.vault_command == "reindex":
        root = vault_mod.vault_root()
        return vault_mod.reindex(root)
    if args.vault_command == "backfill-file":
        root = vault_mod.vault_root()
        session_key = args.session
        if ":" in session_key:
            harness, _, sid = session_key.partition(":")
        else:
            harness, sid = args.harness, session_key
            session_key = "%s:%s" % (harness, sid)
        return vault_mod.extract_transcript(
            root, harness, session_key, args.path,
            rel=args.rel
        )
    if args.vault_command == "backfill-db":
        root = vault_mod.vault_root()
        session_key = args.session
        if ":" in session_key:
            harness, _, sid = session_key.partition(":")
        else:
            harness, sid = args.harness, session_key
            session_key = "%s:%s" % (harness, sid)
        db_path = Path(args.db) if args.db else qoder_db.locate_db(root)
        if not db_path:
            raise LabError("找不到 local.db，请用 --db 指定路径")
        rows = qoder_db.message_rows(db_path, sid)
        if not rows:
            return {"session_key": session_key, "rows_read": 0, "written": 0, "message": "无数据"}
        return vault_mod.extract_db_session(root, session_key, rows, harness=harness)
    if args.vault_command == "inspect":
        root = vault_mod.vault_root()
        index = vault_mod.read_index(root)
        sessions_list = index.get("sessions", [])
        if args.session:
            session = next((s for s in sessions_list if s.get("session_key") == args.session), None)
            if not session:
                raise LabError("会话不存在：%s" % args.session)
            return {"session_key": args.session, "details": session}
        return {
            "vault_root": str(root),
            "session_count": len(sessions_list),
            "shard_count": int(index.get("shard_count") or 0),  # 顶层键；索引里没有 shards 这个键
            "sessions": [s.get("session_key") for s in sessions_list[:50]],
        }
    if args.vault_command == "move":
        return vault_mod.move_vault(args.to_path)
    if args.vault_command == "purge":
        return vault_mod.purge_vault(confirm=args.confirm)
    raise LabError("未知的 vault 子命令：%s" % args.vault_command)


def _forget_preview(root: Path) -> dict:
    """What `workspace remove` would take out -- the CLI half of the two-step confirm."""
    from . import surface, state as state_mod

    book = surface.read_manifest(root)
    meta = meta_dir(root)
    size = 0
    if meta.is_dir():
        for item in meta.rglob("*"):
            if item.is_file():
                try:
                    size += item.stat().st_size
                except OSError:
                    pass
    card = root / "groma-unit.json"
    return {
        "would_remove": root.as_posix(),
        "blocks": [item.get("path") for item in book.get("surfaces", [])],
        "identity_card": card.as_posix() if card.is_file() else None,
        "retired_units": [item.get("name") or item["unit_id"]
                          for item in state_mod.orphan_units_if_removed(root)],
        "groma_mb": round(size / 1048576, 1),
        "hint": "nothing changed; add --yes to do it",
    }


def _drop_preview(unit_id: str) -> dict:
    """What `unit delete` would take out -- theme axis, same two-step confirm."""
    from . import surface, unitmatch as unitmatch_mod

    record = next((item for item in unitmatch_mod.unit_list() if item["unit_id"] == unit_id), {})
    paths = [Path(path) for path in unitmatch_mod.workspaces_for_unit(unit_id)]
    return {
        "would_delete": record.get("name") or unit_id,
        "unit_id": unit_id,
        "workspaces": [
            {"path": path.as_posix(),
             "blocks": [item.get("path") for item in surface.read_manifest(path).get("surfaces", [])]}
            for path in paths
        ],
        "hint": "nothing changed; add --yes to do it",
    }


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    try:
        root = root_path(args.root, create=args.command == "init")
        if args.command == "init":
            emit(init_workspace(root, args.name, args.owner))
        elif args.command == "attach":
            emit(attach_agent(root, args.name, args.harness))
        elif args.command == "receipt":
            receipt = build_receipt(root, args.note, allow_empty=args.allow_empty)
            saved = save_receipt(root, receipt)
            emit(
                {
                    "receipt": saved.relative_to(root).as_posix(),
                    "file_count": receipt["file_count"],
                    "git_head": receipt["git_head"],
                    "empty": receipt["file_count"] == 0,
                }
            )
        elif args.command == "verify":
            result = verify_receipt(root, _resolve_receipt(root, args.receipt))
            emit(result)
            return VERDICT_CODES.get(result["verdict"], EXIT_FAIL)
        elif args.command == "sources":
            if args.sources_command == "add":
                emit(add_source(root, args.name, args.path, args.harness, args.include))
            elif args.sources_command == "remove":
                emit(remove_source(root, args.name))
            else:
                emit({"sources": load_sources(root)})
        elif args.command == "scan":
            scan = build_scan(root)
            saved = save_scan(root, scan)
            emit(
                {
                    "scan": saved.relative_to(root).as_posix(),
                    "sources": len(scan["sources"]),
                    "file_count": scan["file_count"],
                    "total_bytes": scan["total_bytes"],
                    "skipped_count": scan["skipped_count"],
                }
            )
        elif args.command == "digest":
            digest = build_digest(root, args.include)
            saved = save_digest(root, digest)
            emit(
                {
                    "digest": saved.relative_to(root).as_posix(),
                    "content_stored": digest["content_stored"],
                    "workspace_root": digest["workspace_root"],
                    "include": digest["include"],
                    "file_count": digest["file_count"],
                    "agents": len(digest["agents"]),
                    "event_count": digest["event_count"],
                    "parse_errors": digest["parse_errors"],
                    "skipped_non_session": digest["skipped_non_session"],
                    "unattributed_outside": digest["unattributed_outside"],
                    "unattributed_no_cwd": digest["unattributed_no_cwd"],
                }
            )
        elif args.command == "catchup":
            emit(catchup(root, args.min_interval, args.force, written_by="cli:catchup"))
        elif args.command == "catchup-all":
            emit(catchup_all(written_by="cli:catchup-all"))
        elif args.command == "coverage":
            emit(build_coverage(root, args.days))
        elif args.command == "trigger":
            if args.trigger_command == "install":
                emit(trigger_mod.install(root))
            elif args.trigger_command == "status":
                emit(trigger_mod.status(root))
            else:
                emit(trigger_mod.remove(root))
        elif args.command == "asset":
            if args.asset_command == "build":
                selection = []
                for raw in args.session:
                    source, _, path = raw.partition(":")
                    if not source or not path:
                        raise LabError("--session must be source:path, got: %s" % raw)
                    selection.append((source, path))
                if args.check:
                    # 预演——算指纹+依据摘要，不落盘。
                    built = build_asset(root, args.title, selection, args.note, dry_run=True)
                    emit({"planned": built.get("planned"), "asset_id": built.get("asset_id"),
                          "title": built.get("title"), "sessions": built.get("sessions"),
                          "digest": built.get("digest"), "unchanged": built.get("unchanged", False),
                          "note": "预演：一个字节都没写"})
                else:
                    emit(build_asset(root, args.title, selection, args.note))
            elif args.asset_command == "migrate-singleton":
                # preferred 从账本来——reforges 最新一笔指向的资产代
                # （asset.py 不 import topics，参数注入）。
                from . import topics as topics_mod

                history = topics_mod.reforge_history(args.unit)
                preferred = (history[-1] or {}).get("asset_id") if history else None
                emit(migrate_singleton(root, args.unit, apply=args.yes,
                                       preferred_asset_id=preferred))
            elif args.asset_command == "export":
                target = Path(args.out) if args.out else None
                emit(export_context(root, args.asset_id, target))
            elif args.asset_command == "anchor":
                emit(attach_claims(root, args.asset_id, [{
                    "title": args.title, "session_key": args.session_key,
                    "from_ts": args.from_ts, "to_ts": args.to_ts,
                }]))
            elif args.asset_command == "verify":
                emit(verify_claims(root, args.asset_id))
            else:
                emit({"assets": list_assets(root)})
        elif args.command == "topic":
            emit(_topic(root, args))
        elif args.command == "provider":
            emit(_provider(args))
        elif args.command == "workspace":
            target = Path(args.path).expanduser()
            if args.workspace_command == "add":
                if not target.is_dir():
                    raise LabError("目录不存在：%s" % target)
                emit(register_workspace(target, target.name,
                                  allow_ephemeral=args.allow_ephemeral))
            elif args.workspace_command == "remove":
                if not args.yes:
                    emit(_forget_preview(target))
                    return EXIT_OK
                emit(inject_mod.forget_workspace(target, root))
            else:  # pragma: no cover - argparse rejects unknown subcommands
                raise LabError("unknown workspace command: %s" % args.workspace_command)
        elif args.command == "unit":
            if args.unit_command == "link":
                unit = load_unit(root)
                unitlink.ensure_link(root, unit)
                emit(unitmatch.set_org_path(unit["unit_id"], args.org))
            elif args.unit_command == "list":
                unitmatch.sync_units()
                emit({"units": unitmatch.unit_list()})
            elif args.unit_command == "create":
                home = Path(args.home) if args.home else root
                emit(unitmatch.create_unit(args.name, home, args.org))
            elif args.unit_command == "home":
                emit(unitmatch.set_home(load_unit(root)["unit_id"], Path(args.path).expanduser(),
                                        by_human=True))
            elif args.unit_command == "detect":
                emit(unitmatch.detect(root, args.scan))
            elif args.unit_command == "join":
                anchor = Path(args.root_of) if args.root_of else root
                emit(unitmatch.join(anchor, args.unit, args.path, args.role))
            elif args.unit_command == "leave":
                removed = unitmatch.leave(root, args.path, args.unit)
                emit({"left": removed, "path": str(Path(args.path).expanduser())})
            elif args.unit_command == "rename":
                # CLI 与 web 两条入口一致——双写 topics 标签（显示名源）+ 登记名；
                # topics 先行（空名/超长/撞名在这里先挡），登记名跟随失败则回退，不半写。
                _uid = load_unit(root)["unit_id"]
                _record = topics.resolve_topic(_uid)
                _old = (_record or {}).get("name")
                if _record is not None:
                    topics.rename_topic(_record["topic_id"], args.name)
                try:
                    _renamed = unitmatch.rename_unit(_uid, args.name)
                except LabError:
                    if _record is not None:
                        topics.rename_topic(_record["topic_id"], _old)
                    raise
                emit(_renamed)
            elif args.unit_command == "delete":
                spec = (args.unit or "").strip()
                match = [item for item in unitmatch.unit_list()
                         if spec in (item.get("unit_id"), item.get("name"), item.get("org_path"))]
                if not match:
                    raise LabError("没有这个主题：%s" % (spec or "（空）"))
                if not args.yes:
                    emit(_drop_preview(match[0]["unit_id"]))
                    return EXIT_OK
                emit(inject_mod.drop_unit(match[0]["unit_id"], root))
            elif args.unit_command == "retire":
                spec = (args.unit or "").strip()
                match = [item for item in unitmatch.themes()
                         if spec in (item.get("unit_id"), item.get("name"), item.get("org_path"))]
                if not match:
                    raise LabError("没有这个协作主题：%s" % (spec or "（空）"))
                retired = unitmatch.retire_to_workspace(match[0]["unit_id"])
                emit({"retired": retired["unit_id"], "name": retired.get("name"),
                      "kept": ["身份卡", "注册表行", "一句说明"],
                      "note": "它已退成纯工作区；界面上不再出现，夜里也不会再为它跑归类"})
            elif args.unit_command == "show" and args.value:
                records = find_unit(args.value)
                if not records:
                    raise LabError("unknown unit: %s" % args.value)
                emit(
                    {
                        "value": args.value,
                        "unit_id": records[0].get("unit_id"),
                        "org_path": next((item.get("org_path") for item in records if item.get("org_path")), None),
                        "local_paths": [item["path"] for item in records],
                    }
                )
            else:
                register_workspace(root, load_unit(root).get("name"))
                emit(unitmatch.describe(root))
        elif args.command == "inject":
            if args.inject_command == "apply":
                emit(
                    inject_unit(
                        root,
                        args.asset,
                        faces=args.surface or None,
                        allow_project_files=args.allow_project_file,
                    )
                )
            elif args.inject_command == "status":
                emit(inject_status(root))
            elif args.inject_command == "refresh":
                emit(refresh_payload(root))
            elif args.inject_command == "hook-install":
                emit(hooks.install(root, confirmed=args.confirm))
            elif args.inject_command == "hook-status":
                emit(hooks.status(root))
            elif args.inject_command == "hook-remove":
                emit(hooks.remove(root))
            else:
                emit(uninject(root))
        elif args.command == "settings":
            if args.settings_command == "get":
                emit(load_settings(root))
            else:
                payload = {"collect_interval_minutes": args.interval}
                if args.model is not None:
                    payload["classify_model"] = args.model
                if args.provider is not None:
                    payload["classify_provider"] = args.provider or None
                if args.signature is not None:
                    payload["classify_signature"] = args.signature or None
                emit(save_settings(root, payload))
        elif args.command == "shell":
            try:
                from groma_shell.app import run as run_shell
            except ImportError as exc:  # pragma: no cover - depends on the venv
                raise LabError(
                    "pywebview/pystray are required for `groma shell`; install them with: "
                    "pip install pywebview pystray pillow"
                ) from exc
            run_shell(root, args.port)
        elif args.command == "web":
            try:
                from web.app import create_app
            except ImportError as exc:  # pragma: no cover - depends on the venv
                raise LabError(
                    "Flask is required for `groma web`; install it in this repo's venv: pip install flask"
                ) from exc
            ensure_unit(root)
            sync_agents(root)
            started = catchup(root, written_by="cli:web-start")
            _err("groma: startup catchup -> %s\n" % json.dumps(started, ensure_ascii=False))
            try:
                # 接力回放只在启动段跑（不在任何 GET 页面路径里）；只入账不投递。
                from lab.handoff import replay_unit_handoffs
                replayed = replay_unit_handoffs(root)
                if replayed.get("created"):
                    _err("groma: handoff replay -> %s\n" % json.dumps(replayed, ensure_ascii=False))
            except Exception as exc:  # noqa: BLE001 - 回放失败不拦 web 启动
                _err("groma: handoff replay failed: %s\n" % exc)
            app = create_app(root)
            app.run(host=args.host, port=args.port, debug=False, use_reloader=False)
        elif args.command == "summary":
            summary = build_summary(root, args.days)
            saved = save_summary(root, summary)
            if args.json:
                payload = dict(summary)
                payload["summary"] = saved.relative_to(root).as_posix()
                emit(payload)
            else:
                sys.stdout.write(render_text(summary) + "\n")
                sys.stdout.write("saved: %s\n" % saved.relative_to(root).as_posix())
        elif args.command == "schedule":
            if args.schedule_command == "install":
                emit(schedule_mod.install(root, args.period, kind=args.kind))
            elif args.schedule_command == "status":
                if args.kind:
                    emit(schedule_mod.status(root, kind=args.kind))
                else:
                    emit({"scan": schedule_mod.status(root, kind="scan"),
                          "classify": schedule_mod.status(root, kind="classify")})
            else:
                emit(schedule_mod.remove(root, kind=args.kind))
        elif args.command == "vault":
            emit(_vault(args))
        elif args.command == "migrate":
            from .migrate import run as run_migration

            emit(run_migration(apply=args.yes))
        elif args.command == "observe":
            from . import vault as vault_mod
            from .observe_this_machine import digest_rows_from_index, generate_observation_page
            
            vault_root = vault_mod.vault_root()
            index_path = vault_root / "index.json"
            
            if not index_path.exists():
                raise LabError("Vault 索引不存在：%s\n请先运行 `groma vault reindex`" % index_path)
            
            index = json.loads(index_path.read_text(encoding="utf-8"))
            sessions = index.get("sessions", [])
            
            if not sessions:
                raise LabError("Vault 中没有会话数据\n请先运行 `groma vault backfill-db` 或 `groma vault backfill-file`")
            
            # 构建 digest rows（身份解析与 web 观测页共用一处）
            digest_rows = digest_rows_from_index(sessions, cwd=root.as_posix())
            
            host_name = args.host or "unknown"
            
            if args.out:
                output_path = Path(args.out)
                html_content = generate_observation_page(vault_root, digest_rows, host_name, output_path)
                emit({
                    "output": str(output_path),
                    "size": output_path.stat().st_size,
                    "sessions": len(sessions),
                    "events": sum(r["events"] for r in digest_rows),
                })
            else:
                html_content = generate_observation_page(vault_root, digest_rows, host_name)
                if html_content:
                    sys.stdout.write(html_content)
                else:
                    raise LabError("生成观测页失败")
        else:  # pragma: no cover - argparse rejects unknown commands
            raise LabError("unknown command: %s" % args.command)
    except LabError as exc:
        _err("groma: %s\n" % exc)
        return EXIT_USAGE
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        _err("groma: local failure: %s\n" % exc)
        return EXIT_USAGE
    return EXIT_OK
