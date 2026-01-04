from datetime import date, timedelta
from typing import Optional

from pydantic import computed_field
from sqlmodel import SQLModel


class PlayerSearchResult(SQLModel):
    """Response model for player search results."""

    id: int
    display_name: Optional[str] = None
    slug: Optional[str] = None
    school: Optional[str] = None


class PlayerProfileRead(SQLModel):
    """Response model for player profile (bio section on player detail page).

    Contains raw fields from database joins and computed properties for
    formatted display values.
    """

    # Core identity
    id: int
    slug: Optional[str] = None
    display_name: Optional[str] = None

    # Bio fields from PlayerMaster
    birthdate: Optional[date] = None
    birth_city: Optional[str] = None
    birth_state_province: Optional[str] = None
    birth_country: Optional[str] = None
    school: Optional[str] = None
    high_school: Optional[str] = None
    shoots: Optional[str] = None

    # From PlayerStatus (joined)
    position_code: Optional[str] = None
    raw_position: Optional[str] = None
    height_in: Optional[int] = None
    weight_lb: Optional[int] = None

    # From CombineAnthro (most recent)
    wingspan_in: Optional[float] = None

    # Photo URL (computed at runtime from filesystem, not stored in DB)
    photo_url: Optional[str] = None

    @computed_field  # type: ignore[misc]
    @property
    def age_formatted(self) -> Optional[str]:
        """Calculate age as 'Xy Xm Xd' format from birthdate."""
        if not self.birthdate:
            return None

        today = date.today()
        years = today.year - self.birthdate.year
        months = today.month - self.birthdate.month
        days = today.day - self.birthdate.day

        # Adjust for negative days
        if days < 0:
            months -= 1
            # Get days in previous month
            prev_month = today.replace(day=1) - timedelta(days=1)
            days += prev_month.day

        # Adjust for negative months
        if months < 0:
            years -= 1
            months += 12

        return f"{years}y {months}m {days}d"

    @computed_field  # type: ignore[misc]
    @property
    def height_formatted(self) -> Optional[str]:
        r"""Format height as feet'inches" (e.g., 6'9")."""
        if not self.height_in:
            return None
        feet = self.height_in // 12
        inches = self.height_in % 12
        return f"{feet}'{inches}\""

    @computed_field  # type: ignore[misc]
    @property
    def weight_formatted(self) -> Optional[str]:
        """Format weight with lbs suffix."""
        if not self.weight_lb:
            return None
        return f"{self.weight_lb} lbs"

    @computed_field  # type: ignore[misc]
    @property
    def wingspan_formatted(self) -> Optional[str]:
        r"""Format wingspan as feet'inches" with half-inch precision."""
        if not self.wingspan_in:
            return None
        # Round to nearest half inch for display
        rounded = round(self.wingspan_in * 2) / 2
        feet = int(rounded) // 12
        inches = rounded % 12
        # Format inches: show .5 if present, otherwise whole number
        if inches == int(inches):
            return f"{feet}'{int(inches)}\""
        else:
            return f"{feet}'{inches}\""

    @computed_field  # type: ignore[misc]
    @property
    def hometown(self) -> Optional[str]:
        """Compose hometown from city and state/country.

        Returns 'City, State' for US players, 'City, Country' for international,
        or just 'Country' if only country is known and it's not USA.
        """
        parts = []
        if self.birth_city:
            parts.append(self.birth_city)
        if self.birth_state_province:
            parts.append(self.birth_state_province)
        elif self.birth_country and self.birth_country != "USA":
            parts.append(self.birth_country)

        if parts:
            return ", ".join(parts)

        return None

    @computed_field  # type: ignore[misc]
    @property
    def position(self) -> Optional[str]:
        """Return position code or fallback to raw_position."""
        return self.position_code or self.raw_position
