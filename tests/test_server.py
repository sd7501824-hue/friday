import json
import sys
from pathlib import Path

# ensure project root is on sys.path so imports work under pytest
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

import server


def test_home():
    client = server.app.test_client()
    res = client.get("/")
    assert res.status_code == 200
    assert b"FRIDAY Server Running" in res.data


def test_ask():
    client = server.app.test_client()
    payload = {"message": "testing"}
    res = client.post("/ask", data=json.dumps(payload), content_type="application/json")
    assert res.status_code == 200
    j = res.get_json()
    assert isinstance(j, dict)
    assert j.get("reply") == "FRIDAY: testing"
