"""
Views for invoice creation, management, and printing.
"""

import json
import logging
from decimal import Decimal
from datetime import datetime
from django.utils import timezone

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse, HttpResponse
from django.views.decorators.http import require_http_methods
from django.db import transaction

from .models import Invoice, InvoiceLineItem, InvoicePayment, Order, Customer, Vehicle, InventoryItem
from .forms import InvoiceLineItemForm, InvoicePaymentForm
from .utils import get_user_branch
from .services import OrderService, CustomerService, VehicleService

logger = logging.getLogger(__name__)


@login_required
@require_http_methods(["GET"])
def invoice_upload(request):
    """Display invoice upload page"""
    return render(request, 'tracker/invoice_upload.html', {})


@login_required
@require_http_methods(["GET"])
def api_search_started_orders(request):
    """
    API endpoint to search for started orders by vehicle plate number.
    Used for autocomplete/dropdown in invoice creation form.

    Query parameters:
    - plate: vehicle plate number (required)

    Returns JSON with list of available started orders
    """
    from django.http import JsonResponse

    plate = (request.GET.get('plate') or '').strip().upper()
    if not plate:
        return JsonResponse({'success': False, 'message': 'Plate number required', 'orders': []})

    try:
        user_branch = get_user_branch(request.user)
        orders = OrderService.find_all_started_orders_for_plate(user_branch, plate)

        orders_data = []
        for order in orders:
            orders_data.append({
                'id': order.id,
                'order_number': order.order_number or f"ORD{order.id}",
                'plate_number': order.vehicle.plate_number if order.vehicle else plate,
                'customer': {
                    'id': order.customer.id,
                    'name': order.customer.full_name,
                    'phone': order.customer.phone
                } if order.customer else None,
                'started_at': order.started_at.isoformat() if order.started_at else order.created_at.isoformat(),
                'type': order.type,
                'status': order.status
            })

        return JsonResponse({
            'success': True,
            'orders': orders_data,
            'count': len(orders_data)
        })
    except Exception as e:
        logger.warning(f"Error searching started orders by plate: {e}")
        return JsonResponse({'success': False, 'message': str(e), 'orders': []})


