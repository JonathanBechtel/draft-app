"""
SQLModels for players, to be stored in the database.
"""
from typing import Optional
from sqlmodel import Field as SQLField

from app.models.players import PlayerBase
from app.schemas.base import SoftDeleteMixin

class PlayerTable(PlayerBase, SoftDeleteMixin, table=True):
    id: Optional[int] = SQLField(default=None, primary_key=True)