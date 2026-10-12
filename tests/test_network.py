import asyncio
from types import MethodType
from contextlib import asynccontextmanager
from unittest.mock import AsyncMock, MagicMock

import pytest
import aiohttp

import twitch as twitch_module
import websocket as websocket_module
from twitch import Twitch
from utils import RateLimiter
from websocket import Websocket
from exceptions import GQLException
from constants import State


# Websocket._backoff_connect

class FakeClock:
    def __init__(self):
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now


class FakeWSConnect:
    """
    Fake session.ws_connect: each entry of 'outcomes' is either an exception to raise
    on connect, or None for a successful connection.
    """
    def __init__(self, outcomes):
        self.outcomes = list(outcomes)
        self.connects = 0

    def __call__(self, url, **kwargs):
        @asynccontextmanager
        async def ctx():
            self.connects += 1
            outcome = self.outcomes.pop(0)
            if outcome is not None:
                raise outcome
            yield MagicMock(name=f"ws{self.connects}")
        return ctx()


def make_websocket(monkeypatch, outcomes):
    session = MagicMock()
    session.ws_connect = FakeWSConnect(outcomes)
    ws = Websocket.__new__(Websocket)
    ws._idx = 0
    ws._twitch = MagicMock()
    ws._twitch.settings.proxy = None
    ws._twitch.get_session = AsyncMock(return_value=session)
    sleeps: list[float] = []

    async def fake_sleep(delay):
        sleeps.append(delay)

    clock = FakeClock()
    monkeypatch.setattr(websocket_module.asyncio, "sleep", fake_sleep)
    monkeypatch.setattr(websocket_module, "monotonic", clock)
    return ws, session.ws_connect, sleeps, clock


def run_connect(ws, connections: int, uptimes: list[float], clock: FakeClock):
    async def runner():
        count = 0
        gen = ws._backoff_connect("wss://example", maximum=180)
        async for _ in gen:
            clock.now += uptimes[count]
            count += 1
            if count >= connections:
                break
        await gen.aclose()
    asyncio.run(runner())


def test_backoff_sleeps_after_short_connections(monkeypatch):
    ws, connect, sleeps, clock = make_websocket(monkeypatch, [None, None, None])
    run_connect(ws, 3, [1, 1, 1], clock)
    # we sleep before every reconnect, and the backoff keeps growing
    assert len(sleeps) == 2
    assert 0.9 <= sleeps[0] <= 1.1
    assert 1.8 <= sleeps[1] <= 2.2


def test_backoff_resets_after_stable_connection(monkeypatch):
    ws, connect, sleeps, clock = make_websocket(monkeypatch, [None, None, None, None])
    run_connect(ws, 4, [1, 1, 120, 1], clock)
    assert len(sleeps) == 3
    assert 1.8 <= sleeps[1] <= 2.2
    # the third connection was up for 2 minutes, so the backoff starts anew
    assert 0.9 <= sleeps[2] <= 1.1


def test_backoff_sleeps_after_connection_errors(monkeypatch):
    ws, connect, sleeps, clock = make_websocket(
        monkeypatch,
        [aiohttp.ClientConnectionError(), asyncio.TimeoutError(), None],
    )
    run_connect(ws, 1, [1], clock)
    assert connect.connects == 3
    assert len(sleeps) == 2
    assert 0.9 <= sleeps[0] <= 1.1
    assert 1.8 <= sleeps[1] <= 2.2


# Twitch.request

class FakeResponse:
    def __init__(self, status: int, headers=None, json_data=None, content_type="application/json"):
        self.status = status
        self.headers = headers or {}
        self.content_type = content_type
        self._json = json_data
        self.released = False

    async def read(self) -> bytes:
        return b''

    async def json(self, *args, **kwargs):
        if self.content_type != "application/json":
            raise aiohttp.ContentTypeError(MagicMock(), ())
        return self._json

    def release(self):
        self.released = True

    def __repr__(self):
        return f"<FakeResponse {self.status}>"


