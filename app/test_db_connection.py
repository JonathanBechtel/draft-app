import os
import asyncio
import re
from sqlalchemy import text
from dotenv import load_dotenv
from sqlalchemy.ext.asyncio import create_async_engine


load_dotenv()

print("DATABASE_URL:", os.getenv('DATABASE_URL'))
async def async_main() -> None:
    engine = create_async_engine(re.sub(r'^postgresql:', 'postgresql+asyncpg:', os.getenv('DATABASE_URL')), echo=True)
    async with engine.connect() as conn:
        result = await conn.execute(text("select 'hello world'"))
        print(result.fetchall())
    await engine.dispose()

asyncio.run(async_main())