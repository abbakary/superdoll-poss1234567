"""
Utility to determine order type based on extracted invoice item codes.
Compares item codes against LabourCode mappings to classify orders as
labour, service, sales, or mixed types.
"""

import json
import logging
from typing import List, Dict, Tuple, Set

logger = logging.getLogger(__name__)


def determine_order_type_from_codes(item_codes: List[str]) -> Tuple[str, List[str], Dict]:
    """
    Determine order type based on invoice item codes.

    Logic:
    - LabourCode table stores: SERVICE (tyre/service codes) and LABOUR (labour codes)
    - If item code found in LabourCode table -> map to its category (service or labour)
    - If item code NOT found -> treat as 'sales' (unmapped products/items)
    - If mixed categories detected -> 'mixed' type with all categories
    - If no codes provided or cannot determine -> 'unspecified'

    Args:
        item_codes: List of item codes extracted from invoice

    Returns:
        Tuple of:
        - order_type: 'labour', 'service', 'sales', 'unspecified', or 'mixed'
        - categories: List of unique categories found (includes "sales" for unmapped)
        - mapping_info: Dict with code->category mappings and unmapped codes
    """
    if not item_codes:
        return 'unspecified', [], {'mapped': {}, 'unmapped': [], 'categories_found': [], 'order_types_found': []}

    from tracker.models import LabourCode

    # Clean and normalize codes
    cleaned_codes = [str(code).strip() for code in item_codes if code]
    if not cleaned_codes:
        return 'sales', [], {'mapped': {}, 'unmapped': [], 'categories_found': [], 'order_types_found': []}

    # Query database for matching labour codes
    found_codes = LabourCode.objects.filter(
        code__in=cleaned_codes,
        is_active=True
    ).values('code', 'category')

    # Build mappings
    code_to_category = {}
    categories_found = set()
    unmapped_codes = []

    found_code_set = set()
    for row in found_codes:
        code = row['code']
        category = row['category']
        code_to_category[code] = category
        categories_found.add(category)
        found_code_set.add(code)

    # Track unmapped codes (treat as sales)
    for code in cleaned_codes:
        if code not in found_code_set:
            unmapped_codes.append(code)

    has_unmapped = len(unmapped_codes) > 0

    # Determine order types based on mapped categories
    order_types_found = set()

    for category in categories_found:
        order_type = _normalize_category_to_order_type(category)
        order_types_found.add(order_type)

    # Add sales if there are unmapped codes
    if has_unmapped:
        order_types_found.add('sales')
        categories_found.add('sales')

    # Determine final order type
    if len(order_types_found) == 0:
        final_order_type = 'sales'
        final_categories = ['sales']
    elif len(order_types_found) == 1:
        final_order_type = list(order_types_found)[0]
        final_categories = sorted(list(categories_found))
    else:
        final_order_type = 'mixed'
        final_categories = sorted(list(categories_found))

    mapping_info = {
        'mapped': code_to_category,
        'unmapped': unmapped_codes,
        'categories_found': final_categories,
        'order_types_found': sorted(list(order_types_found)),
    }

    logger.info(
        f"Order type detection: codes={cleaned_codes}, "
        f"categories={final_categories}, type={final_order_type}, "
        f"mapped={len(code_to_category)}, unmapped={len(unmapped_codes)}"
    )

    return final_order_type, final_categories, mapping_info


def _normalize_category_to_order_type(category: str) -> str:
    """
    Normalize a labour code category to a valid order type.

    Examples:
    - 'labour' -> 'labour'
    - 'tyre service' -> 'service'
    - 'tyre service / makill' -> 'service'
    - None/empty -> 'unspecified'
    """
    if not category:
        return 'unspecified'

    category_lower = category.lower().strip()

    # Direct mapping
    if category_lower == 'labour':
        return 'labour'
    elif 'tyre' in category_lower or 'service' in category_lower:
        return 'service'
    else:
        return 'labour'


def get_mixed_order_status_display(order_type: str, order_types_found: List[str] = None, categories: List[str] = None) -> str:
    """
    Generate a display string for order status showing types and categories.

    Args:
        order_type: The determined order type ('sales', 'service', 'labour', 'mixed')
        order_types_found: List of actual order types found (e.g., ['sales', 'labour'])
        categories: List of labour code categories found

    Examples:
    - 'service', ['service'], ['tyre service'] -> 'Service'
    - 'labour', ['labour'], ['labour'] -> 'Labour'
    - 'mixed', ['sales', 'labour'], ['labour'] -> 'Sales and Labour'
    - 'mixed', ['labour', 'service'], ['labour', 'tyre service'] -> 'Labour and Service'
    - 'sales', ['sales'], [] -> 'Sales'
    """
    if order_type == 'mixed' and order_types_found:
        # Format as "Type1 and Type2 and Type3"
        type_names = [_format_type_name(t) for t in sorted(order_types_found)]
        return ' and '.join(type_names)
    else:
        return _format_type_name(order_type)


def _format_type_name(order_type: str) -> str:
    """Format order type name for display."""
    if order_type == 'labour':
        return 'Labour'
    elif order_type == 'service':
        return 'Service'
    elif order_type == 'sales':
        return 'Sales'
    elif order_type == 'inquiry':
        return 'Inquiry'
    else:
        return order_type.title()
