from django.db import models
from django.contrib.auth.models import User
from django.utils import timezone
from django.db.models import Q
from datetime import timedelta
from decimal import Decimal
import uuid


class Branch(models.Model):
    """Business branch/location for multi-region scoping."""
    name = models.CharField(max_length=128, unique=True)
    code = models.CharField(max_length=32, unique=True)
    region = models.CharField(max_length=128, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["code"], name="idx_branch_code"),
            models.Index(fields=["region"], name="idx_branch_region"),
        ]

    def __str__(self) -> str:
        r = f" ({self.region})" if self.region else ""
        return f"{self.name}{r}"


class Salesperson(models.Model):
    """Sales personnel for tracking sales transactions and audit purposes."""
    code = models.CharField(max_length=32, unique=True, help_text="Unique salesperson code (e.g., 346, 401)")
    name = models.CharField(max_length=255, help_text="Salesperson name (e.g., Maria Shayo, DCV POS)")
    is_active = models.BooleanField(default=True)
    is_default = models.BooleanField(default=False, help_text="Set as default salesperson for sales without assigned seller")
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ["code"]
        indexes = [
            models.Index(fields=["code"], name="idx_salesperson_code"),
            models.Index(fields=["is_active"], name="idx_salesperson_active"),
            models.Index(fields=["is_default"], name="idx_salesperson_default"),
        ]
        verbose_name = "Salesperson"
        verbose_name_plural = "Salespeople"

    def __str__(self) -> str:
        return f"{self.code} - {self.name}"

    def save(self, *args, **kwargs):
        """Ensure only one default salesperson."""
        if self.is_default:
            Salesperson.objects.filter(is_default=True).exclude(id=self.id).update(is_default=False)
        super().save(*args, **kwargs)

    @classmethod
    def get_default(cls):
        """Get the default salesperson."""
        return cls.objects.filter(is_default=True).first() or cls.objects.filter(code='401').first() or cls.objects.first()


class Customer(models.Model):
    TYPE_CHOICES = [
        ("government", "Government"),
        ("ngo", "NGO"),
        ("company", "Private Company"),
        ("personal", "Personal"),
    ]
    PERSONAL_SUBTYPE = [("owner", "Owner"), ("driver", "Driver")]
    STATUS_CHOICES = [
        ("arrived", "Arrived"),
        ("in_service", "In Service"),
        ("completed", "Completed"),
        ("departed", "Departed"),
    ]

    code = models.CharField(max_length=32, unique=True, editable=False)
    branch = models.ForeignKey('Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='customers')
    full_name = models.CharField(max_length=255)
    phone = models.CharField(max_length=20)
    whatsapp = models.CharField(max_length=20, blank=True, null=True, help_text="WhatsApp number (if different from phone)")
    email = models.EmailField(blank=True, null=True)
    address = models.TextField(blank=True, null=True)

    # keep this as "notes" so your forms work, but mark as deprecated
    notes = models.TextField(
        blank=True,
        null=True,
        help_text='General notes about the customer (deprecated, use CustomerNote model instead)'
    )

    customer_type = models.CharField(max_length=20, choices=TYPE_CHOICES, null=True, blank=True)
    organization_name = models.CharField(max_length=255, blank=True, null=True)
    tax_number = models.CharField(max_length=64, blank=True, null=True)
    personal_subtype = models.CharField(max_length=16, choices=PERSONAL_SUBTYPE, blank=True, null=True)

    registration_date = models.DateTimeField(default=timezone.now)
    arrival_time = models.DateTimeField(blank=True, null=True)
    current_status = models.CharField(max_length=20, choices=STATUS_CHOICES, default="arrived")

    total_visits = models.PositiveIntegerField(default=0)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    last_visit = models.DateTimeField(blank=True, null=True)

    def save(self, *args, **kwargs):
        if not self.code:
            self.code = f"CUST{str(uuid.uuid4())[:8].upper()}"
            while Customer.objects.filter(code=self.code).exists():
                self.code = f"CUST{str(uuid.uuid4())[:8].upper()}"
        if not self.arrival_time:
            self.arrival_time = timezone.now()
        super().save(*args, **kwargs)

    def get_icon_for_customer_type(self):
        """Return appropriate icon class based on customer type"""
        if not self.customer_type:
            return 'user'
        
        icon_map = {
            'government': 'landmark',
            'ngo': 'hands-helping',
            'company': 'building',
            'personal': 'user',
        }
        return icon_map.get(self.customer_type, 'user')
        
    def __str__(self):
        return f"{self.full_name} ({self.code})"

    class Meta:
        indexes = [
            models.Index(fields=["full_name"], name="idx_cust_name"),
            models.Index(fields=["phone"], name="idx_cust_phone"),
            models.Index(fields=["email"], name="idx_cust_email"),
            models.Index(fields=["registration_date"], name="idx_cust_reg"),
            models.Index(fields=["last_visit"], name="idx_cust_lastvisit"),
            models.Index(fields=["customer_type"], name="idx_cust_type"),
        ]
        constraints = [
            models.UniqueConstraint(
                fields=["branch", "full_name", "phone", "organization_name", "tax_number"],
                name="uniq_customer_identity",
            )
        ]


class Vehicle(models.Model):
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="vehicles")
    plate_number = models.CharField(max_length=32)
    make = models.CharField(max_length=64, blank=True, null=True)
    model = models.CharField(max_length=64, blank=True, null=True)
    vehicle_type = models.CharField(max_length=64, blank=True, null=True)

    def __str__(self):
        return f"{self.plate_number} - {self.make or ''} {self.model or ''}"

    class Meta:
        indexes = [
            models.Index(fields=["customer"], name="idx_vehicle_customer"),
            models.Index(fields=["plate_number"], name="idx_vehicle_plate"),
        ]


