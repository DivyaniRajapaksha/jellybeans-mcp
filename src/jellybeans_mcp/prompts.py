"""Reusable prompt templates for common management reports."""

from mcp.server import MCPServer


def register(mcp: MCPServer) -> None:
    @mcp.prompt(title="Daily summary")
    def daily_summary(day: str = "today") -> str:
        """End-of-day summary for one date (YYYY-MM-DD or 'today')."""
        return (
            f"Prepare the Jellybeans end-of-day summary for {day}. Use start_date = end_date = that day.\n"
            "1. get_revenue_summary: net revenue, bills, average bill, cash vs bank transfer.\n"
            "2. get_visit_stats: children (sessions + group bookings), average play time.\n"
            "3. get_peak_hours: the busiest hour.\n"
            "4. get_exceptions: list any cancellations, voids, write-offs or corrections with reasons.\n"
            "Keep it to a short bulleted report with amounts in LKR, and flag anything unusual."
        )

    @mcp.prompt(title="Weekly report")
    def weekly_report(end_date: str = "today") -> str:
        """Week-over-week performance report for the 7 days ending on end_date."""
        return (
            f"Write the Jellybeans weekly report for the 7 days ending {end_date}, compared with the 7 days before.\n"
            "Call get_revenue_summary, get_visit_stats and get_customer_insights for both weeks, plus "
            "get_revenue_trend (day) for the current week, get_package_performance, get_peak_hours and "
            "get_cashier_performance for the current week.\n"
            "Structure: headline numbers with % change, what drove the change, best/worst days, package mix, "
            "staffing implications from peak hours, and 2-3 concrete recommendations."
        )

    @mcp.prompt(title="Compare periods")
    def compare_periods(a_start: str, a_end: str, b_start: str, b_end: str) -> str:
        """Compare two date ranges (e.g. this month vs last month)."""
        return (
            f"Compare Jellybeans performance for period A ({a_start} to {a_end}) "
            f"with period B ({b_start} to {b_end}).\n"
            "For each period call get_revenue_summary, get_visit_stats, get_revenue_breakdown and "
            "get_customer_insights. Present a side-by-side table (A, B, change, % change) and explain the "
            "biggest differences. Note that periods of different lengths should be compared per day."
        )

    @mcp.prompt(title="Monthly report")
    def monthly_report(month: str = "this month") -> str:
        """Performance report for one calendar month (YYYY-MM or 'this month' / 'last month')."""
        return (
            f"Write the Jellybeans monthly report for {month}.\n"
            "1. get_period_performance(granularity='month') ending in that month (13 months, so last year's same "
            "month is included): revenue, bills, average bill, children, new customers, and % change vs the "
            "previous month and vs the same month last year.\n"
            "2. For that month: get_revenue_breakdown, get_package_performance, get_peak_hours, "
            "get_customer_insights, and get_revenue_trend(granularity='day') for the best and worst days.\n"
            "If the month is still in progress (partial), say so and compare per day where it matters. "
            "Finish with 2-3 concrete recommendations. Amounts in LKR."
        )

    @mcp.prompt(title="Yearly report")
    def yearly_report(year: str = "this year") -> str:
        """Performance report for one calendar year (Jan-Dec), e.g. '2026'."""
        return (
            f"Write the Jellybeans yearly report for {year} (calendar year, January to December).\n"
            "1. get_period_performance(granularity='year') for year-over-year results.\n"
            "2. get_period_performance(granularity='month') for that year: seasonality, best and worst months.\n"
            "3. For the whole year: get_revenue_breakdown, get_package_performance and get_customer_insights.\n"
            "If the year is still in progress (partial), say so and avoid treating it as a full year. "
            "Finish with priorities for next year. Amounts in LKR."
        )
