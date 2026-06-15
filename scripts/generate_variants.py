"""离线生成话术同义变体草稿（人工审核后入库，绝不自动生效）。

商用形态建议：变体池为主、实时LLM为辅——多样性由离线生成+人审保证，
运行时按通话轮换（零LLM延迟、合规100%预审）。

用法:
  python scripts/generate_variants.py                 # 为所有可改写模板各生成5条
  python scripts/generate_variants.py TPL_NO_MONEY_001 TPL_PLAN_ASK_001 -n 8

输出 variants_draft.json / variants_draft.md，人工审核后通过
  PUT /api/v1/admin/knowledge/templates/{id}  body: {"variants": [...]}
写入，再 POST /api/v1/admin/knowledge/reload 生效。
"""
from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import get_settings  # noqa: E402
from app.engines.llm_client import LLMClient  # noqa: E402
from app.knowledge import seed  # noqa: E402

PROMPT = """你是电话调解话术编辑。为下面这条调解员话术生成{n}条同义变体，要求：
1. 含义、语气、礼貌程度与原句一致，口语化、适合电话播报，每条不超过30字；
2. 原句中的 {{变量}} 占位符必须原样保留，不得增删改名；
3. 严禁出现威胁、施压、冒充司法机关、承诺减免或处理结果的表述；
4. 只输出JSON字符串数组，不要解释。

原句：{text}"""


async def gen(template_ids: list[str], n: int) -> None:
    s = get_settings()
    if not s.llm_api_key:
        print("未配置 LLM_API_KEY，无法生成。请在 .env 中配置后重试。")
        sys.exit(1)
    llm = LLMClient(s)
    templates = {t["template_id"]: t for t in seed.TEMPLATES}
    if not template_ids:
        template_ids = [t["template_id"] for t in seed.TEMPLATES
                        if t.get("need_rewrite") or t.get("variants")]
    drafts: dict[str, dict] = {}
    try:
        for tid in template_ids:
            t = templates.get(tid)
            if not t:
                print(f"  跳过未知模板 {tid}")
                continue
            prompt = PROMPT.format(n=n, text=t["template_text"])
            out = await llm.complete([{"role": "user", "content": prompt}],
                                     max_tokens=600, temperature=0.7)
            variants: list[str] = []
            if out:
                try:
                    data = json.loads(out.replace("```json", "").replace("```", "").strip())
                    variants = [str(v).strip() for v in data if str(v).strip()]
                except json.JSONDecodeError:
                    print(f"  {tid}: LLM输出非JSON，已跳过")
            drafts[tid] = {"original": t["template_text"], "variants": variants}
            print(f"  {tid}: 生成 {len(variants)} 条")
    finally:
        await llm.aclose()

    Path("variants_draft.json").write_text(
        json.dumps(drafts, ensure_ascii=False, indent=2), encoding="utf-8")
    md = ["# 话术变体草稿（待人工审核）\n"]
    for tid, d in drafts.items():
        md.append(f"## {tid}\n原句：{d['original']}\n")
        md += [f"- [ ] {v}" for v in d["variants"]]
        md.append("")
    Path("variants_draft.md").write_text("\n".join(md), encoding="utf-8")
    print("\n已输出 variants_draft.json / variants_draft.md")
    print("人工勾选审核后，调用 PUT /api/v1/admin/knowledge/templates/{id} 写入 variants 字段。")


if __name__ == "__main__":
    args = [a for a in sys.argv[1:] if not a.startswith("-")]
    n = 5
    if "-n" in sys.argv:
        n = int(sys.argv[sys.argv.index("-n") + 1])
        args = [a for a in args if a != str(n)]
    asyncio.run(gen(args, n))
