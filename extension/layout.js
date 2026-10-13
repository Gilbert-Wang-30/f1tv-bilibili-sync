// One-tab split view on F1 TV: the player shrinks to a 16:9 box in the top-left 77 % of the
// viewport and the stats dashboard (dashboard.html?mode=iframe, an extension page) fills the
// L-shaped rest. Content script, isolated world, top frame only. Exposes window.FoneLayout:
//   enable() / disable() / toggle() / isOn()   the choice is kept in chrome.storage.local
//                                              ('layout': 'split' | 'off') and applied whenever
//                                              an F1 TV player is on screen
//   popOut()                                   the stats in a window of their own instead:
//                                              turns the split off and sends the background
//                                              {target: 'background', type: 'popout-dashboard'}
// The S key toggles (Alt+Shift is Windows' input-language switch, so no modifiers).
//
// F1 TV's player (Tiledmedia) paints in two position:fixed layers that both cover the viewport:
//   #video-grid-root > div.video-grid (z 100)    the views (tiledmedia-view > video), F1 TV's menus
//     ... > div.tmw-player-ui-container (z 999)  Tiledmedia's control bar; fixed, so it escapes
//                                                the grid's box although it sits inside the grid
// Both are pinned to the player box by an injected !important stylesheet: F1 TV re-renders and
// swaps these nodes on content and feed changes, and a selector keeps matching the new ones
// where inline styles would be lost. Everything inside is sized in % of the grid, so it
// follows; each tiledmedia-view has its own ResizeObserver that hands the SDK its new size.
// The dashboard iframe sits just below the grid's z-index: the player box stays the player's,
// the rest of the screen is the dashboard's.
//
// To the dashboard, on load and on every change (viewport size, fullscreen):
//   {type: 'fone-layout', rect: {x, y, w, h}, vw, vh, fullscreen}     CSS px, viewport-relative
// From the dashboard (only from our iframe, only from the extension's origin):
//   {type: 'fone-layout-cmd', cmd: 'off' | 'popout' | 'fullscreen' | 'rect'}
// 'fullscreen' toggles page fullscreen; a click in the iframe also activates this frame, so the
// browser allows it. 'rect' asks for the rect message again.
//   {type: 'fone-layout-cmd', cmd: 'onboards', slots: [{x, y, w, h, channelId}, ...]}
// Driver onboards in the dashboard: at most 2 video rectangles (CSS px, viewport-relative, inside
// the viewport), channelId an F1 TV channel id or null (an empty slot); re-sent every 2 s while
// any is shown, [] = none. page.js (main world) shows F1 TV's own player there: forwarded as a
// 'fone-cmd' event like content.js's commands. Turning the split off (or the player going away)
// forwards slots: [].
(() => {
  'use strict';
  if (window.top !== window) return;
  if (window.FoneLayout) { document.documentElement.setAttribute('data-fone-layout-ready', 'again'); return; }

  const RATIO = 0.77; // the player box's share of the viewport width (70 % looked a bit small)
  const GRID = 'div.video-grid';
  // :not(#…) lifts our rules to id specificity, above any !important class rule of F1 TV's.
  const PINNED = [GRID, 'div.tmw-player-ui-container'].map((s) => `${s}:not(#fone-none)`).join(',');
  const FS_BUTTON = '.tm-ui-fullscreentogglebutton'; // Tiledmedia UI's fullscreen toggle
  const GRACE = 1500; // F1 TV may drop the player and build a new one on a content change
  const DASH_URL = chrome.runtime.getURL('dashboard.html?mode=iframe');
  const DASH_ORIGIN = chrome.runtime.getURL('').replace(/\/$/, '');
  const html = document.documentElement;

  let want = false; // the user's choice
  let chosen = false; // chosen on this page before storage answered
  let active = false; // applied right now: wanted, and a player on screen
  let rect = null; // the player box {x, y, w, h}, CSS px
  let vw = 0;
  let vh = 0;
  let style = null;
  let frame = null;
  let grid = null; // the div.video-grid we last saw
  let panel = null; // fone's panel (content.js) as we found it: {el, top, right, dx, dy}
  let dropTimer = 0;
  let watcher = null;
  let swapped = null; // the element F1 TV made fullscreen before we swapped in the page
  let dashBox = null; // the dashboard's box for the player ({w, h}, 'player' command), null: ours

  // ---- geometry -----------------------------------------------------------------------

  // ~77 % of the width at 16:9; on wide screens (beyond ~2.2:1) the full height instead.
  // Whole multiples of 16×9 px, so F1 TV's 16:9 player fills the box with no sub-pixel seam.
  function boxFor(w0, h0) {
    // the dashboard's own box in its 'onboards' mode (a smaller picture, room for onboards below)
    if (dashBox && dashBox.w <= w0 && dashBox.h <= h0) return { x: 0, y: 0, w: dashBox.w, h: dashBox.h };
    let n = Math.round((w0 * RATIO) / 16);
    if (n * 9 > h0) n = Math.floor(h0 / 9);
    return { x: 0, y: 0, w: n * 16, h: n * 9 };
  }

  // The box is always at 0,0, so the control bar's rule means the same whether its fixed
  // position is relative to the viewport or (with the containment below) to the grid.
  function css(r) {
    const box = `position:fixed!important;left:${r.x}px!important;top:${r.y}px!important;right:auto!important;`
      + `bottom:auto!important;width:${r.w}px!important;height:${r.h}px!important;min-width:0!important;`
      + 'min-height:0!important;max-width:none!important;max-height:none!important;margin:0!important;'
      + 'transform:none!important;';
    // contain:paint makes the grid the containing block of anything else F1 TV positions fixed
    // inside it (menus, overlays) and clips it to the box, so none of it covers the dashboard.
    return `${PINNED}{${box}}\n${GRID}:not(#fone-none){contain:paint!important}`;
  }

  function applyStyle() {
    if (!style) {
      style = document.createElement('style');
      style.id = 'fone-layout-style';
    }
    const text = css(rect);
    if (style.textContent !== text) style.textContent = text;
    // Last in <head>: after F1 TV's own sheets, which its chunks keep adding as they load.
    if (!style.isConnected) (document.head || html).appendChild(style);
  }

  // ---- the dashboard -----------------------------------------------------------------------

  // Just below the player: the grid keeps the box, the iframe gets every pixel around it.
  function frameCss() {
    const z = parseInt(grid ? getComputedStyle(grid).zIndex : '', 10);
    return 'position:fixed!important;left:0!important;top:0!important;width:100%!important;height:100%!important;'
      + 'margin:0!important;padding:0!important;border:0!important;background:transparent!important;'
      + `display:block!important;visibility:visible!important;z-index:${z > 0 ? z - 1 : 99}!important;`;
  }

  function ensureFrame() {
    if (!frame) {
      frame = document.createElement('iframe');
      frame.id = 'fone-dash';
      frame.title = 'fone stats';
      frame.src = DASH_URL;
      frame.addEventListener('load', post);
    }
    frame.style.cssText = frameCss();
    // Outside F1 TV's #app, which it makes inert while the player is open.
    if (!frame.isConnected) html.appendChild(frame);
  }

  function post() {
    const w = frame?.contentWindow;
    if (!active || !w) return;
    w.postMessage({ type: 'fone-layout', rect: { ...rect }, vw, vh, fullscreen: !!document.fullscreenElement }, DASH_ORIGIN);
  }

  // ---- the dashboard's onboards ---------------------------------------------------------------

  const OB_MAX = 4; // the dashboard fits up to 3 onboard slots; one spare
  let obLast = null; // the slots forwarded last (diagnostics)

  // -> [{x, y, w, h, channelId}] or null (refused as a whole)
  function onboardSlots(slots) {
    if (!Array.isArray(slots) || slots.length > OB_MAX) return null;
    const W = html.clientWidth;
    const H = html.clientHeight;
    const num = (v) => typeof v === 'number' && Number.isFinite(v);
    const out = [];
    for (const s of slots) {
      if (!s || typeof s !== 'object') return null;
      const { x, y, w, h, channelId } = s;
      if (![x, y, w, h].every(num) || w <= 0 || h <= 0) return null;
      if (x < -1 || y < -1 || x + w > W + 1 || y + h > H + 1) return null; // (1 px of rounding)
      if (channelId !== null && !(Number.isInteger(channelId) && channelId > 0)) return null;
      out.push({ x, y, w, h, channelId });
    }
    return out;
  }

  function sendOnboards(slots) {
    obLast = slots;
    document.dispatchEvent(new CustomEvent('fone-cmd', { detail: JSON.stringify({ cmd: 'onboards', slots }) }));
  }

  // ---- fone's own panel ----------------------------------------------------------------

  const panelEl = () => document.getElementById('fone-overlay')?.shadowRoot?.querySelector('.wrap') || null;

  // content.js pins its panel to the viewport's top-right corner, which now belongs to the
  // dashboard: keep it in the player box's corner instead, with the same insets.
  function placePanel() {
    const el = panelEl();
    if (panel && panel.el !== el) restorePanel();
    if (!el) return;
    if (!panel) {
      const cs = getComputedStyle(el);
      panel = { el, attr: el.hasAttribute('style'), top: el.style.top, right: el.style.right, dy: parseFloat(cs.top) || 0, dx: parseFloat(cs.right) || 0 };
    }
    el.style.top = `${rect.y + panel.dy}px`;
    el.style.right = `${vw - rect.x - rect.w + panel.dx}px`;
  }

  function restorePanel() {
    if (!panel) return;
    const { el } = panel;
    el.style.top = panel.top;
    el.style.right = panel.right;
    if (!panel.attr && !el.getAttribute('style')) el.removeAttribute('style');
    panel = null;
  }

  // ---- fullscreen -------------------------------------------------------------------------
  // F1 TV's fullscreen (its button, and a double-click on the video) is requestFullscreen() on
  // div.video-grid: the video alone. The browser's own !important styles then stretch the grid
  // over the screen and nothing outside it is drawn, the dashboard included. With the split
  // on we make the whole page fullscreen instead: player box and dashboard fill the screen.

  function togglePageFullscreen() {
    if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
    else html.requestFullscreen({ navigationUI: 'hide' }).catch(() => {});
  }

  function playerAlone() {
    const fs = document.fullscreenElement;
    return !!(fs && fs !== html && grid && (fs.contains(grid) || grid.contains(fs)));
  }

  // The player alone is fullscreen (a path we didn't catch, or split turned on while it was):
  // swap it for page fullscreen. That needs a user activation; Chrome gives the page a fresh
  // one when it grants fullscreen, and a click or key that turned the split on carries one.
  // Without it, leave the player fullscreen: the split comes back when fullscreen ends.
  function fixFullscreen() {
    if (!playerAlone() || !navigator.userActivation?.isActive) return;
    const asked = document.fullscreenElement;
    document.exitFullscreen()
      .then(() => html.requestFullscreen({ navigationUI: 'hide' }))
      .then(() => { swapped = asked; })
      .catch(() => {});
  }

  // F1 TV's button, caught before its own handler. layout_page.js (main world, optional) marks
  // <html data-fone-fs> when it redirects F1 TV's own requestFullscreen call; that also covers
  // the double-click, so then the button is left to F1 TV.
  window.addEventListener('click', (e) => {
    if (!active || !e.isTrusted || html.hasAttribute('data-fone-fs')) return;
    if (!e.composedPath().some((n) => n.matches?.(FS_BUTTON))) return;
    e.preventDefault();
    e.stopImmediatePropagation();
    togglePageFullscreen();
  }, true);

  document.addEventListener('fullscreenchange', () => {
    // F1 TV learns that its fullscreen ended from a 'fullscreenchange' on the element it made
    // fullscreen. After a swap, that event came while the page was fullscreen already, so F1 TV
    // still believes it is: pass the real end on (its fullscreen button state resets).
    if (!document.fullscreenElement && swapped) {
      swapped.dispatchEvent(new Event('fullscreenchange'));
      swapped = null;
    }
    if (!active) return;
    fixFullscreen();
    relayout(true);
  });

  // ---- applying -----------------------------------------------------------------------

  function relayout(force) {
    if (!active) return;
    const w = html.clientWidth;
    const h = html.clientHeight;
    if (!force && w === vw && h === vh) return;
    vw = w;
    vh = h;
    rect = boxFor(w, h);
    applyStyle();
    // While the player alone is fullscreen the browser stretches the grid over the screen and
    // our box would only misplace the control bar (and our panel) inside it: stand aside.
    const alone = playerAlone();
    style.media = alone ? 'not all' : '';
    if (alone) restorePanel();
    else placePanel();
    post();
  }
  const onResize = () => relayout(false);

  function activate() {
    try { activateUnsafe(); } catch (e) { lastError = String(e?.stack || e).slice(0, 400); throw e; }
  }

  function activateUnsafe() {
    active = true;
    grid = document.querySelector(GRID);
    html.setAttribute('data-fone-layout', 'split'); // read by layout_page.js
    vw = vh = 0;
    relayout(false);
    ensureFrame();
    window.addEventListener('resize', onResize);
    fixFullscreen();
  }

  function deactivate() {
    clearTimeout(dropTimer);
    dropTimer = 0;
    if (!active) return;
    active = false;
    sendOnboards([]); // the onboards go with the dashboard
    window.removeEventListener('resize', onResize);
    html.removeAttribute('data-fone-layout');
    style?.remove();
    frame?.remove();
    frame = null;
    restorePanel();
    grid = rect = dashBox = null;
    vw = vh = 0;
  }

  // On every DOM change while the split is wanted: cheap unless something we care about moved.
  function sync() {
    if (!want) return;
    const g = document.querySelector(GRID);
    if (!g) {
      // Back to the catalogue, or a player being rebuilt: wait a moment before tearing down,
      // so a rebuild doesn't reload the dashboard.
      if (active && !dropTimer) dropTimer = setTimeout(() => { dropTimer = 0; if (!document.querySelector(GRID)) deactivate(); }, GRACE);
      return;
    }
    clearTimeout(dropTimer);
    dropTimer = 0;
    if (!active) { activate(); return; }
    if (g !== grid) {
      grid = g;
      frame.style.cssText = frameCss();
    }
    if (!style.isConnected) applyStyle();
    if (!frame.isConnected) ensureFrame();
    if (!playerAlone() && panelEl() !== (panel?.el ?? null)) placePanel(); // content.js's panel appeared
  }

  function watch(on) {
    if (on && !watcher) {
      watcher = new MutationObserver(sync);
      watcher.observe(html, { childList: true, subtree: true });
    } else if (!on && watcher) {
      watcher.disconnect();
      watcher = null;
    }
  }

  function setWant(on) {
    want = on;
    watch(on);
    if (on) sync();
    else deactivate();
  }

  function choose(on) {
    chosen = true;
    try {
      chrome.storage.local.set({ layout: on ? 'split' : 'off' }).catch(() => {});
    } catch { /* extension reloaded; this script is orphaned */ }
    setWant(on);
  }

  // ---- API, hotkey, dashboard commands -------------------------------------------------------

  const enable = () => choose(true);
  const disable = () => choose(false);
  const toggle = () => { choose(!want); return want; };
  const isOn = () => want;

  function popOut() {
    disable(); // the stats move to their own window; the player gets the tab back
    try {
      chrome.runtime.sendMessage({ target: 'background', type: 'popout-dashboard' }).catch(() => {});
    } catch { /* extension reloaded */ }
  }

  window.FoneLayout = Object.freeze({ enable, disable, toggle, isOn, popOut });

  // Diagnostics, readable from the page's own world (DevTools console, tests):
  // document.dispatchEvent(new Event('fone-layout-debug')), then <html data-fone-layout-debug>.
  let lastError = null;
  document.addEventListener('fone-layout-debug', () => {
    html.setAttribute('data-fone-layout-debug', JSON.stringify({
      want, chosen, active, grid: !!document.querySelector(GRID), rect, vw, vh,
      frame: !!frame?.isConnected, style: !!style?.isConnected, error: lastError, onboards: obLast,
    }));
  });
  html.setAttribute('data-fone-layout-ready', '');

  window.addEventListener('keydown', (e) => {
    // Plain S, like fone's other one-key shortcuts; F1 TV's player only uses Space and the
    // arrows. e.code, so it's the same key under any layout or input method.
    if (e.code !== 'KeyS' || e.altKey || e.shiftKey || e.ctrlKey || e.metaKey || e.repeat) return;
    const t = e.composedPath()[0];
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    if (!want && !document.querySelector(GRID)) return; // no player here: nothing to show it with
    e.preventDefault();
    e.stopPropagation();
    toggle();
  }, true);

  // O flips the dashboard between its 'stats' and 'onboards' modes (chrome.storage.local
  // 'statsMode', which the dashboard follows), while the split is on.
  window.addEventListener('keydown', (e) => {
    if (e.code !== 'KeyO' || e.altKey || e.shiftKey || e.ctrlKey || e.metaKey || e.repeat || !active) return;
    const t = e.composedPath()[0];
    if (t && (t.isContentEditable || /^(INPUT|TEXTAREA|SELECT)$/.test(t.tagName))) return;
    e.preventDefault();
    e.stopPropagation();
    try {
      chrome.storage.local.get('statsMode').then(({ statsMode }) => chrome.storage.local.set({
        statsMode: statsMode === 'onboards' ? 'stats' : 'onboards',
      })).catch(() => {});
    } catch { /* extension reloaded */ }
  }, true);

  window.addEventListener('message', (e) => {
    if (!frame || e.source !== frame.contentWindow || e.origin !== DASH_ORIGIN || e.data?.type !== 'fone-layout-cmd') return;
    const { cmd } = e.data;
    if (cmd === 'off') disable();
    else if (cmd === 'popout') popOut();
    else if (cmd === 'fullscreen') togglePageFullscreen();
    else if (cmd === 'rect') post();
    else if (cmd === 'player') {
      // whole 16x9 steps, between 40% and 80% of the width, on screen; anything else: ours
      const b = e.data.box;
      const w = html.clientWidth, h = html.clientHeight;
      const ok = b && Number.isInteger(b.w) && Number.isInteger(b.h) && b.w % 16 === 0 && b.w / 16 === b.h / 9
        && b.w >= w * 0.4 && b.w <= w * 0.8 && b.h <= h;
      const next = ok ? { w: b.w, h: b.h } : null;
      if (JSON.stringify(next) !== JSON.stringify(dashBox)) {
        dashBox = next;
        relayout(true);
      }
    }
    else if (cmd === 'onboards' && active) {
      const slots = onboardSlots(e.data.slots);
      if (slots) sendOnboards(slots);
    }
  });

  chrome.storage.local.get('layout').then(({ layout }) => {
    if (!chosen && layout === 'split') setWant(true);
  }).catch(() => {});
})();
