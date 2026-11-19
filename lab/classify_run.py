"""One classification pass: candidate pack -> harness -> tokens -> evidence events.

The prompt is built here and nowhere else, because two properties have to hold
together: the classifier only ever sees tokens/titles/signals (never a path),
and its answer is only ever a list of tokens that `classify.resolve` must still
recognise. Anything else -- no provider, not logged in, a bad exit code, prose
instead of a JSON array -- ends with nothing written.

The harness is invoked in a shape that cannot feed back into our own collection:
`-p` for non-interactive, `--no-session-persistence` so the run leaves no Qoder
session behind (otherwise Groma would collect its own classifier tomorrow),
`--tools ""` because classification needs no file access, and the default
permission mode -- bypass is never used unattended.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import classify, provider, topics
from .state import state_root

DEFAULT_MODEL = "Qwen3.8-Flash"
PROMPT_LIMIT = 200
RUNS = "topic_classify_runs.jsonl"
STATE_TEXT = {
    "applied": "已应用",
    "failed": "命令行失败",
    "unparsable": "输出无法解析",
    "not_logged_in": "未登录",
}


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def prompt_for(topic_name: str, pack: dict, limit: int = PROMPT_LIMIT,
               intent: str | None = None) -> str:
    """What the classifier is allowed to see: ids, titles, signals. No paths."""
    lines = [
        "你在做主题归类，只输出一个 JSON 数组，元素必须是候选列表里出现过的 token 原样。",
        "主题：%s" % topic_name,
    ]
    if intent:
        lines.append("主题说明：%s" % intent)
    lines += [
        "规则：只能从候选里选；不要编造 token；没有合适的就输出 []；不要输出数组以外的任何字符。",
        "候选（token | 标题 | 信号）：",
    ]
    for item in (pack.get("items") or [])[:limit]:
        title = item.get("title") or ""
        signals = ", ".join(item.get("signals") or [])
        lines.append("%s | %s | %s" % (item.get("token"), title, signals))
    return "\n".join(lines)


def parse_tokens(text: str) -> list[str]:
    """Pull the token array out of whatever wrapper the model used. Never guesses."""
    body = (text or "").strip()
    if "```" in body:
        start = body.find("```")
        end = body.find("```", start + 3)
        if end == -1:
            raise ValueError("代码块没有闭合")
        body = body[start + 3:end]
        stripped = body.lstrip()
        if stripped.lower().startswith("json"):
            body = stripped[4:]
    left, right = body.find("["), body.rfind("]")
    if left == -1 or right == -1 or right < left:
        raise ValueError("没有找到 JSON 数组")
    data = json.loads(body[left:right + 1])
    if not isinstance(data, list):
        raise ValueError("不是数组")
    return [str(item).strip() for item in data if str(item).strip()]


def run_once(root: Path, topic: str, model: str | None = None, dry_run: bool = False,
             provider_path: str | None = None, limit: int = PROMPT_LIMIT,
             intent: str | None = None) -> dict:
    """One pass. `dry_run` stops right before the events are written."""
    root = Path(root)
    pack = classify.candidates(root)
    record = topics.resolve_topic(topic)
    if record is None:
        return {"state": "no_topic", "topic": topic, "written": 0}

    from .settings import load_settings

    settings = load_settings()
    model = model or settings.get("classify_model") or DEFAULT_MODEL
    provider_path = provider_path or settings.get("classify_provider")
    signature = settings.get("classify_signature")
    definition = (intent or record.get("intent") or "").strip()
    if not definition:
        return {"state": "no_intent", "topic": record["name"],
                "reason": "这个主题没有一句说明；自动归类不猜，先补 intent", "written": 0}

    located = provider.check(provider_path, signature) if provider_path else provider.check(None, signature)
    if located.get("state") != "ok":
        return {"state": "no_provider", "detail": located.get("state"), "written": 0}
    if not provider.login_state(located["path"]).get("logged_in"):
        if not dry_run:
            _record_attempt(record, pack, model, "not_logged_in")
        return {"state": "not_logged_in", "written": 0}

    prompt = prompt_for(record["name"], pack, limit, definition)
    argv = [located["path"], "-p", "--no-session-persistence", "--tools", "",
            "--permission-mode", "default", "-m", model, prompt]
    done = provider.run(argv)
    if done.returncode != 0:
        stderr_tail = (done.stderr or "").strip()[-300:]
        if not dry_run:
            _record_attempt(record, pack, model, "failed",
                            returncode=done.returncode, stderr_tail=stderr_tail)
        return {"state": "failed", "returncode": done.returncode,
                "stderr_tail": stderr_tail, "written": 0}
    try:
        tokens = parse_tokens(done.stdout)
    except ValueError as exc:
        stdout_head = (done.stdout or "").strip()[:200]
        if not dry_run:
            _record_attempt(record, pack, model, "unparsable",
                            reason=str(exc), stdout_head=stdout_head)
        return {"state": "unparsable", "reason": str(exc),
                "stdout_head": stdout_head, "written": 0}

    base = {"topic_id": record["topic_id"], "topic": record["name"], "model": model,
            "candidates": len(pack.get("items") or []), "chosen": tokens}
    if dry_run:
        return {**base, "state": "dry_run", "written": 0}

    applied = classify.apply(root, record["name"], tokens, decided_by="llm:qoderclicn@%s" % model)
    result = {**base, "state": "applied", "added": applied["added"],
              "refused": applied["refused_tokens"], "rejected": applied["rejected"],
              "written": applied["added"]}
    _record_run(result)
    return result


def _record_run(record: dict) -> None:
    path = state_root() / RUNS
    path.parent.mkdir(parents=True, exist_ok=True)
    entry = {**record, "at": _now()}
    with path.open("a", encoding="utf-8", newline="\n") as handle:
        handle.write(json.dumps(entry, ensure_ascii=False, sort_keys=True) + "\n")


def _record_attempt(record: dict, pack: dict, model: str, state: str, **extra) -> None:
    """失败也要入账（刀 W26a1）：账本里只有成功，就等于断链时没人能看见。"""
    _record_run({"topic_id": record["topic_id"], "topic": record["name"], "model": model,
                 "candidates": len(pack.get("items") or []), "state": state,
                 "written": 0, **extra})


def last_runs(limit: int = 5) -> list[dict]:
    path = state_root() / RUNS
    if not path.is_file():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows[-limit:]


def run_summary(rows: list[dict], topic_name: str) -> dict:
    """最后一次尝试与最后一次成功分开（刀 W26a1）。

    `applied` 是唯一算成功的状态；其余（failed / unparsable / not_logged_in）
    都只是"试过"。两者同值才说明链是通的。
    """
    mine = [row for row in rows if row.get("topic") == topic_name]
    success = next((row for row in reversed(mine) if row.get("state") == "applied"), None)
    attempt = mine[-1] if mine else None
    return {"attempt_at": (attempt or {}).get("at"),
            "attempt_state": (attempt or {}).get("state"),
            "success_at": (success or {}).get("at")}


def run_all(root: Path, model: str | None = None, dry_run: bool = False,
            provider_path: str | None = None, limit: int = PROMPT_LIMIT) -> dict:
    """Every topic that has a definition, one pass each. No definition, no call."""
    rows = []
    for record in topics.list_topics():
        if not str(record.get("intent") or "").strip():
            rows.append({"topic": record["name"], "state": "skipped_no_intent", "added": 0})
            continue
        result = run_once(root, record["name"], model=model, dry_run=dry_run,
                          provider_path=provider_path, limit=limit)
        rows.append({"topic": record["name"], "state": result["state"],
                     "added": result.get("added", 0),
                     "chosen": len(result.get("chosen") or [])})
    return {"model": model or DEFAULT_MODEL, "dry_run": dry_run, "topics": len(rows),
            "written": sum(row.get("added", 0) for row in rows), "results": rows}


def _active_keys(topic_id: str) -> set[tuple[str, str]]:
    return {(row["source"], row["path"]) for row in topics.active_evidence(topic_id)}


def _served_keys(topic_id: str) -> set[tuple[str, str]] | None:
    served = topics.last_reforge(topic_id)
    if served is None:
        return None
    return {(str(item[0]), str(item[1])) for item in served.get("selection") or []}


def maintain(root: Path, model: str | None = None, provider_path: str | None = None,
             limit: int = PROMPT_LIMIT) -> dict:
    """归类 → 需要时重铸 → （按设置）把注入源换到新资产。

    Re-forge only when the live evidence differs from what was last served, and
    re-point injection only when the current source is *this topic's own asset*:
    a chain that silently replaces somebody else's asset is not automation.
    """
    from .inject import inject_unit
    from .settings import load_settings

    settings = load_settings()
    classified = run_all(root, model=model, provider_path=provider_path, limit=limit)
    steps = []
    for record in topics.list_topics():
        topic_id = record["topic_id"]
        active = _active_keys(topic_id)
        served = _served_keys(topic_id)
        if not active:
            steps.append({"topic": record["name"], "state": "no_evidence"})
            continue
        if served == active:
            steps.append({"topic": record["name"], "state": "unchanged"})
            continue
        if not settings.get("auto_reforge", True):
            steps.append({"topic": record["name"], "state": "reforge_off",
                          "active": len(active), "served": len(served or ())})
            continue
        built = topics.reforge(root, topic_id)
        if built.get("asset_id") is None:
            steps.append({"topic": record["name"], "state": "stale_evidence",
                          "skipped_stale": built.get("skipped_stale", 0),
                          "reason": built.get("reason")})
            continue
        step = {"topic": record["name"], "state": "reforged", "asset_id": built["asset_id"],
                "sessions": built["sessions"], "skipped_stale": built.get("skipped_stale", 0)}
        if settings.get("auto_inject", True):
            from .state import list_workspaces
            from .workspace import meta_dir, read_json

            previous = set(topics.asset_ids_for(topic_id)) - {built["asset_id"]}
            moved = []
            for item in list_workspaces():
                path = Path(item["path"])
                manifest = meta_dir(path) / "injections" / "manifest.json"
                if not manifest.is_file():
                    continue
                current = (read_json(manifest).get("source") or {}).get("asset_id")
                if current in previous:
                    try:
                        inject_unit(path, built["asset_id"], unit_id=topic_id)
                        moved.append(path.name)
                    except Exception:
                        continue
            step["injected"] = moved
        steps.append(step)
    return {"classified": classified, "steps": steps}