@login_required
@require_http_methods(["POST"])
def api_upload_extract_invoice(request):
    """
    Upload an invoice file and extract structured data.

    Default is PREVIEW-ONLY (no records created). Send commit=true to persist
    and link to a started order.

    Optional POST fields:
      - selected_order_id: to link to an existing started order (when commit=true)
      - plate: plate number to match started order or create temp customer (when commit=true)
      - commit: 'true' to create Invoice + Items; otherwise only preview is returned.

    When commit=true:
      - Links to an existing started order when possible, otherwise creates a new order for real customers.
      - Preserves extracted Net (subtotal), VAT (tax_amount) and Gross (total_amount). If no items were parsed,
        totals are kept as-is to ensure KPIs sum correctly.
    """
    from tracker.utils.invoice_extractor import extract_from_bytes
    import traceback

    user_branch = get_user_branch(request.user)

    # Validate upload
    uploaded = request.FILES.get('file')
    if not uploaded:
        return JsonResponse({'success': False, 'message': 'No file uploaded'})

    try:
        file_bytes = uploaded.read()
    except Exception as e:
        logger.error(f"Failed to read uploaded file: {e}")
        return JsonResponse({'success': False, 'message': 'Failed to read uploaded file'})

    # Run PDF text extractor (no OCR required)
    try:
        from tracker.utils.pdf_text_extractor import extract_from_bytes as extract_pdf_text
        extracted = extract_pdf_text(file_bytes, uploaded.name if uploaded else 'document.pdf')
    except Exception as e:
        logger.error(f"PDF extraction error: {e}\n{traceback.format_exc()}")
        return JsonResponse({
            'success': False,
            'message': 'Failed to extract invoice data from file',
            'error': str(e),
            'ocr_available': False
        })

    # If extraction failed, return error but allow manual entry
    if not extracted.get('success'):
        return JsonResponse({
            'success': False,
            'message': extracted.get('message', 'Could not extract data from file. Please enter invoice details manually.'),
            'error': extracted.get('error'),
            'ocr_available': extracted.get('ocr_available', False),
            'data': extracted  # Include any partial data for manual completion
        })

    header = extracted.get('header') or {}
    items = extracted.get('items') or []
    raw_text = extracted.get('raw_text') or ''

    # If commit flag not provided, return preview only
    commit = str(request.POST.get('commit', '')).lower() == 'true'
    if not commit:
        # Enrich items with category information for preview
        from tracker.views_invoice_upload import _get_item_code_categories

        item_codes = [item.get('code') for item in items if item.get('code')]
        code_categories = _get_item_code_categories(item_codes)

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
            'mode': 'preview',
            'header': header,
            'items': enriched_items,
            'raw_text': raw_text,
            'ocr_available': extracted.get('ocr_available', False)
        })

    # Get identifiers from POST (commit path only)
    selected_order_id = request.POST.get('selected_order_id') or None
    plate = (request.POST.get('plate') or '').strip().upper() or None
    customer_id = request.POST.get('customer_id') or None

    # Try to load the selected order first
    selected_order = None
    if selected_order_id:
        try:
            selected_order = Order.objects.get(id=int(selected_order_id), branch=user_branch)
        except Exception as e:
            logger.warning(f"Selected order {selected_order_id} not found: {e}")
            selected_order = None

    # If no selected_order but plate provided, find started order
    if not selected_order and plate:
        try:
            selected_order = OrderService.find_started_order_by_plate(user_branch, plate)
        except Exception as e:
            logger.warning(f"Could not find started order for plate {plate}: {e}")
            selected_order = None

    # Determine customer to use
    customer_obj = None

    # Priority 1: Use explicit customer_id if provided
    if customer_id and not customer_obj:
        try:
            customer_obj = Customer.objects.get(id=int(customer_id), branch=user_branch)
        except Exception:
            customer_obj = None

    # Priority 2: Use customer from selected order if available
    if selected_order and selected_order.customer:
        customer_obj = selected_order.customer

    # Priority 3: Try to create/find customer using extracted data
    if not customer_obj:
        cust_name = (header.get('customer_name') or '').strip()
        cust_phone = (header.get('phone') or '').strip()

        # Prefer composite identifier (name + plate) when available
        if cust_name and plate:
            try:
                composite = CustomerService.find_customer_by_name_and_plate(
                    branch=user_branch,
                    full_name=cust_name,
                    plate_number=plate,
                )
                if composite:
                    customer_obj = composite
            except Exception as e:
                logger.warning(f"Composite name+plate lookup failed: {e}")

        if not customer_obj and cust_name and cust_phone:
            try:
                # Try to find existing customer with extracted name and phone
                customer_obj, created = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=cust_name,
                    phone=cust_phone,
                    email=(header.get('email') or '').strip() or None,
                    address=(header.get('address') or '').strip() or None,
                    create_if_missing=True
                )
            except Exception as e:
                logger.warning(f"Failed to create/get customer from extracted data: {e}")
                customer_obj = None
        elif not customer_obj and cust_name:
            # Only name available - use deterministic phone for deduplication
            # This ensures same customer name always maps to same customer record
            try:
                deterministic_phone = f"INVOICE_{cust_name.upper()[:50].replace(' ', '_')}"
                customer_obj, created = CustomerService.create_or_get_customer(
                    branch=user_branch,
                    full_name=cust_name,
                    phone=deterministic_phone,
                    email=(header.get('email') or '').strip() or None,
                    address=(header.get('address') or '').strip() or None,
                    create_if_missing=True
                )
            except Exception as e:
                logger.warning(f"Failed to create/get customer with deterministic phone: {e}")
                customer_obj = None

    # Priority 4: Try to find customer by plate number (via vehicles)
    if not customer_obj and plate:
        try:
            vehicle = Vehicle.objects.filter(
                plate_number__iexact=plate,
                customer__branch=user_branch
            ).select_related('customer').first()
            if vehicle and vehicle.customer:
                customer_obj = vehicle.customer
        except Exception as e:
            logger.warning(f"Failed to find customer by plate {plate}: {e}")
            customer_obj = None

    # If still no customer, return extraction data for manual review
    if not customer_obj:
        logger.warning("No customer found for invoice upload. Extraction data returned for manual review.")
        return JsonResponse({
            'success': False,
            'message': 'Could not identify customer from invoice or provided data. Please enter customer details manually.',
            'data': extracted,
            'ocr_available': extracted.get('ocr_available', False)
        })

    # Ensure vehicle if plate
    vehicle = None
    if plate and customer_obj:
        try:
            vehicle = VehicleService.create_or_get_vehicle(customer=customer_obj, plate_number=plate)
        except Exception as e:
            logger.warning(f"Failed to create/get vehicle for plate {plate}: {e}")
            vehicle = None

    # Create or attach order if needed
    order = selected_order
    if not order and customer_obj:
        try:
            # Only create a new order if this is not a temporary customer
            is_temp = (str(customer_obj.full_name or '').startswith('Plate ') and
                      str(customer_obj.phone or '').startswith('PLATE_'))

            if is_temp:
                # For temp customers, use selected order or create minimal order
                if not order:
                    order = OrderService.create_order(
                        customer=customer_obj,
                        order_type='service',
                        branch=user_branch,
                        vehicle=vehicle,
                        description=f'Auto-created from invoice upload'
                    )
            else:
                # For real customers, use OrderService
                try:
                    order = OrderService.create_order(
                        customer=customer_obj,
                        order_type='service',
                        branch=user_branch,
                        vehicle=vehicle,
                        description=f'Auto-created from invoice upload'
                    )
                except Exception as e:
                    logger.warning(f"Failed to create order from invoice upload: {e}")
                    order = None
        except Exception as e:
            logger.warning(f"Error handling order creation: {e}")
            order = None

    # Create or reuse invoice record (enforce 1 invoice per order)
    try:
        # If an order exists and already has an invoice, reuse it
        inv = None
        if order:
            try:
                inv = Invoice.objects.filter(order=order).first()
            except Exception:
                inv = None
        if inv is None:
            inv = Invoice()
        inv.branch = user_branch
        inv.order = order
        inv.customer = customer_obj
        try:
            inv.vehicle = vehicle or (order.vehicle if order and getattr(order, 'vehicle', None) else None)
        except Exception:
            inv.vehicle = vehicle

        # Parse invoice date
        inv.invoice_date = None
        if header.get('date'):
            # Try parse date in common formats
            for fmt in ("%d/%m/%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%y"):
                try:
                    inv.invoice_date = datetime.strptime(header.get('date'), fmt).date()
                    break
                except Exception:
                    continue
        if not inv.invoice_date:
            inv.invoice_date = timezone.localdate()

        # Set invoice details
        inv.reference = (header.get('reference') or header.get('invoice_no') or header.get('code_no') or '').strip() or f"UPLOAD-{timezone.now().strftime('%Y%m%d%H%M%S')}"
        inv.attended_by = (header.get('attended_by') or '').strip() or None
        inv.kind_attention = (header.get('kind_attention') or '').strip() or None
        inv.remarks = (header.get('remarks') or '').strip() or None
        inv.notes = (header.get('notes') or '').strip() or ''

        # Seller information (do not map seller into customer)
        inv.seller_name = (header.get('seller_name') or '').strip() or None
        inv.seller_address = (header.get('seller_address') or '').strip() or None
        inv.seller_phone = (header.get('seller_phone') or '').strip() or None
        inv.seller_email = (header.get('seller_email') or '').strip() or None
        inv.seller_tax_id = (header.get('seller_tax_id') or '').strip() or None
        inv.seller_vat_reg = (header.get('seller_vat_reg') or '').strip() or None

        # Helper to safely convert extracted values to Decimal
        def ensure_decimal(value, default=Decimal('0')):
            """Convert various types to Decimal safely"""
            if value is None:
                return default
            if isinstance(value, Decimal):
                return value
            try:
                # Handle string values with commas or spaces
                if isinstance(value, str):
                    cleaned = value.strip().replace(',', '').replace(' ', '')
                    if cleaned and cleaned not in ('.', '-'):
                        return Decimal(cleaned)
                else:
                    return Decimal(str(value))
            except (ValueError, TypeError, Exception):
                logger.warning(f"Failed to convert value to Decimal: {value} (type: {type(value).__name__})")
            return default

        # Set monetary fields with proper defaults - convert extracted values to Decimal
        # IMPORTANT: These values come directly from extraction and must be validated
        extracted_subtotal = header.get('subtotal')
        extracted_tax = header.get('tax')
        extracted_total = header.get('total')

        inv.subtotal = ensure_decimal(extracted_subtotal, Decimal('0'))
        inv.tax_amount = ensure_decimal(extracted_tax, Decimal('0'))

        # For total: prefer extracted value, fallback to calculated (subtotal + tax)
        if extracted_total is not None:
            inv.total_amount = ensure_decimal(extracted_total, None)
            if inv.total_amount is None:
                inv.total_amount = inv.subtotal + inv.tax_amount
        else:
            inv.total_amount = inv.subtotal + inv.tax_amount

        # Set tax rate if extracted (percentage)
        if header.get('tax_rate'):
            try:
                tax_rate_val = header.get('tax_rate')
                if isinstance(tax_rate_val, str):
                    tax_rate_val = Decimal(tax_rate_val.replace('%', '').strip())
                else:
                    tax_rate_val = Decimal(str(tax_rate_val))
                inv.tax_rate = tax_rate_val
            except (ValueError, TypeError):
                inv.tax_rate = Decimal('0')

        # Final validation - ensure all monetary fields are Decimal and not None
        # If subtotal and tax are both 0 but total_amount is set, calculate them from line items
        # This handles cases where extraction couldn't find these values but total was found
        inv.subtotal = inv.subtotal or Decimal('0')
        inv.tax_amount = inv.tax_amount or Decimal('0')
        inv.total_amount = inv.total_amount or (inv.subtotal + inv.tax_amount)

        # Log the extracted amounts for debugging
        logger.info(f"Invoice extraction: subtotal={inv.subtotal}, tax={inv.tax_amount}, total={inv.total_amount}")

        inv.created_by = request.user
        if not getattr(inv, 'invoice_number', None):
            inv.generate_invoice_number()
        inv.save()

        # Persist uploaded document into invoice.document for traceability
        try:
            from django.core.files.base import ContentFile
            filename = (uploaded.name if uploaded and getattr(uploaded, 'name', None) else f"invoice_{inv.invoice_number}.pdf")
            if 'file_bytes' in locals() and file_bytes:
                inv.document.save(filename, ContentFile(file_bytes), save=True)
        except Exception:
            # Non-fatal: continue without blocking invoice creation
            pass

        # Aggregate duplicate line items by code (fallback to description) before creation
        def _aggregate_items(items_list):
            """Aggregate duplicate items by code/description to prevent duplicates.

            Handles cases where the same item appears multiple times in extraction,
            combining quantities and preserving pricing information.
            """
            bucket = {}
            for it in items_list:
                # Normalize description and code
                desc = (it.get('description') or 'Item').strip()
                code = (it.get('item_code') or it.get('code') or '').strip()

                # Create a unique key: prefer code, fallback to normalized description
                # Normalize description by converting to lowercase and removing extra spaces
                desc_normalized = ' '.join(desc.lower().split())
                key = code if code else desc_normalized

                # Parse numeric values safely
                try:
                    qty = Decimal(str(it.get('qty') or 1))
                except (ValueError, TypeError, Exception):
                    qty = Decimal('1')

                unit = (it.get('unit') or '').strip() or None

                # Extract pricing: prefer rate (unit price), fallback to value
                rate = it.get('rate')
                value = it.get('value')

                try:
                    if rate:
                        rate = Decimal(str(rate))
                    else:
                        rate = None
                except (ValueError, TypeError, Exception):
                    rate = None

                try:
                    if value is not None:
                        value = Decimal(str(value))
                    else:
                        value = None
                except (ValueError, TypeError, Exception):
                    value = None

                # Initialize or update bucket entry
                if key not in bucket:
                    bucket[key] = {
                        'code': code or None,
                        'description': desc,
                        'qty': Decimal('0'),
                        'unit': unit,
                        'rates': [],  # Track all rates for averaging
                        'values': []  # Track all values for summing
                    }

                # Accumulate quantities and values
                bucket[key]['qty'] += qty
                if unit and not bucket[key]['unit']:
                    bucket[key]['unit'] = unit
                if rate:
                    bucket[key]['rates'].append(rate)
                if value:
                    bucket[key]['values'].append(value)

            # Build final items list
            out = []
            for v in bucket.values():
                final_qty = v['qty'] if v['qty'] > 0 else Decimal('1')

                # Calculate unit price: prefer average of rates, fallback to calculated from values
                unit_price = Decimal('0')
                if v['rates']:
                    # Average of all provided rates
                    unit_price = sum(v['rates']) / len(v['rates'])
                elif v['values']:
                    # Calculate from total value / quantity
                    total_value = sum(v['values'])
                    unit_price = total_value / final_qty if final_qty > 0 else Decimal('0')

                out.append({
                    'code': v['code'],
                    'description': v['description'],
                    'qty': final_qty,
                    'unit': v['unit'],
                    'unit_price': unit_price,
                })

            return out

        aggregated = _aggregate_items(items) if items else []
        # Get order type mapping for item codes
        item_codes = [it.get('code') for it in aggregated if it.get('code')]
        try:
            from tracker.views_invoice_upload import _get_item_code_categories
            code_order_types = _get_item_code_categories(item_codes)
        except Exception as e:
            logger.warning(f"Failed to get code categories: {e}")
            code_order_types = {}

        # Replace previous items if reusing an existing invoice, then create new ones
        try:
            try:
                if inv and getattr(inv, 'id', None):
                    InvoiceLineItem.objects.filter(invoice=inv).delete()
            except Exception:
                pass
            to_create = []
            for it in aggregated:
                qty = Decimal(str(it.get('qty') or '1'))
                price = Decimal(str(it.get('unit_price') or '0'))
                line_total = qty * price
                code = it.get('code')

                # Determine order_type from code
                order_type = 'unspecified'
                if code and code in code_order_types:
                    order_type = code_order_types[code].get('order_type', 'unspecified')

                to_create.append(InvoiceLineItem(
                    invoice=inv,
                    code=code or None,
                    description=it.get('description') or 'Item',
                    quantity=qty,
                    unit=it.get('unit') or None,
                    unit_price=price,
                    tax_rate=Decimal('0'),
                    line_total=line_total,
                    tax_amount=Decimal('0'),
                    order_type=order_type,
                ))
            if to_create:
                InvoiceLineItem.objects.bulk_create(to_create)
                logger.info(f"Created {len(to_create)} line items from extraction with order types")
        except Exception as e:
            logger.warning(f"Failed to bulk create invoice line items: {e}")

        # IMPORTANT: Preserve extracted Net, VAT, and Gross values for uploaded invoices
        # But if extraction didn't find subtotal/tax, calculate from line items
        extracted_subtotal = header.get('subtotal')
        extracted_tax = header.get('tax')
        extracted_total = header.get('total')

        inv.subtotal = ensure_decimal(extracted_subtotal, None)
        inv.tax_amount = ensure_decimal(extracted_tax, None)
        inv.total_amount = ensure_decimal(extracted_total, None)

        # If subtotal is missing/zero but we have line items, calculate it from them
        # This ensures Net Revenue KPI is never zero when there are actual line items
        if inv.subtotal is None or inv.subtotal == Decimal('0'):
            has_items = inv.id and InvoiceLineItem.objects.filter(invoice=inv).exists()
            if has_items:
                # Calculate subtotal and VAT from line items
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

        # Ensure total_amount is set correctly
        if inv.total_amount is None or inv.total_amount == Decimal('0'):
            inv.total_amount = (inv.subtotal or Decimal('0')) + (inv.tax_amount or Decimal('0'))

        # Final defaults: ensure all are Decimal and not None
        inv.subtotal = inv.subtotal or Decimal('0')
        inv.tax_amount = inv.tax_amount or Decimal('0')
        inv.total_amount = inv.total_amount or (inv.subtotal + inv.tax_amount)

        inv.save(update_fields=['subtotal', 'tax_amount', 'total_amount'])

        # Create payment record for tracking
        if inv.total_amount and inv.total_amount > 0:
            try:
                payment = InvoicePayment()
                payment.invoice = inv
                payment.amount = Decimal('0')  # Default to unpaid
                payment.payment_method = 'on_delivery'  # Default payment method
                payment.save()
            except Exception as e:
                logger.warning(f"Failed to create payment record for uploaded invoice: {e}")

        # If linked to started order, update order with finalized details
        if order:
            try:
                order = OrderService.update_order_from_invoice(
                    order=order,
                    customer=customer_obj,
                    vehicle=vehicle,
                    description=order.description
                )
            except Exception as e:
                logger.warning(f"Failed to update order from invoice: {e}")

        # If we reused an existing invoice for the order, inform the client
        reused_message = 'Invoice created from upload'
        if order:
            try:
                only_this = Invoice.objects.filter(order=order, id=inv.id).exists()
                reused_message = 'Invoice updated/linked to existing order invoice' if only_this else reused_message
            except Exception:
                pass

        # Redirect to order_detail if order exists, otherwise to invoice_detail
        if order:
            redirect_url = request.build_absolute_uri(f'/tracker/orders/{order.id}/')
        else:
            redirect_url = request.build_absolute_uri(f'/tracker/invoices/{inv.id}/')

        return JsonResponse({
            'success': True,
            'message': reused_message,
            'invoice_id': inv.id,
            'invoice_number': inv.invoice_number,
            'redirect_url': redirect_url
        })

    except Exception as e:
        logger.error(f"Error saving invoice from extraction: {e}\n{traceback.format_exc()}")
        return JsonResponse({
            'success': False,
            'message': 'Failed to save invoice',
            'error': str(e)
        })




