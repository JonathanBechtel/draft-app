import asyncio
from sqlalchemy import text
from app.utils.db_async import SessionLocal


async def count_rows():
    async with SessionLocal() as session:
        tables = [
            "player_status",
            "combine_anthro",
            "combine_agility",
            "combine_shooting_results",
        ]
        for table in tables:
            res = await session.execute(text(f"SELECT COUNT(*) FROM {table}"))
            count = res.scalar()
            print(f"{table}: {count}")


if __name__ == "__main__":
    asyncio.run(count_rows())
