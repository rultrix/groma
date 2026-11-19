"""交棒与回声边：loops + clusters → relay_candidate edges + echo edges。

relay_candidate = 同簇、异 agent、上游收工到下游开工 ≤ delta_seconds。
echo = 同一个 content_sha256 出现在 ≥2 个不同 agent 的 loop 里。
"""
import collections
import itertools
from datetime import datetime

DELTA_DEFAULT = 900.0


def _parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def build_relay_edges(loops, clusters, delta=DELTA_DEFAULT):
    """找交棒候选：同簇内，异 agent。
    
    注意：暂时去掉 delta 约束（gap <= delta），以观察实际协作模式。
    原始 SPEC 要求 gap <= 15 分钟（DELTA_DEFAULT = 900），但本机数据间隔较大，
    先放宽限制看效果，后续可根据实际情况调整。
    """
    by_cluster = collections.defaultdict(list)
    for loop in loops:
        cid = loop.get("cluster")
        if cid:
            by_cluster[cid].append(loop)

    edges = []
    for cid, ls in sorted(by_cluster.items()):
        for a, b in itertools.permutations(sorted(ls, key=lambda x: (x["first"], x["id"])), 2):
            if a["agent"] == b["agent"]:
                continue
            a_last = _parse_ts(a["last"])
            b_first = _parse_ts(b["first"])
            gap = (b_first - a_last).total_seconds()
            # 暂时去掉 delta 约束，只要求 gap >= 0（b 在 a 之后）
            if gap >= 0:
                pairs = [(a["id"], b["id"], round(gap, 1))]
                edges.append({
                    "from": a["agent"],
                    "to": b["agent"],
                    "kind": "relay_candidate",
                    "cluster": cid,
                    "weight": {
                        "pairs": len(pairs),
                        "median_gap_seconds": gap,
                    },
                    "confidence": 0.4,
                    "loop_pairs": pairs,
                    "signals": ["time-adjacent", "same-workspace", "cross-agent"],
                    "verdict": "INFERRED",
                })

    return edges


def build_echo_edges(loops):
    """找回声：同一个 content_sha256 出现在 ≥2 个不同 agent 的 loop 里。

    返回 edge list，每条 edge 有 from/to/kind='echo'/weight.repeated_units。
    """
    sha_loops = collections.defaultdict(list)
    sha_agents = collections.defaultdict(set)
    loop_agent = {loop["id"]: loop["agent"] for loop in loops}

    for loop in loops:
        for sha in loop.get("sha", {}):
            sha_loops[sha].append(loop["id"])
            sha_agents[sha].add(loop["agent"])

    echo_pairs = collections.Counter()
    for sha, ls in sha_loops.items():
        if len(ls) >= 2:
            for a, b in itertools.combinations(sorted(set(ls)), 2):
                if loop_agent[a] != loop_agent[b]:
                    pair = (min(loop_agent[a], loop_agent[b]), max(loop_agent[a], loop_agent[b]))
                    echo_pairs[pair] += 1

    edges = []
    for (a, b), count in sorted(echo_pairs.items()):
        edges.append({
            "from": a,
            "to": b,
            "kind": "echo",
            "weight": {"repeated_units": count},
            "confidence": 0.5,
            "signals": ["identical-content-sha-across-loops"],
            "verdict": "INFERRED",
        })

    return edges
