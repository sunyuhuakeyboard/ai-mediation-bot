"""通话会话状态：Redis JSON（TTL）或离线内存，单回合仅 1 读 1 写。"""
from __future__ import annotations

from dataclasses import asdict, dataclass, field

import orjson

HISTORY_LIMIT = 6   # 仅保留近6条用于LLM上下文，控制prompt长度


@dataclass
class CallState:
    call_id: str
    case: dict = field(default_factory=dict)
    current_node: str = "N001"
    slots: dict = field(default_factory=dict)
    turn_index: int = 0
    retries: dict = field(default_factory=dict)
    history: list = field(default_factory=list)        # [(role, text)]
    risk_flags: list = field(default_factory=list)
    node_trail: list = field(default_factory=list)     # 走过的节点（质检用）
    call_result: str | None = None
    ended: bool = False
    transfer_human: bool = False
    last_fragment: str | None = None                   # ASR碎片合并：上轮未命中文本
    variant_cursor: dict = field(default_factory=dict)  # 话术变体轮换游标
    okcti_last_request_key: str | None = None           # OKCTI 重试幂等：最近请求指纹
    okcti_last_response: dict = field(default_factory=dict)  # 最近一次 SSE 响应快照

    def remember(self, role: str, text: str) -> None:
        self.history.append([role, text])
        if len(self.history) > HISTORY_LIMIT:
            self.history = self.history[-HISTORY_LIMIT:]

    def visit(self, node_id: str) -> None:
        if not self.node_trail or self.node_trail[-1] != node_id:
            self.node_trail.append(node_id)

    def dumps(self) -> str:
        return orjson.dumps(asdict(self)).decode()

    @classmethod
    def loads(cls, raw: str) -> "CallState":
        return cls(**orjson.loads(raw))


class StateStore:
    """redis=None 时为进程内内存模式（offline/单测）。"""

    def __init__(self, redis=None, ttl: int = 3600) -> None:
        self.redis = redis
        self.ttl = ttl
        self._mem: dict[str, str] = {}

    @staticmethod
    def _key(call_id: str) -> str:
        return f"call:{call_id}:state"

    async def get(self, call_id: str) -> CallState | None:
        raw = (await self.redis.get(self._key(call_id))) if self.redis else self._mem.get(self._key(call_id))
        return CallState.loads(raw) if raw else None

    async def save(self, state: CallState) -> None:
        raw = state.dumps()
        if self.redis:
            await self.redis.set(self._key(state.call_id), raw, ex=self.ttl)
        else:
            self._mem[self._key(state.call_id)] = raw

    async def delete(self, call_id: str) -> None:
        if self.redis:
            await self.redis.delete(self._key(call_id))
        else:
            self._mem.pop(self._key(call_id), None)
