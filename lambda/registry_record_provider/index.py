"""Custom resource provider for Bedrock AgentCore Agent Registry records (A2A type).

Schema verified against boto3 1.43.6 service model:

  CreateRegistryRecord input:
    registryId*     (arn:aws...:registry/)?[a-zA-Z0-9]{12,16}
    name*           [a-zA-Z0-9][a-zA-Z0-9_\\-\\.\\/]*
    description     Description
    descriptorType* enum ['MCP', 'A2A', 'CUSTOM', 'AGENT_SKILLS']
    descriptors     structure:
                      a2a.agentCard.{ schemaVersion, inlineContent }
                      mcp.server.{...}, mcp.tools.{...}
                      custom.{ inlineContent }
                      agentSkills.{ skillMd, skillDefinition }
    recordVersion   pattern [a-zA-Z0-9.-]+
    clientToken
  CreateRegistryRecord output:
    recordArn*  arn:...:registry/<rid>/record/<recordId 12-char>
    status*     enum
    (NOTE: recordId not returned — extract from recordArn)

  DeleteRegistryRecord input:
    registryId* recordId* (recordId pattern: (arn ...)?[a-zA-Z0-9]{12})

  SubmitRegistryRecordForApproval input:
    registryId* recordId*
  SubmitRegistryRecordForApproval output:
    registryArn*, recordArn*, recordId*, status*, updatedAt*
"""
from __future__ import annotations

import json
import logging
import os
import re
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
_client = boto3.client("bedrock-agentcore-control", region_name=REGION)

# Trailing 12-char segment of a record ARN: ".../record/<recordId>"
_RECORD_ID_FROM_ARN = re.compile(r"/record/([a-zA-Z0-9]{12})$")


def _extract_record_id(arn: str) -> str:
    m = _RECORD_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    registry_id: str = props.get("registryId", "")
    logger.info("Resolved registryId=%r (len=%d)", registry_id, len(registry_id))

    if req == "Create":
        agent_card_json: str = props["agentCard"]  # already a JSON string
        resp = _client.create_registry_record(
            registryId=registry_id,
            name=props["name"],
            description=props.get("description", props["name"]),
            recordVersion=props["recordVersion"],
            descriptorType="A2A",
            descriptors={
                "a2a": {
                    "agentCard": {
                        "schemaVersion": "0.3",
                        "inlineContent": agent_card_json,
                    },
                },
            },
        )
        logger.info("CREATE_REGISTRY_RECORD RESPONSE: %s", json.dumps(resp, default=str))
        record_arn = resp.get("recordArn") or ""
        record_id = _extract_record_id(record_arn)
        if not record_id:
            raise RuntimeError(
                f"could not extract recordId from arn={record_arn!r}"
            )

        if _truthy(props.get("submitForApproval", "true")):
            try:
                sub_resp = _client.submit_registry_record_for_approval(
                    registryId=registry_id,
                    recordId=record_id,
                )
                logger.info("SUBMIT RESPONSE: %s", json.dumps(sub_resp, default=str))
            except ClientError as e:
                # The record is created either way; don't fail the resource.
                logger.warning("submit_registry_record_for_approval failed: %s", e)

        return {
            "PhysicalResourceId": record_id,
            "Data": {
                "recordId": record_id,
                "recordArn": record_arn,
            },
        }

    if req == "Update":
        # Re-creation handled by CFn via physical resource id changes.
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