@login_required
def invoice_detail(request, pk):
    """View invoice details and manage line items/payments"""
    invoice = get_object_or_404(Invoice, pk=pk)

    if request.method == 'POST':
        action = request.POST.get('action')

        if action == 'add_line_item':
            form = InvoiceLineItemForm(request.POST)
            if form.is_valid():
                line_item = form.save(commit=False)
                line_item.invoice = invoice

                # Determine order_type from item code if available
                if line_item.code:
                    try:
                        from tracker.views_invoice_upload import _get_item_code_categories
                        code_categories = _get_item_code_categories([line_item.code])
                        if line_item.code in code_categories:
                            line_item.order_type = code_categories[line_item.code].get('order_type', 'unspecified')
                    except Exception as e:
                        logger.warning(f"Failed to determine order_type for code {line_item.code}: {e}")
                        line_item.order_type = 'unspecified'
                else:
                    line_item.order_type = 'unspecified'

                line_item.save()
                invoice.calculate_totals().save()
                messages.success(request, 'Line item added.')
                return redirect('tracker:invoice_detail', pk=invoice.pk)

        elif action == 'delete_line_item':
            item_id = request.POST.get('item_id')
            try:
                item = InvoiceLineItem.objects.get(id=item_id, invoice=invoice)
                item.delete()
                invoice.calculate_totals().save()
                messages.success(request, 'Line item deleted.')
            except InvoiceLineItem.DoesNotExist:
                messages.error(request, 'Line item not found.')
            return redirect('tracker:invoice_detail', pk=invoice.pk)

        elif action == 'update_payment':
            form = InvoicePaymentForm(request.POST)
            if form.is_valid():
                payment = form.save(commit=False)
                payment.invoice = invoice
                payment.save()
                messages.success(request, 'Payment information updated.')
                return redirect('tracker:invoice_detail', pk=invoice.pk)

    line_item_form = InvoiceLineItemForm()
    payment_form = InvoicePaymentForm()

    return render(request, 'tracker/invoice_detail.html', {
        'invoice': invoice,
        'line_item_form': line_item_form,
        'payment_form': payment_form,
    })


