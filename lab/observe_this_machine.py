"""单机观测页生成器：从 vault + digest → HTML 页面。

整合 observer_events / clusters / relay / gamma，生成自包含 HTML。
移植自 rultrix-single-instance/docs/single-machine/gen_this_machine.py。
"""
import collections
import datetime as dt
import hashlib
import html
import json
from pathlib import Path

from lab.observer_events import emit_events
from lab.clusters import build_loops, build_clusters
from lab.relay import build_relay_edges, build_echo_edges
from lab.gamma import compute_gamma
from lab.agents import agent_for_session
from lab.qoder_db import subagent_types
from lab.state import load_agents
from lab.timeformat import local_text, local_tz, parse_utc


BLUE, AMBER, RED, GREEN, GREY, FAINT, INK, PANEL = (
    '#0f766e', '#b45309', '#b91c1c', '#047857', '#94a3b8', '#64748b', '#0f172a', '#f1f5f9')


def n(v):
    return '{:,}'.format(int(round(v)))


def pct(x):
    return '%.1f%%' % (x * 100)


def build_observer_doc(events, loops, clusters, relay_edges, echo_edges, gamma_result, host_name):
    """从 Groma 数据构建 observer-json 文档结构。"""
    agents_set = {loop["agent"] for loop in loops}
    ws_set = {loop["ws"] for loop in loops if loop["ws"]}

    hours_all = set()
    for loop in loops:
        hours_all.update(loop.get("hours", []))

    span_start = min((loop["first"] for loop in loops), default=None)
    span_end = max((loop["last"] for loop in loops), default=None)
    if span_start and span_end:
        start_dt = dt.datetime.fromisoformat(span_start.replace("Z", "+00:00"))
        end_dt = dt.datetime.fromisoformat(span_end.replace("Z", "+00:00"))
        span_hours = round((end_dt - start_dt).total_seconds() / 3600.0, 1)
    else:
        span_hours = 0

    coverage_ratio = round(len(hours_all) / max(1.0, span_hours), 3) if span_hours else 0

    agent_metrics = collections.defaultdict(lambda: {"loops": 0, "events": 0, "writes": 0, "harness": set()})
    agent_kinds = {}
    for event in events:
        kind = event.get("agent_kind")
        if kind:
            agent_kinds.setdefault(event["agent_key"], kind)
    for loop in loops:
        m = agent_metrics[loop["agent"]]
        m["loops"] += 1
        m["events"] += loop["events"]
        m["writes"] += loop.get("writes", 0)
        if loop.get("harness"):
            m["harness"].add(loop["harness"])

    nodes = []
    for agent_id, m in agent_metrics.items():
        agent_loops = [loop for loop in loops if loop["agent"] == agent_id]
        first = min(loop["first"] for loop in agent_loops)
        last = max(loop["last"] for loop in agent_loops)
        nodes.append({
            "id": agent_id,
            "kind": "agent",
            "identity": {"harness": sorted(m["harness"]), "host": host_name,
                         "kind": agent_kinds.get(agent_id, "primary")},
            "metrics": {
                "loops": m["loops"],
                "events": m["events"],
                "writes": m["writes"],
                "first": first,
                "last": last,
            },
            "verdict": "OBSERVED",
        })

    for ws in ws_set:
        ws_loops = [loop for loop in loops if loop["ws"] == ws]
        nodes.append({
            "id": "ws-" + hashlib.sha256(ws.encode()).hexdigest()[:12],
            "kind": "workspace",
            "identity": {"basename": ws or "(unknown)"},
            "metrics": {"loops": len(ws_loops)},
            "verdict": "OBSERVED",
        })

    for cluster in clusters:
        nodes.append({
            "id": cluster["id"],
            "kind": "task_cluster",
            "identity": {"ws": cluster["ws"], "harnesses": cluster.get("harnesses", [])},
            "metrics": {
                "loops": len(cluster["loops"]),
                "events": cluster["events"],
                "agents": len(cluster["agents"]),
            },
            "first": cluster["first"],
            "last": cluster["last"],
            "verdict": "INFERRED",
        })

    edges = relay_edges + echo_edges
    edges_by_kind = collections.Counter(e["kind"] for e in edges)

    summary = {
        "host": host_name,
        "events": len(events),
        "agents": len(agents_set),
        "workspaces": len(ws_set),
        "loops": len(loops),
        "task_clusters": len(clusters),
        "coverage_ratio": coverage_ratio,
        "gamma": gamma_result,
        "edges_by_kind": dict(edges_by_kind),
    }

    return {"summary": summary, "nodes": nodes, "edges": edges}