class LabourCode(models.Model):
    """
    Mapping of item codes to order types/categories.
    Used to automatically determine order type when processing invoices.
    Category values: 'labour', 'service', 'tyre service', 'sales', or 'unspecified'.
    """
    code = models.CharField(max_length=32, unique=True, db_index=True)
    description = models.CharField(max_length=255)
    category = models.CharField(max_length=64, help_text="Order type category: 'labour', 'service', 'tyre service', 'sales', or 'unspecified'")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['code']
        indexes = [
            models.Index(fields=['code'], name='idx_labour_code'),
            models.Index(fields=['category'], name='idx_labour_category'),
            models.Index(fields=['is_active'], name='idx_labour_active'),
        ]

    def __str__(self):
        return f"{self.code} - {self.description} ({self.category})"


class Order(models.Model):
    TYPE_CHOICES = [("service", "Service"), ("sales", "Sales"), ("inquiry", "Inquiries"), ("labour", "Labour"), ("unspecified", "Unspecified")]
    STATUS_CHOICES = [
        ("created", "Started"),
        ("in_progress", "In Progress"),
        ("overdue", "Overdue"),
        ("completed", "Completed"),
        ("cancelled", "Cancelled"),
    ]
    # Status lifecycle: created -> in_progress -> (overdue or completed or cancelled)
    # created = "Started": Order just created, not yet auto-progressed
    # in_progress: Order auto-progressed after 10 mins, actively being worked on
    # overdue: Order exceeded 2 hours while in_progress
    # completed: Order finished successfully
    # cancelled: Order cancelled by user
    PRIORITY_CHOICES = [("low", "Low"), ("medium", "Medium"), ("high", "High"), ("urgent", "Urgent")]

    order_number = models.CharField(max_length=32, unique=True, editable=False)
    branch = models.ForeignKey('Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='orders')
    customer = models.ForeignKey(Customer, on_delete=models.CASCADE, related_name="orders")
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name="orders")
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default="created")
    # Mixed order types - JSON array of category names found in invoice items
    # e.g., ["labour", "tyre service"] when items map to multiple categories
    mixed_categories = models.TextField(blank=True, null=True, help_text="JSON array of categories detected from invoice items")
    priority = models.CharField(max_length=16, choices=PRIORITY_CHOICES, default="medium")

    description = models.TextField(blank=True, null=True)

    # Sales fields
    item_name = models.CharField(max_length=64, blank=True, null=True)
    brand = models.CharField(max_length=64, blank=True, null=True)
    quantity = models.PositiveIntegerField(blank=True, null=True)
    tire_type = models.CharField(max_length=32, blank=True, null=True)

    # Consultation fields
    inquiry_type = models.CharField(max_length=64, blank=True, null=True)
    questions = models.TextField(blank=True, null=True)
    contact_preference = models.CharField(max_length=16, blank=True, null=True)
    follow_up_date = models.DateField(blank=True, null=True)

    # Timestamps and assignment
    created_at = models.DateTimeField(default=timezone.now)
    started_at = models.DateTimeField(blank=True, null=True)
    completed_at = models.DateTimeField(blank=True, null=True)
    cancelled_at = models.DateTimeField(blank=True, null=True)

    assigned_to = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name="assigned_orders")

    # Completion evidence and signer
    signature_file = models.ImageField(upload_to='order_signatures/', blank=True, null=True)
    completion_attachment = models.FileField(upload_to='order_attachments/', blank=True, null=True)
    signed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders_signed')
    signed_at = models.DateTimeField(blank=True, null=True)
    # completion_date is kept for historical compatibility; completed_at is canonical timestamp used across views


    # Additional fields used across the app
    completion_date = models.DateTimeField(blank=True, null=True)
    cancellation_reason = models.TextField(blank=True, null=True)

    # Time estimation and tracking
    estimated_duration = models.PositiveIntegerField(blank=True, null=True, help_text="Estimated duration in minutes")
    actual_duration = models.PositiveIntegerField(blank=True, null=True, help_text="Actual duration in minutes")

    # Delay/overrun reason for orders that took longer than estimated
    overrun_reason = models.TextField(blank=True, null=True, help_text="Reason for exceeding estimated duration")
    overrun_reported_at = models.DateTimeField(blank=True, null=True)
    overrun_reported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders_overrun_reported')

    # Delay reason for orders that exceeded 2+ hours
    delay_reason = models.ForeignKey('DelayReason', on_delete=models.SET_NULL, null=True, blank=True, related_name='orders', help_text="Reason for delay if order exceeded 2 hours threshold")
    delay_reason_reported_at = models.DateTimeField(blank=True, null=True)
    delay_reason_reported_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='orders_delay_reason_reported')
    exceeded_9_hours = models.BooleanField(default=False, help_text="Whether order exceeded the 2-hour threshold (used for delay reason tracking)")

    # Job card/identification number for quick order lookup (optional)
    job_card_number = models.CharField(max_length=64, blank=True, null=True, unique=True)

    def __str__(self):
        return f"{self.order_number} - {self.customer.full_name}"

    def calculate_estimated_duration(self):
        """Calculate estimated duration in minutes based on started_at and completed_at."""
        if not self.started_at or not self.completed_at:
            return None
        from .utils.time_utils import calculate_estimated_duration
        return calculate_estimated_duration(self.started_at, self.completed_at)

    def get_overdue_status(self):
        """Get overdue status with working hours elapsed."""
        from .utils.time_utils import get_order_overdue_status
        return get_order_overdue_status(self)

    def is_overdue(self):
        """Check if order is overdue (2+ hours in progress)."""
        if self.status != 'in_progress' or not self.started_at:
            return False
        from .utils.time_utils import is_order_overdue
        return is_order_overdue(self.started_at)

    def auto_progress_if_elapsed(self):
        """Automatically move created -> in_progress after 10 minutes."""
        if self.status == 'created' and (timezone.now() - self.created_at) >= timedelta(minutes=10):
            self.status = 'in_progress'
            self.started_at = self.started_at or timezone.now()
            self.save(update_fields=['status', 'started_at'])

    class Meta:
        indexes = [
            models.Index(fields=["order_number"], name="idx_order_number"),
            models.Index(fields=["status"], name="idx_order_status"),
            models.Index(fields=["type"], name="idx_order_type"),
            models.Index(fields=["created_at"], name="idx_order_created"),
        ]

    def _generate_order_number(self) -> str:
        """Generate a unique human-friendly order number."""
        from uuid import uuid4

        prefix = 'ORD'
        base = timezone.now().strftime('%Y%m%d%H%M%S')
        # Retry until unique to avoid collision under concurrent requests
        for _ in range(5):
            candidate = f"{prefix}{base}{uuid4().hex[:4].upper()}"
            if not Order.objects.filter(order_number=candidate).exists():
                return candidate
        # Fallback to full UUID if repeated collisions occur
        return f"{prefix}{uuid4().hex.upper()}"

    def save(self, *args, **kwargs):
        """Ensure order numbers exist and inquiries auto-complete."""
        if not self.order_number:
            self.order_number = self._generate_order_number()
        # If this is an inquiry, make it completed and set completed timestamps
        if self.type == 'inquiry':
            now = timezone.now()
            # Preserve any explicit completed_at if already provided, otherwise set
            if not self.completed_at:
                self.completed_at = now
            if not self.completion_date:
                self.completion_date = now
            # Force status to completed
            self.status = 'completed'
        super().save(*args, **kwargs)