@login_required
def invoice_list(request, order_id=None):
    """List invoices for an order or all invoices"""
    if order_id:
        invoices = Invoice.objects.filter(order_id=order_id)
        order = get_object_or_404(Order, pk=order_id)
        title = f'Invoices for Order {order.order_number}'
    else:
        invoices = Invoice.objects.all()
        order = None
        title = 'All Invoices'
    
    return render(request, 'tracker/invoice_list.html', {
        'invoices': invoices,
        'order': order,
        'title': title,
    })


@login_required
def invoice_print(request, pk):
    """Display invoice in print-friendly format"""
    invoice = get_object_or_404(Invoice, pk=pk)
    context = {
        'invoice': invoice,
    }
    return render(request, 'tracker/invoice_print.html', context)


@login_required
@require_http_methods(["GET","POST"])
def invoice_pdf(request, pk):
    """Generate and download invoice as PDF"""
    invoice = get_object_or_404(Invoice, pk=pk)

    try:
        from django.template.loader import render_to_string
        from weasyprint import HTML, CSS
        import io
        import os

        logo_left_path = os.path.join(os.path.dirname(__file__), '..', 'tracker', 'static', 'assets', 'images', 'logo', 'stm_logo.png')
        logo_right_path = os.path.join(os.path.dirname(__file__), '..', 'tracker', 'static', 'assets', 'images', 'logo', 'wecare.png')

        context = {
            'invoice': invoice,
            'logo_left_url': f'file://{os.path.abspath(logo_left_path)}',
            'logo_right_url': f'file://{os.path.abspath(logo_right_path)}',
        }

        html_string = render_to_string('tracker/invoice_print.html', context)
        html = HTML(string=html_string, base_url=request.build_absolute_uri('/'))
        pdf = html.write_pdf()

        response = HttpResponse(pdf, content_type='application/pdf')
        response['Content-Disposition'] = f'attachment; filename="Invoice_{invoice.invoice_number}.pdf"'
        return response
    except ImportError:
        messages.error(request, 'PDF generation not available. Please install weasyprint.')
        return redirect('tracker:invoice_print', pk=pk)
    except Exception as e:
        logger.error(f"Error generating PDF for invoice {pk}: {e}")
        messages.error(request, 'Error generating PDF.')
        return redirect('tracker:invoice_print', pk=pk)


