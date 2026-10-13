"""Playback diagnostics: POST from the extension, GET to read them back (diag.py)."""
import json
import time

from fastapi import FastAPI
from fastapi.testclient import TestClient

from fone.diag import DiagStore, make_router


def client_for(store):
    app = FastAPI()
    app.include_router(make_router(store))
    return TestClient(app)


def test_events_round_trip_and_filter(tmp_path):
    store = DiagStore(tmp_path)
    c = client_for(store)
    t = time.time()
    body = {"client": "mac-1", "build": "0.2.15", "platform": "macOS",
            "events": [{"t": t, "type": "snap", "drift": 0.01}, {"t": t + 1, "type": "wake", "slept": 3600}]}
    assert c.post("/api/diag", json=body).json() == {"ok": True, "stored": 2}
    c.post("/api/diag", json={"client": "win", "events": [{"t": t + 2, "type": "snap"}]})
    got = c.get("/api/diag", params={"client": "mac-1"}).json()
    assert [e["type"] for e in got["events"]] == ["snap", "wake"]
    assert got["events"][1]["slept"] == 3600 and got["events"][1]["build"] == "0.2.15"
    assert set(got["clients"]) == {"mac-1", "win"} and got["clients"]["mac-1"]["platform"] == "macOS"
    assert [e["type"] for e in c.get("/api/diag", params={"type": "wake"}).json()["events"]] == ["wake"]
    assert len(c.get("/api/diag", params={"since": t + 0.5}).json()["events"]) == 2
    assert len(c.get("/api/diag", params={"limit": 1}).json()["events"]) == 1


def test_survives_a_restart_and_caps_oversized_events(tmp_path):
    store = DiagStore(tmp_path)
    store_events = [{"t": 1.0, "type": "snap", "blob": "x" * 10000}, {"t": 2.0, "type": "seek"}]
    c = client_for(store)
    c.post("/api/diag", json={"client": "c", "events": store_events})
    again = DiagStore(tmp_path)  # a server restart
    evs = again.query(None, None, None, 10)
    assert [e["type"] for e in evs] == ["snap", "seek"] and evs[0]["truncated"] > 10000 and "blob" not in evs[0]
    files = list(tmp_path.glob("*.jsonl"))
    assert len(files) == 1 and all(json.loads(line)["client"] == "c" for line in files[0].read_text().splitlines())


def test_rejects_garbage(tmp_path):
    c = client_for(DiagStore(tmp_path))
    assert c.post("/api/diag", json={"events": []}).status_code == 422  # no client
    assert c.post("/api/diag", json={"client": "c", "events": [{}] * 500}).status_code == 422  # too many
