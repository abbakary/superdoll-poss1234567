"""
Views for quick order start workflow and started orders management.
Allows users to quickly start an order with plate number, then complete the order.
"""

import json
import logging
from datetime import datetime
from decimal import Decimal
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpRequest
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db import transaction

from .models import Order, Customer, Vehicle, Branch, ServiceType, ServiceAddon, InventoryItem, Invoice, InvoiceLineItem
from .utils import get_user_branch, scope_queryset
from .services import OrderService

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["POST"])
def api_start_order(request):
    """
    Start order endpoint enhanced:
    Accepts:
      - plate_number (required)
      - order_type (service|sales|inquiry|labour|unspecified|mixed)
      - use_existing_customer (optional boolean)
      - existing_customer_id (optional int)
      - service_selection (optional list of service names)
      - estimated_duration (optional int minutes)
      - force_new_order (optional boolean) - if True, creates new order even if one exists for this plate

    If plate exists in current branch and use_existing_customer is not provided, the endpoint will return existing_customer info
    so the frontend can ask the user whether to reuse existing customer or continue as new.

    If an order with status='created' already exists for this plate and force_new_order is False, return that order instead of creating a duplicate.
    If force_new_order is True, create a new order regardless of existing orders.
    """
    try:
        data = json.loads(request.body)
        plate_number = (data.get('plate_number') or '').strip().upper()
        order_type = data.get('order_type', 'service')
        use_existing = data.get('use_existing_customer', False)
        existing_customer_id = data.get('existing_customer_id')
        service_selection = data.get('service_selection') or []
        estimated_duration = data.get('estimated_duration')
        force_new_order = data.get('force_new_order', False)

        if not plate_number and not (use_existing and existing_customer_id):
            return JsonResponse({'success': False, 'error': 'Vehicle plate number is required'}, status=400)

        # Accept all valid order types from Order.TYPE_CHOICES plus 'mixed' for multi-category orders
        valid_order_types = ['service', 'sales', 'inquiry', 'labour', 'unspecified', 'mixed']
        if order_type not in valid_order_types:
            return JsonResponse({'success': False, 'error': f'Invalid order type. Must be one of: {", ".join(valid_order_types)}'}, status=400)

        user_branch = get_user_branch(request.user)
        from .services import CustomerService, VehicleService

        with transaction.atomic():
            # Decide which customer to use
            if existing_customer_id:
                # Use the specified customer
                if user_branch:
                    customer = get_object_or_404(Customer, id=existing_customer_id, branch=user_branch)
                else:
                    customer = get_object_or_404(Customer, id=existing_customer_id)
                    # Use customer's branch if user doesn't have one
                    if not user_branch and customer.branch:
                        user_branch = customer.branch
            else:
                customer = None

            # Check for existing orders only if not using a pre-selected customer and not forcing new order
            # (if customer is pre-selected or force_new_order is True, allow creating new order with same plate)
            existing_vehicle = None
            if plate_number and not existing_customer_id and not force_new_order:
                existing_vehicle = Vehicle.objects.filter(plate_number__iexact=plate_number, customer__branch=user_branch).select_related('customer').first()
                if existing_vehicle:
                    # Check if there's already a started (in_progress) order for this vehicle
                    existing_order = Order.objects.filter(
                        vehicle=existing_vehicle,
                        status__in=['created', 'in_progress']
                    ).order_by('-created_at').first()

                    if existing_order:
                        # Return existing order instead of creating a duplicate
                        return JsonResponse({
                            'success': True,
                            'order_id': existing_order.id,
                            'order_number': existing_order.order_number,
                            'plate_number': plate_number,
                            'started_at': existing_order.started_at.isoformat() if existing_order.started_at else None,
                            'existing_order': True,
                            'message': 'Existing order found for this plate'
                        }, status=200)

                    # Inform frontend that a customer exists for this plate
                    return JsonResponse({
                        'success': True,
                        'existing_customer': {
                            'id': existing_vehicle.customer.id,
                            'full_name': existing_vehicle.customer.full_name,
                            'phone': existing_vehicle.customer.phone,
                        },
                        'existing_vehicle': {
                            'id': existing_vehicle.id,
                            'plate': existing_vehicle.plate_number,
                            'make': existing_vehicle.make,
                            'model': existing_vehicle.model,
                        },
                        'message': 'Vehicle found for existing customer. Use the existing customer link or continue to create a new order.'
                    }, status=200)

            # Create new customer if not using pre-selected one
            if not customer:
                try:
                    name_src = plate_number or f"Customer {timezone.now().strftime('%Y%m%d%H%M')}"
                    phone_src = plate_number and f"PLATE_{plate_number}" or None
                    customer, customer_created = CustomerService.create_or_get_customer(
                        branch=user_branch,
                        full_name=f"Plate {name_src}" if plate_number else name_src,
                        phone=phone_src,
                        customer_type='personal',
                    )
                except Exception:
                    customer, customer_created = Customer.objects.get_or_create(
                        branch=user_branch,
                        full_name=f"Plate {plate_number}" if plate_number else f"Customer {timezone.now().strftime('%Y%m%d%H%M')}",
                        phone=(f"PLATE_{plate_number}" if plate_number else None),
                        organization_name=None,
                        tax_number=None,
                        defaults={'customer_type': 'personal'}
                    )

            # Create or get vehicle for the customer
            vehicle = None
            if plate_number and customer:
                vehicle = VehicleService.create_or_get_vehicle(
                    customer=customer,
                    plate_number=plate_number
                )

            # Calculate estimated duration from selected services if provided
            try:
                if service_selection and order_type == 'service':
                    svc_objs = ServiceType.objects.filter(name__in=service_selection, is_active=True)
                    from .models import ServiceAddon
                    add_objs = ServiceAddon.objects.filter(name__in=service_selection)
                    total_minutes = sum(int(s.estimated_minutes or 0) for s in svc_objs) + sum(int(a.estimated_minutes or 0) for a in add_objs)
                    if total_minutes:
                        estimated_duration = total_minutes
            except Exception:
                pass

            # Build description
            desc = f"Order started"
            if plate_number:
                desc += f" for {plate_number}"
            if service_selection:
                desc += ": " + ", ".join(service_selection)

            # Only reuse existing orders if force_new_order is False
            order = None
            if not force_new_order:
                # Prefer returning any existing 'created' order to avoid duplicates
                if vehicle:
                    existing_created = Order.objects.filter(
                        vehicle=vehicle,
                        status='created'
                    ).order_by('-created_at').first()

                    if existing_created:
                        order = existing_created
                        try:
                            from .services import CustomerService
                            CustomerService.update_customer_visit(customer)
                        except Exception:
                            pass

            if not order:
                if not force_new_order and vehicle:
                    # If there's already an active in-progress order, reuse it
                    existing_order = Order.objects.filter(
                        vehicle=vehicle,
                        status__in=['in_progress', 'overdue']
                    ).order_by('-started_at').first()

                    if existing_order:
                        order = existing_order
                        try:
                            from .services import CustomerService
                            CustomerService.update_customer_visit(customer)
                        except Exception:
                            pass

            if not order:
                # Create new order as 'created' (started state). It will auto-progress to 'in_progress' after 10 minutes.
                # Use OrderService to ensure proper visit tracking
                order = OrderService.create_order(
                    customer=customer,
                    order_type=order_type,
                    branch=user_branch,
                    vehicle=vehicle,
                    description=desc,
                    priority='medium',
                    estimated_duration=estimated_duration if estimated_duration else None,
                )

        return JsonResponse({
            'success': True,
            'order_id': order.id,
            'order_number': order.order_number,
            'plate_number': plate_number,
            'status': order.status,
            'created_at': order.created_at.isoformat(),
            'started_at': order.started_at.isoformat() if order.started_at else None,
        }, status=201)

    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Error starting order: {str(e)}")
        return JsonResponse({'success': False, 'error': f'Server error: {str(e)}'}, status=500)