class OrderComponent(models.Model):
    """
    Tracks multiple order types/components for a single order.
    Allows orders to have both service and sales components added at different times.
    """
    TYPE_CHOICES = [("service", "Service"), ("sales", "Sales")]

    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='components')
    type = models.CharField(max_length=16, choices=TYPE_CHOICES)
    added_at = models.DateTimeField(default=timezone.now)
    added_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_components_added')
    reason = models.TextField(blank=True, null=True, help_text="Reason for adding this component")

    # Optional reference to linked invoice
    invoice = models.ForeignKey('Invoice', on_delete=models.SET_NULL, null=True, blank=True, related_name='order_component')

    # Signature status for this component
    signed_at = models.DateTimeField(blank=True, null=True)
    signed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='order_components_signed')
    signature_file = models.ImageField(upload_to='order_component_signatures/', blank=True, null=True)

    class Meta:
        ordering = ['added_at']
        unique_together = [['order', 'type']]  # One component per type per order
        indexes = [
            models.Index(fields=['order', 'type'], name='idx_order_component_type'),
            models.Index(fields=['added_at'], name='idx_order_component_added'),
        ]

    def __str__(self):
        return f"{self.order.order_number} - {self.get_type_display()}"


class OrderInvoiceLink(models.Model):
    """
    Tracks multiple invoices linked to an order with reasons.
    Allows attaching additional invoices to the same order at different times.
    """
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='invoice_links')
    invoice = models.ForeignKey('Invoice', on_delete=models.CASCADE, related_name='order_links')
    reason = models.TextField(blank=True, null=True, help_text="Reason for adding this invoice (e.g., Additional parts, Follow-up service, etc.)")
    linked_at = models.DateTimeField(default=timezone.now)
    linked_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoice_links_created')
    is_primary = models.BooleanField(default=False, help_text="Primary invoice for this order")

    class Meta:
        ordering = ['-linked_at']
        unique_together = [['order', 'invoice']]  # Prevent duplicate links
        indexes = [
            models.Index(fields=['order'], name='idx_order_invoice_link_order'),
            models.Index(fields=['invoice'], name='idx_order_invoice_link_invoice'),
            models.Index(fields=['linked_at'], name='idx_order_invoice_link_date'),
        ]

    def __str__(self):
        return f"{self.order.order_number} - Invoice {self.invoice.invoice_number}"


