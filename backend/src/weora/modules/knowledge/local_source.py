"""校验设备提交的本地目录快照，来源始终是相对于目录的路径。"""

import hashlib
import json
import re
from typing import Annotated, Self

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator, model_validator

from weora.modules.knowledge.review_source import registered_repo
from weora_code_graph.extractor import DEFAULT_INDEX_FILES, MAX_INDEX_FILES, language_for

MAX_LOCAL_BYTES = 8 * 1024 * 1024
MAX_LOCAL_BODY_BYTES = 18 * 1024 * 1024
MAX_LOCAL_FILE_BYTES = 256_000
_CONTROL = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_PATH_CONTROL = re.compile(r"[\x00-\x1f\x7f:\\]")
LocalKey = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]


class LocalCodeFile(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    path: str = Field(min_length=1, max_length=480)
    text: str = Field(max_length=MAX_LOCAL_FILE_BYTES)

    @field_validator("path")
    @classmethod
    def relative_source_path(cls, value: str) -> str:
        if (
            _PATH_CONTROL.search(value)
            or any(not part or part.startswith(".") for part in value.split("/"))
            or language_for(value) is None
        ):
            raise ValueError("源码文件路径无效。")
        return value

    @field_validator("text")
    @classmethod
    def bounded_source(cls, value: str) -> str:
        if _CONTROL.search(value) or len(value.encode("utf-8")) >= MAX_LOCAL_FILE_BYTES:
            raise ValueError("源码必须为 UTF-8 文本且小于单文件上限。")
        return value


class LocalCheckout(BaseModel):
    """设备观察到的 Git 来源，仓库关联和内容等价由平台验证。"""

    model_config = ConfigDict(extra="forbid", strict=True)
    origin: str = Field(min_length=1, max_length=512)
    commit: str = Field(pattern=r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
    subdir: str = Field(max_length=480)
    dirty: bool

    @field_validator("origin")
    @classmethod
    def valid_origin(cls, value: str) -> str:
        if registered_repo(value) is None:
            raise ValueError("Git 源地址无效。")
        return value

    @field_validator("subdir")
    @classmethod
    def relative_directory(cls, value: str) -> str:
        if value and (_PATH_CONTROL.search(value) or any(part in {"", ".", ".."} for part in value.split("/"))):
            raise ValueError("Git 子目录必须为相对路径。")
        return value


class LocalCodeSnapshot(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    source_key: LocalKey
    name: str = Field(min_length=1, max_length=160, pattern=r"^[^/\\:\x00-\x1f\x7f]+$")
    incremental: bool = False
    max_files: int = Field(default=DEFAULT_INDEX_FILES, ge=1, le=MAX_INDEX_FILES)
    candidates: int = Field(ge=0, le=1_000_000)
    files: list[LocalCodeFile] = Field(max_length=MAX_INDEX_FILES)
    checkout: LocalCheckout | None = None

    @field_validator("name")
    @classmethod
    def non_empty_name(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("目录名称不能为空。")
        return value

    @model_validator(mode="after")
    def valid_manifest(self) -> Self:
        paths = {item.path.casefold() for item in self.files}
        if len(paths) != len(self.files) or len(self.files) != min(self.max_files, self.candidates):
            raise ValueError("源码清单包含重复路径或文件数不匹配。")
        if sum(len(item.text.encode("utf-8")) for item in self.files) > MAX_LOCAL_BYTES:
            raise ValueError("源码总大小超过上限。")
        return self

    def hashes(self) -> dict[str, str]:
        return {item.path: hashlib.sha256(item.text.encode("utf-8")).hexdigest() for item in self.files}

    def matches_git_blobs(self, count: int, expected: dict[str, str]) -> bool:
        if count != self.candidates or expected.keys() != {item.path for item in self.files}:
            return False
        for item in self.files:
            oid = expected[item.path]
            content = item.text.encode("utf-8")
            # 按 Git 对象格式比较实际字节，不能用客户端自报的提交代替验证。
            digest = hashlib.sha1(usedforsecurity=False) if len(oid) == 40 else hashlib.sha256()
            digest.update(f"blob {len(content)}\0".encode())
            digest.update(content)
            if digest.hexdigest() != oid:
                return False
        return True

    def revision(self, hashes: dict[str, str], checkout: dict[str, str] | None = None) -> str:
        # 内容哈希仍标识设备快照，有关联时同时记录服务端确认的来源。
        parts: list[object] = [self.name, self.max_files, self.candidates, hashes]
        if checkout is not None:
            parts.append(checkout)
        payload = json.dumps(parts, sort_keys=True).encode()
        return hashlib.sha256(payload).hexdigest()
