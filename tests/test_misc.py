import os
import sys
import json
import asyncio
from types import SimpleNamespace

import pytest
import aiohttp
from yarl import URL

from gui import LoginData
from inventory import TimedDrop
from twitch import is_recoverable
from utils import redact, json_load, json_save
from exceptions import (
    ExitRequest, GQLException, LoginException, CaptchaRequired, MinerException
)


# TimedDrop._update_real_minutes

def _drop(current: int, required: int = 10) -> SimpleNamespace:
    return SimpleNamespace(
        real_current_minutes=current,
        extra_current_minutes=3,
        required_minutes=required,
        _on_state_changed=lambda: None,
    )


@pytest.mark.parametrize(
    "current, delta, expected",
    [
        (5, 2, 7),
        (5, -2, 3),
        (2, -5, 0),  # used to end up at -5
        (0, -1, 0),
        (8, 5, 10),
        (5, 5, 10),
        (10, 0, 10),
    ],
)
def test_update_real_minutes_clamps(current, delta, expected):
    drop = _drop(current)
    TimedDrop._update_real_minutes(drop, delta)
    assert drop.real_current_minutes == expected
    assert drop.extra_current_minutes == 0


# LoginData.username_valid

@pytest.mark.parametrize(
    "username, valid",
    [
        ("abc", True),
        ("Some_User_123", True),
        ("a" * 25, True),
        ("ab", False),
        ("a" * 26, False),
        ("bad name!", False),  # valid length, invalid characters - used to pass
        ("user.name", False),
        ("", False),
    ],
)
def test_username_valid(username, valid):
    assert LoginData(username, "password", '').username_valid() is valid


# redact

def test_redact_headers_and_payloads():
    text = str({
        "headers": {"Authorization": "OAuth abc123", "Client-Id": "public"},
        "data": {"device_code": "S1", "password": "it's S2", "token": "S3"},
        "json": {"refresh_token": "S4", "access_token": "S5"},
    })
    result = redact(text)
    for secret in ("abc123", "S1", "S2", "S3", "S4", "S5"):
        assert secret not in result
    assert "public" in result
    assert "Authorization" in result


def test_redact_json_websocket_message():
    message = '{"type":"LISTEN","data":{"topics":["a.1"],"auth_token":"SECRET\\"X"}}'
    result = redact(message)
    assert "SECRET" not in result
    assert '"topics":["a.1"]' in result


def test_redact_urls():
    url = URL("https://usher.ttvnw.net/api/channel/hls/x.m3u8?sig=abcdef&token=%7B%22a%22%7D")
    result = redact(f"Request: (url={url!r}, proxy={URL('http://user:pa55@proxy:8080')!r})")
    assert "abcdef" not in result
    assert "%7B" not in result
    assert "pa55" not in result and "user:" not in result
    assert "proxy:8080" in result
    # cookies
    assert "SEC" not in redact("'Set-Cookie': 'auth-token=SEC; Path=/'")


def test_redact_leaves_other_text_alone():
    text = "{'tokens': 'keep', 'user_code': 'ABCDEFGH', 'signatures_count': 1}"
    assert redact(text) == text


# json_load / json_save

DEFAULTS = {"a": 1, "b": "x"}


@pytest.mark.parametrize(
    "content",
    [
        "{not json",
        b"\xff\xfe\x00garbage",
        '{"a": {"__type": "datetime", "data": "nope"}}',  # TypeError from _deserialize
        "[1, 2, 3]",
    ],
)
def test_json_load_corrupted_falls_back(tmp_path, content):
    path = tmp_path / "settings.json"
    if isinstance(content, bytes):
        path.write_bytes(content)
    else:
        path.write_text(content, encoding="utf8")
    result = json_load(path, DEFAULTS)
    assert result == DEFAULTS
    assert result is not DEFAULTS
    assert not path.exists()
    assert (tmp_path / "settings.json.bad").exists()


def test_json_save_roundtrip(tmp_path):
    path = tmp_path / "settings.json"
    json_save(path, {"a": 2, "b": "y"})
    assert json_load(path, DEFAULTS) == {"a": 2, "b": "y"}
    assert not (tmp_path / "settings.json.new").exists()
    if sys.platform != "win32":
        assert (os.stat(path).st_mode & 0o777) == 0o600


def test_json_load_missing_uses_defaults(tmp_path):
    assert json_load(tmp_path / "missing.json", DEFAULTS) == DEFAULTS


# is_recoverable

@pytest.mark.parametrize(
    "exc, recoverable",
    [
        (MinerException(), True),
        (GQLException("service error"), True),
        (aiohttp.ClientPayloadError(), True),
        (asyncio.TimeoutError(), True),
        (KeyError("data"), True),
        (TypeError(), True),
        (json.JSONDecodeError("x", "", 0), True),
        (ExitRequest(), False),
        (LoginException(), False),
        (CaptchaRequired(), False),
        (aiohttp.ClientConnectorCertificateError(None, Exception()), False),
        (RuntimeError(), False),
    ],
)
def test_is_recoverable(exc, recoverable):
    assert is_recoverable(exc) is recoverable