@login_required
@require_http_methods(["POST"])
def api_check_plate(request):
    """Check if a plate number exists under the current branch and return customer/vehicle info."""
    try:
        data = json.loads(request.body)
        plate_number = (data.get('plate_number') or '').strip().upper()
        if not plate_number:
            return JsonResponse({'found': False})

        user_branch = get_user_branch(request.user)
        vehicle = Vehicle.objects.filter(plate_number__iexact=plate_number, customer__branch=user_branch).select_related('customer').first()
        if not vehicle:
            return JsonResponse({'found': False})

        return JsonResponse({'found': True, 'customer': {'id': vehicle.customer.id, 'full_name': vehicle.customer.full_name, 'phone': vehicle.customer.phone}, 'vehicle': {'id': vehicle.id, 'plate': vehicle.plate_number, 'make': vehicle.make, 'model': vehicle.model}})
    except Exception as e:
        logger.error(f"Error checking plate: {e}")
        return JsonResponse({'found': False, 'error': str(e)}, status=500)


@login_required
@require_http_methods(["GET"])
def api_service_types(request):
    """Return list of active service types, addons, and inventory items for UI."""
    try:
        svc_qs = ServiceType.objects.filter(is_active=True).order_by('name')
        service_types = [{'name': s.name, 'estimated_minutes': s.estimated_minutes or 0} for s in svc_qs]

        addon_qs = ServiceAddon.objects.filter(is_active=True).order_by('name')
        service_addons = [{'name': a.name, 'estimated_minutes': a.estimated_minutes or 0} for a in addon_qs]

        items_qs = InventoryItem.objects.select_related('brand').filter(is_active=True).order_by('brand__name', 'name')
        inventory_items = []
        for item in items_qs:
            brand_name = item.brand.name if item.brand else 'Unbranded'
            inventory_items.append({
                'id': item.id,
                'name': item.name,
                'brand': brand_name,
                'quantity': item.quantity or 0,
                'price': float(item.price or 0)
            })

        logger.debug(f"api_service_types: Returning {len(inventory_items)} inventory items")
        return JsonResponse({
            'service_types': service_types,
            'service_addons': service_addons,
            'inventory_items': inventory_items
        })
    except Exception as e:
        logger.error(f"Error fetching service types: {e}", exc_info=True)
        return JsonResponse({
            'service_types': [],
            'service_addons': [],
            'inventory_items': []
        }, status=500)


