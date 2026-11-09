"""令牌文件是独立 API 身份，不借用 Git 密码，异常不能回显文件或秘密正文。"""

from pathlib import Path

import pytest
from pydantic import SecretStr

from weora.modules.delivery.domain.models import DeliveryError
from weora.modules.delivery.infrastructure.git import ConfiguredCredentials


def test_api_identity_is_separate_and_never_in_repr(tmp_path: Path) -> None:
    path = tmp_path / "api-token.txt"
    path.write_text("\ufefftest-token\n", encoding="utf-8")
    credentials = ConfiguredCredentials(
        {"git": SecretStr('{"username":"test","password":"git-password"}')}, {"api": str(path)}
    )
    material = credentials.get("api")
    assert material.api_token == "test-token" and material.password == ""
    assert "test-token" not in repr(material)
    assert credentials.get("git").password == "git-password"
    with pytest.raises(ValueError):
        ConfiguredCredentials({"api": SecretStr("{}")}, {"api": str(path)})


@pytest.mark.parametrize("contents", ["", "two\nlines", "x" * 5000])
def test_bad_token_file_is_rejected_without_disclosing_contents(tmp_path: Path, contents: str) -> None:
    path = tmp_path / "invalid.txt"
    path.write_text(contents, encoding="utf-8")
    with pytest.raises(DeliveryError) as error:
        ConfiguredCredentials({}, {"api": str(path)}).get("api")
    assert error.value.code == "CREDENTIAL_UNAVAILABLE"


def test_missing_or_relative_file_is_rejected(tmp_path: Path) -> None:
    for path in [str(tmp_path / "absent.txt"), "relative.txt"]:
        with pytest.raises(DeliveryError):
            ConfiguredCredentials({}, {"api": path}).get("api")
