"""Base Classes to Use as MixIns Elsewhere in App"""

from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class SoftDeleteMixin(SQLModel):
    deleted_at: Optional[datetime] = Field(default=None, index=True)