@login_required
def started_orders_dashboard(request):
    """
    Display all started orders for the current branch.
    Shows orders that have been initiated and are being managed, regardless of creation method.
    Supports filtering by status and includes all orders (created, in_progress, completed, etc).
    Grouped by plate number for easy continuation.

    GET params:
    - status: Filter by order status (default: shows created, in_progress, completed from today/recent)
    - sort_by: Sort orders by 'started_at', 'plate_number', 'order_type' (default: '-started_at')
    - search: Search by plate number or customer name
    """
    from django.db.models import Q, Count

    status_filter = request.GET.get('status', '')
    sort_by = request.GET.get('sort_by', '-started_at')
    search_query = request.GET.get('search', '').strip()

    # Build base queryset: scope to user's branch/permissions
    base_orders = scope_queryset(Order.objects.all(), request.user, request)

    # Apply status filter
    if status_filter:
        # Specific status requested
        orders = base_orders.filter(status=status_filter).select_related('customer', 'vehicle')
    else:
        # Default: show active orders (created/in_progress/overdue) + completed from today
        today = timezone.now().date()
        orders = base_orders.filter(
            Q(status__in=['created', 'in_progress', 'overdue']) |  # All active orders (including overdue)
            Q(status='completed', completed_at__date=today)  # Completed today
        ).select_related('customer', 'vehicle')

    # Apply search filter
    if search_query:
        orders = orders.filter(
            Q(vehicle__plate_number__icontains=search_query) |
            Q(customer__full_name__icontains=search_query)
        )

    # Apply sorting (handle related fields properly)
    if sort_by == 'plate_number':
        orders = orders.order_by('vehicle__plate_number')
    elif sort_by == 'type':
        orders = orders.order_by('type')
    elif sort_by == 'started_at':
        orders = orders.order_by('started_at')
    elif sort_by == '-started_at':
        orders = orders.order_by('-started_at')
    else:
        # Default: sort by newest first
        orders = orders.order_by('-started_at')

    # Group orders by plate number
    orders_by_plate = {}
    for order in orders:
        plate = order.vehicle.plate_number if order.vehicle else 'Unknown'
        if plate not in orders_by_plate:
            orders_by_plate[plate] = []
        orders_by_plate[plate].append(order)

    # Calculate statistics
    # Total started orders: all active statuses (created, in_progress, overdue)
    total_started = base_orders.filter(
        status__in=['created', 'in_progress', 'overdue']
    ).count()

    # Orders started today: those created today (before or after auto-progression)
    today = timezone.now().date()
    today_started = base_orders.filter(
        status__in=['created', 'in_progress', 'overdue'],
        created_at__date=today
    ).count()

    # Calculate repeated vehicles today (vehicles with 2+ orders created today)
    today_orders = base_orders.filter(
        created_at__date=today,
        vehicle__isnull=False
    ).values('vehicle__plate_number').annotate(order_count=Count('id')).filter(order_count__gte=2)
    repeated_vehicles_today = today_orders.count()

    context = {
        'orders': orders,
        'orders_by_plate': orders_by_plate,
        'total_started': total_started,
        'today_started': today_started,
        'repeated_vehicles_today': repeated_vehicles_today,
        'search_query': search_query,
        'status_filter': status_filter,
        'sort_by': sort_by,
        'title': 'Started Orders',
    }

    return render(request, 'tracker/started_orders_dashboard.html', context)