@login_required
@require_http_methods(["GET"])
def api_inventory_for_invoice(request):
    """API endpoint to fetch inventory items for invoice line items"""
    try:
        items = InventoryItem.objects.select_related('brand').filter(is_active=True).order_by('brand__name', 'name')
        data = []
        for item in items:
            brand_name = item.brand.name if item.brand else 'Unbranded'
            data.append({
                'id': item.id,
                'name': item.name,
                'brand': brand_name,
                'quantity': item.quantity or 0,
                'price': float(item.price or 0),
            })
        return JsonResponse({'items': data})
    except Exception as e:
        logger.error(f"Error fetching inventory items: {e}")


@login_required
@require_http_methods(["GET"])
def api_recent_invoices(request):
    """Return JSON list of recent invoices for sidebar"""
    try:
        from .utils import get_user_branch
        from django.urls import reverse
        branch = get_user_branch(request.user)
        qs = Invoice.objects.select_related('customer').order_by('-invoice_date')
        if branch:
            qs = qs.filter(branch=branch)
        invoices = qs[:8]
        data = []
        for inv in invoices:
            try:
                detail = reverse('tracker:invoice_detail', kwargs={'pk': inv.id})
                prn = reverse('tracker:invoice_print', kwargs={'pk': inv.id})
                pdf = reverse('tracker:invoice_pdf', kwargs={'pk': inv.id})
            except Exception:
                detail = f"/invoices/{inv.id}/"
                prn = f"/invoices/{inv.id}/print/"
                pdf = f"/invoices/{inv.id}/pdf/"
            data.append({
                'id': inv.id,
                'invoice_number': inv.invoice_number,
                'customer_name': inv.customer.full_name if inv.customer else '',
                'total_amount': float(inv.total_amount or 0),
                'status': inv.status,
                'detail_url': detail,
                'print_url': prn,
                'pdf_url': pdf,
            })
        return JsonResponse({'invoices': data})
    except Exception as e:
        logger.error(f"Error fetching recent invoices: {e}")
        return JsonResponse({'invoices': []})


