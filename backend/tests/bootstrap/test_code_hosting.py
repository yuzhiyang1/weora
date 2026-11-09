"""连接配置失败关闭；不读取开发者配置或调用外部平台。"""

from uuid import uuid4

import pytest
from pydantic import SecretStr, ValidationError

from tests.support.settings import IsolatedAppSettings
from weora.bootstrap.code_hosting import HostingConnectionSettings


def values() -> dict[str, object]:
    return {
        "workspace_id": uuid4(),
        "remote_id": "team-repo",
        "version": "v1",
        "provider": "codeup",
        "repository_id": "test-repo",
        "platform_repository": "123",
        "clone_url": "https://codeup.example/team/repo.git",
        "api_origin": "https://api.example",
        "web_url": "https://codeup.example/team/repo",
        "credential_ref": "team-token-v1",
        "target_branches": ["main"],
    }


@pytest.mark.parametrize(
    "field,value",
    [
        ("clone_url", "file:///some/repository"),
        ("clone_url", "https://user:secret@example/repo"),
        ("clone_url", "ext::some-command"),
        ("clone_url", "ssh://root@example/repo"),
        ("api_origin", "http://api.example"),
        ("api_origin", "https://api.example/path"),
        ("api_origin", "https://api.example/"),
        ("web_url", "javascript:alert(1)"),
        ("platform_repository", "123,456"),
        ("target_branches", ["main..other"]),
        ("provider", "unimplemented-gitlab"),
    ],
)
def test_invalid_connections_fail_without_echoing_credentials(field: str, value: object) -> None:
    with pytest.raises(ValidationError) as error:
        HostingConnectionSettings.model_validate(values() | {field: value})
    assert "user:secret" not in str(error.value)


def test_multiple_repositories_and_redacted_secret_references() -> None:
    first = HostingConnectionSettings.model_validate(values())
    second = first.model_copy(update={"remote_id": "another-repo", "platform_repository": "456"})
    settings = IsolatedAppSettings(
        hosting_connections=(first, second), hosting_credentials={"team-token-v1": SecretStr("secret")}
    )
    assert len(settings.hosting_connections) == 2
    assert "hosting_credentials" not in settings.model_dump()
    assert "SecretStr" not in repr(settings)
    assert first.connection().fingerprint != second.connection().fingerprint
    with pytest.raises(ValidationError):
        IsolatedAppSettings(hosting_connections=(first, first))
