"""Prometheus 运行指标（/metrics 端点暴露）。"""
from prometheus_client import Counter, Histogram

TURNS_TOTAL = Counter(
    "mediation_turns_total", "对话回合数", ["action"])
TURN_LATENCY_MS = Histogram(
    "mediation_turn_latency_ms", "回合端到端延迟（ms，不含ASR/TTS）",
    buckets=(10, 50, 100, 200, 400, 800, 1200, 1800, 3000, 6000))
COMPLIANCE_HITS = Counter(
    "mediation_compliance_hits_total", "合规规则拦截次数", ["rule_id"])
LLM_CALLS = Counter(
    "mediation_llm_calls_total", "LLM调用结果", ["outcome"])   # ok / fallback
CALLS_BLOCKED = Counter(
    "mediation_calls_blocked_total", "外呼被策略拦截", ["reason"])  # dnd/dnc/freq
