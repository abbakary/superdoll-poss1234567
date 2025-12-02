from django.utils import timezone
from django.utils.deprecation import MiddlewareMixin
from datetime import timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .models import Order

class TimezoneMiddleware(MiddlewareMixin):
    def process_request(self, request):
        tzname = request.COOKIES.get('django_timezone')
        if tzname:
            try:
                timezone.activate(ZoneInfo(tzname))
            except ZoneInfoNotFoundError:
                timezone.deactivate()
        else:
            timezone.deactivate()

class AutoProgressOrdersMiddleware(MiddlewareMixin):
    """Automatically progress orders from 'created' to 'in_progress' after 10 minutes
    without requiring users to visit the order page.

    Also marks orders as overdue based on 2 hour threshold.
    Also computes header notification metrics for stale in-progress orders (>24h).
    """
    def process_request(self, request):
        try:
            now = timezone.now()
            # Bulk-progress eligible orders
            ten_min_ago = now - timedelta(minutes=10)
            # Auto-progress orders from 'created' to 'in_progress' after 10 minutes
            # Set started_at to the created_at timestamp (when order was initiated)
            # Exclude inquiries as they auto-complete
            updated = Order.objects.filter(status='created', created_at__lte=ten_min_ago).exclude(type='inquiry')
            if updated.exists():
                # Use F() to set started_at from created_at, preserving the actual start time
                from django.db.models import F
                updated.update(status='in_progress', started_at=F('created_at'))
        except Exception as e:
            # Do not block the request pipeline on errors
            pass

        # Mark orders as overdue based on 2 hour threshold
        # Only check orders that are in_progress and have started_at set
        try:
            from .utils.time_utils import is_order_overdue
            now = timezone.now()
            in_progress_orders = Order.objects.filter(
                status='in_progress',
                started_at__isnull=False
            ).exclude(type='inquiry').select_related('customer')

            for order in in_progress_orders:
                if is_order_overdue(order.started_at, now):
                    order.status = 'overdue'
                    order.save(update_fields=['status'])
        except Exception as e:
            # Do not block the request pipeline on errors
            pass

        # Compute stale in-progress (>24h) for header notifications
        try:
            cutoff = timezone.now() - timedelta(hours=24)
            stale_qs = Order.objects.select_related('customer').filter(status='in_progress').filter(
                ( (timezone.is_aware(cutoff) and (timezone.now() - cutoff).total_seconds() >= 0) and
                  ((~Order.started_at.isnull()) | (~Order.created_at.isnull()))
                )
            )
        except Exception:
            # Fallback simple filter
            stale_qs = Order.objects.filter(status='in_progress', started_at__lte=timezone.now()-timedelta(hours=24))
        try:
            request.stale_in_progress_count = stale_qs.count()
            request.stale_in_progress_list = list(stale_qs.order_by('-started_at')[:5].values('id','order_number','customer__full_name','started_at'))
        except Exception:
            request.stale_in_progress_count = 0
            request.stale_in_progress_list = []
