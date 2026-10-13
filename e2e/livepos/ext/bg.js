// Test background. The f1-token handler is the one proposed for fone's background.js.
'use strict';
importScripts('f1token.js');

// The F1 TV token for livepos.js. Only fone's own offscreen document may have it: never a
// content script (sender.tab) or any other page, so nothing on f1tv.formula1.com can ask.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'background' || msg.type !== 'f1-token') return;
  if (sender.id !== chrome.runtime.id || sender.tab || sender.url !== chrome.runtime.getURL('offscreen.html')) {
    sendResponse(null);
    return;
  }
  FoneF1Token.f1Token().then(sendResponse, () => sendResponse(null));
  return true; // answered asynchronously
});

// Test plumbing: the page asks for the offscreen document.
chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'test-bg' || msg.type !== 'ensure-offscreen') return;
  chrome.runtime.getContexts({ contextTypes: ['OFFSCREEN_DOCUMENT'] }).then(async (have) => {
    if (!have.length) await chrome.offscreen.createDocument({ url: 'offscreen.html', reasons: ['BLOBS'], justification: 'livepos test' });
    sendResponse(true);
  });
  return true;
});
