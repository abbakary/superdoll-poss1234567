"""
Vehicle Tracking and Service Analytics Dashboard
Provides detailed tracking of vehicles by service period (daily, weekly, monthly)
with analytics, charts, and detailed invoice/order information.
"""

import logging
import json
from collections import defaultdict
import re
from datetime import datetime, timedelta
from decimal import Decimal
from django.db.models import Count, Sum, Q, F, DecimalField
from django.db.models.functions import Cast
from django.http import JsonResponse
from django.shortcuts import render
from django.contrib.auth.decorators import login_required
from django.views.decorators.http import require_http_methods
from django.utils import timezone

from tracker.models import Vehicle, Order, Invoice, InvoiceLineItem, LabourCode, Customer
from tracker.utils.order_type_detector import _normalize_category_to_order_type
from .utils import get_user_branch

logger = logging.getLogger(__name__)


@login_required
def vehicle_tracking_dashboard(request):
    """
    Vehicle Tracking Dashboard - Shows vehicles that came for service
    with daily, weekly, and monthly analytics.
    """
    user_branch = get_user_branch(request.user)
    
    # Get filter parameters
    period = request.GET.get('period', 'monthly')  # daily, weekly, monthly
    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    status_filter = request.GET.get('status', '')  # completed, pending, all
    order_type_filter = request.GET.get('order_type', '')  # service, sales, labour
    
    # Set default date range
    if not end_date:
        end_date = timezone.now().date()
    else:
        try:
            end_date = datetime.strptime(end_date, '%Y-%m-%d').date()
        except:
            end_date = timezone.now().date()
    
    if not start_date:
        if period == 'daily':
            start_date = end_date
        elif period == 'weekly':
            start_date = end_date - timedelta(days=7)
        else:  # monthly
            start_date = end_date - timedelta(days=30)
    else:
        try:
            start_date = datetime.strptime(start_date, '%Y-%m-%d').date()
        except:
            start_date = end_date - timedelta(days=30)
    
    context = {
        'period': period,
        'start_date': start_date.isoformat(),
        'end_date': end_date.isoformat(),
        'status_filter': status_filter,
        'order_type_filter': order_type_filter,
    }
    
    return render(request, 'tracker/vehicle_tracking_dashboard.html', context)