class OrderAttachment(models.Model):
    order = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='attachments')
    file = models.FileField(upload_to='order_attachments/')
    uploaded_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='uploaded_order_attachments')
    uploaded_at = models.DateTimeField(auto_now_add=True)
    title = models.CharField(max_length=255, blank=True, null=True)

    def filename(self):
        try:
            return self.file.name.split('/')[-1]
        except Exception:
            return self.file.name

    def __str__(self):
        return f"Attachment #{self.id} for {self.order.order_number}"

    class Meta:
        ordering = ['-uploaded_at']
        indexes = [
            models.Index(fields=['order'], name='idx_order_attachment_order'),
            models.Index(fields=['uploaded_at'], name='idx_order_att_uploaded_at'),
        ]


class OrderAttachmentSignature(models.Model):
    """Tracks signed versions of supporting documents, separate from order completion."""
    attachment = models.OneToOneField(OrderAttachment, on_delete=models.CASCADE, related_name='signature')
    signed_file = models.FileField(upload_to='order_attachments_signed/')
    signature_image = models.ImageField(upload_to='order_attachment_signatures/', blank=True, null=True)
    signed_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='signed_order_attachments')
    signed_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Signed: {self.attachment.filename()} for {self.attachment.order.order_number}"

    class Meta:
        ordering = ['-signed_at']
        indexes = [
            models.Index(fields=['attachment'], name='idx_att_sig_attachment'),
            models.Index(fields=['signed_at'], name='idx_att_sig_signed_at'),
        ]


