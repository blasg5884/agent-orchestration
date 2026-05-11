"""POST /v1/invoke

Forwards a natural-language prompt to the orchestrator AgentCore Runtime.
"""
from __future__ import annotations

import json
import logging
import os
import uuid
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
ORCHESTRATOR_RUNTIME_ARN = os.environ["ORCHESTRATOR_RUNTIME_ARN"]
ORCHESTRATOR_QUALIFIER = os.environ.get("ORCHESTRATOR_QUALIFIER", "DEFAULT")

_agentcore = boto3.client("bedrock-agentcore", region_name=REGION)


def _resp(status: int, body: Any) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str, ensure_ascii=False),
    }


def handler(event: dict, _context: Any) -> dict:
    raw = event.get("body") or "{}"
    if event.get("isBase64Encoded"):
        import base64

        raw = base64.b64decode(raw).decode("utf-8")
    try:
        body = json.loads(raw) if isinstance(raw, str) else raw
    except json.JSONDecodeError:
        return _resp(400, {"error": "body must be valid JSON"})

    prompt = body.get("prompt")
    if not prompt:
        return _resp(400, {"error": "'prompt' is required"})

    # invoke_agent_runtime requires runtimeSessionId to be 33–100 chars.
    # If a caller-supplied sessionId is too short, pad it deterministically.
    raw_session = body.get("sessionId") or ""
    if len(raw_session) >= 33:
        session_id = raw_session
    elif raw_session:
        session_id = (raw_session + "-" + uuid.uuid4().hex)[:100]
    else:
        session_id = uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars

    try:
        resp = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=ORCHESTRATOR_RUNTIME_ARN,
            qualifier=ORCHESTRATOR_QUALIFIER,
            runtimeSessionId=session_id,
            payload=json.dumps({"prompt": prompt}).encode("utf-8"),
        )
    except Exception as e:  # noqa: BLE001
        logger.exception("invoke_agent_runtime failed")
        return _resp(500, {"error": str(e)})

    # Response payload is a StreamingBody.
    payload_bytes = resp["response"].read()
    try:
        payload = json.loads(payload_bytes)
    except json.JSONDecodeError:
        payload = {"raw": payload_bytes.decode("utf-8", errors="replace")}

    return _resp(200, {"sessionId": session_id, "result": payload})
