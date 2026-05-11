"""Custom resource provider for Bedrock AgentCore Agent Registry records (AGENT type).

Boto3-based, with full CloudWatch logging of API responses. The registryId
parameter is received directly via CloudFormation ResourceProperties (which
CloudFormation fully resolves before invoking the Lambda — no token issues).
"""
from __future__ import annotations

import json
import logging
import os
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
_client = boto3.client("bedrock-agentcore-control", region_name=REGION)


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    registry_id: str = props.get("registryId", "")
    logger.info("Resolved registryId: %r (len=%d)", registry_id, len(registry_id))

    if req == "Create":
        agent_card_json: str = props["agentCard"]  # already a JSON string
        resp = _client.create_registry_record(
            registryId=registry_id,
            name=props["name"],
            description=props.get("description", props["name"]),
            recordVersion=props["recordVersion"],
            descriptorType="AGENT",
            descriptors={
                "agent": {
                    "card": {
                        "schemaVersion": "0.3",
                        "inlineContent": agent_card_json,
                    },
                },
            },
        )
        logger.info("CREATE_REGISTRY_RECORD RESPONSE: %s", json.dumps(resp, default=str))
        record_id = resp.get("recordId") or ""
        record_arn = resp.get("recordArn") or ""

        if _truthy(props.get("submitForApproval", "true")):
            try:
                sub_resp = _client.submit_registry_record_for_approval(
                    registryId=registry_id,
                    recordId=record_id,
                )
                logger.info("SUBMIT RESPONSE: %s", json.dumps(sub_resp, default=str))
            except ClientError as e:
                # Don't fail the record creation if submit fails — record still exists.
                logger.warning("submit_registry_record_for_approval failed: %s", e)

        return {
            "PhysicalResourceId": record_id or f"{registry_id}/{props['name']}",
            "Data": {
                "recordId": record_id,
                "recordArn": record_arn,
            },
        }

    if req == "Update":
        # Re-creation on prop change is handled by CloudFormation via
        # PhysicalResourceId — return existing.
        return {
            "PhysicalResourceId": event["PhysicalResourceId"],
            "Data": {"recordId": event["PhysicalResourceId"]},
        }

    if req == "Delete":
        record_id = event["PhysicalResourceId"]
        try:
            _client.delete_registry_record(
                registryId=registry_id,
                recordId=record_id,
            )
            logger.info("delete_registry_record succeeded: %s", record_id)
        except ClientError as e:
            logger.warning("delete_registry_record failed (continuing): %s", e)
        return {"PhysicalResourceId": record_id}

    raise ValueError(f"unknown request type: {req}")
