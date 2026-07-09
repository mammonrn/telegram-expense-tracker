import pytest

from auth import DriveAuthError, _extract_code, load_drive_credentials


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
