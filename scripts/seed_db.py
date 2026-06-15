"""把内置种子知识（Excel镜像）灌入 PostgreSQL。

用法: python scripts/seed_db.py   （读取 .env / 环境变量中的 PG_DSN）
"""
import asyncio
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.db.models import Base  # noqa: E402
from app.db.postgres import init_engine, session_factory  # noqa: E402


async def main() -> None:
    from app.db import models as M
    from app.knowledge import seed as S

    engine = init_engine()
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    sf = session_factory()
    pairs = [(M.SopNode, S.NODES), (M.IntentLabel, S.LABELS),
             (M.DecisionRoute, S.ROUTES), (M.Strategy, S.STRATEGIES),
             (M.ScriptTemplate, S.TEMPLATES), (M.PromptComponent, S.COMPONENTS),
             (M.ComplianceRule, S.COMPLIANCE_RULES), (M.QcRule, S.QC_RULES)]
    async with sf() as session:
        for model, rows in pairs:
            for row in rows:
                await session.merge(model(**row))
            print(f"  {model.__tablename__:<22} upsert {len(rows)} rows")
        await session.merge(M.Case(**S.DEMO_CASE))
        await session.commit()
    print("种子知识灌入完成。如服务已在运行，请调用 POST /api/v1/admin/knowledge/reload 热更新。")


if __name__ == "__main__":
    asyncio.run(main())
