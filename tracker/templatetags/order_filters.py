"""
Template filters for displaying order information
"""

from django import template
from tracker.utils.order_type_detector import get_mixed_order_status_display
import json

register = template.Library()


@register.filter
def order_type_display(order):
    """
    Display order type with mixed type support.
    Shows "Labour and Service", "Service and Sales", etc. for mixed types.
    """
    if not order:
        return "Unknown"

    if order.type == 'mixed' and order.mixed_categories:
        try:
            categories = json.loads(order.mixed_categories)
            # Determine order types from categories
            order_types = set()
            for category in categories:
                category_lower = category.lower().strip()
                if category_lower == 'sales':
                    order_types.add('sales')
                elif category_lower == 'labour':
                    order_types.add('labour')
                elif 'tyre' in category_lower or 'service' in category_lower:
                    order_types.add('service')

            # Format display
            if order_types:
                type_names = sorted(list(order_types))
                formatted = ' and '.join([_format_type(t) for t in type_names])
                return formatted
        except (json.JSONDecodeError, TypeError):
            pass

    return _format_type(order.type)


@register.filter
def order_type_badge(order):
    """
    Generate HTML badge for order type with mixed type support.
    Uses visible colors for better contrast.
    """
    if not order:
        return '<span class="badge bg-dark rounded-pill text-white">Unknown</span>'

    # Determine badge color and icon based on type and mixed categories
    badge_type = order.type
    if order.type == 'mixed':
        badge_type = 'mixed'

    badge_html = ""

    if badge_type == 'service':
        badge_html = '<span class="badge bg-primary rounded-pill text-white"><i class="fa fa-wrench me-1"></i>Service</span>'
    elif badge_type == 'sales':
        badge_html = '<span class="badge bg-success rounded-pill text-white"><i class="fa fa-shopping-cart me-1"></i>Sales</span>'
    elif badge_type == 'labour':
        badge_html = '<span class="badge bg-info rounded-pill text-white"><i class="fa fa-tools me-1"></i>Labour</span>'
    elif badge_type == 'inquiry':
        badge_html = '<span class="badge bg-danger rounded-pill text-white"><i class="fa fa-question-circle me-1"></i>Inquiry</span>'
    elif badge_type == 'unknown':
        badge_html = '<span class="badge bg-secondary rounded-pill text-white"><i class="fa fa-question me-1"></i>Other</span>'
    elif badge_type == 'mixed':
        # For mixed types, show all constituent types
        if order.mixed_categories:
            try:
                categories = json.loads(order.mixed_categories)
                order_types = set()
                for category in categories:
                    category_lower = category.lower().strip()
                    if category_lower == 'sales':
                        order_types.add('sales')
                    elif category_lower == 'labour':
                        order_types.add('labour')
                    elif 'tyre' in category_lower or 'service' in category_lower:
                        order_types.add('service')

                # Create combined badge with better color
                if order_types:
                    type_names = sorted(list(order_types))
                    formatted = ' & '.join([_format_type(t) for t in type_names])
                    badge_html = f'<span class="badge bg-dark rounded-pill text-white"><i class="fa fa-layer-group me-1"></i>{formatted}</span>'
                else:
                    badge_html = '<span class="badge bg-dark rounded-pill text-white">Mixed</span>'
            except (json.JSONDecodeError, TypeError):
                badge_html = '<span class="badge bg-dark rounded-pill text-white">Mixed</span>'
        else:
            badge_html = '<span class="badge bg-dark rounded-pill text-white">Mixed</span>'
    elif badge_type == 'unknown':
        badge_html = '<span class="badge bg-secondary rounded-pill text-white"><i class="fa fa-question me-1"></i>Other</span>'
    else:
        badge_html = f'<span class="badge bg-dark rounded-pill text-white">{order.type.title()}</span>'

    return badge_html


def _format_type(order_type):
    """Format order type for display"""
    if order_type == 'labour':
        return 'Labour'
    elif order_type == 'service':
        return 'Service'
    elif order_type == 'sales':
        return 'Sales'
    elif order_type == 'inquiry':
        return 'Inquiry'
    elif order_type == 'unknown':
        return 'Other'
    else:
        return order_type.title()
