import socket
import unittest
import requests
from unittest.mock import MagicMock, patch

from src.ops.safe_http import SafeHTTPError, _PinnedAdapter, _pinned_get, safe_get, validate_public_http_url


def _dns_record(address):
    family = socket.AF_INET6 if ":" in address else socket.AF_INET
    return (family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", (address, 0))


def _response(status, url, headers=None):
    response = MagicMock()
    response.status_code = status
    response.url = url
    response.headers = headers or {}
    return response


class TestSafeHTTP(unittest.TestCase):
    def test_pinned_https_uses_checked_ip_and_original_tls_identity(self):
        request = requests.Request("GET", "https://jobs.example.org:8443/job").prepare()
        adapter = _PinnedAdapter("93.184.216.34", "jobs.example.org")
        try:
            pool = adapter.get_connection_with_tls_context(request, True)
            self.assertEqual(pool.host, "93.184.216.34")
            self.assertEqual(pool.port, 8443)
            self.assertEqual(pool.assert_hostname, "jobs.example.org")
            connection = pool._new_conn()
            self.assertEqual(connection.server_hostname, "jobs.example.org")
            with patch("urllib3.connection.connection.create_connection") as connect:
                connection._new_conn()
                self.assertEqual(connect.call_args.args[0], ("93.184.216.34", 8443))
        finally:
            adapter.close()

    def test_pinned_transport_resolves_once_disables_proxies_and_closes_session(self):
        with patch("src.ops.safe_http.socket.getaddrinfo", side_effect=[
            [_dns_record("93.184.216.34")], [_dns_record("127.0.0.1")],
        ]) as dns, patch("src.ops.safe_http.requests.Session") as session_type:
            session = session_type.return_value
            response = MagicMock()
            session.get.return_value = response
            original_close = response.close
            result = _pinned_get("https://jobs.example.org/job", headers={"Host": "evil"}, stream=True)
            self.assertEqual(dns.call_count, 1)
            self.assertFalse(session.trust_env)
            self.assertEqual(session.get.call_args.kwargs["headers"]["Host"], "jobs.example.org")
            adapter = session.mount.call_args.args[1]
            self.assertEqual(adapter.address, "93.184.216.34")
            result.close()
            original_close.assert_called_once()
            session.close.assert_called_once()
            adapter.close()

    def test_rebound_private_address_is_rejected_before_transport(self):
        with patch("src.ops.safe_http.socket.getaddrinfo", side_effect=[
            [_dns_record("93.184.216.34")], [_dns_record("127.0.0.1")],
        ]), patch("src.ops.safe_http.requests.Session") as session:
            with self.assertRaises(SafeHTTPError):
                safe_get("https://jobs.example.org/job")
            session.assert_not_called()

    def test_pinned_adapter_rejects_proxy_or_disabled_certificate_checks(self):
        adapter = _PinnedAdapter("93.184.216.34", "jobs.example.org")
        request = requests.Request("GET", "https://jobs.example.org/").prepare()
        try:
            with self.assertRaises(SafeHTTPError):
                adapter.get_connection_with_tls_context(request, False)
            with self.assertRaises(SafeHTTPError):
                adapter.get_connection_with_tls_context(request, True, proxies={"https": "http://localhost"})
        finally:
            adapter.close()

    def test_public_ipv4_ipv6_literals_are_accepted(self):
        self.assertEqual(validate_public_http_url("https://93.184.216.34/job"), (True, None))
        self.assertEqual(validate_public_http_url("https://[2606:4700:4700::1111]/job"), (True, None))

    def test_private_literal_credentials_and_non_http_are_rejected(self):
        for url in (
            "http://127.0.0.1/admin",
            "http://[::1]/admin",
            "http://user:secret@example.org/job",
            "ftp://example.org/job",
        ):
            with self.subTest(url=url):
                valid, reason = validate_public_http_url(url)
                self.assertFalse(valid)
                self.assertTrue(reason)

    def test_dns_mixed_addresses_and_dns_errors_fail_closed(self):
        def mixed_resolver(*_args, **_kwargs):
            return [_dns_record("93.184.216.34"), _dns_record("10.0.0.5")]

        valid, _ = validate_public_http_url("https://jobs.example.org/", resolver=mixed_resolver)
        self.assertFalse(valid)

        def failed_resolver(*_args, **_kwargs):
            raise socket.gaierror("DNS unavailable")

        valid, reason = validate_public_http_url("https://jobs.example.org/", resolver=failed_resolver)
        self.assertFalse(valid)
        self.assertIn("çözümlenemedi", reason)

    def test_private_redirect_is_rejected_before_second_network_request(self):
        initial = "https://jobs.example.org/job"
        redirect = _response(302, initial, {"Location": "http://169.254.169.254/latest/meta-data"})
        requester = MagicMock(return_value=redirect)

        def validator(url):
            if "169.254.169.254" in url:
                return False, "private target"
            return True, None

        with self.assertRaisesRegex(SafeHTTPError, "private target"):
            safe_get(initial, validator=validator, requester=requester)

        requester.assert_called_once()
        self.assertFalse(requester.call_args.kwargs["allow_redirects"])
        redirect.close.assert_called_once()

    def test_public_redirect_checks_each_hop_and_closes_intermediate_response(self):
        first_url = "https://jobs.example.org/job"
        second_url = "https://careers.example.net/position"
        first = _response(301, first_url, {"Location": second_url})
        final = _response(200, second_url)
        requester = MagicMock(side_effect=[first, final])
        checked = []

        def validator(url):
            checked.append(url)
            return True, None

        result = safe_get(first_url, validator=validator, requester=requester)

        self.assertEqual(requester.call_count, 2)
        self.assertTrue(all(call.kwargs["allow_redirects"] is False for call in requester.call_args_list))
        self.assertIn(second_url, checked)
        first.close.assert_called_once()
        self.assertEqual(result.url, second_url)
        self.assertEqual(result.history, [first])
        result.close()

    def test_redirect_limit_and_timeout_are_bounded(self):
        url = "https://jobs.example.org/job"
        redirect = _response(302, url, {"Location": "/again"})
        requester = MagicMock(return_value=redirect)
        with self.assertRaisesRegex(SafeHTTPError, "sınırı aştı"):
            safe_get(url, max_redirects=0, validator=lambda _url: (True, None), requester=requester)
        requester.assert_called_once()
        redirect.close.assert_called_once()

        with self.assertRaises(ValueError):
            safe_get(url, timeout=0, validator=lambda _url: (True, None), requester=requester)


if __name__ == "__main__":
    unittest.main()
