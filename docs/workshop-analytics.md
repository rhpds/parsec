# Workshop Analytics Tool

Pre-built analytics queries for RHDP workshop reporting and metrics.

## Overview

The workshop analytics tool provides 5 common reporting patterns for RHDP workshop data, eliminating the need to write complex SQL queries manually. All outputs are markdown-formatted and compatible with the `parsec_pptx.py` presentation generator.

## Features

- **Pre-built queries**: No SQL knowledge required
- **Standardized business logic**: Consistent metrics across all reports
- **Presentation-ready**: Markdown output compatible with parsec_pptx.py
- **Sales integration**: Pipeline touched, closed amount, win rate tracking
- **White glove tracking**: Stage-specific breakdowns (PROD, EVENT, DEV)

## Actions

| Action | Description | Use Case |
|--------|-------------|----------|
| `monthly_summary` | Comprehensive single month/quarter report | Regular monthly/quarterly reports |
| `quarterly_comparison` | YoY comparison with growth % | Executive presentations, trend analysis |
| `sales_influence` | Pipeline and closed deals metrics | ROI analysis, sales attribution |
| `white_glove_breakdown` | Stage and category deep-dive | White glove program analysis |
| `seat_utilization` | Efficiency and waste analysis | Resource optimization, cost analysis |

## Quick Start

### Monthly Summary

Get all metrics for a single period:

```python
query_workshop_analytics(
    action="monthly_summary",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

**Output includes**:
- Total workshops, seats, users, ratings
- White glove breakdown by stage
- Seat utilization metrics
- Failed provisions count

### Quarterly Comparison

Compare two periods with automatic growth calculations:

```python
query_workshop_analytics(
    action="quarterly_comparison",
    start_date="2026-04-01",
    end_date="2026-06-30",
    baseline_start_date="2025-04-01",
    baseline_end_date="2025-06-30"
)
```

**Output includes**:
- All summary metrics for both periods
- Growth percentages
- Side-by-side comparison table

### Sales Influence

Track workshop impact on sales pipeline:

```python
query_workshop_analytics(
    action="sales_influence",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

**Output includes**:
- Pipeline touched ($M)
- Closed amount ($M)
- Opportunities touched/closed
- Win rate %

## Database Schema

The tool queries these tables:

### workshop
- `id` - Workshop ID
- `created_at` - Creation timestamp (used for date filtering)
- `user_seats` - Total provisioned seats
- `assigned_users` - Actually used seats
- `experience_rating` - User satisfaction rating
- `white_glove` - Boolean flag for white glove workshops
- `stage_name` - Environment stage (PROD, EVENT, DEV)
- `failed_provisions` - Count of failed provisions
- `catalog_id` - FK to catalog_items

### catalog_items
- `id` - Catalog item ID
- `name` - Catalog item name
- `category` - Category (Workshops, Brand_Events, Labs, Demos, etc.)

### provision_sales
- `workshop_id` - FK to workshop
- `opportunity_id` - FK to sales_opportunity
- `opportunity_created_date` - When opportunity was created

### sales_opportunity
- `opportunity_id` - Opportunity ID
- `opportunity_name` - Opportunity name
- `amount` - Dollar amount
- `stage_name` - Sales stage (Discover, Qualify, Develop, Propose, Negotiate, Commit, Closed Won, Closed Lost)
- `close_date` - Close date

## Business Rules

### Q1 2025 White Glove Adjustment

Before the white glove toggle was implemented, **26 PROD white glove workshops** were manually tracked in Q1 2025.

When comparing Q1 2025 to other periods:
- System count: 32 PROD white glove
- Manual count: 26 additional
- **Actual total: 58 PROD white glove**

The tool outputs the system count (32) - users should add the manual count (26) when generating reports for Q1 2025.

### Seat Utilization Calculation

```
utilization_% = (assigned_users / user_seats) * 100
wasted_seats = user_seats - assigned_users
```

### Win Rate Calculation

```
win_rate_% = (closed_amount / pipeline_touched) * 100
```

Only includes opportunities in active stages: Discover, Qualify, Develop, Propose, Negotiate, Commit, Closed Won, Closed Lost

## Output Format

All actions return markdown with this structure:

```markdown
# Workshop Data Export - Q1 2026

## Summary

**Total Workshops**: 3,405
**Total User Seats**: 33,939
**Assigned Users**: 13,324
**Average Experience Rating**: 9.97
**White Glove Workshops**: 233
**Failed Provisions**: 1,246

## Sales Influence

**Pipeline Touched**: $1,109M
**Closed Amount**: $264M
**Opportunities Touched**: 370
**Win Rate**: 23.8%

## White Glove by Stage

**PROD White Glove**: 207
**EVENT White Glove**: 25
**DEV White Glove**: 1

## Key Metrics

- Seat Utilization: 39.26%
- Wasted Seats: 20,615 (60.7%)
```

## Integration

### With Presentation Generator

The markdown output is designed for `parsec_pptx.py` (rhpds-utils/workshop-reports):

```bash
# Save Parsec output to file
cat > q1_2026_report.md

# Generate PowerPoint
python parsec_pptx.py q1_2026_report.md \
    --baseline q1_2025_report.md \
    -o presentations/Q1_2026_Report.pptx
```

### With Other Tools

The markdown can also be:
- Converted to PDF (pandoc, markdown-pdf)
- Imported into Confluence/Notion
- Used in Slack/GitHub reports
- Processed by other markdown tools

## Security

All queries use `provision_db.execute_query()` with:
- SQL validation (only SELECT allowed)
- 500-row limit
- Read-only enforcement
- Safe parameter substitution

## Performance

Queries are optimized with:
- Indexed date range filters
- Aggregations pushed to database
- Limited result sets
- Efficient JOINs

Typical execution time: 1-3 seconds for quarterly data.

## Troubleshooting

### No data returned
- Check date range has workshops
- Verify date format is YYYY-MM-DD
- Confirm `created_at` timestamps are in expected timezone

### Missing sales data
- Check `provision_sales` and `sales_opportunity` tables exist
- Verify opportunities have `amount` and `stage_name` populated
- Confirm `opportunity_created_date` is within date range

### Missing white glove data
- Ensure `white_glove` boolean field exists in workshop table
- Check `stage_name` field is populated (PROD, EVENT, DEV)

## Examples

### Monthly Report (January 2026)

```
User: "Give me January 2026 workshop metrics"

Parsec:
query_workshop_analytics(
    action="monthly_summary",
    start_date="2026-01-01",
    end_date="2026-01-31"
)

Output: Comprehensive January report with all metrics
```

### Q2 2026 vs Q2 2025

```
User: "Compare Q2 2026 to last year"

Parsec:
query_workshop_analytics(
    action="quarterly_comparison",
    start_date="2026-04-01",
    end_date="2026-06-30",
    baseline_start_date="2025-04-01",
    baseline_end_date="2025-06-30"
)

Output: Side-by-side comparison with growth percentages
```

### Sales Impact Analysis

```
User: "Show workshop ROI for Q1 2026"

Parsec:
query_workshop_analytics(
    action="sales_influence",
    start_date="2026-01-01",
    end_date="2026-03-31"
)

Output: Pipeline touched, closed amount, win rate metrics
```

## Related

- **Tool implementation**: `src/tools/workshops.py`
- **Agent guidance**: `config/prompts/workshop_analytics.md`
- **Presentation generator**: https://github.com/rhpds/rhpds-utils/tree/main/workshop-reports
- **SQL templates**: https://github.com/rhpds/rhpds-utils/tree/main/workshop-reports/example_queries
