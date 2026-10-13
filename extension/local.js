// This install's own defaults. In the repo it's a placeholder, so no server address lives in
// the code: tools/publish_ext.sh writes the published copy from tools/publish.env
// (FONE_SERVERS), and an extension loaded straight from the repo asks for the server in its
// settings. Loaded before anything reads the defaults: background.js (importScripts), the
// content scripts (first in manifest.json) and options.html.
self.FONE_LOCAL = { servers: [] };