def relay_stats(doc):
    """按有向对聚合交棒：次数、次数加权中位间隔、回声单元数。"""
    rel, gap, echo = {}, {}, {}
    for e in doc["edges"]:
        k = (e["from"], e["to"])
        if e["kind"] == "relay_candidate":
            p = e["weight"]["pairs"]
            rel[k] = rel.get(k, 0) + p
            gap[k] = gap.get(k, 0.0) + p * (e["weight"].get("median_gap_seconds") or 0.0)
        elif e["kind"] == "echo":
            echo[k] = max(echo.get(k, 0), e["weight"].get("repeated_units", 0))
    return rel, gap, echo


def role(m):
    if m["writes"] >= 1000:
        return "主力产出"
    if m["writes"] == 0 and m["loops"] >= 100:
        return "只跑不改"
    if m["loops"] <= 5:
        return "路过看一眼"
    return "边做边改"


def derive(doc, host_name):
    """从 observer-json 文档计算页面指标。"""
    s = doc["summary"]
    agents = sorted((x for x in doc["nodes"] if x["kind"] == "agent"),
                    key=lambda x: -x["metrics"]["loops"])
    clusters = [x for x in doc["nodes"] if x["kind"] == "task_cluster"]
    rel, gap, echo = relay_stats(doc)
    total = sum(rel.values())
    ranked = sorted(rel.items(), key=lambda kv: -kv[1])
    a, b = (ranked[0][0][0], ranked[0][0][1]) if ranked else (None, None)
    ab = rel.get((a, b), 0)
    ba = rel.get((b, a), 0)
    top2 = ab + ba
    pair_gap = (gap.get((a, b), 0.0) + gap.get((b, a), 0.0)) / top2 / 60.0 if top2 else 0.0

    # 可见的日/周文案按本机日历；绝对时刻（X 坐标、嵌入数据）仍是 UTC。
    tz = local_tz()
    weeks = {}
    for c in clusters:
        first = parse_utc(c["first"])
        if first is None:
            continue
        key = first.astimezone(tz).isocalendar()[:2]
        weeks[key] = weeks.get(key, 0) + 1
    week_key, week_count = max(weeks.items(), key=lambda kv: (kv[1], kv[0])) if weeks else (None, 0)
    if week_key:
        monday = dt.date.fromisocalendar(week_key[0], week_key[1], 1)
        week_label = "%s–%s" % (monday.strftime("%m/%d"), (monday + dt.timedelta(days=6)).strftime("%m/%d"))
    else:
        week_label = "—"

    by_id = {x["id"]: x for x in agents}
    firsts = [x for x in (parse_utc(c["first"]) for c in clusters) if x is not None]
    lasts = [x for x in (parse_utc(c["last"]) for c in clusters) if x is not None]
    start = local_text(min(firsts), tz=tz, date_only=True) if firsts else ""
    end = local_text(max(lasts), tz=tz, date_only=True) if lasts else ""

    gamma_info = s.get("gamma", {})
    gamma_val = gamma_info.get("gamma") if isinstance(gamma_info, dict) else None
    producers = gamma_info.get("producers", 0) if isinstance(gamma_info, dict) else 0
    consumed = gamma_info.get("consumed_proxy", 0) if isinstance(gamma_info, dict) else 0

    return {
        "doc": doc,
        "host": host_name,
        "start": start,
        "end": end,
        "events": s["events"],
        "loops": s["loops"],
        "agents": agents,
        "agent_count": s["agents"],
        "clusters": clusters,
        "cluster_count": s["task_clusters"],
        "coverage": s["coverage_ratio"],
        "gamma": gamma_val,
        "producers": producers,
        "consumed": consumed,
        "groups": s["edges_by_kind"].get("relay_candidate", 0),
        "pairs": total,
        "a": a, "b": b, "ab": ab, "ba": ba,
        "top2": top2,
        "top2_share": (top2 / total) if total else 0.0,
        "pair_gap": pair_gap,
        "pair_role_a": role(by_id[a]["metrics"]) if a else "",
        "pair_role_b": role(by_id[b]["metrics"]) if b else "",
        "pair_loops_a": by_id[a]["metrics"]["loops"] if a else 0,
        "pair_loops_b": by_id[b]["metrics"]["loops"] if b else 0,
        "pair_harness_a": "/".join(by_id[a]["identity"]["harness"]) if a else "",
        "pair_harness_b": "/".join(by_id[b]["identity"]["harness"]) if b else "",
        "echo": echo.get((a, b), echo.get((b, a), 0)) if a else 0,
        "week_label": week_label,
        "week_key": week_key,
        "week_count": week_count,
    }


