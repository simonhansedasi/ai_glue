"""Token auth: AIGLUE_TOKEN as the Basic-auth password on everything except /proxy/*."""
import base64
import json
import os
import sys
from unittest.mock import MagicMock, patch

import pytest

os.environ["AIGLUE_DB"] = "/tmp/aiglue_test_auth.db"
sys.path.insert(0, os.path.dirname(os.path.dirname(__file__)))

from src.store import init_db
import app as flask_app
from routes import dashboard

init_db()


@pytest.fixture
def client():
    flask_app.app.config["TESTING"] = True
    with flask_app.app.test_client() as c:
        yield c


def basic(pw, user="anyone"):
    return {"Authorization": "Basic " + base64.b64encode(f"{user}:{pw}".encode()).decode()}


def test_open_when_token_unset(client, monkeypatch):
    monkeypatch.delenv("AIGLUE_TOKEN", raising=False)
    assert client.get("/api/stats").status_code == 200
    assert client.get("/export").status_code == 200


@pytest.mark.parametrize("path", ["/", "/executive", "/aggregate", "/api/stats", "/api/summary", "/export", "/export/training"])
def test_reads_need_credentials(client, monkeypatch, path):
    monkeypatch.setenv("AIGLUE_TOKEN", "s3cret")
    r = client.get(path)
    assert r.status_code == 401 and "Basic" in r.headers["WWW-Authenticate"]
    assert client.get(path, headers=basic("wrong")).status_code == 401
    assert client.get(path, headers=basic("s3cret")).status_code == 200


def test_write_endpoint_needs_credentials(client, monkeypatch):
    monkeypatch.setenv("AIGLUE_TOKEN", "s3cret")
    assert client.post("/flags/mark-reviewed").status_code == 401
    assert client.post("/flags/mark-reviewed", headers=basic("wrong")).status_code == 401
    with patch("routes.dashboard._load_config", return_value={}):
        assert client.post("/flags/mark-reviewed", headers=basic("s3cret")).status_code == 204


def test_empty_password_is_rejected(client, monkeypatch):
    monkeypatch.setenv("AIGLUE_TOKEN", "s3cret")
    assert client.get("/api/stats", headers=basic("")).status_code == 401


def test_proxy_is_exempt(client, monkeypatch):
    monkeypatch.setenv("AIGLUE_TOKEN", "s3cret")
    up = MagicMock(status_code=200, content=b'{"content":[{"type":"text","text":"x"}],"usage":{}}',
                   headers={"content-type": "application/json"})
    with patch("requests.request", return_value=up), patch("routes.proxy.check", return_value=[]):
        r = client.post("/proxy/anthropic/v1/messages", data=json.dumps(
            {"model": "claude-haiku-4-5-20251001", "max_tokens": 5, "messages": [{"role": "user", "content": "hi"}]}),
            headers={"Content-Type": "application/json", "x-api-key": "k"})
    assert r.status_code == 200


def test_child_fetches_send_the_token(monkeypatch):
    resp = MagicMock()
    resp.json.return_value = {"recent": []}
    for token, expected in (("tok", ("aiglue", "tok")), (None, None)):
        monkeypatch.delenv("AIGLUE_TOKEN", raising=False)
        if token:
            monkeypatch.setenv("AIGLUE_TOKEN", token)
        with patch("routes.dashboard.http.get", return_value=resp) as get:
            dashboard._fetch_child("child", "http://child:5010")
        assert get.call_args[1]["auth"] == expected
