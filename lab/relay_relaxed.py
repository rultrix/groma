"""宽松版交棒检测：去掉 cluster 和 delta 限制，看所有可能的协作。"""
import collections
import itertools
from datetime import datetime


def _parse_ts(s):
    return datetime.fromisoformat(str(s).replace("Z", "+00:00"))


def build_relay_edges_relaxed(loops):
    """找所有可能的交棒：异 agent、时间相邻（不管 cluster 和 delta）。
    
    返回 edge list，按时间顺序检查所有 agent 对。
    """
    # 按时间排序所有 loops
    sorted_loops = sorted(loops, key=lambda x: x["first"])
    
    edges = []
    edge_map = collections.defaultdict(lambda: {"pairs": 0, "gaps": []})
    
    # 检查所有相邻的 loop 对
    for i in range(len(sorted_loops) - 1):
        a = sorted_loops[i]
        b = sorted_loops[i + 1]
        
        # 必须是不同 agent
        if a["agent"] == b["agent"]:
            continue
        
        a_last = _parse_ts(a["last"])
        b_first = _parse_ts(b["first"])
        gap = (b_first - a_last).total_seconds()
        
        # 只关心正的时间间隔（b 在 a 之后）
        if gap >= 0:
            key = (a["agent"], b["agent"])
            edge_map[key]["pairs"] += 1
            edge_map[key]["gaps"].append(gap)
    
    # 转换为 edge list
    for (from_agent, to_agent), data in sorted(edge_map.items()):
        gaps = sorted(data["gaps"])
        median_gap = gaps[len(gaps) // 2] if gaps else 0
        
        edges.append({
            "from": from_agent,
            "to": to_agent,
            "kind": "relay_candidate",
            "cluster": None,  # 不限制 cluster
            "weight": {
                "pairs": data["pairs"],
                "median_gap_seconds": median_gap,
                "min_gap_seconds": min(gaps) if gaps else 0,
                "max_gap_seconds": max(gaps) if gaps else 0,
            },
            "confidence": 0.3,  # 降低置信度，因为放宽了限制
            "signals": ["time-adjacent", "cross-agent", "relaxed-constraints"],
            "verdict": "INFERRED",
        })
    
    return edges