@login_required
def started_order_detail(request, order_id):
    """
    Show detail view for a started order with options to:
    - Upload/scan document for extraction
    - Manually enter customer details
    - Upload document and auto-populate
    - Edit and complete the order
    
    GET params:
    - tab: Active tab ('overview', 'customer', 'vehicle', 'document', 'order_details')
    """
    user_branch = get_user_branch(request.user)
    is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
    order = get_object_or_404(Order, id=order_id)
    branch_mismatch = False
    if user_branch and order.branch and order.branch != user_branch:
        branch_mismatch = True
    
    if request.method == 'POST':
        # Handle form submissions for different sections
        action = request.POST.get('action')

        if action == 'update_customer':
            # Update customer details
            order.customer.full_name = request.POST.get('full_name', order.customer.full_name)
            order.customer.phone = request.POST.get('phone', order.customer.phone)
            order.customer.email = request.POST.get('email', order.customer.email) or None
            order.customer.address = request.POST.get('address', order.customer.address) or None
            order.customer.customer_type = request.POST.get('customer_type', order.customer.customer_type)
            personal_subtype = request.POST.get('personal_subtype', '').strip()
            if personal_subtype:
                order.customer.personal_subtype = personal_subtype
            order.customer.save()
            
        elif action == 'update_vehicle':
            # Update vehicle details
            if order.vehicle:
                order.vehicle.make = request.POST.get('make', order.vehicle.make)
                order.vehicle.model = request.POST.get('model', order.vehicle.model)
                order.vehicle.vehicle_type = request.POST.get('vehicle_type', order.vehicle.vehicle_type)
                order.vehicle.save()

        elif action == 'update_order_details':
            # Update selected services, add-ons, items, and estimated duration
            try:
                services = request.POST.getlist('services') or []
                est = request.POST.get('estimated_duration') or None
                item_id = request.POST.get('item_id') or None
                item_quantity = request.POST.get('item_quantity') or None

                # Handle item/brand update for sales orders
                if order.type == 'sales' and item_id:
                    try:
                        from .models import InventoryItem
                        item = InventoryItem.objects.select_related('brand').get(id=int(item_id))
                        order.item_name = item.name
                        order.brand = item.brand.name if item.brand else 'Unbranded'
                        if item_quantity:
                            try:
                                order.quantity = int(item_quantity)
                            except (ValueError, TypeError):
                                pass
                    except InventoryItem.DoesNotExist:
                        logger.warning(f"Inventory item {item_id} not found when updating order {order.id}")
                    except Exception as e:
                        logger.error(f"Error updating item for order {order.id}: {e}")

                # Handle services/add-ons update
                if services:
                    # Append services to description (simple storage)
                    svc_text = ', '.join(services)
                    base_desc = order.description or ''
                    # Remove previous Services/Add-ons lines if exists
                    lines = [l for l in base_desc.split('\n') if not (l.strip().lower().startswith('services:') or l.strip().lower().startswith('add-ons:') or l.strip().lower().startswith('tire services:'))]

                    # For sales orders, append as add-ons; for service orders, append as services
                    if order.type == 'sales':
                        lines.append(f"Tire Services: {svc_text}")
                    else:
                        lines.append(f"Services: {svc_text}")

                    order.description = '\n'.join([l for l in lines if l.strip()])

                # Update estimated duration
                if est:
                    try:
                        order.estimated_duration = int(est)
                    except Exception:
                        pass

                order.save()
                # Redirect to refresh page and show changes
                return redirect('tracker:started_order_detail', order_id=order.id)
            except Exception as e:
                logger.error(f"Error updating order details: {e}")

        
        elif action == 'complete_order':
            # Handle delay reason before completing
            try:
                from .utils.time_utils import is_order_overdue
                from .models import DelayReason
                exceeds_9_hours = False
                if order.started_at:
                    exceeds_9_hours = is_order_overdue(order.started_at) if order.status == 'in_progress' else (
                        order.actual_duration and order.actual_duration >= (9 * 60)
                    )

                # Check if delay reason is required
                if exceeds_9_hours:
                    delay_reason_id = request.POST.get('delay_reason')
                    if not delay_reason_id:
                        messages.error(request, 'Order has exceeded 2 hours. Please select a delay reason before completing.')
                        return redirect('tracker:started_order_detail', order_id=order.id)

                    try:
                        delay_reason = DelayReason.objects.get(id=delay_reason_id)
                        order.delay_reason = delay_reason
                        order.delay_reason_reported_at = timezone.now()
                        order.delay_reason_reported_by = request.user
                        order.exceeded_9_hours = True
                    except DelayReason.DoesNotExist:
                        messages.error(request, 'Selected delay reason not found. Please select a valid reason.')
                        return redirect('tracker:started_order_detail', order_id=order.id)
                else:
                    # For orders not exceeding 2 hours, save optional delay reason if provided
                    delay_reason_id = request.POST.get('delay_reason')
                    if delay_reason_id:
                        try:
                            delay_reason = DelayReason.objects.get(id=delay_reason_id)
                            order.delay_reason = delay_reason
                            order.delay_reason_reported_at = timezone.now()
                            order.delay_reason_reported_by = request.user
                        except DelayReason.DoesNotExist:
                            logger.warning(f"Optional delay reason ID {delay_reason_id} not found for order {order.id}")

                # Save optional comments
                comments = (request.POST.get('delay_reason_comments') or '').strip()
                if comments:
                    order.overrun_reason = comments
                    order.overrun_reported_at = order.overrun_reported_at or timezone.now()
                    order.overrun_reported_by = order.overrun_reported_by or request.user

            except Exception as e:
                logger.error(f"Error handling delay reason for order {order.id}: {e}")
                messages.error(request, f'Error processing delay reason: {str(e)}')
                return redirect('tracker:started_order_detail', order_id=order.id)

            # Mark order as completed
            order.status = 'completed'
            order.completed_at = timezone.now()
            order.save()

            messages.success(request, 'Order completed successfully.')
            return redirect('tracker:started_orders_dashboard')
    
    active_tab = request.GET.get('tab', 'overview')

    # Check if order exceeds 2+ hours
    exceeds_9_hours = False
    if order.started_at:
        try:
            from .utils.time_utils import is_order_overdue
            exceeds_9_hours = is_order_overdue(order.started_at) if order.status == 'in_progress' else (
                order.actual_duration and order.actual_duration >= (2 * 60)  # 2 hours in minutes
            )
        except Exception:
            exceeds_9_hours = False

    # Fetch delay reason categories and reasons for template rendering
    delay_reasons_by_category = {}
    try:
        from .models import DelayReasonCategory, DelayReason
        for category in DelayReasonCategory.objects.filter(is_active=True):
            reasons = list(DelayReason.objects.filter(category=category, is_active=True).values('id', 'reason_text'))
            delay_reasons_by_category[category.category] = reasons
        # Convert to JSON string for template
        import json
        delay_reasons_for_template = json.dumps(delay_reasons_by_category)
    except Exception:
        delay_reasons_for_template = delay_reasons_by_category

    context = {
        'order': order,
        'customer': order.customer,
        'vehicle': order.vehicle,
        'active_tab': active_tab,
        'title': f'Order {order.order_number}',
        'branch_mismatch': branch_mismatch,
        'user_branch': user_branch,
        'is_admin': is_admin,
        'exceeds_9_hours': exceeds_9_hours,
        'delay_reason_categories': [],
        'delay_reasons_by_category': delay_reasons_for_template,
    }

    return render(request, 'tracker/started_order_detail.html', context)


