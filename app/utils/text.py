"""文本工具：模板变量渲染、中文数字/金额/日期/分期槽位抽取、TTS清洗、首句截断。"""
from __future__ import annotations

import re

# ---------------- 模板变量渲染 ----------------
_VAR_RE = re.compile(r"\{([a-zA-Z_][a-zA-Z0-9_]*)\}")


def render(template: str, ctx: dict) -> str:
    """用 ctx 替换 {var}；缺失变量保留原样（由 missing_vars/strip_unfilled 处理）。"""
    def _sub(m: re.Match) -> str:
        v = ctx.get(m.group(1))
        return str(v) if v not in (None, "") else m.group(0)
    return _VAR_RE.sub(_sub, template or "")


def missing_vars(template: str, ctx: dict) -> list[str]:
    return [k for k in _VAR_RE.findall(template or "") if ctx.get(k) in (None, "")]


def strip_unfilled(text: str) -> str:
    """移除残留的 {var} 占位符（安全兜底，正常路由下不应出现）。"""
    return _VAR_RE.sub("", text or "").replace("  ", " ").strip()


# ---------------- 中文数字 ----------------
_CN_DIGIT = {"零": 0, "一": 1, "二": 2, "两": 2, "三": 3, "四": 4, "五": 5,
             "六": 6, "七": 7, "八": 8, "九": 9}
_CN_UNIT = {"十": 10, "百": 100, "千": 1000, "万": 10000}


def cn2int(s: str) -> int | None:
    """常见口语中文数字转整数：一千 / 两千五 / 一万二 / 十五 / 3千 等。"""
    if not s:
        return None
    if s.isdigit():
        return int(s)
    total, section, num = 0, 0, 0
    for ch in s:
        if ch in _CN_DIGIT:
            num = _CN_DIGIT[ch]
        elif ch.isdigit():
            num = num * 10 + int(ch)
        elif ch in _CN_UNIT:
            u = _CN_UNIT[ch]
            if u == 10000:
                section = (section + (num or 0)) * u
                total += section
                section, num = 0, 0
            else:
                section += (num if num else 1) * u
                num = 0
        else:
            return None
    total += section + num
    # "两千五"=2500、"一万二"=12000 的口语缩略
    if num and section == 0 and total > num:
        pass
    return total if total else (num or None)


_TRAIL_SHORT = re.compile(r"([一二两三四五六七八九]|\d)[千万]([一二两三四五六七八九])$")


def _fix_colloquial(raw: str, val: int | None) -> int | None:
    """修正"两千五/一万二"这类缩略（末位数字按上一级单位的1/10计）。"""
    if val is None:
        return None
    m = _TRAIL_SHORT.search(raw)
    if m:
        unit = 1000 if "千" in raw else 10000
        return val - _CN_DIGIT.get(m.group(2), 0) + _CN_DIGIT.get(m.group(2), 0) * unit // 10
    return val


# ---------------- 槽位抽取 ----------------
_AMOUNT_RE = re.compile(
    r"(?:还|处理|拿|出|给|支付|承担|付)?\s*"
    r"((?:\d+(?:\.\d+)?)|(?:[一二两三四五六七八九十百千万零]{1,8}))\s*"
    r"(万|千)?\s*(?:元|块钱|块)"
)
_AMOUNT_BARE_WAN = re.compile(r"((?:\d+(?:\.\d+)?)|(?:[一二两三四五六七八九十零]{1,4}))\s*万(?!一)")
# 动词引导的裸数字金额："能还1000" / "先拿两千" / "出一千五"（避开 期/号/月/点 等量词）
_AMOUNT_VERB_RE = re.compile(
    r"(?:还|处理|拿|出|给|付|支付|承担)\s*(?:个|了)?\s*"
    r"((?:\d{2,7}(?:\.\d+)?)|(?:[一二两三四五六七八九十百千万零]{2,8}))"
    r"(?![\d期号日月点年个%])"
)


def parse_amount(text: str) -> float | None:
    """抽取金额（元）。支持：1000元 / 一千块 / 3千 / 1.5万 / 两千五。"""
    m = _AMOUNT_RE.search(text)
    if m:
        raw, unit = m.group(1), m.group(2)
        val = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else _fix_colloquial(raw, cn2int(raw))
        if val is None:
            return None
        if unit == "万":
            val *= 10000
        elif unit == "千":
            val *= 1000
        return float(val)
    m = _AMOUNT_BARE_WAN.search(text)
    if m:
        raw = m.group(1)
        val = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else cn2int(raw)
        return float(val) * 10000 if val else None
    m = _AMOUNT_VERB_RE.search(text)
    if m:
        raw = m.group(1)
        if re.fullmatch(r"\d+(\.\d+)?", raw):
            return float(raw)
        return float(_fix_colloquial(raw, cn2int(raw)) or 0) or None
    return None


