"""任务簇切分：events → loops → clusters。

loop = 同一个 agent 在同一个 workspace 里的连续工作段（由 observer_events 按时间断点预切）。
cluster = 同一个 workspace 里、时间间隔 ≤ gap_seconds 的一组 loop。
"""
import collections
import hashlib
from datetime import datetime, timedelta, timezone

GAP_DEFAULT = 1800.0


def _h12(*parts):
    return hashlib.sha256("|".join(str(p) for p in parts).encode()).hexdigest()[:12]


def _parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def build_loops(events):
    """把事件流聚合为 loop 摘要。

    输入：observer_events 发射的事件 dict list。
    输出：loop dict list，每个 loop 有 id/agent/ws/first/last/events/roles/sha/writes 等。
    """
    by_key = collections.defaultdict(list)
    for e in events:
        by_key[e["loop_key"]].append(e)

    loops = []
    for key, evts in by_key.items():
        evts.sort(key=lambda e: e["ts_utc"])
        first = _parse_ts(evts[0]["ts_utc"])
        last = _parse_ts(evts[-1]["ts_utc"])

        roles = collections.Counter()
        tools = collections.Counter()
        sha = collections.Counter()
        writes = 0

        for e in evts:
            roles[e.get("beat_role") or "unknown"] += 1
            tn = e.get("tool_name")
            if tn:
                tools[tn] += 1
                if tn.lower() in ("search_replace", "create_file", "write", "edit",
                                  "apply_patch", "bash"):
                    writes += 1
            cs = e.get("content_sha256")
            if cs:
                sha[cs] += 1

        agent = evts[0]["agent_key"]
        ws = evts[0]["workspace_key"]
        harness = evts[0]["harness_name"]

        loops.append({
            "id": "loop-" + _h12(key),
            "kind": "loop",
            "key": key,
            "agent": agent,
            "harness": harness,
            "ws": ws,
            "first": first.isoformat(),
            "last": last.isoformat(),
            "events": len(evts),
            "hours": sorted({e["ts_utc"][:13] for e in evts}),
            "roles": roles,
            "tools": tools,
            "sha": sha,
            "writes": writes,
            "span_s": (last - first).total_seconds(),
        })

    return sorted(loops, key=lambda x: (x["first"], x["id"]))


def build_clusters(loops, gap=GAP_DEFAULT):
    """按 workspace + 时间断点切任务簇。

    同一个 ws 内，相邻 loop 的间隔 > gap 即断簇。
    """
    by_ws = collections.defaultdict(list)
    for loop in loops:
        by_ws[loop["ws"]].append(loop)

    clusters = []
    for ws, ls in sorted(by_ws.items()):
        ls = sorted(ls, key=lambda x: (x["first"], x["id"]))
        cur, prev_last = [], None
        for loop in ls:
            loop_first = _parse_ts(loop["first"])
            if prev_last and (loop_first - prev_last).total_seconds() > gap:
                if cur:
                    clusters.append(_make_cluster(cur, ws, len(clusters)))
                cur = []
            cur.append(loop)
            prev_last = max(prev_last or loop_first, _parse_ts(loop["last"]))
        if cur:
            clusters.append(_make_cluster(cur, ws, len(clusters)))

    return clusters


def _make_cluster(loops, ws, index):
    agents = sorted({loop["agent"] for loop in loops})
    harnesses = sorted({loop["harness"] for loop in loops if loop["harness"]})
    first = loops[0]["first"]
    last = max(loop["last"] for loop in loops)
    cid = "tc-" + _h12(ws, first, len(loops), index)
    for loop in loops:
        loop["cluster"] = cid
    return {
        "id": cid,
        "kind": "task_cluster",
        "ws": ws,
        "first": first,
        "last": last,
        "loops": [loop["id"] for loop in loops],
        "agents": agents,
        "events": sum(loop["events"] for loop in loops),
        "writes": sum(loop["writes"] for loop in loops),
        "harnesses": harnesses,
    }
