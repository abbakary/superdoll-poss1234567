# Salesperson Feature Implementation Summary

This document outlines the complete implementation of the Salesperson feature for tracking sales transactions and audit purposes.

## Overview

The salesperson feature has been implemented to allow users to:
- Assign salespeople to invoices and line items
- Filter orders by salesperson
- Track which salesperson made each sale for audit purposes
- View salesperson information in order details and orders list

## Changes Made

### 1. Database Models

#### New Model: `Salesperson`
- **File**: `tracker/models.py`
- **Fields**:
  - `code`: CharField (unique, e.g., "346", "401")
  - `name`: CharField (e.g., "Maria Shayo", "DCV POS")
  - `is_active`: BooleanField (default=True)
  - `is_default`: BooleanField (default=False, only one can be default)
  - `created_at`: DateTimeField (auto_now_add=True)
  - `updated_at`: DateTimeField (auto_now=True)

#### Updated Models:
- **Invoice**: Added `salesperson` ForeignKey to Salesperson
- **InvoiceLineItem**: Added `salesperson` ForeignKey to Salesperson (for sales line items)

### 2. Admin Interface

- **File**: `tracker/admin.py`
- Created `SalespersonAdmin` with:
  - List display: code, name, is_active, is_default, created_at
  - Search fields: code, name
  - List filters: is_active, is_default, created_at
  - Custom search that prioritizes exact code matches

### 3. Invoice Upload Modal

- **File**: `tracker/templates/tracker/partials/invoice_upload_modal.html`
- Added salesperson dropdown that:
  - Only appears if invoice contains sales line items
  - Loads salespeople from API endpoint
  - Defaults to the marked default salesperson
  - Required field when sales items are present

### 4. API Endpoints

- **File**: `tracker/views_invoice_upload.py`
- **New Endpoint**: `api_get_salespersons` (GET `/api/salespersons/`)
  - Returns all active salespeople
  - Returns: `{ success: true, salespersons: [...] }`

### 5. Invoice Extraction & Creation

- **File**: `tracker/views_invoice_upload.py`
- **Function**: `api_create_invoice_from_upload`
  - Accepts `salesperson_id` from form POST
  - Assigns salesperson to invoice
  - Assigns salesperson to sales line items
  - Falls back to default salesperson if not provided

### 6. Order Detail Page

- **File**: `tracker/templates/tracker/order_detail.html`
- Added salesperson display in:
  - Invoice summary (shows code and name)
  - Line items table (new "Salesperson" column)
  - Both primary and additional invoices

### 7. Orders List Page

- **File**: `tracker/templates/tracker/orders_list.html`
- Added salesperson filter dropdown in regular orders section
- Added salesperson column in orders table
- Shows first available salesperson from invoices

### 8. Orders List View

- **File**: `tracker/views.py`
- **Function**: `orders_list`
  - Added `salesperson_id` filter parameter
  - Filters orders by invoice line items with selected salesperson
  - Passes active salespeople list to template

### 9. URL Routing

- **File**: `tracker/urls.py`
- Added route: `path("api/salespersons/", views_invoice_upload.api_get_salespersons, name="api_get_salespersons")`

### 10. Management Command

- **File**: `tracker/management/commands/setup_salespeople.py`
- Command: `python manage.py setup_salespeople`
- Sets up default salespeople:
  - 346 - Maria Shayo (is_default=False)
  - 401 - DCV POS (is_default=True)

## Setup Instructions

### 1. Run Migrations

First, you need to create the migrations for the new Salesperson model and the updated fields:

```bash
python manage.py makemigrations
python manage.py migrate
```

### 2. Set Up Default Salespeople

Run the management command to create the default salespeople:

```bash
python manage.py setup_salespeople
```

This will create:
- Code: 346, Name: Maria Shayo
- Code: 401, Name: DCV POS (set as default)

### 3. Verify Setup

Check the Django admin to verify salespeople were created:
- Go to: `/admin/tracker/salesperson/`
- You should see both salespeople listed

## Usage

### Upload Invoice with Sales

1. Go to order detail page
2. Click "Upload & Extract Invoice"
3. Upload PDF with sales items
4. The "Salesperson" dropdown will appear (only if sales items found)
5. Select the salesperson who made the sale
6. Click "Create Order"

### Filter Orders by Salesperson

1. Go to Orders page (Regular Orders view)
2. Use the "Salesperson" filter dropdown
3. Select desired salesperson
4. Click "Filter" button
5. Table will show only orders with sales from that salesperson

### View Salesperson in Order Details

1. Go to any order detail page
2. In the "Invoices" section:
   - Invoice summary shows salesperson code and name
   - Line items table has a "Salesperson" column
   - Shows salesperson code and name for each sales line item

### Manage Salespeople (Admin Only)

1. Go to Django Admin: `/admin/tracker/salesperson/`
2. Add new salesperson:
   - Enter code (e.g., "350")
   - Enter name (e.g., "John Doe")
   - Check "is_active" if should be available
   - Check "is_default" to make it the default (only one allowed)
3. Save

## Key Features

✓ **Unique Codes**: Salespeople identified by code (e.g., "346", "401")
✓ **Default Salesperson**: 401 (DCV POS) is default when not specified
✓ **Sales Only**: Salesperson dropdown only shows for sales items
✓ **Audit Trail**: Every sales line item tracks which salesperson made it
✓ **Filtering**: Orders list can be filtered by salesperson
✓ **Multi-page**: Salesperson info appears in:
  - Invoice upload modal (dropdown)
  - Order detail page (display)
  - Orders list page (display + filter)

## Database Schema

### Salesperson Table
```sql
CREATE TABLE tracker_salesperson (
  id INTEGER PRIMARY KEY,
  code VARCHAR(32) UNIQUE NOT NULL,
  name VARCHAR(255) NOT NULL,
  is_active BOOLEAN DEFAULT TRUE,
  is_default BOOLEAN DEFAULT FALSE,
  created_at DATETIME AUTO_NOW_ADD,
  updated_at DATETIME AUTO_NOW
);
```

### Updated Invoice Table
```sql
ALTER TABLE tracker_invoice ADD COLUMN salesperson_id INTEGER;
ALTER TABLE tracker_invoice ADD FOREIGN KEY (salesperson_id) 
  REFERENCES tracker_salesperson(id) SET NULL;
```

### Updated InvoiceLineItem Table
```sql
ALTER TABLE tracker_invoicelineitem ADD COLUMN salesperson_id INTEGER;
ALTER TABLE tracker_invoicelineitem ADD FOREIGN KEY (salesperson_id)
  REFERENCES tracker_salesperson(id) SET NULL;
```

## Testing Checklist

- [ ] Run migrations successfully
- [ ] Run setup_salespeople command
- [ ] Verify salespeople visible in admin
- [ ] Upload invoice with sales items
- [ ] Salesperson dropdown appears for sales
- [ ] Select different salesperson
- [ ] Verify saved to invoice
- [ ] View order detail page
- [ ] Salesperson shown in invoice summary
- [ ] Salesperson shown in line items table
- [ ] Go to orders list page
- [ ] Filter by salesperson
- [ ] Orders filtered correctly
- [ ] Salesperson column shows in table
- [ ] Create new salesperson in admin
- [ ] New salesperson appears in dropdown

## Notes

- The 401 (DCV POS) salesperson is marked as default and will be used automatically if no salesperson is selected
- Only active salespeople appear in dropdowns and filters
- Salesperson info is preserved in audit logs through the database relationships
- The implementation supports multiple invoices per order, each with potentially different salespeople
