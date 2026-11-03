"""Protocol fixture: real subprocess and pipes, no network or actual credentials."""
import base64
import json
import os
import sys
from pathlib import Path

PNG = base64.b64encode(b"\x89PNG\r\n\x1a\nfixture").decode()
mode = os.environ.get("FAKE_MODE", "success")
authenticated = mode not in {"login", "wait_login", "bad_url", "anonymous"}


def send(message):
    print(json.dumps(message), flush=True)


def event(method, params):
    send({"method": method, "params": params})


for line in sys.stdin:
    request = json.loads(line)
    if "method" not in request:
        # Confirm the client explicitly rejects a privileged server request.
        if request.get("id") == "approval" and "error" in request:
            event("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": "completed"}})
        continue
    method = request["method"]
    if "id" not in request:
        continue
    result = {}
    if method == "account/read":
        result = {"account": {"type": "chatgpt", "email": "secret@example.com", "planType": "plus"} if authenticated else None}
    elif method == "account/login/start":
        result = {"type": "chatgptDeviceCode", "loginId": "login", "verificationUrl": "https://auth.openai.com/codex/device", "userCode": "TEST-1234"}
        if mode == "bad_url":
            result["verificationUrl"] = "https://evil.example/codex/device"
        if mode == "login":
            authenticated = True
            event("account/login/completed", {"loginId": "other", "success": False})
            event("account/login/completed", {"loginId": "login", "success": True})
    elif method == "account/logout":
        authenticated = False
    elif method == "account/rateLimits/read":
        if mode in {"quota_unsupported", "quota_error"}:
            send({"id": request["id"], "error": {"code": -32601 if mode == "quota_unsupported" else 401, "message": "secret-token"}})
            continue
        if mode == "quota_empty":
            result = {"rateLimits": {}}
        else:
            result = {"accountId": "secret-account", "rateLimits": {
                "limitId": "codex", "planType": "plus",
                "primary": {"usedPercent": 25, "windowDurationMins": 300, "resetsAt": 1793700000},
                "secondary": {"usedPercent": 80, "windowDurationMins": 10080, "resetsAt": 1794200000},
                "credits": {"hasCredits": False, "unlimited": False, "balance": "0"},
            }}
    elif method.startswith("account/rateLimit"):
        raise AssertionError("Quota reads must not consume reset credits")
    elif method == "model/list":
        model_a = {"id": "a", "model": "model-a", "displayName": "Model A", "isDefault": True,
                   "inputModalities": ["text", "image"]}
        model_b = {"id": "b", "model": "model-b", "displayName": "Model B", "isDefault": False,
                   "inputModalities": ["text"]}
        if mode == "models_empty":
            result = {"data": [], "nextCursor": None}
        elif mode == "models_repeat":
            result = {"data": [model_a], "nextCursor": "repeated"}
        elif mode == "models_paginated":
            if request["params"].get("cursor"):
                result = {"data": [model_a, model_b, {"model": "hidden-model", "hidden": True}], "nextCursor": None}
            else:
                result = {"data": [model_a], "nextCursor": "page-2"}
        else:
            result = {"data": [model_a, model_b], "nextCursor": None}
    elif method == "thread/start":
        assert request["params"]["sandbox"] == "read-only"
        result = {"thread": {"id": "thread"}}
    elif method == "turn/start":
        if mode == "edit":
            inputs = request["params"]["input"]
            assert inputs[0]["text"].startswith("Edit the attached reference")
            assert len(inputs) == 2 and inputs[1]["type"] == "localImage"
            reference = Path(inputs[1]["path"])
            assert reference.is_absolute() and reference.parent == Path.cwd()
            assert reference.read_bytes() == base64.b64decode(PNG)
        result = {"turn": {"id": "turn"}}
        if mode == "crash":
            sys.exit(1)
        if mode == "rpc_error":
            send({"id": request["id"], "error": {"code": 401, "message": "secret-token"}})
            continue
        if mode != "wait_image":
            item = {"id": "img", "type": "imageGeneration", "status": "completed", "result": PNG}
            if mode == "bad_image":
                item["result"] = "not base64"
            if mode == "file_id":
                item.pop("result")
                item["fileId"] = "file_123"
            if mode == "saved_path":
                path = Path.cwd() / "generated.png"
                path.write_bytes(base64.b64decode(PNG))
                item.pop("result")
                item["savedPath"] = str(path)
            if mode == "quota":
                item["failure"] = {"type": "usageLimitExceeded"}
            # Emit before the response, to expose notification-registration races.
            event("item/completed", {"threadId": "other", "turnId": "turn", "item": item})
            if mode != "no_image":
                event("item/completed", {"threadId": "thread", "turnId": "turn", "item": item})
                event("item/completed", {"threadId": "thread", "turnId": "turn", "item": item})
            if mode == "approval":
                send({"id": "approval", "method": "item/commandExecution/requestApproval", "params": {}})
            else:
                status = "failed" if mode == "failed_turn" else "completed"
                event("turn/completed", {"threadId": "thread", "turn": {"id": "turn", "status": status}})
    send({"id": request["id"], "result": result})
