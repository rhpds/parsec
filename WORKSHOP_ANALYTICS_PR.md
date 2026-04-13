# Workshop Analytics Tool for Parsec

## Summary

Add dedicated workshop analytics tool with pre-built queries for common workshop reporting patterns. This eliminates manual SQL writing and provides standardized, presentation-ready outputs for quarterly and monthly workshop reports.

## Changes

### New Files
- `src/tools/workshops.py` - Workshop analytics tool with 5 pre-built actions

### Tool Actions

1. **monthly_summary** - Comprehensive single month report
   - Total workshops, seats, users, rating
   - White glove by stage (PROD, EVENT, DEV)
   - Seat utilization metrics
   - Failed provisions

2. **quarterly_comparison** - Year-over-year quarterly comparison
   - All monthly metrics for both periods
   - Automatic growth percentage calculations
   - Side-by-side comparison table

3. **sales_influence** - Sales impact metrics
   - Pipeline touched ($M)
   - Closed amount ($M)
   - Opportunities touched/closed
   - Win rate percentage

4. **white_glove_breakdown** - Detailed white glove analysis
   - By stage and category
   - Seat utilization per stage
   - White glove vs standard comparison

5. **seat_utilization** - Efficiency analysis
   - Waste by workshop type
   - Category-level utilization
   - White glove vs standard breakdown

## Usage Example

```
User: "Give me a Q1 2026 workshop summary"

Parsec Agent:
  query_workshop_analytics(
    action="monthly_summary",
    start_date="2026-01-01",
    end_date="2026-03-31"
  )

Output: Markdown-formatted report ready for parsec_pptx.py presentation generation
```

## Benefits

- **Self-service**: No SQL knowledge required
- **Consistent**: Standardized business logic (white glove tracking, seat calculations, sales metrics)
- **Integration-ready**: Markdown output compatible with existing parsec_pptx.py presentation generator
- **Extensible**: Easy to add new analytics actions

## Testing

All queries use the existing `provision_db.execute_query()` wrapper, maintaining:
- SQL validation and security model
- 500-row limit
- Read-only enforcement
- Markdown output format

## Next Steps (Future PRs)

1. Add tool definition to `src/agent/tool_definitions.py`
2. Create `config/prompts/workshop_agent.md` for agent guidance
3. Add integration tests
4. Document in user-facing docs

## Related

- Workshop presentation generator: https://github.com/rhpds/rhpds-utils/tree/main/workshop-reports
- SQL query templates: https://github.com/rhpds/rhpds-utils/tree/main/workshop-reports/example_queries

---

**Ready for review!** This is a focused PR adding just the core workshop analytics tool. Tool definition and agent prompts can follow in subsequent PRs to keep changes atomic.
