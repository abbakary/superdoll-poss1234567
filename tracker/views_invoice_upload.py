"""
Invoice upload and extraction endpoints.
Handles two-step process: extract preview → create/update records
"""

import json
import logging
import re
from decimal import Decimal
import time
from functools import wraps
from django.db.utils import OperationalError
from datetime import datetime
from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.views.decorators.http import require_http_methods
from django.utils import timezone
from django.db import transaction

from .models import Order, Customer, Vehicle, Invoice, InvoiceLineItem, InvoicePayment, Branch, Salesperson
from .utils import get_user_branch
from .services import OrderService, CustomerService, VehicleService

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["GET"])
def api_get_salespersons(request):
    """API endpoint to fetch all active salespersons."""
    try:
        salespersons = Salesperson.objects.filter(is_active=True).order_by('code').values('id', 'code', 'name', 'is_default')
        return JsonResponse({
            'success': True,
            'salespersons': list(salespersons)
        })
    except Exception as e:
        logger.error(f"Error fetching salespersons: {e}")
        return JsonResponse({
            'success': False,
            'message': 'Failed to fetch salespersons'
        }, status=500)


def retry_on_db_lock(max_retries=3, initial_delay=0.1):
    """
    Decorator to retry a view function on database lock errors.
    This helps with SQLite concurrency issues by properly handling transaction rollback.

    Args:
        max_retries: Number of times to retry on database lock
        initial_delay: Initial delay in seconds before first retry
    """
    def decorator(func):
        @wraps(func)
        def wrapper(request, *args, **kwargs):
            from django.db import connection

            last_error = None
            delay = initial_delay

            for attempt in range(max_retries):
                try:
                    return func(request, *args, **kwargs)
                except (OperationalError, transaction.TransactionManagementError) as e:
                    error_msg = str(e).lower()
                    # Retry on database lock errors or broken transaction errors
                    if 'database is locked' in error_msg or 'current transaction' in error_msg or 'broken' in error_msg:
                        last_error = e

                        # Ensure transaction is properly cleaned up
                        try:
                            transaction.rollback()
                            connection.close()
                        except Exception as cleanup_err:
                            logger.warning(f"Error during transaction cleanup: {cleanup_err}")

                        if attempt < max_retries - 1:
                            logger.warning(f"Database lock detected (attempt {attempt + 1}/{max_retries}), retrying after {delay}s...")
                            time.sleep(delay)
                            delay *= 2  # Exponential backoff
                            continue
                    raise

            # All retries failed
            logger.error(f"Failed after {max_retries} retries: {last_error}")
            return JsonResponse({
                'success': False,
                'message': 'Database temporarily unavailable. Please try again in a moment.'
            }, status=503)

        return wrapper
    return decorator

def _save_with_retry(instance, update_fields=None, retries=5, delay=0.2):
    for i in range(int(retries)):
        try:
            if update_fields:
                instance.save(update_fields=update_fields)
            else:
                instance.save()
            return True
        except OperationalError as e:
            if 'database is locked' in str(e).lower() and i < int(retries) - 1:
                time.sleep(delay * (i + 1))
                continue
            raise
    return False


def _get_item_code_categories(item_codes):
    """
    Helper function to get category information for item codes.
    Queries LabourCode for each code and returns category and order type.

    Args:
        item_codes: List of item codes extracted from invoice

    Returns:
        Dict mapping code -> {category, order_type, color_class}
    """
    from tracker.models import LabourCode
    from tracker.utils.order_type_detector import _normalize_category_to_order_type

    if not item_codes:
        return {}

    # Clean codes
    cleaned_codes = [str(code).strip() for code in item_codes if code]
    if not cleaned_codes:
        return {}

    # Query database
    found_codes = LabourCode.objects.filter(
        code__in=cleaned_codes,
        is_active=True
    ).values('code', 'category')

    result = {}
    found_code_set = set()

    for row in found_codes:
        code = row['code']
        category = row['category']
        order_type = _normalize_category_to_order_type(category)

        # Assign color based on order type
        color_map = {
            'labour': 'badge-labour',
            'service': 'badge-service',
            'sales': 'badge-sales',
        }

        result[code] = {
            'category': category,
            'order_type': order_type,
            'color_class': color_map.get(order_type, 'badge-secondary')
        }
        found_code_set.add(code)

    # Add unmapped codes as 'sales'
    for code in cleaned_codes:
        if code not in found_code_set:
            result[code] = {
                'category': 'Sales',
                'order_type': 'sales',
                'color_class': 'badge-sales'
            }

    return result


