"""
Revenue analytics utilities for calculating breakdown by order type.
Provides functions to aggregate and analyze revenue by sales/service/labour categories.
"""

from decimal import Decimal
from django.db.models import Sum, Q, F, DecimalField
from django.db.models.functions import Coalesce
from django.utils import timezone
from datetime import timedelta
from tracker.models import Invoice, InvoiceLineItem


def get_revenue_by_order_type(invoices_qs=None, date_from=None, date_to=None):
    """
    Calculate total revenue breakdown by order type (sales, service, labour, unknown).

    Unmapped/unknown item codes are tracked separately to allow better visibility
    into which items don't have specified types.

    Args:
        invoices_qs: QuerySet of invoices to analyze (optional, defaults to all)
        date_from: Start date for filtering invoices (optional)
        date_to: End date for filtering invoices (optional)

    Returns:
        Dict with keys:
        - sales: Total revenue from sales items
        - service: Total revenue from service items
        - labour: Total revenue from labour items
        - unknown: Total revenue from items with unspecified/unmapped codes
        - total: Total revenue across all types
        - count: Number of invoices analyzed
    """
    if invoices_qs is None:
        invoices_qs = Invoice.objects.filter(status__in=['draft', 'issued', 'paid'])
    else:
        invoices_qs = invoices_qs.filter(status__in=['draft', 'issued', 'paid'])
    
    # Apply date filtering if provided
    if date_from:
        invoices_qs = invoices_qs.filter(invoice_date__gte=date_from)
    if date_to:
        invoices_qs = invoices_qs.filter(invoice_date__lte=date_to)
    
    # Get invoice IDs to filter line items
    invoice_ids = list(invoices_qs.values_list('id', flat=True))
    
    if not invoice_ids:
        return {
            'sales': Decimal('0'),
            'service': Decimal('0'),
            'labour': Decimal('0'),
            'unknown': Decimal('0'),
            'total': Decimal('0'),
            'count': 0,
        }
    
    # Aggregate line items by order type
    line_items = InvoiceLineItem.objects.filter(invoice_id__in=invoice_ids)
    
    result = {
        'sales': Decimal('0'),
        'service': Decimal('0'),
        'labour': Decimal('0'),
        'unknown': Decimal('0'),
        'total': Decimal('0'),
        'count': len(invoice_ids),
    }
    
    # Sum line totals by order type
    # Items with order_type='unknown' or None are treated as unspecified/unmapped codes
    for item in line_items:
        order_type = item.order_type or 'unknown'
        line_value = item.line_total + item.tax_amount if item.tax_amount else item.line_total

        if order_type in result:
            result[order_type] = Decimal(str(result[order_type])) + Decimal(str(line_value))
        else:
            # Any unrecognized type defaults to 'unknown'
            result['unknown'] = Decimal(str(result['unknown'])) + Decimal(str(line_value))
    
    # Calculate total
    result['total'] = (
        result['sales'] +
        result['service'] +
        result['labour'] +
        result['unknown']
    )
    
    return result


def get_revenue_by_order_type_this_month():
    """Get revenue breakdown by order type for current month."""
    now = timezone.now()
    month_start = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    month_end = (month_start + timedelta(days=32)).replace(day=1) - timedelta(seconds=1)

    invoices_qs = Invoice.objects.filter(
        status__in=['draft', 'issued', 'paid'],
        invoice_date__gte=month_start.date(),
        invoice_date__lte=month_end.date()
    )

    return get_revenue_by_order_type(invoices_qs)


def get_revenue_by_order_type_all_time():
    """Get revenue breakdown by order type for all time."""
    invoices_qs = Invoice.objects.filter(status__in=['draft', 'issued', 'paid'])
    return get_revenue_by_order_type(invoices_qs)


def get_revenue_by_order_type_for_vehicles(vehicle_ids, date_from=None, date_to=None):
    """
    Calculate revenue breakdown by order type for specific vehicles during a date range.
    
    Args:
        vehicle_ids: List of vehicle IDs or single vehicle ID
        date_from: Start date (optional)
        date_to: End date (optional)
    
    Returns:
        Dict with revenue breakdown by order type (same format as get_revenue_by_order_type)
    """
    if isinstance(vehicle_ids, (list, tuple)):
        invoices_qs = Invoice.objects.filter(vehicle_id__in=vehicle_ids)
    else:
        invoices_qs = Invoice.objects.filter(vehicle_id=vehicle_ids)
    
    return get_revenue_by_order_type(invoices_qs, date_from, date_to)


def format_revenue_value(value):
    """Format a revenue value as string with proper decimal places."""
    if not value:
        return "0"
    try:
        decimal_val = Decimal(str(value))
        return str(int(decimal_val))
    except Exception:
        return str(value)
