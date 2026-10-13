// Test page (Playwright drives it): talks to the background and the offscreen document.
'use strict';
window.T = {
  ensure: () => chrome.runtime.sendMessage({ target: 'test-bg', type: 'ensure-offscreen' }),
  call: (type, opts) => chrome.runtime.sendMessage({ target: 'test-offscreen', type, opts }),
  // Any context but the offscreen document must be refused the token.
  tokenFromPage: () => chrome.runtime.sendMessage({ target: 'background', type: 'f1-token' }),
};
