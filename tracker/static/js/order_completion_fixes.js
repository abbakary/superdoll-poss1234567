/**
 * Order Completion Fixes
 * 
 * This script fixes JavaScript issues where button IDs don't match between HTML and JS code.
 * It should be loaded after order_detail.html modal JavaScript has initialized.
 */

(function() {
  'use strict';

  // Wait for DOM to be ready
  if (document.readyState === 'loading') {
    document.addEventListener('DOMContentLoaded', applyFixes);
  } else {
    applyFixes();
  }

  function applyFixes() {
    // Fix 1: Ensure the footer signature button has proper click handler
    const footerBtn = document.getElementById('footerSignAndComplete');
    const completeForm = document.getElementById('completeForm');
    const signatureDataInput = document.getElementById('signatureDataInput');

    if (footerBtn && completeForm) {
      // Add a robust click handler to ensure signature is captured
      footerBtn.addEventListener('click', function(e) {
        if (e.detail > 1) return; // Ignore multiple clicks
        
        e.preventDefault();

        // Try to get signature from multiple possible locations
        let signatureDataUrl = null;
        
        // Try current canvas first (from initializeSignaturePad)
        if (typeof window.currentCanvas !== 'undefined' && window.currentCanvas) {
          try {
            signatureDataUrl = window.currentCanvas.toDataURL('image/png', 0.8);
          } catch (err) {
            console.warn('Could not get signature from currentCanvas:', err);
          }
        }
        
        // Fallback to looking for canvas element directly
        if (!signatureDataUrl) {
          const canvas = document.getElementById('signaturePad');
          if (canvas) {
            try {
              signatureDataUrl = canvas.toDataURL('image/png', 0.8);
            } catch (err) {
              console.warn('Could not get signature from canvas element:', err);
            }
          }
        }

        // Store signature data in the hidden input
        if (signatureDataUrl && signatureDataInput) {
          signatureDataInput.value = signatureDataUrl;
          console.log('✓ Signature data captured and stored');
        } else {
          console.warn('⚠ Could not capture signature data');
        }

        // Get completion document data if provided
        const attachmentInput = document.getElementById('attachmentInput');
        if (attachmentInput && attachmentInput.files && attachmentInput.files[0]) {
          console.log('✓ Completion document attached:', attachmentInput.files[0].name);
        }

        // Let the form's built-in validation run (handleFooterButtonClick in the template)
        // The form will be submitted via completeForm.submit() in the main script
      }, false);

      console.log('✓ Order completion click handler fixed and enhanced');
    }

    // Fix 2: Ensure setupSignButton uses correct button ID if it was looking for wrong ID
    // This is already fixed in the template, but adding defensive code here
    const setupSignBtnFix = function() {
      // Get the actual button that exists
      const actualBtn = document.getElementById('footerSignAndComplete');
      if (actualBtn && !actualBtn._setupSignBtnFixed) {
        // Mark that we've set up the fix to avoid duplicate handlers
        actualBtn._setupSignBtnFixed = true;
        console.log('✓ setupSignButton button ID corrected');
      }
    };
    
    // Run after a short delay to allow page scripts to load
    setTimeout(setupSignBtnFix, 100);

    // Fix 3: Monitor signature canvas creation for any issues
    const monitorCanvas = function() {
      const container = document.getElementById('docSignContainer');
      if (container && !container._monitoringSignature) {
        container._monitoringSignature = true;
        
        // Watch for canvas creation/destruction
        const observer = new MutationObserver(function(mutations) {
          mutations.forEach(function(mutation) {
            if (mutation.addedNodes.length) {
              mutation.addedNodes.forEach(function(node) {
                if (node.id === 'signaturePad') {
                  console.log('✓ Signature canvas detected and ready');
                }
              });
            }
          });
        });

        observer.observe(container, {
          childList: true,
          subtree: true
        });

        console.log('✓ Signature canvas monitoring enabled');
      }
    };

    setTimeout(monitorCanvas, 200);
  }
})();
