"""
Time utilities for calculating order duration and overdue status.
Overdue threshold: 2 calendar hours (simple calculation).
"""

from datetime import datetime, timedelta
from django.utils import timezone


# Simple overdue threshold: 2 calendar hours
OVERDUE_THRESHOLD_HOURS = 2


def is_order_overdue(started_at: datetime, now: datetime = None) -> bool:
    """
    Check if an order has exceeded the 9-hour calendar threshold.
    Simple calculation: just check elapsed calendar hours, no working hour complexity.

    Args:
        started_at: Order start datetime
        now: Current datetime (defaults to timezone.now())

    Returns:
        True if order has been active for 9+ calendar hours, False otherwise
    """
    if not started_at:
        return False

    if now is None:
        now = timezone.now()

    # Ensure both datetimes are timezone-aware
    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)
    if timezone.is_naive(now):
        now = timezone.make_aware(now)

    # Simple calendar hours check
    elapsed_hours = (now - started_at).total_seconds() / 3600.0

    return elapsed_hours >= OVERDUE_THRESHOLD_HOURS


def get_order_overdue_status(order) -> dict:
    """
    Get the overdue status of an order.
    Simple calculation: just check elapsed calendar hours.

    Args:
        order: Order instance

    Returns:
        Dictionary with:
        - is_overdue (bool): Whether the order is overdue (9+ hours elapsed)
        - hours_elapsed (float): Calendar hours since start
        - overdue_by_hours (float): How many hours over the threshold (0 if not overdue)
    """
    result = {
        'is_overdue': False,
        'hours_elapsed': 0.0,
        'overdue_by_hours': 0.0,
    }

    if not order.started_at:
        return result

    now = timezone.now()

    # Ensure timezone-aware
    started_at = order.started_at
    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at)
    if timezone.is_naive(now):
        now = timezone.make_aware(now)

    # Simple calendar hours check
    elapsed_hours = (now - started_at).total_seconds() / 3600.0
    result['hours_elapsed'] = round(elapsed_hours, 2)

    if elapsed_hours >= OVERDUE_THRESHOLD_HOURS:
        result['is_overdue'] = True
        result['overdue_by_hours'] = round(elapsed_hours - OVERDUE_THRESHOLD_HOURS, 2)

    return result


def format_hours(hours: float) -> str:
    """
    Format hours as a human-readable string.

    Args:
        hours: Number of hours (float)

    Returns:
        Formatted string like "9h 30m" or "2h 15m"
    """
    if hours < 0:
        return "0h"

    total_minutes = int(hours * 60)
    hours_part = total_minutes // 60
    minutes_part = total_minutes % 60

    if hours_part == 0 and minutes_part == 0:
        return "0h"
    elif hours_part == 0:
        return f"{minutes_part}m"
    elif minutes_part == 0:
        return f"{hours_part}h"
    else:
        return f"{hours_part}h {minutes_part}m"


def estimate_completion_time(started_at: datetime, estimated_minutes: int = None) -> dict:
    """
    Estimate the completion time based on start time and estimated duration.

    Args:
        started_at: Order start datetime
        estimated_minutes: Estimated duration in minutes (defaults to 9 hours)

    Returns:
        Dictionary with:
        - estimated_end (datetime): Estimated completion datetime
        - estimated_hours (float): Estimated duration in hours
        - formatted (str): Human-readable format
    """
    if not started_at:
        return None

    if estimated_minutes is None:
        estimated_minutes = OVERDUE_THRESHOLD_HOURS * 60

    estimated_hours = estimated_minutes / 60.0

    # Simple: add estimated hours to start time
    estimated_end = started_at + timedelta(hours=estimated_hours)

    return {
        'estimated_end': estimated_end,
        'estimated_hours': estimated_hours,
        'formatted': format_hours(estimated_hours),
    }


def calculate_estimated_duration(started_at: datetime, completed_at: datetime, work_start_hour: int = 8, work_end_hour: int = 17) -> int | None:
    """
    Calculate duration in minutes between two datetimes, counting ONLY working hours.

    Working window: [work_start_hour:00, work_end_hour:00) local time (default 08:00-17:00).

    Args:
        started_at: Start datetime
        completed_at: End datetime
        work_start_hour: Start of working day (hour, 0-23)
        work_end_hour: End of working day (hour, 0-23)

    Returns:
        Integer minutes within working windows, or None if inputs are missing.
    """
    if not started_at or not completed_at:
        return None

    # Ensure timezone-aware and use current timezone for working-window boundaries
    tz = timezone.get_current_timezone()

    if timezone.is_naive(started_at):
        started_at = timezone.make_aware(started_at, tz)
    else:
        started_at = timezone.localtime(started_at, tz)

    if timezone.is_naive(completed_at):
        completed_at = timezone.make_aware(completed_at, tz)
    else:
        completed_at = timezone.localtime(completed_at, tz)

    # If end precedes start, return zero minutes
    if completed_at <= started_at:
        return 0

    # Iterate day by day and sum overlap with working window
    total_seconds = 0

    current_day = started_at.date()
    end_day = completed_at.date()

    # Helper to build aware datetime for a specific day and hour
    from datetime import time as dtime

    while current_day <= end_day:
        day_start_naive = datetime.combine(current_day, dtime(hour=work_start_hour, minute=0))
        day_end_naive = datetime.combine(current_day, dtime(hour=work_end_hour, minute=0))
        day_start = timezone.make_aware(day_start_naive, tz)
        day_end = timezone.make_aware(day_end_naive, tz)

        # Determine overlap interval for this day
        interval_start = max(started_at, day_start)
        interval_end = min(completed_at, day_end)

        if interval_end > interval_start:
            total_seconds += (interval_end - interval_start).total_seconds()

        # Next day
        current_day = current_day + timedelta(days=1)

    # Convert to whole minutes
    return int(total_seconds // 60)
