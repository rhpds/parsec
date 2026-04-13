# Workshop Analytics Guidance

When users ask about RHDP workshop metrics, reports, or analytics, use the `query_workshop_analytics` tool.

## When to Use

- "Give me Q1 2026 workshop data"
- "Compare Q2 2026 vs Q2 2025 workshops"
- "Show me workshop sales influence"
- "White glove workshop breakdown"
- "Seat utilization analysis"

## Available Actions

### 1. monthly_summary
Comprehensive single month or quarter report with all key metrics.

**When**: User wants a complete snapshot of a period (month or quarter)

**Includes**:
- Total workshops, seats, assigned users, average rating
- White glove count and breakdown by stage (PROD, EVENT, DEV)
- Seat utilization metrics
- Failed provisions

**Example**:
```
query_workshop_analytics(
    action="monthly_summary",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

### 2. quarterly_comparison
Year-over-year quarterly comparison with automatic growth calculations.

**When**: User wants to compare two periods (Q1 2026 vs Q1 2025, Q2 2026 vs Q2 2025, etc.)

**Includes**:
- All summary metrics for both periods
- Automatic growth percentage calculations
- Side-by-side comparison table

**Example**:
```
query_workshop_analytics(
    action="quarterly_comparison",
    start_date="2026-04-01",
    end_date="2026-06-30",
    baseline_start_date="2025-04-01",
    baseline_end_date="2025-06-30"
)
```

### 3. sales_influence
Pipeline touched, closed deals, and win rate metrics.

**When**: User asks about sales impact, ROI, or revenue attribution

**Includes**:
- Pipeline touched (total opportunity value in millions)
- Closed amount (won deals in millions)
- Opportunities touched and closed counts
- Win rate percentage

**Example**:
```
query_workshop_analytics(
    action="sales_influence",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

### 4. white_glove_breakdown
Detailed white glove analysis by stage and category.

**When**: User wants deep-dive on white glove performance

**Includes**:
- Breakdown by stage (PROD, EVENT, DEV)
- Category performance for white glove workshops
- Utilization comparisons vs standard workshops

**Example**:
```
query_workshop_analytics(
    action="white_glove_breakdown",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

### 5. seat_utilization
Efficiency and waste analysis by workshop type and category.

**When**: User asks about efficiency, waste, or resource optimization

**Includes**:
- White glove vs standard utilization
- Wasted seats by category
- Efficiency metrics

**Example**:
```
query_workshop_analytics(
    action="seat_utilization",
    start_date="2026-01-01",
    end_date="2026-03-31"
)
```

## Important Business Rules

### Q1 2025 White Glove Adjustment
Before the white glove toggle was implemented in the system, **26 PROD white glove workshops** were manually tracked in Q1 2025.

When comparing Q1 2025 to any other period:
- Query returns: 32 PROD white glove workshops
- Actual total: **58 PROD white glove** (32 from system + 26 manual)
- **Always note this adjustment in your response**

Example note: "Note: Q1 2025 PROD white glove count includes 26 manually tracked workshops before the toggle implementation."

### Date Format
Always use YYYY-MM-DD format:
- ✅ "2026-01-01"
- ❌ "1/1/2026"
- ❌ "Jan 1 2026"

### Quarter Date Ranges
- Q1: Jan 1 - Mar 31
- Q2: Apr 1 - Jun 30
- Q3: Jul 1 - Sep 30
- Q4: Oct 1 - Dec 31

## Output Format

All actions return markdown-formatted results that are compatible with the `parsec_pptx.py` presentation generator in the rhpds-utils repository.

Typical markdown structure:
```markdown
# Workshop Data Export - Q1 2026

## Summary
**Total Workshops**: 3,405
**Total User Seats**: 33,939
...

## Sales Influence
**Pipeline Touched**: $1,109M
**Closed Amount**: $264M
...

## White Glove by Stage
**PROD White Glove**: 207
**EVENT White Glove**: 25
...
```

## Common Workflows

### Monthly Report
User: "Give me January 2026 workshop metrics"

```
query_workshop_analytics(
    action="monthly_summary",
    start_date="2026-01-01",
    end_date="2026-01-31"
)
```

### Quarterly Comparison
User: "Compare Q1 2026 vs Q1 2025"

```
query_workshop_analytics(
    action="quarterly_comparison",
    start_date="2026-01-01",
    end_date="2026-03-31",
    baseline_start_date="2025-01-01",
    baseline_end_date="2025-03-31"
)
```

### Sales Impact
User: "Show me workshop sales influence for Q2"

```
query_workshop_analytics(
    action="sales_influence",
    start_date="2026-04-01",
    end_date="2026-06-30"
)
```

## Integration with Presentation Generator

After getting markdown output from workshop analytics:

1. Save markdown to a file (e.g., `q1_2026_report.md`)
2. Use `parsec_pptx.py` to generate PowerPoint:
   ```bash
   python parsec_pptx.py q1_2026_report.md --baseline q1_2025_report.md
   ```

This creates a presentation with:
- Title slide
- Executive summary
- Summary comparison tables
- Monthly breakdowns
- White glove charts
- Category performance
- Sales influence (if available)

## Error Handling

If a query fails:
- Check date format (must be YYYY-MM-DD)
- Verify baseline dates are provided for quarterly_comparison
- Ensure database tables exist (workshop, catalog_items, provision_sales, sales_opportunity)

If no data is returned:
- Verify date range has workshops
- Check that white_glove boolean field exists
- Confirm sales tables are populated

## Related Tools

- `query_provisions_db`: For custom SQL queries beyond pre-built actions
- `db_describe_table`: To check workshop table schema
- `db_read_knowledge`: For workshop domain knowledge and business rules
