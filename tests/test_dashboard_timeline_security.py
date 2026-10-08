from contextlib import redirect_stdout
from io import StringIO
from types import SimpleNamespace

from tests.dashboard_helpers import load_dashboard_helpers


def test_timeline_connection_closes_and_failure_is_surfaced_without_secret(monkeypatch):
    token = "synthetic-timeline-api-key"
    monkeypatch.setenv("GEMINI_API_KEY", token)
    dashboard = load_dashboard_helpers()
    timeline = dashboard["get_job_timeline_events"]
    messages = []
    timeline.__globals__["st"] = SimpleNamespace(error=messages.append)

    class BrokenConnection:
        closed = False

        def execute(self, *_args):
            raise RuntimeError(f"connection failed ?key={token}")

        def close(self):
            self.closed = True

    connection = BrokenConnection()
    timeline.__globals__["get_connection"] = lambda: connection
    output = StringIO()
    with redirect_stdout(output):
        assert timeline(7) == []

    assert connection.closed
    assert messages == ["Etkinlik geçmişi şu anda yüklenemiyor."]
    assert token not in output.getvalue()


def test_timeline_connection_closes_after_success():
    dashboard = load_dashboard_helpers()
    timeline = dashboard["get_job_timeline_events"]

    class Connection:
        closed = False

        def execute(self, *_args):
            return SimpleNamespace(fetchall=lambda: [("status_change", "now", "note")])

        def close(self):
            self.closed = True

    connection = Connection()
    timeline.__globals__["get_connection"] = lambda: connection
    assert timeline(7) == [("status_change", "now", "note")]
    assert connection.closed
