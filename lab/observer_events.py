"""observer 事件发射器：从 vault + digest 发射 observer 需要的事件流。

设计选择（2026-11-09）：
  当前走方案 C——直接扫 vault 目录树，不经过 index.json。
  理由：observer 是单机分析模块，读速度优先；index.json 的完整性校验对分析场景非必需。

  未来若 observer 反复运行（如 web 服务刷新观测页），可改为方案 B：
    - 在 lab/vault.py 新增 iter_records(vault_root, session_key=None) → Iterator[dict]
    - 内部走 read_index()，但缓存 index 到内存，后续运行跳过元数据解析
    - observer_events.py 改为调 iter_records()，不再自己拼路径
    - 优点：守 C5 精神（消费者不遍历树）、布局变化自动适配、反复运行更快
    - 缺点：多一层抽象，首次运行比直接扫盘慢（要解析 index + segment）
"""
import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Iterator


def emit_events(
    vault_root: str | Path,
    digest_rows: list[dict],
    host_name: str,
    gap_seconds: float = 1800.0,
) -> list[dict]:
    """发射 observer 需要的事件 dict list。

    流程：
    1. 直接扫 vault_root 目录树（harness/session_dir/shard.ndjson）
    2. 建 digest 的 session_key → {cwd, ws_ref} 映射
    3. 逐分片读 NDJSON → 发射事件
    4. 同 session 内按 gap_seconds 切 loop（算 loop_key）
    """
    vault_root = Path(vault_root)
    if not vault_root.is_dir():
        return []

    digest_map = _build_digest_map(digest_rows)
    events = []

    # 方案 C：直接遍历目录树，不走 index.json
    for harness_dir in vault_root.iterdir():
        if not harness_dir.is_dir():
            continue
        harness = harness_dir.name

        for session_dir in harness_dir.iterdir():
            if not session_dir.is_dir():
                continue
            safe_dir_name = session_dir.name
            session_key = _recover_session_key(harness, safe_dir_name, session_dir)

            digest_info = digest_map.get(session_key, {})
            cwd = digest_info.get("cwd", "")
            ws_ref = digest_info.get("ws_ref", "")
            digest_agent_id = digest_info.get("agent_id", "")
            digest_agent_kind = digest_info.get("agent_kind", "")

            session_events = []
            for shard_path in sorted(session_dir.glob("s*.ndjson")):
                if not shard_path.is_file():
                    continue
                for record in _read_shard_events(shard_path):
                    event = _vault_record_to_event(
                        record, session_key, harness, cwd, ws_ref, host_name,
                        digest_agent_id, digest_agent_kind,
                    )
                    if event:
                        session_events.append(event)

            session_events.sort(key=lambda e: e["ts_utc"])
            _assign_loop_keys(session_events, session_key, gap_seconds)
            events.extend(session_events)

    return events


def _recover_session_key(harness: str, safe_dir: str, session_dir: Path) -> str:
    """从 safe_dir 反推 session_key。

    safe_dir 是 session_key 的归一化形式（冒号/斜杠转连字符，casefold）。
    反推不完美（连字符可能是原生的也可能是转义的），但对 observer 来说够用：
    我们只需要一个稳定的键来 join digest，digest 里记的是完整 session_key。

    如果 segment.json 存在，优先从那里读真实 session_key。
    """
    segment_path = session_dir / "segment.json"
    if segment_path.is_file():
        try:
            segment = json.loads(segment_path.read_text(encoding="utf-8"))
            sk = segment.get("session_key")
            if sk:
                return sk
        except (json.JSONDecodeError, OSError):
            pass

    # 降级：用 safe_dir 当键（不完美，但稳定）
    return "%s:%s" % (harness, safe_dir)


def _build_digest_map(digest_rows: list[dict]) -> dict[str, dict]:
    """session_key → {cwd, ws_ref, agent_id}"""
    result = {}
    for row in digest_rows:
        sk = row.get("session_key")
        if not sk:
            # 尝试从 path 构造 session_key（db/<uuid> 格式）
            path = row.get("path", "")
            harness = row.get("harness", "")
            if path.startswith("db/") and harness:
                sk = "%s:%s" % (harness, path[3:])
        if not sk:
            continue
        result[sk] = {
            "cwd": row.get("cwd", ""),
            "ws_ref": row.get("ws_ref", ""),
            "agent_id": row.get("agent_id", ""),
            "agent_kind": row.get("agent_kind", ""),
        }
    return result


def _read_shard_events(shard_path: Path) -> Iterator[dict]:
    """读一个 NDJSON 分片，逐行 yield vault record dict"""
    with shard_path.open("r", encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue


def _vault_record_to_event(
    record: dict,
    session_key: str,
    harness: str,
    cwd: str,
    ws_ref: str,
    host_name: str,
    digest_agent_id: str = "",
    digest_agent_kind: str = "",
) -> dict | None:
    """vault record → observer event dict。字段不全则返回 None"""
    ts_utc = record.get("ts_utc")
    # 优先使用 digest 的 agent_id（统一归属），vault 记录的 agent_id 作为后备
    agent_id = digest_agent_id or record.get("agent_id")
    if not ts_utc or not agent_id:
        return None

    role = record.get("role", "")
    rec_type = record.get("type", "")
    beat_role = _map_beat_role(role, rec_type)

    tool = record.get("tool") or {}
    tool_name = tool.get("name") if isinstance(tool, dict) else None

    content_sha256 = record.get("stored_sha256") or ""
    source = record.get("source") or {}
    if not content_sha256 and isinstance(source, dict):
        content_sha256 = source.get("source_sha256", "")

    return {
        "loop_key": "",
        "agent_key": agent_id,
        "agent_kind": digest_agent_kind,
        "workspace_key": ws_ref,
        "harness_name": harness,
        "ts_utc": ts_utc,
        "beat_role": beat_role,
        "tool_name": tool_name,
        "model_name": None,
        "tokens_total": 0,
        "content_sha256": content_sha256,
        "cwd": cwd,
        "host_name": host_name,
    }


def _map_beat_role(vault_role: str, vault_type: str) -> str:
    """vault 的 role/type → observer 的 beat_role"""
    if vault_type == "tool":
        return "tool"
    if vault_type == "reasoning":
        return "reasoning"
    if vault_role == "assistant":
        return "reply"
    if vault_role == "user":
        return "user"
    return "other"


def _assign_loop_keys(events: list[dict], session_key: str, gap_seconds: float):
    """同 session 内按时间断点切 loop，给每个事件赋 loop_key"""
    if not events:
        return

    gap = timedelta(seconds=gap_seconds)
    loop_id = 0
    prev_ts = _parse_ts(events[0]["ts_utc"])

    for i, event in enumerate(events):
        if i == 0:
            event["loop_key"] = "%s:loop-%d" % (session_key, loop_id)
            continue

        curr_ts = _parse_ts(event["ts_utc"])
        if curr_ts - prev_ts > gap:
            loop_id += 1

        event["loop_key"] = "%s:loop-%d" % (session_key, loop_id)
        prev_ts = curr_ts


def _parse_ts(ts_str: str) -> datetime:
    """解析 ISO8601 时间戳"""
    return datetime.fromisoformat(ts_str.replace("Z", "+00:00"))
