"""Custom resource provider for Bedrock AgentCore Agent Registry.

Schema verified against boto3 1.43.6 service-2.json
(botocore/data/bedrock-agentcore-control/2023-06-05).

Lifecycle handled:
  Create:
    1. Ensure the AgentCore service-linked role exists so AgentCore can
       provision the registry's internal workload identity.
    2. create_registry.
    3. Poll get_registry until status=READY and remains READY for a
       stable window (guards against READY → CREATE_FAILED races during
       async workload-identity setup).
  Update:
    Self-healing: if the existing registry (by PhysicalResourceId) is gone
    in AgentCore (drift), recreate it and return a new PhysicalResourceId
    so CloudFormation performs a REPLACEMENT and downstream consumers pick
    up the fresh ID via SSM.
  Delete:
    delete_registry, tolerating "not found" so destroy is idempotent.
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
_control = boto3.client("bedrock-agentcore-control", region_name=REGION)
_iam = boto3.client("iam")

_REGISTRY_ID_FROM_ARN = re.compile(r":registry/([a-zA-Z0-9]{12,16})$")

AGENTCORE_SLR_SERVICE_NAME = "bedrock-agentcore.amazonaws.com"

READY_POLL_INTERVAL_SEC = 5
READY_POLL_TIMEOUT_SEC = 8 * 60
# Require the registry to remain READY this long before we trust it —
# AgentCore's async workload-identity setup can briefly report READY before
# transitioning to CREATE_FAILED if SLR-backed provisioning fails.
REQUIRED_STABLE_READY_SEC = 30

TERMINAL_FAILURE = {"CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED"}


def _extract_registry_id(arn: str) -> str:
    m = _REGISTRY_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def _ensure_service_linked_role() -> None:
    """Idempotently create the AgentCore service-linked role."""
    try:
        _iam.create_service_linked_role(AWSServiceName=AGENTCORE_SLR_SERVICE_NAME)
        logger.info("Created service-linked role for %s", AGENTCORE_SLR_SERVICE_NAME)
        # Give IAM and AgentCore a moment to propagate the SLR.
        time.sleep(15)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if "exists" in str(e).lower() or code in ("InvalidInput",):
            logger.info("SLR already exists (or InvalidInput meaning exists) — continuing")
        else:
            logger.warning("create_service_linked_role error (continuing): %s", e)


def _registry_exists(registry_id: str) -> bool:
    try:
        _control.get_registry(registryId=registry_id)
        return True
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code == "ResourceNotFoundException":
            return False
        raise


def _wait_for_registry_ready(registry_id: str) -> dict:
    deadline = time.time() + READY_POLL_TIMEOUT_SEC
    last: dict = {}
    ready_since: float | None = None
    while time.time() < deadline:
        try:
            resp = _control.get_registry(registryId=registry_id)
        except ClientError as e:
            code = e.response.get("Error", {}).get("Code", "")
            if code == "ResourceNotFoundException":
                # Brief eventual-consistency window after create_registry.
                logger.warning("get_registry not-found yet for %s, retrying", registry_id)
                ready_since = None
                time.sleep(READY_POLL_INTERVAL_SEC)
                continue
            raise
        status = resp.get("status")
        logger.info("Registry %s status=%s", registry_id, status)
        last = resp
        if status == "READY":
            if ready_since is None:
                ready_since = time.time()
            elif (time.time() - ready_since) >= REQUIRED_STABLE_READY_SEC:
                return resp
        else:
            ready_since = None
        if status in TERMINAL_FAILURE:
            reason = resp.get("statusReason") or "(no statusReason)"
            raise RuntimeError(
                f"Registry {registry_id} entered terminal state {status}: {reason}"
            )
        time.sleep(READY_POLL_INTERVAL_SEC)
    raise TimeoutError(
        f"Registry {registry_id} did not reach stable READY within {READY_POLL_TIMEOUT_SEC}s "
        f"(last status={last.get('status')})"
    )


def _create_registry(props: dict) -> tuple[str, str]:
    _ensure_service_linked_role()
    resp = _control.create_registry(
        name=props["name"],
        description=props.get("description", ""),
        authorizerType="AWS_IAM",
        approvalConfiguration={
            "autoApproval": _truthy(props.get("autoApprove", "true")),
        },
    )
    logger.info("CREATE_REGISTRY RESPONSE: %s", json.dumps(resp, default=str))
    registry_arn = resp.get("registryArn") or ""
    registry_id = _extract_registry_id(registry_arn)
    if not registry_id:
        raise RuntimeError(f"could not extract registryId from arn={registry_arn!r}")
    _wait_for_registry_ready(registry_id)
    return registry_id, registry_arn


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    if req == "Create":
        registry_id, registry_arn = _create_registry(props)
        return {
            "PhysicalResourceId": registry_id,
            "Data": {
                "registryId": registry_id,
                "registryArn": registry_arn,
                "name": props["name"],
            },
        }

    if req == "Update":
        old_id = event["PhysicalResourceId"]
        # Self-heal: if CloudFormation's recorded registry has been deleted out
        # of band (e.g. AgentCore async cleanup after a previous failed run),
        # recreate. Returning a NEW PhysicalResourceId tells CloudFormation to
        # treat this as a Replacement.
        if _registry_exists(old_id):
            logger.info("Existing registry %s is alive — no-op update", old_id)
            return {
                "PhysicalResourceId": old_id,
                "Data": {
                    "registryId": old_id,
                    "registryArn": f"arn:aws:bedrock-agentcore:{REGION}:*:registry/{old_id}",
                    "name": props.get("name", ""),
                },
            }
        logger.warning(
            "Registry %s no longer exists in AgentCore — recreating to recover from drift",
            old_id,
        )
        registry_id, registry_arn = _create_registry(props)
        return {
            "PhysicalResourceId": registry_id,
            "Data": {
                "registryId": registry_id,
                "registryArn": registry_arn,
                "name": props["name"],
            },
        }

    if req == "Delete":
        registry_id = event["PhysicalResourceId"]
        try:
            _control.delete_registry(registryId=registry_id)
            logger.info("delete_registry succeeded for %s", registry_id)
        except ClientError as e:
            logger.warning("delete_registry failed (tolerated): %s", e)
        return {"PhysicalResourceId": registry_id}

    raise ValueError(f"unknown request type: {req}")
