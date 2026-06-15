"""决策表路由引擎：(当前节点, 意图/抗性, 槽位条件, 置信度, 优先级) -> 下一动作。

候选键查找顺序：(节点,意图) > (ANY,意图) > (节点,抗性) > (ANY,抗性) > (节点,ANY) > (ANY,ANY)，
合并后按 priority 降序逐条过滤（enabled / 置信度 / 槽位条件），首条命中即返回。
纯内存字典索引，单次匹配微秒级（方案目标 <10ms）。
"""
from __future__ import annotations

from typing import Any


def slot_condition_ok(cond: dict | None, slots: dict) -> bool:
    for key, expect in (cond or {}).items():
        cur = slots.get(key)
        if expect == "__present__":
            if cur in (None, "", False):
                return False
        elif expect == "__missing__":
            if cur not in (None, "", False):
                return False
        elif isinstance(expect, bool):
            if bool(cur) != expect:
                return False
        elif str(cur) != str(expect):
            return False
    return True


def match_route(snap, node_id: str, cls, slots: dict) -> dict[str, Any] | None:
    keys: list[tuple[str, str]] = [(node_id, cls.intent), ("ANY", cls.intent)]
    if cls.objection and cls.objection != cls.intent:
        keys += [(node_id, cls.objection), ("ANY", cls.objection)]
    keys += [(node_id, "ANY"), ("ANY", "ANY")]

    seen: set[str] = set()
    candidates: list[dict] = []
    for k in keys:
        for r in snap.routes_index.get(k, []):
            rid = r["route_id"]
            if rid in seen:
                continue
            seen.add(rid)
            candidates.append(r)
    candidates.sort(key=lambda r: r.get("priority", 50), reverse=True)

    for r in candidates:
        if not r.get("enabled", True):
            continue
        if cls.confidence < (r.get("confidence_min") or 0.0):
            continue
        if not slot_condition_ok(r.get("slot_condition"), slots):
            continue
        return r
    return None
