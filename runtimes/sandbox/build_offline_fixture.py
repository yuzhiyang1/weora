"""从本机已安装的明确运行文件制作离线验收夹具，不复制用户目录、系统配置或凭据。"""

import argparse
import hashlib
import io
import json
import re
import subprocess
import tarfile
from pathlib import Path


def build(output: Path, site_packages: Path) -> None:
    output.mkdir(parents=True, exist_ok=True)
    roots = [
        Path(x)
        for x in (
            "/usr/bin/python3.12",
            "/usr/bin/git",
            "/usr/lib/python3.12",
            "/usr/lib/git-core",
            "/usr/share/git-core",
        )
    ]
    binaries = [Path("/usr/bin/python3.12"), Path("/usr/bin/git")]
    binaries.extend(Path("/usr/lib/python3.12").rglob("*.so"))
    libraries: set[Path] = set()
    for binary in binaries:
        result = subprocess.run(["ldd", str(binary)], capture_output=True, text=True, check=True)
        for name in re.findall(r"(/[A-Za-z0-9_./+:-]+)", result.stdout):
            library = Path(name)
            if library.is_file():
                libraries.add(library)
    packages = {"pytest", "_pytest", "iniconfig", "packaging", "pluggy", "pygments"}
    package_roots: list[Path] = []
    for name in sorted(packages):
        package = site_packages / name
        if not package.is_dir():
            raise ValueError(f"Required pure Python package is absent: {name}")
        package_roots.append(package)
        package_roots.extend(site_packages.glob(name + "-*.dist-info"))
    provenance: dict[str, object] = {
        "weora.managed": "v1",
        "purpose": "offline-sandbox-acceptance-only",
        "files": {},
        "system_packages": subprocess.check_output(
            ["dpkg-query", "-W", "-f=${Package}=${Version}\\n", "python3.12", "git", "libc6"], text=True
        ).splitlines(),
    }
    hashes: dict[str, str] = {}
    provenance["files"] = hashes

    with tarfile.open(output / "rootfs.tar", "w") as archive:
        seen: set[str] = set()

        def add_file(source: Path, destination: str) -> None:
            if destination in seen:
                return
            data = source.read_bytes()
            item = tarfile.TarInfo(destination)
            item.size, item.mode = len(data), source.stat().st_mode & 0o777
            archive.addfile(item, io.BytesIO(data))
            seen.add(destination)
            hashes[destination] = hashlib.sha256(data).hexdigest()

        for root in roots:
            paths = [root] if root.is_file() else sorted(root.rglob("*"))
            for path in paths:
                if "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.is_file():
                    # Git helper 多数是同一二进制的符号链接，保留链接避免膨胀数百 MiB。
                    destination = str(path).lstrip("/")
                    if path.is_symlink():
                        item = archive.gettarinfo(str(path), destination)
                        archive.addfile(item)
                        seen.add(destination)
                    else:
                        add_file(path, destination)
        for library in sorted(libraries):
            add_file(library, str(library).lstrip("/"))
        for root in package_roots:
            for path in sorted(root.rglob("*")):
                if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                    continue
                if path.suffix.lower() in {".dll", ".pyd", ".so"}:
                    raise ValueError("Acceptance dependencies must be pure Python")
                add_file(path, "opt/weora-testdeps/" + str(path.relative_to(site_packages)))
        # pytest 自带的 py 兼容模块位于顶层单文件，不在 pytest 包目录内。
        add_file(site_packages / "py.py", "opt/weora-testdeps/py.py")
        link = tarfile.TarInfo("usr/bin/python3")
        link.type, link.linkname = tarfile.SYMTYPE, "python3.12"
        archive.addfile(link)
        # 只生成夹具所需的最小公开账户映射，不读取宿主 /etc/passwd 或其他配置。
        account = b"weora:x:10001:10001:Weora:/tmp:/usr/bin/python3\n"
        item = tarfile.TarInfo("etc/passwd")
        item.size, item.mode = len(account), 0o644
        archive.addfile(item, io.BytesIO(account))
    provenance["rootfs_sha256"] = hashlib.sha256((output / "rootfs.tar").read_bytes()).hexdigest()
    (output / "provenance.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps({"files": len(hashes), "rootfs_sha256": provenance["rootfs_sha256"], "output": str(output)}))


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--site-packages", required=True, type=Path)
    arguments = parser.parse_args()
    build(arguments.output, arguments.site_packages)
