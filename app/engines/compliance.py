"""合规规则引擎（对应 09_合规规则库 CR001~CR010）。

执行顺序：
1. 动态规则（最高优先）：
   CR001 未确认本人 -> 拦截平台名/委托方名/案号/金额数字/欠款类关键词；
   CR005 对方为第三方 -> 同上（不含姓名，N005 需要用姓名确认是否认识）；
2. 静态正则规则（CR002 冒充 / CR003 威胁 / CR004 承诺 / CR009 法律结论 / CR010 验证码转账）；
命中即整句替换为该规则的预审修复话术（修复话术本身已合规），并记录违规供质检（QC001/QC006）。
行为类规则（CR006/007/008）由决策路由保障，质检兜底核查。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# 金额数字（阿拉伯数字 + 货币单位）
_AMOUNT_PAT = re.compile(r"\d+(?:\.\d+)?\s*(?:元|块钱|块|万元|万)")


@dataclass
class ComplianceResult:
    passed: bool = True
    text: str = ""
    violations: list[dict] = field(default_factory=list)
    repaired: bool = False


class ComplianceEngine:
    def check(self, snap, text: str, slots: dict, case: dict | None) -> ComplianceResult:
        out = text or ""
        violations: list[dict] = []
        identity = bool(slots.get("identity_confirmed"))
        # 已确认本人时不再当第三方处理：避免上一轮 NOT_SELF 误判残留触发 CR005，
        # 把已经过方案确认门控的金额复述吞成"为保护隐私我不便说明具体内容"。
        third_party = bool(slots.get("not_self")) and not identity

        # ---- 动态隐私规则 ----
        for rule in snap.compliance_dynamic:
            kind = rule.get("dynamic_kind")
            if kind == "PRIVACY_PRE_IDENTITY" and identity:
                continue
            if kind == "THIRD_PARTY" and not third_party:
                continue
            hit = self._dynamic_hit(rule, out, case)
            if hit:
                violations.append({"rule_id": rule["rule_id"],
                                   "rule_type": rule["rule_type"], "hit": hit})
                out = rule.get("repair_text") or "为保护信息安全，我需要先确认是否本人。"
                break

        # ---- 静态正则规则 ----
        for rule, patterns in snap.compliance_static:
            matched = None
            for p in patterns:
                if p.search(out):
                    matched = p.pattern
                    break
            if matched:
                violations.append({"rule_id": rule["rule_id"],
                                   "rule_type": rule["rule_type"], "hit": matched})
                out = rule.get("repair_text") or "我会如实记录您的意见。"
                break  # 修复话术为预审安全文本，单次替换即可

        return ComplianceResult(passed=not violations, text=out,
                                violations=violations, repaired=bool(violations))

    @staticmethod
    def _dynamic_hit(rule: dict, text: str, case: dict | None) -> str | None:
        for kw in rule.get("trigger_keywords") or []:
            if kw and kw in text:
                return kw
        if _AMOUNT_PAT.search(text):
            return "金额数字"
        for f in ("platform_name", "creditor_name", "case_id"):
            v = (case or {}).get(f)
            if v and str(v) in text:
                return f
        return None
