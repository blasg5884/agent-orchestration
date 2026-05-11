"""Custom resource provider for Bedrock AgentCore Agent Registry records (A2A)."""
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

TERMINAL_FAIL = {"CREATE_FAILED", "UPDATE_FAILED", "REJECTED"}
POST_SUBMIT_STABLE = {"APPROVED", "PENDING_APPROVAL"}


def _extract_record_id(arn: str) -> str:
    m = _RECORD_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def _verify_registry_visible(registry_id: str) -> None:
    """Ensure the registry is visible AND READY from this Lambda's session.

    Handles eventual consistency: AgentCore APIs can take a few seconds before
    a newly-created registry is visible to subsequent control-plane operations.
    Also catches the case where CloudFormation thinks the registry exists but
    AgentCore has cleaned it up (drift), giving the operator a clear error.
    """
    deadline = time.time() + 90  # up to 90s of retries
    last_err: Exception | None = None
    last_status: str | None = None
    while time.time() < deadline:
        try:
            resp = _client.get_registry(registryId=registry_id)
            last_status = resp.get("status")
            logger.info("Registry %s visible, status=%s", registry_id, last_status)
            if last_status == "READY":
                return
            if last_status in ("CREATE_FAILED", "DELETE_FAILED", "DELETING"):
                reason = resp.get("statusReason") or "(no reason)"
                raise RuntimeError(
                    f"Registry {registry_id} is in {last_status}: {reason}. "
                    f"Recovery: run `cdk destroy AgentRegistryStack` then `cdk deploy --all`."
                )
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                last_err = e
                logger.warning(
                    "Registry %s not visible yet (will retry)", registry_id
                )
            else:
                raise
        time.sleep(POLL_INTERVAL_SEC)
    if last_status is None:
        raise RuntimeError(
            f"Registry {registry_id} not found in AgentCore. CloudFormation state is "
            f"stale (registry was likely cleaned up by AgentCore after an earlier "
            f"failed deployment). Recovery: `cdk destroy AgentRegistryStack` then "
            f"`cdk deploy --all`.  Last error: {last_err}"
        )
    raise RuntimeError(
        f"Registry {registry_id} never became READY (last status={last_status})"
    )


def _create_record_with_retry(registry_id: str, **kwargs: Any) -> dict:
    """Wraps create_registry_record with retry on transient ResourceNotFoundException."""
    last_err: Exception | None = None
    for attempt in range(1, 7):
        try:
            return _client.create_registry_record(registryId=registry_id, **kwargs)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code != "ResourceNotFoundException":
                raise
            last_err = e
            backoff = 5 * attempt
            logger.warning(
                "create_registry_record attempt %d: registry %s not found, retrying in %ds",
                attempt,
                registry_id,
                backoff,
            )
            time.sleep(backoff)
    raise RuntimeError(
        f"create_registry_record gave up after retries; registry {registry_id} not "
        f"visible to this API path. Last error: {last_err}"
    )


def _wait_for_record_status(registry_id: str, record_id: str, acceptable: set[str]) -> dict:
    deadline = time.time() + POLL_TIMEOUT_SEC
    last: dict = {}
    while time.time() < deadline:
        resp = _client.get_registry_record(registryId=registry_id, recordId=record_id)
        status = resp.get("status")
        logger.info("Record %s status=%s", record_id, status)
        last = resp
        if status in acceptable:
            return resp
        if status in TERMINAL_FAIL:
            reason = resp.get("statusReason") or "(no reason)"
            raise RuntimeError(f"Record {record_id} terminal state {status}: {reason}")
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
        # Verify the registry exists and is READY from THIS Lambda's session,
        # then call createRegistryRecord with retry to absorb any residual
        # eventual-consistency between get_registry and create_registry_record.
        _verify_registry_visible(registry_id)

        agent_card_json: str = props["agentCard"]
        resp = _create_record_with_retry(
            registry_id,
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

        post_create = _wait_for_record_status(
            registry_id, record_id, {"DRAFT"} | POST_SUBMIT_STABLE
        )
        logger.info("Record post-create status=%s", post_create.get("status"))

        if _truthy(props.get("submitForApproval", "true")) and post_create.get("status") == "DRAFT":
            try:
                sub_resp = _client.submit_registry_record_for_approval(
                    registryId=registry_id,
                    recordId=record_id,
                )
                logger.info("SUBMIT RESPONSE: %s", json.dumps(sub_resp, default=str))
                _wait_for_record_status(registry_id, record_id, POST_SUBMIT_STABLE)
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
            logger.warning("delete_registry_record failed (tolerated): %s", e)
        return {"PhysicalResourceId": record_id}

    raise ValueError(f"unknown request type: {req}")
