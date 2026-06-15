"""PostgreSQL 数据模型（SQLAlchemy 2.0）。

表结构与《AI电话调解机器人_资料准备字段模板.xlsx》逐表对应：
  02_案件字段表      -> cases
  03_SOP节点表       -> sop_nodes
  04_意图抗性标签表  -> intent_labels
  05_决策路由表      -> decision_routes
  06_调解策略库      -> strategies
  07_话术模板库      -> script_templates
  08_Prompt组件库    -> prompt_components
  09_合规规则库      -> compliance_rules
  10_通话质检规则    -> qc_rules
运行数据：call_sessions / call_turns / quality_reports
"""
from __future__ import annotations

from datetime import datetime

from sqlalchemy import (JSON, Boolean, DateTime, Float, Index, Integer,
                        Numeric, String, Text, func)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column


class Base(DeclarativeBase):
    pass


# ---------------- 业务案件（02_案件字段表） ----------------
class Case(Base):
    __tablename__ = "cases"

    case_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    debtor_name: Mapped[str] = mapped_column(String(64))
    debtor_gender: Mapped[str | None] = mapped_column(String(8))
    debtor_phone: Mapped[str | None] = mapped_column(String(32))
    id_last4: Mapped[str | None] = mapped_column(String(8))
    platform_name: Mapped[str | None] = mapped_column(String(128))      # 确认本人后方可披露
    principal_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))
    total_amount: Mapped[float | None] = mapped_column(Numeric(14, 2))   # 高敏字段
    overdue_days: Mapped[int | None] = mapped_column(Integer)
    creditor_name: Mapped[str | None] = mapped_column(String(128))
    mediation_org: Mapped[str | None] = mapped_column(String(128))       # 可披露
    official_verify_channel: Mapped[str | None] = mapped_column(String(255))
    notice_status: Mapped[str | None] = mapped_column(String(16))
    case_status: Mapped[str | None] = mapped_column(String(32), default="待跟进")
    extra: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now(), onupdate=func.now())


# ---------------- 知识库（03~10 表） ----------------
class SopNode(Base):
    __tablename__ = "sop_nodes"

    node_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    node_name: Mapped[str] = mapped_column(String(64))
    stage: Mapped[str | None] = mapped_column(String(32))
    node_goal: Mapped[str | None] = mapped_column(String(255))
    entry_template_id: Mapped[str | None] = mapped_column(String(64))    # 节点主话术/主问句
    enter_condition: Mapped[str | None] = mapped_column(String(255))
    required_slots: Mapped[list | None] = mapped_column(JSON, default=list)
    allowed_actions: Mapped[str | None] = mapped_column(String(255))
    forbidden_actions: Mapped[str | None] = mapped_column(String(255))
    default_next: Mapped[str | None] = mapped_column(String(16))
    fallback_node: Mapped[str | None] = mapped_column(String(16))
    max_retry: Mapped[int] = mapped_column(Integer, default=1)
    allow_template: Mapped[bool] = mapped_column(Boolean, default=True)
    allow_llm: Mapped[bool] = mapped_column(Boolean, default=False)
    auto_chain: Mapped[bool] = mapped_column(Boolean, default=False)     # 陈述完自动衔接默认下一节点主问句
    remark: Mapped[str | None] = mapped_column(String(255))


class IntentLabel(Base):
    __tablename__ = "intent_labels"

    label_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    label_type: Mapped[str] = mapped_column(String(16))                  # 意图/抗性/风险/兜底/系统
    label_name: Mapped[str] = mapped_column(String(64))
    category: Mapped[str | None] = mapped_column(String(32))
    examples: Mapped[str | None] = mapped_column(String(255))
    keywords: Mapped[list | None] = mapped_column(JSON, default=list)
    risk_level: Mapped[str] = mapped_column(String(8), default="低")
    suggest_node: Mapped[str | None] = mapped_column(String(16))
    global_priority: Mapped[bool] = mapped_column(Boolean, default=False)
    confidence_min: Mapped[float] = mapped_column(Float, default=0.0)
    objection_label: Mapped[str | None] = mapped_column(String(48))      # 对应抗性标签
    remark: Mapped[str | None] = mapped_column(String(255))