@login_required
@require_http_methods(["POST"])
def invoice_finalize(request, pk):
    """Finalize invoice and change status to issued"""
    invoice = get_object_or_404(Invoice, pk=pk)

    if invoice.status == 'draft':
        if invoice.line_items.count() == 0:
            messages.error(request, 'Invoice must have at least one line item.')
            return redirect('tracker:invoice_detail', pk=pk)

        invoice.status = 'issued'
        invoice.save()
        messages.success(request, f'Invoice {invoice.invoice_number} finalized.')

    return redirect('tracker:invoice_detail', pk=pk)


@login_required
@require_http_methods(["GET"])
def invoice_document_download(request, pk):
    """Download uploaded invoice document"""
    invoice = get_object_or_404(Invoice, pk=pk)

    # Verify user has access to this invoice
    user_branch = get_user_branch(request.user)
    if not request.user.is_superuser:
        if invoice.branch and user_branch and invoice.branch.id != user_branch.id:
            messages.error(request, "You don't have permission to access this invoice.")
            return redirect('tracker:invoice_list')

    if not invoice.document:
        messages.error(request, 'This invoice has no document attached.')
        return redirect('tracker:invoice_detail', pk=pk)

    try:
        # Open the file from storage
        response = HttpResponse(invoice.document.read(), content_type='application/octet-stream')

        # Get the original filename from the document path
        filename = invoice.document.name.split('/')[-1] if invoice.document.name else f'Invoice_{invoice.invoice_number}.pdf'

        response['Content-Disposition'] = f'attachment; filename="{filename}"'
        return response
    except Exception as e:
        logger.error(f"Error downloading invoice document {pk}: {e}")
        messages.error(request, 'Error downloading document.')
        return redirect('tracker:invoice_detail', pk=pk)


