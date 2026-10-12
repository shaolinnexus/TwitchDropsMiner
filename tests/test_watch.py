import asyncio
from contextlib import asynccontextmanager
from unittest.mock import MagicMock

import aiohttp

from channel import Channel, Stream


PLAYLIST_URL = "https://video-weaver.example/v1/playlist/abc.m3u8"


def chunk_url(n: int) -> str:
    return f"https://video-edge.example/v1/segment/{n}.ts"


def playlist(*numbers: int) -> str:
    lines = ["#EXTM3U", "#EXT-X-TARGETDURATION:6"]
    for n in numbers:
        lines.append("#EXTINF:2.000,live")
        lines.append(chunk_url(n))
    return "\n".join(lines)


class FakeResponse:
    def __init__(self, status: int, text: str = ''):
        self.status = status
        self._text = text

    async def text(self, *args, **kwargs) -> str:
        return self._text


class FakeTwitch:
    def __init__(self):
        self.settings = MagicMock(available_drops_check=False)
        self.gui = MagicMock()
        self.playlist_response: FakeResponse | Exception = FakeResponse(200, playlist(1))
        self.head_statuses: dict[str, int] = {}
        self.heads: list[str] = []

    @asynccontextmanager
    async def request(self, method, url, **kwargs):
        if method == "GET":
            assert url == PLAYLIST_URL
            if isinstance(self.playlist_response, Exception):
                raise self.playlist_response
            yield self.playlist_response
        else:
            assert method == "HEAD"
            self.heads.append(url)
            yield FakeResponse(self.head_statuses.get(url, 200))


def make_channel() -> tuple[FakeTwitch, Channel, Stream]:
    twitch = FakeTwitch()
    channel = Channel(twitch, id=123, login="streamer")  # type: ignore[arg-type]
    stream = Stream(channel, id=456, game=None, viewers=10, title="title")
    stream._stream_url = PLAYLIST_URL  # skip the GQL/usher part
    channel._stream = stream
    return twitch, channel, stream


def send(channel: Channel) -> bool:
    return asyncio.run(channel.send_watch())


def test_sends_all_chunks_initially():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(1, 2, 3))
    assert send(channel) is True
    assert twitch.heads == [chunk_url(1), chunk_url(2), chunk_url(3)]
    assert stream._stream_url == PLAYLIST_URL


def test_only_new_chunks_are_sent():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(1, 2, 3))
    assert send(channel) is True
    twitch.heads.clear()
    twitch.playlist_response = FakeResponse(200, playlist(2, 3, 4, 5))
    assert send(channel) is True
    assert twitch.heads == [chunk_url(4), chunk_url(5)]


def test_newest_chunk_resent_if_nothing_new():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(1, 2, 3))
    assert send(channel) is True
    twitch.heads.clear()
    assert send(channel) is True
    assert twitch.heads == [chunk_url(3)]


def test_sent_record_is_bounded():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(*range(200)))
    assert send(channel) is True
    assert len(stream._sent_segments) == stream._sent_segments.maxlen


def test_failed_older_chunk_does_not_abort():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(1, 2, 3))
    twitch.head_statuses[chunk_url(1)] = 404
    assert send(channel) is True
    assert twitch.heads == [chunk_url(1), chunk_url(2), chunk_url(3)]
    assert stream._stream_url == PLAYLIST_URL


def test_failed_newest_chunk_fails_and_invalidates():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, playlist(1, 2, 3))
    twitch.head_statuses[chunk_url(3)] = 403
    assert send(channel) is False
    assert stream._stream_url is None


def test_empty_playlist_fails():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(200, "#EXTM3U\n#EXT-X-ENDLIST\n")
    assert send(channel) is False
    assert twitch.heads == []
    assert stream._stream_url is None


def test_playlist_error_status_invalidates():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(404, '')
    assert send(channel) is False
    assert stream._stream_url is None


def test_playlist_json_error_invalidates():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = FakeResponse(
        200, '[{"url": "x", "error": "Can not find channel", "error_code": "x"}]'
    )
    assert send(channel) is False
    assert stream._stream_url is None


def test_request_exception_is_handled():
    twitch, channel, stream = make_channel()
    twitch.playlist_response = aiohttp.ClientPayloadError("broken")
    assert send(channel) is False
    assert stream._stream_url is None


def test_no_stream():
    twitch, channel, stream = make_channel()
    channel._stream = None
    assert send(channel) is False
