// Test offscreen document, wired like fone's offscreen.js: the token comes from the
// background right before each connection, through getToken.
'use strict';
const f1Token = () => chrome.runtime.sendMessage({ target: 'background', type: 'f1-token' });

chrome.runtime.onMessage.addListener((msg, sender, sendResponse) => {
  if (msg?.target !== 'test-offscreen') return;
  if (msg.type === 'start') FoneLivePos.start({ ...msg.opts, getToken: f1Token });
  else if (msg.type === 'stop') FoneLivePos.stop();
  sendResponse(FoneLivePos.status());
});
