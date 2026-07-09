import json
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse

import pytest

from auth import (
    _CODE_VERIFIER_CHARS,
    DriveAuthError,
    _extract_code,
    _generate_code_verifier,
    build_authorization_url,
    exchange_code,
    load_drive_credentials,
)


def test_extract_code_from_bare_code():
    assert _extract_code("4/0Ab_ExampleAuthCode123") == "4/0Ab_ExampleAuthCode123"


def test_extract_code_strips_whitespace():
    assert _extract_code("  abc123  \n") == "abc123"


def test_extract_code_from_full_redirect_url():
    url = "http://localhost/?state=xyz&code=4%2F0Ab_Example&scope=drive"
    assert _extract_code(url) == "4/0Ab_Example"


def test_extract_code_from_url_missing_code_raises():
    with pytest.raises(DriveAuthError):
        _extract_code("http://localhost/?state=xyz")


def test_extract_code_empty_raises():
    with pytest.raises(DriveAuthError):
        _extract_code("")


def test_load_drive_credentials_missing_token_file_raises_with_instructions(tmp_path):
    missing_path = tmp_path / "does_not_exist.json"
    with pytest.raises(DriveAuthError, match="authorize_drive.py"):
        load_drive_credentials(str(missing_path))


# -- PKCE code_verifier: regression tests for the "invalid_grant: Invalid
# code verifier" bug (googleapis/google-auth-library-python-oauthlib#354),
# where relying on the library's `autogenerate_code_verifier` default left
# flow.code_verifier as None instead of a real, reusable verifier. -------


def test_generate_code_verifier_matches_rfc7636():
    verifier = _generate_code_verifier()
    assert 43 <= len(verifier) <= 128
    assert all(c in _CODE_VERIFIER_CHARS for c in verifier)


def test_generate_code_verifier_is_random_each_time():
    assert _generate_code_verifier() != _generate_code_verifier()


@pytest.fixture
def fake_client_secret(tmp_path):
    path = tmp_path / "client_secret.json"
    path.write_text(
        json.dumps(
            {
                "installed": {
                    "client_id": "fake-client-id.apps.googleusercontent.com",
                    "project_id": "fake-project",
                    "auth_uri": "https://accounts.google.com/o/oauth2/auth",
                    "token_uri": "https://oauth2.googleapis.com/token",
                    "auth_provider_x509_cert_url": "https://www.googleapis.com/oauth2/v1/certs",
                    "client_secret": "fake-secret",
                }
            }
        ),
        encoding="utf-8",
    )
    return str(path)


def test_build_authorization_url_sets_explicit_code_verifier(fake_client_secret):
    flow, auth_url = build_authorization_url(fake_client_secret)

    # The whole point of the fix: the verifier is never None/empty, and it
    # was set by us (not left to the library's version-dependent default).
    assert flow.code_verifier
    assert flow.autogenerate_code_verifier is False

    query = parse_qs(urlparse(auth_url).query)
    assert query.get("code_challenge_method") == ["S256"]
    assert query.get("code_challenge")  # present and non-empty


def test_exchange_code_sends_the_same_verifier_used_for_the_challenge(fake_client_secret, monkeypatch):
    import requests

    flow, _ = build_authorization_url(fake_client_secret)
    verifier_at_auth_time = flow.code_verifier

    captured = {}
    real_request = requests.Session.request

    def fake_request(self, method, url, **kwargs):
        if "token" in url:
            captured["data"] = kwargs.get("data")
            response = requests.Response()
            response.status_code = 200
            response._content = (
                b'{"access_token": "fake", "refresh_token": "fake", '
                b'"expires_in": 3600, "token_type": "Bearer", '
                b'"scope": "https://www.googleapis.com/auth/drive"}'
            )
            response.headers["content-type"] = "application/json"
            response.request = SimpleNamespace(url=url, headers={}, method="POST", body=kwargs.get("data"))
            return response
        return real_request(self, method, url, **kwargs)

    monkeypatch.setattr(requests.Session, "request", fake_request)

    exchange_code(flow, "http://localhost/?state=xyz&code=FAKE_CODE")

    # This is the exact bug: with the library's implicit default, this
    # could end up sending the string "None" instead of the real verifier.
    assert captured["data"]["code_verifier"] == verifier_at_auth_time
    assert flow.code_verifier == verifier_at_auth_time


def test_exchange_code_raises_if_flow_has_no_code_verifier():
    broken_flow = SimpleNamespace(code_verifier=None)
    with pytest.raises(DriveAuthError, match="no PKCE code_verifier"):
        exchange_code(broken_flow, "some-code")
