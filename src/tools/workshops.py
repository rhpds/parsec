"""Tool: query_workshop_analytics — Pre-built workshop reporting queries.

Provides common workshop analytics patterns and quarterly/monthly reports
without requiring users to write complex SQL. Wraps the provision_db tool
with workshop-specific business logic.
"""

import logging
from typing import Literal

from src.tools.provision_db import execute_query

logger = logging.getLogger(__name__)


async def get_workshop_analytics(
    action: Literal[
        "monthly_summary",
        "quarterly_comparison",
        "sales_influence",
        "white_glove_breakdown",
        "seat_utilization",
    ],
    start_date: str,
    end_date: str,
    baseline_start_date: str | None = None,
    baseline_end_date: str | None = None,
) -> dict:
    """Execute pre-built workshop analytics queries.

    Args:
        action: The type of report to generate
        start_date: Start date (YYYY-MM-DD format)
        end_date: End date (YYYY-MM-DD format)
        baseline_start_date: Baseline period start (for comparisons)
        baseline_end_date: Baseline period end (for comparisons)

    Returns:
        dict with markdown-formatted results ready for presentation generation
    """
    if action == "monthly_summary":
        return await _monthly_summary(start_date, end_date)
    elif action == "quarterly_comparison":
        if not baseline_start_date or not baseline_end_date:
            return {"error": "Quarterly comparison requires baseline dates"}
        return await _quarterly_comparison(
            start_date, end_date, baseline_start_date, baseline_end_date
        )
    elif action == "sales_influence":
        return await _sales_influence(start_date, end_date)
    elif action == "white_glove_breakdown":
        return await _white_glove_breakdown(start_date, end_date)
    elif action == "seat_utilization":
        return await _seat_utilization(start_date, end_date)

    return {"error": f"Unknown action: {action}"}


async def _monthly_summary(start_date: str, end_date: str) -> dict:
    """Generate monthly workshop summary report."""
    sql = f"""
WITH target_month AS (
  SELECT
    '{start_date}'::date AS month_start,
    '{end_date}'::date AS month_end
),
monthly_stats AS (
  SELECT
    COUNT(DISTINCT w.id) AS total_workshops,
    SUM(w.user_seats) AS total_seats,
    SUM(w.assigned_users) AS assigned_users,
    AVG(w.experience_rating) AS avg_rating,
    SUM(CASE WHEN w.failed_provisions > 0 THEN 1 ELSE 0 END) AS failed_provisions,
    COUNT(DISTINCT w.id) FILTER (WHERE w.white_glove = true) AS white_glove_count
  FROM workshop w, target_month tm
  WHERE w.created_at >= tm.month_start
    AND w.created_at < tm.month_end + interval '1 day'
),
white_glove_by_stage AS (
  SELECT
    w.stage_name,
    COUNT(DISTINCT w.id) AS workshop_count
  FROM workshop w, target_month tm
  WHERE w.created_at >= tm.month_start
    AND w.created_at < tm.month_end + interval '1 day'
    AND w.white_glove = true
  GROUP BY w.stage_name
)
SELECT
  '# Workshop Data Export - {start_date} to {end_date}' AS report_header,
  '## Summary' AS section1,
  '**Total Workshops**: ' || total_workshops AS metric1,
  '**Total User Seats**: ' || total_seats AS metric2,
  '**Assigned Users**: ' || assigned_users AS metric3,
  '**Average Experience Rating**: ' || ROUND(avg_rating::numeric, 2) AS metric4,
  '**White Glove Workshops**: ' || white_glove_count AS metric5,
  '**Failed Provisions**: ' || failed_provisions AS metric6,
  '## White Glove by Stage' AS section2,
  '**PROD White Glove**: ' || COALESCE((SELECT workshop_count FROM white_glove_by_stage WHERE stage_name = 'PROD'), 0) AS wg_prod,
  '**EVENT White Glove**: ' || COALESCE((SELECT workshop_count FROM white_glove_by_stage WHERE stage_name = 'EVENT'), 0) AS wg_event,
  '**DEV White Glove**: ' || COALESCE((SELECT workshop_count FROM white_glove_by_stage WHERE stage_name = 'DEV'), 0) AS wg_dev,
  '## Key Metrics' AS section3,
  '- Seat Utilization: ' || ROUND((assigned_users::numeric / NULLIF(total_seats, 0)::numeric * 100), 2) || '%' AS util_pct,
  '- Wasted Seats: ' || (total_seats - assigned_users) || ' (' || ROUND(((total_seats - assigned_users)::numeric / NULLIF(total_seats, 0)::numeric * 100), 1) || '%)' AS wasted
FROM monthly_stats
"""
    return await execute_query(sql)


