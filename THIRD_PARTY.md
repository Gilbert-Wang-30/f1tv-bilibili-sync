# Third-party software

fone itself is MIT (see `LICENSE`). It ships, pulls in or runs these:

## Vendored in this repository

| | Licence | Where |
|---|---|---|
| **hls.js** 1.7.3, light build (video-dev/hls.js) | Apache-2.0 | `extension/vendor/hls.light.min.js`; licence text in `extension/vendor/LICENSE.hls.js` |

## Python packages (installed into the server image by pip, see `server/requirements.txt`)

| | Licence |
|---|---|
| fastapi | MIT |
| uvicorn | BSD-3-Clause |
| httpx | BSD-3-Clause |
| numpy | BSD-3-Clause |
| websockets | BSD-3-Clause |
| brotli | MIT |
| tzdata | Apache-2.0 |

## In the server's Docker image (`server/Dockerfile`, Alpine packages)

| | Licence | Used for |
|---|---|---|
| ffmpeg | LGPL-2.1+ / GPL-2+ (as built by Alpine) | audio extraction, video remux, decoding for sponsor-clip detection and backfill |
| tesseract-ocr (+ English data) | Apache-2.0 | reading MultiViewer's "Track time" clock off the streamer's stream |

## Run as separate containers (not vendored, not modified)

| | Licence | Used for |
|---|---|---|
| **BililiveRecorder** (录播姬), Docker image `bililive/recorder` | GPL-3.0 | recording the Bilibili room (`docker-compose.yml`, `recorder/`) |
| **Playwright** Python image `mcr.microsoft.com/playwright/python` | Apache-2.0 | the end-to-end tests only (`e2e/`) |

The licences above are those published by each project at the time of writing; check the
project's own licence file for the authoritative text.