def make_request_twitch(monkeypatch, responses):
    fake = MagicMock()
    fake.settings.proxy = None
    fake.gui.close_requested = False
    session = MagicMock()
    session.timeout.total = 10
    responses = list(responses)

    async def session_request(method, url, **kwargs):
        return responses.pop(0)

    session.request = session_request
    fake.get_session = AsyncMock(return_value=session)

    async def passthrough(coro):
        return await coro

    fake.gui.coro_unless_closed = passthrough
    waits: list[float] = []

    async def fake_wait_for(aw, timeout):
        waits.append(timeout)
        aw.close()  # the never-ending 'wait_until_closed' coroutine
        raise asyncio.TimeoutError()

    monkeypatch.setattr(twitch_module.asyncio, "wait_for", fake_wait_for)
    return fake, waits


def do_request(fake) -> int:
    async def runner():
        async with Twitch.request(fake, "GET", "https://example.com") as response:
            return response.status
    return asyncio.run(runner())


def test_request_429_honours_retry_after(monkeypatch):
    fake, waits = make_request_twitch(
        monkeypatch, [FakeResponse(429, {"Retry-After": "7"}), FakeResponse(200)]
    )
    assert do_request(fake) == 200
    assert waits == [7.0]
    fake.print.assert_not_called()


def test_request_429_without_retry_after_backs_off(monkeypatch):
    fake, waits = make_request_twitch(
        monkeypatch, [FakeResponse(429), FakeResponse(429), FakeResponse(204)]
    )
    assert do_request(fake) == 204
    assert len(waits) == 2
    assert 0.9 <= waits[0] <= 1.1
    assert 1.8 <= waits[1] <= 2.2


def test_request_4xx_is_passed_through(monkeypatch):
    fake, waits = make_request_twitch(monkeypatch, [FakeResponse(404)])
    assert do_request(fake) == 404
    assert waits == []


# Twitch.gql_request

def make_gql_twitch(response: FakeResponse):
    fake = MagicMock()
    fake._qgl_limiter = RateLimiter(capacity=5, window=1)
    fake.get_auth = AsyncMock(return_value=MagicMock())

    @asynccontextmanager
    async def request(method, url, **kwargs):
        yield response

    fake.request = request
    fake._auth_failed = MethodType(Twitch._auth_failed, fake)
    return fake


def do_gql(fake):
    async def runner():
        try:
            return await Twitch.gql_request(fake, MagicMock())
        finally:
            # don't leave the limiter's reset task pending
            if fake._qgl_limiter._reset_task is not None:
                fake._qgl_limiter._reset_task.cancel()
    return asyncio.run(runner())


def test_gql_401_invalidates_auth_and_restarts():
    fake = make_gql_twitch(FakeResponse(401, json_data={"error": "Unauthorized"}))
    with pytest.raises(GQLException):
        do_gql(fake)
    fake._auth_state.invalidate.assert_called_once()
    fake.change_state.assert_called_once_with(State.RESTART)


def test_gql_json_401_error_invalidates_auth():
    fake = make_gql_twitch(FakeResponse(
        200,
        json_data={"error": "Unauthorized", "status": 401, "message": "token is invalid"},
    ))
    with pytest.raises(GQLException):
        do_gql(fake)
    fake.change_state.assert_called_once_with(State.RESTART)


def test_gql_non_json_response():
    fake = make_gql_twitch(FakeResponse(200, content_type="text/html"))
    with pytest.raises(GQLException):
        do_gql(fake)
    fake.change_state.assert_not_called()


@pytest.mark.parametrize(
    "response_json",
    [
        {"errors": [{"message": "server error"}], "data": {"user": {}}},  # no path
        {"errors": [{"message": "server error", "path": ["user"]}]},  # no data
        {"errors": [{"message": "server error", "path": []}], "data": None},
        {"errors": [{"message": "server error", "path": ["a", "b"]}], "data": {}},
    ],
)
def test_gql_server_error_edge_cases(response_json):
    fake = make_gql_twitch(FakeResponse(200, json_data=response_json))
    with pytest.raises(GQLException):
        do_gql(fake)


def test_gql_server_error_nullifies_path():
    fake = make_gql_twitch(FakeResponse(200, json_data={
        "errors": [{"message": "server error", "path": ["user", "stream"]}],
        "data": {"user": {"stream": {"id": 1}}},
    }))
    assert do_gql(fake)["data"] == {"user": {"stream": None}}
