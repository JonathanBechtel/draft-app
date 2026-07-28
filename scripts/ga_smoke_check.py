"""Quick smoke test for the GA4 service account.

Usage:
    GOOGLE_APPLICATION_CREDENTIALS=~/.config/draftguru/ga-sa.json \
    GA4_PROPERTY_ID=123456789 \
    python scripts/ga_smoke_check.py

Prints active users + pageviews per day for the last 7 days.
"""

from __future__ import annotations

import os
import sys

from google.analytics.data_v1beta import BetaAnalyticsDataClient
from google.analytics.data_v1beta.types import (
    DateRange,
    Dimension,
    Metric,
    RunReportRequest,
)


def main() -> int:
    property_id = os.environ.get("GA4_PROPERTY_ID")
    if not property_id:
        print(
            "ERROR: set GA4_PROPERTY_ID (the 9-digit GA4 property ID)", file=sys.stderr
        )
        return 2
    creds = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS")
    if not creds:
        print(
            "ERROR: set GOOGLE_APPLICATION_CREDENTIALS to the service account JSON path",
            file=sys.stderr,
        )
        return 2

    client = BetaAnalyticsDataClient()
    request = RunReportRequest(
        property=f"properties/{property_id}",
        dimensions=[Dimension(name="date")],
        metrics=[Metric(name="activeUsers"), Metric(name="screenPageViews")],
        date_ranges=[DateRange(start_date="7daysAgo", end_date="today")],
    )
    response = client.run_report(request)

    print(f"{'date':<10}  {'activeUsers':>12}  {'pageviews':>10}")
    print("-" * 38)
    for row in response.rows:
        date = row.dimension_values[0].value
        users = row.metric_values[0].value
        views = row.metric_values[1].value
        print(f"{date:<10}  {users:>12}  {views:>10}")
    if not response.rows:
        print("(no rows returned — property may be empty for this window)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
