import json
import logging
import csv
from django import http
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse, HttpRequest, HttpResponse
from django.views.decorators.http import require_http_methods
from django.core.paginator import Paginator
from django.db.models import Count, Avg, Q, Sum, Case, When, F, Value, DecimalField, ExpressionWrapper
from django.db.models.functions import TruncDate, TruncDay, TruncMonth, Concat, Coalesce
from django.utils import timezone
from django.template.loader import render_to_string
from django.contrib.auth.views import LoginView
from datetime import timedelta
from .forms import ProfileForm, CustomerStep1Form, CustomerStep2Form, CustomerStep3Form, CustomerStep4Form, VehicleForm, OrderForm, CustomerEditForm, SystemSettingsForm, BrandForm, InquiryCreationForm, InquiryNoteForm
from django.urls import reverse
from django.contrib import messages
from django.core.cache import cache
from django.core.files.base import ContentFile
import base64
import json
import time
from django.contrib.auth.decorators import login_required, user_passes_test
from django.contrib.auth.models import User, Group
from django.views.decorators.http import require_http_methods
from django.views.decorators.csrf import csrf_exempt
from django.core.exceptions import ValidationError
from .models import Profile, Customer, Order, Vehicle, InventoryItem, CustomerNote, Brand, Branch, OrderAttachment, OrderAttachmentSignature, ServiceType, ServiceAddon, InquiryNote
from django.core.paginator import Paginator
from .utils import add_audit_log, get_audit_logs, clear_audit_logs, scope_queryset, get_user_branch
from .services import OrderService
from .utils.pdf_signature import (
    embed_signature_in_pdf,
    SignatureEmbedError,
    build_signed_filename,
    embed_signature_in_image,
    build_signed_name,
)
from datetime import datetime, timedelta



from django.contrib.auth.views import LogoutView
from django.views.generic import View

logger = logging.getLogger(__name__)


def _mark_overdue_orders():
    """
    Mark orders as overdue after 9 calendar hours elapsed.
    Simple calculation: just check elapsed time, no working hour complexity.
    """
    try:
        from .utils.time_utils import is_order_overdue, OVERDUE_THRESHOLD_HOURS

        now = timezone.now()
        # Ensure inquiries are treated as completed (retroactively normalize existing data)
        Order.objects.filter(type='inquiry').exclude(status='completed').update(status='completed', completed_at=now, completion_date=now)

        # Auto progress: created -> in_progress after 10 minutes (exclude inquiries)
        # Set started_at to created_at when auto-progressing
        created_cutoff = now - timedelta(minutes=10)
        Order.objects.filter(status="created", created_at__lte=created_cutoff).exclude(type='inquiry').update(status="in_progress", started_at=F('created_at'))

        # Mark orders as overdue if they've been active for 9+ calendar hours
        # Check in_progress orders with started_at set
        in_progress_orders = Order.objects.filter(
            status='in_progress',
            started_at__isnull=False
        ).exclude(type='inquiry')

        for order in in_progress_orders:
            if is_order_overdue(order.started_at, now):
                order.status = 'overdue'
                order.save(update_fields=['status'])

        # Check created orders that are waiting too long (created but not yet auto-progressed)
        # An order in 'created' status for 9+ hours should be marked as overdue
        created_orders = Order.objects.filter(
            status='created',
            created_at__isnull=False
        ).exclude(type='inquiry')

        for order in created_orders:
            if is_order_overdue(order.created_at, now):
                order.status = 'overdue'
                order.save(update_fields=['status'])

    except Exception as e:
        logger.warning(f"Error in _mark_overdue_orders: {e}")
        pass

class CustomLoginView(LoginView):
    template_name = "registration/login.html"

    def form_valid(self, form):
        response = super().form_valid(form)
        remember = self.request.POST.get("remember")
        if not remember:
            self.request.session.set_expiry(0)
        else:
            self.request.session.set_expiry(60 * 60 * 24 * 14)
        try:
            from .signals import _client_ip
            ip = _client_ip(self.request)
            ua = (self.request.META.get('HTTP_USER_AGENT') or '')[:200]
            add_audit_log(self.request.user, 'login', f'Login at {timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")} from {ip or "?"} UA: {ua}', ip=ip, user_agent=ua)
        except Exception:
            pass
        return response

    def get_success_url(self):
        user = self.request.user
        if user.is_superuser:
            return reverse('tracker:dashboard')
        if user.groups.filter(name='manager').exists():
            return reverse('tracker:orders_list')
        if user.is_staff:
            return reverse('tracker:users_list')
        return reverse('tracker:dashboard')

class CustomLogoutView(LogoutView):
    next_page = 'login'  # This will use the URL name 'login' for redirection
    
    def dispatch(self, request, *args, **kwargs):
        try:
            from .signals import _client_ip
            ip = _client_ip(request)
            ua = (request.META.get('HTTP_USER_AGENT') or '')[:200]
            add_audit_log(request.user, 'logout', f'Logout at {timezone.localtime().strftime("%Y-%m-%d %H:%M:%S")}', ip=ip, user_agent=ua)
        except Exception:
            pass
        return super().dispatch(request, *args, **kwargs)


@login_required
def api_order_status(request: HttpRequest, pk: int):
    _mark_overdue_orders()
    try:
        o = Order.objects.get(pk=pk)
        data = {
            'success': True,
            'id': o.id,
            'status': o.status,
            'status_display': o.get_status_display(),
            'estimated_duration': o.estimated_duration,
            'actual_duration': o.actual_duration,
            'created_at': o.created_at,
            'started_at': o.started_at,
            'completed_at': o.completed_at,
            'cancelled_at': o.cancelled_at,
        }
        return JsonResponse(data)
    except Order.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Not found'}, status=404)

@login_required
def api_orders_statuses(request: HttpRequest):
    _mark_overdue_orders()
    ids_param = request.GET.get('ids') or ''
    try:
        ids = [int(x) for x in ids_param.replace(',', ' ').split() if x.isdigit()]
    except Exception:
        ids = []
    qs = scope_queryset(Order.objects.filter(id__in=ids), request.user, request)
    out = {}
    for o in qs:
        out[str(o.id)] = {
            'status': o.status,
            'status_display': o.get_status_display(),
            'created_at': o.created_at,
            'started_at': o.started_at,
            'completed_at': o.completed_at,
            'cancelled_at': o.cancelled_at,
        }
    return JsonResponse({'success': True, 'orders': out})

@login_required
def api_order_invoice_totals(request: HttpRequest, pk: int):
    """
    Get aggregated invoice totals for an order.
    Sums VAT, NET (subtotal), and Gross amounts from all linked invoices.
    """
    from .models import Order, OrderInvoiceLink
    from decimal import Decimal
    from django.db.models import Sum

    orders_qs = scope_queryset(Order.objects.all(), request.user, request)

    try:
        order = orders_qs.get(pk=pk)
    except Order.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Order not found'}, status=404)

    # Get all linked invoices for this order
    invoice_links = OrderInvoiceLink.objects.filter(order=order).select_related('invoice')

    # Initialize totals
    total_net = Decimal('0')
    total_vat = Decimal('0')
    total_gross = Decimal('0')
    invoice_count = 0

    # Sum amounts from all linked invoices
    invoices_data = []
    for link in invoice_links:
        invoice = link.invoice
        subtotal = invoice.subtotal or Decimal('0')
        tax = invoice.tax_amount or Decimal('0')
        gross = invoice.total_amount or Decimal('0')

        total_net += subtotal
        total_vat += tax
        total_gross += gross
        invoice_count += 1

        invoices_data.append({
            'id': invoice.id,
            'invoice_number': invoice.invoice_number,
            'invoice_date': invoice.invoice_date.isoformat() if invoice.invoice_date else None,
            'subtotal': float(subtotal),
            'vat': float(tax),
            'total': float(gross),
            'is_primary': link.is_primary,
            'linked_at': link.linked_at.isoformat(),
        })

    return JsonResponse({
        'success': True,
        'order_id': order.id,
        'order_number': order.order_number,
        'invoice_count': invoice_count,
        'aggregated_totals': {
            'net': float(total_net),
            'vat': float(total_vat),
            'gross': float(total_gross),
        },
        'invoices': invoices_data,
    })

@login_required
def api_service_distribution(request: HttpRequest):
    """Return service type distribution for the selected period.
    period: one of week, month, quarter, year
    Applies branch/user scoping via scope_queryset.
    """
    try:
        period = (request.GET.get('period') or '').strip().lower()
        today = timezone.localdate()
        # Determine start date based on period
        if period in ('week', 'this_week'):
            start_date = today - timedelta(days=today.weekday())
            label = 'This Week'
        elif period in ('month', 'this_month'):
            start_date = today.replace(day=1)
            label = 'This Month'
        elif period in ('quarter', 'this_quarter'):
            q_start_month = ((today.month - 1) // 3) * 3 + 1
            start_date = today.replace(month=q_start_month, day=1)
            label = 'This Quarter'
        elif period in ('year', 'this_year'):
            start_date = today.replace(month=1, day=1)
            label = 'This Year'
        else:
            # Default to current month if unspecified
            start_date = today.replace(day=1)
            label = 'This Month'

        orders_qs = scope_queryset(Order.objects.all(), request.user, request)
        # Filter by created_at date range (inclusive)
        filtered = orders_qs.filter(created_at__date__gte=start_date, created_at__date__lte=today)
        rows = filtered.values('type').annotate(c=Count('id'))
        counts = {r['type']: r['c'] for r in rows}
        # Ensure consistent order of labels
        labels = ['Sales', 'Service', 'Inquiry']
        keys = ['sales', 'service', 'inquiry']
        values = [int(counts.get(k, 0) or 0) for k in keys]
        return JsonResponse({
            'success': True,
            'labels': labels,
            'values': values,
            'label': label,
        })
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=400)

