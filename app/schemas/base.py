"""Base classes to use as mixins elsewhere in app."""

from sqlmodel import SQLModel, Field
from typing import Optional
from datetime import datetime


class SoftDeleteMixin(SQLModel):
    deleted_at: Optional[datetime] = Field(default=None, index=True)