@login_required
@require_http_methods(["POST"])
def api_extract_invoice_preview(request):
    """
    Step 1: Extract invoice data from uploaded PDF for preview.
    Returns extracted customer, order, and payment information.
    Does NOT create any records yet.

    POST fields:
      - file: PDF file to extract
      - selected_order_id (optional): Started order ID to link to
      - plate (optional): Vehicle plate number

    Returns:
      - success: true/false
      - header: Customer and payment info {invoice_no, customer_name, address, date, subtotal, tax, total}
      - items: Line items [{description, qty, value, code, category, order_type, color_class}]
      - raw_text: Full extracted text for reference
      - message: Error/status message
    """
    user_branch = get_user_branch(request.user)

    # Validate file upload
    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({
            'success': False,
            'message': 'No file uploaded'
        })

    try:
        file_bytes = uploaded.read()
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        return JsonResponse({
            'success': False,
            'message': 'Failed to read uploaded file'
        })

    # Extract text from PDF (non-OCR extractor with filename)
    try:
        from tracker.utils.pdf_text_extractor import extract_from_bytes as extract_pdf_text
        extracted = extract_pdf_text(file_bytes, uploaded.name)
    except Exception as e:
        logger.error(f"PDF extraction error: {e}")
        return JsonResponse({
            'success': False,
            'message': f'Failed to extract invoice data: {str(e)}',
            'error': str(e)
        })

    # If extraction failed - still return partial data for manual completion
    if not extracted.get('success'):
        logger.info(f"Extraction failed: {extracted.get('error')} - {extracted.get('message')}")
        return JsonResponse({
            'success': False,
            'message': extracted.get('message', 'Could not extract data from PDF. Please enter invoice details manually.'),
            'error': extracted.get('error'),
            'raw_text': extracted.get('raw_text', ''),
            'header': extracted.get('header', {}),
            'items': extracted.get('items', [])
        })

    # Return extracted preview data
    header = extracted.get('header') or {}
    items = extracted.get('items') or []

    # Get categories for all item codes
    item_codes = [item.get('code') for item in items if item.get('code')]
    code_categories = _get_item_code_categories(item_codes)

    # Enrich items with category information
    enriched_items = []
    for item in items:
        code = item.get('code', '')
        category_info = code_categories.get(code, {
            'category': 'Sales',
            'order_type': 'sales',
            'color_class': 'badge-sales'
        })

        enriched_items.append({
            'description': item.get('description', ''),
            'qty': int(item.get('qty', 1)) if isinstance(item.get('qty'), (int, float)) else 1,
            'unit': item.get('unit'),
            'code': code,
            'value': float(item.get('value') or 0),
            'rate': float(item.get('rate') or 0),
            'category': category_info.get('category'),
            'order_type': category_info.get('order_type'),
            'color_class': category_info.get('color_class')
        })

    return JsonResponse({
        'success': True,
        'message': 'Invoice data extracted successfully',
        'header': {
            'invoice_no': header.get('invoice_no'),
            'code_no': header.get('code_no'),
            'customer_name': header.get('customer_name'),
            'phone': header.get('phone'),
            'email': header.get('email'),
            'address': header.get('address'),
            'reference': header.get('reference'),
            'date': header.get('date'),
            'subtotal': float(header.get('subtotal') or 0),
            'tax': float(header.get('tax') or 0),
            'total': float(header.get('total') or 0),
            'payment_method': header.get('payment_method'),
            'delivery_terms': header.get('delivery_terms'),
            'remarks': header.get('remarks'),
            'attended_by': header.get('attended_by'),
            'kind_attention': header.get('kind_attention'),
            'seller_name': header.get('seller_name'),
            'seller_address': header.get('seller_address'),
            'seller_phone': header.get('seller_phone'),
            'seller_email': header.get('seller_email'),
            'seller_tax_id': header.get('seller_tax_id'),
            'seller_vat_reg': header.get('seller_vat_reg'),
        },
        'items': enriched_items,
        'raw_text': extracted.get('raw_text', '')
    })