class Brand(models.Model):
    name = models.CharField(max_length=128, unique=True)
    description = models.TextField(blank=True, null=True)
    website = models.URLField(blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"], name="idx_brand_name"),
            models.Index(fields=["is_active"], name="idx_brand_active"),
        ]

    def __str__(self) -> str:
        return self.name


class InventoryItem(models.Model):
    name = models.CharField(max_length=128)
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='items')
    description = models.TextField(blank=True, null=True)
    quantity = models.PositiveIntegerField(default=0)
    price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    sku = models.CharField(max_length=64, blank=True, null=True)
    barcode = models.CharField(max_length=64, blank=True, null=True)
    reorder_level = models.PositiveIntegerField(default=5)
    location = models.CharField(max_length=128, blank=True, null=True)
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"], name="idx_inv_name"),
            models.Index(fields=["quantity"], name="idx_inv_qty"),
            models.Index(fields=["is_active"], name="idx_inv_active"),
        ]
        constraints = [
            models.UniqueConstraint(fields=["name", "brand"], name="uniq_item_brand_name")
        ]

    def __str__(self) -> str:
        b = self.brand.name if self.brand else "Unbranded"
        return f"{b} - {self.name}"


class InventoryAdjustment(models.Model):
    ADJUSTMENT_TYPES = (
        ("addition", "Addition"),
        ("removal", "Removal"),
    )
    item = models.ForeignKey(InventoryItem, on_delete=models.CASCADE, related_name='adjustments')
    adjustment_type = models.CharField(max_length=16, choices=ADJUSTMENT_TYPES)
    quantity = models.PositiveIntegerField()
    reference = models.CharField(max_length=64, blank=True, null=True)
    notes = models.TextField(blank=True, null=True)
    adjusted_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='inventory_adjustments')
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['created_at'], name='idx_inv_adj_created'),
            models.Index(fields=['adjustment_type'], name='idx_inv_adj_type'),
        ]

    # Backwards-friendly aliases used by older utility scripts
    @property
    def user(self):
        return self.adjusted_by

    @property
    def date(self):
        return self.created_at

    def __str__(self) -> str:
        return f"{self.get_adjustment_type_display()} {self.quantity} × {self.item}"


class Profile(models.Model):
    user = models.OneToOneField(User, on_delete=models.CASCADE, related_name='profile')
    branch = models.ForeignKey(Branch, on_delete=models.SET_NULL, null=True, blank=True, related_name='profiles')
    photo = models.ImageField(upload_to='profile_photos/', blank=True, null=True)
    created_at = models.DateTimeField(auto_now_add=True)

    def __str__(self) -> str:
        return f"Profile of {self.user.username}"


