import curl_cffi.requests

from schedule_bot import browser_client


class FakeResponse:
    pass


class FakeSession:
    instances: list["FakeSession"] = []

    def __init__(self, *args, **kwargs):
        self.closed = False
        self.get_calls: list[tuple] = []
        self.raise_on_referer = False
        FakeSession.instances.append(self)

    def get(self, url, **kwargs):
        self.get_calls.append((url, kwargs))
        if self.raise_on_referer and "Referer" not in kwargs.get("headers", {}):
            raise ConnectionError("boom")
        return FakeResponse()

    def close(self):
        self.closed = True

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()
        return False


def test_fetch_url_closes_session_after_success(monkeypatch):
    FakeSession.instances.clear()
    monkeypatch.setattr(curl_cffi.requests, "Session", FakeSession)

    browser_client.fetch_url("https://example.com/schedule.pdf", timeout=5)

    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].closed is True


def test_fetch_url_closes_session_when_referer_fetch_raises(monkeypatch):
    FakeSession.instances.clear()

    class RaisingSession(FakeSession):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            self.raise_on_referer = True

    monkeypatch.setattr(curl_cffi.requests, "Session", RaisingSession)

    try:
        browser_client.fetch_url(
            "https://example.com/schedule.pdf", timeout=5, referer="https://example.com"
        )
    except ConnectionError:
        pass

    assert len(FakeSession.instances) == 1
    assert FakeSession.instances[0].closed is True