def svg_timeline(f):
    """生成时间轴 SVG。"""
    clusters = f["clusters"]
    if not clusters:
        return '<svg viewBox="0 0 1000 100" xmlns="http://www.w3.org/2000/svg"><text x="500" y="50" text-anchor="middle" fill="%s">暂无数据</text></svg>' % FAINT

    lanes = sorted((x for x in f["doc"]["nodes"] if x["kind"] == "workspace"),
                   key=lambda x: -x["metrics"]["loops"])
    W, pad_l, pad_r, top = 1000, 150, 16, 48
    lane_h, gap = 26, 12
    t0 = min(dt.datetime.fromisoformat(c["first"]) for c in clusters)
    t1 = max(dt.datetime.fromisoformat(c["last"]) for c in clusters)
    span = (t1 - t0).total_seconds() or 1.0
    xw = W - pad_l - pad_r

    def X(t):
        return pad_l + (t - t0).total_seconds() / span * xw

    H = top + len(lanes) * (lane_h + gap) + 74
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" '
         'font-family="-apple-system,PingFang SC,sans-serif" role="img" '
         'aria-label="任务分布时间轴">' % (W, H),
         '<rect width="%d" height="%d" fill="#f8fafc"/>' % (W, H)]

    cur = t0.astimezone(local_tz()).replace(day=1, hour=0, minute=0, second=0)
    last_local = t1.astimezone(local_tz())
    while cur < last_local:
        if cur >= t0.astimezone(local_tz()):
            x = X(cur)
            o.append('<line x1="%.1f" y1="%d" x2="%.1f" y2="%d" stroke="%s"/>'
                     % (x, top - 10, x, H - 44, GREY))
            o.append('<text x="%.1f" y="%d" fill="%s" font-size="11">%s</text>'
                     % (x + 4, top - 16, FAINT, cur.strftime("%m/%d")))
        cur = (cur.replace(day=28) + dt.timedelta(days=4)).replace(day=1)

    if f["week_key"]:
        monday = dt.date.fromisocalendar(f["week_key"][0], f["week_key"][1], 1)
        ws = dt.datetime.combine(monday, dt.time(), tzinfo=local_tz())
        we = ws + dt.timedelta(days=7)
        o.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" fill="%s" fill-opacity="0.13"/>'
                 % (X(ws), top - 8, max(2.0, X(we) - X(ws)), H - top - 30, AMBER))

    for i, lane in enumerate(lanes):
        y = top + i * (lane_h + gap)
        base = lane["identity"]["basename"]
        base = (base[:17] + "…") if len(base) > 18 else base
        o.append('<text x="8" y="%d" fill="%s" font-size="12">%s</text>' % (y + 12, INK, html.escape(base)))
        o.append('<text x="8" y="%d" fill="%s" font-size="10.5">%s 段</text>'
                 % (y + 25, FAINT, n(lane["metrics"]["loops"])))
        o.append('<line x1="%d" y1="%d" x2="%d" y2="%d" stroke="%s"/>'
                 % (pad_l - 10, y + lane_h / 2, W - pad_r, y + lane_h / 2, GREY))
        mine = sorted((c for c in clusters if c["identity"]["ws"] == lane["id"]),
                      key=lambda c: c["first"])
        prev = None
        for c in mine:
            x1 = X(dt.datetime.fromisoformat(c["first"]))
            x2 = X(dt.datetime.fromisoformat(c["last"]))
            if prev is not None and x1 < prev:
                x1 = prev + 0.6
            w = max(2.4, x2 - x1)
            prev = x1 + w
            fill = BLUE if c["metrics"]["agents"] >= 2 else GREY
            o.append('<rect x="%.1f" y="%d" width="%.1f" height="%d" rx="2" fill="%s"/>'
                     % (x1, y + 3, w, lane_h - 6, fill))

    ly = H - 26
    x = pad_l
    for col, txt in ((GREY, "一条线自己干完"), (BLUE, "两条线以上一起干")):
        o.append('<rect x="%d" y="%d" width="14" height="9" rx="2" fill="%s"/>' % (x, ly - 8, col))
        o.append('<text x="%d" y="%d" fill="%s" font-size="11">%s</text>' % (x + 19, ly, FAINT, txt))
        x += 150
    o.append('<text x="8" y="%d" fill="%s" font-size="11">一个竖块 = 一次任务簇</text>'
             % (H - 44, FAINT))
    o.append('</svg>')
    return '\n'.join(o)