class CustomerNote(models.Model):
    customer = models.ForeignKey(
        Customer,
        on_delete=models.CASCADE,
        related_name='note_entries',
        related_query_name='note_entry',
    )
    content = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='customer_notes')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['customer'], name='idx_cnote_customer'),
            models.Index(fields=['created_at'], name='idx_cnote_created'),
        ]

    def __str__(self) -> str:
        return f"Note for {self.customer.full_name} at {timezone.localtime(self.created_at).strftime('%Y-%m-%d %H:%M')}"


class ServiceType(models.Model):
    """Admin-managed service types for 'Service' orders."""
    name = models.CharField(max_length=128, unique=True)
    estimated_minutes = models.PositiveIntegerField(default=0, help_text="Estimated duration in minutes")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"], name="idx_service_type_name"),
            models.Index(fields=["is_active"], name="idx_service_type_active"),
        ]

    def __str__(self) -> str:
        return self.name


class ServiceAddon(models.Model):
    """Admin-managed add-on services for 'Sales' orders (e.g., installation, balancing)."""
    name = models.CharField(max_length=128, unique=True)
    estimated_minutes = models.PositiveIntegerField(default=0, help_text="Estimated duration in minutes")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ["name"]
        indexes = [
            models.Index(fields=["name"], name="idx_service_addon_name"),
            models.Index(fields=["is_active"], name="idx_service_addon_active"),
        ]

    def __str__(self) -> str:
        return self.name