@login_required
def dashboard(request: HttpRequest):
    # Normalize statuses before computing metrics
    _mark_overdue_orders()
    # Always calculate fresh metrics for accurate data
    today = timezone.localdate()

    # Import aggregation functions and datetime utilities at the top of the function to avoid UnboundLocalError
    from decimal import Decimal
    from django.db.models import Sum, Count, Avg
    from datetime import datetime

    # Branch-scoped base querysets with safe fallback for staff/admin without branch assignment
    # Exclude temporary customers (those with full_name starting with "Plate " and phone starting with "PLATE_")
    from .utils import get_user_branch
    _branch = get_user_branch(request.user)
    if not _branch and (getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)):
        orders_qs = Order.objects.all().exclude(
            customer__full_name__startswith='Plate ',
            customer__phone__startswith='PLATE_'
        )
        customers_qs = Customer.objects.all().exclude(
            full_name__startswith='Plate ',
            phone__startswith='PLATE_'
        )
    else:
        orders_qs = scope_queryset(Order.objects.all(), request.user, request).exclude(
            customer__full_name__startswith='Plate ',
            customer__phone__startswith='PLATE_'
        )
        customers_qs = scope_queryset(Customer.objects.all(), request.user, request).exclude(
            full_name__startswith='Plate ',
            phone__startswith='PLATE_'
        )

    # Remove caching to ensure fresh data
    metrics = None

    if True:  # Always recalculate
        total_orders = orders_qs.count()
        total_customers = customers_qs.count()

        status_counts_qs = orders_qs.values("status").annotate(c=Count("id"))
        type_counts_qs = orders_qs.values("type").annotate(c=Count("id"))
        priority_counts_qs = orders_qs.values("priority").annotate(c=Count("id"))

        status_counts = {x["status"]: x["c"] for x in status_counts_qs}
        type_counts = {x["type"]: x["c"] for x in type_counts_qs}
        priority_counts = {x["priority"]: x["c"] for x in priority_counts_qs}

        # Count persisted overdue
        try:
            overdue_count = orders_qs.filter(status="overdue").count()
            status_counts["overdue"] = overdue_count
        except Exception:
            status_counts.setdefault("overdue", 0)

        # Ensure all possible status values exist in status_counts (even if zero)
        all_statuses = ["created", "in_progress", "overdue", "completed", "cancelled"]
        for status in all_statuses:
            if status not in status_counts:
                status_counts[status] = 0

        # Ensure we have a count for completed orders, even if it's zero
        completed_orders = orders_qs.filter(status="completed").count()
        completion_rate = (completed_orders / total_orders * 100) if total_orders > 0 else 0
        
        # Update status_counts to ensure 'completed' key exists
        status_counts['completed'] = completed_orders
        
        # Also count completed today - MySQL compatible using range
        from .utils.mysql_compat import get_date_range
        today_date = timezone.now().date()
        start_dt, end_dt = get_date_range(today_date)
        completed_today_count = orders_qs.filter(status="completed").filter(
            Q(completed_at__gte=start_dt, completed_at__lte=end_dt) |
            Q(completed_at__isnull=True, created_at__gte=start_dt, created_at__lte=end_dt)
        ).count()

        # New orders created today (status 'created' within today's range)
        # Exclude temporary customers
        try:
            new_orders_today = orders_qs.filter(status="created").filter(created_at__gte=start_dt, created_at__lte=end_dt).count()
        except Exception:
            # Fallback: total 'created' if date filtering fails
            new_orders_today = orders_qs.filter(status="created").count()

        # New customers this month - MySQL compatible
        # Exclude temporary customers
        from .utils.mysql_compat import month_start_filter
        new_customers_this_month = customers_qs.filter(
            month_start_filter('registration_date')
        ).count()

        # Keep original fields/logic for compatibility, but use valid types/statuses
        average_order_value = 0
        pending_inquiries_count = orders_qs.filter(
            type="inquiry",
            status__in=["created", "in_progress"],
        ).count()

        # Upcoming appointments (next 7 days) based on active orders
        upcoming_appointments = (
            orders_qs.filter(
                status__in=["created", "in_progress"],
                created_at__date__gte=today,
                created_at__date__lte=today + timedelta(days=7),
            )
            .select_related("customer")
            .order_by("created_at")[:5]
        )

        # Top customers by order count
        from django.db.models import Max

        top_customers = (
            customers_qs.annotate(
                order_count=Count("orders"),
                latest_order_date=Max("orders__created_at")
            )
            .filter(order_count__gt=0)
            .order_by("-order_count")[:5]
        )

        status_percentages = {}
        for s, c in status_counts.items():
            status_percentages[f"{s}_percent"] = (c / total_orders * 100) if total_orders > 0 else 0

        # Get inventory metrics
        from django.db.models import Sum
        from tracker.models import InventoryItem

        # Total inventory items count
        total_inventory_items = InventoryItem.objects.count()
        
        # Sum of all quantities in stock
        total_stock = InventoryItem.objects.aggregate(total=Sum('quantity'))['total'] or 0
        
        # Count of low stock items (quantity <= reorder_level)
        low_stock_count = InventoryItem.objects.filter(quantity__lte=F('reorder_level')).count()
        
        # Count of out of stock items
        out_of_stock_count = InventoryItem.objects.filter(quantity=0).count()
        
        # Revenue KPI Calculations from Invoices
        # New accurate metrics: Gross Revenue (subtotal + VAT) for both this month and all-time
        from tracker.models import Invoice

        gross_revenue_this_month = Decimal('0')
        total_gross_revenue = Decimal('0')
        net_revenue_this_month = Decimal('0')
        total_net_revenue = Decimal('0')
        vat_this_month = Decimal('0')
        total_vat = Decimal('0')
        avg_invoice_amount = Decimal('0')
        invoices_this_month_count = 0
        revenue_by_branch_tsh = {}

        try:
            # Scope invoices to user's branch/permissions
            invoices_qs = scope_queryset(Invoice.objects.all(), request.user, request)

            # Calculate total gross revenue (all time) = sum of total_amount (subtotal + VAT)
            total_gross_sums = invoices_qs.aggregate(
                total_gross=Sum('total_amount'),
                total_net=Sum('subtotal'),
                total_vat_sum=Sum('tax_amount'),
                total_count=Count('id'),
                avg_gross=Avg('total_amount')
            )

            if total_gross_sums.get('total_gross') is not None:
                total_gross_revenue = Decimal(str(total_gross_sums.get('total_gross')))

            if total_gross_sums.get('total_net') is not None:
                total_net_revenue = Decimal(str(total_gross_sums.get('total_net')))

            if total_gross_sums.get('total_vat_sum') is not None:
                total_vat = Decimal(str(total_gross_sums.get('total_vat_sum')))

            if total_gross_sums.get('avg_gross') is not None:
                avg_invoice_amount = Decimal(str(total_gross_sums.get('avg_gross')))

            # Month ranges for this month's calculations
            today_date = timezone.localdate()
            month_start = today_date.replace(day=1)
            month_end = today_date

            # Get this month's invoices using created_at for accuracy
            # (invoice_date might not be set correctly for extracted invoices)
            month_start_datetime = timezone.make_aware(datetime.combine(month_start, datetime.min.time()))
            month_end_datetime = timezone.make_aware(datetime.combine(month_end, datetime.max.time()))

            month_invoices = invoices_qs.filter(
                created_at__gte=month_start_datetime,
                created_at__lte=month_end_datetime
            )

            # Calculate gross revenue this month (subtotal + VAT)
            month_gross_sums = month_invoices.aggregate(
                month_gross=Sum('total_amount'),
                month_net=Sum('subtotal'),
                month_vat_sum=Sum('tax_amount'),
                month_count=Count('id')
            )

            if month_gross_sums.get('month_gross') is not None:
                gross_revenue_this_month = Decimal(str(month_gross_sums.get('month_gross')))

            if month_gross_sums.get('month_net') is not None:
                net_revenue_this_month = Decimal(str(month_gross_sums.get('month_net')))

            if month_gross_sums.get('month_vat_sum') is not None:
                vat_this_month = Decimal(str(month_gross_sums.get('month_vat_sum')))

            if month_gross_sums.get('month_count') is not None:
                invoices_this_month_count = month_gross_sums.get('month_count')

            # Daily revenue calculations (today's invoices)
            today_date = timezone.localdate()
            today_start_datetime = timezone.make_aware(datetime.combine(today_date, datetime.min.time()))
            today_end_datetime = timezone.make_aware(datetime.combine(today_date, datetime.max.time()))

            today_invoices = invoices_qs.filter(
                created_at__gte=today_start_datetime,
                created_at__lte=today_end_datetime
            )

            # Calculate gross revenue today
            today_gross_revenue = Decimal('0')
            today_net_revenue = Decimal('0')
            today_vat = Decimal('0')

            today_gross_sums = today_invoices.aggregate(
                today_gross=Sum('total_amount'),
                today_net=Sum('subtotal'),
                today_vat_sum=Sum('tax_amount')
            )

            if today_gross_sums.get('today_gross') is not None:
                today_gross_revenue = Decimal(str(today_gross_sums.get('today_gross')))

            if today_gross_sums.get('today_net') is not None:
                today_net_revenue = Decimal(str(today_gross_sums.get('today_net')))

            if today_gross_sums.get('today_vat_sum') is not None:
                today_vat = Decimal(str(today_gross_sums.get('today_vat_sum')))

            # Revenue by branch (Gross Value)
            branch_sums = invoices_qs.values('branch__name').annotate(
                total=Sum('total_amount')
            ).order_by('branch__name')

            for item in branch_sums:
                branch_name = item['branch__name'] or 'Unassigned'
                amount = Decimal(str(item['total'])) if item['total'] is not None else Decimal('0')
                revenue_by_branch_tsh[branch_name] = amount

        except Exception as e:
            logger.error(f"Error aggregating revenue KPIs from invoices: {e}")
            gross_revenue_this_month = Decimal('0')
            total_gross_revenue = Decimal('0')
            net_revenue_this_month = Decimal('0')
            total_net_revenue = Decimal('0')
            vat_this_month = Decimal('0')
            total_vat = Decimal('0')
            avg_invoice_amount = Decimal('0')
            invoices_this_month_count = 0
            revenue_by_branch_tsh = {}

        # Revenue breakdown by order type
        revenue_by_type = {}
        revenue_by_type_this_month = {}
        revenue_by_type_today = {}
        try:
            from tracker.utils.revenue_utils import get_revenue_by_order_type

            # All-time revenue by type
            revenue_by_type = get_revenue_by_order_type(invoices_qs)

            # This month's revenue by type
            revenue_by_type_this_month = get_revenue_by_order_type(month_invoices)

            # Today's revenue by type
            revenue_by_type_today = get_revenue_by_order_type(today_invoices)
        except Exception as e:
            logger.warning(f"Error calculating revenue by order type: {e}")
            revenue_by_type = {
                'sales': Decimal('0'),
                'service': Decimal('0'),
                'labour': Decimal('0'),
                'unknown': Decimal('0'),
                'total': Decimal('0'),
                'count': 0,
            }
            revenue_by_type_this_month = {
                'sales': Decimal('0'),
                'service': Decimal('0'),
                'labour': Decimal('0'),
                'unknown': Decimal('0'),
                'total': Decimal('0'),
                'count': 0,
            }
            revenue_by_type_today = {
                'sales': Decimal('0'),
                'service': Decimal('0'),
                'labour': Decimal('0'),
                'unknown': Decimal('0'),
                'total': Decimal('0'),
                'count': 0,
            }

        metrics = {
            'total_orders': total_orders,
            'completed_orders': completed_orders,
            'completed_today': completed_today_count,
            'new_orders_today': new_orders_today,
            'total_customers': total_customers,
            'completion_rate': round(completion_rate, 1),
            'status_counts': status_counts,
            'type_counts': type_counts,
            'priority_counts': priority_counts,
            'new_customers_this_month': new_customers_this_month,
            'pending_inquiries_count': pending_inquiries_count,
            'average_order_value': average_order_value,
            # Revenue KPIs - Fresh calculation based on Gross Revenue (subtotal + VAT)
            'gross_revenue_this_month': gross_revenue_this_month,      # Gross revenue this month
            'total_gross_revenue': total_gross_revenue,                # Total gross revenue (all time)
            'net_revenue_this_month': net_revenue_this_month,          # Net revenue this month (subtotal)
            'total_net_revenue': total_net_revenue,                    # Total net revenue (all time)
            'vat_this_month': vat_this_month,                          # VAT this month
            'total_vat': total_vat,                                    # Total VAT (all time)
            'avg_invoice_amount': avg_invoice_amount,                  # Average invoice amount
            'invoices_this_month_count': invoices_this_month_count,    # Number of invoices this month
            # Daily revenue KPIs
            'today_gross_revenue': today_gross_revenue,                # Gross revenue today
            'today_net_revenue': today_net_revenue,                    # Net revenue today
            'today_vat': today_vat,                                    # VAT today
            'revenue_by_branch_tsh': revenue_by_branch_tsh,
            # Revenue breakdown by order type
            'revenue_by_type': revenue_by_type,
            'revenue_by_type_this_month': revenue_by_type_this_month,
            'revenue_by_type_today': revenue_by_type_today,
            'upcoming_appointments': list(upcoming_appointments.values('id', 'customer__full_name', 'created_at')),
            'top_customers': list(top_customers.values('id', 'full_name', 'order_count', 'phone', 'email', 'total_spent', 'latest_order_date', 'registration_date')),
            'recent_orders': list(orders_qs.select_related("customer").exclude(status="completed").order_by("-created_at").values('id', 'customer__full_name', 'status', 'created_at')[:10]),
            'inventory_metrics': {
                'total_items': total_inventory_items,
                'total_stock': total_stock,
                'low_stock_count': low_stock_count,
                'out_of_stock_count': out_of_stock_count,
            }
        }
        # Don't cache metrics to ensure fresh data
        # cache.set(cache_key, metrics, 60)

    # Always fresh data for fast-updating sections
    recent_orders = list(
        orders_qs.select_related("customer").exclude(status="completed").order_by("-created_at")[:10]
    )
    # Fix completed today calculation - MySQL compatible
    from datetime import date
    from .utils.mysql_compat import today_filter, get_date_range
    
    today = timezone.now().date()
    today_start, today_end = get_date_range(today)
    
    # Count completed orders by completed_at date (if set) or created_at date - MySQL compatible
    completed_today = orders_qs.filter(
        status="completed"
    ).filter(
        Q(completed_at__gte=today_start, completed_at__lte=today_end) |
        Q(completed_at__isnull=True, created_at__gte=today_start, created_at__lte=today_end)
    ).count()

    # Use completed_today from metrics if available, otherwise calculate fresh
    completed_today_final = metrics.get('completed_today', completed_today)
    
    context = {**metrics, "recent_orders": recent_orders, "completed_today": completed_today_final, "current_time": timezone.now(), "revenue_by_branch_tsh": revenue_by_branch_tsh}
    # render after charts

    # Build sales_chart_json (monthly Orders vs Completed for last 12 months)
    from django.db.models.functions import TruncMonth

    # Last 12 months for type 'sales'
    last_months = [(today.replace(day=1) - timezone.timedelta(days=1)).replace(day=1)]
    for _ in range(11):
        prev = (last_months[-1] - timezone.timedelta(days=1)).replace(day=1)
        last_months.append(prev)
    last_months = list(reversed(last_months))

    # Simplified monthly data without TruncMonth to avoid timezone issues
    monthly_total_map = {}
    monthly_completed_map = {}
    
    try:
        # Get orders from last 12 months without complex date truncation
        twelve_months_ago = today - timedelta(days=365)
        
        total_orders = orders_qs.filter(type="sales", created_at__date__gte=twelve_months_ago).count()
        completed_orders = orders_qs.filter(type="sales", status="completed", created_at__date__gte=twelve_months_ago).count()
        
        # Use current month as key for simplicity
        current_month = today.replace(day=1)
        monthly_total_map[current_month] = total_orders
        monthly_completed_map[current_month] = completed_orders
    except Exception:
        pass

    def _month_label(d):
        return d.strftime("%b %Y")

    sales_chart = {
        "labels": [_month_label(m) for m in last_months],
        "total": [monthly_total_map.get(m, 0) for m in last_months],
        "completed": [monthly_completed_map.get(m, 0) for m in last_months],
    }

    # Periodized datasets
    curr_month_start = today.replace(day=1)
    curr_days = [curr_month_start + timezone.timedelta(days=i) for i in range((today - curr_month_start).days + 1)]

    # Simplified daily data
    daily_total_prev_map = {}
    daily_completed_prev_map = {}
    
    try:
        # Get today's data without complex date truncation
        today_total = orders_qs.filter(type="sales", created_at__date=today).count()
        today_completed = orders_qs.filter(type="sales", status="completed", created_at__date=today).count()
        
        daily_total_prev_map[today] = today_total
        daily_completed_prev_map[today] = today_completed
    except Exception:
        pass
    sales_last_month = {
        "labels": [d.strftime("%Y-%m-%d") for d in curr_days],
        "total": [daily_total_prev_map.get(d, 0) for d in curr_days],
        "completed": [daily_completed_prev_map.get(d, 0) for d in curr_days],
    }

    last_7_days = [today - timezone.timedelta(days=i) for i in range(6, -1, -1)]
    # Simplified weekly data
    daily_total_map = {}
    daily_completed_map = {}
    
    try:
        # Get last 7 days data
        for i in range(7):
            date = today - timedelta(days=i)
            total = orders_qs.filter(type="sales", created_at__date=date).count()
            completed = orders_qs.filter(type="sales", status="completed", created_at__date=date).count()
            daily_total_map[date] = total
            daily_completed_map[date] = completed
    except Exception:
        pass
    sales_last_week = {
        "labels": [d.strftime("%Y-%m-%d") for d in last_7_days],
        "total": [daily_total_map.get(d, 0) for d in last_7_days],
        "completed": [daily_completed_map.get(d, 0) for d in last_7_days],
    }

    from django.db.models.functions import TruncHour
    hourly_total_qs = orders_qs.filter(type="sales", created_at__date=today).annotate(h=TruncHour("created_at")).values("h").annotate(c=Count("id"))
    hourly_completed_qs = orders_qs.filter(type="sales", status="completed", completed_at__date=today).annotate(h=TruncHour("completed_at")).values("h").annotate(c=Count("id"))
    hourly_total_map = {row["h"].hour: row["c"] for row in hourly_total_qs if row["h"]}
    hourly_completed_map = {row["h"].hour: row["c"] for row in hourly_completed_qs if row["h"]}
    hours = list(range(0, 24))
    sales_today = {"labels": [f"{h:02d}:00" for h in hours], "total": [hourly_total_map.get(h, 0) for h in hours], "completed": [hourly_completed_map.get(h, 0) for h in hours]}

    sales_periods = {"last_year": sales_chart, "last_month": sales_last_month, "last_week": sales_last_week, "today": sales_today}

    # Sparkline last 8 days
    last_8_days = [today - timezone.timedelta(days=i) for i in range(7, -1, -1)]
    total_order_spark = {
        "labels": [d.strftime("%Y-%m-%d") for d in last_8_days],
        "total": [daily_total_map.get(d, 0) for d in last_8_days],
        "completed": [daily_completed_map.get(d, 0) for d in last_8_days],
    }

    # Top customers by orders per period
    def _period_range(name):
        if name == "today":
            return today, today
        if name == "yesterday":
            y = today - timezone.timedelta(days=1)
            return y, y
        if name == "last_week":
            return today - timezone.timedelta(days=6), today
        # last_month (previous calendar month)
        start = (today.replace(day=1) - timezone.timedelta(days=1)).replace(day=1)
        end = today.replace(day=1) - timezone.timedelta(days=1)
        return start, end

    top_orders_json_data = {}
    for p in ["today", "yesterday", "last_week", "last_month"]:
        start_d, end_d = _period_range(p)
        rows = (
            orders_qs.filter(created_at__date__gte=start_d, created_at__date__lte=end_d)
            .values("customer__full_name")
            .annotate(c=Count("id"))
            .order_by("-c")[:5]
        )
        top_orders_json_data[p] = {
            "labels": [r["customer__full_name"] or "Unknown" for r in rows],
            "values": [r["c"] for r in rows],
        }

    # Add inventory metrics to context
    inventory_metrics = metrics.get('inventory_metrics', {})
    
    branches = list(Branch.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
    context = {
        **metrics,
        "recent_orders": recent_orders,
        "completed_today": completed_today,
        "current_time": timezone.now(),
        "sales_chart_json": json.dumps(sales_chart),
        "sales_chart_periods_json": json.dumps(sales_periods),
        "total_order_spark_json": json.dumps(total_order_spark),
        "top_orders_json": json.dumps(top_orders_json_data),
        "inventory_metrics": inventory_metrics,  # Add inventory metrics to template context
        "branches": branches,
    }
    return render(request, "tracker/dashboard.html", context)


@login_required
def customers_list(request: HttpRequest):
    from django.db.models import Q
    q = request.GET.get('q','').strip()
    f_type = request.GET.get('type','').strip()
    f_status = request.GET.get('status','').strip()

    # Exclude temporary customers (those with full_name starting with "Plate " and phone starting with "PLATE_")
    customers_qs = scope_queryset(Customer.objects.all(), request.user, request).exclude(
        full_name__startswith='Plate ',
        phone__startswith='PLATE_'
    )
    qs = customers_qs.order_by('-registration_date')
    if q:
        qs = qs.filter(
            Q(full_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(code__icontains=q)
        )
    if f_type:
        qs = qs.filter(customer_type=f_type)

    # Stats - fix calculations with current date
    from datetime import date
    today_date = date.today()

    # Apply status filters based on today's activity and visit history
    if f_status == 'active':
        # Active today: customers who visited today (based on last_visit date)
        qs = qs.filter(last_visit__date=today_date)
    elif f_status == 'inactive':
        # Inactive: customers who have never visited or didn't visit today
        qs = qs.filter(total_visits=0)
    elif f_status == 'returning':
        # Returning: customers with more than 1 total visit
        qs = qs.filter(total_visits__gt=1)

    # KPI calculations for header
    active_customers = customers_qs.filter(last_visit__date=today_date).count()
    new_customers_today = customers_qs.filter(registration_date__date=today_date).count()
    returning_customers = customers_qs.filter(total_visits__gt=1).count()

    paginator = Paginator(qs, 20)
    page = request.GET.get('page')
    customers = paginator.get_page(page)
    branches = list(Branch.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
    return render(request, "tracker/customers_list.html", {
        "customers": customers,
        "q": q,
        "active_customers": active_customers,
        "new_customers_today": new_customers_today,
        "returning_customers": returning_customers,
        "branches": branches,
    })


@login_required
def customers_search(request: HttpRequest):
    q = request.GET.get("q", "").strip()
    customer_id = request.GET.get("id")
    recent = request.GET.get("recent")
    details = request.GET.get("details")

    results = []
    customers_qs = scope_queryset(Customer.objects.all(), request.user, request)
    if details:
        customers_qs = customers_qs.prefetch_related('vehicles')

    if customer_id:
        try:
            customer = customers_qs.get(id=customer_id)
            results = [customer]
        except Customer.DoesNotExist:
            pass
    elif recent:
        results = customers_qs.order_by('-last_visit', '-registration_date')[:10]
    elif q:
        q_upper = q.upper()
        exact_plate_qs = customers_qs.filter(vehicles__plate_number__iexact=q_upper)
        if exact_plate_qs.exists():
            results = exact_plate_qs.order_by('-last_visit', '-registration_date')[:10]
        else:
            results = customers_qs.filter(
                Q(full_name__icontains=q) |
                Q(phone__icontains=q) |
                Q(email__icontains=q) |
                Q(code__icontains=q) |
                Q(vehicles__plate_number__istartswith=q_upper)
            ).distinct().order_by('-last_visit', '-registration_date')[:20]

    data = []
    for c in results:
        item = {
            "id": c.id,
            "code": c.code,
            "name": c.full_name,
            "phone": c.phone,
            "email": c.email or '',
            "type": c.customer_type or 'personal',
            "customer_type_display": c.get_customer_type_display() if c.customer_type else 'Personal',
            "last_visit": c.last_visit.isoformat() if c.last_visit else None,
            "total_visits": c.total_visits,
            "address": c.address or '',
        }
        if details and customer_id:
            item.update({
                "organization_name": c.organization_name or '',
                "tax_number": c.tax_number or '',
                "personal_subtype": c.personal_subtype or '',
                "current_status": c.current_status or '',
                "registration_date": c.registration_date.isoformat() if c.registration_date else None,
                "vehicles": [
                    {"id": v.id, "plate_number": v.plate_number, "make": v.make or '', "model": v.model or ''}
                    for v in c.vehicles.all()
                ],
                "orders": [
                    {"id": o.id, "order_number": o.order_number, "type": o.type, "status": o.status, "created_at": o.created_at.isoformat()}
                    for o in c.orders.order_by('-created_at')[:5]
                ],
            })
        data.append(item)
    return JsonResponse({"results": data})


@login_required
def api_customers_summary(request: HttpRequest):
    ids = (request.GET.get('ids') or '').strip()
    if not ids:
        return JsonResponse({'success': False, 'error': 'ids required'})
    try:
        id_list = [int(x) for x in ids.split(',') if x.isdigit()]
    except Exception:
        id_list = []
    qs = scope_queryset(Customer.objects.filter(id__in=id_list), request.user, request)
    payload = {}
    for c in qs:
        payload[str(c.id)] = {
            'last_visit': c.last_visit.isoformat() if c.last_visit else None,
            'total_visits': c.total_visits or 0,
            'customer_type': c.customer_type or 'personal',
        }
    return JsonResponse({'success': True, 'customers': payload})


@login_required
def api_customers_list(request: HttpRequest):
    """API endpoint to get all customers for dropdown selection (e.g., inquiries)"""
    qs = scope_queryset(Customer.objects.all().order_by('full_name'), request.user, request)
    customers = [
        {
            'id': c.id,
            'full_name': c.full_name,
            'phone': c.phone or '',
            'customer_type': c.customer_type or 'personal',
        }
        for c in qs
    ]
    return JsonResponse({'success': True, 'customers': customers})


@login_required
def customer_detail(request: HttpRequest, pk: int):
    customers_qs = scope_queryset(Customer.objects.all(), request.user, request)
    c = get_object_or_404(customers_qs, pk=pk)
    vehicles = c.vehicles.all()
    orders = c.orders.order_by("-created_at")[:20]

    # Flash info when redirected from registration duplicate detection
    if request.GET.get('flash') == 'existing_customer':
        messages.info(request, f"Customer '{c.full_name}' already exists. You have been redirected to their profile.")

    # Charts: last 6 months order trend and status distribution
    from django.db.models import Count
    from django.db.models.functions import TruncMonth
    from calendar import month_abbr

    today = timezone.localdate().replace(day=1)
    months = []
    m_ptr = today
    for _ in range(6):
        months.append(m_ptr)
        # move back one month safely
        prev = (m_ptr - timezone.timedelta(days=1)).replace(day=1)
        m_ptr = prev
    months = list(reversed(months))

    month_counts_qs = (
        scope_queryset(Order.objects.filter(customer=c), request.user, request)
        .annotate(m=TruncMonth("created_at"))
        .values("m")
        .annotate(c=Count("id"))
    )
    month_map = {row["m"].date(): row["c"] for row in month_counts_qs if row["m"]}
    cd_trend = {
        "labels": [f"{month_abbr[m.month]} {m.year}" for m in months],
        "values": [month_map.get(m, 0) for m in months],
    }

    status_qs = (
        scope_queryset(Order.objects.filter(customer=c), request.user, request)
        .values("status")
        .annotate(c=Count("id"))
    )
    status_labels = []
    status_values = []
    for row in status_qs:
        label = (row["status"] or "").replace("_", " ").title()
        status_labels.append(label or "Unknown")
        status_values.append(row["c"])
    cd_status = {"labels": status_labels, "values": status_values}

    return render(request, "tracker/customer_detail.html", {
        'customer': c,
        'vehicles': vehicles,
        'orders': orders,
        'page_title': c.full_name,
        'cd_trend': json.dumps(cd_trend),
        'cd_status': json.dumps(cd_status),
    })


@login_required
def add_customer_note(request: HttpRequest, pk: int):
    """Add or update a note on a customer's profile"""
    customers_qs_note = scope_queryset(Customer.objects.all(), request.user, request)
    customer = get_object_or_404(customers_qs_note, pk=pk)
    note_id = request.POST.get('note_id')

    if request.method == 'POST':
        note_content = request.POST.get('note', '').strip()
        if note_content:
            try:
                if note_id:  # Update existing note
                    note = get_object_or_404(CustomerNote, id=note_id, customer=customer)
                    note.content = note_content
                    note.save()
                    action = 'updated'
                else:  # Create new note
                    note = CustomerNote.objects.create(
                        customer=customer,
                        content=note_content,
                        created_by=request.user
                    )
                    action = 'added'
                
                # Log the action
                add_audit_log(
                    user=request.user,
                    action_type=f'customer_note_{action}',
                    description=f'{action.capitalize()} a note for customer {customer.full_name}',
                    customer_id=customer.id,
                    note_id=note.id
                )
                
                messages.success(request, f'Note {action} successfully.')
            except Exception as e:
                messages.error(request, f'Error saving note: {str(e)}')
        else:
            messages.error(request, 'Note content cannot be empty.')
    
    # Redirect back to the customer detail page
    return redirect('tracker:customer_detail', pk=customer.id)


def delete_customer_note(request: HttpRequest, customer_id: int, note_id: int):
    """Delete a customer note"""
    if request.method == 'POST':
        try:
            note = get_object_or_404(CustomerNote, id=note_id, customer_id=customer_id)
            
            # Log the action before deletion
            add_audit_log(
                user=request.user,
                action_type='customer_note_deleted',
                description=f'Deleted a note for customer {note.customer.full_name}',
                customer_id=customer_id,
                note_id=note_id
            )
            
            note.delete()
            return JsonResponse({'success': True})
            
        except Exception as e:
            return JsonResponse(
                {'success': False, 'error': str(e)}, 
                status=400
            )
    
    return JsonResponse(
        {'success': False, 'error': 'Invalid request method'}, 
        status=405
    )


@login_required
def customer_register(request: HttpRequest):
    # Get the current step from POST or GET, default to 1
    step = int(request.POST.get("step", request.GET.get("step", 1)))
    is_ajax = request.headers.get('X-Requested-With') == 'XMLHttpRequest'
    load_step = request.GET.get('load_step') == '1'  # Check if this is a step load request
    
    def get_form_errors(form):
        errors = {}
        for field in form:
            if field.errors:
                errors[field.name] = [str(error) for error in field.errors]
        return errors
    
    def get_template_context(step, form, **kwargs):
        # Get inventory items for new single dropdown system
        inventory_items = InventoryItem.objects.select_related('brand').filter(is_active=True, brand__isnull=False).order_by('brand__name', 'name')
        
        # Build item data mapping for JavaScript
        item_data = {}
        for item in inventory_items:
            if item.name and item.brand:
                item_data[str(item.id)] = {
                    'name': item.name,
                    'brand': item.brand.name,
                    'quantity': item.quantity
                }
        
        # Load dynamic service types and sales add-ons for steps that need them
        try:
            from .models import ServiceType, ServiceAddon
            svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
            addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
            service_types = [{
                'name': s.name,
                'estimated_minutes': int(s.estimated_minutes or 0)
            } for s in svc_qs]
            sales_addons = [{
                'name': a.name,
                'estimated_minutes': int(a.estimated_minutes or 0)
            } for a in addon_qs]
        except Exception:
            service_types = []
            sales_addons = []

        context = {
            'step': step,
            'form': form,
            'intent': request.session.get('reg_step2', {}).get('intent'),
            'step1': request.session.get('reg_step1', {}),
            'step2': request.session.get('reg_step2', {}),
            'step3': request.session.get('reg_step3', {}),
            'today': timezone.now().date(),
            'brands': Brand.objects.filter(is_active=True),
            'inventory_items': inventory_items,
            'item_data_json': json.dumps(item_data),
            'service_types': service_types,
            'sales_addons': sales_addons,
            'service_offers': [
                'Oil Change', 'Engine Diagnostics', 'Brake Repair', 'Tire Rotation',
                'Wheel Alignment', 'Battery Check', 'Fluid Top-Up', 'General Maintenance'
            ],
            **kwargs
        }

        # For Step 3 Inquiry, prepare an OrderForm so template can render proper dropdowns
        try:
            if step == 3 and context.get('intent') == 'inquiry':
                type_map = {"inquiry": "inquiry"}
                initial = {"type": type_map.get('inquiry', 'inquiry')}
                context['order_form'] = OrderForm(initial=initial)
                context['order_type'] = initial['type']
        except Exception:
            pass
        return context
    
    def render_form(step, form, **kwargs):
        context = get_template_context(step, form, **kwargs)
        return render(request, 'tracker/partials/customer_registration_form.html', context)
    
    def json_response(success, form=None, redirect_url=None, **kwargs):
        response_data = {
            'success': success,
            'redirect_url': redirect_url,
            **kwargs
        }
        
        if form is not None:
            if form.is_valid():
                response_data['form_html'] = render_form(step, form).content.decode('utf-8')
            else:
                response_data['errors'] = get_form_errors(form)
                response_data['form_html'] = render_form(step, form).content.decode('utf-8')
        
        return JsonResponse(response_data)
    
    # Handle GET request for loading a specific step via AJAX
    if request.method == 'GET' and is_ajax and load_step:
        form_class = {
            1: CustomerStep1Form,
            2: CustomerStep2Form,
            3: CustomerStep3Form,
            4: CustomerStep4Form
        }.get(step, CustomerStep1Form)
        
        # Initialize form with session data if available
        form_data = request.session.get(f'reg_step{step}', {})
        form = form_class(initial=form_data)
        
        # Render the form template
        ctx = get_template_context(step, form)
        # Provide complete context for step 4 to control which section renders and prefill fields
        if step == 4:
            _intent = ctx.get('intent')
            type_map = {"service": "service", "sales": "sales", "inquiry": "inquiry"}
            order_initial = {"type": type_map.get(_intent)} if _intent in type_map else {}
            # Infer when needed from step3
            step3d = ctx.get('step3') or {}
            if not order_initial.get('type'):
                inferred = None
                if step3d.get('item_name') or step3d.get('brand') or step3d.get('quantity'):
                    inferred = 'sales'
                elif step3d.get('service_selection') or step3d.get('service_type'):
                    inferred = 'service'
                else:
                    inferred = 'inquiry'
                order_initial['type'] = inferred
            # Prefill from step3
            if order_initial['type'] == 'service':
                sel_services = step3d.get('service_selection') or step3d.get('service_type') or []
                if sel_services:
                    order_initial['service_selection'] = sel_services
                if step3d.get('description'):
                    order_initial['description'] = step3d.get('description')
                if step3d.get('estimated_duration'):
                    order_initial['estimated_duration'] = step3d.get('estimated_duration')
            elif order_initial['type'] == 'sales':
                if step3d.get('item_id'):
                    order_initial['item_name'] = step3d.get('item_id')
                if step3d.get('quantity'):
                    order_initial['quantity'] = step3d.get('quantity')
                if step3d.get('tire_type'):
                    order_initial['tire_type'] = step3d.get('tire_type')
                if step3d.get('brand'):
                    order_initial['brand'] = step3d.get('brand')
                if step3d.get('description'):
                    order_initial['description'] = step3d.get('description')
            elif order_initial['type'] == 'inquiry':
                if step3d.get('priority'):
                    order_initial['priority'] = step3d.get('priority')
                if step3d.get('inquiry_type'):
                    order_initial['inquiry_type'] = step3d.get('inquiry_type')
                if step3d.get('questions'):
                    order_initial['questions'] = step3d.get('questions')
                if step3d.get('contact_preference'):
                    order_initial['contact_preference'] = step3d.get('contact_preference')
                followup = step3d.get('followup_date') or step3d.get('follow_up_date')
                if followup:
                    order_initial['follow_up_date'] = followup
            ctx['order_form'] = OrderForm(initial=order_initial)
            ctx['order_type'] = order_initial.get('type')
            ctx['vehicle_form'] = VehicleForm()
        form_html = render_to_string('tracker/partials/customer_registration_form.html', ctx, request=request)

        return JsonResponse({
            'success': True,
            'form_html': form_html,
            'step': step
        })
    
    # Handle form submission
    if request.method == "POST":
        # Global quick-save: allow saving just the customer from any step
        save_only_flag = request.POST.get("save_only") == "1"
        if save_only_flag and step != 1:
            step1_data = request.session.get("reg_step1", {}) or {}
            # Minimal validation
            full_name = (step1_data.get("full_name") or '').strip()
            phone = (step1_data.get("phone") or '').strip()
            if not full_name:
                if is_ajax:
                    return json_response(False, message="Please complete Step 1 (customer info) before saving.", message_type="error")
                messages.error(request, "Please complete Step 1 (customer info) before saving.")
                return redirect(f"{reverse('tracker:customer_register')}?step=1")
            # Duplicate handling and creation using centralized service
            from .services import CustomerService
            user_branch = get_user_branch(request.user)

            try:
                c, created = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=full_name,
                    phone=phone,
                    whatsapp=step1_data.get("whatsapp"),
                    email=step1_data.get("email"),
                    address=step1_data.get("address"),
                    notes=step1_data.get("notes"),
                    customer_type=step1_data.get("customer_type"),
                    organization_name=step1_data.get("organization_name"),
                    tax_number=step1_data.get("tax_number"),
                    personal_subtype=step1_data.get("personal_subtype"),
                )

                if not created:
                    # Customer already exists
                    if is_ajax:
                        dup_url = reverse("tracker:customer_detail", kwargs={'pk': c.id}) + "?flash=existing_customer"
                        return json_response(False, message=f"Customer '{full_name}' already exists.", message_type="info", redirect_url=dup_url)
                    messages.info(request, f"Customer '{full_name}' already exists. Redirected to their profile.")
                    return redirect("tracker:customer_detail", pk=c.id)
            except Exception as e:
                logger.warning(f"Error creating customer in save_only flow: {e}")
                if is_ajax:
                    return json_response(False, message="Error creating customer. Please try again.", message_type="error")
                messages.error(request, "Error creating customer. Please try again.")
                return redirect(f"{reverse('tracker:customer_register')}?step=1")
            # Clear session step1 after save
            request.session.pop('reg_step1', None)
            if is_ajax:
                return json_response(True, message="Customer saved successfully", message_type="success", redirect_url=reverse("tracker:customer_detail", kwargs={'pk': c.id}))
            messages.success(request, "Customer saved successfully")
            return redirect("tracker:customer_detail", pk=c.id)
        if step == 1:
            form = CustomerStep1Form(request.POST)
            action = request.POST.get("action")
            save_only = request.POST.get("save_only") == "1"

            if form.is_valid():
                data = form.cleaned_data
                full_name = data.get("full_name")
                phone = data.get("phone")
                user_branch = get_user_branch(request.user)

                # Check for existing customer using CustomerService
                from .services import CustomerService
                try:
                    existing_customer = CustomerService.find_duplicate_customer(
                        branch=user_branch,
                        full_name=full_name,
                        phone=phone,
                        organization_name=data.get("organization_name"),
                        tax_number=data.get("tax_number"),
                        customer_type=data.get("customer_type")
                    )
                except Exception as e:
                    logger.warning(f"Error checking for duplicate customer: {e}")
                    existing_customer = None

                # If customer exists and user can access it, redirect or show message
                if existing_customer:
                    can_access = request.user.is_superuser or (
                        user_branch is not None and existing_customer.branch_id == user_branch.id
                    )

                    if can_access:
                        # Same branch: redirect to existing customer
                        dup_url = reverse("tracker:customer_detail", kwargs={'pk': existing_customer.id}) + "?flash=existing_customer"
                        if is_ajax:
                            return json_response(
                                False,
                                form=form,
                                message=f"Customer '{full_name}' already exists. Redirected to their profile.",
                                message_type='info',
                                redirect_url=dup_url
                            )
                        messages.info(request, f"Customer '{full_name}' already exists. Redirected to their profile.")
                        return redirect(dup_url)
                    else:
                        # Different branch: allow creation but warn
                        if is_ajax:
                            messages.warning(request, f"A customer with the same details exists in another branch. A separate customer will be created in your branch.")
                        else:
                            messages.warning(request, f"A customer with the same details exists in another branch. A separate customer will be created in your branch.")

                # If action is to save the customer immediately
                if action == "save_customer" or save_only:
                    try:
                        c, created = CustomerService.create_or_get_customer(
                            branch=user_branch,
                            full_name=full_name,
                            phone=phone,
                            whatsapp=data.get("whatsapp"),
                            email=data.get("email"),
                            address=data.get("address"),
                            notes=data.get("notes"),
                            customer_type=data.get("customer_type"),
                            organization_name=data.get("organization_name"),
                            tax_number=data.get("tax_number"),
                            personal_subtype=data.get("personal_subtype"),
                        )

                        # Clear session data after saving
                        request.session.pop('reg_step1', None)
                        request.session.pop('reg_step2', None)
                        request.session.pop('reg_step3', None)

                        if is_ajax:
                            return json_response(
                                True,
                                message="Customer saved successfully",
                                message_type="success",
                                redirect_url=reverse("tracker:customer_detail", kwargs={'pk': c.id})
                            )

                        messages.success(request, "Customer saved successfully")
                        return redirect("tracker:customer_detail", pk=c.id)
                    except Exception as e:
                        logger.warning(f"Error creating customer: {e}")
                        if is_ajax:
                            return json_response(False, form=form, message="Error creating customer", message_type="error")
                        messages.error(request, "Error creating customer")
                        return redirect(f"{reverse('tracker:customer_register')}?step=1")

                # Continue to next step (don't create yet, just save to session)
                request.session["reg_step1"] = form.cleaned_data
                request.session.save()

                if is_ajax:
                    return json_response(True)

                return redirect(f"{reverse('tracker:customer_register')}?step=2")
            else:
                if is_ajax:
                    return json_response(False, form=form)
        
        elif step == 2:
            form = CustomerStep2Form(request.POST)
            if form.is_valid():
                request.session["reg_step2"] = form.cleaned_data
                request.session.save()
                intent = form.cleaned_data.get("intent")
                # If inquiry, skip service type selection and go to step 4
                next_step = 4 if intent == "inquiry" else 3
                
                if is_ajax:
                    return json_response(True, next_step=next_step)

                return redirect(f"{reverse('tracker:customer_register')}?step={next_step}")
            elif is_ajax:
                return json_response(False, form=form)
                
        elif step == 3:
            # Determine intent from previous step to decide handling
            _intent = request.session.get('reg_step2', {}).get('intent')
            # If Inquiry, bypass CustomerStep3Form (which is for service type) and persist inquiry fields
            if _intent == 'inquiry':
                step3_data = {
                    'type': 'inquiry',
                    'priority': (request.POST.get('priority') or '').strip(),
                    'vehicle': (request.POST.get('vehicle') or '').strip(),
                    'inquiry_type': (request.POST.get('inquiry_type') or '').strip(),
                    'contact_preference': (request.POST.get('contact_preference') or '').strip(),
                    'questions': request.POST.get('questions') or '',
                }
                request.session['reg_step3'] = step3_data
                request.session.save()
                if is_ajax:
                    return json_response(True, next_step=4)
                return redirect(f"{reverse('tracker:customer_register')}?step=4")
            # Otherwise validate the normal Step 3 form
            form = CustomerStep3Form(request.POST)
            if form.is_valid():
                # Persist step 3 selections, including non-form fields for dynamic summary/show in step 4
                step3_data = dict(form.cleaned_data)
                if _intent == 'sales':
                    item_id = (request.POST.get('item_name') or '').strip()
                    # Get item details if item_id is provided
                    item_name = ''
                    brand_name = ''
                    if item_id:
                        try:
                            item = InventoryItem.objects.select_related('brand').get(id=item_id)
                            item_name = item.name
                            brand_name = item.brand.name if item.brand else ''
                        except InventoryItem.DoesNotExist:
                            pass
                    
                    step3_data.update({
                        'item_id': item_id,
                        'item_name': item_name,
                        'brand': brand_name,
                        'quantity': (request.POST.get('quantity') or '').strip(),
                        'tire_type': (request.POST.get('tire_type') or '').strip(),
                        'description': request.POST.get('description', '').strip(),
                        'tire_services': request.POST.getlist('tire_services') or [],
                        'estimated_duration': (request.POST.get('estimated_duration') or '').strip(),
                    })
                elif _intent == 'service':
                    step3_data.update({
                        'service_selection': request.POST.getlist('service_selection') or [],
                        'plate_number': request.POST.get('plate_number', '').strip(),
                        'make': request.POST.get('make', '').strip(),
                        'model': request.POST.get('model', '').strip(),
                        'vehicle_type': request.POST.get('vehicle_type', '').strip(),
                        'description': request.POST.get('description', '').strip(),
                        'estimated_duration': request.POST.get('estimated_duration', '').strip(),
                    })
                request.session["reg_step3"] = step3_data
                request.session.save()
                
                if is_ajax:
                    return json_response(True, next_step=4)

                return redirect(f"{reverse('tracker:customer_register')}?step=4")
            elif is_ajax:
                return json_response(False, form=form)
                
        elif step == 4:
            form = CustomerStep4Form(request.POST)
            if form.is_valid():
                # Get all session data
                step1_data = request.session.get("reg_step1", {})
                step2_data = request.session.get("reg_step2", {})
                step3_data = request.session.get("reg_step3", {})
                
                # Validate that we have required data
                if not step1_data.get("full_name"):
                    if is_ajax:
                        return json_response(
                            False,
                            form=form,
                            message="Missing customer information. Please start from Step 1.",
                            message_type="error",
                            redirect_url=f"{reverse('tracker:customer_register')}?step=1"
                        )
                    messages.error(request, "Missing customer information. Please start from Step 1.")
                    return redirect(f"{reverse('tracker:customer_register')}?step=1")
                
                # Check for existing customer with same name and phone
                data = {**step1_data, **form.cleaned_data}
                full_name = data.get("full_name")
                phone = data.get("phone")
                
                # Match DB uniqueness: check same-branch exact duplicate first (branch, full_name, phone, organization_name, tax_number)
                user_branch = get_user_branch(request.user)
                org_name = data.get("organization_name") or None
                tax_num = data.get("tax_number") or None

                existing_same_branch = Customer.objects.filter(
                    branch=user_branch,
                    full_name__iexact=full_name,
                    phone=phone,
                    organization_name=org_name,
                    tax_number=tax_num,
                ).first()

                if existing_same_branch:
                    can_access = getattr(request.user, 'is_superuser', False) or (user_branch is not None and getattr(existing_same_branch, 'branch_id', None) == user_branch.id)
                    if is_ajax and can_access:
                        dup_url = reverse("tracker:customer_detail", kwargs={'pk': existing_same_branch.id}) + "?flash=existing_customer"
                        return json_response(
                            False,
                            form=form,
                            message=f"Customer '{full_name}' with phone '{phone}' already exists in your branch. Redirected to their profile.",
                            message_type="info",
                            redirect_url=dup_url
                        )
                    messages.info(request, f"Customer '{full_name}' with phone '{phone}' already exists in your branch. Redirected to their profile.")
                    detail_url = reverse("tracker:customer_detail", kwargs={'pk': existing_same_branch.id}) + "?flash=existing_customer"
                    return redirect(detail_url)

                # If exists in another branch with same identity, allow creation but warn
                existing_other = Customer.objects.filter(
                    full_name__iexact=full_name,
                    phone=phone,
                    organization_name=org_name,
                    tax_number=tax_num,
                ).exclude(branch=user_branch).first()
                if existing_other:
                    other_branch = getattr(existing_other, 'branch', None)
                    branch_name = getattr(other_branch, 'name', other_branch) if other_branch else 'another branch'
                    messages.warning(request, f"A customer with the same identity exists in {branch_name}. A separate customer will be created for your branch.")

                # Create new customer using centralized service
                from .services import CustomerService
                try:
                    c, _ = CustomerService.create_or_get_customer(
                        branch=user_branch,
                        full_name=full_name,
                        phone=phone,
                        whatsapp=data.get("whatsapp"),
                        email=data.get("email"),
                        address=data.get("address"),
                        notes=data.get("notes") or data.get("additional_notes"),
                        customer_type=data.get("customer_type"),
                        organization_name=org_name,
                        tax_number=tax_num,
                        personal_subtype=data.get("personal_subtype"),
                    )
                except Exception as e:
                    logger.warning(f"Error creating customer at step 4: {e}")
                    if is_ajax:
                        return json_response(False, form=form, message="Error creating customer", message_type="error")
                    messages.error(request, "Error creating customer")
                    return render(request, "tracker/customer_register.html", get_template_context(4, form))
                
                # Create vehicle if vehicle information is provided
                v = None
                intent = step2_data.get("intent")
                service_type = step3_data.get("service_type")
                
                # Get vehicle information from form
                plate_number = request.POST.get("plate_number", "").strip()
                make = request.POST.get("make", "").strip()
                model = request.POST.get("model", "").strip()
                vehicle_type = request.POST.get("vehicle_type", "").strip()
                
                # Create vehicle if any vehicle information is provided using centralized service
                from .services import VehicleService
                if plate_number or make or model or vehicle_type:
                    v = VehicleService.create_or_get_vehicle(
                        customer=c,
                        plate_number=plate_number or None,
                        make=make or None,
                        model=model or None,
                        vehicle_type=vehicle_type or None
                    )

                # Create order based on intent and service type
                o = None
                description = request.POST.get("description", "").strip()
                
                if intent == "sales":
                    # Get data from step 3 session
                    step3_data = request.session.get('reg_step3', {})
                    item_id = step3_data.get('item_id') or request.POST.get("item_name")
                    quantity = step3_data.get('quantity') or request.POST.get("quantity")
                    tire_type = step3_data.get('tire_type') or request.POST.get("tire_type")
                    tire_services = step3_data.get('tire_services', []) or request.POST.getlist("tire_services")

                    if item_id and quantity:
                        try:
                            item = InventoryItem.objects.select_related('brand').get(id=item_id)
                            qty_int = int(quantity)
                            
                            # Check inventory
                            if item.quantity < qty_int:
                                if is_ajax:
                                    return json_response(
                                        False,
                                        form=form,
                                        message=f'Only {item.quantity} in stock for {item.name} ({item.brand.name})',
                                        message_type='error'
                                    )
                                messages.error(request, f'Only {item.quantity} in stock for {item.name} ({item.brand.name})')
                                return render(request, "tracker/customer_register.html", get_template_context(4, form))

                            desc_addons = (", addons: " + ", ".join(tire_services)) if tire_services else ""
                            final_description = description or f"Tire Sales: {item.name} ({item.brand.name}) - {tire_type}{desc_addons}"

                            # Compute estimated duration from selected add-ons
                            est_minutes = 0
                            try:
                                if tire_services:
                                    from .models import ServiceAddon
                                    addons = ServiceAddon.objects.filter(name__in=tire_services, is_active=True)
                                    est_minutes = int(sum(int(a.estimated_minutes or 0) for a in addons))
                            except Exception:
                                est_minutes = 0

                            o = OrderService.create_order(
                                customer=c,
                                order_type="sales",
                                branch=get_user_branch(request.user),
                                vehicle=v,
                                description=final_description,
                                item_name=item.name,
                                brand=item.brand.name,
                                quantity=qty_int,
                                tire_type=tire_type,
                                estimated_duration=est_minutes or None
                            )

                            # Adjust inventory
                            from .utils import adjust_inventory
                            adjust_inventory(item.name, item.brand.name, -qty_int)
                            
                        except InventoryItem.DoesNotExist:
                            if is_ajax:
                                return json_response(False, form=form, message='Selected item not found')
                            messages.error(request, 'Selected item not found in inventory')
                            return render(request, "tracker/customer_register.html", get_template_context(4, form))
                        except ValueError:
                            if is_ajax:
                                return json_response(False, form=form, message='Invalid quantity')
                            messages.error(request, 'Invalid quantity')
                            return render(request, "tracker/customer_register.html", get_template_context(4, form))
                    else:
                        if is_ajax:
                            return json_response(False, form=form, message='Item and quantity are required')
                        messages.error(request, 'Item and quantity are required for sales orders')
                        return render(request, "tracker/customer_register.html", get_template_context(4, form))
                        
                # ... (rest of the code remains the same)
                elif intent == "service":
                    # Get data from step 3 session
                    step3_data = request.session.get('reg_step3', {})
                    selected_svcs = step3_data.get('service_selection', []) or request.POST.getlist('service_selection')
                    desc_svcs = (", services: " + ", ".join(selected_svcs)) if selected_svcs else ""
                    final_description = description or f"Car Service{desc_svcs}"
                    estimated_duration = step3_data.get('estimated_duration') or request.POST.get("estimated_duration")
                    # Derive from selected service types when not provided
                    try:
                        est_int = int(estimated_duration) if estimated_duration else None
                    except (ValueError, TypeError):
                        est_int = None
                    if est_int is None and selected_svcs:
                        try:
                            from .models import ServiceType
                            svc_qs = ServiceType.objects.filter(name__in=selected_svcs, is_active=True)
                            est_int = int(sum(int(s.estimated_minutes or 0) for s in svc_qs)) or None
                        except Exception:
                            est_int = None

                    o = OrderService.create_order(
                        customer=c,
                        order_type="service",
                        branch=get_user_branch(request.user),
                        vehicle=v,
                        description=final_description,
                        estimated_duration=est_int
                    )

                    
                elif intent == "inquiry":
                    # Get data from step 3 session
                    step3_data = request.session.get('reg_step3', {})
                    inquiry_type = step3_data.get('inquiry_type') or request.POST.get("inquiry_type")
                    questions = step3_data.get('questions') or request.POST.get("questions")
                    contact_preference = step3_data.get('contact_preference') or request.POST.get("contact_preference")
                    followup_date = step3_data.get('followup_date') or request.POST.get("followup_date")
                    
                    final_description = description or f"Inquiry: {inquiry_type} - {questions}"

                    o = OrderService.create_order(
                        customer=c,
                        order_type="inquiry",
                        branch=get_user_branch(request.user),
                        vehicle=v,
                        description=final_description,
                        inquiry_type=inquiry_type,
                        questions=questions,
                        contact_preference=contact_preference,
                        follow_up_date=followup_date if followup_date else None
                    )


                # Clear session data
                for key in ["reg_step1", "reg_step2", "reg_step3"]:
                    request.session.pop(key, None)
                
                if is_ajax:
                    if o:
                        return json_response(
                            True,
                            message="Customer registered and order created successfully",
                            message_type="success",
                            redirect_url=reverse("tracker:order_detail", kwargs={'pk': o.id})
                        )
                    else:
                        return json_response(
                            True,
                            message="Customer registered successfully",
                            message_type="success",
                            redirect_url=reverse("tracker:customer_detail", kwargs={'pk': c.id})
                        )
                    
                if o:
                    messages.success(request, "Customer registered and order created successfully")
                    return redirect("tracker:order_detail", pk=o.id)
                else:
                    messages.success(request, "Customer registered successfully")
                    return redirect("tracker:customer_detail", pk=c.id)
            elif is_ajax:
                return json_response(False, form=form)
        
        if is_ajax:
            return json_response(False, form=form)
    
    # Handle GET requests or load_step AJAX requests
    if is_ajax and request.method == 'GET' and 'load_step' in request.GET:
        # Return just the form HTML for AJAX requests
        if step == 1:
            form = CustomerStep1Form(initial=request.session.get("reg_step1"))
        elif step == 2:
            form = CustomerStep2Form(initial=request.session.get("reg_step2"))
        elif step == 3:
            form = CustomerStep3Form(initial=request.session.get("reg_step3"))
        else:
            form = CustomerStep4Form()
        
        return json_response(True, form=form)
    
    # For non-AJAX GET requests, render the full page
    context = {"step": step}
    # Read previously selected intent for conditional rendering
    session_step2 = request.session.get("reg_step2", {}) or {}
    intent = session_step2.get("intent")
    context["intent"] = intent

    # Include previous steps for all steps (for conditional rendering)
    context["step1"] = request.session.get("reg_step1", {})
    context["step2"] = session_step2
    context["step3"] = request.session.get("reg_step3", {})
    context["today"] = timezone.now().date()
    # Get brands and inventory items for all steps
    context["brands"] = Brand.objects.filter(is_active=True)
    inventory_items = InventoryItem.objects.select_related('brand').filter(is_active=True, brand__isnull=False).order_by('brand__name', 'name')
    context["inventory_items"] = inventory_items

    # Build item data mapping for JavaScript
    item_data = {}
    for item in inventory_items:
        if item.name and item.brand:
            item_data[str(item.id)] = {
                'name': item.name,
                'brand': item.brand.name,
                'quantity': item.quantity
            }
    context["item_data_json"] = json.dumps(item_data)

    # Dynamic service types and sales add-ons
    try:
        from .models import ServiceType, ServiceAddon
        svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
        addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
        context["service_types"] = [{
            'name': s.name,
            'estimated_minutes': int(s.estimated_minutes or 0)
        } for s in svc_qs]
        context["sales_addons"] = [{
            'name': a.name,
            'estimated_minutes': int(a.estimated_minutes or 0)
        } for a in addon_qs]
    except Exception:
        context["service_types"] = []
        context["sales_addons"] = []

    context["service_offers"] = [
        'Oil Change', 'Engine Diagnostics', 'Brake Repair', 'Tire Rotation',
        'Wheel Alignment', 'Battery Check', 'Fluid Top-Up', 'General Maintenance'
    ]

    if step == 1:
        context["form"] = CustomerStep1Form(initial=request.session.get("reg_step1"))
    elif step == 2:
        context["form"] = CustomerStep2Form(initial=session_step2)
    elif step == 3:
        context["form"] = CustomerStep3Form(initial=request.session.get("reg_step3"))
    else:
        context["form"] = CustomerStep4Form()
        context["vehicle_form"] = VehicleForm()
        # Prefill order type based on intent and selected services
        type_map = {"service": "service", "sales": "sales", "inquiry": "inquiry"}
        order_initial = {"type": type_map.get(intent)} if intent in type_map else {}
        # Prefill from step3
        step3d = context["step3"] or {}
        # If intent missing, infer from step3 data
        if not order_initial.get("type"):
            inferred = None
            if step3d.get("item_name") or step3d.get("brand") or step3d.get("quantity"):
                inferred = "sales"
            elif step3d.get("service_selection") or step3d.get("service_type"):
                inferred = "service"
            else:
                inferred = "inquiry"
            order_initial["type"] = inferred
            # also set intent in context for template logic
            context["intent"] = context.get("intent") or ("sales" if inferred=="sales" else ("service" if inferred=="service" else "inquiry"))
        context["order_type"] = order_initial.get("type")
        if intent == "service":
            sel_services = step3d.get("service_selection") or step3d.get("service_type") or []
            if sel_services:
                order_initial["service_selection"] = sel_services
        elif intent == "sales":
            if step3d.get("item_name"):
                order_initial["item_name"] = step3d.get("item_name")
            if step3d.get("brand"):
                order_initial["brand"] = step3d.get("brand")
            if step3d.get("quantity"):
                order_initial["quantity"] = step3d.get("quantity")
            if step3d.get("tire_type"):
                order_initial["tire_type"] = step3d.get("tire_type")
        context["order_form"] = OrderForm(initial=order_initial)
    
    if is_ajax:
        # This shouldn't normally be reached, but just in case
        return json_response(True, **context)
    
    return render(request, "tracker/customer_register.html", context)


# Service settings: types and add-ons
@login_required

def service_types_list(request: HttpRequest):
    types = ServiceType.objects.all().order_by('name')
    return render(request, 'tracker/service_types.html', {'types': types})

@login_required

def service_addons_list(request: HttpRequest):
    addons = ServiceAddon.objects.all().order_by('name')
    return render(request, 'tracker/service_addons.html', {'addons': addons})

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def create_service_type(request: HttpRequest):
    from django.http import JsonResponse
    try:
        data = json.loads(request.body)
        name = (data.get('name') or '').strip()
        est = int(data.get('estimated_minutes') or 0)
        active = bool(data.get('is_active', True))
        if not name:
            return JsonResponse({'success': False, 'error': 'Name is required'}, status=400)
        if ServiceType.objects.filter(name__iexact=name).exists():
            return JsonResponse({'success': False, 'error': 'Service type already exists'}, status=400)
        t = ServiceType.objects.create(name=name, estimated_minutes=est, is_active=active)
        return JsonResponse({'success': True, 'id': t.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def update_service_type(request: HttpRequest, pk: int):
    from django.http import JsonResponse
    try:
        t = get_object_or_404(ServiceType, pk=pk)
        data = json.loads(request.body)
        name = (data.get('name') or '').strip()
        est = int(data.get('estimated_minutes') or 0)
        active = bool(data.get('is_active', True))
        if not name:
            return JsonResponse({'success': False, 'error': 'Name is required'}, status=400)
        if ServiceType.objects.filter(name__iexact=name).exclude(pk=pk).exists():
            return JsonResponse({'success': False, 'error': 'Another type with this name exists'}, status=400)
        t.name = name
        t.estimated_minutes = est
        t.is_active = active
        t.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def create_service_addon(request: HttpRequest):
    from django.http import JsonResponse
    try:
        data = json.loads(request.body)
        name = (data.get('name') or '').strip()
        est = int(data.get('estimated_minutes') or 0)
        active = bool(data.get('is_active', True))
        if not name:
            return JsonResponse({'success': False, 'error': 'Name is required'}, status=400)
        if ServiceAddon.objects.filter(name__iexact=name).exists():
            return JsonResponse({'success': False, 'error': 'Service add-on already exists'}, status=400)
        a = ServiceAddon.objects.create(name=name, estimated_minutes=est, is_active=active)
        return JsonResponse({'success': True, 'id': a.id})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def update_service_addon(request: HttpRequest, pk: int):
    from django.http import JsonResponse
    try:
        a = get_object_or_404(ServiceAddon, pk=pk)
        data = json.loads(request.body)
        name = (data.get('name') or '').strip()
        est = int(data.get('estimated_minutes') or 0)
        active = bool(data.get('is_active', True))
        if not name:
            return JsonResponse({'success': False, 'error': 'Name is required'}, status=400)
        if ServiceAddon.objects.filter(name__iexact=name).exclude(pk=pk).exists():
            return JsonResponse({'success': False, 'error': 'Another add-on with this name exists'}, status=400)
        a.name = name
        a.estimated_minutes = est
        a.is_active = active
        a.save()
        return JsonResponse({'success': True})
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
def start_order(request: HttpRequest):
    """Start a new order by selecting a customer"""
    customers = scope_queryset(Customer.objects.all(), request.user, request).order_by('full_name')
    return render(request, 'tracker/select_customer.html', {
        'customers': customers,
        'page_title': 'Select Customer for New Order'
    })


@login_required
def create_order_for_customer(request: HttpRequest, pk: int):
    """Create a new order for a specific customer.
    This endpoint receives a customer ID and creates an order for that customer.
    It prevents duplicate customer creation by enforcing the customer relationship.
    """
    from .utils import adjust_inventory
    customers_qs = scope_queryset(Customer.objects.all(), request.user, request)
    c = get_object_or_404(customers_qs, pk=pk)

    if request.method == "POST":
        form = OrderForm(request.POST)
        # Ensure vehicle belongs to this customer ONLY
        form.fields["vehicle"].queryset = c.vehicles.all()

        if form.is_valid():
            o = form.save(commit=False)
            # CRITICAL: Always bind order to the pre-selected customer
            # This prevents accidental customer duplication
            o.customer = c
            o.branch = get_user_branch(request.user)
            o.status = "created"

            # Handle vehicle creation if new vehicle info is provided
            if not o.vehicle:
                plate_number = request.POST.get("plate_number", "").strip()
                make = request.POST.get("make", "").strip()
                model = request.POST.get("model", "").strip()
                vehicle_type = request.POST.get("vehicle_type", "").strip()

                # Create vehicle if any vehicle information is provided
                if plate_number or make or model or vehicle_type:
                    v = Vehicle.objects.create(
                        customer=c,
                        plate_number=plate_number or None,
                        make=make or None,
                        model=model or None,
                        vehicle_type=vehicle_type or None
                    )
                    o.vehicle = v

            # Handle service selections for service orders
            if o.type == 'service':
                service_selection = request.POST.getlist('service_selection')
                if service_selection:
                    # Update description with selected services
                    desc = o.description or ""
                    desc_services = "Selected services: " + ", ".join(service_selection)
                    if desc:
                        desc = desc + "\n" + desc_services
                    else:
                        desc = desc_services
                    o.description = desc

                    # Update estimated duration based on selected services if not already set
                    if not o.estimated_duration or o.estimated_duration == 50:
                        try:
                            from .models import ServiceType
                            service_types = ServiceType.objects.filter(name__in=service_selection, is_active=True)
                            total_minutes = sum(int(s.estimated_minutes or 0) for s in service_types)
                            o.estimated_duration = total_minutes or 50
                        except Exception:
                            pass

            # Handle tire services for sales orders
            elif o.type == 'sales':
                tire_services = request.POST.getlist('tire_services')
                if tire_services:
                    # Update description with selected tire services
                    desc = o.description or ""
                    desc_services = "Tire services: " + ", ".join(tire_services)
                    if desc:
                        desc = desc + "\n" + desc_services
                    else:
                        desc = desc_services
                    o.description = desc

                    # Update estimated duration based on selected tire services
                    try:
                        from .models import ServiceAddon
                        addons = ServiceAddon.objects.filter(name__in=tire_services, is_active=True)
                        total_minutes = sum(int(a.estimated_minutes or 0) for a in addons)
                        # Add to existing estimated duration if it exists
                        current_duration = o.estimated_duration or 0
                        o.estimated_duration = current_duration + total_minutes
                    except Exception:
                        pass

            # Inventory check for sales
            if o.type == 'sales':
                name = (o.item_name or '').strip()
                brand = (o.brand or '').strip()
                qty = int(o.quantity or 0)
                from django.db.models import Sum
                available = InventoryItem.objects.filter(name=name, brand__name__iexact=brand).aggregate(total=Sum('quantity')).get('total') or 0
                if not name or not brand or qty <= 0:
                    messages.error(request, 'Item, brand and valid quantity are required')
                    return render(request, "tracker/order_create.html", {"customer": c, "form": form})
                if available < qty:
                    messages.error(request, f'Only {available} in stock for {name} ({brand})')
                    return render(request, "tracker/order_create.html", {"customer": c, "form": form})
            o.save()

            # Update customer visit/arrival status using centralized service
            try:
                from .services import CustomerService
                # update_customer_visit() handles last_visit and total_visits updates
                # Do NOT manually set last_visit before calling it, as this breaks visit counting
                CustomerService.update_customer_visit(c)
            except Exception:
                pass

            # Deduct inventory after save
            if o.type == 'sales':
                qty_int = int(o.quantity or 0)
                ok, _, remaining = adjust_inventory(o.item_name, o.brand, -qty_int)
                if ok:
                    messages.success(request, f"Order created. Remaining stock for {o.item_name} ({o.brand}): {remaining}")
                else:
                    messages.warning(request, 'Order created, but inventory not adjusted')
            else:
                messages.success(request, "Order created successfully")
            return redirect("tracker:order_detail", pk=o.id)
        else:
            messages.error(request, "Please fix form errors and try again")
    else:
        form = OrderForm()
        form.fields["vehicle"].queryset = c.vehicles.all()

    # Dynamic service types and add-ons for order form
    try:
        from .models import ServiceType, ServiceAddon
        svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
        addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
        service_types = [{
            'name': s.name,
            'estimated_minutes': int(s.estimated_minutes or 0)
        } for s in svc_qs]
        sales_addons = [{
            'name': a.name,
            'estimated_minutes': int(a.estimated_minutes or 0)
        } for a in addon_qs]
    except Exception:
        service_types = []
        sales_addons = []

    return render(request, "tracker/order_create.html", {
        "customer": c,
        "form": form,
        "service_types": service_types,
        "sales_addons": sales_addons
    })


@login_required
def customer_groups(request: HttpRequest):
    """Advanced customer groups page with detailed analytics and insights"""
    from django.db.models import Count, Sum, Avg, Max, Min, Q, F
    from django.db.models.functions import TruncMonth, TruncWeek
    from datetime import datetime, timedelta
    
    # Handle AJAX requests
    # If this is an AJAX request to load a group's detail partial, we must NOT early-return here.
    # Only delegate to the JSON data endpoint when it's an AJAX request without load_group=1.
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' and request.GET.get('load_group') != '1':
        return customer_groups_data(request)
        
    # Optional server-side chart generation (matplotlib may be unavailable in some envs)
    try:
        from tracker.utils.chart_utils import generate_monthly_trend_chart
    except Exception:
        generate_monthly_trend_chart = None
    
    # Get filter parameters
    selected_group = request.GET.get('group', 'all')
    time_period = request.GET.get('period', '6months')
    sort_by = request.GET.get('sort')
    
    # Set default sort if not provided or empty
    if not sort_by:
        sort_by = 'total_spent'
    
    # Validate sort field
    valid_sort_fields = [
        'total_spent', 'recent_orders_count', 'last_order_date', 'first_order_date',
        'service_orders', 'sales_orders', 'inquiry_orders', 'completed_orders',
        'cancelled_orders', 'vehicles_count'
    ]
    
    # Extract field name and direction
    sort_field = sort_by.lstrip('-')
    sort_direction = '-' if sort_by.startswith('-') else ''
    
    # Validate sort field
    if sort_field not in valid_sort_fields:
        sort_field = 'total_spent'
        sort_direction = '-'
    
    sort_by = f"{sort_direction}{sort_field}"
    
    # Calculate time range
    today = timezone.now().date()
    if time_period == '1month':
        start_date = today - timedelta(days=30)
    elif time_period == '3months':
        start_date = today - timedelta(days=90)
    elif time_period == '6months':
        start_date = today - timedelta(days=180)
    elif time_period == '1year':
        start_date = today - timedelta(days=365)
    else:
        start_date = today - timedelta(days=180)  # default
    
    # Base customer queryset with annotations
    customers_base = scope_queryset(Customer.objects.all(), request.user, request).annotate(
        recent_orders_count=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
        last_order_date=Max('orders__created_at'),
        first_order_date=Min('orders__created_at'),
        service_orders=Count('orders', filter=Q(orders__type='service', orders__created_at__date__gte=start_date)),
        sales_orders=Count('orders', filter=Q(orders__type='sales', orders__created_at__date__gte=start_date)),
        inquiry_orders=Count('orders', filter=Q(orders__type='inquiry', orders__created_at__date__gte=start_date)),
        completed_orders=Count('orders', filter=Q(orders__status='completed', orders__created_at__date__gte=start_date)),
        cancelled_orders=Count('orders', filter=Q(orders__status='cancelled', orders__created_at__date__gte=start_date)),
        vehicles_count=Count('vehicles', distinct=True)
    )
    
    # Get all defined customer types from the model
    all_customer_types = dict(Customer.TYPE_CHOICES)
    
    # Calculate total customers (all customers in the system)
    total_customers = scope_queryset(Customer.objects.all(), request.user, request).count()
    
    # Calculate active customers this month (customers with orders in the last 30 days)
    one_month_ago = timezone.now() - timedelta(days=30)
    active_customers_this_month = scope_queryset(Customer.objects.all(), request.user, request).filter(
        last_visit__gte=one_month_ago
    ).distinct().count()
    
    # Customer type groups with detailed analytics
    customer_groups = {}
    
    # Get customer counts per group for current period
    current_period_counts = dict(scope_queryset(Customer.objects.all(), request.user, request).values_list('customer_type').annotate(
        count=Count('id')
    ).values_list('customer_type', 'count'))
    
    # Get customer counts for previous period for growth calculation
    prev_period_start = start_date - (today - start_date)  # Same length as current period
    prev_period_counts = dict(scope_queryset(Customer.objects.filter(
        registration_date__lt=start_date,
        registration_date__gte=prev_period_start
    ), request.user, request).values_list('customer_type').annotate(
        count=Count('id')
    ).values_list('customer_type', 'count'))
    
    # Process each customer type
    for customer_type, display_name in all_customer_types.items():
        # Get customers for this group in current period
        group_customers = customers_base.filter(customer_type=customer_type)
        group_customer_count = current_period_counts.get(customer_type, 0)
        
        # Calculate growth percentage
        prev_count = prev_period_counts.get(customer_type, 0)
        growth_percent = 0
        if prev_count > 0:
            growth_percent = round(((group_customer_count - prev_count) / prev_count) * 100, 1)
        elif group_customer_count > 0:
            growth_percent = 100  # If no previous customers but have current, show 100% growth
            
        # Initialize default values for groups with no customers
        group_stats = {
            'total_revenue': 0,
            'avg_revenue_per_customer': 0,
            'total_orders': 0,
            'avg_orders_per_customer': 0,
            'avg_order_value': 0,
            'total_service_orders': 0,
            'total_sales_orders': 0,
            'total_inquiry_orders': 0,
            'total_completed_orders': 0,
            'total_cancelled_orders': 0,
            'total_vehicles': 0,
        }
        
        # If group has customers, get their stats
        if group_customer_count > 0:
            group_stats = group_customers.aggregate(
                total_revenue=Sum('total_spent') or 0,
                total_orders=Sum('recent_orders_count') or 0,
                total_service_orders=Sum('service_orders') or 0,
                total_sales_orders=Sum('sales_orders') or 0,
                total_inquiry_orders=Sum('inquiry_orders') or 0,
                total_completed_orders=Sum('completed_orders') or 0,
                total_cancelled_orders=Sum('cancelled_orders') or 0,
                total_vehicles=Sum('vehicles_count') or 0,
            )
            
            # Calculate averages
            group_stats['avg_revenue_per_customer'] = (
                group_stats['total_revenue'] / group_customer_count 
                if group_customer_count > 0 else 0
            )
            group_stats['avg_orders_per_customer'] = (
                group_stats['total_orders'] / group_customer_count 
                if group_customer_count > 0 else 0
            )
            group_stats['avg_order_value'] = (
                group_stats['total_revenue'] / group_stats['total_orders'] 
                if group_stats['total_orders'] > 0 else 0
            )
        
        # Only calculate metrics if there are customers
        if total_customers > 0:
            group_stats = group_customers.aggregate(
                total_revenue=Sum('total_spent') or 0,
                avg_revenue_per_customer=Avg('total_spent') or 0,
                total_orders=Sum('recent_orders_count') or 0,
                avg_orders_per_customer=Avg('recent_orders_count') or 0,
                avg_order_value=Avg('total_spent') or 0,
                total_service_orders=Sum('service_orders') or 0,
                total_sales_orders=Sum('sales_orders') or 0,
                total_inquiry_orders=Sum('inquiry_orders') or 0,
                total_completed_orders=Sum('completed_orders') or 0,
                total_cancelled_orders=Sum('cancelled_orders') or 0,
                total_vehicles=Sum('vehicles_count') or 0,
            )
        
        # Customer segmentation within group
        high_value = group_customers.filter(total_spent__gte=1000).count()
        medium_value = group_customers.filter(total_spent__gte=500, total_spent__lt=1000).count()
        low_value = group_customers.filter(total_spent__lt=500).count()
        
        # Activity levels
        very_active = group_customers.filter(recent_orders_count__gte=5).count()
        active = group_customers.filter(recent_orders_count__gte=2, recent_orders_count__lt=5).count()
        inactive = group_customers.filter(recent_orders_count__lt=2).count()
        
        # Service preferences
        service_preference = group_customers.filter(service_orders__gt=F('sales_orders')).count()
        sales_preference = group_customers.filter(sales_orders__gt=F('service_orders')).count()
        mixed_preference = total_customers - service_preference - sales_preference if total_customers > 0 else 0
        
        # Recent activity trends
        recent_new_customers = group_customers.filter(registration_date__date__gte=start_date).count()
        returning_customers = group_customers.filter(total_visits__gt=1).count()
        
        # Calculate completion rate (completed orders / (completed + cancelled))
        completed = group_stats.get('total_completed_orders', 0) or 0
        cancelled = group_stats.get('total_cancelled_orders', 0) or 0
        total_orders_for_completion = completed + cancelled
        completion_rate = (completed / total_orders_for_completion * 100) if total_orders_for_completion > 0 else 0
        
        # Get top customers in this group (up to 5)
        top_customers = list(group_customers.order_by('-total_spent')[:5])
        
        # Add group to results
        customer_groups[customer_type] = {
            'name': display_name,
            'code': customer_type,
            'total_customers': group_customer_count,
            'growth_percent': growth_percent,
            'stats': group_stats,
            'segmentation': {
                'high_value': high_value,
                'medium_value': medium_value,
                'low_value': low_value,
            },
            'activity_levels': {
                'very_active': very_active,
                'active': active,
                'inactive': inactive,
            },
            'service_preferences': {
                'service_preference': service_preference,
                'sales_preference': sales_preference,
                'mixed_preference': mixed_preference,
            },
            'trends': {
                'recent_new_customers': recent_new_customers,
                'returning_customers': returning_customers,
                'completion_rate': round(completion_rate, 1) if group_customer_count > 0 else 0,
            },
            'top_customers': top_customers,
        }
    
    # Overall statistics - use base queryset without any filters for accurate totals
    overall_stats = {
        'total_revenue': customers_base.aggregate(total=Sum('total_spent'))['total'] or 0,
        'total_orders': customers_base.aggregate(total=Count('orders'))['total'] or 0,
    }
    
    # Calculate growth for overall metrics
    prev_period_stats = Customer.objects.filter(
        registration_date__lt=start_date,
        registration_date__gte=prev_period_start
    ).aggregate(
        total_revenue=Sum('total_spent', default=0),
        total_orders=Count('orders'),
        total_customers=Count('id')
    )
    
    # Calculate growth percentages
    overall_stats['revenue_growth'] = 0
    if prev_period_stats['total_revenue'] and prev_period_stats['total_revenue'] > 0:
        overall_stats['revenue_growth'] = round(
            ((overall_stats['total_revenue'] - prev_period_stats['total_revenue']) / 
             prev_period_stats['total_revenue']) * 100, 1
        )
        
    overall_stats['orders_growth'] = 0
    if prev_period_stats['total_orders'] > 0:
        overall_stats['orders_growth'] = round(
            ((overall_stats['total_orders'] - prev_period_stats['total_orders']) / 
             prev_period_stats['total_orders']) * 100, 1
        )
    
    # Calculate averages safely to avoid division by zero
    overall_stats['avg_revenue_per_customer'] = (
        overall_stats['total_revenue'] / total_customers 
        if total_customers > 0 else 0
    )
    overall_stats['avg_orders_per_customer'] = (
        overall_stats['total_orders'] / total_customers 
        if total_customers > 0 else 0
    )
    
    # Get detailed customer list for selected group
    detailed_customers = []
    selected_group_display = ''
    if selected_group != 'all' and selected_group in dict(Customer.TYPE_CHOICES):
        detailed_customers = customers_base.filter(customer_type=selected_group).order_by(sort_by)[:50]
        selected_group_display = dict(Customer.TYPE_CHOICES).get(selected_group, selected_group)
    
    # Monthly trends for charts
    monthly_trends = {}
    monthly_charts = {}
    monthly_chart_data = {}
    
    for customer_type, display_name in Customer.TYPE_CHOICES:
        # Get monthly order data
        monthly_data = (Order.objects
                       .filter(customer__customer_type=customer_type, created_at__date__gte=start_date)
                       .annotate(month=TruncMonth('created_at'))
                       .values('month')
                       .annotate(
                           orders=Count('id'),
                           customers=Count('customer', distinct=True)
                       )
                       .order_by('month'))
        
        # Convert QuerySet to list of dicts for the template
        monthly_data_list = list(monthly_data)
        
        # Store the raw data
        monthly_trends[customer_type] = {
            'name': display_name,
            'data': monthly_data_list
        }
        
        # Prepare light payload for client-side chart (labels + series)
        if monthly_data_list:
            labels = [d['month'].strftime('%b %Y') if hasattr(d['month'], 'strftime') else str(d['month']) for d in monthly_data_list]
            series = [int(d.get('orders') or 0) for d in monthly_data_list]
            monthly_chart_data[customer_type] = {'labels': labels, 'series': series, 'title': f"{display_name} - Monthly Order Trends"}
        
        # Generate the chart image (if generator available)
        if monthly_data_list and callable(generate_monthly_trend_chart):
            chart_title = f"{display_name} - Monthly Order Trends"
            chart_image = generate_monthly_trend_chart(
                monthly_data_list,
                title=chart_title
            )
            monthly_charts[customer_type] = chart_image
    
    # Initialize variables with default values if not defined
    total_revenue = getattr(customers_base.aggregate(total=Sum('total_spent')), 'total', 0) or 0
    total_orders = getattr(customers_base.aggregate(total=Count('orders')), 'total', 0) or 0
    
    # Calculate growth percentages with proper default values
    revenue_growth = 0
    orders_growth = 0
    customers_growth = 0
    
    # Prepare context for the template
    context = {
        'customer_groups': customer_groups,
        'selected_group': selected_group,
        'time_period': time_period,
        'sort_by': sort_by,
        'selected_group_display': selected_group_display,
        'detailed_customers': detailed_customers,
        'total_customers': total_customers or 0,
        'total_revenue': total_revenue,
        'total_orders': total_orders,
        'revenue_growth': revenue_growth,
        'orders_growth': orders_growth,
        'customers_growth': customers_growth,
        'chart_image': chart_image if 'chart_image' in locals() else None,
    }
    
    # If it's an AJAX request, return JSON response
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        # If frontend requested to load a single group's detail HTML, return rendered partial
        if request.GET.get('load_group') == '1' and selected_group and selected_group != 'all':
            html = render_to_string('tracker/partials/customer_group_detail.html', context, request=request)
            return JsonResponse({'success': True, 'html': html})

        from django.core import serializers

        # Convert the context to a JSON-serializable format
        response_data = {
            'customer_groups': customer_groups,
            'total_customers': total_customers,
            'total_revenue': float(total_revenue) if total_revenue else 0,
            'total_orders': total_orders,
            'revenue_growth': float(revenue_growth) if revenue_growth else 0,
            'orders_growth': float(orders_growth) if orders_growth else 0,
            'customers_growth': float(customers_growth) if customers_growth else 0,
        }
        return JsonResponse(response_data)
    
    # Define active groups (groups with customers)
    active_groups = [group for group, data in customer_groups.items() if data['total_customers'] > 0]
    
    # For regular requests, render the full template
    context = {
        'customer_groups': customer_groups,
        'overall_stats': overall_stats,
        'selected_group': selected_group,
        'selected_group_display': selected_group_display,
        'time_period': time_period,
        'sort_by': sort_by,
        'detailed_customers': detailed_customers,
        'monthly_trends': monthly_trends,
        'monthly_charts': monthly_charts,
        'monthly_chart_data': json.dumps(monthly_chart_data),  # Client-side chart payload
        'customer_type_choices': Customer.TYPE_CHOICES,
        'start_date': start_date,
        'end_date': today,
        'total_customers': total_customers,
        'active_customers_this_month': active_customers_this_month,
        'active_groups': active_groups,  # List of group codes with customers
    }
    
    # For regular requests, render the full template
    return render(request, 'tracker/customer_groups.html', context)

@login_required
def customer_groups_advanced(request: HttpRequest):
    """Advanced customer groups page with AJAX functionality"""
    branches = list(Branch.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
    return render(request, 'tracker/customer_groups_advanced.html', {'branches': branches})


@login_required
def api_customer_groups_data(request: HttpRequest):
    """Advanced API endpoint for customer groups data"""
    from django.db.models import Count, Sum, Avg, Q, F, Max, Min
    from datetime import datetime, timedelta
    
    # Get parameters
    group = request.GET.get('group', 'all')
    period = request.GET.get('period', '6months')
    
    # Calculate date range
    today = timezone.now().date()
    if period == '1month':
        start_date = today - timedelta(days=30)
    elif period == '3months':
        start_date = today - timedelta(days=90)
    elif period == '1year':
        start_date = today - timedelta(days=365)
    else:
        start_date = today - timedelta(days=180)
    
    # Get all customer types
    customer_types = ['government', 'ngo', 'company', 'personal']
    
    # Build group statistics
    groups_data = {}
    total_customers = 0
    total_orders = 0
    total_revenue = 0
    
    for customer_type in customer_types:
        # Get customers for this group
        customers = Customer.objects.filter(customer_type=customer_type).annotate(
            total_orders=Count('orders'),
            recent_orders=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
            service_orders=Count('orders', filter=Q(orders__type='service')),
            sales_orders=Count('orders', filter=Q(orders__type='sales')),
            inquiry_orders=Count('orders', filter=Q(orders__type='inquiry')),
            completed_orders=Count('orders', filter=Q(orders__status='completed')),
            last_order_date=Max('orders__created_at'),
            vehicles_count=Count('vehicles', distinct=True)
        )
        
        customer_count = customers.count()
        group_orders = sum(c.total_orders for c in customers) or 0
        group_revenue = sum(float(c.total_spent or 0) for c in customers) or 0
        
        # Calculate averages
        avg_orders = group_orders / customer_count if customer_count > 0 else 0
        avg_revenue = group_revenue / customer_count if customer_count > 0 else 0
        
        # Get top customers
        top_customers = list(customers.order_by('-total_spent')[:5].values(
            'id', 'full_name', 'phone', 'total_spent', 'total_orders', 'last_order_date'
        ))
        
        groups_data[customer_type] = {
            'name': dict(Customer.TYPE_CHOICES)[customer_type],
            'customer_count': customer_count,
            'total_orders': group_orders,
            'total_revenue': float(group_revenue),
            'avg_orders': round(avg_orders, 1),
            'avg_revenue': round(float(avg_revenue), 2),
            'top_customers': top_customers
        }
        
        total_customers += customer_count
        total_orders += group_orders
        total_revenue += float(group_revenue)
    
    # If specific group requested, get detailed data
    group_details = None
    if group != 'all' and group in customer_types:
        customers = Customer.objects.filter(customer_type=group).annotate(
            total_orders=Count('orders'),
            recent_orders=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
            service_orders=Count('orders', filter=Q(orders__type='service')),
            sales_orders=Count('orders', filter=Q(orders__type='sales')),
            inquiry_orders=Count('orders', filter=Q(orders__type='inquiry')),
            completed_orders=Count('orders', filter=Q(orders__status='completed')),
            last_order_date=Max('orders__created_at'),
            vehicles_count=Count('vehicles', distinct=True)
        ).order_by('-total_spent')
        
        group_details = {
            'customers': list(customers.values(
                'id', 'full_name', 'phone', 'email', 'total_spent', 'total_orders',
                'recent_orders', 'service_orders', 'sales_orders', 'inquiry_orders',
                'completed_orders', 'last_order_date', 'vehicles_count', 'registration_date'
            )[:50]),
            'stats': groups_data.get(group, {})
        }
    
    return JsonResponse({
        'success': True,
        'groups': groups_data,
        'totals': {
            'customers': total_customers,
            'orders': total_orders,
            'revenue': round(total_revenue, 2)
        },
        'group_details': group_details,
        'period': period
    })

@login_required
def customer_groups_data(request: HttpRequest):
    """API endpoint for AJAX requests to get customer groups data"""
    from django.db.models import Count, Sum, Avg, Q, F
    from datetime import datetime, timedelta
    
    # Get filter parameters
    selected_group = request.GET.get('group', 'all')
    time_period = request.GET.get('period', '6months')
    draw = int(request.GET.get('draw', 1))
    start = int(request.GET.get('start', 0))
    length = int(request.GET.get('length', 10))
    search_value = request.GET.get('search[value]', '')
    
    # Calculate date ranges based on time period
    end_date = datetime.now()
    if time_period == 'week':
        start_date = end_date - timedelta(days=7)
    elif time_period == 'month':
        start_date = end_date - timedelta(days=30)
    elif time_period == '3months':
        start_date = end_date - timedelta(days=90)
    elif time_period == '6months':
        start_date = end_date - timedelta(days=180)
    elif time_period == 'year':
        start_date = end_date - timedelta(days=365)
    else:
        start_date = datetime(2000, 1, 1)  # All time
    
    # Base query for customers
    customers = Customer.objects.all()
    
    # Apply search filter
    if search_value:
        customers = customers.filter(
            Q(first_name__icontains=search_value) |
            Q(last_name__icontains=search_value) |
            Q(phone__icontains=search_value) |
            Q(email__icontains=search_value)
        )
    
    # Apply group filter
    if selected_group and selected_group != 'all':
        if selected_group == 'high_value':
            customers = customers.annotate(
                order_count=Count('orders')
            ).filter(
                order_count__gt=0,
                total_spent__gt=1000  # Example threshold for high-value
            )
        elif selected_group == 'inactive':
            customers = customers.filter(
                last_order_date__lt=end_date - timedelta(days=180)
            )
        # Add more group filters as needed
    
    # Get total count before pagination
    total_records = customers.count()
    
    # Apply pagination
    customers = customers[start:start + length]
    
    # Prepare data for DataTables
    data = []
    for customer in customers:
        data.append({
            'id': customer.id,
            'full_name': f"{customer.first_name} {customer.last_name}",
            'phone': customer.phone,
            'email': customer.email,
            'total_spent': float(customer.total_spent) if customer.total_spent else 0,
            'recent_orders_count': customer.orders.count(),
            'last_order_date': customer.last_order_date.strftime('%Y-%m-%d') if customer.last_order_date else 'N/A',
            'actions': f'''
                <a href="/customer/{customer.id}/" class="btn btn-sm btn-primary">
                    <i class="fas fa-eye"></i> View
                </a>
                <a href="/customer/{customer.id}/edit/" class="btn btn-sm btn-secondary">
                    <i class="fas fa-edit"></i> Edit
                </a>
            '''
        })
    
    # Prepare response
    response = {
        'draw': draw,
        'recordsTotal': total_records,
        'recordsFiltered': total_records,
        'data': data,
    }
    
    return JsonResponse(response)

@login_required
def orders_list(request: HttpRequest):
    from django.db.models import Q, Sum, Count

    # Persist overdue statuses before listing
    _mark_overdue_orders()

    # Get timezone from cookie or use default
    tzname = request.COOKIES.get('django_timezone')

    # Get the view mode from GET parameter (regular or started)
    view_mode = request.GET.get("view", "regular")

    status = request.GET.get("status", "all")
    type_filter = request.GET.get("type", "all")
    priority = request.GET.get("priority", "")
    date_range = request.GET.get("date_range", "")
    customer_id = request.GET.get("customer", "")
    salesperson_id = request.GET.get("salesperson", "")

    # Exclude temporary customers (those with full_name starting with "Plate " and phone starting with "PLATE_")
    orders = scope_queryset(Order.objects.select_related("customer", "vehicle").order_by("-created_at"), request.user, request).exclude(
        customer__full_name__startswith='Plate ',
        customer__phone__startswith='PLATE_'
    )

    # Apply filters
    if status == "overdue":
        orders = orders.filter(status="overdue")
    elif status != "all":
        orders = orders.filter(status=status)
    if type_filter != "all":
        orders = orders.filter(type=type_filter)
    if priority:
        orders = orders.filter(priority=priority)
    if customer_id:
        orders = orders.filter(customer_id=customer_id)
    # Period filters: daily/weekly/monthly/yearly (aliases: today/week/month/year)
    dr = (date_range or '').lower()
    if dr in ("daily", "today"):
        today = timezone.localdate()
        orders = orders.filter(created_at__date=today)
    elif dr in ("weekly", "week"):
        week_ago = timezone.now() - timedelta(days=7)
        orders = orders.filter(created_at__gte=week_ago)
    elif dr in ("monthly", "month"):
        month_ago = timezone.now() - timedelta(days=30)
        orders = orders.filter(created_at__gte=month_ago)
    elif dr in ("yearly", "year"):
        now = timezone.now()
        start_year = now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        orders = orders.filter(created_at__gte=start_year)

    # Get counts for stats
    # Exclude temporary customers (those with full_name starting with "Plate " and phone starting with "PLATE_")
    base_orders_qs = scope_queryset(Order.objects.all(), request.user, request).exclude(
        customer__full_name__startswith='Plate ',
        customer__phone__startswith='PLATE_'
    )
    total_orders = base_orders_qs.count()
    pending_orders = base_orders_qs.filter(status="created").count()
    active_orders = base_orders_qs.filter(status__in=["created", "in_progress", "overdue"]).count()
    completed_today = base_orders_qs.filter(status="completed", completed_at__date=timezone.localdate()).count()
    urgent_orders = base_orders_qs.filter(priority="urgent").count()
    # Overdue KPI: respect user branch scoping and optional admin branch filter
    overdue_count = base_orders_qs.filter(status="overdue").count()
    revenue_today = 0

    # Get started orders KPIs (for the Started Orders view)
    started_orders_qs = scope_queryset(Order.objects.filter(status__in=['created', 'in_progress', 'overdue']), request.user, request)
    started_total = started_orders_qs.count()
    started_pending = started_orders_qs.filter(status='in_progress').count()
    started_completed = scope_queryset(Order.objects.filter(status='completed'), request.user, request).count()

    # Calculate documents uploaded (document_scans count)
    documents_uploaded = 0
    try:
        from .models import DocumentScan
        documents_uploaded = DocumentScan.objects.filter(
            order__in=started_orders_qs
        ).count()
    except Exception:
        pass

    # Fetch started orders if view mode is 'started'
    started_orders = []
    if view_mode == "started":
        plate_search = (request.GET.get("plate_search") or "").strip().upper()
        started_status = request.GET.get("status", "all")
        started_sort = request.GET.get("sort_by", "-started_at")

        started_list_qs = started_orders_qs.select_related("customer", "vehicle")

        if started_status != "all":
            started_list_qs = started_list_qs.filter(status=started_status)

        if plate_search:
            started_list_qs = started_list_qs.filter(vehicle__plate_number__istartswith=plate_search)

        if started_sort == "plate_number":
            started_list_qs = started_list_qs.order_by("vehicle__plate_number")
        elif started_sort == "customer_name":
            started_list_qs = started_list_qs.order_by("customer__full_name")
        elif started_sort == "started_at":
            started_list_qs = started_list_qs.annotate(sort_time=Coalesce('started_at', 'created_at')).order_by("sort_time")
        else:
            started_list_qs = started_list_qs.annotate(sort_time=Coalesce('started_at', 'created_at')).order_by("-sort_time")

        started_orders_list = started_list_qs
        paginator = Paginator(started_orders_list, 20)
        page = request.GET.get('page')
        started_orders = paginator.get_page(page)
    else:
        # For regular view, paginate regular orders
        paginator = Paginator(orders, 20)
        page = request.GET.get('page')
        orders = paginator.get_page(page)

    branches = list(Branch.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
    return render(request, "tracker/orders_list.html", {
        "orders": orders,
        "started_orders": started_orders,
        "order_view_mode": view_mode,
        "status": status,
        "type": type_filter,
        "total_orders": total_orders,
        "pending_orders": pending_orders,
        "active_orders": active_orders,
        "completed_today": completed_today,
        "urgent_orders": urgent_orders,
        "overdue_count": overdue_count,
        "revenue_today": revenue_today,
        "started_total": started_total,
        "started_pending": started_pending,
        "started_completed": started_completed,
        "documents_uploaded": documents_uploaded,
        "branches": branches,
        "plate_search": (request.GET.get("plate_search") or ""),
        "started_status": request.GET.get("status", "all"),
        "started_sort": request.GET.get("sort_by", "-started_at"),
    })
    # Support GET ?customer=<id> to go straight into order form for that customer
    if request.method == 'GET':
        cust_id = request.GET.get('customer')
        if cust_id:
            c = get_object_or_404(Customer, pk=cust_id)
            form = OrderForm()
            form.fields['vehicle'].queryset = c.vehicles.all()
            # Provide dynamic service types and add-ons
            try:
                from .models import ServiceType, ServiceAddon
                svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
                addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
                service_types = [{
                    'name': s.name,
                    'estimated_minutes': int(s.estimated_minutes or 0)
                } for s in svc_qs]
                sales_addons = [{
                    'name': a.name,
                    'estimated_minutes': int(a.estimated_minutes or 0)
                } for a in addon_qs]
            except Exception:
                service_types = []
                sales_addons = []
            return render(request, "tracker/order_create.html", {"customer": c, "form": form, "service_types": service_types, "sales_addons": sales_addons})
        form = OrderForm()
        try:
            form.fields['vehicle'].queryset = Vehicle.objects.none()
        except Exception:
            pass
        try:
            from .models import ServiceType, ServiceAddon
            svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
            addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
            service_types = [{
                'name': s.name,
                'estimated_minutes': int(s.estimated_minutes or 0)
            } for s in svc_qs]
            sales_addons = [{
                'name': a.name,
                'estimated_minutes': int(a.estimated_minutes or 0)
            } for a in addon_qs]
        except Exception:
            service_types = []
            sales_addons = []
        return render(request, "tracker/order_create.html", {"form": form, "service_types": service_types, "sales_addons": sales_addons})

    # Handle POST (AJAX or standard form submit)
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        customer_id = request.POST.get('customer_id')
        if not customer_id:
            return JsonResponse({'success': False, 'message': 'Customer ID is required'})
        # IMPORTANT: Enforce branch scope when retrieving customer to prevent cross-branch access
        user_branch = get_user_branch(request.user)
        customer_qs = Customer.objects.filter(id=customer_id)
        if user_branch and not request.user.is_superuser:
            customer_qs = customer_qs.filter(branch=user_branch)
        customer = get_object_or_404(customer_qs)
        order_data = {
            'customer': customer,
            'type': request.POST.get('type'),
            'priority': request.POST.get('priority', 'medium'),
            'status': 'created',
            'description': request.POST.get('description', ''),
            'estimated_duration': request.POST.get('estimated_duration') or None,
            'item_name': (request.POST.get('item_name') or '').strip(),
            'quantity': None,
            'inquiry_type': (request.POST.get('inquiry_type') or '').strip(),
            'questions': request.POST.get('questions', ''),
            'contact_preference': (request.POST.get('contact_preference') or '').strip(),
            'follow_up_date': request.POST.get('follow_up_date') or None,
        }
        vehicle_id = request.POST.get('vehicle')
        if vehicle_id:
            vehicle = get_object_or_404(Vehicle, id=vehicle_id, customer=customer)
            order_data['vehicle'] = vehicle
        else:
            # Check if new vehicle info is provided
            plate_number = request.POST.get('plate_number', '').strip()
            make = request.POST.get('make', '').strip()
            model = request.POST.get('model', '').strip()
            vehicle_type = request.POST.get('vehicle_type', '').strip()
            
            # Create vehicle if any vehicle information is provided
            if plate_number or make or model or vehicle_type:
                vehicle = Vehicle.objects.create(
                    customer=customer,
                    plate_number=plate_number or None,
                    make=make or None,
                    model=model or None,
                    vehicle_type=vehicle_type or None
                )
                order_data['vehicle'] = vehicle
        if order_data.get('type') == 'sales':
            item_id = (order_data.get('item_name') or '').strip()
            try:
                qty = int(request.POST.get('quantity') or 0)
            except (TypeError, ValueError):
                qty = 0
            if not item_id or qty <= 0:
                return JsonResponse({'success': False, 'message': 'Item selection and valid quantity are required', 'code': 'invalid'})
            
            try:
                item = InventoryItem.objects.select_related('brand').get(id=item_id)
                if item.quantity < qty:
                    return JsonResponse({'success': False, 'message': f'Only {item.quantity} in stock for {item.name} ({item.brand.name})', 'code': 'insufficient_stock', 'available': item.quantity})
                order_data['item_name'] = item.name
                order_data['brand'] = item.brand.name
                order_data['quantity'] = qty
            except InventoryItem.DoesNotExist:
                return JsonResponse({'success': False, 'message': 'Selected item not found in inventory', 'code': 'not_found'})
        # Create order using centralized service
        order = OrderService.create_order(
            customer=customer,
            order_type=order_data['type'],
            branch=get_user_branch(request.user),
            vehicle=order_data.get('vehicle'),
            description=order_data.get('description'),
            priority=order_data.get('priority', 'medium'),
            item_name=order_data.get('item_name'),
            brand=order_data.get('brand'),
            quantity=order_data.get('quantity'),
            inquiry_type=order_data.get('inquiry_type'),
            questions=order_data.get('questions'),
            contact_preference=order_data.get('contact_preference'),
            follow_up_date=order_data.get('follow_up_date'),
            estimated_duration=order_data.get('estimated_duration'),
        )
        remaining = None
        if order.type == 'sales':
            from .utils import adjust_inventory
            qty_int = int(order.quantity or 0)
            ok, status, rem = adjust_inventory(order.item_name, order.brand, -qty_int)
            remaining = rem if ok else None
        return JsonResponse({'success': True, 'message': 'Order created successfully', 'order_id': order.id, 'remaining': remaining})

    # Standard form submit (non-AJAX)
    customer_id = request.POST.get('customer_id') or request.GET.get('customer')
    if not customer_id:
        messages.error(request, 'Customer is required to create an order')
        return render(request, "tracker/order_create.html")
    customers_qs2 = scope_queryset(Customer.objects.all(), request.user, request)
    c = get_object_or_404(customers_qs2, pk=customer_id)
    form = OrderForm(request.POST)
    form.fields['vehicle'].queryset = c.vehicles.all()
    if form.is_valid():
        o = form.save(commit=False)

        # Handle vehicle creation if new vehicle info is provided
        vehicle = o.vehicle
        if not vehicle:
            plate_number = request.POST.get('plate_number', '').strip()
            make = request.POST.get('make', '').strip()
            model = request.POST.get('model', '').strip()
            vehicle_type = request.POST.get('vehicle_type', '').strip()

            # Create vehicle if any vehicle information is provided
            if plate_number or make or model or vehicle_type:
                vehicle = Vehicle.objects.create(
                    customer=c,
                    plate_number=plate_number or None,
                    make=make or None,
                    model=model or None,
                    vehicle_type=vehicle_type or None
                )

        # Sales inventory validation - item_name and brand are already set by form.clean()
        if o.type == 'sales':
            name = (o.item_name or '').strip()
            brand = (o.brand or '').strip()
            qty = int(o.quantity or 0)
            if not name or not brand or qty <= 0:
                messages.error(request, 'Item selection and valid quantity are required')
                return render(request, "tracker/order_create.html", {"customer": c, "form": form})

        # Create order using centralized service
        o = OrderService.create_order(
            customer=c,
            order_type=o.type,
            branch=get_user_branch(request.user),
            vehicle=vehicle,
            description=o.description,
            priority=o.priority,
            item_name=o.item_name,
            brand=o.brand,
            quantity=o.quantity,
            tire_type=getattr(o, 'tire_type', None),
            inquiry_type=o.inquiry_type,
            questions=o.questions,
            contact_preference=o.contact_preference,
            follow_up_date=o.follow_up_date,
            estimated_duration=o.estimated_duration,
        )
        if o.type == 'sales':
            from .utils import adjust_inventory
            qty_int = int(o.quantity or 0)
            ok, status, remaining = adjust_inventory(o.item_name, o.brand, -qty_int)
            if ok:
                messages.success(request, f"Order created. Remaining stock for {o.item_name} ({o.brand}): {remaining}")
            else:
                messages.warning(request, 'Order created, but inventory not adjusted')
        else:
            messages.success(request, 'Order created successfully')
        return redirect('tracker:order_detail', pk=o.id)
    messages.error(request, 'Please fix form errors and try again')
    return render(request, "tracker/order_create.html", {"customer": c, "form": form})


@login_required
def order_edit(request: HttpRequest, pk: int):
    """Edit an existing order"""
    order = get_object_or_404(Order, pk=pk)

    if request.method == 'POST':
        form = OrderForm(request.POST, instance=order)
        if form.is_valid():
            # Track if services are being added for the first time
            had_no_services_before = not order.estimated_duration or order.estimated_duration == 0

            # Handle service selections for service orders
            if order.type == 'service':
                service_selection = request.POST.getlist('service_selection')
                if service_selection:
                    # Update description with selected services
                    desc = order.description or ""
                    desc_services = "Selected services: " + ", ".join(service_selection)
                    if desc:
                        # Remove old service description if exists
                        lines = [l for l in desc.split('\n') if not l.strip().lower().startswith('selected services:')]
                        if lines:
                            desc = '\n'.join(lines) + "\n" + desc_services
                        else:
                            desc = desc_services
                    else:
                        desc = desc_services
                    form.instance.description = desc

                    # Update estimated duration based on selected services
                    try:
                        from .models import ServiceType
                        service_types = ServiceType.objects.filter(name__in=service_selection, is_active=True)
                        total_minutes = sum(int(s.estimated_minutes or 0) for s in service_types)
                        form.instance.estimated_duration = total_minutes or 50
                    except Exception:
                        pass

            # Handle tire services for sales orders
            elif order.type == 'sales':
                tire_services = request.POST.getlist('tire_services')
                if tire_services:
                    # Update description with selected tire services
                    desc = order.description or ""
                    desc_services = "Tire services: " + ", ".join(tire_services)
                    if desc:
                        # Remove old tire services description if exists
                        lines = [l for l in desc.split('\n') if not l.strip().lower().startswith('tire services:')]
                        if lines:
                            desc = '\n'.join(lines) + "\n" + desc_services
                        else:
                            desc = desc_services
                    else:
                        desc = desc_services
                    form.instance.description = desc

                    # Update estimated duration based on selected tire services
                    try:
                        from .models import ServiceAddon
                        addons = ServiceAddon.objects.filter(name__in=tire_services, is_active=True)
                        total_minutes = sum(int(a.estimated_minutes or 0) for a in addons)
                        # Set estimated duration (don't add to existing as we're replacing)
                        form.instance.estimated_duration = total_minutes or 50
                    except Exception:
                        pass

            # Ensure started_at is set when order is being progressed from 'created' status
            # This handles the case where an order was created from an invoice and services are now being added
            if form.instance.status != 'created' and not form.instance.started_at:
                form.instance.started_at = timezone.now()

            order = form.save()
            messages.success(request, 'Order updated successfully.')
            return redirect('tracker:order_detail', pk=order.pk)
    else:
        form = OrderForm(instance=order)

    # Set the vehicle queryset to only include vehicles for this customer
    form.fields['vehicle'].queryset = order.customer.vehicles.all()

    return render(request, 'tracker/order_form.html', {
        'form': form,
        'order': order,
        'title': 'Edit Order',
        'customer': order.customer
    })


@login_required
def order_delete(request: HttpRequest, pk: int):
    """Delete an order"""
    order = get_object_or_404(Order, pk=pk)
    customer = order.customer
    
    if request.method == 'POST':
        try:
            # Log the deletion before actually deleting
            add_audit_log(
                request.user,
                'order_deleted',
                f'Deleted order {order.order_number} for customer {customer.full_name}',
                order_id=order.id,
                customer_id=customer.id
            )
        except Exception:
            pass
            
        order.delete()
        messages.success(request, f'Order {order.order_number} has been deleted.')
        
        # Redirect based on the 'next' parameter or to customer detail
        next_url = request.POST.get('next', None)
        if next_url:
            return redirect(next_url)
        return redirect('tracker:customer_detail', pk=customer.id)
    
    # If not a POST request, redirect to order detail
    return redirect('tracker:order_detail', pk=order.id)


@login_required
def customer_detail(request: HttpRequest, pk: int):
    customers_qs = scope_queryset(Customer.objects.all(), request.user, request)
    try:
        customer = customers_qs.get(pk=pk)
    except Customer.DoesNotExist:
        # Customer either doesn't exist or is not accessible to this user.
        messages.warning(request, "Customer not found or you don't have permission to view this customer.")
        return redirect('tracker:customers_list')

    # Scope orders to user's branch for proper filtering
    orders = scope_queryset(customer.orders.all(), request.user, request).order_by('-created_at')
    try:
        started_order = orders.filter(status='in_progress').first()
    except Exception:
        started_order = None
    vehicles = customer.vehicles.all()
    notes = customer.note_entries.all().order_by('-created_at')
    invoices = customer.invoices.all().order_by('-invoice_date', '-created_at')

    return render(request, "tracker/customer_detail.html", {
        'customer': customer,
        'orders': orders,
        'vehicles': vehicles,
        'notes': notes,
        'invoices': invoices,
        'started_order_id': getattr(started_order, 'id', None),
    })


@login_required
def request_customer_access(request: HttpRequest, pk: int):
    """Record an access request for a customer owned by another branch.
    - Logs an audit entry.
    - Tries to email branch users if email backend is configured.
    - Shows an informative message and redirects to customers list.
    """
    try:
        customer = Customer.objects.get(pk=pk)
    except Customer.DoesNotExist:
        messages.error(request, "Customer not found.")
        return redirect('tracker:customers_list')

    user_branch = get_user_branch(request.user)
    if getattr(request.user, 'is_superuser', False) or (user_branch is not None and getattr(customer, 'branch_id', None) == user_branch.id):
        messages.info(request, "You already have access to this customer.")
        return redirect('tracker:customer_detail', pk=customer.id)

    # Log the access request
    try:
        add_audit_log(request.user, 'request_customer_access', f"Requested access to customer {customer.full_name} (id={customer.id})")
    except Exception:
        pass

    # Try to notify branch users by email (best-effort)
    notified = 0
    try:
        from django.core.mail import send_mail
        from django.conf import settings
        branch_users = User.objects.filter(profile__branch=customer.branch, is_active=True)
        emails = [u.email for u in branch_users if u.email]
        if emails:
            subject = f"Access request for customer {customer.full_name}"
            body = f"User {request.user.get_full_name() or request.user.username} has requested access to customer {customer.full_name} (ID: {customer.id}).\n\nPlease review and grant access if appropriate."
            from_email = getattr(settings, 'DEFAULT_FROM_EMAIL', None) or None
            try:
                send_mail(subject, body, from_email, list(set(emails)), fail_silently=True)
                notified = len(emails)
            except Exception:
                notified = 0
    except Exception:
        notified = 0

    if notified:
        messages.success(request, f"Access request sent to {notified} branch user(s).")
    else:
        messages.success(request, "Access request recorded. Branch owner will be notified in the system.")

    return redirect('tracker:customers_list')


@login_required
def order_detail(request: HttpRequest, pk: int):
    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)
    # Auto-progress created -> in_progress after 10 minutes
    try:
        order.auto_progress_if_elapsed()
    except Exception:
        pass

    # Prefer primary-linked invoice if present; otherwise, fall back to earliest created
    invoice = None
    try:
      primary_link = order.invoice_links.filter(is_primary=True).select_related('invoice').first()
      if primary_link:
        invoice = primary_link.invoice
      elif order.invoices.exists():
        invoice = order.invoices.order_by('created_at').first()
    except Exception:
      invoice = order.invoices.order_by('created_at').first() if order.invoices.exists() else None

    # Extract services from description for better display
    selected_services = []
    if order.description:
        # Look for "Selected services:", "Services:", or "Tire services:" patterns
        lines = order.description.split('\n')
        for line in lines:
            line_lower = line.strip().lower()
            if any(line_lower.startswith(prefix) for prefix in ['selected services:', 'services:', 'tire services:', 'add-ons:']):
                # Extract service names after the colon
                services_text = line.split(':', 1)[1].strip() if ':' in line else ''
                if services_text:
                    services = [s.strip() for s in services_text.split(',') if s.strip()]
                    selected_services.extend(services)

    # Calculate time metrics
    time_metrics = {
        'estimated_minutes': order.estimated_duration,
        'actual_minutes': order.actual_duration,
        'started_at': order.started_at,
        'completed_at': order.completed_at,
        'created_at': order.created_at,
    }

    # Calculate elapsed time if order has started
    if order.started_at:
        elapsed_seconds = (timezone.now() - order.started_at).total_seconds()
        time_metrics['elapsed_minutes'] = int(elapsed_seconds // 60)
    elif order.created_at:
        elapsed_seconds = (timezone.now() - order.created_at).total_seconds()
        time_metrics['elapsed_minutes'] = int(elapsed_seconds // 60)

    # Calculate remaining time if order has estimated duration and hasn't completed
    if order.estimated_duration and not order.completed_at:
        estimated_end = (order.started_at or order.created_at) + timedelta(minutes=order.estimated_duration)
        remaining_seconds = (estimated_end - timezone.now()).total_seconds()
        time_metrics['remaining_minutes'] = max(0, int(remaining_seconds // 60))
        time_metrics['overdue'] = remaining_seconds < 0

    # Get available invoices for linking (invoices from same customer, not yet linked)
    linked_invoice_ids = order.invoice_links.values_list('invoice_id', flat=True)
    available_invoices = order.customer.invoices.exclude(id__in=linked_invoice_ids).order_by('-invoice_date')

    # Prepare context
    line_item_categories = {}
    try:
        codes = []
        if invoice and invoice.line_items.exists():
            codes.extend([li.code for li in invoice.line_items.all() if getattr(li, 'code', None)])
        for link in order.invoice_links.all():
            inv = getattr(link, 'invoice', None)
            if inv and inv.line_items.exists():
                codes.extend([li.code for li in inv.line_items.all() if getattr(li, 'code', None)])
        if codes:
            from tracker.views_invoice_upload import _get_item_code_categories
            line_item_categories = _get_item_code_categories(codes)
    except Exception:
        line_item_categories = {}

    # Check if order exceeds 9+ working hours
    exceeds_9_hours = False
    if order.started_at:
        from .utils.time_utils import is_order_overdue
        exceeds_9_hours = is_order_overdue(order.started_at) if order.status == 'in_progress' else (
            order.actual_duration and order.actual_duration >= (9 * 60)  # 9 hours in minutes
        )

    # Get delay reason categories and reasons
    delay_reason_categories = []
    delay_reasons_by_category = {}
    try:
        from tracker.models import DelayReasonCategory, DelayReason
        import json
        delay_reason_categories = list(DelayReasonCategory.objects.filter(is_active=True))
        for category in delay_reason_categories:
            reasons = list(DelayReason.objects.filter(category=category, is_active=True).values('id', 'reason_text'))
            delay_reasons_by_category[category.category] = reasons
        # Convert to JSON string for template
        delay_reasons_json = {}
        for category in delay_reason_categories:
            reasons = list(DelayReason.objects.filter(category=category, is_active=True).values('id', 'reason_text'))
            delay_reasons_json[category.category] = reasons
    except Exception:
        delay_reasons_json = {}

    # Prepare delay reasons JSON for template
    delay_reasons_for_template = {}
    try:
        from tracker.models import DelayReasonCategory, DelayReason
        for category_obj in delay_reason_categories:
            reasons_list = list(DelayReason.objects.filter(category=category_obj, is_active=True).values('id', 'reason_text'))
            delay_reasons_for_template[category_obj.category] = reasons_list
    except Exception:
        delay_reasons_for_template = delay_reasons_by_category

    context = {
        "order": order,
        "invoice": invoice,
        "selected_services": selected_services,
        "time_metrics": time_metrics,
        "available_invoices": available_invoices,
        "line_item_categories": line_item_categories,
        "exceeds_9_hours": exceeds_9_hours,
        "delay_reason_categories": delay_reason_categories,
        "delay_reasons_by_category": delay_reasons_for_template,
    }
    return render(request, "tracker/order_detail.html", context)


@login_required
def update_order_status(request: HttpRequest, pk: int):
    """Manual status transitions to in_progress are disabled; progression is automatic.
    Use complete_order or cancel_order endpoints for finalization."""
    orders_qs2 = scope_queryset(Order.objects.all(), request.user, request)
    o = get_object_or_404(orders_qs2, pk=pk)
    messages.error(request, "Order status to In Progress is managed automatically after 10 minutes. Use Complete or Cancel for final steps.")
    return redirect("tracker:order_detail", pk=o.id)


@login_required
def complete_order(request: HttpRequest, pk: int):
    """Complete an order requiring a drawn signature and a completion attachment.
    Accepts either a file upload for signature or a base64-encoded 'signature_data' image.
    Computes duration and adjusts inventory for sales."""

    orders_qs3 = scope_queryset(Order.objects.all(), request.user, request)
    o = get_object_or_404(orders_qs3, pk=pk)
    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=o.id)

    # Inquiry orders require no uploads/signature; auto-complete if requested
    if o.type == 'inquiry':
        now = timezone.now()
        if not o.started_at:
            o.started_at = now
            o.status = 'in_progress'
        o.status = 'completed'
        o.completed_at = now
        o.completion_date = now
        o.actual_duration = int(((now - (o.started_at or o.created_at)).total_seconds()) // 60)
        o.signed_by = request.user
        o.signed_at = now
        o.save(update_fields=['status','started_at','completed_at','completion_date','actual_duration','signed_by','signed_at'])
        messages.success(request, 'Inquiry marked as completed.')
        return redirect('tracker:order_detail', pk=o.id)

    # Gather inputs (non-inquiry)
    sig = request.FILES.get('signature_file')
    sig_data = request.POST.get('signature_data') or ''
    att = request.FILES.get('completion_attachment')
    doc_kind = (request.POST.get('completion_doc_type') or '').strip().lower()
    is_job_card = doc_kind in {'job_card', 'jobcard', 'job card'}

    # Server-side validation rules
    ALLOWED_ATTACHMENT_EXTS = ['.jpg','.jpeg','.png','.gif','.webp','.pdf','.doc','.docx','.xls','.xlsx','.txt']
    ALLOWED_SIGNATURE_EXTS = ['.jpg','.jpeg','.png','.webp']
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB
    MAX_SIGNATURE_BYTES = 2 * 1024 * 1024  # 2 MB

    signature_bytes = None

    def _ext_of_name(name):
        try:
            return ('.' + name.split('.')[-1].lower()) if '.' in name else ''
        except Exception:
            return ''

    # If signature file missing but signature_data exists, decode it into an uploaded file
    # First: enforce overrun reason if ETA exceeded. Accept overrun_reason from POST and save it before proceeding.
    try:
        # Fetch overrun reason from POST (check multiple field names for compatibility)
        overrun_reason_input = request.POST.get('overrun_reason') or request.POST.get('delay_reason') or request.POST.get('signature_reason') or None
    except Exception:
        overrun_reason_input = None

    try:
        reference_time = o.started_at or o.created_at or timezone.now()
        elapsed_minutes_check = int(((timezone.now() - reference_time).total_seconds()) // 60)
    except Exception:
        elapsed_minutes_check = None

    if o.type == 'service' and o.estimated_duration and elapsed_minutes_check is not None:
        try:
            if elapsed_minutes_check > int(o.estimated_duration):
                # Overrun happened. If reason provided in the completing POST, save it; otherwise require it.
                if overrun_reason_input:
                    o.overrun_reason = overrun_reason_input
                    o.overrun_reported_at = timezone.now()
                    o.overrun_reported_by = request.user
                    o.save(update_fields=['overrun_reason','overrun_reported_at','overrun_reported_by'])
                elif not o.overrun_reason:
                    messages.error(request, 'Order has exceeded estimated time. Please provide a reason before completing.')
                    return redirect('tracker:order_detail', pk=o.id)
        except Exception:
            pass

    # Check if order exceeds 9+ working hours
    exceeds_9_hours = False
    try:
        from .utils.time_utils import is_order_overdue
        if o.started_at:
            exceeds_9_hours = is_order_overdue(o.started_at)
    except Exception:
        pass

    # Persist optional free-text delay reason (optional field, can be provided for any order)
    # This is stored in overrun_reason field
    try:
        delay_reason_text = (request.POST.get('delay_reason_text') or '').strip()
        if delay_reason_text:
            o.overrun_reason = delay_reason_text
            o.overrun_reported_at = timezone.now()
            o.overrun_reported_by = request.user
            # Mark as exceeded_9_hours if delay reason provided and order exceeded 9 hours
            if exceeds_9_hours:
                o.exceeded_9_hours = True
            o.save(update_fields=['overrun_reason', 'overrun_reported_at', 'overrun_reported_by', 'exceeded_9_hours'])
    except Exception as e:
        logger.warning(f"Error saving delay reason for order {o.id}: {str(e)}")

    if not sig and sig_data and sig_data.startswith('data:image/') and ';base64,' in sig_data:
        try:
            header, b64 = sig_data.split(';base64,', 1)
            ext = (header.split('/')[-1] or 'png').split(';')[0]
            signature_bytes = base64.b64decode(b64)
            if len(signature_bytes) > MAX_SIGNATURE_BYTES:
                messages.error(request, 'Signature image is too large.')
                return redirect('tracker:order_detail', pk=o.id)
            sig = ContentFile(signature_bytes, name=f"signature_{o.id}_{int(time.time())}.{ext}")
        except Exception as e:
            logger.warning(f"Failed to decode signature_data for order {o.id}: {str(e)}")
            sig = None

    # If a signature file was uploaded directly, validate size/type and capture bytes
    if sig and hasattr(sig, 'name'):
        s_ext = _ext_of_name(sig.name)
        if s_ext not in ALLOWED_SIGNATURE_EXTS:
            messages.error(request, 'Invalid signature file type. Use PNG or JPG.')
            return redirect('tracker:order_detail', pk=o.id)
        if hasattr(sig, 'size') and sig.size > MAX_SIGNATURE_BYTES:
            messages.error(request, 'Signature file too large (max 2MB).')
            return redirect('tracker:order_detail', pk=o.id)
        if signature_bytes is None:
            try:
                sig.seek(0)
            except Exception:
                pass
            signature_bytes = sig.read()
        try:
            sig.seek(0)
        except Exception:
            pass

    if not sig:
        logger.error(f"No signature provided for order {o.id}. sig_file: {sig}, sig_data length: {len(sig_data) if sig_data else 0}, sig_data starts with 'data:image/': {sig_data.startswith('data:image/') if sig_data else False}")
        messages.error(request, 'Please draw a signature to complete the order.')
        return redirect('tracker:order_detail', pk=o.id)

    # Validate completion attachment if present and embed signature when appropriate
    signed_attachment = None
    if att:
        a_ext = _ext_of_name(att.name)
        if a_ext not in ALLOWED_ATTACHMENT_EXTS:
            messages.error(request, 'Unsupported attachment type. Allowed: images, PDF, Office documents, text.')
            return redirect('tracker:order_detail', pk=o.id)
        if hasattr(att, 'size') and att.size > MAX_ATTACHMENT_BYTES:
            messages.error(request, 'Attachment too large (max 10MB).')
            return redirect('tracker:order_detail', pk=o.id)
        image_exts = {'.jpg','.jpeg','.png','.gif','.webp'}
        if a_ext == '.pdf':
            if signature_bytes is None:
                try:
                    sig.seek(0)
                    signature_bytes = sig.read()
                except Exception:
                    signature_bytes = None
                finally:
                    try:
                        sig.seek(0)
                    except Exception:
                        pass
            if not signature_bytes:
                messages.error(request, 'Could not access the signature image for PDF embedding.')
                return redirect('tracker:order_detail', pk=o.id)
            try:
                try:
                    att.seek(0)
                except Exception:
                    pass
                pdf_bytes = att.read()
                if is_job_card:
                    signed_pdf_bytes = embed_signature_in_pdf(pdf_bytes, signature_bytes, preset='job_card')
                else:
                    signed_pdf_bytes = embed_signature_in_pdf(pdf_bytes, signature_bytes)
                signed_name = build_signed_filename(att.name)
                signed_attachment = ContentFile(signed_pdf_bytes, name=signed_name)
            except SignatureEmbedError as exc:
                messages.error(request, str(exc))
                return redirect('tracker:order_detail', pk=o.id)
            except Exception:
                messages.error(request, 'Could not embed the signature into the PDF document.')
                return redirect('tracker:order_detail', pk=o.id)
        elif a_ext in image_exts:
            if signature_bytes is None:
                try:
                    sig.seek(0)
                    signature_bytes = sig.read()
                except Exception:
                    signature_bytes = None
                finally:
                    try:
                        sig.seek(0)
                    except Exception:
                        pass
            if not signature_bytes:
                messages.error(request, 'Could not access the signature image for embedding.')
                return redirect('tracker:order_detail', pk=o.id)
            try:
                try:
                    att.seek(0)
                except Exception:
                    pass
                img_bytes = att.read()
                if is_job_card:
                    out_bytes = embed_signature_in_image(img_bytes, signature_bytes, preset='job_card')
                else:
                    out_bytes = embed_signature_in_image(img_bytes, signature_bytes)
                out_name = build_signed_name(att.name)
                signed_attachment = ContentFile(out_bytes, name=out_name)
            except SignatureEmbedError as exc:
                messages.error(request, str(exc))
                return redirect('tracker:order_detail', pk=o.id)
            except Exception:
                messages.error(request, 'Could not embed the signature into the image document.')
                return redirect('tracker:order_detail', pk=o.id)
        else:
            try:
                att.seek(0)
            except Exception:
                pass

    now = timezone.now()
    if not o.started_at:
        o.started_at = now
        o.status = 'in_progress'

    try:
        sig.seek(0)
    except Exception:
        pass
    o.signature_file = sig
    if signed_attachment is not None:
        o.completion_attachment = signed_attachment
    elif att:
        o.completion_attachment = att
    o.signed_by = request.user
    o.signed_at = now
    o.completion_date = now

    o.status = 'completed'
    o.completed_at = now
    reference_time = o.started_at or o.created_at
    o.actual_duration = int(max(0, (now - reference_time).total_seconds() // 60))

    # Calculate estimated_duration using working hours (8 AM - 5 PM)
    if o.started_at:
        from .utils.time_utils import calculate_estimated_duration
        estimated_mins = calculate_estimated_duration(o.started_at, o.completed_at)
        if estimated_mins is not None:
            o.estimated_duration = estimated_mins

    if o.type == 'sales' and (o.quantity or 0) > 0 and o.item_name and o.brand:
        from .utils import adjust_inventory
        adjust_inventory(o.item_name, o.brand, (o.quantity or 0))

    # Supporting attachments are independent; do not auto-embed signature into them during completion

    o.save()
    try:
        add_audit_log(request.user, 'order_completed', f"Order {o.order_number} completed with digital signature")
    except Exception:
        pass
    messages.success(request, 'Order marked as completed.')
    return redirect('tracker:order_detail', pk=o.id)


@login_required
@require_http_methods(["POST"])
def sign_order_document(request: HttpRequest, pk: int):
    """Generate a signed PDF by embedding the provided signature into the final page."""
    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    pdf_file = (
        request.FILES.get('document')
        or request.FILES.get('pdf')
        or request.FILES.get('file')
    )
    signature_payload = request.POST.get('signature_data') or ''

    if not pdf_file or not signature_payload:
        return JsonResponse({'success': False, 'error': 'PDF document and signature are required.'}, status=400)

    MAX_PDF_BYTES = 10 * 1024 * 1024  # 10 MB
    MAX_SIGNATURE_BYTES = 2 * 1024 * 1024  # 2 MB

    filename_lower = (pdf_file.name or '').lower()
    if not filename_lower.endswith('.pdf'):
        return JsonResponse({'success': False, 'error': 'Only PDF documents can be signed.'}, status=400)

    if hasattr(pdf_file, 'size') and pdf_file.size and pdf_file.size > MAX_PDF_BYTES:
        return JsonResponse({'success': False, 'error': 'PDF exceeds maximum size of 10MB.'}, status=400)

    def _decode_signature(payload: str) -> bytes:
        if ';base64,' in payload:
            payload = payload.split(';base64,', 1)[1]
        payload = payload.strip()
        if not payload:
            raise ValueError('Signature payload is empty.')
        try:
            return base64.b64decode(payload)
        except Exception as exc:
            raise ValueError('Signature payload is not valid base64.') from exc

    try:
        signature_bytes = _decode_signature(signature_payload)
    except ValueError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)

    if len(signature_bytes) > MAX_SIGNATURE_BYTES:
        return JsonResponse({'success': False, 'error': 'Signature image is too large (max 2MB).'}, status=400)

    try:
        try:
            pdf_file.seek(0)
        except Exception:
            pass
        pdf_bytes = pdf_file.read()
        preset = 'job_card' if (request.POST.get('completion_doc_type') or '').strip().lower() in {'job_card','jobcard','job card'} else None
        if preset:
            signed_pdf_bytes = embed_signature_in_pdf(pdf_bytes, signature_bytes, preset=preset)
        else:
            signed_pdf_bytes = embed_signature_in_pdf(pdf_bytes, signature_bytes)
    except SignatureEmbedError as exc:
        return JsonResponse({'success': False, 'error': str(exc)}, status=400)
    except Exception:
        return JsonResponse({'success': False, 'error': 'Unable to sign the document.'}, status=500)

    signed_name = build_signed_filename(pdf_file.name)

    signed_content = ContentFile(signed_pdf_bytes, name=signed_name)
    order.completion_attachment.save(signed_name, signed_content, save=False)

    signature_file_name = f"signature_{order.id}_{int(time.time())}.png"
    order.signature_file.save(signature_file_name, ContentFile(signature_bytes), save=False)

    now = timezone.now()
    if not order.started_at:
        order.started_at = now
        order.status = 'in_progress'
    order.status = 'completed'
    order.completed_at = now
    order.completion_date = now
    reference_time = order.started_at or order.created_at
    order.actual_duration = int(max(0, (now - reference_time).total_seconds() // 60))
    order.signed_by = request.user
    order.signed_at = now

    if order.type == 'sales' and (order.quantity or 0) > 0 and order.item_name and order.brand:
        from .utils import adjust_inventory
        adjust_inventory(order.item_name, order.brand, (order.quantity or 0))

    order.save(update_fields=['status', 'completed_at', 'completion_date', 'actual_duration', 'signed_by', 'signed_at'])

    try:
        add_audit_log(request.user, 'order_completed', f"Order {order.order_number} signed and archived as PDF")
    except Exception:
        pass

    response = HttpResponse(signed_pdf_bytes, content_type='application/pdf')
    response['Content-Disposition'] = f'attachment; filename="{signed_name}"'
    try:
        response['X-Signed-Document-URL'] = order.completion_attachment.url
    except Exception:
        response['X-Signed-Document-URL'] = ''
    return response


@login_required
@require_http_methods(["POST"])
def sign_existing_document(request: HttpRequest, pk: int):
    """Embed a drawn signature into an already uploaded attachment for this order.

    POST params:
    - attachment_id: optional OrderAttachment id; if missing, falls back to order.completion_attachment
    - signature_data: base64 data URL or base64 string (required)
    - completion_doc_type: optional, if 'job_card' uses job card placement preset
    """
    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    attachment_id = (request.POST.get('attachment_id') or '').strip()
    signature_payload = request.POST.get('signature_data') or ''
    doc_kind = (request.POST.get('completion_doc_type') or '').strip().lower()
    use_job_card = doc_kind in {'job_card','jobcard','job card'}

    if not signature_payload:
        messages.error(request, 'Signature is required.')
        return redirect('tracker:order_detail', pk=order.id)

    # Resolve source document
    src_bytes = None
    src_name = None
    if attachment_id:
        try:
            att = get_object_or_404(OrderAttachment, pk=int(attachment_id), order=order)
        except Exception:
            messages.error(request, 'Attachment not found for this order.')
            return redirect('tracker:order_detail', pk=order.id)
        try:
            att.file.open('rb')
            src_bytes = att.file.read()
            src_name = att.file.name or 'document'
        finally:
            try:
                att.file.close()
            except Exception:
                pass
    elif order.completion_attachment:
        try:
            order.completion_attachment.open('rb')
            src_bytes = order.completion_attachment.read()
            src_name = order.completion_attachment.name or 'document'
        finally:
            try:
                order.completion_attachment.close()
            except Exception:
                pass
    else:
        messages.error(request, 'No document selected to sign.')
        return redirect('tracker:order_detail', pk=order.id)

    lower = (src_name or '').lower()
    is_pdf = lower.endswith('.pdf')

    # Decode signature
    try:
        payload = signature_payload
        if ';base64,' in payload:
            payload = payload.split(';base64,', 1)[1]
        signature_bytes = base64.b64decode(payload)
    except Exception:
        messages.error(request, 'Invalid signature payload.')
        return redirect('tracker:order_detail', pk=order.id)

    if len(signature_bytes) > (2 * 1024 * 1024):
        messages.error(request, 'Signature image is too large (max 2MB).')
        return redirect('tracker:order_detail', pk=order.id)

    # Perform embedding
    try:
        if is_pdf:
            out = embed_signature_in_pdf(src_bytes, signature_bytes, preset='job_card' if use_job_card else None)
            out_name = build_signed_filename(src_name)
            out_content = ContentFile(out, name=out_name)
        else:
            out = embed_signature_in_image(src_bytes, signature_bytes, preset='job_card' if use_job_card else None)
            out_name = build_signed_name(src_name)
            out_content = ContentFile(out, name=out_name)
    except SignatureEmbedError as exc:
        messages.error(request, str(exc))
        return redirect('tracker:order_detail', pk=order.id)
    except Exception:
        messages.error(request, 'Could not embed signature into the document.')
        return redirect('tracker:order_detail', pk=order.id)

    # Save as new attachment and ensure signature file stored
    OrderAttachment.objects.create(order=order, file=out_content, uploaded_by=request.user)
    if not order.signature_file:
        order.signature_file.save(f"signature_{order.id}_{int(time.time())}.png", ContentFile(signature_bytes), save=False)
    now = timezone.now()
    if not order.started_at:
        order.started_at = now
        order.status = 'in_progress'
    order.status = 'completed'
    order.completed_at = now
    order.completion_date = now
    reference_time = order.started_at or order.created_at
    order.actual_duration = int(max(0, (now - reference_time).total_seconds() // 60))
    order.signed_by = request.user
    order.signed_at = now
    order.save(update_fields=['status', 'started_at', 'completed_at', 'completion_date', 'actual_duration', 'signed_by', 'signed_at'])

    messages.success(request, 'Signed copy created and attached to the order.')
    return redirect('tracker:order_detail', pk=order.id)


@login_required
def cancel_order(request: HttpRequest, pk: int):
    """Cancel an order with a required reason."""
    orders_qs4 = scope_queryset(Order.objects.all(), request.user, request)
    o = get_object_or_404(orders_qs4, pk=pk)
    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=o.id)
    # Disallow cancelling inquiries — they are auto-completed on creation
    if o.type == 'inquiry':
        messages.error(request, 'Inquiry orders cannot be cancelled.')
        return redirect('tracker:order_detail', pk=o.id)

    reason = (request.POST.get('reason') or '').strip()
    if not reason:
        messages.error(request, 'Cancellation requires a reason.')
        return redirect('tracker:order_detail', pk=o.id)
    now = timezone.now()
    o.status = 'cancelled'
    o.cancelled_at = now
    o.cancellation_reason = reason
    o.save(update_fields=['status', 'cancelled_at', 'cancellation_reason'])
    try:
        add_audit_log(request.user, 'order_cancelled', f"Order {o.order_number} cancelled: {reason}")
    except Exception:
        pass
    messages.success(request, 'Order cancelled.')
    return redirect('tracker:order_detail', pk=o.id)


@login_required
def add_order_attachments(request: HttpRequest, pk: int):
    orders_qs5 = scope_queryset(Order.objects.all(), request.user, request)
    o = get_object_or_404(orders_qs5, pk=pk)
    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=o.id)
    if o.type == 'inquiry':
        messages.error(request, 'Cannot add attachments to inquiry orders.')
        return redirect('tracker:order_detail', pk=o.id)
    files = request.FILES.getlist('attachments')
    added = 0
    skipped = 0

    ALLOWED_ATTACHMENT_EXTS = ['.jpg','.jpeg','.png','.gif','.webp','.pdf','.doc','.docx','.xls','.xlsx','.txt']
    MAX_ATTACHMENT_BYTES = 10 * 1024 * 1024  # 10 MB

    def _ext_of_name(name):
        try:
            return ('.' + name.split('.')[-1].lower()) if '.' in name else ''
        except Exception:
            return ''

    for f in files:
        try:
            ext = _ext_of_name(f.name)
            if ext not in ALLOWED_ATTACHMENT_EXTS:
                skipped += 1
                continue
            if hasattr(f, 'size') and f.size > MAX_ATTACHMENT_BYTES:
                skipped += 1
                continue
            OrderAttachment.objects.create(order=o, file=f, uploaded_by=request.user)
            added += 1
        except Exception:
            skipped += 1
            continue
    if added:
        try:
            add_audit_log(request.user, 'attachment_added', f"Added {added} attachment(s) to order {o.order_number}")
        except Exception:
            pass
        msg = f'Uploaded {added} attachment(s).'
        if skipped:
            msg += f' {skipped} file(s) were skipped due to unsupported type or size.'
        messages.success(request, msg)
    else:
        if skipped:
            messages.error(request, 'No attachments uploaded. Files were unsupported or too large.')
        else:
            messages.error(request, 'No attachments were uploaded.')
    return redirect('tracker:order_detail', pk=o.id)


@login_required
@require_http_methods(["POST"])
def sign_supporting_documents(request: HttpRequest, pk: int):
    """Sign supporting documents with signature only after order is completed."""
    from .models import OrderAttachmentSignature

    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    if order.status != 'completed':
        return JsonResponse({'success': False, 'error': 'Order must be completed before signing supporting documents.'}, status=400)

    attachment_id = request.POST.get('attachment_id')
    signature_data = request.POST.get('signature_data', '')

    if not attachment_id or not signature_data:
        return JsonResponse({'success': False, 'error': 'Attachment ID and signature are required.'}, status=400)

    try:
        attachment = OrderAttachment.objects.get(id=int(attachment_id), order=order)
    except OrderAttachment.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Attachment not found.'}, status=404)

    if OrderAttachmentSignature.objects.filter(attachment=attachment).exists():
        return JsonResponse({'success': False, 'error': 'This document has already been signed.'}, status=400)

    MAX_SIGNATURE_BYTES = 2 * 1024 * 1024

    try:
        if ';base64,' in signature_data:
            header, b64_data = signature_data.split(';base64,', 1)
            ext = (header.split('/')[-1] or 'png').split(';')[0]
        else:
            b64_data = signature_data.strip()
            ext = 'png'

        signature_bytes = base64.b64decode(b64_data)

        if len(signature_bytes) > MAX_SIGNATURE_BYTES:
            return JsonResponse({'success': False, 'error': 'Signature image is too large (max 2MB).'}, status=400)

    except Exception as e:
        logger.error(f"Failed to decode signature: {e}")
        return JsonResponse({'success': False, 'error': 'Invalid signature data.'}, status=400)

    try:
        attachment.file.open('rb')
        doc_bytes = attachment.file.read()
        attachment.file.close()
    except Exception as e:
        logger.error(f"Failed to read attachment: {e}")
        return JsonResponse({'success': False, 'error': 'Could not read the document file.'}, status=400)

    signed_file_content = None
    filename_lower = attachment.filename().lower()

    if filename_lower.endswith('.pdf'):
        try:
            signed_bytes = embed_signature_in_pdf(doc_bytes, signature_bytes)
            signed_name = build_signed_filename(attachment.filename())
            signed_file_content = ContentFile(signed_bytes, name=signed_name)
        except Exception as e:
            logger.error(f"Failed to embed signature in PDF: {e}")
            return JsonResponse({'success': False, 'error': 'Could not embed signature into PDF.'}, status=400)
    else:
        image_exts = {'.jpg', '.jpeg', '.png', '.gif', '.webp'}
        if any(filename_lower.endswith(ext) for ext in image_exts):
            try:
                signed_bytes = embed_signature_in_image(doc_bytes, signature_bytes)
                signed_name = build_signed_name(attachment.filename())
                signed_file_content = ContentFile(signed_bytes, name=signed_name)
            except Exception as e:
                logger.error(f"Failed to embed signature in image: {e}")
                return JsonResponse({'success': False, 'error': 'Could not embed signature into image.'}, status=400)
        else:
            return JsonResponse({'success': False, 'error': 'Only PDF and image files can be signed.'}, status=400)

    try:
        sig_img = ContentFile(signature_bytes, name=f"sig_{attachment.id}_{int(time.time())}.png")
        att_sig = OrderAttachmentSignature(
            attachment=attachment,
            signed_file=signed_file_content,
            signature_image=sig_img,
            signed_by=request.user
        )
        att_sig.save()

        try:
            add_audit_log(request.user, 'supporting_doc_signed', f"Signed supporting document for order {order.order_number}")
        except Exception:
            pass

        return JsonResponse({
            'success': True,
            'message': 'Document signed successfully.',
            'attachment_id': attachment_id,
            'signed_at': att_sig.signed_at.isoformat(),
            'signed_by': att_sig.signed_by.get_full_name() or att_sig.signed_by.username
        })
    except Exception as e:
        logger.error(f"Failed to save signature: {e}")
        return JsonResponse({'success': False, 'error': 'Could not save the signed document.'}, status=400)


@login_required
def delete_order_attachment(request: HttpRequest, att_id: int):
    att = get_object_or_404(OrderAttachment, pk=att_id)
    # Enforce branch access via the attachment's order
    allowed_orders = scope_queryset(Order.objects.all(), request.user, request)
    if not allowed_orders.filter(pk=att.order_id).exists():
        messages.error(request, 'You do not have permission to modify this attachment.')
        return redirect('tracker:order_detail', pk=att.order_id)
    order_id = att.order_id
    try:
        att.delete()
        messages.success(request, 'Attachment deleted.')
    except Exception:
        messages.error(request, 'Could not delete attachment.')
    return redirect('tracker:order_detail', pk=order_id)


@login_required
def add_order_component(request: HttpRequest, pk: int):
    """Add an additional order component (service or sales) to an order."""
    from .models import OrderComponent, Invoice

    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=order.id)

    if order.status == 'cancelled':
        messages.error(request, 'Cannot add components to cancelled orders.')
        return redirect('tracker:order_detail', pk=order.id)

    component_type = request.POST.get('component_type', '').strip().lower()
    reason = request.POST.get('reason', '').strip()
    invoice_id = request.POST.get('invoice_id')

    if not component_type or component_type not in ['service', 'sales']:
        messages.error(request, 'Invalid component type. Please select Service or Sales.')
        return redirect('tracker:order_detail', pk=order.id)

    if not reason:
        messages.error(request, 'Please provide a reason for adding this component.')
        return redirect('tracker:order_detail', pk=order.id)

    if OrderComponent.objects.filter(order=order, type=component_type).exists():
        messages.error(request, f'This order already has a {component_type.title()} component.')
        return redirect('tracker:order_detail', pk=order.id)

    try:
        invoice = None

        # Handle existing invoice link
        if invoice_id:
            invoice = Invoice.objects.get(pk=invoice_id, order=order)

        # Handle invoice upload with values
        invoice_file = request.FILES.get('invoice_file')
        invoice_subtotal = request.POST.get('invoice_subtotal', '0').strip()
        invoice_tax_amount = request.POST.get('invoice_tax_amount', '0').strip()
        invoice_total_amount = request.POST.get('invoice_total_amount', '0').strip()

        if invoice_file or (invoice_subtotal and float(invoice_subtotal or 0) > 0):
            from decimal import Decimal
            try:
                subtotal = Decimal(str(invoice_subtotal or '0').replace(',', ''))
                tax_amount = Decimal(str(invoice_tax_amount or '0').replace(',', ''))
                total_amount = Decimal(str(invoice_total_amount or '0').replace(',', ''))

                # Create new invoice for this order
                invoice = Invoice.objects.create(
                    branch=order.branch,
                    order=order,
                    customer=order.customer,
                    vehicle=order.vehicle,
                    invoice_date=timezone.localdate(),
                    subtotal=subtotal,
                    tax_amount=tax_amount,
                    total_amount=total_amount or (subtotal + tax_amount),
                    created_by=request.user
                )
                invoice.generate_invoice_number()

                # Save uploaded file if provided
                if invoice_file:
                    invoice.document = invoice_file

                invoice.save()
            except (ValueError, Decimal.InvalidOperation):
                messages.error(request, 'Invalid invoice amounts provided.')
                return redirect('tracker:order_detail', pk=order.id)

        component = OrderComponent.objects.create(
            order=order,
            type=component_type,
            reason=reason,
            invoice=invoice,
            added_by=request.user
        )
        messages.success(request, f'Added {component_type.title()} component to order.')
        return redirect('tracker:order_detail', pk=order.id)
    except Invoice.DoesNotExist:
        messages.error(request, 'Selected invoice not found for this order.')
        return redirect('tracker:order_detail', pk=order.id)
    except Exception as e:
        messages.error(request, f'Error adding component: {str(e)}')
        return redirect('tracker:order_detail', pk=order.id)


@login_required
def link_invoice_to_order(request: HttpRequest, pk: int):
    """Link an additional invoice to an order with a reason."""
    from .models import OrderInvoiceLink, Invoice

    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=order.id)

    if order.status == 'completed' or order.status == 'cancelled':
        messages.error(request, 'Cannot link invoices to completed or cancelled orders.')
        return redirect('tracker:order_detail', pk=order.id)

    invoice_id = request.POST.get('invoice_id')
    reason = request.POST.get('reason', '').strip()
    is_primary = request.POST.get('is_primary') == 'on'

    if not invoice_id:
        messages.error(request, 'Please select an invoice to link.')
        return redirect('tracker:order_detail', pk=order.id)

    if not reason:
        messages.error(request, 'Please provide a reason for linking this invoice.')
        return redirect('tracker:order_detail', pk=order.id)

    try:
        invoice = Invoice.objects.get(pk=invoice_id, customer=order.customer)

        if OrderInvoiceLink.objects.filter(order=order, invoice=invoice).exists():
            messages.error(request, 'This invoice is already linked to this order.')
            return redirect('tracker:order_detail', pk=order.id)

        if is_primary:
            OrderInvoiceLink.objects.filter(order=order, is_primary=True).update(is_primary=False)

        OrderInvoiceLink.objects.create(
            order=order,
            invoice=invoice,
            reason=reason,
            is_primary=is_primary,
            linked_by=request.user
        )
        messages.success(request, 'Invoice linked to order successfully.')
        return redirect('tracker:order_detail', pk=order.id)
    except Invoice.DoesNotExist:
        messages.error(request, 'Selected invoice not found for this customer.')
        return redirect('tracker:order_detail', pk=order.id)
    except Exception as e:
        messages.error(request, f'Error linking invoice: {str(e)}')
        return redirect('tracker:order_detail', pk=order.id)


@login_required
def remove_invoice_link(request: HttpRequest, pk: int):
    """Remove an invoice link from an order."""
    from .models import OrderInvoiceLink

    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    if request.method != 'POST':
        return redirect('tracker:order_detail', pk=order.id)

    link_id = request.POST.get('link_id')

    if not link_id:
        messages.error(request, 'Invalid request.')
        return redirect('tracker:order_detail', pk=order.id)

    try:
        link = OrderInvoiceLink.objects.get(pk=link_id, order=order)
        invoice_number = link.invoice.invoice_number
        link.delete()
        messages.success(request, f'Removed link to invoice {invoice_number}.')
        return redirect('tracker:order_detail', pk=order.id)
    except OrderInvoiceLink.DoesNotExist:
        messages.error(request, 'Invoice link not found.')
        return redirect('tracker:order_detail', pk=order.id)
    except Exception as e:
        messages.error(request, f'Error removing link: {str(e)}')
        return redirect('tracker:order_detail', pk=order.id)





@login_required
def customers_export(request: HttpRequest):
    q = request.GET.get('q','').strip()
    qs = scope_queryset(Customer.objects.all().order_by('-registration_date'), request.user, request)
    if q:
        qs = qs.filter(full_name__icontains=q)
    import csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="customers.csv"'
    writer = csv.writer(response)
    writer.writerow(['Code','Name','Phone','Type','Visits','Last Visit'])
    for c in qs.iterator():
        writer.writerow([c.code, c.full_name, c.phone, c.customer_type, c.total_visits, c.last_visit.isoformat() if c.last_visit else '' ])
    return response

@login_required
def orders_export(request: HttpRequest):
    status = request.GET.get('status','all')
    type_ = request.GET.get('type','all')
    qs = scope_queryset(Order.objects.select_related('customer').order_by('-created_at'), request.user, request)
    if status != 'all':
        qs = qs.filter(status=status)
    if type_ != 'all':
        qs = qs.filter(type=type_)
    import csv
    response = HttpResponse(content_type='text/csv')
    response['Content-Disposition'] = 'attachment; filename="orders.csv"'
    writer = csv.writer(response)
    writer.writerow(["Order","Customer","Type","Status","Priority","Created At"])
    for o in qs.iterator():
        writer.writerow([o.order_number, o.customer.full_name, o.type, o.status, o.priority, o.created_at.isoformat()])
    return response

@login_required
def customer_groups_export(request: HttpRequest):
    """Export filtered customer group data to CSV"""
    from datetime import timedelta
    selected_group = request.GET.get('group', '')
    time_period = request.GET.get('period', '6months')
    today = timezone.now().date()
    if time_period == '1month':
        start_date = today - timedelta(days=30)
    elif time_period == '3months':
        start_date = today - timedelta(days=90)
    elif time_period == '6months':
        start_date = today - timedelta(days=180)
    elif time_period == '1year':
        start_date = today - timedelta(days=365)
    else:
        start_date = today - timedelta(days=180)

    qs = scope_queryset(Customer.objects.all(), request.user, request).annotate(
        recent_orders_count=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
        last_order_date=Max('orders__created_at'),
        service_orders=Count('orders', filter=Q(orders__type='service', orders__created_at__date__gte=start_date)),
        sales_orders=Count('orders', filter=Q(orders__type='sales', orders__created_at__date__gte=start_date)),
        inquiry_orders=Count('orders', filter=Q(orders__type='inquiry', orders__created_at__date__gte=start_date)),
        completed_orders=Count('orders', filter=Q(orders__status='completed', orders__created_at__date__gte=start_date)),
        vehicles_count=Count('vehicles', distinct=True),
    )
    if selected_group and selected_group in dict(Customer.TYPE_CHOICES):
        qs = qs.filter(customer_type=selected_group)
    import csv
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = 'attachment; filename="customer_group.csv"'
    w = csv.writer(resp)
    w.writerow(['Code','Name','Phone','Type','Visits','Total Spent','Orders (period)','Service','Sales','inquiry','Completed (period)','Vehicles','Last Order'])
    for c in qs.iterator():
        w.writerow([
            c.code,
            c.full_name,
            c.phone,
            c.customer_type,
            c.total_visits,
            c.total_spent,
            c.recent_orders_count,
            c.service_orders,
            c.sales_orders,
            c.inquiry_orders,
            c.completed_orders,
            c.vehicles_count,
            c.last_order_date.isoformat() if c.last_order_date else '',
        ])
    return resp

@login_required
def profile(request: HttpRequest):
    """Update current user's profile (name and photo)."""
    user = request.user
    
    # Get or create profile
    profile_obj, created = Profile.objects.get_or_create(user=user)
    
    if request.method == 'POST':
        form = ProfileForm(
            request.POST, 
            request.FILES, 
            instance=profile_obj,
            user=user
        )
        if form.is_valid():
            form.save(user)  # Pass the user to the save method
            messages.success(request, 'Profile updated successfully!')
            return redirect('tracker:profile')
        else:
            # Add form errors to messages
            for field, errors in form.errors.items():
                for error in errors:
                    messages.error(request, f"{field.title()}: {error}")
    else:
        form = ProfileForm(instance=profile_obj, user=user)
    
    return render(request, 'tracker/profile.html', {
        'form': form,
        'profile': profile_obj,
        'user': user
    })

@login_required
def api_check_customer_exists(request: HttpRequest):
    """Quick customer existence check for registration workflow."""
    phone = (request.GET.get("phone") or "").strip()

    if not phone:
        return JsonResponse({"exists": False})

    user_branch = get_user_branch(request.user)

    # Try exact match first
    c = Customer.objects.filter(
        phone=phone,
        branch=user_branch
    ).exclude(
        full_name__startswith="Plate "
    ).first()
    
    # If not found, try with normalized phone numbers
    if not c:
        # Remove all non-digit characters for comparison
        import re
        clean_phone = re.sub(r'[^0-9]', '', phone)
        if clean_phone:
            # Try to find customer with similar phone format
            c = Customer.objects.filter(
                branch=user_branch
            ).exclude(
                full_name__startswith="Plate "
            ).extra(
                where=["REPLACE(REPLACE(REPLACE(phone, ' ', ''), '-', ''), '+', '') = %s"],
                params=[clean_phone]
            ).first()

    if not c:
        return JsonResponse({"exists": False})

    data = {
        "id": c.id,
        "code": c.code,
        "full_name": c.full_name,
        "phone": c.phone,
        "email": c.email or "",
        "customer_type": c.customer_type or "",
        "organization_name": c.organization_name or "",
        "total_visits": c.total_visits,
        "detail_url": reverse("tracker:customer_detail", kwargs={"pk": c.id}),
    }
    return JsonResponse({"exists": True, "customer": data})


@login_required
def api_check_customer_duplicate(request: HttpRequest):
    full_name = (request.GET.get("full_name") or "").strip()
    phone = (request.GET.get("phone") or "").strip()
    customer_type = (request.GET.get("customer_type") or "").strip()
    org = (request.GET.get("organization_name") or "").strip()
    tax = (request.GET.get("tax_number") or "").strip()

    if not full_name or not phone:
        return JsonResponse({"exists": False})

    qs = Customer.objects.all()
    if customer_type == "personal":
        qs = qs.filter(full_name=full_name, phone=phone, customer_type="personal")
    elif customer_type in ["government", "ngo", "company"]:
        if not org or not tax:
            return JsonResponse({"exists": False})
        qs = qs.filter(
            full_name=full_name,
            phone=phone,
            organization_name=org,
            tax_number=tax,
            customer_type=customer_type,
        )
    else:
        qs = qs.filter(full_name=full_name, phone=phone)
        if org:
            qs = qs.filter(organization_name=org)
        if tax:
            qs = qs.filter(tax_number=tax)

    c = qs.first()
    if not c:
        return JsonResponse({"exists": False})

    data = {
        "id": c.id,
        "code": c.code,
        "full_name": c.full_name,
        "phone": c.phone,
        "email": c.email or "",
        "address": c.address or "",
        "customer_type": c.customer_type or "",
        "organization_name": c.organization_name or "",
        "tax_number": c.tax_number or "",
        "total_visits": c.total_visits,
        "last_visit": c.last_visit.isoformat() if c.last_visit else "",
        "detail_url": reverse("tracker:customer_detail", kwargs={"pk": c.id}),
        "create_order_url": reverse("tracker:create_order_for_customer", kwargs={"pk": c.id}),
    }
    return JsonResponse({"exists": True, "customer": data})


@login_required
def api_recent_orders(request: HttpRequest):
    recents = scope_queryset(Order.objects.select_related("customer", "vehicle").exclude(status="completed").order_by("-created_at"), request.user, request)[:10]
    data = [
        {
            "order_number": r.order_number,
            "status": r.status,
            "type": r.type,
            "priority": r.priority,
            "customer": r.customer.full_name,
            "vehicle": r.vehicle.plate_number if r.vehicle else None,
            "created_at": r.created_at.isoformat(),
        }
        for r in recents
    ]
    return JsonResponse({"orders": data})

@login_required
def api_inventory_items(request: HttpRequest):
    """API endpoint to get all inventory items with their brands"""
    from django.db.models import Sum, F
    
    cache_key = "api_inv_items_v2"
    data = cache.get(cache_key)
    
    if not data:
        # Get items with their brand names and total quantities
        items = (
            InventoryItem.objects
            .annotate(brand_name=F('brand__name'))
            .values('name', 'brand_name')
            .annotate(total_quantity=Sum('quantity'))
            .order_by('brand_name', 'name')
        )
        
        # Format the response
        formatted_items = [
            {
                'name': item['name'],
                'brand': item['brand_name'],
                'quantity': item['total_quantity'] or 0
            }
            for item in items
        ]
        
        data = {"items": formatted_items}
        cache.set(cache_key, data, 300)  # Cache for 5 minutes
        
    return JsonResponse(data)

@login_required
def api_inventory_brands(request: HttpRequest):
    from django.db.models import Sum, Min
    name = request.GET.get("name", "").strip()
    if not name:
        return JsonResponse({"brands": []})
    cache_key = f"api_inv_brands_{name}"
    data = cache.get(cache_key)
    if not data:
        # Aggregate by brand for this item
        rows = (
            InventoryItem.objects.filter(name=name)
            .values("brand")
            .annotate(quantity=Sum("quantity"), min_price=Min("price"))
            .order_by("brand")
        )
        non_empty = []
        unbranded_qty = 0
        unbranded_price = None
        for r in rows:
            b = (r["brand"] or "").strip()
            q = r["quantity"] or 0
            p = r["min_price"]
            if b:
                non_empty.append({"brand": b, "quantity": q, "price": str(p) if p is not None else ""})
            else:
                unbranded_qty += q
                if p is not None:
                    unbranded_price = p if unbranded_price is None else min(unbranded_price, p)
        brands = non_empty
        # Always include an aggregated Unbranded option when quantity exists
        if unbranded_qty > 0:
            brands.append({
                "brand": "Unbranded",
                "quantity": unbranded_qty,
                "price": str(unbranded_price) if unbranded_price is not None else ""
            })
        data = {"brands": brands}
        cache.set(cache_key, data, 120)
    return JsonResponse(data)

@login_required
def api_create_item_with_brand(request: HttpRequest):
    """API endpoint to create a new item with brand during order creation"""
    if request.method == 'POST':
        try:
            data = json.loads(request.body)
            item_name = data.get('item_name', '').strip()
            brand_name = data.get('brand_name', '').strip()
            
            if not item_name or not brand_name:
                return JsonResponse({'success': False, 'error': 'Item name and brand name are required'})
            
            # Get or create brand
            brand, created = Brand.objects.get_or_create(
                name__iexact=brand_name,
                defaults={'name': brand_name, 'is_active': True}
            )
            
            # Create inventory item
            item = InventoryItem.objects.create(
                name=item_name,
                brand=brand,
                quantity=0,  # Start with 0 quantity
                price=0,
                is_active=True
            )
            
            return JsonResponse({
                'success': True,
                'item': {
                    'id': item.id,
                    'name': item.name,
                    'brand': brand.name,
                    'label': f"{brand.name} - {item.name}"
                }
            })
            
        except Exception as e:
            return JsonResponse({'success': False, 'error': str(e)})
    
    return JsonResponse({'success': False, 'error': 'Invalid request method'})

@login_required
def api_inventory_stock(request: HttpRequest):
    """API endpoint to check inventory stock for an item"""
    name = request.GET.get('name', '').strip()
    brand = request.GET.get('brand', '').strip()
    
    if not name or not brand:
        return JsonResponse({'error': 'Both name and brand parameters are required'}, status=400)
    
    try:
        item = InventoryItem.objects.get(name__iexact=name, brand__name__iexact=brand)
        return JsonResponse({
            'name': item.name,
            'brand': item.brand,
            'quantity': item.quantity,
            'unit': item.unit,
            'unit_price': item.unit_price
        })
    except InventoryItem.DoesNotExist:
        return JsonResponse({'error': 'Item not found'}, status=404)

@login_required
def vehicle_add(request: HttpRequest, customer_id: int):
    """Add a new vehicle for a customer"""
    customers_qs_vadd = scope_queryset(Customer.objects.all(), request.user, request)
    customer = get_object_or_404(customers_qs_vadd, pk=customer_id)

    # Handle AJAX requests
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest' and request.method == 'POST':
        try:
            # Get form data from POST
            plate_number = request.POST.get('plate_number', '').strip()
            make = request.POST.get('make', '').strip()
            model = request.POST.get('model', '').strip()
            vehicle_type = request.POST.get('vehicle_type', '').strip()
            
            # Validate that at least one field is provided
            if not plate_number and not make and not model:
                return JsonResponse({
                    'success': False, 
                    'error': 'Please provide at least one vehicle field (plate number, make, or model)'
                })
            
            # Create the vehicle
            vehicle = Vehicle.objects.create(
                customer=customer,
                plate_number=plate_number or None,
                make=make or None,
                model=model or None,
                vehicle_type=vehicle_type or None
            )
            
            # Return success response with vehicle details
            return JsonResponse({
                'success': True,
                'vehicle': {
                    'id': vehicle.id,
                    'plate_number': vehicle.plate_number or '',
                    'make': vehicle.make or '',
                    'model': vehicle.model or '',
                    'vehicle_type': vehicle.vehicle_type or ''
                },
                'message': 'Vehicle added successfully'
            })
            
        except Exception as e:
            return JsonResponse({
                'success': False, 
                'error': f'Error adding vehicle: {str(e)}'
            })

    # Handle regular form submissions
    if request.method == 'POST':
        form = VehicleForm(request.POST)
        if form.is_valid():
            vehicle = form.save(commit=False)
            vehicle.customer = customer
            vehicle.save()
            messages.success(request, 'Vehicle added successfully.')
            return redirect('tracker:customer_detail', pk=customer_id)
    else:
        form = VehicleForm()
    
    return render(request, 'tracker/vehicle_form.html', {
        'form': form,
        'customer': customer,
        'title': 'Add Vehicle'
    })


@login_required
def customer_delete(request: HttpRequest, pk: int):
    """Delete a customer and all associated data"""
    customers_qs_del = scope_queryset(Customer.objects.all(), request.user, request)
    customer = get_object_or_404(customers_qs_del, pk=pk)

    if request.method == 'POST':
        # Log the deletion before actually deleting
        try:
            add_audit_log(
                request.user,
                'customer_deleted',
                f'Deleted customer {customer.full_name} (ID: {customer.id})',
                customer_id=customer.id
            )
        except Exception:
            pass
        
        # Delete the customer (this will cascade to related objects)
        customer.delete()
        messages.success(request, f'Customer {customer.full_name} has been deleted.')
        return redirect('tracker:customers_list')
    
    # If not a POST request, redirect to customer detail
    return redirect('tracker:customer_detail', pk=customer.id)


@login_required
def vehicle_edit(request: HttpRequest, pk: int):
    """Edit an existing vehicle"""
    vehicle = get_object_or_404(Vehicle, pk=pk)
    
    if request.method == 'POST':
        form = VehicleForm(request.POST, instance=vehicle)
        if form.is_valid():
            form.save()
            messages.success(request, 'Vehicle updated successfully.')
            return redirect('tracker:customer_detail', pk=vehicle.customer_id)
    else:
        form = VehicleForm(instance=vehicle)
    
    return render(request, 'tracker/vehicle_form.html', {
        'form': form,
        'vehicle': vehicle,
        'customer': vehicle.customer,
        'title': 'Edit Vehicle'
    })


@login_required
def vehicle_delete(request: HttpRequest, pk: int):
    """Delete a vehicle"""
    vehicle = get_object_or_404(Vehicle, pk=pk)
    customer_id = vehicle.customer_id
    
    if request.method == 'POST':
        vehicle.delete()
        messages.success(request, 'Vehicle deleted successfully.')
        return redirect('tracker:customer_detail', pk=customer_id)
    
    return render(request, 'tracker/confirm_delete.html', {
        'object': vehicle,
        'cancel_url': reverse('tracker:customer_detail', kwargs={'pk': customer_id}),
        'item_type': 'vehicle'
    })


@login_required
def api_customer_vehicles(request: HttpRequest, customer_id: int):
    """API endpoint to get vehicles for a specific customer"""
    try:
        customers_qs_av = scope_queryset(Customer.objects.all(), request.user, request)
        customer = customers_qs_av.get(pk=customer_id)
        vehicles = [{
            'id': v.id,
            'make': v.make or '',
            'model': v.model or '',
            'year': getattr(v, 'year', None),
            'license_plate': v.plate_number or '',
            'vin': getattr(v, 'vin', '') or ''
        } for v in customer.vehicles.all()]

        return JsonResponse({
            'success': True,
            'vehicles': vehicles
        })
    except Customer.DoesNotExist:
        return JsonResponse({'success': False, 'error': 'Customer not found'}, status=404)

@login_required
def api_notifications_summary(request: HttpRequest):
    """Return notification summary for header dropdown: today's visitors, low stock, overdue orders"""
    from datetime import timedelta
    stock_threshold = int(request.GET.get('stock_threshold', 5) or 5)

    # Normalize statuses once per request
    _mark_overdue_orders()

    # Use timezone-aware date for consistency
    today_date = timezone.localdate()
    now = timezone.now()
    cutoff = now - timedelta(hours=24)

    # Today's visitors (customers who registered today OR have orders today)
    from django.db.models import Q
    base_customers = scope_queryset(Customer.objects.all(), request.user, request)
    todays_qs = base_customers.filter(
        Q(registration_date__date=today_date) |
        Q(orders__created_at__date=today_date)
    ).distinct().order_by('-registration_date')
    todays_count = todays_qs.count()
    todays = [{
        'id': c.id,
        'name': c.full_name,
        'code': c.code,
        'time': c.registration_date.isoformat() if c.registration_date else None,
        'type': 'new_customer' if c.registration_date and c.registration_date.date() == today_date else 'returning_customer'
    } for c in todays_qs[:8]]

    # Low stock items
    low_qs = InventoryItem.objects.filter(quantity__lte=stock_threshold).order_by('quantity', 'name')
    low_count = low_qs.count()
    low_stock = [{
        'id': i.id,
        'name': i.name,
        'brand': i.brand.name if i.brand else 'Unbranded',
        'quantity': i.quantity
    } for i in low_qs[:8]]

    # Overdue orders (persisted or derived for safety)
    base_orders = scope_queryset(Order.objects.select_related('customer'), request.user, request)
    overdue_qs = base_orders.filter(status='overdue').order_by('created_at')
    overdue_count = overdue_qs.count()
    if overdue_count == 0:
        # Fallback derivation in case normalization skipped
        overdue_qs = base_orders.filter(status__in=['created','in_progress'], created_at__lt=cutoff).exclude(type='inquiry').order_by('created_at')
        overdue_count = overdue_qs.count()
    def age_minutes(dt):
        return int((now - dt).total_seconds() // 60) if dt else None
    overdue = [{
        'id': o.id,
        'order_number': o.order_number,
        'customer': o.customer.full_name,
        'status': o.status,
        'age_minutes': age_minutes(o.created_at)
    } for o in overdue_qs[:8]]

    total_new = todays_count + low_count + overdue_count
    return JsonResponse({
        'success': True,
        'counts': {
            'today_visitors': todays_count,
            'low_stock': low_count,
            'overdue_orders': overdue_count,
            'total': total_new,
        },
        'items': {
            'today_visitors': todays,
            'low_stock': low_stock,
            'overdue_orders': overdue,
        }
    })

# Permissions
is_manager = user_passes_test(lambda u: u.is_authenticated and (u.is_superuser or u.groups.filter(name='manager').exists()))

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def create_brand(request):
    """API endpoint to create a new brand via AJAX"""
    from django.http import JsonResponse
    
    try:
        data = json.loads(request.body)
        name = data.get('name', '').strip()
        
        if not name:
            return JsonResponse({'success': False, 'error': 'Brand name is required'}, status=400)
            
        # Check if brand already exists (case-insensitive)
        existing_brand = Brand.objects.filter(name__iexact=name).first()
        if existing_brand:
            # Return the existing brand instead of error
            return JsonResponse({
                'success': True,
                'brand': {
                    'id': existing_brand.id,
                    'name': existing_brand.name,
                    'description': existing_brand.description or '',
                    'website': existing_brand.website or ''
                },
                'message': 'Brand already exists'
            })
            
        # Create the brand
        brand = Brand.objects.create(
            name=name,
            description=data.get('description', '').strip(),
            website=data.get('website', '').strip(),
            is_active=True
        )
        
        return JsonResponse({
            'success': True,
            'brand': {
                'id': brand.id,
                'name': brand.name,
                'description': brand.description or '',
                'website': brand.website or ''
            },
            'message': 'Brand created successfully'
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@csrf_exempt
@require_http_methods(["POST"])
def update_brand(request, pk):
    """API endpoint to update a brand via AJAX"""
    from django.http import JsonResponse
    
    try:
        brand = get_object_or_404(Brand, pk=pk)
        data = json.loads(request.body)
        
        name = data.get('name', '').strip()
        if not name:
            return JsonResponse({'success': False, 'error': 'Brand name is required'}, status=400)
            
        # Check if another brand with this name exists (case-insensitive)
        existing_brand = Brand.objects.filter(name__iexact=name).exclude(pk=pk).first()
        if existing_brand:
            return JsonResponse({
                'success': False, 
                'error': f'A brand with the name "{name}" already exists.'
            }, status=400)
            
        # Update the brand
        brand.name = name
        brand.description = data.get('description', '').strip()
        brand.website = data.get('website', '').strip()
        brand.is_active = data.get('is_active', True)
        brand.save()
        
        return JsonResponse({
            'success': True,
            'brand': {
                'id': brand.id,
                'name': brand.name,
                'description': brand.description or '',
                'website': brand.website or '',
                'is_active': brand.is_active
            },
            'message': 'Brand updated successfully'
        })
        
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@is_manager
def inventory_low_stock(request: HttpRequest):
    """View for displaying inventory items that are low in stock"""
    from .models import InventoryItem
    from django.db.models import Q, F, Sum, ExpressionWrapper, FloatField
    
    # Get threshold from query params or use default (items at or below reorder level)
    threshold = request.GET.get('threshold')
    try:
        threshold = int(threshold) if threshold else None
    except (ValueError, TypeError):
        threshold = None
    
    # Get low stock items
    if threshold is not None:
        # Use custom threshold from query params
        low_stock_items = InventoryItem.objects.filter(
            quantity__lte=threshold,
            is_active=True
        )
    else:
        # Use reorder level if no custom threshold provided
        low_stock_items = InventoryItem.objects.filter(
            quantity__lte=F('reorder_level'),
            is_active=True
        )
    
    # Annotate with total value
    low_stock_items = low_stock_items.annotate(
        total_value=ExpressionWrapper(
            F('price') * F('quantity'),
            output_field=FloatField()
        )
    ).order_by('quantity')
    
    # Calculate summary stats
    summary = {
        'total_items': low_stock_items.count(),
        'total_quantity': low_stock_items.aggregate(total=Sum('quantity'))['total'] or 0,
        'total_value': low_stock_items.aggregate(total=Sum(F('price') * F('quantity')))['total'] or 0,
    }
    
    # Get items that are completely out of stock
    out_of_stock = low_stock_items.filter(quantity=0)
    
    context = {
        'items': low_stock_items,
        'out_of_stock': out_of_stock,
        'summary': summary,
        'threshold': threshold,
    }
    
    return render(request, 'tracker/inventory_low_stock.html', context)

@login_required
@is_manager
def inventory_stock_management(request: HttpRequest):
    """View for managing inventory stock levels and adjustments"""
    from .models import InventoryItem, InventoryAdjustment
    from .forms import InventoryAdjustmentForm
    from django.db.models import Sum, F, ExpressionWrapper, FloatField
    from django.db.models.functions import Coalesce
    from django.shortcuts import render, redirect
    from django.contrib import messages
    
    # Get all active inventory items with current stock levels
    items = InventoryItem.objects.filter(is_active=True).order_by('name')
    
    # Calculate total value for each item
    items = items.annotate(
        total_value=ExpressionWrapper(
            F('price') * F('quantity'),
            output_field=FloatField()
        )
    )
    
    # Handle stock adjustment form submission
    if request.method == 'POST':
        form = InventoryAdjustmentForm(request.POST)
        if form.is_valid():
            adjustment = form.save(commit=False)
            adjustment.user = request.user
            adjustment.save()
            
            # Update the inventory item quantity
            item = adjustment.item
            if adjustment.adjustment_type == 'add':
                item.quantity += adjustment.quantity
            else:
                item.quantity = max(0, item.quantity - adjustment.quantity)  # Prevent negative quantities
            item.save()
            
            messages.success(request, f'Stock level updated for {item.name}')
            return redirect('tracker:inventory_stock_management')
    else:
        form = InventoryAdjustmentForm()
    
    # Get recent adjustments
    recent_adjustments = InventoryAdjustment.objects.select_related('item', 'adjusted_by').order_by('-created_at')[:10]
    
    # Calculate inventory summary
    summary = {
        'total_items': items.count(),
        'total_quantity': items.aggregate(total=Sum('quantity'))['total'] or 0,
        'total_value': items.aggregate(total=Sum(F('price') * F('quantity')))['total'] or 0,
        'low_stock_count': items.filter(quantity__lte=F('reorder_level')).count(),
    }
    
    return render(request, 'tracker/inventory_stock_management.html', {
        'items': items,
        'form': form,
        'recent_adjustments': recent_adjustments,
        'summary': summary,
    })


@login_required
@is_manager
def brand_list(request: HttpRequest):
    """List all brands with management options"""
    brands = Brand.objects.all().order_by('name')
    return render(request, 'tracker/brand_list.html', {'brands': brands})

@login_required
@is_manager
def branches_list(request: HttpRequest):
    """List all branches with management options"""
    branches = Branch.objects.all().order_by('name')
    return render(request, 'tracker/branch_list.html', {'branches': branches})

@login_required
@is_manager
def api_create_branch(request: HttpRequest):
    """Create a new Branch via JSON API"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        payload = json.loads(request.body.decode('utf-8')) if request.body else {}
        name = (payload.get('name') or '').strip()
        code = (payload.get('code') or '').strip().upper()
        region = (payload.get('region') or '').strip() or None
        if not name or not code:
            return JsonResponse({'success': False, 'error': 'Name and code are required'}, status=400)
        if Branch.objects.filter(name__iexact=name).exists():
            return JsonResponse({'success': False, 'error': 'A branch with this name already exists'}, status=400)
        if Branch.objects.filter(code__iexact=code).exists():
            return JsonResponse({'success': False, 'error': 'A branch with this code already exists'}, status=400)
        b = Branch.objects.create(name=name, code=code, region=region, is_active=True)
        return JsonResponse({
            'success': True,
            'branch': {
                'id': b.id,
                'name': b.name,
                'code': b.code,
                'region': b.region,
                'is_active': b.is_active,
            }
        })
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@is_manager
def api_update_branch(request: HttpRequest, pk: int):
    """Update an existing Branch via JSON API"""
    if request.method != 'POST':
        return JsonResponse({'success': False, 'error': 'Invalid method'}, status=405)
    try:
        branch = get_object_or_404(Branch, pk=pk)
        payload = json.loads(request.body.decode('utf-8')) if request.body else {}
        name = (payload.get('name') or '').strip()
        code = (payload.get('code') or '').strip().upper()
        region = (payload.get('region') or '').strip() or None
        is_active = bool(payload.get('is_active'))

        if not name or not code:
            return JsonResponse({'success': False, 'error': 'Name and code are required'}, status=400)

        if Branch.objects.filter(name__iexact=name).exclude(pk=branch.pk).exists():
            return JsonResponse({'success': False, 'error': 'A branch with this name already exists'}, status=400)
        if Branch.objects.filter(code__iexact=code).exclude(pk=branch.pk).exists():
            return JsonResponse({'success': False, 'error': 'A branch with this code already exists'}, status=400)

        branch.name = name
        branch.code = code
        branch.region = region
        branch.is_active = is_active
        branch.save()

        return JsonResponse({
            'success': True,
            'branch': {
                'id': branch.id,
                'name': branch.name,
                'code': branch.code,
                'region': branch.region,
                'is_active': branch.is_active,
            },
            'message': 'Branch updated successfully'
        })
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON data'}, status=400)
    except Exception as e:
        return JsonResponse({'success': False, 'error': str(e)}, status=500)

@login_required
@is_manager
def inventory_list(request: HttpRequest):
    # Get search parameters
    q = request.GET.get('q', '').strip()
    brand_filter = request.GET.get('brand', '').strip()
    
    # Start with base queryset - only fetch necessary fields for the list view
    qs = InventoryItem.objects.select_related('brand').only(
        'name', 'description', 'quantity', 'price', 'cost_price', 'sku', 'barcode',
        'reorder_level', 'is_active', 'created_at', 'brand__name'
    ).order_by('-created_at')
    
    # Apply search filter if provided
    if q:
        qs = qs.filter(
            Q(name__icontains=q) |
            Q(description__icontains=q) |
            Q(sku__icontains=q) |
            Q(barcode__icontains=q) |
            Q(brand__name__icontains=q)
        )
    
    # Apply brand filter if provided
    if brand_filter:
        try:
            brand_id = int(brand_filter)
            qs = qs.filter(brand_id=brand_id)
        except (ValueError, TypeError):
            # Invalid brand ID, ignore the filter
            pass
    
    # Get distinct active brands for filter dropdown
    # Cache this queryset since it's used in the template
    from django.core.cache import cache
    cache_key = 'active_brands_list'
    brands = cache.get(cache_key)
    
    if brands is None:
        brands = list(Brand.objects.filter(is_active=True).order_by('name').values('id', 'name'))
        # Cache for 1 hour
        cache.set(cache_key, brands, 3600)
    
    # Paginate results
    items_per_page = 20
    paginator = Paginator(qs, items_per_page)
    
    # Get current page from request
    page_number = request.GET.get('page')
    try:
        page_number = int(page_number) if page_number and page_number.isdigit() else 1
        items = paginator.page(page_number)
    except (ValueError, EmptyPage):
        # If page is not an integer or out of range, deliver first page
        items = paginator.page(1)
    
    # Calculate range of items being displayed
    start_index = (items.number - 1) * items_per_page + 1
    end_index = min(start_index + items_per_page - 1, paginator.count)
    
    context = {
        'items': items,
        'q': q,
        'brands': brands,
        'selected_brand': brand_filter,
        'total_items': paginator.count,
        'start_index': start_index,
        'end_index': end_index,
    }
    
    # Add HTTP headers for caching
    response = render(request, 'tracker/inventory_list.html', context)
    response['Cache-Control'] = 'no-cache, no-store, must-revalidate'  # Prevent caching
    response['Pragma'] = 'no-cache'  # HTTP 1.0
    response['Expires'] = '0'  # Proxies
    
    return response

@login_required
@is_manager
def inventory_create(request: HttpRequest):
    from .forms import InventoryItemForm
    if request.method == 'POST':
        form = InventoryItemForm(request.POST)
        if form.is_valid():
            item = form.save()
            from .utils import clear_inventory_cache
            clear_inventory_cache(item.name, item.brand)
            try:
                add_audit_log(request.user, 'inventory_create', f"Item '{item.name}' ({item.brand or 'Unbranded'}) qty={item.quantity}")
            except Exception:
                pass
            messages.success(request, 'Inventory item created')
            return redirect('tracker:inventory_list')
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = InventoryItemForm()
    return render(request, 'tracker/inventory_form.html', { 'form': form, 'mode': 'create' })

@login_required
@is_manager
def inventory_edit(request: HttpRequest, pk: int):
    from .forms import InventoryItemForm
    item = get_object_or_404(InventoryItem, pk=pk)
    if request.method == 'POST':
        form = InventoryItemForm(request.POST, instance=item)
        if form.is_valid():
            item = form.save()
            from .utils import clear_inventory_cache
            clear_inventory_cache(item.name, item.brand)
            try:
                add_audit_log(request.user, 'inventory_update', f"Item '{item.name}' ({item.brand or 'Unbranded'}) now qty={item.quantity}")
            except Exception:
                pass
            messages.success(request, 'Inventory item updated')
            return redirect('tracker:inventory_list')
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = InventoryItemForm(instance=item)
    return render(request, 'tracker/inventory_form.html', { 'form': form, 'mode': 'edit', 'item': item })

@login_required
@is_manager
def inventory_delete(request: HttpRequest, pk: int):
    item = get_object_or_404(InventoryItem, pk=pk)
    if request.method == 'POST':
        from .utils import clear_inventory_cache
        name, brand = item.name, item.brand
        item.delete()
        clear_inventory_cache(name, brand)
        try:
            add_audit_log(request.user, 'inventory_delete', f"Deleted item '{name}' ({brand or 'Unbranded'})")
        except Exception:
            pass
        messages.success(request, 'Inventory item deleted')
        return redirect('tracker:inventory_list')
    return render(request, 'tracker/inventory_delete.html', { 'item': item })

# Admin-only: Organization Management
@login_required
@user_passes_test(lambda u: u.is_superuser)
def organization_management(request: HttpRequest):
    org_types = ['government', 'ngo', 'company']
    q = request.GET.get('q','').strip()
    status = request.GET.get('status','')
    sort_by = request.GET.get('sort','last_order_date')
    time_period = request.GET.get('period','6months')

    # Period
    today = timezone.now().date()
    if time_period == '1month':
        start_date = today - timezone.timedelta(days=30)
    elif time_period == '3months':
        start_date = today - timezone.timedelta(days=90)
    elif time_period == '1year':
        start_date = today - timezone.timedelta(days=365)
    else:
        start_date = today - timezone.timedelta(days=180)

    base = scope_queryset(Customer.objects.filter(customer_type__in=org_types), request.user, request)
    if q:
        base = base.filter(Q(full_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(organization_name__icontains=q) | Q(code__icontains=q))

    customers_qs = base.annotate(
        recent_orders_count=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
        last_order_date=Max('orders__created_at'),
        service_orders=Count('orders', filter=Q(orders__type='service', orders__created_at__date__gte=start_date)),
        sales_orders=Count('orders', filter=Q(orders__type='sales', orders__created_at__date__gte=start_date)),
        inquiry_orders=Count('orders', filter=Q(orders__type='inquiry', orders__created_at__date__gte=start_date)),
        completed_orders=Count('orders', filter=Q(orders__status='completed', orders__created_at__date__gte=start_date)),
        cancelled_orders=Count('orders', filter=Q(orders__status='cancelled', orders__created_at__date__gte=start_date)),
        vehicles_count=Count('vehicles', distinct=True)
    )

    if status == 'returning':
        customers_qs = customers_qs.filter(total_visits__gt=1)

    if sort_by in ['recent_orders_count','total_spent','last_order_date','vehicles_count','completed_orders']:
        customers_qs = customers_qs.order_by(f'-{sort_by}')
    else:
        customers_qs = customers_qs.order_by('-last_order_date')

    paginator = Paginator(customers_qs, 20)
    page = request.GET.get('page')
    customers = paginator.get_page(page)

    # Header counts
    type_counts = base.values('customer_type').annotate(c=Count('id'))
    counts = {row['customer_type']: row['c'] for row in type_counts}
    total_org = sum(counts.values()) if counts else 0

    # Charts
    orders_scope = scope_queryset(Order.objects.filter(customer__in=base, created_at__date__gte=start_date), request.user, request)
    if status == 'returning':
        orders_scope = orders_scope.filter(customer__total_visits__gt=1)
    type_dist = {r['type']: r['c'] for r in orders_scope.values('type').annotate(c=Count('id'))}
    from django.db.models.functions import TruncMonth
    month_rows = orders_scope.annotate(m=TruncMonth('created_at')).values('m').annotate(c=Count('id')).order_by('m')
    trend_labels = [(r['m'].strftime('%Y-%m') if r['m'] else '') for r in month_rows]
    trend_values = [r['c'] for r in month_rows]
    charts = {
        'type': {
            'labels': ['Service','Sales','inquiry'],
            'values': [type_dist.get('service',0), type_dist.get('sales',0), type_dist.get('inquiry',0)]
        },
        'trend': {'labels': trend_labels, 'values': trend_values}
    }

    return render(request, 'tracker/organization.html', {
        'customers': customers,
        'q': q,
        'counts': counts,
        'total_org': total_org,
        'status': status,
        'sort_by': sort_by,
        'time_period': time_period,
        'start_date': start_date,
        'end_date': today,
        'charts_json': json.dumps(charts),
    })

@login_required
@user_passes_test(lambda u: u.is_superuser)
def organization_export(request: HttpRequest):
    org_types = ['government','ngo','company']
    q = request.GET.get('q','').strip()
    status = request.GET.get('status','')
    time_period = request.GET.get('period','6months')
    today = timezone.now().date()
    if time_period == '1month':
        start_date = today - timezone.timedelta(days=30)
    elif time_period == '3months':
        start_date = today - timezone.timedelta(days=90)
    elif time_period == '1year':
        start_date = today - timezone.timedelta(days=365)
    else:
        start_date = today - timezone.timedelta(days=180)

    base = scope_queryset(Customer.objects.filter(customer_type__in=org_types), request.user, request)
    if q:
        base = base.filter(Q(full_name__icontains=q) | Q(phone__icontains=q) | Q(email__icontains=q) | Q(organization_name__icontains=q) | Q(code__icontains=q))
    qs = base.annotate(
        recent_orders_count=Count('orders', filter=Q(orders__created_at__date__gte=start_date)),
        last_order_date=Max('orders__created_at'),
        service_orders=Count('orders', filter=Q(orders__type='service', orders__created_at__date__gte=start_date)),
        sales_orders=Count('orders', filter=Q(orders__type='sales', orders__created_at__date__gte=start_date)),
        inquiry_orders=Count('orders', filter=Q(orders__type='inquiry', orders__created_at__date__gte=start_date)),
        completed_orders=Count('orders', filter=Q(orders__status='completed', orders__created_at__date__gte=start_date)),
        vehicles_count=Count('vehicles', distinct=True),
    )
    if status == 'returning':
        qs = qs.filter(total_visits__gt=1)

    import csv
    resp = HttpResponse(content_type='text/csv')
    resp['Content-Disposition'] = 'attachment; filename="organization_customers.csv"'
    w = csv.writer(resp)
    w.writerow(['Code','Organization','Contact','Phone','Type','Visits','Orders (period)','Service','Sales','Consult','Completed','Vehicles','Last Order'])
    for c in qs.iterator():
        w.writerow([
            c.code,
            c.organization_name or '',
            c.full_name,
            c.phone,
            c.customer_type,
            c.total_visits,
            c.recent_orders_count,
            c.service_orders,
            c.sales_orders,
            c.inquiry_orders,
            c.completed_orders,
            c.vehicles_count,
            c.last_order_date.isoformat() if c.last_order_date else ''
        ])
    return resp

@login_required
@user_passes_test(lambda u: u.is_superuser or u.is_staff)
def users_list(request: HttpRequest):
    q = request.GET.get('q','').strip()
    branch_param = (request.GET.get('branch') or '').strip()
    qs = User.objects.all().order_by('-date_joined')
    if q:
        qs = qs.filter(Q(username__icontains=q) | Q(first_name__icontains=q) | Q(last_name__icontains=q) | Q(email__icontains=q))

    # Superusers can filter across branches, staff are restricted to their assigned branch
    if request.user.is_superuser:
        if branch_param:
            if branch_param.isdigit():
                qs = qs.filter(profile__branch_id=int(branch_param))
            else:
                b = Branch.objects.filter(name__iexact=branch_param).first()
                if b:
                    qs = qs.filter(profile__branch_id=b.id)
    else:
        b = getattr(getattr(request.user, 'profile', None), 'branch', None)
        qs = qs.filter(profile__branch=b) if b else qs.none()

    branches = list(Branch.objects.filter(is_active=True).order_by('name').values_list('name', flat=True))
    return render(request, 'tracker/users_list.html', { 'users': qs[:100], 'q': q, 'branches': branches, 'selected_branch': branch_param })

@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_create(request: HttpRequest):
    from .forms import AdminUserCreateForm
    if request.method == 'POST':
        form = AdminUserCreateForm(request.POST)
        if form.is_valid():
            new_user = form.save()
            add_audit_log(request.user, 'user_create', f'Created user {new_user.username}')
            messages.success(request, 'User created')
            return redirect('tracker:users_list')
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = AdminUserCreateForm()
    return render(request, 'tracker/user_create.html', { 'form': form })

@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_edit(request: HttpRequest, pk: int):
    from .forms import AdminUserForm
    u = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        form = AdminUserForm(request.POST, instance=u)
        if form.is_valid():
            updated_user = form.save()
            add_audit_log(request.user, 'user_update', f'Updated user {updated_user.username}')
            messages.success(request, 'User updated')
            return redirect('tracker:users_list')
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = AdminUserForm(instance=u)
    return render(request, 'tracker/user_edit.html', { 'form': form, 'user_obj': u })

@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_toggle_active(request: HttpRequest, pk: int):
    u = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        u.is_active = not u.is_active
        u.save(update_fields=['is_active'])
        add_audit_log(request.user, 'user_toggle_active', f'Toggled active for {u.username} -> {u.is_active}')
        messages.success(request, f'User {"activated" if u.is_active else "deactivated"}.')
    return redirect('tracker:users_list')

@login_required
@user_passes_test(lambda u: u.is_superuser)
def user_reset_password(request: HttpRequest, pk: int):
    import random, string
    u = get_object_or_404(User, pk=pk)
    if request.method == 'POST':
        temp = ''.join(random.choices(string.ascii_letters + string.digits, k=10))
        u.set_password(temp)
        u.save()
        add_audit_log(request.user, 'user_reset_password', f'Reset password for {u.username}')
        messages.success(request, f'Temporary password for {u.username}: {temp}')
    return redirect('tracker:users_list')


@login_required
def customer_edit(request: HttpRequest, pk: int):
    customers_qs_edit = scope_queryset(Customer.objects.all(), request.user, request)
    customer = get_object_or_404(customers_qs_edit, pk=pk)
    if request.method == 'POST':
        form = CustomerEditForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            try:
                add_audit_log(request.user, 'customer_update', f"Updated customer {customer.full_name} ({customer.code})")
            except Exception:
                pass
            messages.success(request, 'Customer updated successfully')
            return redirect('tracker:customer_detail', pk=customer.id)
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = CustomerEditForm(instance=customer)
    return render(request, 'tracker/customer_edit.html', { 'form': form, 'customer': customer })


@login_required
def customers_quick_create(request: HttpRequest):
    """Quick customer creation for order form"""
    if request.method == 'POST' and request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            full_name = request.POST.get('full_name', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            customer_type = request.POST.get('customer_type', 'personal')

            if not full_name or not phone:
                return JsonResponse({'success': False, 'message': 'Name and phone are required'})

            # Create customer using centralized service (handles deduplication automatically)
            from .services import CustomerService
            customer_branch = get_user_branch(request.user)

            try:
                customer, created = CustomerService.create_or_get_customer(
                    branch=customer_branch,
                    full_name=full_name,
                    phone=phone,
                    email=email if email else None,
                    customer_type=customer_type,
                )

                # If customer already existed, return that info
                if not created:
                    return JsonResponse({
                        'success': False,
                        'message': f'A similar customer already exists: {customer.full_name} ({customer.phone})',
                        'customer_id': customer.id,
                        'customer_name': customer.full_name,
                        'customer_phone': str(customer.phone)
                    })
            except Exception as e:
                logger.warning(f"Error creating customer in quick_create: {e}")
                return JsonResponse({'success': False, 'message': f'Error creating customer: {str(e)}'})

            try:
                add_audit_log(request.user, 'customer_create', f"Created customer {customer.full_name} ({customer.code})")
            except Exception:
                pass

            return JsonResponse({
                'success': True,
                'message': 'Customer created successfully',
                'customer': {
                    'id': customer.id,
                    'name': customer.full_name,
                    'phone': customer.phone,
                    'email': customer.email or '',
                    'code': customer.code,
                    'type': customer.customer_type
                }
            })

        except Exception as e:
            return JsonResponse({'success': False, 'message': f'Error creating customer: {str(e)}'})

    return JsonResponse({'success': False, 'message': 'Invalid request'})


@login_required
def inquiries(request: HttpRequest):
    """View and manage customer inquiries"""
    # Get filter parameters
    inquiry_type = request.GET.get('type', '')
    status = request.GET.get('status', '')
    follow_up = request.GET.get('follow_up', '')

    # Base queryset for inquiry orders (inquiries)
    queryset = scope_queryset(Order.objects.filter(type='inquiry').select_related('customer').order_by('-created_at'), request.user, request)

    # Apply filters
    if inquiry_type:
        queryset = queryset.filter(inquiry_type=inquiry_type)

    if status:
        queryset = queryset.filter(status=status)

    if follow_up == 'required':
        queryset = queryset.filter(follow_up_date__isnull=False)
    elif follow_up == 'overdue':
        today = timezone.localdate()
        queryset = queryset.filter(
            follow_up_date__lte=today,
            status__in=['created', 'in_progress']
        )

    # Pagination
    paginator = Paginator(queryset, 12)  # Show 12 inquiries per page
    page = request.GET.get('page')
    inquiries = paginator.get_page(page)

    # Statistics
    base_queryset = scope_queryset(Order.objects.filter(type='inquiry'), request.user, request)
    stats = {
        'new': base_queryset.filter(status='created').count(),
        'in_progress': base_queryset.filter(status='in_progress').count(),
        'resolved': base_queryset.filter(status='completed').count(),
    }
    # Add total count for the template
    stats['total'] = stats['new'] + stats['in_progress'] + stats['resolved']

    context = {
        'inquiries': inquiries,
        'stats': stats,
        'today': timezone.localdate(),
    }

    return render(request, 'tracker/inquiries.html', context)


@login_required
def inquiry_detail(request: HttpRequest, pk: int):
    """Get inquiry details for modal view"""
    if request.headers.get('X-Requested-With') == 'XMLHttpRequest':
        try:
            inquiry = get_object_or_404(Order, pk=pk, type='inquiry')

            data = {
                'id': inquiry.id,
                'customer': {
                    'name': inquiry.customer.full_name,
                    'phone': inquiry.customer.phone,
                    'email': inquiry.customer.email or '',
                },
                'inquiry_type': inquiry.inquiry_type or 'General',
                'contact_preference': inquiry.contact_preference or 'Phone',
                'questions': inquiry.questions or '',
                'status': inquiry.status,
                'status_display': inquiry.get_status_display(),
                'created_at': inquiry.created_at.isoformat(),
                'follow_up_date': inquiry.follow_up_date.isoformat() if inquiry.follow_up_date else None,
                'responses': [],  # In a real app, you'd have a related model for responses
            }

            return JsonResponse(data)

        except Exception as e:
            return JsonResponse({'error': str(e)}, status=400)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def inquiry_respond(request: HttpRequest, pk: int):
    """Respond to a customer inquiry"""
    from .utils import send_sms
    from .models import InquiryNote
    inquiry = get_object_or_404(Order, pk=pk, type='inquiry')

    if request.method == 'POST':
        response_text = request.POST.get('response', '').strip()
        follow_up_required = request.POST.get('follow_up_required') == 'on'
        follow_up_date = request.POST.get('follow_up_date')

        if not response_text:
            messages.error(request, 'Response message is required')
            return redirect('tracker:inquiries')

        # Append response into inquiry questions thread
        stamp = timezone.now().strftime('%Y-%m-%d %H:%M')
        trail = f"[{stamp}] Response: {response_text}"
        if inquiry.questions:
            inquiry.questions = (inquiry.questions or '') + "\n\n" + trail
        else:
            inquiry.questions = trail

        # Create InquiryNote entry
        try:
            InquiryNote.objects.create(
                inquiry=inquiry,
                note_type='response',
                content=response_text,
                created_by=request.user,
                is_visible_to_customer=True
            )
        except Exception:
            pass

        # Update follow-up date if required
        if follow_up_required and follow_up_date:
            try:
                inquiry.follow_up_date = follow_up_date
            except ValueError:
                pass

        # Mark as in progress if not already completed
        if inquiry.status == 'created':
            inquiry.status = 'in_progress'

        inquiry.save()
        try:
            add_audit_log(request.user, 'inquiry_respond', f"Responded to inquiry #{inquiry.id} for {inquiry.customer.full_name}")
        except Exception:
            pass

        # Send SMS to the customer's phone
        phone = inquiry.customer.phone
        sms_message = f"Hello {inquiry.customer.full_name}, regarding your inquiry ({inquiry.inquiry_type or 'General'}): {response_text} — Superdoll Support"
        ok, info = send_sms(phone, sms_message)
        if ok:
            messages.success(request, 'Response sent via SMS')
        else:
            messages.warning(request, f'Response saved, but SMS not sent: {info}')
        return redirect('tracker:inquiries')

    return redirect('tracker:inquiries')


@login_required
def update_inquiry_status(request: HttpRequest, pk: int):
    """Update inquiry status"""
    inquiry = get_object_or_404(Order, pk=pk, type='inquiry')

    if request.method == 'POST':
        new_status = request.POST.get('status')

        if new_status in ['created', 'in_progress', 'completed']:
            old_status = inquiry.status
            inquiry.status = new_status

            if new_status == 'completed':
                inquiry.completed_at = timezone.now()

            inquiry.save()
            try:
                add_audit_log(request.user, 'inquiry_status_update', f"Inquiry #{inquiry.id}: {old_status} -> {new_status}")
            except Exception:
                pass

            status_display = {
                'created': 'New',
                'in_progress': 'In Progress',
                'completed': 'Resolved'
            }

            messages.success(request, f'Inquiry status updated to {status_display.get(new_status, new_status)}')
        else:
            messages.error(request, 'Invalid status')

    return redirect('tracker:inquiries')


@login_required
@require_http_methods(["POST"])
def api_create_inquiry(request: HttpRequest):
    """API endpoint to create a new inquiry"""
    from .forms import InquiryCreationForm
    from .models import InquiryNote
    from .services import CustomerService

    if request.method == 'POST':
        user_branch = get_user_branch(request.user)
        customer_mode = request.POST.get('customer_mode', 'existing')

        # Handle new customer creation
        customer = None
        if customer_mode == 'new':
            new_name = (request.POST.get('new_customer_name') or '').strip()
            new_phone = (request.POST.get('new_customer_phone') or '').strip()

            if not new_name or not new_phone:
                return JsonResponse({
                    'success': False,
                    'message': 'Customer name and phone are required'
                }, status=400)

            try:
                customer, created = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=new_name,
                    phone=new_phone,
                    create_if_missing=True
                )
            except Exception as e:
                logger.error(f"Failed to create new customer for inquiry: {e}")
                return JsonResponse({
                    'success': False,
                    'message': f'Failed to create customer: {str(e)}'
                }, status=400)
        else:
            # Use existing customer from form
            form = InquiryCreationForm(request.POST)
            if not form.is_valid():
                errors = {field: [str(error) for error in field_errors]
                         for field, field_errors in form.errors.items()}
                return JsonResponse({
                    'success': False,
                    'message': 'Form validation failed',
                    'errors': errors
                }, status=400)
            customer = form.cleaned_data.get('customer')
            inquiry_type = form.cleaned_data.get('inquiry_type')
            questions = form.cleaned_data.get('questions')
            priority = form.cleaned_data.get('priority')
            follow_up_date = form.cleaned_data.get('follow_up_date')

        # For new customer mode, get other fields from request
        if customer_mode == 'new':
            inquiry_type = request.POST.get('inquiry_type', '')
            questions = request.POST.get('questions', '')
            priority = request.POST.get('priority', 'medium')
            follow_up_date = request.POST.get('follow_up_date') or None

        if not customer:
            return JsonResponse({
                'success': False,
                'message': 'Customer is required'
            }, status=400)

        if not inquiry_type or not questions:
            return JsonResponse({
                'success': False,
                'message': 'Inquiry type and questions are required'
            }, status=400)

        try:
            # Create inquiry order
            inquiry = Order.objects.create(
                order_number=Order()._generate_order_number(),
                type='inquiry',
                status='completed',
                customer=customer,
                inquiry_type=inquiry_type,
                questions=questions,
                priority=priority,
                follow_up_date=follow_up_date,
                branch=user_branch,
                created_at=timezone.now(),
                started_at=timezone.now(),
                completed_at=timezone.now(),
            )

            # Update customer visit tracking
            from .services import CustomerService
            try:
                CustomerService.update_customer_visit(customer)
            except Exception as e:
                logger.warning(f"Error updating customer visit for inquiry: {e}")

            # Create initial note
            InquiryNote.objects.create(
                inquiry=inquiry,
                note_type='status_change',
                content=f"Inquiry created: {inquiry_type}",
                created_by=request.user,
                is_visible_to_customer=True
            )

            try:
                add_audit_log(request.user, 'inquiry_create', f"Created inquiry #{inquiry.id} for {customer.full_name} ({inquiry_type})")
            except Exception:
                pass

            return JsonResponse({
                'success': True,
                'message': 'Inquiry created successfully',
                'inquiry_id': inquiry.id
            })
        except Exception as e:
            logger.error(f"Failed to create inquiry: {e}")
            return JsonResponse({
                'success': False,
                'message': str(e)
            }, status=400)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
@require_http_methods(["POST"])
def api_add_inquiry_note(request: HttpRequest, pk: int):
    """API endpoint to add a note or response to an inquiry"""
    from .forms import InquiryNoteForm
    from .models import InquiryNote

    inquiry = get_object_or_404(Order, pk=pk, type='inquiry')

    if request.method == 'POST':
        form = InquiryNoteForm(request.POST)

        if form.is_valid():
            try:
                note_type = form.cleaned_data['note_type']
                content = form.cleaned_data['content']

                # Create note
                note = InquiryNote.objects.create(
                    inquiry=inquiry,
                    note_type=note_type,
                    content=content,
                    created_by=request.user,
                    is_visible_to_customer=(note_type == 'response')
                )

                # Update inquiry status if not already completed
                if inquiry.status == 'created':
                    inquiry.status = 'in_progress'
                    inquiry.save(update_fields=['status'])

                try:
                    add_audit_log(request.user, 'inquiry_note_add', f"Added {note_type} to inquiry #{inquiry.id}")
                except Exception:
                    pass

                return JsonResponse({
                    'success': True,
                    'message': f'{note_type.title()} added successfully',
                    'note_id': note.id,
                    'created_at': note.created_at.isoformat(),
                    'created_by': request.user.first_name or request.user.username
                })
            except Exception as e:
                return JsonResponse({
                    'success': False,
                    'message': str(e)
                }, status=400)
        else:
            errors = {field: [str(error) for error in field_errors]
                     for field, field_errors in form.errors.items()}
            return JsonResponse({
                'success': False,
                'message': 'Form validation failed',
                'errors': errors
            }, status=400)

    return JsonResponse({'error': 'Invalid request'}, status=400)


@login_required
def api_inquiry_notes(request: HttpRequest, pk: int):
    """API endpoint to get inquiry notes/timeline"""
    from .models import InquiryNote

    inquiry = get_object_or_404(Order, pk=pk, type='inquiry')

    # Get all notes (only show non-visible notes to staff)
    notes = InquiryNote.objects.filter(inquiry=inquiry)

    notes_data = [
        {
            'id': note.id,
            'type': note.note_type,
            'type_display': note.get_note_type_display(),
            'content': note.content,
            'created_by': note.created_by.first_name or note.created_by.username if note.created_by else 'System',
            'created_at': note.created_at.isoformat(),
            'is_visible_to_customer': note.is_visible_to_customer
        }
        for note in notes
    ]

    return JsonResponse({
        'success': True,
        'notes': notes_data
    })


@login_required
@require_http_methods(["POST"])
def api_inquiry_bulk_action(request: HttpRequest):
    """API endpoint for bulk inquiry actions"""
    action = request.POST.get('action')
    inquiry_ids = request.POST.getlist('inquiry_ids[]')

    if not inquiry_ids or not action:
        return JsonResponse({'success': False, 'message': 'Invalid parameters'}, status=400)

    try:
        inquiries = Order.objects.filter(pk__in=inquiry_ids, type='inquiry')

        if action == 'mark_resolved':
            count = inquiries.update(status='completed', completed_at=timezone.now())
            message = f'{count} inquiry(ies) marked as resolved'

        elif action == 'mark_pending':
            count = inquiries.update(status='in_progress')
            message = f'{count} inquiry(ies) marked as pending'

        elif action == 'export_csv':
            response = HttpResponse(content_type='text/csv')
            response['Content-Disposition'] = 'attachment; filename="inquiries.csv"'

            writer = csv.writer(response)
            writer.writerow(['ID', 'Customer', 'Type', 'Status', 'Priority', 'Created', 'Follow-up Date'])

            for inq in inquiries:
                writer.writerow([
                    inq.id,
                    inq.customer.full_name,
                    inq.inquiry_type,
                    inq.get_status_display(),
                    inq.priority,
                    inq.created_at.strftime('%Y-%m-%d %H:%M'),
                    inq.follow_up_date.strftime('%Y-%m-%d') if inq.follow_up_date else ''
                ])

            return response

        try:
            add_audit_log(request.user, 'inquiry_bulk_action', f"Bulk action '{action}' on {len(inquiry_ids)} inquiries")
        except Exception:
            pass

        return JsonResponse({'success': True, 'message': message})

    except Exception as e:
        return JsonResponse({'success': False, 'message': str(e)}, status=400)


@login_required
@require_http_methods(["POST"])
def api_save_delay_reason(request: HttpRequest, pk: int):
    """API endpoint to save delay reason without completing the order"""
    orders_qs = scope_queryset(Order.objects.all(), request.user, request)
    order = get_object_or_404(orders_qs, pk=pk)

    try:
        # Get delay reason ID and category from request
        delay_reason_id = request.POST.get('delay_reason_id') or request.POST.get('delay_reason')
        delay_reason_category = request.POST.get('delay_reason_category')
        delay_reason_text = request.POST.get('delay_reason_text')
        delay_reason_comments = request.POST.get('delay_reason_comments', '').strip()

        delay_reason_saved = False

        # Try to save using the selected ID (primary path)
        if delay_reason_id:
            try:
                from tracker.models import DelayReason
                delay_reason = DelayReason.objects.get(id=delay_reason_id)
                order.delay_reason = delay_reason
                order.delay_reason_reported_at = timezone.now()
                order.delay_reason_reported_by = request.user
                order.exceeded_9_hours = True

                # Save comments if provided
                if delay_reason_comments:
                    order.overrun_reason = delay_reason_comments
                    order.save(update_fields=['delay_reason', 'delay_reason_reported_at', 'delay_reason_reported_by', 'exceeded_9_hours', 'overrun_reason', 'overrun_reported_at'])
                    order.overrun_reported_at = timezone.now()
                else:
                    order.save(update_fields=['delay_reason', 'delay_reason_reported_at', 'delay_reason_reported_by', 'exceeded_9_hours'])

                delay_reason_saved = True
                add_audit_log(request.user, 'order_delay_reason_saved', f"Delay reason '{delay_reason.reason_text}' saved for order {order.order_number}")

                return JsonResponse({
                    'success': True,
                    'message': f'Delay reason saved: {delay_reason.reason_text}',
                    'delay_reason': {
                        'id': delay_reason.id,
                        'text': delay_reason.reason_text,
                        'category': delay_reason.category.category if delay_reason.category else 'unknown'
                    }
                })
            except Exception as e:
                logger.warning(f"Error saving delay reason by ID for order {order.id}: {str(e)}")

        # Fallback: try text + category if ID didn't work
        if not delay_reason_saved and delay_reason_text and delay_reason_category:
            try:
                from tracker.models import DelayReason, DelayReasonCategory
                cat_obj = DelayReasonCategory.objects.filter(category=delay_reason_category, is_active=True).first()
                if cat_obj:
                    dr = DelayReason.objects.filter(category=cat_obj, reason_text__iexact=delay_reason_text).first()
                    if not dr:
                        dr = DelayReason.objects.create(category=cat_obj, reason_text=delay_reason_text, is_active=True)

                    order.delay_reason = dr
                    order.delay_reason_reported_at = timezone.now()
                    order.delay_reason_reported_by = request.user
                    order.exceeded_9_hours = True

                    # Save comments if provided
                    if delay_reason_comments:
                        order.overrun_reason = delay_reason_comments
                        order.overrun_reported_at = timezone.now()
                        order.save(update_fields=['delay_reason', 'delay_reason_reported_at', 'delay_reason_reported_by', 'exceeded_9_hours', 'overrun_reason', 'overrun_reported_at'])
                    else:
                        order.save(update_fields=['delay_reason', 'delay_reason_reported_at', 'delay_reason_reported_by', 'exceeded_9_hours'])

                    add_audit_log(request.user, 'order_delay_reason_saved', f"Delay reason '{dr.reason_text}' saved for order {order.order_number}")

                    return JsonResponse({
                        'success': True,
                        'message': f'Delay reason saved: {dr.reason_text}',
                        'delay_reason': {
                            'id': dr.id,
                            'text': dr.reason_text,
                            'category': cat_obj.category if cat_obj else 'unknown'
                        }
                    })
                else:
                    return JsonResponse({
                        'success': False,
                        'message': 'Selected delay reason category not found.'
                    }, status=400)
            except Exception as e:
                logger.error(f"Error saving delay reason via text+category for order {order.id}: {str(e)}")
                return JsonResponse({
                    'success': False,
                    'message': f'Error saving delay reason: {str(e)}'
                }, status=400)

        return JsonResponse({
            'success': False,
            'message': 'No delay reason provided. Please select a reason.'
        }, status=400)

    except Exception as e:
        logger.error(f"Error in api_save_delay_reason: {str(e)}")
        return JsonResponse({
            'success': False,
            'message': f'Error: {str(e)}'
        }, status=500)


@login_required
def system_settings(request: HttpRequest):
    def defaults():
        return {
            'company_name': '',
            'default_priority': 'medium',
            'enable_unbranded_alias': True,
            'allow_order_without_vehicle': True,
            'sms_provider': 'none',
        }
    data = cache.get('system_settings', None) or defaults()
    if request.method == 'POST':
        form = SystemSettingsForm(request.POST)
        if form.is_valid():
            new_data = {**defaults(), **form.cleaned_data}
            changes = []
            for k, old_val in (data or {}).items():
                new_val = new_data.get(k)
                if new_val != old_val:
                    changes.append(f"{k}: '{old_val}' -> '{new_val}'")
            cache.set('system_settings', new_data, None)
            add_audit_log(request.user, 'system_settings_update', '; '.join(changes) if changes else 'No changes')
            messages.success(request, 'Settings updated')
            return redirect('tracker:system_settings')
        else:
            messages.error(request, 'Please correct errors and try again')
    else:
        form = SystemSettingsForm(initial=data)
    return render(request, 'tracker/system_settings.html', {'form': form, 'settings': data})

@login_required
@user_passes_test(lambda u: u.is_superuser)
def audit_logs(request: HttpRequest):
    if request.method == 'POST' and request.POST.get('action') == 'clear':
        clear_audit_logs()
        add_audit_log(request.user, 'audit_logs_cleared', 'Cleared all audit logs')
        messages.success(request, 'Audit logs cleared')
        return redirect('tracker:audit_logs')
    
    q = request.GET.get('q', '').strip()
    action_filter = request.GET.get('action', '').strip()
    user_filter = request.GET.get('user', '').strip()
    
    logs = get_audit_logs()
    
    if q or action_filter or user_filter:
        filtered_logs = []
        for log in logs:
            # Convert all searchable fields to lowercase for case-insensitive search
            log_user = str(log.get('user', '')).lower()
            log_action = str(log.get('action', '')).lower()
            log_description = str(log.get('description', '')).lower()
            log_meta = str(log.get('meta', {})).lower()
            
            # Apply filters
            matches = True
            
            # General search (q parameter)
            if q:
                q = q.lower()
                if not (q in log_user or q in log_action or q in log_description or q in log_meta):
                    matches = False
            
            # Action filter
            if matches and action_filter:
                if action_filter.lower() not in log_action:
                    matches = False
            
            # User filter
            if matches and user_filter:
                if user_filter.lower() not in log_user:
                    matches = False
            
            if matches:
                filtered_logs.append(log)
        logs = filtered_logs
    
    # Get unique actions and users for filter dropdowns
    all_actions = sorted(set(log.get('action', '') for log in get_audit_logs() if log.get('action')))
    all_users = sorted(set(log.get('user', '') for log in get_audit_logs() if log.get('user')))
    
    context = {
        'logs': logs,
        'q': q,
        'action_filter': action_filter,
        'user_filter': user_filter,
        'all_actions': all_actions,
        'all_users': all_users,
    }
    return render(request, 'tracker/audit_logs.html', context)

@login_required
@user_passes_test(lambda u: u.is_superuser)
def backup_restore(request: HttpRequest):
    if request.GET.get('download'):
        import json
        payload = {
            'system_settings': cache.get('system_settings', {}),
        }
        add_audit_log(request.user, 'backup_download', 'Downloaded system settings backup')
        resp = HttpResponse(json.dumps(payload, indent=2), content_type='application/json')
        resp['Content-Disposition'] = 'attachment; filename="backup.json"'
        return resp
    if request.method == 'POST':
        action = request.POST.get('action')
        if action == 'reset_settings':
            cache.delete('system_settings')
            add_audit_log(request.user, 'settings_reset', 'Reset system settings to defaults')
            messages.success(request, 'System settings have been reset to defaults')
            return redirect('tracker:backup_restore')
        if action == 'restore_settings' and request.FILES.get('file'):
            f = request.FILES['file']
            try:
                data = json.load(f)
                settings_data = data.get('system_settings') or {}
                if isinstance(settings_data, dict):
                    cache.set('system_settings', settings_data, None)
                    add_audit_log(request.user, 'settings_restored', 'Restored system settings from uploaded backup')
                    messages.success(request, 'Settings restored from backup')
                else:
                    messages.error(request, 'Invalid backup file format')
            except Exception as e:
                messages.error(request, f'Failed to restore: {e}')
            return redirect('tracker:backup_restore')
    return render(request, 'tracker/backup_restore.html')


# ---------------------------
# Reports System
# ---------------------------
# All report views are now in the reports.py module
# These are just aliases for backward compatibility
