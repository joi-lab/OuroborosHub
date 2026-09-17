import pytest

from lib.client import MailClient


def test_xoauth2_formats_imap_and_smtp_and_finishes_error_challenge():
    client = MailClient({"EMAIL_USER": "test@example.org", "EMAIL_AUTH_MODE": "oauth2", "EMAIL_OAUTH_ACCESS_TOKEN": "test-token"})
    class IMAP:
        def authenticate(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback(b"") == b"user=test@example.org\x01auth=Bearer test-token\x01\x01"
            assert callback(b'{"status":"401"}') == b""
    class SMTP:
        def auth(self, mechanism, callback):
            assert mechanism == "XOAUTH2"
            assert callback() == "user=test@example.org\x01auth=Bearer test-token\x01\x01"
            assert callback(b'{"status":"401"}') == ""
    client._auth(IMAP())
    client._auth(SMTP(), smtp=True)


def test_refresh_uses_existing_credentials_and_caches_access_token(monkeypatch):
    calls = []
    class Response:
        status_code = 200
        def json(self):
            return {"access_token": "refreshed", "expires_in": 3600}
    def post(url, data, timeout):
        assert url == "https://oauth2.googleapis.com/token"
        assert data == {"grant_type": "refresh_token", "refresh_token": "refresh", "client_id": "client", "client_secret": "secret"}
        calls.append(data)
        return Response()
    monkeypatch.setattr("lib.client.httpx.post", post)
    client = MailClient({"EMAIL_OAUTH_REFRESH_TOKEN": "refresh", "EMAIL_OAUTH_CLIENT_ID": "client", "EMAIL_OAUTH_CLIENT_SECRET": "secret"})
    assert client._access_token() == "refreshed"
    assert client._access_token() == "refreshed"
    assert len(calls) == 1


def test_refresh_error_never_echoes_provider_response_or_secret(monkeypatch):
    class Response:
        status_code = 401
        text = "credential secret"
    monkeypatch.setattr("lib.client.httpx.post", lambda *a, **kw: Response())
    with pytest.raises(RuntimeError, match=r"OAuth token refresh failed \(HTTP 401\)"):
        MailClient({"EMAIL_OAUTH_REFRESH_TOKEN": "refresh"})._access_token()