class Invoice(models.Model):
    """Invoice records generated from orders"""
    STATUS_CHOICES = [
        ('draft', 'Draft'),
        ('issued', 'Issued'),
        ('paid', 'Paid'),
        ('cancelled', 'Cancelled'),
    ]

    # Invoice identification
    invoice_number = models.CharField(max_length=32, unique=True, editable=False, db_index=True)
    status = models.CharField(max_length=16, choices=STATUS_CHOICES, default='draft')

    # Relationships
    branch = models.ForeignKey('Branch', on_delete=models.PROTECT, null=True, blank=True, related_name='invoices')
    order = models.ForeignKey(Order, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    customer = models.ForeignKey(Customer, on_delete=models.PROTECT, related_name='invoices')
    vehicle = models.ForeignKey(Vehicle, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices')
    salesperson = models.ForeignKey('Salesperson', on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices', help_text="Salesperson associated with this invoice")

    # Invoice details
    invoice_date = models.DateField(default=timezone.now)
    due_date = models.DateField(blank=True, null=True)
    code_no = models.CharField(max_length=128, blank=True, null=True, help_text="Supplier/Invoice code number")
    reference = models.CharField(max_length=128, blank=True, null=True, help_text="Customer PO or reference number")

    # Amounts
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Tax percentage")
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0)

    # Additional fields
    notes = models.TextField(blank=True, null=True)
    terms = models.TextField(blank=True, null=True)
    attended_by = models.CharField(max_length=128, blank=True, null=True)
    kind_attention = models.CharField(max_length=128, blank=True, null=True)
    remarks = models.TextField(blank=True, null=True)

    # Seller / Supplier information extracted from invoices (optional)
    seller_name = models.CharField(max_length=255, blank=True, null=True)
    seller_address = models.TextField(blank=True, null=True)
    seller_phone = models.CharField(max_length=64, blank=True, null=True)
    seller_email = models.CharField(max_length=128, blank=True, null=True)
    seller_tax_id = models.CharField(max_length=64, blank=True, null=True)
    seller_vat_reg = models.CharField(max_length=64, blank=True, null=True)

    # Document storage (uploaded invoice file)
    document = models.FileField(upload_to='invoices/', blank=True, null=True, help_text="Uploaded invoice document (PDF, image, etc.)")

    # Tracking
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='invoices_created')
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['-invoice_date', '-invoice_number']
        indexes = [
            models.Index(fields=['invoice_number'], name='idx_invoice_number'),
            models.Index(fields=['customer'], name='idx_invoice_customer'),
            models.Index(fields=['order'], name='idx_invoice_order'),
            models.Index(fields=['status'], name='idx_invoice_status'),
        ]

    def __str__(self) -> str:
        return f"Invoice {self.invoice_number} - {self.customer.full_name}"

    def calculate_totals(self):
        """Recalculate totals from line items, considering per-item VAT"""
        line_items = self.line_items.all()

        # Calculate subtotal from all line items
        self.subtotal = sum(Decimal(str(item.line_total)) for item in line_items) if line_items.exists() else Decimal('0')

        # Calculate tax: sum of per-item taxes + invoice-level tax on subtotal
        per_item_tax = sum(Decimal(str(item.tax_amount)) for item in line_items) if line_items.exists() else Decimal('0')
        invoice_level_tax = self.subtotal * (Decimal(str(self.tax_rate)) / 100) if self.tax_rate else Decimal('0')
        self.tax_amount = per_item_tax + invoice_level_tax

        # Calculate total
        self.total_amount = self.subtotal + self.tax_amount
        return self

    def generate_invoice_number(self):
        """Generate sequential invoice number"""
        if self.invoice_number:
            return self.invoice_number
        from datetime import datetime
        year = datetime.now().year
        prefix = f"INV-{year}-"
        existing = Invoice.objects.filter(invoice_number__startswith=prefix).values_list('invoice_number', flat=True)
        max_seq = 0
        for inv_no in existing:
            try:
                seq = int(inv_no.split(prefix)[1])
                if seq > max_seq:
                    max_seq = seq
            except Exception:
                continue
        next_seq = max_seq + 1
        candidate = f"{prefix}{next_seq:05d}"
        while Invoice.objects.filter(invoice_number=candidate).exists():
            next_seq += 1
            candidate = f"{prefix}{next_seq:05d}"
        self.invoice_number = candidate
        return self.invoice_number


class InvoiceLineItem(models.Model):
    """Line items in an invoice"""
    invoice = models.ForeignKey(Invoice, on_delete=models.CASCADE, related_name='line_items')

    # Item details
    code = models.CharField(max_length=128, blank=True, null=True, help_text='Item code from invoice')
    description = models.CharField(max_length=255)
    item_type = models.CharField(
        max_length=16,
        choices=[('product', 'Product'), ('service', 'Service'), ('custom', 'Custom')],
        default='custom'
    )

    # Reference to inventory item (optional)
    inventory_item = models.ForeignKey(InventoryItem, on_delete=models.SET_NULL, null=True, blank=True)

    # Quantities and pricing
    quantity = models.DecimalField(max_digits=10, decimal_places=2, default=1)
    unit = models.CharField(max_length=16, blank=True, null=True, help_text="e.g., 'PCS', 'UNT', 'HR'")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2)

    # Calculated
    line_total = models.DecimalField(max_digits=12, decimal_places=2, editable=False)

    # Per-item VAT/Tax
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0, help_text="Tax percentage for this line item")
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0, editable=False)

    # Order type from labour code matching (sales, service, labour, unspecified)
    order_type = models.CharField(
        max_length=16,
        choices=[('sales', 'Sales'), ('service', 'Service'), ('labour', 'Labour'), ('unspecified', 'Unspecified')],
        default='unspecified',
        db_index=True,
        help_text="Order type determined from item code or category"
    )

    # Salesperson for this line item (if order_type is sales)
    salesperson = models.ForeignKey('Salesperson', on_delete=models.SET_NULL, null=True, blank=True, related_name='line_items', help_text="Salesperson who made this sale")

    # Tracking
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['invoice', 'created_at']

    def save(self, *args, **kwargs):
        # Only recalculate line_total if it wasn't explicitly set (from extraction)
        # This preserves extracted values from invoices while supporting manual entry
        if not self.line_total or self.line_total == Decimal('0'):
            self.line_total = self.quantity * self.unit_price

        # Only recalculate tax_amount if it wasn't explicitly set
        if not self.tax_amount or self.tax_amount == Decimal('0'):
            self.tax_amount = self.line_total * (self.tax_rate / 100) if self.tax_rate else Decimal('0')

        super().save(*args, **kwargs)

    def __str__(self) -> str:
        return f"{self.description} x {self.quantity}"


