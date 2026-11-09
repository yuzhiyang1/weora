"""从已签名 Debian 索引获取浏览器依赖公开包，逐包核对 SHA256 后形成构建缓存。"""

import argparse
import concurrent.futures
import hashlib
import json
import re
import subprocess
import urllib.parse
import urllib.request
from pathlib import Path

PLAN = r"""
import json,re,shlex,subprocess,urllib.parse
dry=subprocess.run(['node','/opt/weora-browser/node_modules/playwright/cli.js','install-deps','--dry-run','chromium'],capture_output=True,text=True)
names=[line.strip() for line in dry.stdout.splitlines() if re.fullmatch(r'  [a-z0-9+.-]+',line)]
assert names, dry.stdout
command=['apt-get','--print-uris','--download-only','--assume-yes','--no-install-recommends','-o','Debug::NoLocking=true','install',*names]
uris=subprocess.check_output(command,text=True)
rows=[]
for line in uris.splitlines():
 if not line.startswith("'http"): continue
 url,filename,size=shlex.split(line)[:3]
 parsed=urllib.parse.urlsplit(url)
 assert parsed.netloc=='deb.debian.org'
 package=urllib.parse.unquote(filename).split('_')[0]
 metadata=subprocess.check_output(['apt-cache','show',package],text=True)
 candidates=[]
 for block in metadata.split(chr(10)+chr(10)):
  values=dict(item.split(': ',1) for item in block.splitlines() if ': ' in item and not item.startswith(' '))
  source=urllib.parse.unquote(values.get('Filename','').rsplit('/',1)[-1])
  if source==urllib.parse.unquote(parsed.path.rsplit('/',1)[-1]): candidates.append(values)
 assert candidates, {'filename':filename,'metadata':metadata[:1800]}
 meta=candidates[0]
 rows.append({'url':urllib.parse.urlunsplit(('https',parsed.netloc,parsed.path,'','')),'filename':filename,'size':int(size),'sha256':meta['SHA256']})
print(json.dumps(rows))
"""


def fetch(image: str, output: Path, parts: Path) -> None:
    if not re.fullmatch(r"sha256:[0-9a-f]{64}", image):
        raise ValueError("The metadata image must use its exact local digest")
    command = [
        "docker",
        "--host",
        "unix:///var/run/docker.sock",
        "--config",
        "/var/lib/weora/docker-cli",
        "run",
        "--rm",
        "--network",
        "none",
        "--read-only",
        "--cap-drop",
        "ALL",
        "--security-opt",
        "no-new-privileges=true",
        "--user",
        "10001:10001",
        "--tmpfs",
        "/tmp:rw,nosuid,nodev,size=16777216,uid=10001,gid=10001,mode=0700",
        "--label",
        "weora.managed=v1",
        "--label",
        "weora.purpose=build-metadata",
        image,
        "python3",
        "-c",
        PLAN,
    ]
    rows = json.loads(subprocess.check_output(command))
    if len(rows) > 200 or sum(row["size"] for row in rows) > 268435456:
        raise ValueError("Browser dependency package budget exceeded")
    output.mkdir(parents=True, exist_ok=True)
    parts.mkdir(parents=True, exist_ok=True)
    jobs: list[tuple[int, int, int]] = []
    for index, row in enumerate(rows):
        if not re.fullmatch(r"[A-Za-z0-9_.%+~:-]+\.deb", row["filename"]):
            raise ValueError("Invalid signed package filename")
        cached = output / row["filename"]
        if cached.exists():
            if hashlib.sha256(cached.read_bytes()).hexdigest() != row["sha256"]:
                raise ValueError("Existing package cache failed signed index verification")
            continue
        jobs.extend((index, start, min(start + 1048576, row["size"]) - 1) for start in range(0, row["size"], 1048576))
    print(
        json.dumps({"packages": len(rows), "missing_range_bytes": sum(end - start + 1 for _, start, end in jobs)}),
        flush=True,
    )

    def part(job: tuple[int, int, int]) -> tuple[tuple[int, int, int], bytes]:
        index, start, end = job
        row = rows[index]
        destination = parts / (row["sha256"] + "." + str(start) + ".part")
        if destination.exists() and destination.stat().st_size == end - start + 1:
            return job, destination.read_bytes()
        request = urllib.request.Request(row["url"], headers={"Range": f"bytes={start}-{end}"})
        with urllib.request.urlopen(request, timeout=20) as response:
            data = response.read(end - start + 2)
            if len(data) != end - start + 1 or response.status not in {200, 206}:
                raise ValueError("Official Debian package range response mismatch")
        destination.write_bytes(data)
        print(json.dumps({"downloaded": len(data), "package": row["filename"], "offset": start}), flush=True)
        return job, data

    with concurrent.futures.ThreadPoolExecutor(max_workers=16) as executor:
        chunks = dict(executor.map(part, jobs))
    for index, row in enumerate(rows):
        selected = [job for job in jobs if job[0] == index]
        if not selected:
            continue
        data = b"".join(chunks[job] for job in selected)
        if hashlib.sha256(data).hexdigest() != row["sha256"]:
            raise ValueError("Package did not match the signed apt index")
        (output / row["filename"]).write_bytes(data)
    (parts / "verified.json").write_text(json.dumps({"source_image": image, "packages": rows}, indent=2))
    print(json.dumps({"verified_packages": len(rows), "total_bytes": sum(row["size"] for row in rows)}), flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--image", required=True)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--parts", required=True, type=Path)
    arguments = parser.parse_args()
    fetch(arguments.image, arguments.output, arguments.parts)