_DATE_PATS = [
    r"(?:下下?个?月)(?:[一二三四五六七八九十\d]{1,3})?[号日]?(?:之?前)?",
    r"(?:这个?月|本月)?(?:[一二三四五六七八九十\d]{1,3})[号日](?:之?前)?",
    r"\d{1,2}月\d{1,2}[号日]",
    r"(?:月底|月初|月中|年底|年前)",
    r"(?:今天|明天|后天|大后天)",
    r"(?:下下?|这|本)?(?:周|星期|礼拜)[一二三四五六日天]",
    r"(?:发了?工资|工资到账)(?:之后|以后|后)?",
]
_DATE_RE = re.compile("(" + "|".join(_DATE_PATS) + ")")


def parse_date_text(text: str) -> str | None:
    """抽取还款/回访时间的口语表述（保留原文，便于复述与人工核对）。"""
    m = _DATE_RE.search(text)
    return m.group(1) if m else None


_CLOCK_RE = re.compile(r"((?:今天|明天|后天)?(?:上午|中午|下午|晚上)?\s*(?:[一二三四五六七八九十\d]{1,3})点(?:半|多|左右)?)")


def parse_callback_time(text: str) -> str | None:
    """抽取回访时间：明天下午3点 / 晚上8点 / 周五上午。"""
    m = _CLOCK_RE.search(text)
    if m:
        return m.group(1).strip()
    d = parse_date_text(text)
    if d:
        for p in ("上午", "中午", "下午", "晚上"):
            if p in text:
                return f"{d}{p}"
        return d
    return None


_INST_RE = re.compile(r"分?\s*((?:\d{1,2})|(?:[一二两三四五六七八九十]{1,3}))\s*期")
_PER_RE = re.compile(r"每期\s*((?:\d+(?:\.\d+)?)|(?:[一二两三四五六七八九十百千万零]{1,8}))\s*(万|千)?\s*(?:元|块)?")


def parse_installment(text: str) -> tuple[int | None, float | None]:
    """抽取分期：期数 + 每期金额。"""
    cnt = None
    m = _INST_RE.search(text)
    if m:
        raw = m.group(1)
        cnt = int(raw) if raw.isdigit() else cn2int(raw)
    per = None
    m = _PER_RE.search(text)
    if m:
        raw, unit = m.group(1), m.group(2)
        per = float(raw) if re.fullmatch(r"\d+(\.\d+)?", raw) else _fix_colloquial(raw, cn2int(raw))
        if per is not None:
            per = per * (10000 if unit == "万" else 1000 if unit == "千" else 1)
    return cnt, per


# ---------------- TTS 输出清洗 ----------------
_MD_RE = re.compile(r"[*#`>\[\]()【】\-—_~|]+")
_WS_RE = re.compile(r"\s+")


def sanitize_tts(text: str) -> str:
    """电话播报清洗：去换行/markdown/编号/多空白，统一句读。"""
    t = (text or "").replace("\n", "，").replace("\r", "")
    t = re.sub(r"^\s*\d+[\.、]\s*", "", t)
    t = _MD_RE.sub("", t)
    t = _WS_RE.sub("", t)
    t = re.sub(r"[，,]{2,}", "，", t).strip("，,")
    return t.strip()


_SENT_END = "。！？!?；;"


# ---------------- 否定语境检测 ----------------
# "我不可能下个月还1000" 不是承诺方案——金额/时间出现在否定语境时不得写入方案槽位
_NEG_PATTERNS = (
    "不可能", "没办法", "没法", "还不了", "还不上", "拿不出", "出不了", "付不了",
    "哪有", "哪来", "怎么可能", "别想", "不会还", "不想还", "没钱还", "凑不出",
    "做不到", "不现实", "没戏",
)


def is_negated_plan(text: str) -> bool:
    """判断金额/时间是否处于否定语境（否定词与数字同句出现即视为非承诺）。"""
    if not text:
        return False
    # 按句切分，仅当否定词与金额/时间表述同句时判否定
    for seg in re.split(r"[。！？!?；;，,]", text):
        if any(neg in seg for neg in _NEG_PATTERNS):
            # 仅当否定词与金额/时间表述同小句出现才判否定，
            # 避免误杀"实在拿不出太多，下个月还1000吧"这类让步+承诺
            if re.search(r"[\d一二两三四五六七八九十百千万]", seg) or "号" in seg or "月" in seg:
                return True
    return False


def first_sentence_cut(buf: str, max_chars: int) -> tuple[str, bool]:
    """流式累积中按首句/上限截断。返回 (文本, 是否已可定稿)。"""
    for i, ch in enumerate(buf):
        if ch in _SENT_END and i >= 5:
            return buf[: i + 1], True
    if len(buf) >= max_chars:
        cut = buf[:max_chars]
        for sep in ("，", ",", "、"):
            p = cut.rfind(sep)
            if p >= 8:
                return cut[:p] + "。", True
        return cut + "。", True
    return buf, False
