'use strict';
const DEFAULTS = {
  servers: self.FONE_LOCAL?.servers || [], // local.js: this install's, or none
  channel: '',
  autoHold: true,
};
const $ = (id) => document.getElementById(id);

function servers() {
  return $('servers').value.split('\n').map((s) => s.trim().replace(/\/$/, '')).filter(Boolean);
}

async function test() {
  $('out').textContent = 'Testing…';
  const lines = [];
  for (const base of servers()) {
    try {
      const h = await (await fetch(`${base}/api/health`, { signal: AbortSignal.timeout(3000) })).json();
      const chans = await (await fetch(`${base}/api/channels`)).json();
      lines.push(`✅ ${base} — ${h.files} recordings`);
      const sel = $('channel');
      const keep = sel.value;
      sel.length = 1;
      for (const c of chans) sel.add(new Option(`${c.name} (${c.files} files${c.live ? ', LIVE now' : ''})`, c.name));
      sel.value = keep;
      break;
    } catch (e) {
      lines.push(`❌ ${base} — ${e.message}`);
    }
  }
  $('out').textContent = lines.join('\n');
}

chrome.storage.sync.get(DEFAULTS).then((s) => {
  $('servers').value = s.servers.join('\n');
  $('autoHold').checked = s.autoHold;
  if (s.channel) $('channel').add(new Option(s.channel, s.channel));
  $('channel').value = s.channel;
  test();
});

$('save').addEventListener('click', async () => {
  await chrome.storage.sync.set({ servers: servers(), channel: $('channel').value, autoHold: $('autoHold').checked });
  $('out').textContent = 'Saved.';
});
$('test').addEventListener('click', test);
