import threading
import socket
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from unittest.mock import Mock, patch

import pytest
import requests

from src.enrichment import dynamic_enricher as enrichment
_ORIGINAL_REQUEST = requests.sessions.Session.request
_ORIGINAL_CONNECT = socket.socket.connect
_ORIGINAL_GETADDRINFO = socket.getaddrinfo


def test_search_candidate_cannot_fetch_loopback():
    hits = []

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            hits.append(self.path)
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<html>Harmless local sentinel</html>")

        def log_message(self, *args):
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        url = f"http://127.0.0.1:{server.server_port}/sentinel"
        expected_url = url

        def only_local_request(session, method, url, **kwargs):
            assert url == expected_url, "PoC may only contact its own loopback server"
            session.trust_env = False
            return _ORIGINAL_REQUEST(session, method, url, **kwargs)

        def only_local_connect(sock, address):
            assert address == ("127.0.0.1", server.server_port)
            return _ORIGINAL_CONNECT(sock, address)

        def only_local_dns(host, port, *args, **kwargs):
            assert host == "127.0.0.1" and port == server.server_port
            return _ORIGINAL_GETADDRINFO(host, port, *args, **kwargs)

        with patch.object(requests.sessions.Session, "request", only_local_request), \
                patch.object(socket.socket, "connect", only_local_connect), \
                patch.object(socket, "getaddrinfo", only_local_dns):
            result, reason = enrichment.extract_candidate_provider(
                {"url": url}, "Example role", "Example company", "Example city", "serper"
            )
        assert result is None
        assert hits == [], f"Internal server was reached: {hits}"
        assert reason.startswith("request_error:")
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=2)


@pytest.mark.parametrize("url", [
    "http://127.0.0.1/", "http://[::1]/", "http://169.254.169.254/latest/meta-data/",
    "http://10.0.0.1/", "http://192.168.1.1/", "file:///etc/passwd",
    "https://user:password@example.com/",
])
def test_generic_enrichment_blocks_private_and_non_http_urls(url):
    with patch("src.ops.safe_http._pinned_get") as request:
        assert enrichment.fetch(url) == (None, None)
        assert enrichment.fetch_diagnostic(url)[0] is None
        request.assert_not_called()


def test_enrichment_rejects_public_redirect_to_private_target():
    response = Mock(status_code=302, url="https://example.com/job")
    response.headers = {"Location": "http://127.0.0.1/secret"}
    with patch("src.ops.safe_http.socket.getaddrinfo", return_value=[
        (2, 1, 6, "", ("93.184.216.34", 443))
    ]), patch("src.ops.safe_http._pinned_get", return_value=response) as request:
        assert enrichment.fetch_diagnostic("https://example.com/job")[0] is None
        assert request.call_count == 1
        response.close.assert_called_once()


def test_enrichment_closes_oversized_response():
    response = Mock(status_code=200, url="https://example.com/job", encoding="utf-8")
    response.headers = {"Content-Type": "text/html"}
    response._safe_http_deadline = None
    response.iter_content.return_value = [b"x" * (2 * 1024 * 1024 + 1)]
    with patch.object(enrichment, "safe_get", return_value=response):
        assert enrichment.fetch_diagnostic(response.url)[2] == "response_too_large"
        response.close.assert_called_once()
