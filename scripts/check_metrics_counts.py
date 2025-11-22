import asyncio
from sqlalchemy import text
from app.utils.db_async import SessionLocal


async def check_counts():
    async with SessionLocal() as session:
        res_snapshots = await session.execute(
            text("SELECT count(*) FROM metric_snapshots")
        )
        count_snapshots = res_snapshots.scalar()

        res_values = await session.execute(
            text("SELECT count(*) FROM player_metric_values")
        )
        count_values = res_values.scalar()

        print(f"metric_snapshots: {count_snapshots}")
        print(f"player_metric_values: {count_values}")


if __name__ == "__main__":
    asyncio.run(check_counts())