@login_required
@require_http_methods(["POST"])
def api_update_order_from_extraction(request):
    """
    Update an existing order with extracted/edited data from the extraction modal.

    Form fields:
      - order_id: the order to update
      - extracted_customer_type: 'personal', 'company', 'government', 'ngo'
      - extracted_personal_subtype: 'owner' or 'driver' (for personal customers)
      - extracted_organization_name: (for organizational customers)
      - extracted_tax_number: (for organizational customers)
      - extracted_customer_name: customer full name
      - extracted_phone: customer phone
      - extracted_email: customer email (optional)
      - extracted_address: customer address (optional)
      - extracted_description: order description
      - extracted_estimated_duration: estimated duration in minutes
      - extracted_priority: low, medium, high, urgent
      - extracted_services: comma-separated service names
      - extracted_plate: vehicle plate (optional)
      - extracted_make: vehicle make (optional)
      - extracted_model: vehicle model (optional)
    """
    try:
        user_branch = get_user_branch(request.user)
        order_id = request.POST.get('order_id')

        if not order_id:
            return JsonResponse({
                'success': False,
                'error': 'Order ID is required'
            }, status=400)

        # Get the order
        is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
        if user_branch and not is_admin:
            order = get_object_or_404(Order, id=order_id, branch=user_branch)
        else:
            order = get_object_or_404(Order, id=order_id)

        # Extract form data
        customer_type = request.POST.get('extracted_customer_type', '').strip()
        personal_subtype = request.POST.get('extracted_personal_subtype', '').strip()
        organization_name = request.POST.get('extracted_organization_name', '').strip()
        tax_number = request.POST.get('extracted_tax_number', '').strip()

        customer_name = request.POST.get('extracted_customer_name', '').strip()
        phone = request.POST.get('extracted_phone', '').strip()
        email = request.POST.get('extracted_email', '').strip()
        address = request.POST.get('extracted_address', '').strip()

        description = request.POST.get('extracted_description', '').strip()
        estimated_duration = request.POST.get('extracted_estimated_duration', '').strip()
        priority = request.POST.get('extracted_priority', 'medium').strip()
        services = request.POST.get('extracted_services', '').strip()

        plate_number = request.POST.get('extracted_plate', '').strip().upper()
        vehicle_make = request.POST.get('extracted_make', '').strip()
        vehicle_model = request.POST.get('extracted_model', '').strip()

        # Validate required fields
        if not customer_name or not phone:
            return JsonResponse({
                'success': False,
                'error': 'Customer name and phone are required'
            }, status=400)

        if not customer_type:
            return JsonResponse({
                'success': False,
                'error': 'Customer type is required'
            }, status=400)

        if customer_type not in ['personal', 'company', 'government', 'ngo']:
            return JsonResponse({
                'success': False,
                'error': 'Invalid customer type'
            }, status=400)

        # Validate customer type specific fields
        if customer_type == 'personal' and not personal_subtype:
            return JsonResponse({
                'success': False,
                'error': 'Personal subtype is required for personal customers'
            }, status=400)

        if customer_type in ['company', 'government', 'ngo']:
            if not organization_name or not tax_number:
                return JsonResponse({
                    'success': False,
                    'error': 'Organization name and tax number are required'
                }, status=400)

        with transaction.atomic():
            from .services import CustomerService, VehicleService

            # Update or create customer
            if customer_type == 'personal':
                customer, _ = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=customer_name,
                    phone=phone,
                    customer_type=customer_type,
                    personal_subtype=personal_subtype,
                    email=email or None,
                    address=address or None,
                )
            else:
                customer, _ = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=customer_name,
                    phone=phone,
                    customer_type=customer_type,
                    organization_name=organization_name,
                    tax_number=tax_number,
                    email=email or None,
                    address=address or None,
                )

            # Update order customer
            order.customer = customer

            # Update or create vehicle if plate is provided
            vehicle = None
            if plate_number:
                vehicle = VehicleService.create_or_get_vehicle(
                    customer=customer,
                    plate_number=plate_number,
                    make=vehicle_make or None,
                    model=vehicle_model or None,
                )
                order.vehicle = vehicle

            # Parse estimated duration
            try:
                est_duration = int(estimated_duration) if estimated_duration else None
            except (ValueError, TypeError):
                est_duration = None

            # Build description with services if provided
            final_description = description or ''
            if services:
                service_list = [s.strip() for s in services.split(',') if s.strip()]
                if service_list:
                    services_text = f"Services: {', '.join(service_list)}"
                    final_description = f"{final_description}\n{services_text}" if final_description else services_text

            # Update order fields
            order.description = final_description
            order.priority = priority if priority in ['low', 'medium', 'high', 'urgent'] else 'medium'
            if est_duration:
                order.estimated_duration = est_duration

            order.save()

            # Handle adding another component if requested
            add_component = request.POST.get('add_component', '').strip().lower() in ['on', 'true', '1', 'yes']
            if add_component:
                from .models import OrderComponent

                component_type = request.POST.get('component_type', '').strip().lower()
                component_reason = request.POST.get('component_reason', '').strip()

                if component_type in ['service', 'sales'] and component_reason:
                    # Check if component already exists
                    if not OrderComponent.objects.filter(order=order, type=component_type).exists():
                        # Create the component
                        component = OrderComponent.objects.create(
                            order=order,
                            type=component_type,
                            reason=component_reason,
                            added_by=request.user
                        )

                        # Store additional details if it's a sales component
                        if component_type == 'sales':
                            component_item_name = request.POST.get('component_item_name', '').strip()
                            component_brand = request.POST.get('component_brand', '').strip()
                            component_quantity = request.POST.get('component_quantity', '1').strip()
                            component_tire_type = request.POST.get('component_tire_type', '').strip()

                            # Append to order description for reference
                            if component_item_name:
                                component_desc = f"\n\nAdded Item ({component_type.title()}):\n- Item: {component_item_name}"
                                if component_brand:
                                    component_desc += f"\n- Brand: {component_brand}"
                                if component_quantity:
                                    component_desc += f"\n- Qty: {component_quantity}"
                                if component_tire_type:
                                    component_desc += f"\n- Type: {component_tire_type}"
                                order.description = (order.description or '') + component_desc
                                order.save()

        return JsonResponse({
            'success': True,
            'message': 'Order updated successfully',
            'order_id': order.id,
            'order_number': order.order_number
        }, status=200)

    except Exception as e:
        logger.error(f"Error updating order from extraction: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': f'Failed to update order: {str(e)}'
        }, status=500)