async def _sales_influence(start_date: str, end_date: str) -> dict:
    """Generate sales influence report for workshop-touched opportunities."""
    sql = f"""
WITH target_period AS (
  SELECT
    '{start_date}'::date AS period_start,
    '{end_date}'::date AS period_end
),
sales_influence AS (
  SELECT
    SUM(so.amount) FILTER (WHERE so.stage_name IN ('Discover', 'Qualify', 'Develop', 'Propose', 'Negotiate', 'Commit', 'Closed Won', 'Closed Lost')) AS touched_amount,
    SUM(so.amount) FILTER (WHERE so.stage_name = 'Closed Won') AS closed_amount,
    COUNT(DISTINCT so.opportunity_id) FILTER (WHERE so.stage_name IN ('Discover', 'Qualify', 'Develop', 'Propose', 'Negotiate', 'Commit', 'Closed Won', 'Closed Lost')) AS touched_count,
    COUNT(DISTINCT so.opportunity_id) FILTER (WHERE so.stage_name = 'Closed Won') AS closed_count
  FROM provision_sales ps, target_period tp
  JOIN sales_opportunity so ON ps.opportunity_id = so.opportunity_id
  WHERE ps.opportunity_created_date >= tp.period_start
    AND ps.opportunity_created_date < tp.period_end + interval '1 day'
)
SELECT
  '## Sales Influence' AS section_header,
  '**Pipeline Touched**: $' || ROUND(touched_amount / 1000000, 1) || 'M' AS pipeline,
  '**Closed Amount**: $' || ROUND(closed_amount / 1000000, 1) || 'M' AS closed,
  '**Opportunities Touched**: ' || touched_count AS opps_touched,
  '**Opportunities Closed**: ' || closed_count AS opps_closed,
  '**Win Rate**: ' || ROUND(closed_amount::numeric / NULLIF(touched_amount, 0)::numeric * 100, 1) || '%' AS win_rate
FROM sales_influence
"""
    return await execute_query(sql)


async def _white_glove_breakdown(start_date: str, end_date: str) -> dict:
    """Get detailed white glove workshop breakdown by stage and category."""
    sql = f"""
SELECT
  w.stage_name AS "Stage",
  ci.category AS "Category",
  COUNT(DISTINCT w.id) AS "White Glove Count",
  SUM(w.user_seats) AS "User Seats",
  SUM(w.assigned_users) AS "Assigned Users",
  ROUND(AVG(w.experience_rating)::numeric, 2) AS "Avg Rating"
FROM workshop w
JOIN catalog_items ci ON w.catalog_id = ci.id
WHERE w.white_glove = true
  AND w.created_at >= '{start_date}'::date
  AND w.created_at < '{end_date}'::date + interval '1 day'
GROUP BY w.stage_name, ci.category
ORDER BY "White Glove Count" DESC
LIMIT 50
"""
    return await execute_query(sql)


async def _seat_utilization(start_date: str, end_date: str) -> dict:
    """Analyze seat utilization and waste by workshop type and category."""
    sql = f"""
SELECT
  CASE WHEN w.white_glove THEN 'White Glove' ELSE 'Standard' END AS "Workshop Type",
  ci.category AS "Category",
  COUNT(DISTINCT w.id) AS "Workshops",
  SUM(w.user_seats) AS "Total Seats",
  SUM(w.assigned_users) AS "Assigned",
  SUM(w.user_seats - w.assigned_users) AS "Wasted",
  ROUND((SUM(w.assigned_users)::numeric / NULLIF(SUM(w.user_seats), 0)::numeric * 100), 2) || '%' AS "Utilization"
FROM workshop w
JOIN catalog_items ci ON w.catalog_id = ci.id
WHERE w.created_at >= '{start_date}'::date
  AND w.created_at < '{end_date}'::date + interval '1 day'
GROUP BY CASE WHEN w.white_glove THEN 'White Glove' ELSE 'Standard' END, ci.category
ORDER BY "Wasted" DESC
LIMIT 50
"""
    return await execute_query(sql)


async def _quarterly_comparison(
    current_start: str,
    current_end: str,
    baseline_start: str,
    baseline_end: str,
) -> dict:
    """Generate year-over-year quarterly comparison."""
    sql = f"""
WITH current_quarter AS (
  SELECT
    COUNT(DISTINCT w.id) AS total_workshops,
    SUM(w.user_seats) AS total_seats,
    SUM(w.assigned_users) AS assigned_users,
    AVG(w.experience_rating) AS avg_rating,
    COUNT(DISTINCT w.id) FILTER (WHERE w.white_glove = true) AS white_glove_count
  FROM workshop w
  WHERE w.created_at >= '{current_start}'::date
    AND w.created_at < '{current_end}'::date + interval '1 day'
),
baseline_quarter AS (
  SELECT
    COUNT(DISTINCT w.id) AS total_workshops,
    SUM(w.user_seats) AS total_seats,
    SUM(w.assigned_users) AS assigned_users,
    AVG(w.experience_rating) AS avg_rating,
    COUNT(DISTINCT w.id) FILTER (WHERE w.white_glove = true) AS white_glove_count
  FROM workshop w
  WHERE w.created_at >= '{baseline_start}'::date
    AND w.created_at < '{baseline_end}'::date + interval '1 day'
)
SELECT
  '## Quarterly Comparison' AS section_header,
  '| Metric | Baseline | Current | Growth |' AS table_header,
  '|--------|----------|---------|--------|' AS table_divider,
  '| Workshops | ' || b.total_workshops || ' | ' || c.total_workshops || ' | +' || ROUND(((c.total_workshops::numeric / NULLIF(b.total_workshops, 0)::numeric - 1) * 100), 0) || '% |' AS row1,
  '| User Seats | ' || b.total_seats || ' | ' || c.total_seats || ' | +' || ROUND(((c.total_seats::numeric / NULLIF(b.total_seats, 0)::numeric - 1) * 100), 0) || '% |' AS row2,
  '| White Glove | ' || b.white_glove_count || ' | ' || c.white_glove_count || ' | +' || ROUND(((c.white_glove_count::numeric / NULLIF(b.white_glove_count, 0)::numeric - 1) * 100), 0) || '% |' AS row3,
  '| Avg Rating | ' || ROUND(b.avg_rating::numeric, 2) || ' | ' || ROUND(c.avg_rating::numeric, 2) || ' | +' || ROUND(((c.avg_rating::numeric / NULLIF(b.avg_rating, 0)::numeric - 1) * 100), 0) || '% |' AS row4
FROM current_quarter c, baseline_quarter b
"""
    return await execute_query(sql)
