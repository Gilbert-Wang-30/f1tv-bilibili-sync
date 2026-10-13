// Runs in F1 TV's own JS world (manifest "world": "MAIN"): optional companion of layout.js.
//
// While the split view is on (<html data-fone-layout="split">, set by layout.js), F1 TV's
// fullscreen becomes fullscreen of the whole page, so the stats stay beside the video. F1 TV
// asks for it with requestFullscreen() on div.video-grid, from its fullscreen button and from
// a double-click on the video (its own click timing, no dblclick event). Only that call holds
// the click's user activation, so it has to be redirected here, in the page's world: from the
// isolated world layout.js can only catch the button, before F1 TV's handler runs.
(() => {
  'use strict';
  if (window.__foneLayoutPage) return;
  window.__foneLayoutPage = true;

  const html = document.documentElement;
  const request = Element.prototype.requestFullscreen;
  if (typeof request !== 'function') return;
  html.setAttribute('data-fone-fs', ''); // layout.js: F1 TV's button needn't be intercepted

  Element.prototype.requestFullscreen = function (...args) {
    if (this === html || html.dataset.foneLayout !== 'split' || !this.closest('.video-grid')) {
      return request.apply(this, args);
    }
    const asked = this;
    // F1 TV learns that fullscreen ended from a 'fullscreenchange' on the element it asked for;
    // that event now goes to <html>, so pass the end on (its fullscreen button state resets).
    const ended = () => {
      if (document.fullscreenElement) return;
      document.removeEventListener('fullscreenchange', ended);
      asked.dispatchEvent(new Event('fullscreenchange'));
    };
    return request.apply(html, args).then(() => { document.addEventListener('fullscreenchange', ended); });
  };
})();
