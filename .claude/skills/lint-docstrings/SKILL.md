---
name: lint-docstrings
description: Review and fix docstrings to follow Google-style conventions. Use when asked to check docstrings, format docstrings, or lint docstrings.
allowed-tools: Read, Grep, Glob, Edit
---

# Docstring Linting

Review files for docstring compliance and fix any issues.

## Instructions

1. Find the target files (specified by user, or recently modified files)
2. Check each public function, class, and method for proper docstrings
3. Report issues and fix them

## Google-Style Docstring Format

### Functions

```python
def calculate_percentile(value: float, distribution: list[float]) -> int:
    """Calculate the percentile rank of a value within a distribution.

    Args:
        value: The value to rank.
        distribution: Sorted list of values forming the reference distribution.

    Returns:
        Percentile rank from 0-100.

    Raises:
        ValueError: If distribution is empty.
    """
```

Rules:
- First line: imperative summary ending with period
- Blank line before Args/Returns/Raises sections
- Args: one line per param, name followed by colon and description
- Returns: describe the return value (omit if None)
- Raises: list exceptions that may be raised

### Classes

```python
class PlayerMetrics:
    """Container for computed player statistics.

    Holds percentile rankings and z-scores for a player across
    multiple statistical categories.

    Attributes:
        player_id: Unique identifier for the player.
        percentiles: Dict mapping metric names to percentile values.
        computed_at: Timestamp when metrics were calculated.
    """
```

### Simple Functions (no args)

```python
def get_current_timestamp() -> datetime:
    """Return the current UTC timestamp."""
```

One-liner docstrings: opening and closing quotes on same line.

## Test Docstring Format

Tests use a custom format with Fixtures, Scenario, and Expected sections:

```python
def test_player_slug_generation(db_session):
    """Slug is generated from player name on create.

    Fixtures:
        db_session: Clean database session.

    Scenario:
        Create player with name "LeBron James".

    Expected:
        Player.slug equals "lebron-james".
    """
```

```python
def test_percentile_edge_case_empty_distribution():
    """Percentile calculation handles empty distribution.

    Scenario:
        Call calculate_percentile with empty list.

    Expected:
        Raises ValueError with message about empty distribution.
    """
```

Rules for tests:
- First line: concise description of what's being tested
- Fixtures: list test fixtures/dependencies used (if any)
- Scenario: describe the setup and action
- Expected: describe the expected outcome

## Checklist

When reviewing, check:
- [ ] All public functions have docstrings
- [ ] First line is imperative mood ("Return", "Calculate", not "Returns", "Calculates")
- [ ] First line ends with period
- [ ] Args section documents all parameters
- [ ] Returns section present (unless function returns None)
- [ ] One-liners fit on single line with quotes
- [ ] Test docstrings follow Fixtures/Scenario/Expected format
