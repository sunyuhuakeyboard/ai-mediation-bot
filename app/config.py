"""全局配置（pydantic-settings，支持 .env / 环境变量覆盖）。"""
from functools import lru_cache

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

    app_name: str = "ai-mediation-bot"
    debug: bool = False

    # 离线演示模式：不依赖 PG/Redis，知识库走内置种子，状态存内存（用于本地试跑/单测）
    offline_mode: bool = False
    auto_create_tables: bool = True      # 启动时自动建表
    auto_seed_if_empty: bool = True      # 知识表为空时自动灌入内置种子（来自Excel模板）

    # ---- 存储 ----
    pg_dsn: str = "postgresql+asyncpg://mediation:mediation@localhost:5432/mediation"
    pg_pool_size: int = 10
    redis_url: str = "redis://localhost:6379/0"
    call_state_ttl: int = 3600                    # 通话状态 TTL（秒）
    knowledge_channel: str = "knowledge:reload"   # 知识热更新广播频道

    # ---- 小模型意图分类服务（可选；不配或超时自动降级为关键词分类）----
    classifier_url: str | None = None             # 例: http://127.0.0.1:8100/classify
    classifier_timeout_ms: int = 250

    # ---- LLM（OpenAI 兼容接口：Doubao方舟 / Qwen DashScope / DeepSeek / vLLM 均可）----
    llm_base_url: str = "https://ark.cn-beijing.volces.com/api/v3"
    llm_api_key: str = ""
    llm_model: str = "doubao-seed-2-0-mini-260428"
    llm_temperature: float = 0.3
    llm_max_tokens: int = 60
    llm_connect_timeout_ms: int = 300
    llm_first_token_timeout_ms: int = 1500        # 首token超时：连接成功但首字符迟迟不到 → 立即兜底
    llm_total_timeout_ms: int = 3000              # 总预算：首token到首句结束；超时仍尝试落地部分输出
    llm_disable_thinking: bool = True             # 关闭思考模式：短回复无需推理，降低首token延迟
    llm_classifier_enabled: bool = True           # 关键词分类返回 UNKNOWN 时，让 LLM 兜底打标签
    llm_classifier_timeout_ms: int = 1200         # LLM 意图分类预算：超时即降级为 UNKNOWN
    reply_max_chars: int = 48                     # 单句输出上限（约束"不超过30字"留余量）

    # 金额/时间复述强制走模板渲染（零幻觉），不交给LLM改写
    plan_confirm_force_template: bool = True

    # ---- 商用合规与体验 ----
    # 开场合规披露：录音告知 + AI身份披露（多地监管要求，不建议关闭）
    opening_disclosure: bool = True
    opening_disclosure_text: str = "提示您，本次通话将会录音，由智能调解助理协助您沟通。"
    # 外呼策略：勿扰时段 / 单号码当日呼叫上限 / DNC谢绝名单
    outbound_policy_enabled: bool = True
    dnd_start_hour: int = 21              # 勿扰开始（含）
    dnd_end_hour: int = 8                 # 勿扰结束（不含）
    daily_call_limit: int = 2             # 同一号码每日外呼上限
    # 话术变体池轮换：同模板多个人审变体按通话顺序轮换，避免复读机感（零LLM延迟）
    variant_rotation: bool = True
    # ASR碎片合并：上一轮未命中路由时，与本轮文本拼接后重试一次
    asr_merge_enabled: bool = True
    # UNKNOWN长尾：先用受限LLM简短回应并拉回流程，失败再退"没听清"
    freeform_fallback_llm: bool = True
    # 金额合理性：承诺金额超过欠款总额×系数或<1元 → 清槽复核，不进确认
    amount_anomaly_factor: float = 1.5
    # 通话结束后LLM深度质检（规则引擎盲区兜底；离线/未配LLM时自动跳过）
    llm_audit_enabled: bool = False
    llm_audit_max_lines: int = 20

    # ---- OKCTI / LLM-IVR SSE 对接 ----
    okcti_auth_enabled: bool = False
    okcti_app_id: str = ""
    okcti_app_secret: str = ""
    okcti_response_charset: str = "UTF-8"
    okcti_force_start: bool = True          # CTI平台通常已完成外呼策略控制；联调默认不二次拦截
    okcti_transfer_skill: str = "人工坐席"
    okcti_tts_spk_name: str = ""
    okcti_msg_chunk_chars: int = 200    # 投递切分阈值：≤该值不分段，避免 IVR 重复播报
    okcti_silence_max_turns: int = 2    # 连续静音超过该次数即优雅结束，避免 IVR 反复自播
    # OKCTI 上游未传 case 字段时的兜底值（生产）
    okcti_default_mediation_org: str = "亦法云调解中心"
    okcti_default_debtor_name: str = "张小贤"
    okcti_default_platform_name: str = "AI调解中心"
    okcti_default_creditor_name: str = "橘子分期"
    # IVR 语音交互参数（毫秒）：全部为 0 时各厂商默认值不一致，最常见就是 IVR 自播 cmdcontent
    # 一次形成"每句话说两遍"的听感。显式给出后由我方驱动节奏。
    okcti_voice_allow_stop: int = 1     # 允许用户语音打断
    okcti_voice_block_time: int = 500   # bot 播报开始 500ms 内屏蔽用户输入，避免噪声触发 ASR
    okcti_voice_timeout: int = 6000     # 用户开口超时 6s，超时即由 OKCTI 发 usrtype=9，不要自播
    okcti_voice_min_speak: int = 300    # 有效说话最少 300ms（过短当噪声丢弃）
    okcti_voice_min_pause: int = 700    # 用户停顿 700ms 视为结束（中文口语典型值）

    # ---- 质检 ----
    latency_warn_ms: int = 1800                   # QC010: P95 响应阈值


@lru_cache
def get_settings() -> Settings:
    return Settings()