class DecisionRoute(Base):
    __tablename__ = "decision_routes"
    __table_args__ = (Index("ix_routes_node_intent", "current_node", "intent_label"),)

    route_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    current_node: Mapped[str] = mapped_column(String(16))                # 节点ID或 ANY
    intent_label: Mapped[str] = mapped_column(String(48))                # 意图/抗性标签或 ANY
    slot_condition: Mapped[dict | None] = mapped_column(JSON, default=dict)
    risk_level: Mapped[str | None] = mapped_column(String(8))
    confidence_min: Mapped[float] = mapped_column(Float, default=0.0)
    next_node: Mapped[str] = mapped_column(String(16))
    action_type: Mapped[str] = mapped_column(String(32))
    strategy_id: Mapped[str | None] = mapped_column(String(48))
    template_id: Mapped[str | None] = mapped_column(String(64))
    prompt_component_ids: Mapped[list | None] = mapped_column(JSON, default=list)
    set_slots: Mapped[dict | None] = mapped_column(JSON, default=dict)   # 命中后写入的槽位
    priority: Mapped[int] = mapped_column(Integer, default=50)
    is_global: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(255))


class Strategy(Base):
    __tablename__ = "strategies"

    strategy_id: Mapped[str] = mapped_column(String(48), primary_key=True)
    strategy_name: Mapped[str] = mapped_column(String(64))
    nodes: Mapped[str | None] = mapped_column(String(128))
    intents: Mapped[str | None] = mapped_column(String(128))
    goal: Mapped[str | None] = mapped_column(String(255))
    instruction: Mapped[str] = mapped_column(Text)
    allowed_actions: Mapped[str | None] = mapped_column(String(255))
    forbidden_actions: Mapped[str | None] = mapped_column(String(255))
    need_llm: Mapped[str | None] = mapped_column(String(8))              # 是/否/可
    risk_level: Mapped[str | None] = mapped_column(String(8))
    fallback_template_id: Mapped[str | None] = mapped_column(String(64))


class ScriptTemplate(Base):
    __tablename__ = "script_templates"

    template_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    node_id: Mapped[str | None] = mapped_column(String(16))
    strategy_id: Mapped[str | None] = mapped_column(String(48))
    intent_label: Mapped[str | None] = mapped_column(String(64))
    template_text: Mapped[str] = mapped_column(Text)
    variants: Mapped[list | None] = mapped_column(JSON, default=list)   # 人审同义变体（轮换防复读机）
    variables: Mapped[list | None] = mapped_column(JSON, default=list)
    can_direct: Mapped[bool] = mapped_column(Boolean, default=True)
    need_rewrite: Mapped[bool] = mapped_column(Boolean, default=False)
    compliance_level: Mapped[str | None] = mapped_column(String(8), default="高")
    quality_score: Mapped[int | None] = mapped_column(Integer, default=90)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(255))


class PromptComponent(Base):
    __tablename__ = "prompt_components"

    component_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    component_type: Mapped[str] = mapped_column(String(32))
    nodes: Mapped[str | None] = mapped_column(String(255), default="ALL")
    enable_condition: Mapped[str | None] = mapped_column(String(128))
    content: Mapped[str] = mapped_column(Text)
    priority: Mapped[int] = mapped_column(Integer, default=50)
    required: Mapped[bool] = mapped_column(Boolean, default=False)
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(255))


class ComplianceRule(Base):
    __tablename__ = "compliance_rules"

    rule_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    rule_type: Mapped[str] = mapped_column(String(32))
    trigger_keywords: Mapped[list | None] = mapped_column(JSON, default=list)   # 正则列表（文本拦截类）
    stage: Mapped[str | None] = mapped_column(String(32))
    risk_level: Mapped[str | None] = mapped_column(String(8))
    requirement: Mapped[str | None] = mapped_column(String(255))
    action: Mapped[str | None] = mapped_column(String(64))
    repair_text: Mapped[str | None] = mapped_column(String(255))
    dynamic_kind: Mapped[str | None] = mapped_column(String(32))   # PRIVACY_PRE_IDENTITY / THIRD_PARTY / None
    intercept: Mapped[bool] = mapped_column(Boolean, default=True) # 文本拦截类；行为类=False（由路由/质检保障）
    enabled: Mapped[bool] = mapped_column(Boolean, default=True)
    remark: Mapped[str | None] = mapped_column(String(255))