@login_required
@require_http_methods(["POST"])
def api_create_order_from_modal(request):
    """
    Create order from modal form submission or invoice upload.
    Accepts form data with order type, customer type, and extracted details.

    Form fields:
      - order_type: 'service', 'sales', 'inquiry', or 'upload'
      - customer_id: (optional) existing customer ID for pre-selected customer
      - customer_type: 'personal', 'company', 'government', 'ngo'
      - personal_subtype: 'owner' or 'driver' (for personal customers)
      - organization_name: (required for organizational customers)
      - tax_number: (required for organizational customers)
      - customer_name: full name
      - phone: phone number
      - email: email (optional)
      - address: address (optional)
      - description: order description
      - estimated_duration: minutes
      - priority: low, medium, high, urgent
      - plate_number: vehicle plate (optional)
      - vehicle_make: vehicle make (optional)
      - vehicle_model: vehicle model (optional)
      - subtotal: (for upload type) Net/Subtotal amount
      - tax_amount: (for upload type) VAT/Tax amount
      - total_amount: (for upload type) Gross/Total amount
    """
    try:
        user_branch = get_user_branch(request.user)

        # Check if customer_id is provided (pre-selected customer from order creation page)
        customer_id = request.POST.get('customer_id')
        if customer_id:
            # Use existing customer - do NOT create new one
            try:
                is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
                if user_branch and not is_admin:
                    customer = Customer.objects.get(id=int(customer_id), branch=user_branch)
                else:
                    customer = Customer.objects.get(id=int(customer_id))
            except (Customer.DoesNotExist, ValueError):
                return JsonResponse({
                    'success': False,
                    'error': 'Selected customer not found'
                }, status=400)
        else:
            # Extract customer data from form
            order_type = request.POST.get('order_type', 'service').strip()
            customer_type = request.POST.get('customer_type', 'personal').strip()
            personal_subtype = request.POST.get('personal_subtype', '').strip()
            organization_name = request.POST.get('organization_name', '').strip()
            tax_number = request.POST.get('tax_number', '').strip()

            customer_name = request.POST.get('customer_name', '').strip()
            phone = request.POST.get('phone', '').strip()
            email = request.POST.get('email', '').strip()
            address = request.POST.get('address', '').strip()

            # Validate required fields
            if not customer_name or not phone:
                return JsonResponse({
                    'success': False,
                    'error': 'Customer name and phone are required'
                }, status=400)

            if order_type not in ['service', 'sales', 'inquiry', 'upload']:
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid order type'
                }, status=400)

            if customer_type not in ['personal', 'company', 'government', 'ngo']:
                return JsonResponse({
                    'success': False,
                    'error': 'Invalid customer type'
                }, status=400)

            # Validate customer type specific fields
            if customer_type == 'personal' and not personal_subtype:
                return JsonResponse({
                    'success': False,
                    'error': 'Personal subtype is required for personal customers'
                }, status=400)

            if customer_type in ['company', 'government', 'ngo']:
                if not organization_name or not tax_number:
                    return JsonResponse({
                        'success': False,
                        'error': 'Organization name and tax number are required'
                    }, status=400)

            from .services import CustomerService

            # Create or get customer
            if customer_type == 'personal':
                customer, _ = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=customer_name,
                    phone=phone,
                    customer_type=customer_type,
                    personal_subtype=personal_subtype,
                    email=email or None,
                    address=address or None,
                )
            else:
                customer, _ = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=customer_name,
                    phone=phone,
                    customer_type=customer_type,
                    organization_name=organization_name,
                    tax_number=tax_number,
                    email=email or None,
                    address=address or None,
                )

        # Extract order details
        order_type = (request.POST.get('order_type') or request.POST.get('type') or 'service').strip()
        description = request.POST.get('description', '').strip()
        estimated_duration = request.POST.get('estimated_duration', '').strip()
        priority = request.POST.get('priority', 'medium').strip()

        plate_number = request.POST.get('plate_number', '').strip().upper()
        vehicle_make = request.POST.get('vehicle_make', '').strip()
        vehicle_model = request.POST.get('vehicle_model', '').strip()

        # For upload type, extract invoice amounts
        subtotal = request.POST.get('subtotal', '0').strip()
        tax_amount = request.POST.get('tax_amount', '0').strip()
        total_amount = request.POST.get('total_amount', '0').strip()

        with transaction.atomic():
            from .services import VehicleService

            # Create or get vehicle if plate is provided
            vehicle = None
            if plate_number:
                vehicle = VehicleService.create_or_get_vehicle(
                    customer=customer,
                    plate_number=plate_number,
                    make=vehicle_make or None,
                    model=vehicle_model or None,
                )

            # Parse estimated duration
            try:
                est_duration = int(estimated_duration) if estimated_duration else None
            except (ValueError, TypeError):
                est_duration = None

            # Create order using OrderService to ensure proper visit tracking
            order = OrderService.create_order(
                customer=customer,
                order_type=order_type,
                branch=user_branch,
                vehicle=vehicle,
                description=description or f"Order for {customer.full_name}",
                priority=priority if priority in ['low', 'medium', 'high', 'urgent'] else 'medium',
                estimated_duration=est_duration,
            )

            # For upload type, create an invoice with extracted data
            if order_type == 'upload':
                from decimal import Decimal
                try:
                    subtotal_val = Decimal(str(subtotal or '0').replace(',', ''))
                    tax_val = Decimal(str(tax_amount or '0').replace(',', ''))
                    total_val = Decimal(str(total_amount or '0').replace(',', ''))

                    # Create invoice linked to this order
                    invoice = Invoice.objects.create(
                        branch=user_branch,
                        order=order,
                        customer=customer,
                        vehicle=vehicle,
                        invoice_date=timezone.localdate(),
                        subtotal=subtotal_val,
                        tax_amount=tax_val,
                        total_amount=total_val or (subtotal_val + tax_val),
                        created_by=request.user
                    )
                    invoice.generate_invoice_number()
                    invoice.save()

                    # If description contains item details, create line items
                    if description:
                        from .models import InvoiceLineItem
                        lines = description.split('\n')
                        for line in lines:
                            if line.strip():
                                InvoiceLineItem.objects.create(
                                    invoice=invoice,
                                    description=line.strip(),
                                    quantity=1,
                                    unit_price=Decimal('0'),
                                    order_type='unknown'
                                )
                except Exception as e:
                    logger.warning(f"Failed to create invoice from upload: {e}")

        # Return success response
        return JsonResponse({
            'success': True,
            'message': 'Order created successfully',
            'order_id': order.id,
            'order_number': order.order_number,
            'redirect_url': f'/tracker/orders/started/{order.id}/'
        }, status=201)

    except Exception as e:
        logger.error(f"Error creating order from modal: {str(e)}", exc_info=True)
        return JsonResponse({
            'success': False,
            'error': f'Failed to create order: {str(e)}'
        }, status=500)


