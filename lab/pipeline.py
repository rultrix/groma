"""流水线角色表：harness → 站位，人工配一次；棒位自动推导。

分层原则（母本 §4.2）：切换时刻是 digest 硬事实（自动、零额度），角色语义是
人工声明（一次配表），棒位 = 最近会话的 harness 查表得「已完成站」、下一站即棒位。
第一期固定三站：任务书 / 施工 / 验收；顺序即流水线，加站另立一期。
不配表的主题：角色接力不启用，只有同工作流触发（零配置可用）。
"""

from __future__ import annotations

from .workspace import LabError

# 站位顺序即流水线；加站另立一期。
STATIONS = ("任务书", "施工", "验收")


def normalise_roles(roles: dict[str, str] | None) -> dict[str, str]:
    """校验并清洗一张角色表；空表原样返回空 dict（= 清表）。

    非法形状一律拒配（fail-loud）：站位必须是三站之一，一个站位至多一个 harness，
    harness 键非空。
    """
    cleaned: dict[str, str] = {}
    for harness, station in (roles or {}).items():
        key = str(harness or "").strip()
        value = str(station or "").strip()
        if not key:
            continue  # 空 harness = 这一站没配，跳过
        if not value:
            continue
        if value not in STATIONS:
            raise LabError("站位只能是 %s 之一，收到：%s" % ("、".join(STATIONS), value))
        if value in cleaned.values():
            raise LabError("一个站位只能配一个 harness：%s 被配了两次" % value)
        cleaned[key] = value
    return cleaned


def role_of(topic_roles: dict[str, str] | None, harness: str) -> str | None:
    """harness 查表得角色；未配表或表里没有这个 harness 都返回 None。"""
    if not topic_roles:
        return None
    return topic_roles.get(str(harness or "").strip())


def derive_baton(topic_roles: dict[str, str] | None, latest_harness: str | None) -> dict:
    """棒位推导：最近会话的 harness 查表得「已完成站」，下一站即棒位。

    - 未配表：`enabled=False`（角色接力不启用，零配置可用）。
    - 最近 harness 不在表里：`mapped=False`——推不出棒位是诚实账，不猜。
    - 已到最后一站：`next_station=None`（流水线走完，没有下一棒）。
    """
    harness = str(latest_harness or "").strip()
    if not topic_roles:
        return {"enabled": False, "mapped": False, "latest_harness": harness,
                "role": None, "completed_station": None, "next_station": None}
    role = role_of(topic_roles, harness)
    if role is None or role not in STATIONS:
        return {"enabled": True, "mapped": False, "latest_harness": harness,
                "role": None, "completed_station": None, "next_station": None}
    index = STATIONS.index(role)
    next_station = STATIONS[index + 1] if index + 1 < len(STATIONS) else None
    return {"enabled": True, "mapped": True, "latest_harness": harness,
            "role": role, "completed_station": role, "next_station": next_station}