@login_required
@require_http_methods(["POST"])
@retry_on_db_lock(max_retries=3, initial_delay=0.5)
def api_create_invoice_from_upload(request):
    """
    Step 2: Create/update customer, order, and invoice from extracted invoice data.
    This is called after user confirms extracted data.

    POST fields:
      - selected_order_id (optional): Existing started order to update
      - plate (optional): Vehicle plate number
      - pre_selected_customer_id (optional): Pre-selected customer ID (skip customer creation)

      Customer fields:
      - customer_name: Customer full name
      - customer_phone: Customer phone number
      - customer_email (optional): Customer email
      - customer_address (optional): Customer address
      - customer_type: personal|company|ngo|government

      Invoice fields:
      - invoice_number: Invoice number from invoice
      - invoice_date: Invoice date
      - subtotal: Subtotal amount
      - tax_amount: Tax/VAT amount
      - total_amount: Total amount
      - notes (optional): Additional notes

      Line items (arrays):
      - item_description[]: Item description
      - item_qty[]: Item quantity
      - item_price[]: Item unit price

    Returns:
      - success: true/false
      - invoice_id: Created invoice ID
      - order_id: Created/updated order ID
      - customer_id: Created/updated customer ID
      - redirect_url: URL to view created invoice
    """
    user_branch = get_user_branch(request.user)

    # Precompute order type detection before transaction
    item_codes_pre = request.POST.getlist('item_code[]')
    item_codes_pre = [code.strip() for code in item_codes_pre if code and code.strip()]
    item_codes_pre = sorted(set(item_codes_pre))
    detected_order_type = None
    categories = []
    mapping_info = {}
    for _ in range(3):
        try:
            from tracker.utils.order_type_detector import determine_order_type_from_codes
            detected_order_type, categories, mapping_info = determine_order_type_from_codes(item_codes_pre)
            break
        except OperationalError as e:
            if 'database is locked' in str(e).lower():
                time.sleep(0.2)
                continue
            else:
                detected_order_type, categories, mapping_info = 'sales', [], {'mapped': {}, 'unmapped': item_codes_pre, 'categories_found': [], 'order_types_found': []}
                break
        except Exception:
            detected_order_type, categories, mapping_info = 'sales', [], {'mapped': {}, 'unmapped': item_codes_pre, 'categories_found': [], 'order_types_found': []}
            break

    try:
        with transaction.atomic():
            # Priority 1: Use pre-selected customer (from started order detail page)
            # This prevents creating duplicate customers when uploading invoice for known customer
            customer_id = request.POST.get('pre_selected_customer_id') or request.POST.get('customer_id')
            customer_obj = None
            created = False

            customer_name = request.POST.get('customer_name', '').strip()
            customer_phone = request.POST.get('customer_phone', '').strip()
            customer_email = request.POST.get('customer_email', '').strip() or None
            customer_address = request.POST.get('customer_address', '').strip() or None
            customer_type = request.POST.get('customer_type', 'personal')
            plate = (request.POST.get('plate') or '').strip().upper() or None

            if customer_id:
                try:
                    is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
                    if user_branch and not is_admin:
                        customer_obj = Customer.objects.get(id=int(customer_id), branch=user_branch)
                    else:
                        customer_obj = Customer.objects.get(id=int(customer_id))
                    logger.info(f"Using pre-selected customer for invoice upload: {customer_obj.id}")
                except (Customer.DoesNotExist, ValueError):
                    return JsonResponse({'success': False, 'message': 'Selected customer not found'})

                # If the selected customer is a temporary placeholder from quick start (Plate/PLATE_),
                # check if the extracted customer details already exist as a real customer first.
                try:
                    is_temp = (customer_obj.full_name or '').startswith('Plate ') or (customer_obj.phone or '').startswith('PLATE_')
                    if is_temp and customer_name:
                        # CRITICAL: Before updating temp customer, check if extracted details already belong to another customer
                        # This prevents creating duplicate customers when same invoice is uploaded multiple times
                        existing_real_customer = None

                        # Check if customer with this phone exists (strong identifier)
                        if customer_phone:
                            existing_real_customer = Customer.objects.filter(
                                phone=customer_phone,
                                branch=user_branch
                            ).exclude(id=customer_obj.id).first()

                        # If found existing customer with same phone, use that instead
                        if existing_real_customer:
                            logger.info(f"Found existing customer {existing_real_customer.id} with phone {customer_phone}, using instead of temp customer {customer_obj.id}")
                            customer_obj = existing_real_customer
                        else:
                            # No existing customer found, update the temporary one in-place
                            # Map organization fields from either naming convention
                            org_name = (request.POST.get('organization_name') or request.POST.get('customer_organization_name') or '').strip() or None
                            tax_num = (request.POST.get('tax_number') or request.POST.get('customer_tax_number') or '').strip() or None
                            customer_obj.full_name = customer_name or customer_obj.full_name
                            if customer_phone:
                                customer_obj.phone = customer_phone
                            if customer_email:
                                customer_obj.email = customer_email
                            if customer_address:
                                customer_obj.address = customer_address
                            if customer_type:
                                customer_obj.customer_type = customer_type
                            if org_name:
                                customer_obj.organization_name = org_name
                            if tax_num:
                                customer_obj.tax_number = tax_num
                            customer_obj.save()
                            logger.info(f"Updated temporary customer {customer_obj.id} with extracted details from invoice")
                except Exception as e:
                    logger.warning(f"Failed to check/update temporary customer: {e}")
            else:
                # No pre-selected customer - require minimum customer info and check for existing customers
                if not customer_name:
                    return JsonResponse({'success': False, 'message': 'Customer name is required'})

                # Map organization fields from either naming convention
                org_name = (request.POST.get('organization_name') or request.POST.get('customer_organization_name') or '').strip() or None
                tax_num = (request.POST.get('tax_number') or request.POST.get('customer_tax_number') or '').strip() or None

                # Use centralized service which does proper deduplication
                try:
                    # If phone missing, try to find by name first within branch
                    if not customer_phone:
                        customer_obj = CustomerService.find_customer_by_name_only(user_branch, customer_name)
                        created = False
                        if not customer_obj:
                            return JsonResponse({'success': False, 'message': 'Customer phone is required to create a new customer'})
                        # If found, update contact information
                        updated = False
                        if customer_email and (not customer_obj.email or customer_obj.email != customer_email):
                            customer_obj.email = customer_email; updated = True
                        if customer_address and (not customer_obj.address or customer_obj.address != customer_address):
                            customer_obj.address = customer_address; updated = True
                        if customer_type and (not customer_obj.customer_type or customer_obj.customer_type != customer_type):
                            customer_obj.customer_type = customer_type; updated = True
                        if org_name and (not customer_obj.organization_name or customer_obj.organization_name != org_name):
                            customer_obj.organization_name = org_name; updated = True
                        if tax_num and (not customer_obj.tax_number or customer_obj.tax_number != tax_num):
                            customer_obj.tax_number = tax_num; updated = True
                        if updated:
                            customer_obj.save()
                        logger.info(f"Found existing customer by name for invoice upload: {customer_obj.id} - {customer_name}")
                    else:
                        # Phone is provided - check for existing customer with this phone first
                        existing_by_phone = Customer.objects.filter(
                            phone=customer_phone,
                            branch=user_branch
                        ).first()

                        if existing_by_phone:
                            # Use existing customer, update details if needed
                            customer_obj = existing_by_phone
                            created = False
                            updated = False

                            # Update customer details if provided and different
                            if customer_name and customer_obj.full_name != customer_name:
                                customer_obj.full_name = customer_name
                                updated = True
                            if customer_email and (not customer_obj.email or customer_obj.email != customer_email):
                                customer_obj.email = customer_email
                                updated = True
                            if customer_address and (not customer_obj.address or customer_obj.address != customer_address):
                                customer_obj.address = customer_address
                                updated = True
                            if customer_type and (not customer_obj.customer_type or customer_obj.customer_type != customer_type):
                                customer_obj.customer_type = customer_type
                                updated = True
                            if org_name and (not customer_obj.organization_name or customer_obj.organization_name != org_name):
                                customer_obj.organization_name = org_name
                                updated = True
                            if tax_num and (not customer_obj.tax_number or customer_obj.tax_number != tax_num):
                                customer_obj.tax_number = tax_num
                                updated = True

                            if updated:
                                customer_obj.save()

                            logger.info(f"Found existing customer by phone for invoice upload: {customer_obj.id} - {customer_name}")
                        else:
                            # No existing customer found, create a new one
                            customer_obj, created = CustomerService.create_or_get_customer(
                                branch=user_branch,
                                full_name=customer_name,
                                phone=customer_phone,
                                email=customer_email,
                                address=customer_address,
                                customer_type=customer_type,
                                organization_name=org_name,
                                tax_number=tax_num,
                                create_if_missing=True
                            )

                            if created:
                                logger.info(f"Created new customer from invoice upload: {customer_obj.id} - {customer_name}")

                    if not customer_obj:
                        return JsonResponse({
                            'success': False,
                            'message': 'Failed to create or find customer'
                        })

                except Exception as e:
                    logger.error(f"Error in customer creation/lookup for invoice: {e}")
                    return JsonResponse({
                        'success': False,
                        'message': f'Error processing customer: {str(e)}'
                    })

            # Update customer code with extracted Code No if available
            # BRANCH SCOPE ENFORCEMENT: Only update code if it's unique within the customer's branch
            extracted_code_no = request.POST.get('code_no', '').strip()
            if extracted_code_no and customer_obj:
                try:
                    # Check if extracted code_no is different from the current code
                    if customer_obj.code != extracted_code_no:
                        # IMPORTANT: Check code uniqueness only within the customer's branch
                        # This prevents cross-branch code conflicts
                        code_query = Customer.objects.filter(code=extracted_code_no).exclude(id=customer_obj.id)
                        if customer_obj.branch:
                            code_query = code_query.filter(branch=customer_obj.branch)
                        existing_customer = code_query.first()

                        if not existing_customer:
                            old_code = customer_obj.code
                            customer_obj.code = extracted_code_no
                            customer_obj.save(update_fields=['code'])
                            logger.info(f"Updated customer {customer_obj.id} code from {old_code} to {extracted_code_no} in branch {customer_obj.branch}")
                        else:
                            logger.warning(f"Code {extracted_code_no} already used by another customer {existing_customer.id} in branch {customer_obj.branch}, keeping current code")
                except Exception as e:
                    logger.warning(f"Failed to update customer code with extracted code_no: {e}")

            # Extract plate from reference if not explicitly provided
            # The reference field from invoice may contain the vehicle plate number
            if not plate:
                reference = request.POST.get('reference', '').strip().upper()
                if reference:
                    # Remove 'FOR' prefix if present (common in invoices like "FOR T 290 EJF")
                    cleaned_ref = reference
                    if cleaned_ref.startswith('FOR '):
                        cleaned_ref = cleaned_ref[4:].strip()
                    elif cleaned_ref.startswith('FOR'):
                        cleaned_ref = cleaned_ref[3:].strip()

                    # Check if cleaned reference looks like a plate number
                    # Typical format: 2-3 letters + 3-4 digits (e.g., ABC123, T123ABC)
                    if re.match(r'^[A-Z]{1,3}\s*-?\s*\d{1,4}[A-Z]?$', cleaned_ref) or \
                       re.match(r'^[A-Z]{1,3}\d{3,4}$', cleaned_ref) or \
                       re.match(r'^\d{1,4}[A-Z]{2,3}$', cleaned_ref) or \
                       re.match(r'^[A-Z]\s*\d{1,4}\s*[A-Z]{2,3}$', cleaned_ref):
                        plate = cleaned_ref.replace('-', '').replace(' ', '')
                        logger.info(f"Extracted vehicle plate from reference field: {plate} (original: {reference})")

            # Get or create vehicle if plate provided
            # The plate number is extracted from the invoice Reference field
            # and is used to track which vehicles visited the service center
            vehicle = None
            if plate:
                try:
                    vehicle = VehicleService.create_or_get_vehicle(customer=customer_obj, plate_number=plate)
                    logger.info(f"Vehicle linked to customer {customer_obj.id}: {plate}")
                except Exception as e:
                    logger.warning(f"Failed to create/get vehicle for customer {customer_obj.id}: {e}")
                    vehicle = None

            # Get existing started order if provided or auto-match by plate
            selected_order_id = request.POST.get('selected_order_id')
            order = None

            # Priority 1: Use explicitly selected order
            if selected_order_id:
                try:
                    is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
                    if user_branch and not is_admin:
                        order = Order.objects.get(id=int(selected_order_id), branch=user_branch)
                    else:
                        order = Order.objects.get(id=int(selected_order_id))
                    logger.info(f"Found existing order {order.id} to update")
                except Exception as e:
                    logger.warning(f"Could not find existing order {selected_order_id}: {e}")
                    pass

            # Priority 2: Auto-match started order by plate number if no order selected
            # This handles the case where user starts an order with a plate, then uploads invoice with same plate
            if not order and vehicle:
                try:
                    # Look for started (created) service orders with the same vehicle
                    is_admin = getattr(request.user, 'is_superuser', False) or getattr(request.user, 'is_staff', False)
                    query = Order.objects.filter(
                        vehicle=vehicle,
                        customer=customer_obj,
                        status='created'  # Only match unstarted orders
                    ).order_by('-created_at')

                    if user_branch and not is_admin:
                        query = query.filter(branch=user_branch)

                    order = query.first()
                    if order:
                        logger.info(f"Auto-matched invoice to started order {order.id} by plate number {plate}")
                except Exception as e:
                    logger.warning(f"Failed to auto-match order by plate number: {e}")

            logger.info(f"Detected order type from codes: {detected_order_type}, categories: {categories}")

            # If no existing order, create new one with detected type
            if not order:
                try:
                    order = OrderService.create_order(
                        customer=customer_obj,
                        order_type=detected_order_type,
                        branch=user_branch,
                        vehicle=vehicle,
                        description='Created from invoice upload'
                    )
                    logger.info(f"Created new order {order.id} for customer {customer_obj.id}")
                except Exception as e:
                    logger.error(f"Failed to create order for customer {customer_obj.id}: {e}")
                    return JsonResponse({
                        'success': False,
                        'message': f'Failed to create order: {str(e)}'
                    })
            else:
                # Update existing started order to ensure it's linked to the correct customer
                if order.customer_id != customer_obj.id:
                    order.customer = customer_obj
                    logger.info(f"Updated order {order.id} customer from {order.customer_id} to {customer_obj.id}")
                if vehicle and order.vehicle_id != vehicle.id:
                    order.vehicle = vehicle
                    logger.info(f"Updated order {order.id} vehicle to {vehicle.id}")
                _save_with_retry(order, update_fields=['customer', 'vehicle'] if vehicle else ['customer'])

                # IMPORTANT: Update customer visit tracking when reusing an existing order
                # This ensures visit count is incremented even when linking to an existing order on a new day
                try:
                    from .services import CustomerService
                    CustomerService.update_customer_visit(customer_obj)
                except Exception as e:
                    logger.warning(f"Failed to update customer visit when reusing order: {e}")
            
            # Create new invoice for this upload
            posted_inv_number = (request.POST.get('invoice_number') or '').strip()

            # Check if an invoice with this number already exists
            existing_invoice = None
            if posted_inv_number:
                try:
                    existing_invoice = Invoice.objects.get(invoice_number=posted_inv_number)
                except Invoice.DoesNotExist:
                    existing_invoice = None

            # Decide whether to update existing invoice or create new one
            if existing_invoice:
                # Invoice with this number already exists
                if existing_invoice.order_id == order.id if order else False:
                    # Same order - update the existing invoice with new data
                    inv = existing_invoice
                    logger.info(f"Updating existing invoice {existing_invoice.id} with number {posted_inv_number} for order {order.id}")
                else:
                    # Different order or no order - generate a new unique invoice number
                    inv = Invoice()
                    # Generate unique number instead of using duplicate
                    inv.generate_invoice_number()
                    logger.warning(f"Invoice number {posted_inv_number} already exists for different order. Generated new number: {inv.invoice_number}")
            else:
                # No existing invoice - create new one
                inv = Invoice()

            inv.branch = order.branch if order and getattr(order, 'branch', None) else user_branch
            inv.order = order
            inv.customer = customer_obj
            try:
                linked_vehicle = vehicle or (order.vehicle if order and getattr(order, 'vehicle', None) else None)
            except Exception:
                linked_vehicle = vehicle
            inv.vehicle = linked_vehicle

            # Ensure order's vehicle is updated if invoice has a vehicle
            # This ensures consistency between invoice and order vehicle tracking
            if linked_vehicle and order and not order.vehicle_id:
                order.vehicle = linked_vehicle
                logger.info(f"Updated order {order.id} vehicle to {linked_vehicle.id} from invoice vehicle")
                _save_with_retry(order, update_fields=['vehicle'])

            # Parse invoice date
            invoice_date_str = request.POST.get('invoice_date', '')
            try:
                inv.invoice_date = datetime.strptime(invoice_date_str, '%Y-%m-%d').date() if invoice_date_str else timezone.localdate()
            except Exception:
                inv.invoice_date = timezone.localdate()

            # Set invoice fields
            inv.code_no = request.POST.get('code_no', '').strip() or None
            inv.reference = request.POST.get('reference', '').strip() or None

            if not inv.vehicle and inv.reference:
                try:
                    _s = str(inv.reference).strip().upper()
                    if _s.startswith('FOR '):
                        _s = _s[4:].strip()
                    elif _s.startswith('FOR'):
                        _s = _s[3:].strip()
                    if re.match(r'^[A-Z]{1,3}\s*-?\s*\d{1,4}[A-Z]?$', _s) or \
                       re.match(r'^[A-Z]{1,3}\d{3,4}$', _s) or \
                       re.match(r'^\d{1,4}[A-Z]{2,3}$', _s) or \
                       re.match(r'^[A-Z]\s*\d{1,4}\s*[A-Z]{2,3}$', _s):
                        _plate = _s.replace('-', '').replace(' ', '')
                        try:
                            _veh = VehicleService.create_or_get_vehicle(customer=customer_obj, plate_number=_plate)
                        except Exception:
                            _veh = None
                        if _veh:
                            inv.vehicle = _veh
                            if order and not order.vehicle_id:
                                order.vehicle = _veh
                                # Save order with vehicle (function-level retry will handle locks)
                                order.save(update_fields=['vehicle'])
                except Exception:
                    pass

            # Collect all notes/remarks
            notes_parts = []
            if request.POST.get('notes', '').strip():
                notes_parts.append(request.POST.get('notes', '').strip())
            if request.POST.get('remarks', '').strip():
                notes_parts.append(request.POST.get('remarks', '').strip())
            if request.POST.get('delivery_terms', '').strip():
                notes_parts.append(f"Delivery: {request.POST.get('delivery_terms', '').strip()}")
            inv.notes = ' | '.join(notes_parts) if notes_parts else ''

            # Set additional fields
            inv.attended_by = request.POST.get('attended_by', '').strip() or None
            inv.kind_attention = request.POST.get('kind_attention', '').strip() or None
            inv.remarks = request.POST.get('remarks', '').strip() or None

            # Seller information (if provided via POST from extraction preview)
            inv.seller_name = (request.POST.get('seller_name') or '').strip() or None
            inv.seller_address = (request.POST.get('seller_address') or '').strip() or None
            inv.seller_phone = (request.POST.get('seller_phone') or '').strip() or None
            inv.seller_email = (request.POST.get('seller_email') or '').strip() or None
            inv.seller_tax_id = (request.POST.get('seller_tax_id') or '').strip() or None
            inv.seller_vat_reg = (request.POST.get('seller_vat_reg') or '').strip() or None

            # Parse amounts (support multiple possible field names)
            def _dec(val):
                s = str(val or '0')
                try:
                    return Decimal(s.replace(',', ''))
                except Exception:
                    return Decimal('0')

            subtotal = _dec(request.POST.get('subtotal') or request.POST.get('net_value'))
            tax_amount = _dec(request.POST.get('tax_amount') or request.POST.get('tax') or request.POST.get('vat'))
            total_amount = _dec(request.POST.get('total_amount') or request.POST.get('total') or request.POST.get('gross_value'))
            tax_rate = _dec(request.POST.get('tax_rate'))

            inv.subtotal = subtotal
            inv.tax_amount = tax_amount
            inv.tax_rate = tax_rate
            inv.total_amount = total_amount or (subtotal + tax_amount)
            inv.created_by = request.user

            # Handle salesperson assignment (for sales invoices)
            salesperson_id = request.POST.get('salesperson_id')
            if salesperson_id:
                try:
                    salesperson = Salesperson.objects.get(id=int(salesperson_id))
                    inv.salesperson = salesperson
                    logger.info(f"Assigned salesperson {salesperson.code} - {salesperson.name} to invoice")
                except (Salesperson.DoesNotExist, ValueError):
                    logger.warning(f"Invalid salesperson_id: {salesperson_id}, using default")
                    inv.salesperson = Salesperson.get_default()
            else:
                # Use default salesperson if not provided
                inv.salesperson = Salesperson.get_default()

            # Set invoice_number if not already set (for newly created invoices)
            if not getattr(inv, 'invoice_number', None) or not inv.invoice_number:
                if posted_inv_number and not existing_invoice:
                    inv.invoice_number = posted_inv_number
                else:
                    inv.generate_invoice_number()

            # Save invoice (function-level retry will handle database locks)
            inv.save()

            # Save uploaded document if provided (optional in two-step flow)
            try:
                uploaded_file = request.FILES.get('file')
                if uploaded_file:
                    from django.core.files.base import ContentFile
                    try:
                        uploaded_file.seek(0)
                        bytes_ = uploaded_file.read()
                    except Exception:
                        bytes_ = None
                    if bytes_:
                        filename = uploaded_file.name or f"invoice_{inv.invoice_number}.pdf"
                        inv.document.save(filename, ContentFile(bytes_), save=True)
            except Exception:
                # Non-fatal
                pass

            # Create line items with extracted fields
            item_descriptions = request.POST.getlist('item_description[]')
            item_qtys = request.POST.getlist('item_qty[]')
            item_prices = request.POST.getlist('item_price[]')
            item_codes = request.POST.getlist('item_code[]')
            item_units = request.POST.getlist('item_unit[]')
            item_values = request.POST.getlist('item_value[]')

            # Get order type mapping for codes
            code_order_types = _get_item_code_categories(item_codes)

            # Create line items directly from extracted data (no aggregation to preserve extracted values)
            try:
                try:
                    if inv and getattr(inv, 'id', None):
                        from tracker.models import InvoiceLineItem as _InvItem
                        _InvItem.objects.filter(invoice=inv).delete()
                except Exception:
                    pass
                to_create = []
                seen_keys = set()
                for idx, desc in enumerate(item_descriptions):
                    if not desc or not desc.strip():
                        continue
                    try:
                        code = item_codes[idx].strip() if idx < len(item_codes) and item_codes[idx] else None
                        unit = item_units[idx].strip() if idx < len(item_units) and item_units[idx] else None

                        # Parse quantity
                        qty_val = item_qtys[idx] if idx < len(item_qtys) else 1
                        try:
                            qty = Decimal(str(qty_val or 1).replace(',', ''))
                        except Exception:
                            qty = Decimal('1')

                        # Parse unit price
                        price_val = item_prices[idx] if idx < len(item_prices) else Decimal('0')
                        try:
                            unit_price = Decimal(str(price_val or '0').replace(',', ''))
                        except Exception:
                            unit_price = Decimal('0')

                        # Use extracted line value directly (critical: NO RECALCULATION)
                        # This preserves the actual invoice data as extracted from the PDF
                        extracted_value = item_values[idx] if idx < len(item_values) else None
                        try:
                            line_total = Decimal(str(extracted_value or '0').replace(',', '')) if extracted_value else (qty * unit_price)
                        except Exception:
                            line_total = qty * unit_price

                        # Get order type for this item from code mapping
                        item_order_type = 'unknown'
                        if code and code in code_order_types:
                            item_order_type = code_order_types[code].get('order_type', 'unknown')
                        elif code:
                            item_order_type = 'unknown'
                        else:
                            item_order_type = 'unknown'

                        key = (
                            (code or ''),
                            desc.strip().lower(),
                            (unit or ''),
                            str(qty),
                            str(unit_price),
                            str(line_total)
                        )
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)

                        # Create line item with salesperson if it's a sales item
                        line_item_salesperson = None
                        if item_order_type == 'sales' and inv.salesperson:
                            line_item_salesperson = inv.salesperson

                        to_create.append(InvoiceLineItem(
                            invoice=inv,
                            code=code,
                            description=desc.strip(),
                            quantity=qty,
                            unit=unit,
                            unit_price=unit_price,
                            tax_rate=Decimal('0'),
                            line_total=line_total,
                            tax_amount=Decimal('0'),
                            order_type=item_order_type,
                            salesperson=line_item_salesperson,
                        ))
                    except Exception as e:
                        logger.warning(f"Failed to process line item {idx}: {e}")
                        continue

                if to_create:
                    InvoiceLineItem.objects.bulk_create(to_create)
                    logger.info(f"Created {len(to_create)} line items from extracted data with preserved values and order types")
            except Exception as e:
                logger.warning(f"Failed to bulk create line items: {e}")

            # IMPORTANT: Preserve extracted Net, VAT, Gross values for uploaded invoices
            # If extracted subtotal is missing/zero but we have line items, compute from items
            try:
                has_items = inv.line_items.exists()
            except Exception:
                has_items = False

            # Ensure subtotal is set from line items if missing/zero
            # This prevents NET REVENUE from showing 0 when line items exist
            if has_items and (inv.subtotal is None or inv.subtotal == Decimal('0')):
                line_items_subtotal = sum(
                    Decimal(str(item.line_total)) for item in inv.line_items.all()
                )
                if line_items_subtotal > 0:
                    inv.subtotal = line_items_subtotal

                    # If tax_amount wasn't extracted, calculate from line item taxes
                    if inv.tax_amount is None or inv.tax_amount == Decimal('0'):
                        per_item_tax = sum(
                            Decimal(str(item.tax_amount or 0)) for item in inv.line_items.all()
                        )
                        inv.tax_amount = per_item_tax

                    # Ensure total_amount is correct
                    if inv.total_amount is None or inv.total_amount == Decimal('0'):
                        inv.total_amount = inv.subtotal + (inv.tax_amount or Decimal('0'))

                    logger.info(f"Calculated invoice totals from line items: subtotal={inv.subtotal}, tax={inv.tax_amount}, total={inv.total_amount}")

            inv.save(update_fields=['subtotal', 'tax_amount', 'total_amount'])

            # Update order type aggregating categories from ALL linked invoices (primary + additional)
            if order:
                try:
                    from tracker.models import Invoice as _Inv, InvoiceLineItem as _InvItem
                    try:
                        invs_for_order = list(_Inv.objects.filter(order=order))
                    except Exception:
                        invs_for_order = []

                    agg_types = set()
                    agg_categories = set()

                    if invs_for_order:
                        try:
                            from tracker.utils.order_type_detector import determine_order_type_from_codes
                        except Exception:
                            determine_order_type_from_codes = None

                        for inv_obj in invs_for_order:
                            try:
                                codes = list(_InvItem.objects.filter(invoice=inv_obj).values_list('code', flat=True))
                                codes = sorted(set([c for c in codes if c]))
                            except Exception:
                                codes = []

                            inv_type = None
                            inv_categories = []
                            if codes and determine_order_type_from_codes:
                                try:
                                    inv_type, inv_categories, _ = determine_order_type_from_codes(codes)
                                except Exception:
                                    inv_type, inv_categories = None, []
                            elif determine_order_type_from_codes:
                                try:
                                    inv_type, inv_categories, _ = determine_order_type_from_codes([])
                                except Exception:
                                    inv_type, inv_categories = None, []

                            if inv_type:
                                if inv_categories:
                                    try:
                                        from tracker.utils.order_type_detector import _normalize_category_to_order_type as _norm
                                    except Exception:
                                        _norm = None
                                    for cat in inv_categories:
                                        if cat == 'sales':
                                            agg_types.add('sales')
                                        elif _norm:
                                            try:
                                                agg_types.add(_norm(cat))
                                            except Exception:
                                                pass
                                else:
                                    agg_types.add(inv_type)
                            for cat in (inv_categories or []):
                                agg_categories.add(cat)

                    if not agg_types:
                        if detected_order_type:
                            agg_types.add(detected_order_type)
                        for cat in (categories or []):
                            agg_categories.add(cat)

                    final_type = None
                    if len(agg_types) == 0:
                        final_type = 'sales'
                    elif len(agg_types) == 1:
                        final_type = list(agg_types)[0]
                    else:
                        final_type = 'mixed'

                    final_categories = sorted(list(agg_categories)) if agg_categories else []

                    order.type = final_type
                    order.mixed_categories = json.dumps(final_categories) if final_type == 'mixed' and final_categories else None
                    order.save(update_fields=['type', 'mixed_categories'])
                    logger.info(f"Updated order {order.id} aggregated type to {final_type}, categories: {final_categories}")
                except Exception as e:
                    logger.warning(f"Failed to aggregate order type from linked invoices: {e}")

            # Create payment record if total > 0
            if inv.total_amount > 0:
                try:
                    # Map extracted payment method or use form value or default
                    extracted_method = request.POST.get('payment_method', '').strip().lower() or 'on_delivery'
                    payment_method_map = {
                        'cash': 'cash',
                        'cheque': 'cheque',
                        'chq': 'cheque',
                        'bank': 'bank_transfer',
                        'transfer': 'bank_transfer',
                        'card': 'card',
                        'mpesa': 'mpesa',
                        'credit': 'on_credit',
                        'delivery': 'on_delivery',
                        'cod': 'on_delivery',
                        'on_delivery': 'on_delivery',
                    }

                    # Try to match the extracted method to a valid choice
                    payment_method = 'on_delivery'  # Default
                    for key, val in payment_method_map.items():
                        if key in extracted_method:
                            payment_method = val
                            break

                    # Use get_or_create to handle existing payment records
                    payment, created = InvoicePayment.objects.get_or_create(
                        invoice=inv,
                        defaults={
                            'amount': Decimal('0'),  # Default to unpaid (amount 0)
                            'payment_method': payment_method,
                            'payment_date': None,
                            'reference': None,
                        }
                    )

                    if created:
                        logger.info(f"Created payment record for invoice {inv.id}")
                    else:
                        logger.info(f"Payment record already exists for invoice {inv.id}, using existing record")
                except Exception as e:
                    logger.warning(f"Failed to create or get payment record: {e}")
            
            # Update started order with invoice data
            try:
                order = OrderService.update_order_from_invoice(
                    order=order,
                    customer=customer_obj,
                    vehicle=vehicle,
                    description=order.description
                )
            except Exception as e:
                logger.warning(f"Failed to update order from invoice: {e}")

            # Handle additional order types/components
            try:
                additional_order_types_json = request.POST.get('additional_order_types', '[]')
                additional_order_types = json.loads(additional_order_types_json) if additional_order_types_json else []

                if additional_order_types:
                    from .models import OrderComponent

                    for component_data in additional_order_types:
                        component_type = component_data.get('type', '').strip()
                        reason = component_data.get('reason', '').strip()

                        # Validate component type
                        if component_type not in ['service', 'sales']:
                            logger.warning(f"Invalid component type: {component_type}")
                            continue

                        # Check if component already exists
                        component, created = OrderComponent.objects.get_or_create(
                            order=order,
                            type=component_type,
                            defaults={
                                'added_by': request.user,
                                'reason': reason,
                                'invoice': inv
                            }
                        )

                        # If component already existed, update it with new reason if provided
                        if not created and reason:
                            component.reason = reason
                            component.invoice = inv
                            component.save(update_fields=['reason', 'invoice'])
                        elif created:
                            logger.info(f"Created OrderComponent for order {order.id}: type={component_type}")
                        else:
                            logger.info(f"OrderComponent already exists for order {order.id}: type={component_type}")

            except ValueError as e:
                logger.warning(f"Failed to parse additional_order_types JSON: {e}")
            except Exception as e:
                logger.warning(f"Failed to create order components: {e}")

            # Determine if customer was found (pre-selected) or created
            customer_found = bool(customer_id)  # If pre-selected customer_id was provided, customer was found

            # Determine redirect based on whether customer was found
            # If customer was found/existing, redirect to regular order detail page
            # If customer was created new, redirect to started order detail page
            if customer_found:
                redirect_url = f'/orders/{order.id}/'
            else:
                redirect_url = f'/orders/started/{order.id}/'

            # Response - redirect to appropriate order detail page
            return JsonResponse({
                'success': True,
                'message': 'Invoice created and order updated successfully',
                'invoice_id': inv.id,
                'invoice_number': inv.invoice_number,
                'order_id': order.id,
                'customer_id': customer_obj.id,
                'customer_found': customer_found,
                'redirect_url': redirect_url
            })
    
    except Exception as e:
        logger.error(f"Error creating invoice from upload: {e}", exc_info=True)
        return JsonResponse({
            'success': False,
            'message': f'Error: {str(e)}'
        })
