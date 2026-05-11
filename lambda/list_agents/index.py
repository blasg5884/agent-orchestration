"""GET /v1/agents [?name=...]

Lists or searches AGENT-type records in the AgentCore Agent Registry.
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
REGISTRY_ID = os.environ["AGENT_REGISTRY_ID"]
REGISTRY_ARN = os.environ["AGENT_REGISTRY_ARN"]

_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
_data = boto3.client("bedrock-agentcore", region_name=REGION)


def _ok(body: Any) -> dict:
    return {
        "statusCode": 200,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps(body, default=str, ensure_ascii=False),
    }


def _err(status: int, message: str) -> dict:
    return {
        "statusCode": status,
        "headers": {"Content-Type": "application/json"},
        "body": json.dumps({"error": message}, ensure_ascii=False),
    }


def _serialize(record: dict) -> dict:
    return {
        "recordId": record.get("recordId"),
        "recordArn": record.get("recordArn"),
        "name": record.get("name"),
        "description": record.get("description"),
        "descriptorType": record.get("descriptorType"),
        "status": record.get("status"),
        "version": record.get("version") or record.get("recordVersion"),
    }


def handler(event: dict, _context: Any) -> dict:
    params = (event.get("queryStringParameters") or {}) or {}
    name_filter = params.get("name")

    try:
        if name_filter:
            # Search filtered to a specific name. search_registry_records returns
            # only APPROVED records; that's the desired behavior for a public listing.
            resp = _data.search_registry_records(
                registryIds=[REGISTRY_ARN],
                searchQuery=name_filter,
                maxResults=50,
            )
            records = [
                _serialize(r)
                for r in resp.get("registryRecords", [])
                if r.get("name") == name_filter
            ]
            return _ok({"agents": records})

        # No filter — list all records (control plane, paginated).
        records: list[dict] = []
        next_token: str | None = None
        while True:
            kwargs: dict[str, Any] = {"registryId": REGISTRY_ARN, "maxResults": 100}
            if next_token:
                kwargs["nextToken"] = next_token
            resp = _control.list_registry_records(**kwargs)
            for r in resp.get("registryRecords", []):
                records.append(_serialize(r))
            next_token = resp.get("nextToken")
            if not next_token:
                break
        return _ok({"agents": records})
    except Exception as e:  # noqa: BLE001
        logger.exception("list_agents failed")
        return _err(500, str(e))
