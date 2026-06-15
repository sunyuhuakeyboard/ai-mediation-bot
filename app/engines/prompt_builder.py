"""Prompt 组件动态拼装（对应 08_Prompt组件库）。

按路由配置的组件ID列表（缺省为全量必选集）筛选：
- PRIVACY 仅在未确认本人时启用；CASE_SAFE 仅在确认本人且节点匹配时启用；
- ROLE / COMPLIANCE / OUTPUT 永远强制存在；
按 priority 降序拼为单条 user 消息（短句生成场景无需多轮）。
"""
from __future__ import annotations

from app.utils.text import render

DEFAULT_COMPONENTS = ["ROLE", "HISTORY", "NODE", "USER_TEXT", "KNOWN", "CLASSIFY",
                      "STRATEGY", "SCRIPT", "PRIVACY", "CASE_SAFE", "COMPLIANCE",
                      "NO_REPEAT", "OUTPUT"]
_MANDATORY = ("ROLE", "COMPLIANCE", "OUTPUT")


_SLOT_FACT_LABELS = (("repayment_amount", "可还金额"), ("repayment_date", "还款时间"),
                     ("installment_count", "分期期数"), ("installment_amount", "每期金额"),
                     ("callback_time", "回访时间"))
_FLAG_FACTS = (("identity_confirmed", "已确认本人"), ("not_self", "接听人非本人"),
               ("debt_disputed", "用户对欠款有异议"), ("paid_claimed", "用户称已还款"),
               ("amount_disputed", "用户对金额有异议"), ("no_money", "用户表示资金困难"))


def _known_facts(slots: dict) -> str:
    facts = [label for key, label in _FLAG_FACTS if slots.get(key)]
    mw = slots.get("mediation_willingness")
    if mw:
        facts.append(f"调解意愿：{mw}")
    for key, label in _SLOT_FACT_LABELS:
        v = slots.get(key)
        if v not in (None, ""):
            facts.append(f"{label}{v}")
    return "；".join(facts)


def _history_text(history: list | None, limit: int = 4) -> str:
    lines = []
    for role, text in (history or [])[-limit:]:
        lines.append(("用户：" if role == "user" else "调解员：") + str(text))
    return chr(10).join(lines)


def _recent_bot_lines(history: list | None, limit: int = 2) -> str:
    bots = [str(t) for r, t in (history or []) if r == "bot"]
    return "；".join(bots[-limit:])


def build_messages(snap, route: dict, node: dict, strategy: dict | None,
                   template: dict | None, cls, user_text: str,
                   ctx: dict, slots: dict, history: list | None = None) -> list[dict]:
    ids = list(route.get("prompt_component_ids") or DEFAULT_COMPONENTS)
    for must in _MANDATORY:
        if must not in ids:
            ids.append(must)

    identity = bool(slots.get("identity_confirmed"))
    history_text = _history_text(history)
    known_facts = _known_facts(slots)
    recent_bot = _recent_bot_lines(history)
    picked: list[dict] = []
    for cid in ids:
        comp = snap.components.get(cid)
        if not comp or not comp.get("enabled", True):
            continue
        if cid == "PRIVACY" and identity:
            continue
        if cid == "HISTORY" and not history_text:
            continue
        if cid == "KNOWN" and not known_facts:
            continue
        if cid == "NO_REPEAT" and not recent_bot:
            continue
        if cid == "CASE_SAFE":
            if not identity:
                continue
            nodes = comp.get("nodes") or "ALL"
            if nodes != "ALL" and node["node_id"] not in nodes:
                continue
        if cid == "SCRIPT" and not (template or {}).get("template_text"):
            continue
        picked.append(comp)
    picked.sort(key=lambda c: c.get("priority", 50), reverse=True)

    variables = {
        "node_name": node.get("node_name") or "",
        "node_goal": node.get("node_goal") or "",
        "user_text": user_text,
        "intent": cls.intent,
        "objection": cls.objection or "无",
        "risk": cls.risk,
        "strategy_instruction": (strategy or {}).get("instruction") or "",
        "template_text": (template or {}).get("template_text") or "",
        "history_text": history_text,
        "known_facts": known_facts,
        "recent_bot_lines": recent_bot,
        **ctx,
    }
    parts = [render(c["content"], variables) for c in picked]
    prompt = "\n".join(p for p in parts if p.strip())
    return [{"role": "user", "content": prompt}]