@login_required
@require_http_methods(["GET"])
def invoice_document_view(request, pk):
    """View uploaded invoice document inline (for images and PDFs)"""
    invoice = get_object_or_404(Invoice, pk=pk)

    # Verify user has access to this invoice
    user_branch = get_user_branch(request.user)
    if not request.user.is_superuser:
        if invoice.branch and user_branch and invoice.branch.id != user_branch.id:
            messages.error(request, "You don't have permission to access this invoice.")
            return redirect('tracker:invoice_list')

    if not invoice.document:
        messages.error(request, 'This invoice has no document attached.')
        return redirect('tracker:invoice_detail', pk=pk)

    try:
        # Get MIME type based on file extension
        filename = invoice.document.name.lower() if invoice.document.name else ''

        if filename.endswith('.pdf'):
            content_type = 'application/pdf'
        elif filename.endswith(('.jpg', '.jpeg')):
            content_type = 'image/jpeg'
        elif filename.endswith('.png'):
            content_type = 'image/png'
        elif filename.endswith('.gif'):
            content_type = 'image/gif'
        elif filename.endswith('.webp'):
            content_type = 'image/webp'
        else:
            # Default to PDF for unknown types
            content_type = 'application/pdf'

        response = HttpResponse(invoice.document.read(), content_type=content_type)
        response['Content-Disposition'] = 'inline'  # View inline instead of download
        return response
    except Exception as e:
        logger.error(f"Error viewing invoice document {pk}: {e}")
        messages.error(request, 'Error viewing document.')
        return redirect('tracker:invoice_detail', pk=pk)


@login_required
@require_http_methods(["POST"])
def invoice_cancel(request, pk):
    """Cancel an invoice"""
    invoice = get_object_or_404(Invoice, pk=pk)
    
    if invoice.status != 'cancelled':
        invoice.status = 'cancelled'
        invoice.save()
        messages.success(request, f'Invoice {invoice.invoice_number} cancelled.')
    
    return redirect('tracker:invoice_detail', pk=pk)
