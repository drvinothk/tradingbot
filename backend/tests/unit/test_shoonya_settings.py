from __future__ import annotations

from pydantic import SecretStr

from app.config.settings import ShoonyaSettings

_ALL_SET = dict(
    client_id="TESTCID",
    secret_code=SecretStr("TESTSECRET"),
    user_id="FA12345",
    redirect_url="http://127.0.0.1:5000/shoonya/callback",
)


def test_missing_required_fields_empty_when_all_set():
    settings = ShoonyaSettings(**_ALL_SET)
    assert settings.missing_required_fields() == []


def test_missing_required_fields_reports_every_gap():
    settings = ShoonyaSettings(
        client_id="",
        secret_code=SecretStr(""),
        user_id="",
        redirect_url="",
    )
    missing = settings.missing_required_fields()
    assert missing == [
        "SHOONYA_CLIENT_ID",
        "SHOONYA_SECRET_CODE",
        "SHOONYA_USER_ID",
        "SHOONYA_REDIRECT_URL",
    ]


def test_missing_required_fields_reports_only_the_actual_gap():
    settings = ShoonyaSettings(**{**_ALL_SET, "secret_code": SecretStr("")})
    assert settings.missing_required_fields() == ["SHOONYA_SECRET_CODE"]


def test_vendor_code_and_totp_are_not_required():
    """`vendor_code`/`primary_ip`/`backup_ip`/`totp_secret` aren't consumed by
    any code path yet (see `ShoonyaSettings.missing_required_fields`'s own
    docstring) — leaving them blank must not trip validation.
    """
    settings = ShoonyaSettings(**_ALL_SET, vendor_code="", primary_ip="", backup_ip="")
    assert settings.missing_required_fields() == []