@require_http_methods(["POST"])
@login_required
def api_record_overrun_reason(request, order_id):
    """Record an overrun/delay reason for an order (AJAX).
    Expects JSON: { "reason": "text" }
    Saves overrun_reason, overrun_reported_at, overrun_reported_by on the Order.
    Returns { success: true }
    """
    try:
        data = json.loads(request.body)
        reason = (data.get('reason') or '').strip()
        if not reason:
            return JsonResponse({'success': False, 'error': 'Reason is required'}, status=400)
        user_branch = get_user_branch(request.user)
        is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
        if user_branch and not is_admin:
            order = get_object_or_404(Order, id=order_id, branch=user_branch)
        else:
            order = get_object_or_404(Order, id=order_id)
        order.overrun_reason = reason
        order.overrun_reported_at = timezone.now()
        order.overrun_reported_by = request.user
        order.save(update_fields=['overrun_reason','overrun_reported_at','overrun_reported_by'])
        return JsonResponse({'success': True})
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'error': 'Invalid JSON'}, status=400)
    except Exception as e:
        logger.error(f"Error recording overrun reason for order {order_id}: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)






@login_required
@require_http_methods(["GET"])
def api_started_orders_kpis(request):
    """API endpoint to get KPI stats for started orders dashboard (for AJAX updates)."""
    try:
        from django.db.models import Count
        user_branch = get_user_branch(request.user)

        # Total started orders: both 'created' (just initiated) and 'in_progress' (being worked on)
        total_started = Order.objects.filter(
            branch=user_branch,
            status__in=['created', 'in_progress']
        ).count()

        # Orders started today: those created today
        today = timezone.now().date()
        today_started = Order.objects.filter(
            branch=user_branch,
            created_at__date=today,
            status__in=['created', 'in_progress']
        ).count()

        # Calculate repeated vehicles today (vehicles with 2+ orders created today)
        today_orders = Order.objects.filter(
            branch=user_branch,
            created_at__date=today,
            vehicle__isnull=False
        ).values('vehicle__plate_number').annotate(order_count=Count('id')).filter(order_count__gte=2)
        repeated_vehicles_today = today_orders.count()

        return JsonResponse({
            'success': True,
            'total_started': total_started,
            'today_started': today_started,
            'repeated_vehicles_today': repeated_vehicles_today
        })
    except Exception as e:
        logger.error(f"Error fetching started orders KPIs: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)


@login_required
@require_http_methods(["POST"])
def api_quick_stop_order(request):
    """
    Stop a started order quickly (mark as completed) without signature.
    Expects form body: order_id
    Sets completed_at and completion_date, computes actual_duration.
    """
    try:
        from django.utils import timezone
        order_id = request.POST.get('order_id')
        if not order_id:
            return JsonResponse({'success': False, 'error': 'order_id is required'}, status=400)
        user_branch = get_user_branch(request.user)
        is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
        if user_branch and not is_admin:
            order = get_object_or_404(Order, id=int(order_id), branch=user_branch)
        else:
            order = get_object_or_404(Order, id=int(order_id))

        now = timezone.now()
        if not order.started_at:
            order.started_at = now
        order.status = 'completed'
        order.completed_at = now
        order.completion_date = now
        try:
            reference = order.started_at or order.created_at or now
            order.actual_duration = int(((now - reference).total_seconds()) // 60)
        except Exception:
            order.actual_duration = None

        order.save(update_fields=['status','started_at','completed_at','completion_date','actual_duration'])
        return JsonResponse({'success': True, 'order_id': order.id})
    except Exception as e:
        logger.error(f"Error quick-stopping order: {e}")
        return JsonResponse({'success': False, 'error': str(e)}, status=500)
