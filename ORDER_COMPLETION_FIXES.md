# Fix for Order Completion Issues (Signature Not Captured)

## Problem
When users try to complete an order with signature capture in `order_detail.html`, the JavaScript looks for a button with ID `signAndCompleteBtn` which doesn't exist. The actual button has ID `footerSignAndComplete`. This causes the form submission to fail and documents are not saved.

## Root Cause
- Line 1376: `const signAndCompleteBtn = document.getElementById('signAndCompleteBtn');` - This will be `null`
- Line 1503: `if (signAndCompleteBtn) signAndCompleteBtn.addEventListener('click', handleFooterButtonClick);` - Never executes
- Line 4009: `const signBtn = document.getElementById('signAndCompleteBtn');` - This will be `null`
- Line 4437-4438: Tries to click non-existent button - Never executes

## Required Fixes

### Fix 1: Remove reference to non-existent button (Line 1376)
**OLD:**
```javascript
const signAndCompleteBtn = document.getElementById('signAndCompleteBtn');
```

**NEW:** Comment it out or remove the entire line
```javascript
// signAndCompleteBtn element doesn't exist - only footerSignAndComplete is used
```

### Fix 2: Remove redundant handler (Line 1503)
**OLD:**
```javascript
if (footerBtn) footerBtn.addEventListener('click', handleFooterButtonClick);
if (signAndCompleteBtn) signAndCompleteBtn.addEventListener('click', handleFooterButtonClick);
```

**NEW:** Keep only the first line
```javascript
if (footerBtn) footerBtn.addEventListener('click', handleFooterButtonClick);
```

### Fix 3: Update button reference in setupSignButton (Line 4009)
**OLD:**
```javascript
const signBtn = document.getElementById('signAndCompleteBtn');
```

**NEW:**
```javascript
const signBtn = document.getElementById('footerSignAndComplete');
```

### Fix 4: Remove code that tries to click non-existent button (Lines 4435-4439)
**OLD:**
```javascript
footerBtn.addEventListener('click', function(e){
  e.preventDefault();
  const btn = document.getElementById('signAndCompleteBtn');
  if (btn) btn.click();
});
```

**NEW:** Remove this entire block or replace with:
```javascript
footerBtn.addEventListener('click', function(e){
  e.preventDefault();
  // handleFooterButtonClick is already attached to footerBtn above
  // This redundant handler can be removed
});
```

## Alternative: Simpler Fix
If the above is complex, the simplest approach is to just make setupSignButton work by changing line 4009:

```javascript
// Change from:
const signBtn = document.getElementById('signAndCompleteBtn');
// To:
const signBtn = document.getElementById('footerSignAndComplete');
```

This single change will make the `setupSignButton()` function properly attach a click handler to the actual footer button, which will:
1. Validate signature was drawn
2. Capture signature data from canvas to the hidden input field
3. Submit the form with the signature data

## Related Issue: started_order_detail.html
The file `tracker/templates/tracker/started_order_detail.html` has a similar issue on line 1384:
- References a button with ID `completeOrderBtn` that doesn't exist
- Need to add the button or change the JavaScript selector

## How This Affects Users
1. User clicks "Complete Order" button
2. Signature canvas opens but click handler has issues
3. When user draws signature and clicks "Complete Order" button, the signature data is not captured
4. Form submits without signature and order completion fails
5. Signed documents are not displayed on the order detail page

## Testing After Fix
1. Create/start a test order
2. Click "Capture Signature" button
3. Draw a signature on the canvas
4. Click "Complete Order" button
5. Verify order status changes to "completed"
6. Verify signed documents appear on the order detail page
7. Verify signature image is visible

## Files to Modify
- `tracker/templates/tracker/order_detail.html` (Lines: 1376, 1503, 4009, 4435-4439)
- `tracker/templates/tracker/started_order_detail.html` (Line: 1384)