def arrow(x1, y1, x2, y2, color):
    import math
    ang = math.atan2(y2 - y1, x2 - x1)
    L, Wd = 9, 4.5
    bx, by = x2 - L * math.cos(ang), y2 - L * math.sin(ang)
    p1 = (bx - Wd * math.sin(ang), by + Wd * math.cos(ang))
    p2 = (bx + Wd * math.sin(ang), by - Wd * math.cos(ang))
    return ('<line x1="%.1f" y1="%.1f" x2="%.1f" y2="%.1f" stroke="%s" stroke-width="1.8"/>'
            '<polygon points="%.1f,%.1f %.1f,%.1f %.1f,%.1f" fill="%s"/>'
            % (x1, y1, bx, by, color, p1[0], p1[1], p2[0], p2[1], x2, y2, color))


def svg_pair(f):
    """生成两主线交棒 SVG。"""
    if not f["a"]:
        return '<svg viewBox="0 0 1000 100" xmlns="http://www.w3.org/2000/svg"><text x="500" y="50" text-anchor="middle" fill="%s">暂无主线数据</text></svg>' % FAINT

    W, H = 1000, 236
    bw, bh = 300, 84
    lx, rx, ty = 40, 660, 24
    o = ['<svg viewBox="0 0 %d %d" xmlns="http://www.w3.org/2000/svg" '
         'font-family="-apple-system,PingFang SC,sans-serif" role="img" '
         'aria-label="两条主线之间的交棒">' % (W, H),
         '<rect width="%d" height="%d" fill="#f8fafc"/>' % (W, H)]

    for x, aid, harness, rl, loops in ((lx, f["a"], f["pair_harness_a"], f["pair_role_a"], f["pair_loops_a"]),
                                       (rx, f["b"], f["pair_harness_b"], f["pair_role_b"], f["pair_loops_b"])):
        o.append('<rect x="x=%d" y="%d" width="%d" height="%d" rx="8" fill="%s" stroke="%s"/>'
                 % (x, ty, bw, bh, PANEL, BLUE))
        o.append('<text x="%d" y="%d" fill="%s" font-size="14">%s</text>' % (x + 16, ty + 26, INK, aid))
        o.append('<text x="%d" y="%d" fill="%s" font-size="12">%s · %s 段</text>'
                 % (x + 16, ty + 48, AMBER, rl, n(loops)))
        o.append('<text x="%d" y="%d" fill="%s" font-size="11">%s</text>'
                 % (x + 16, ty + 68, FAINT, harness))

    xa, xb = lx + bw, rx
    o.append(arrow(xa + 8, ty + 30, xb - 8, ty + 30, AMBER))
    o.append('<text x="%d" y="%d" fill="%s" font-size="12" text-anchor="middle">交给 %s 次</text>'
             % ((xa + xb) / 2, ty + 22, AMBER, n(f["ab"])))
    o.append(arrow(xb - 8, ty + 62, xa + 8, ty + 62, GREEN))
    o.append('<text x="%d" y="%d" fill="%s" font-size="12" text-anchor="middle">交回 %s 次</text>'
             % ((xa + xb) / 2, ty + 88, GREEN, n(f["ba"])))
    o.append('<text x="%d" y="%d" fill="%s" font-size="12" text-anchor="middle">中位间隔 %.1f 分钟</text>'
             % ((xa + xb) / 2, ty + 124, FAINT, f["pair_gap"]))
    if f["echo"]:
        o.append('<text x="%d" y="%d" fill="%s" font-size="12" text-anchor="middle">'
                 '字节级相同：%s 个内容单元</text>' % (W / 2, ty + 172, RED, n(f["echo"])))
    o.append('</svg>')
    return '\n'.join(o)


