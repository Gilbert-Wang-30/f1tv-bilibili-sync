# Contributing

fone is a personal project: it is built around one streamer's room and one person's home
server, and it changes when that setup does. Pull requests are welcome all the same,
especially ones that make it work for another streamer or another recorder. Please open an
issue first for anything larger than a fix, so the work isn't wasted on something that is
being rewritten.

## Before you open a PR

- Keep the change small and the comments in the style around it (the code explains *why*,
  not what the next line does).
- Run the tests that touch what you changed (below). CI runs the first two layers on every
  push and pull request; the third only runs on a machine with Docker and internet access.
- Don't commit anything from your own setup: server names, IPs, Tailscale hosts, credentials
  (`recorder/api.env` and `tools/publish.env` are gitignored for that reason).

## The three test layers

**1. Extension unit tests** (Node, no browser): the sync maths, the dashboard's layout and
clock, the danmaku filters and lanes, the self-update logic and the live car-position relay.
One file per module, `extension/<module>.test.js`, run from the repository root so
`livepos.test.js` can read `server/tests/fixtures`:

```bash
for t in core dashcore dmcore background livepos; do
  docker run --rm -v "$PWD":/r -w /r/extension node:22-alpine node $t.test.js
done
```

**2. Server tests** (pytest, in the server's Docker image, which has ffmpeg and tesseract):

```bash
docker build -t fone:dev server
docker run --rm -v "$PWD/server":/app -w /app -e PYTHONDONTWRITEBYTECODE=1 \
  fone:dev sh -c 'pip install -q pytest && python -m pytest -q -rs -p no:cacheprovider tests'
```

190+ tests; the ones that need a real recording (`-rs` lists them as skipped) run only
when one is mounted, see the README's Development section for the `FONE_*` switches.

**3. End to end** (`e2e/`): the real server and the real extension in headless Chromium,
all in Docker. These need Docker, a few GB for the Playwright image, and internet access:
`e2e/run.sh` looks up real F1 TV content, MultiViewer sessions and the live-timing archive
to anchor a replay the way production does (the commentary audio is synthetic). They are
slow (minutes) and run nowhere automatically.

```bash
./e2e/run.sh                  # replay, Tiledmedia, 2x, live, danmaku, onboards
./e2e/livepos/run.sh          # the live car-position relay against a mock F1 feed
DASH=real ./e2e/layout_run.sh # the split view on a mock F1 TV page
./e2e/dash_shots.sh           # dashboard screenshots in 20+ window sizes
```

## What a change usually touches

- Sync behaviour: `extension/core.js` (pure maths, tested in `core.test.js`) and
  `extension/offscreen.js` (the engine around it, covered by `e2e/run.sh`).
- The server's routes and data shapes: `docs/architecture.md` describes them; keep it true.
- A new F1 data source or format: note it in `docs/f1-data-sources.md`.