class InvoicePayment(models.Model):
    """Payment information for invoices"""
    PAYMENT_METHOD_CHOICES = [
        ('cash', 'Cash'),
        ('cheque', 'Cheque'),
        ('bank_transfer', 'Bank Transfer'),
        ('card', 'Card'),
        ('mpesa', 'M-Pesa'),
        ('on_delivery', 'Cash on Delivery'),
        ('on_credit', 'On Credit'),
        ('other', 'Other'),
    ]

    invoice = models.OneToOneField(Invoice, on_delete=models.CASCADE, related_name='payment')

    # Payment details
    payment_method = models.CharField(max_length=32, choices=PAYMENT_METHOD_CHOICES)
    amount = models.DecimalField(max_digits=12, decimal_places=2)
    payment_date = models.DateField(blank=True, null=True)
    reference = models.CharField(max_length=128, blank=True, null=True, help_text="Cheque number, transaction ID, etc.")

    # Additional notes
    notes = models.TextField(blank=True, null=True)

    # Tracking
    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        verbose_name = 'Invoice Payment'
        verbose_name_plural = 'Invoice Payments'

    def __str__(self) -> str:
        return f"{self.get_payment_method_display()} - {self.amount}"


class DelayReasonCategory(models.Model):
    """Categories for delay reasons when orders exceed 9+ hours"""
    CATEGORY_CHOICES = [
        ('parts', 'Parts-Related Delays'),
        ('technical', 'Technical / Diagnostic Issues'),
        ('workload', 'High Workload / Operational Capacity'),
        ('customer', 'Customer-Related Causes'),
        ('administrative', 'Administrative / System Issues'),
        ('quality', 'Quality-Control & Testing Delays'),
        ('external', 'External / Environmental Factors'),
    ]

    category = models.CharField(max_length=32, choices=CATEGORY_CHOICES, unique=True)
    description = models.TextField(blank=True, null=True, help_text="Description of this delay category")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category']
        verbose_name_plural = 'Delay Reason Categories'
        indexes = [
            models.Index(fields=['category'], name='idx_delay_category'),
            models.Index(fields=['is_active'], name='idx_delay_category_active'),
        ]

    def __str__(self) -> str:
        return self.get_category_display()


class DelayReason(models.Model):
    """Specific delay reasons within categories"""
    category = models.ForeignKey(DelayReasonCategory, on_delete=models.CASCADE, related_name='reasons')
    reason_text = models.CharField(max_length=255, help_text="Specific delay reason")
    is_active = models.BooleanField(default=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['category', 'reason_text']
        unique_together = [['category', 'reason_text']]
        indexes = [
            models.Index(fields=['category'], name='idx_delay_reason_category'),
            models.Index(fields=['is_active'], name='idx_delay_reason_active'),
        ]

    def __str__(self) -> str:
        return f"{self.get_category_display()} - {self.reason_text}" if hasattr(self, 'category') else self.reason_text

    def get_category_display(self):
        return self.category.get_category_display() if self.category else ''


class InquiryNote(models.Model):
    """Timeline/notes for inquiry conversation threads"""
    inquiry = models.ForeignKey(Order, on_delete=models.CASCADE, related_name='notes', limit_choices_to={'type': 'inquiry'})
    note_type = models.CharField(
        max_length=16,
        choices=[
            ('response', 'Response'),
            ('note', 'Internal Note'),
            ('status_change', 'Status Change'),
            ('attachment', 'Attachment Added'),
        ],
        default='note'
    )
    content = models.TextField()
    created_by = models.ForeignKey(User, on_delete=models.SET_NULL, null=True, blank=True, related_name='inquiry_notes')
    created_at = models.DateTimeField(auto_now_add=True)
    is_visible_to_customer = models.BooleanField(default=True, help_text="Whether customer can see this note in communication thread")

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['inquiry', '-created_at'], name='idx_inquiry_note_created'),
            models.Index(fields=['note_type'], name='idx_inquiry_note_type'),
        ]

    def __str__(self) -> str:
        return f"{self.get_note_type_display()} for Inquiry #{self.inquiry.id}"