CSS = """
* { box-sizing: border-box; }
body { margin: 0; background: #f8fafc; color: #0f172a;
  font-family: "Segoe UI", "Microsoft YaHei", system-ui, sans-serif; }
.wrap { max-width: 1040px; margin: 0 auto; padding: 24px 28px 48px; }
.eyebrow { font-size: 12.5px; letter-spacing: .08em; color: #64748b; text-transform: uppercase; }
h1 { font-size: 22px; line-height: 1.35; color: #0f172a; margin: 10px 0 14px; font-weight: 700; }
.lead { font-size: 15px; line-height: 1.7; margin: 0 0 18px; color: #334155; }
.concl { border-left: 4px solid #b45309; background: #fffbeb; padding: 13px 18px;
  font-size: 15px; font-weight: 600; color: #78350f; border-radius: 0 8px 8px 0; }
.kpis { display: grid; grid-template-columns: repeat(3, 1fr); gap: 14px; margin: 26px 0 8px; }
.kpi { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px; padding: 16px 18px; }
.kpi b { display: block; font-size: 32px; line-height: 1.15; color: #0f172a; font-weight: 700; }
.kpi .k { font-size: 13px; color: #64748b; margin-top: 6px; line-height: 1.5; }
section { background: #fff; border: 1px solid #e2e8f0; border-radius: 12px;
  padding: 22px 24px 24px; margin: 16px 0; }
h2 { font-size: 18px; color: #0f172a; margin: 0 0 14px; padding-left: 11px;
  border-left: 4px solid #0f766e; line-height: 1.4; font-weight: 700; }
p { font-size: 14px; line-height: 1.7; margin: 10px 0; color: #334155; }
.cap { font-size: 13px; color: #64748b; line-height: 1.7; margin: 10px 0 0; }
.svgwrap { overflow-x: auto; margin: 8px 0 2px; }
svg { width: 100%; min-width: 720px; height: auto; display: block; }
ol.take { margin: 6px 0 0; padding-left: 22px; }
ol.take li { font-size: 14px; line-height: 1.7; margin: 9px 0; color: #334155; }
ol.take b { color: #0f172a; }
.bounds { display: grid; gap: 10px; margin-top: 4px; }
.bound { display: flex; gap: 12px; align-items: baseline; font-size: 14px; line-height: 1.7; color: #334155; }
.bound span { flex: 0 0 76px; font-size: 12.5px; font-weight: 600; text-align: center;
  border-radius: 4px; padding: 2px 0; }
.bound .a { color: #b45309; border: 1px solid #b45309; }
.bound .b { color: #b91c1c; border: 1px solid #b91c1c; }
.bound .c { color: #0f766e; border: 1px solid #0f766e; }
table.agents { width: 100%; border-collapse: collapse; font-size: 13px; margin-top: 10px; }
table.agents th, table.agents td { text-align: left; padding: 6px 8px; border-bottom: 1px solid #e2e8f0; }
table.agents th { font-size: 12px; color: #64748b; font-weight: 600; }
table.agents td.id { font-family: Consolas, monospace; font-size: 12px; overflow-wrap: anywhere; }
.tag { display: inline-block; padding: 1px 8px; border-radius: 999px; font-size: 11px; border: 1px solid #e2e8f0; color: #64748b; }
.tag.sub { border-color: #a7f3d0; background: #f0fdfa; color: #065f46; }
footer { margin-top: 26px; font-size: 12.5px; color: #64748b; line-height: 1.7; }
a { color: #0f766e; }
@media (max-width: 720px) {
  .wrap { padding: 20px 16px 32px; }
  h1 { font-size: 20px; }
  .kpis { grid-template-columns: 1fr; }
  .kpi b { font-size: 28px; }
}
"""