@login_required
@require_http_methods(["GET"])
def api_vehicle_tracking_data(request):
    user_branch = get_user_branch(request.user)
    try:
        period = request.GET.get('period', 'monthly')
        start_date_str = request.GET.get('start_date')
        end_date_str = request.GET.get('end_date')
        status_filter = request.GET.get('status', 'all')
        order_type_filter = request.GET.get('order_type', 'all')
        search_query = request.GET.get('search', '').strip()
        if search_query == 'undefined' or search_query == 'null':
            search_query = ''

        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else timezone.now().date()
        except Exception:
            end_date = timezone.now().date()
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else (end_date - timedelta(days=30))
        except Exception:
            start_date = end_date - timedelta(days=30)

        logger.info(f"Vehicle tracking query - Period: {period}, Date range: {start_date} to {end_date}, Search: '{search_query}', User branch: {user_branch}")

        invoices_qs_all = Invoice.objects.select_related('customer', 'vehicle', 'order')
        invoices_qs = invoices_qs_all
        if user_branch:
            invoices_qs = invoices_qs.filter(branch=user_branch)

        def _inv_in_range(inv):
            try:
                inv_date = inv.invoice_date or (getattr(inv, 'created_at', None).date() if getattr(inv, 'created_at', None) else None)
            except Exception:
                inv_date = inv.invoice_date
            if not inv_date:
                return False
            return start_date <= inv_date <= end_date

        invoices = [inv for inv in invoices_qs if _inv_in_range(inv)]

        def _plate_from_reference(ref: str):
            if not ref:
                return None
            s = str(ref).strip().upper()
            if s.startswith('FOR '):
                s = s[4:].strip()
            elif s.startswith('FOR'):
                s = s[3:].strip()
            if re.match(r'^[A-Z]{1,3}\s*-?\s*\d{1,4}[A-Z]?$', s) or \
               re.match(r'^[A-Z]{1,3}\d{3,4}$', s) or \
               re.match(r'^\d{1,4}[A-Z]{2,3}$', s) or \
               re.match(r'^[A-Z]\s*\d{1,4}\s*[A-Z]{2,3}$', s):
                return s.replace('-', '').replace(' ', '')
            return None

        buckets = {}
        # Build buckets per vehicle/plate, merging additional-only invoices into real vehicle buckets
        for inv in invoices:
            try:
                plate_ref = _plate_from_reference(inv.reference)
            except Exception:
                plate_ref = None

            if search_query:
                if not (
                    (plate_ref and search_query.lower() in plate_ref.lower()) or
                    (inv.vehicle and inv.vehicle.plate_number and search_query.lower() in inv.vehicle.plate_number.lower()) or
                    (inv.customer and inv.customer.full_name and search_query.lower() in inv.customer.full_name.lower())
                ):
                    continue

            veh_id = inv.vehicle_id or 0
            plate_val = plate_ref or (inv.vehicle.plate_number if inv.vehicle else '')

            # If invoice has no linked vehicle but has a plate reference, try to merge into real vehicle bucket
            if not veh_id and plate_ref:
                try:
                    # IMPORTANT: Scope vehicle lookup by branch to prevent cross-branch data leakage
                    vehicle_query = Vehicle.objects.filter(plate_number__iexact=plate_ref)
                    if user_branch:
                        vehicle_query = vehicle_query.filter(customer__branch=user_branch)
                    matched_vehicle = vehicle_query.first()
                    if matched_vehicle:
                        veh_id = matched_vehicle.id
                        plate_val = matched_vehicle.plate_number or plate_ref
                        # Prefer matched vehicle object for bucket
                        inv_vehicle_obj = matched_vehicle
                    else:
                        inv_vehicle_obj = inv.vehicle
                except Exception:
                    inv_vehicle_obj = inv.vehicle
            else:
                inv_vehicle_obj = inv.vehicle

            key = (veh_id, plate_val)
            if key not in buckets:
                buckets[key] = {
                    'vehicle': inv_vehicle_obj,
                    'customer': inv.customer,
                    'plate': plate_val,
                    'invoices': [],
                    'orders': set(),
                    'total_spent': Decimal('0'),
                }
            b = buckets[key]
            # Deduplicate invoices within the bucket by id
            if not any(getattr(_i, 'id', None) == getattr(inv, 'id', None) for _i in b['invoices']):
                b['invoices'].append(inv)
                b['total_spent'] += inv.total_amount or Decimal('0')
                if inv.order_id:
                    b['orders'].add(inv.order_id)

        logger.info(f"Buckets built from invoices: {len(buckets)}")

        orders_qs_all = Order.objects.select_related('customer', 'vehicle')
        orders_qs = orders_qs_all.filter(created_at__date__range=[start_date, end_date])
        if user_branch:
            orders_qs = orders_qs.filter(branch=user_branch)

        for order in orders_qs:
            v = order.vehicle
            plate = (v.plate_number if v else '')
            key = (v.id if v else 0, plate)
            if key not in buckets:
                if search_query:
                    match = False
                    if v and v.plate_number and search_query.lower() in v.plate_number.lower():
                        match = True
                    if order.customer and order.customer.full_name and search_query.lower() in order.customer.full_name.lower():
                        match = True
                    if not match:
                        continue
                buckets[key] = {
                    'vehicle': v,
                    'customer': order.customer,
                    'plate': plate,
                    'invoices': [],
                    'orders': set([order.id]),
                    'total_spent': Decimal('0'),
                }
            else:
                buckets[key]['orders'].add(order.id)

        vehicle_data = []
        if buckets:
            for key, b in buckets.items():
                vehicle = b['vehicle']
                inv_qs = b['invoices']

                orders = Order.objects.none()
                if vehicle:
                    try:
                        orders = vehicle.orders.filter(created_at__date__range=[start_date, end_date])
                    except Exception:
                        orders = Order.objects.none()
                if user_branch:
                    orders = orders.filter(branch=user_branch)

                order_links_via_invoices = Order.objects.filter(id__in=[inv.order_id for inv in inv_qs if inv.order_id]).distinct()
                if user_branch:
                    order_links_via_invoices = order_links_via_invoices.filter(branch=user_branch)

                def _count_by_status(qs):
                    return {
                        'completed': qs.filter(status='completed').count(),
                        'in_progress': qs.filter(status='in_progress').count(),
                        'pending': qs.filter(status='created').count(),
                        'overdue': qs.filter(status='overdue').count(),
                        'cancelled': qs.filter(status='cancelled').count(),
                    }

                orders_stats = _count_by_status(orders)
                invoice_links_stats = _count_by_status(order_links_via_invoices)
                order_stats = {
                    'completed': orders_stats['completed'] + invoice_links_stats['completed'],
                    'in_progress': orders_stats['in_progress'] + invoice_links_stats['in_progress'],
                    'pending': orders_stats['pending'] + invoice_links_stats['pending'],
                    'overdue': orders_stats['overdue'] + invoice_links_stats['overdue'],
                    'cancelled': orders_stats['cancelled'] + invoice_links_stats['cancelled'],
                }

                extra_order_ids = list(b.get('orders', set()))
                extra_orders = Order.objects.filter(id__in=extra_order_ids) if extra_order_ids else Order.objects.none()
                if user_branch:
                    extra_orders = extra_orders.filter(branch=user_branch)
                all_orders = orders.union(order_links_via_invoices, extra_orders).order_by('-created_at')
                if not inv_qs and not all_orders.exists():
                    continue

                total_spent = sum((inv.total_amount or Decimal('0')) for inv in inv_qs)
                invoice_count = len(inv_qs)

                order_types = set()
                service_types = set()
                for order in all_orders:
                    order_types.add(order.type)
                    if order.mixed_categories:
                        try:
                            categories = json.loads(order.mixed_categories)
                            for cat in categories:
                                service_types.add(cat)
                        except Exception:
                            pass

                # Helper: classify item codes using LabourCode and normalize to order types
                def _classify_codes(codes):
                    if not codes:
                        return {}
                    cleaned = [str(c).strip() for c in codes if c]
                    if not cleaned:
                        return {}
                    found = LabourCode.objects.filter(code__in=cleaned, is_active=True).values('code', 'category')
                    mapping = {}
                    for row in found:
                        code = row['code']
                        cat = row['category']
                        otype = _normalize_category_to_order_type(cat)
                        color = 'badge-labour' if otype == 'labour' else ('badge-service' if otype == 'service' else 'badge-sales')
                        mapping[code] = {'category': cat, 'order_type': otype, 'color_class': color}
                    for c in cleaned:
                        if c not in mapping:
                            mapping[c] = {'category': 'Sales', 'order_type': 'sales', 'color_class': 'badge-sales'}
                    return mapping

                order_ids_for_union = list(all_orders.values_list('id', flat=True)) if all_orders.exists() else []
                inv_by_orders = Invoice.objects.filter(order_id__in=order_ids_for_union) if order_ids_for_union else Invoice.objects.none()
                if user_branch:
                    inv_by_orders = inv_by_orders.filter(branch=user_branch)
                inv_by_vehicle = Invoice.objects.filter(vehicle_id=vehicle.id) if vehicle else Invoice.objects.none()
                if user_branch:
                    inv_by_vehicle = inv_by_vehicle.filter(branch=user_branch)
                combined_map = {}
                for inv in inv_qs:
                    combined_map[inv.id] = inv
                for inv in inv_by_orders:
                    combined_map[inv.id] = inv
                for inv in inv_by_vehicle:
                    combined_map[inv.id] = inv
                # Ensure invoice list is unique by id
                display_invoices = list(combined_map.values())
                display_invoices.sort(key=lambda x: (x.invoice_date or datetime.min, x.id))
                seen_ids = set()
                display_invoices = [inv for inv in display_invoices if not (getattr(inv, 'id', None) in seen_ids or seen_ids.add(getattr(inv, 'id', None)))]
                valid_display_invoices = [inv for inv in display_invoices if _plate_from_reference(inv.reference)]
                # If there are no valid reference invoices, skip this vehicle entirely
                if not valid_display_invoices:
                    continue

                invoice_list = []
                for invoice in valid_display_invoices:
                    line_items = InvoiceLineItem.objects.filter(invoice=invoice)
                    item_codes = [li.code for li in line_items if li.code]
                    code_map = _classify_codes(item_codes)
                    categories = set()
                    line_items_data = []
                    for item in line_items:
                        info = code_map.get(item.code or '', {'category': 'Sales', 'order_type': 'sales', 'color_class': 'badge-sales'})
                        order_type = info['order_type']
                        category_label = 'Labour' if order_type == 'labour' else ('Service' if order_type == 'service' else 'Sales')
                        categories.add(category_label)
                        # Also accumulate vehicle-level order types from items
                        order_types.add(order_type)
                        line_items_data.append({
                            'code': item.code or '',
                            'description': item.description,
                            'qty': float(item.quantity),
                            'unit_price': int(item.unit_price or 0),
                            'total': int(item.line_total or 0),
                            'category': category_label,
                            'order_type': order_type,
                            'color_class': info['color_class'],
                            'tax_rate': float(item.tax_rate) if item.tax_rate else 0,
                            'tax_amount': float(item.tax_amount) if item.tax_amount else 0,
                        })
                    inv_date_val = invoice.invoice_date
                    if hasattr(inv_date_val, 'date'):
                        inv_date = inv_date_val.date()
                    else:
                        inv_date = inv_date_val
                    if not inv_date and getattr(invoice, 'created_at', None):
                        try:
                            inv_date = invoice.created_at.date()
                        except Exception:
                            inv_date = None
                    invoice_dict = {
                        'invoice_number': invoice.invoice_number,
                        'invoice_date': (inv_date.isoformat() if inv_date else ''),
                        'total_amount': int(invoice.total_amount or 0),
                        'subtotal': int(invoice.subtotal or 0),
                        'tax_amount': int(invoice.tax_amount or 0),
                        'reference': invoice.reference or '',
                        'status': invoice.status,
                        'order_id': invoice.order_id,
                        'order_number': invoice.order.order_number if invoice.order else '',
                        'line_items_count': line_items.count(),
                        'categories': sorted(list(categories)) if categories else ['Service'],
                        'line_items': line_items_data
                    }
                    invoice_list.append(invoice_dict)

                if status_filter != 'all':
                    if status_filter == 'completed' and order_stats['completed'] == 0:
                        continue
                    elif status_filter == 'pending' and order_stats.get('pending', 0) == 0:
                        continue

                if order_type_filter != 'all':
                    if order_type_filter not in order_types:
                        continue

                # Recalculate totals based on valid reference invoices
                total_spent = sum((inv.total_amount or Decimal('0')) for inv in valid_display_invoices)
                invoice_count = len(valid_display_invoices)
                is_returning = invoice_count > 1
                recent_plate = None
                try:
                    recent_source = list(valid_display_invoices)
                    if recent_source:
                        try:
                            recent_invoice = max(
                                recent_source,
                                key=lambda inv: inv.invoice_date or datetime.min
                            )
                        except Exception:
                            recent_invoice = recent_source[0]
                        recent_plate = _plate_from_reference(recent_invoice.reference)
                except Exception:
                    recent_plate = None

                vehicle_dict = {
                    'id': vehicle.id if vehicle else None,
                    'plate_number': recent_plate or (vehicle.plate_number if vehicle else '') or '',
                    'make': vehicle.make if vehicle else '',
                    'model': vehicle.model if vehicle else '',
                    'vehicle_type': vehicle.vehicle_type if vehicle else '',
                    'customer_id': (vehicle.customer.id if vehicle and vehicle.customer else (b['customer'].id if b['customer'] else None)),
                    'customer_name': (vehicle.customer.full_name if vehicle and vehicle.customer else (b['customer'].full_name if b['customer'] else '')),
                    'customer_phone': (vehicle.customer.phone if vehicle and vehicle.customer else (b['customer'].phone if b['customer'] else '')) or '',
                    'total_spent': int(total_spent or 0),
                    'invoice_count': invoice_count,
                    'is_returning': is_returning,
                    'order_stats': order_stats,
                    'order_types': sorted(list(order_types)),
                    'service_types': sorted(list(service_types)) if service_types else [],
                    'invoices': invoice_list,
                    'order_count': all_orders.count(),
                }
                vehicle_data.append(vehicle_dict)

        vehicle_data.sort(key=lambda x: x['total_spent'], reverse=True)
        logger.info(f"Final vehicle_data count: {len(vehicle_data)}, Buckets count: {len(buckets) if buckets else 0}")

        # Calculate revenue breakdown by order type for the selected date range
        revenue_by_type = {
            'sales': 0,
            'service': 0,
            'labour': 0,
            'unknown': 0,
            'total': 0,
        }
        try:
            # Reuse invoices list built above using invoice_date OR created_at fallback
            invoice_ids = [inv.id for inv in invoices]
            if invoice_ids:
                line_items = InvoiceLineItem.objects.filter(
                    invoice_id__in=invoice_ids,
                    invoice__status__in=['draft', 'issued', 'paid']
                ).select_related('invoice')

                if user_branch:
                    line_items = line_items.filter(invoice__branch=user_branch)

                # Sum by order type
                for line_item in line_items:
                    order_type = line_item.order_type or 'unknown'
                    # Include line total plus tax amount if present
                    line_value = (line_item.line_total or Decimal('0')) + (line_item.tax_amount or Decimal('0'))
                    line_value = int(line_value)

                    if order_type in revenue_by_type:
                        revenue_by_type[order_type] += line_value
                    else:
                        revenue_by_type['unknown'] += line_value

            # Calculate total
            revenue_by_type['total'] = sum([v for k, v in revenue_by_type.items() if k != 'total'])
        except Exception as e:
            logger.warning(f"Error calculating revenue by order type for vehicle tracking: {e}")

        summary = {
            'total_vehicles': len(vehicle_data),
            'total_spent': int(sum(v['total_spent'] for v in vehicle_data)),
            'total_invoices': sum(v['invoice_count'] for v in vehicle_data),
            'returning_vehicles': sum(1 for v in vehicle_data if v['is_returning']),
            'order_stats': {
                'completed': sum(v['order_stats']['completed'] for v in vehicle_data),
                'in_progress': sum(v['order_stats']['in_progress'] for v in vehicle_data),
                'pending': sum(v['order_stats']['pending'] for v in vehicle_data),
                'overdue': sum(v['order_stats']['overdue'] for v in vehicle_data),
            },
            'revenue_by_type': revenue_by_type,
        }
        logger.info(f"Summary: {summary}")
        return JsonResponse({
            'success': True,
            'data': vehicle_data,
            'summary': summary,
            'filters': {
                'period': period,
                'start_date': start_date.isoformat(),
                'end_date': end_date.isoformat(),
                'status': status_filter,
                'order_type': order_type_filter,
            }
        })
    except Exception as e:
        logger.error(f"Error fetching vehicle tracking data: {e}", exc_info=True)
        return JsonResponse({'success': False, 'message': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def api_vehicle_analytics(request):
    """
    API endpoint for vehicle analytics and trends.
    
    Returns:
    - Daily/weekly/monthly trends
    - Spending by order type
    - Vehicle visit frequency
    - Average spending per vehicle
    """
    user_branch = get_user_branch(request.user)
    
    try:
        period = request.GET.get('period', 'monthly')
        start_date_str = request.GET.get('start_date')
        end_date_str = request.GET.get('end_date')
        
        # Parse dates
        try:
            end_date = datetime.strptime(end_date_str, '%Y-%m-%d').date() if end_date_str else timezone.now().date()
        except:
            end_date = timezone.now().date()
        
        try:
            start_date = datetime.strptime(start_date_str, '%Y-%m-%d').date() if start_date_str else (end_date - timedelta(days=30))
        except:
            start_date = end_date - timedelta(days=30)
        
        # Build invoices with Python-level date filtering and branch fallback
        invoices_qs_all = Invoice.objects.select_related('vehicle')
        invoices_qs = invoices_qs_all
        if user_branch:
            invoices_qs = invoices_qs.filter(branch=user_branch)

        def _in_range(inv):
            try:
                inv_date = inv.invoice_date or (getattr(inv, 'created_at', None).date() if getattr(inv, 'created_at', None) else None)
            except Exception:
                inv_date = inv.invoice_date
            if not inv_date:
                return False
            return start_date <= inv_date <= end_date

        invoices = [inv for inv in invoices_qs if _in_range(inv)]

        logger.info(f"Analytics - Invoices in range {start_date} to {end_date}: {len(invoices)}")

        # Group data by period in Python
        trends_dict = defaultdict(lambda: {'total_amount': Decimal('0'), 'invoice_count': 0, 'vehicles': set()})

        for invoice in invoices:
            # Get the date portion from invoice_date (handle datetime if needed)
            invoice_date_value = invoice.invoice_date
            if hasattr(invoice_date_value, 'date'):
                # It's a datetime, convert to date
                invoice_date = invoice_date_value.date()
            else:
                # It's already a date
                invoice_date = invoice_date_value

            # Determine grouping key based on period
            if period == 'daily':
                period_key = invoice_date
            elif period == 'weekly':
                # Group by week (Monday of that week)
                period_key = invoice_date - timedelta(days=invoice_date.weekday())
            else:  # monthly
                # Group by first day of month
                period_key = invoice_date.replace(day=1)

            trends_dict[period_key]['total_amount'] += invoice.total_amount or Decimal('0')
            trends_dict[period_key]['invoice_count'] += 1
            if invoice.vehicle_id:
                trends_dict[period_key]['vehicles'].add(invoice.vehicle_id)

        # Convert to list and sort by date
        trends_data = [
            {
                'date': date.isoformat() if date else '',
                'total_amount': float(data['total_amount']),
                'invoice_count': data['invoice_count'],
                'vehicle_count': len(data['vehicles']),
            }
            for date, data in sorted(trends_dict.items())
        ]
        
        # Spending by order type
        spending_by_type = Invoice.objects.filter(id__in=[inv.id for inv in invoices]).filter(
            order__type__isnull=False
        ).values('order__type').annotate(
            total=Sum('total_amount'),
            count=Count('id')
        ).order_by('-total')
        
        spending_by_type_data = [
            {
                'type': item['order__type'],
                'total': float(item['total'] or 0),
                'count': item['count'],
                'average': float((item['total'] or 0) / item['count']) if item['count'] > 0 else 0,
            }
            for item in spending_by_type
        ]
        
        # Top vehicles by spending
        top_vehicle_ids = [inv.vehicle_id for inv in invoices if inv.vehicle_id]
        top_vehicles = Vehicle.objects.filter(id__in=top_vehicle_ids).annotate(
            total_spent=Sum('invoices__total_amount'),
            invoice_count=Count('invoices', distinct=True)
        ).filter(total_spent__isnull=False).order_by('-total_spent')[:10]
        
        top_vehicles_data = [
            {
                'plate_number': v.plate_number,
                'customer_name': v.customer.full_name,
                'total_spent': float(v.total_spent or 0),
                'invoice_count': v.invoice_count,
                'average_per_invoice': float((v.total_spent or 0) / v.invoice_count) if v.invoice_count > 0 else 0,
            }
            for v in top_vehicles
        ]
        
        return JsonResponse({
            'success': True,
            'trends': trends_data,
            'spending_by_type': spending_by_type_data,
            'top_vehicles': top_vehicles_data,
        })
        
    except Exception as e:
        logger.error(f"Error fetching vehicle analytics: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'message': str(e)
        }, status=500)
