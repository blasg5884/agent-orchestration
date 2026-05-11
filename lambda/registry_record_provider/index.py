"""Custom resource provider for Bedrock AgentCore Agent Registry records (A2A type).

Schema verified against boto3 1.43.6 service-2.json.

Lifecycle:
  1. create_registry_record (returns recordArn + status CREATING).
  2. Poll get_registry_record until status leaves CREATING.
  3. If submitForApproval=true, submit_registry_record_for_approval.
  4. Poll until APPROVED (when auto-approval is on) or PENDING_APPROVAL
     (when manual approval) — either is a stable post-submit state.

Record status enum:
  DRAFT | PENDING_APPROVAL | APPROVED | REJECTED | DEPRECATED |
  CREATING | UPDATING | CREATE_FAILED | UPDATE_FAILED

CreateRegistryRecord input:
  registryId*, name*, descriptorType* ('MCP'|'A2A'|'CUSTOM'|'AGENT_SKILLS'),
  descriptors (A2A → {a2a:{agentCard:{schemaVersion,inlineContent}}}),
  recordVersion, description, clientToken
CreateRegistryRecord output:
  recordArn + status  (recordId not directly in output; extract from ARN)
"""
from __future__ import annotations

import json
import logging
import os
import re
import time
from typing import Any

import boto3
from botocore.exceptions import ClientError

logger = logging.getLogger()
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
_client = boto3.client("bedrock-agentcore-control", region_name=REGION)

_RECORD_ID_FROM_ARN = re.compile(r"/record/([a-zA-Z0-9]{12})$")

POLL_INTERVAL_SEC = 5
POLL_TIMEOUT_SEC = 8 * 60

CREATING_STATES = {"CREATING", "UPDATING"}
TERMINAL_FAIL_STATES = {"CREATE_FAILED", "UPDATE_FAILED", "REJECTED"}
POST_SUBMIT_STABLE = {"APPROVED", "PENDING_APPROVAL"}


def _extract_record_id(arn: str) -> str:
    m = _RECORD_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def _wait_for(registry_id: str, record_id: str, acceptable: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT_SEC
    last: dict = {}
    while time.time() < deadline:
        resp = _client.get_registry_record(registryId=registry_id, recordId=record_id)
        status = resp.get("status")
        logger.info("Record %s status=%s", record_id, status)
        last = resp
        if status in acceptable:
            return resp
        if status in TERMINAL_FAIL_STATES:
            reason = resp.get("statusReason") or "(no statusReason)"
            raise RuntimeError(
                f"Record {record_id} terminal state {status}: {reason}"
            )
        time.sleep(POLL_INTERVAL_SEC)
    raise TimeoutError(
        f"Record {record_id} did not reach {acceptable} within {POLL_TIMEOUT_SEC}s "
        f"(last={last.get('status')})"
    )


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    registry_id: str = props.get("registryId", "")
    logger.info("registryId=%r (len=%d)", registry_id, len(registry_id))

    if req == "Create":
        agent_card_json: str = props["agentCard"]
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
            raise RuntimeError(f"could not extract recordId from arn={record_arn!r}")

        # Wait until the record leaves CREATING (typically lands in DRAFT).
        post_create = _wait_for(registry_id, record_id, {"DRAFT"} | POST_SUBMIT_STABLE)
        logger.info("Record post-create stable: status=%s", post_create.get("status"))

        if _truthy(props.get("submitForApproval", "true")) and post_create.get("status") == "DRAFT":
            try:
                sub_resp = _client.submit_registry_record_for_approval(
                    registryId=registry_id,
                    recordId=record_id,
                )
                logger.info("SUBMIT RESPONSE: %s", json.dumps(sub_resp, default=str))
                # Wait for APPROVED (auto-approval) or PENDING_APPROVAL (manual).
                _wait_for(registry_id, record_id, POST_SUBMIT_STABLE)
            except ClientError as e:
                logger.warning("submit_registry_record_for_approval failed: %s", e)

        return {
            "PhysicalResourceId": record_id,
            "Data": {
                "recordId": record_id,
                "recordArn": record_arn,
            },
        }

    if req == "Update":
        return {
            "PhysicalResourceId": event["PhysicalResourceId"],
            "Data": {"recordId": event["PhysicalResourceId"]},
        }

    if req == "Delete":
        record_id = event["PhysicalResourceId"]
        try:
            _client.delete_registry_record(registryId=registry_id, recordId=record_id)
            logger.info("delete_registry_record succeeded: %s", record_id)
        except ClientError as e:
            logger.warning("delete_registry_record failed (continuing): %s", e)
        return {"PhysicalResourceId": record_id}

    raise ValueError(f"unknown request type: {req}")
