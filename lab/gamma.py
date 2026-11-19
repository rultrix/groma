"""γ 传导率：产出被另一条线接走的比例。

分母 = 有 reply 的 loop 数（产出者）。
分子 = 作为交棒上游的 loop 数（被接走的产出）。
γ = 分子 / 分母。
"""


def compute_gamma(loops, relay_edges):
    """算 γ 传导率。

    输入：loops（build_loops 的输出）、relay_edges（build_relay_edges 的输出）。
    输出：dict 有 producers/consumed_proxy/gamma/verdict/note。
    """
    producers = {loop["id"] for loop in loops if loop.get("roles", {}).get("reply", 0) > 0}

    consumed = set()
    for edge in relay_edges:
        if edge["kind"] == "relay_candidate":
            for src_loop, _dst, _gap in edge.get("loop_pairs", []):
                if src_loop in producers:
                    consumed.add(src_loop)

    g = round(len(consumed) / len(producers), 3) if producers else None

    return {
        "producers": len(producers),
        "consumed_proxy": len(consumed),
        "gamma": g,
        "verdict": "INFERRED",
        "note": "分母=有 reply 的 loop 数，分子=作为交棒上游的 loop 数；正文级消费需引用清单。",
    }
