"""知识库引用完整性校验（纯函数，导入脚本与快照加载共用）。

防止业务在 Excel 中改错引用（路由指向不存在的模板/节点等）带病上线。
"""
from __future__ import annotations


def validate_refs(nodes: list[dict], routes: list[dict], templates: list[dict],
                  strategies: list[dict], components: list[dict]) -> list[str]:
    issues: list[str] = []
    node_ids = {n["node_id"] for n in nodes}
    tpl_ids = {t["template_id"] for t in templates}
    strat_ids = {s["strategy_id"] for s in strategies}
    comp_ids = {c["component_id"] for c in components}

    for n in nodes:
        nid = n["node_id"]
        for f in ("default_next", "fallback_node"):
            v = n.get(f)
            if v and v != "END" and v not in node_ids:
                issues.append(f"节点{nid}.{f}={v} 不存在")
        etpl = n.get("entry_template_id")
        if etpl and etpl not in tpl_ids:
            issues.append(f"节点{nid}.entry_template_id={etpl} 话术不存在")

    for r in routes:
        rid = r["route_id"]
        if r.get("current_node") not in node_ids | {"ANY"}:
            issues.append(f"路由{rid}.current_node={r.get('current_node')} 不存在")
        if r.get("next_node") not in node_ids:
            issues.append(f"路由{rid}.next_node={r.get('next_node')} 不存在")
        tid = r.get("template_id")
        if tid and tid not in tpl_ids:
            issues.append(f"路由{rid}.template_id={tid} 话术不存在")
        sid = r.get("strategy_id")
        if sid and sid not in strat_ids:
            issues.append(f"路由{rid}.strategy_id={sid} 策略不存在")
        for cid in r.get("prompt_component_ids") or []:
            if cid not in comp_ids:
                issues.append(f"路由{rid} 引用Prompt组件{cid} 不存在")
        if not isinstance(r.get("slot_condition") or {}, dict):
            issues.append(f"路由{rid}.slot_condition 不是字典")

    for s in strategies:
        fb = s.get("fallback_template_id")
        if fb and fb not in tpl_ids:
            issues.append(f"策略{s['strategy_id']}.fallback_template_id={fb} 话术不存在")

    return issues