def render(f):
    """生成 HTML 页面。"""
    g = f["gamma"]
    share = pct(f["top2_share"])
    rest = f["agent_count"] - 2
    a6, b6 = (f["a"] if f["a"] else ""), (f["b"] if f["b"] else "")
    handoff = ("其中 %s 发生在 %s 和 %s 之间；另外 %d 条只在边上露过面。" % (share, a6, b6, rest)
               if rest > 0 else "其中 %s 发生在这两条线之间。" % share) if f["a"] else ""
    concl = ("不是 %d 个 Agent 各忙各的，是 %s 和 %s 两条线在接力。" % (f["agent_count"], a6, b6)
             if f["top2_share"] >= 0.7 else
             "这台机器上 %d 个 Agent 基本各干各的。" % f["agent_count"]) if f["a"] else "数据不足"
    fill = "约 %d 段把成果交给了另一条线" % round(g * 10) if g is not None else "未采到"

    parts = [
        '<!DOCTYPE html><html lang="zh"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        '<title>这台机器上的 Agent：谁在跟谁接活 · Groma</title>',
        '<style>%s</style></head><body><div class="wrap">' % CSS,
        '<div class="eyebrow">单机观测 · %s · %s → %s</div>' % (html.escape(f["host"]), f["start"], f["end"]),
        '<h1>这台机器上 %d 个 Agent，谁在跟谁接活</h1>' % f["agent_count"],
        '<p class="lead">%s 条事件、%s 段连续工作。真正"一件事前后接手"的有 %s 组、合计 %s 次，%s</p>'
        % (n(f["events"]), n(f["loops"]), n(f["groups"]), n(f["pairs"]), handoff),
        '<div class="concl">结论：%s</div>' % concl,
        '<div class="kpis">',
        '<div class="kpi"><b>%s</b><div class="k">条事件</div>'
        '<div class="k">%d 条线、%s 段连续工作</div></div>'
        % (n(f["events"]), f["agent_count"], n(f["loops"])),
        '<div class="kpi"><b>%s 组</b><div class="k">前后接手</div>'
        '<div class="k">同一件事里上一手收工、下一手接着干，合计 %s 次</div></div>'
        % (n(f["groups"]), n(f["pairs"])),
        '<div class="kpi"><b>%s</b><div class="k">递出去的比例</div>'
        '<div class="k">每 10 段有产出的工作，%s</div></div>'
        % (pct(g) if g is not None else "—", fill),
        '</div>',
        '<section><h2>Agent 一览</h2><table class="agents"><thead><tr>'
        '<th>Agent</th><th>类型</th><th>Harness</th><th>连续工作</th><th>事件</th><th>写</th>'
        '</tr></thead><tbody>',
        ''.join(
            '<tr><td class="id">%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td><td>%s</td></tr>'
            % (html.escape(a["id"]),
               '<span class="tag sub">子 Agent</span>'
               if (a.get("identity") or {}).get("kind") == "sub"
               else '<span class="tag">主 Agent</span>',
               html.escape(", ".join((a.get("identity") or {}).get("harness") or []) or "—"),
               n(a["metrics"]["loops"]), n(a["metrics"]["events"]), n(a["metrics"]["writes"]))
            for a in f.get("agents", [])
        ),
        '</tbody></table></section>',
        '<section><h2>任务分布</h2>',
        '<div class="svgwrap">%s</div>' % svg_timeline(f),
        '<p class="cap">最忙的一周是 %s，%d 件事同时开工。时间轴采到了 %s 的工作小时；'
        '剩下的 %s 是没记录，不是没干活。</p>'
        % (f["week_label"], f["week_count"], pct(f["coverage"]), pct(1 - f["coverage"])),
        '</section>',
    ]

    if f["a"]:
        parts += [
            '<section><h2>两条主线之间发生了什么</h2>',
            '<div class="svgwrap">%s</div>' % svg_pair(f),
            '<ol class="take">',
            '<li><b>谁交给谁。</b>%s 是「%s」，把活交给 %s（「%s」）%s 次；反方向 %s 次，中位间隔 %.1f 分钟。</li>'
            % (a6, f["pair_role_a"], b6, f["pair_role_b"], n(f["ab"]), n(f["ba"]), f["pair_gap"]),
            '<li><b>写了两遍。</b>%s</li>'
            % ('两条线之间有 %s 个字节级相同的内容单元，同一份东西两边各写了一遍。' % n(f["echo"])
               if f["echo"] else '两条线之间没有发现字节级相同的内容。'),
            '<li><b>只能说到这。</b>我们看到的是"谁先收工、谁后开工"和"内容一样"，'
            '看不到后者有没有真的读前者。</li>',
            '</ol></section>',
        ]

    parts += [
        '<section><h2>这页看不到的三件事</h2><div class="bounds">',
        '<div class="bound"><span class="a">看不见</span><div>只有 %s 的工作小时留下了记录。'
        '剩下那部分没有数据，不等于没干活。</div></div>' % pct(f["coverage"]),
        '<div class="bound"><span class="b">不算数</span><div>页上所有"接手"都是候选：'
        '看到前后相邻，但没核对过交付物，不能当成已确认的协作。</div></div>',
        '<div class="bound"><span class="c">不读正文</span><div>只数次数、时间戳和内容哈希，'
        '一句对话原文都没读。</div></div>',
        '</div></section>',
        # 页脚也要走本会话的 local_tz（Y6 的 patch 点在此）——不传 tz 会让 local_text
        # 落到 timeformat 自己的 local_tz()，测试的 mock 与真实"本机口径"都拦不住它（CI 红过一次实录）
        '<footer>Groma 单机观测页 · 生成于 %s</footer>'
        % local_text(dt.datetime.now(dt.timezone.utc), tz=local_tz()),
        '</div></body></html>',
    ]
    return '\n'.join(parts)