class QcRule(Base):
    __tablename__ = "qc_rules"

    qc_id: Mapped[str] = mapped_column(String(16), primary_key=True)
    dimension: Mapped[str] = mapped_column(String(32))
    checkpoint: Mapped[str | None] = mapped_column(String(255))
    rule_text: Mapped[str | None] = mapped_column(String(255))
    severity: Mapped[str | None] = mapped_column(String(8))
    deduct: Mapped[int] = mapped_column(Integer, default=0)
    manual_review: Mapped[bool] = mapped_column(Boolean, default=False)
    output_field: Mapped[str | None] = mapped_column(String(64))
    remark: Mapped[str | None] = mapped_column(String(255))


class DncEntry(Base):
    """谢绝来电名单（用户明确要求停止联系，外呼策略层强制拦截）。"""
    __tablename__ = "dnc_entries"

    phone: Mapped[str] = mapped_column(String(32), primary_key=True)
    reason: Mapped[str | None] = mapped_column(String(128))
    source_call_id: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


# ---------------- 运行数据 ----------------
class CallSession(Base):
    __tablename__ = "call_sessions"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    case_id: Mapped[str | None] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(16), default="进行中")     # 进行中/已结束/转人工
    current_node: Mapped[str | None] = mapped_column(String(16))
    slots: Mapped[dict | None] = mapped_column(JSON, default=dict)
    call_result: Mapped[str | None] = mapped_column(String(32))
    risk_flags: Mapped[list | None] = mapped_column(JSON, default=list)
    transfer_human: Mapped[bool] = mapped_column(Boolean, default=False)
    started_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
    ended_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class CallTurn(Base):
    __tablename__ = "call_turns"
    __table_args__ = (Index("ix_turns_call", "call_id", "turn_index"),)

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    call_id: Mapped[str] = mapped_column(String(64))
    turn_index: Mapped[int] = mapped_column(Integer)
    user_text: Mapped[str | None] = mapped_column(Text)
    intent_label: Mapped[str | None] = mapped_column(String(48))
    objection_label: Mapped[str | None] = mapped_column(String(48))
    emotion_label: Mapped[str | None] = mapped_column(String(24))
    risk_level: Mapped[str | None] = mapped_column(String(8))
    confidence: Mapped[float | None] = mapped_column(Float)
    route_id: Mapped[str | None] = mapped_column(String(32))
    action_type: Mapped[str | None] = mapped_column(String(32))
    node_before: Mapped[str | None] = mapped_column(String(16))
    node_after: Mapped[str | None] = mapped_column(String(16))
    bot_reply: Mapped[str | None] = mapped_column(Text)
    llm_used: Mapped[bool] = mapped_column(Boolean, default=False)
    compliance: Mapped[dict | None] = mapped_column(JSON, default=dict)
    latency_ms: Mapped[dict | None] = mapped_column(JSON, default=dict)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())


class QualityReport(Base):
    __tablename__ = "quality_reports"

    call_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    score: Mapped[int] = mapped_column(Integer, default=100)
    identity_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    mediation_willingness: Mapped[str | None] = mapped_column(String(16))
    main_objection: Mapped[str | None] = mapped_column(String(48))
    repayment_plan: Mapped[dict | None] = mapped_column(JSON, default=dict)
    risk_flags: Mapped[list | None] = mapped_column(JSON, default=list)
    deductions: Mapped[list | None] = mapped_column(JSON, default=list)
    need_human_review: Mapped[bool] = mapped_column(Boolean, default=False)
    call_result: Mapped[str | None] = mapped_column(String(32))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), server_default=func.now())
