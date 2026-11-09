"""离线原生进程替身；模拟官方 CLI 信封，不连接网络，不写入个人配置。"""

import json
import os
import sys
import time

if sys.argv[1:] == ["--version"]:
    print("lark-cli version 1.0.80")
    sys.exit(0)

mode = os.environ["LARKSUITE_CLI_APP_ID"]
if mode == "cli_slow":
    time.sleep(10)
if mode in {"cli_permission", "cli_expired", "cli_rate", "cli_broken_error"}:
    error: object = {
        "type": "authorization" if mode == "cli_permission" else "api",
        "subtype": "rate_limit" if mode == "cli_rate" else "unknown",
        "code": {"cli_expired": 99991663}.get(mode),
        "message": "private-token-do-not-leak",
    }
    if mode == "cli_broken_error":
        error = []
    print(json.dumps({"ok": False, "error": error}), file=sys.stderr)
    sys.exit(1)
if mode in {"cli_oversize", "cli_stderr_oversize"}:
    print("x" * 2_000_001, file=sys.stderr if mode == "cli_stderr_oversize" else sys.stdout, flush=True)
    time.sleep(10)
if mode == "cli_malformed":
    print("private-token-do-not-leak")
    sys.exit(0)

parameters = json.load(sys.stdin)
raw = {
    "message_id": "om_one", "chat_id": "oc_project", "thread_id": "omt_one",
    "create_time": "1609296809000", "sender": {"id": "ou_author"},
    "msg_type": "file", "body": {"content": json.dumps({"file_key": "file_one", "file_name": "plan.txt"})},
}
data: dict[str, object] = {"items": [raw], "has_more": True, "page_token": "opaque-next"}
if mode == "cli_bad_page":
    data.pop("page_token")
data["invocation"] = {
    "argv": sys.argv[1:], "parameters": parameters,
    "environment": sorted(os.environ), "cwd": os.getcwd(),
    "brand": os.environ.get("LARKSUITE_CLI_BRAND"),
    "strict_mode": os.environ.get("LARKSUITE_CLI_STRICT_MODE"),
    "has_token": bool(os.environ.get("LARKSUITE_CLI_TENANT_ACCESS_TOKEN")),
    "config_dir": os.environ.get("LARKSUITE_CLI_CONFIG_DIR"),
}
print(json.dumps({
    "ok": mode != "cli_false_ok",
    "identity": "user" if mode == "cli_wrong_identity" else "bot", "data": data,
}))