def digest_rows_from_index(sessions, cwd="", ws_ref="ws-local",
                           registry=None, sub_types=None):
    """vault index sessions → digest rows carrying the session's real agent.

    R19 裁定（2026-11-17，项目所有者 选 B）：子 Agent 算独立会话，观测页不得再把
    每一行烤成 `agt-<harness>-default`。身份解析与 digest 共用 `agent_for_session`；
    没有名字的会话才落到该 harness 的 primary。registry / sub_types 可注入，测试
    不必读本机注册表与 Qoder 库。
    """
    agents = load_agents() if registry is None else registry
    subs = subagent_types() if sub_types is None else sub_types
    rows = []
    for session in sessions:
        session_key = session.get("session_key", "")
        harness = session.get("harness", "")
        if ":" in session_key:
            session_uuid = session_key.split(":", 1)[1]
        else:
            session_uuid = session_key
        sub_type = subs.get(session_uuid) if harness == "qoder-cn" else None
        agent = agent_for_session(agents, harness, sub_type)
        rows.append({
            "session_key": session_key,
            "harness": harness,
            "path": "db/%s" % session_uuid if harness == "qoder-cn" else session_key,
            "agent_id": agent["agent_id"],
            "agent_name": agent["name"],
            "agent_kind": agent["kind"],
            "parent_agent_id": agent.get("parent_agent_id"),
            "cwd": cwd,
            "ws_ref": ws_ref,
            "events": sum(shard.get("records", 0) for shard in session.get("shards", [])),
        })
    return rows


def generate_observation_page(vault_root, digest_rows, host_name, output_path=None):
    """完整流水线：vault + digest → HTML 页面。"""
    events = emit_events(vault_root, digest_rows, host_name)
    if not events:
        return None

    loops = build_loops(events)
    clusters = build_clusters(loops)
    relay_edges = build_relay_edges(loops, clusters)
    echo_edges = build_echo_edges(loops)
    gamma_result = compute_gamma(loops, relay_edges)

    doc = build_observer_doc(events, loops, clusters, relay_edges, echo_edges, gamma_result, host_name)
    f = derive(doc, host_name)
    html_content = render(f)

    if output_path:
        Path(output_path).write_text(html_content, encoding="utf-8")
        return output_path

    return html_content
