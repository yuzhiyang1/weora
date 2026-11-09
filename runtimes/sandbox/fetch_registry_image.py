"""有界并发获取官方 ECR 镜像；保留分段缓存，逐层核验后生成本地 OCI 导入包。"""

import argparse
import concurrent.futures
import hashlib
import io
import json
import re
import tarfile
import time
import urllib.parse
import urllib.request
from http.client import HTTPMessage, HTTPResponse
from pathlib import Path
from typing import IO, cast


class SecureRedirect(urllib.request.HTTPRedirectHandler):
    """下载跳转只允许 HTTPS，跨主机不转发临时匿名 Registry Bearer。"""

    def redirect_request(
        self, req: urllib.request.Request, fp: IO[bytes], code: int, msg: str, headers: HTTPMessage, newurl: str
    ) -> urllib.request.Request | None:
        if urllib.parse.urlsplit(newurl).scheme != "https":
            raise ValueError("Registry redirect must preserve HTTPS")
        redirected = super().redirect_request(req, fp, code, msg, headers, newurl)
        if redirected and urllib.parse.urlsplit(req.full_url).netloc != urllib.parse.urlsplit(newurl).netloc:
            redirected.remove_header("Authorization")
        return redirected


def fetch(repository: str, digest: str, output: Path, reference: str) -> None:
    """只读官方匿名源；调用方必须再用系统 timeout 设置整个下载阶段的硬时限。"""
    if repository not in {"docker/library/python", "docker/library/node"}:
        raise ValueError("Only explicitly selected official base repositories are allowed")
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", digest):
        raise ValueError("A full verified manifest digest is required")
    if not re.fullmatch(r"docker\.io/weora/[a-z0-9-]+:verified", reference):
        raise ValueError("Local imported reference must remain within the Weora namespace")
    output.mkdir(parents=True, exist_ok=True)
    base = "https://public.ecr.aws"
    realm = f"{base}/token/?service=public.ecr.aws&scope=repository:{repository}:pull"
    with urllib.request.urlopen(realm, timeout=15) as response:
        token = json.loads(response.read(65536))["token"]
    opener = urllib.request.build_opener(SecureRedirect)

    def request(url: str, headers: dict[str, str] | None = None) -> HTTPResponse:
        return cast(
            HTTPResponse,
            opener.open(
                urllib.request.Request(url, headers={"Authorization": "Bearer " + token, **(headers or {})}), timeout=20
            ),
        )

    with request(
        f"{base}/v2/{repository}/manifests/{digest}",
        {"Accept": "application/vnd.oci.image.manifest.v1+json,application/vnd.docker.distribution.manifest.v2+json"},
    ) as response:
        manifest = response.read(1048577)
    if len(manifest) > 1048576 or "sha256:" + hashlib.sha256(manifest).hexdigest() != digest:
        raise ValueError("Official manifest content does not match the pinned digest")
    record = json.loads(manifest)
    with request(f"{base}/v2/{repository}/blobs/{record['config']['digest']}") as response:
        config = response.read(1048577)
    if len(config) > 1048576 or "sha256:" + hashlib.sha256(config).hexdigest() != record["config"]["digest"]:
        raise ValueError("Official image config digest mismatch")
    layers = record["layers"]
    if len(layers) > 20 or sum(layer["size"] for layer in layers) > 1073741824:
        raise ValueError("Selected base image exceeds the download budget")
    jobs = [
        (layer["digest"], start, min(start + 1048576, layer["size"]) - 1)
        for layer in layers
        for start in range(0, layer["size"], 1048576)
    ]

    def part(job: tuple[str, int, int]) -> tuple[tuple[str, int, int], bytes]:
        layer_digest, start, end = job
        destination = output / (layer_digest.split(":")[1] + "." + str(start) + ".part")
        if destination.exists() and destination.stat().st_size == end - start + 1:
            return job, destination.read_bytes()
        for attempt in range(2):
            try:
                with request(
                    f"{base}/v2/{repository}/blobs/{layer_digest}", {"Range": f"bytes={start}-{end}"}
                ) as response:
                    data = response.read(end - start + 2)
                    if response.status != 206 or len(data) != end - start + 1:
                        raise ValueError("Registry range response mismatch")
                destination.write_bytes(data)
                print(json.dumps({"downloaded": len(data), "layer": layer_digest[:19], "start": start}), flush=True)
                return job, data
            except Exception:
                if attempt:
                    raise
                time.sleep(1)
        raise AssertionError("Unreachable")

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        chunks = dict(executor.map(part, jobs))
    blobs = {digest: manifest, record["config"]["digest"]: config}
    for layer in layers:
        data = b"".join(chunks[job] for job in jobs if job[0] == layer["digest"])
        if "sha256:" + hashlib.sha256(data).hexdigest() != layer["digest"]:
            raise ValueError("Official image layer digest mismatch")
        blobs[layer["digest"]] = data
    index = {
        "schemaVersion": 2,
        "manifests": [
            {
                "mediaType": record["mediaType"],
                "digest": digest,
                "size": len(manifest),
                "platform": {"os": "linux", "architecture": "amd64"},
                "annotations": {"org.opencontainers.image.ref.name": reference},
            }
        ],
    }
    with tarfile.open(output / "image.oci.tar", "w") as archive:
        files = {
            "oci-layout": json.dumps({"imageLayoutVersion": "1.0.0"}).encode(),
            "index.json": json.dumps(index).encode(),
        }
        files.update({"blobs/sha256/" + key.split(":")[1]: data for key, data in blobs.items()})
        for name, data in files.items():
            item = tarfile.TarInfo(name)
            item.size = len(data)
            archive.addfile(item, io.BytesIO(data))
    provenance = {
        "weora.managed": "v1",
        "source": f"public.ecr.aws/{repository}",
        "manifest_digest": digest,
        "config_digest": record["config"]["digest"],
        "layers": layers,
        "archive_sha256": hashlib.sha256((output / "image.oci.tar").read_bytes()).hexdigest(),
    }
    (output / "verified.json").write_text(json.dumps(provenance, indent=2), encoding="utf-8")
    print(json.dumps({"verified": digest, "total_layer_bytes": sum(layer["size"] for layer in layers)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repository", required=True)
    parser.add_argument("--digest", required=True)
    parser.add_argument("--reference", required=True)
    parser.add_argument("--output", required=True, type=Path)
    arguments = parser.parse_args()
    fetch(arguments.repository, arguments.digest, arguments.output, arguments.reference)
