// Script of the stub dashboard (layout_dashboard_stub.html): see there.
'use strict';
window.__stub = { born: Math.random().toString(36).slice(2), msgs: [], clicks: [] };
const $ = (id) => document.getElementById(id);

function place(el, x, y, w, h) {
  Object.assign(el.style, { display: w > 0 && h > 0 ? 'block' : 'none', left: `${x}px`, top: `${y}px`, width: `${w}px`, height: `${h}px` });
}

window.addEventListener('message', (e) => {
  if (e.data?.type !== 'fone-layout') return;
  __stub.msgs.push({ data: e.data, origin: e.origin, fromParent: e.source === window.parent, iw: innerWidth, ih: innerHeight });
  const { rect: r, vw, vh, fullscreen } = e.data;
  place($('right'), r.x + r.w, 0, vw - r.x - r.w, vh); // the full-height column right of the box
  place($('bottom'), 0, r.y + r.h, r.x + r.w, vh - r.y - r.h); // under the box
  $('r').textContent = `${r.x},${r.y} ${r.w}×${r.h}`;
  $('v').textContent = `${vw}×${vh}`;
  $('f').textContent = String(fullscreen);
  $('n').textContent = String(__stub.msgs.length);
});

document.addEventListener('click', (e) => { __stub.clicks.push([e.clientX, e.clientY, e.target.id || e.target.tagName]); });
const cmd = (c) => () => parent.postMessage({ type: 'fone-layout-cmd', cmd: c }, '*');
$('fs').addEventListener('click', cmd('fullscreen'));
$('off').addEventListener('click', cmd('off'));
$('pop').addEventListener('click', cmd('popout'));
