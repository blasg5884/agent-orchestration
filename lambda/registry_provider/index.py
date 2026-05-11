"""Custom resource provider for Bedrock AgentCore Agent Registry.

Schema verified against boto3 1.43.6 service model
(botocore/data/bedrock-agentcore-control/2023-06-05/service-2.json).

Lifecycle handled:
  1. Ensure the AgentCore service-linked role exists (so the AgentCore service
     itself can provision sub-resources like the registry's internal workload
     identity). Without the SLR, create_registry returns CREATING but then
     transitions to CREATE_FAILED with console error
     "Unable to create workload identity because access was denied".
  2. Call create_registry.
  3. Poll get_registry until status reaches READY (or a terminal error state)
     before returning success — downstream record creation requires READY.

CreateRegistry input:
  name*, description, authorizerType ('CUSTOM_JWT'|'AWS_IAM'),
  authorizerConfiguration, clientToken,
  approvalConfiguration { autoApproval: bool }

CreateRegistry output:
  registryArn   (registryId is NOT returned — extract from ARN)

Registry status enum:
  CREATING | READY | UPDATING | CREATE_FAILED | UPDATE_FAILED | DELETING | DELETE_FAILED
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

# Service-linked role for AgentCore. Created on first use; ignored if already exists.
AGENTCORE_SLR_SERVICE_NAME = "bedrock-agentcore.amazonaws.com"

READY_POLL_INTERVAL_SEC = 5
READY_POLL_TIMEOUT_SEC = 8 * 60  # Lambda timeout is 10 min; leave headroom.


def _extract_registry_id(arn: str) -> str:
    m = _REGISTRY_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def _ensure_service_linked_role() -> None:
    """Idempotently create the AgentCore service-linked role.

    The SLR lets the AgentCore service create internal sub-resources (workload
    identity, etc.) for resources it provisions on the customer's behalf.
    """
    try:
        _iam.create_service_linked_role(AWSServiceName=AGENTCORE_SLR_SERVICE_NAME)
        logger.info("Created service-linked role for %s", AGENTCORE_SLR_SERVICE_NAME)
        # Brief pause to let IAM propagate the SLR before AgentCore tries to use it.
        time.sleep(10)
    except ClientError as e:
        code = e.response.get("Error", {}).get("Code", "")
        if code in ("InvalidInput", "ServiceAccessNotEnabledException"):
            logger.warning("SLR creation skipped (%s): %s", code, e)
        elif "exists" in str(e).lower():
            logger.info("Service-linked role already exists — continuing")
        else:
            # Don't hard-fail: the role might already exist or the API may not
            # require it. Continue and let the registry creation surface the
            # real error if any.
            logger.warning("create_service_linked_role error (continuing): %s", e)


def _wait_for_registry_ready(registry_id: str) -> dict:
    """Poll get_registry until READY (or a terminal failure)."""
    deadline = time.time() + READY_POLL_TIMEOUT_SEC
    last: dict = {}
    while time.time() < deadline:
        resp = _control.get_registry(registryId=registry_id)
        status = resp.get("status")
        logger.info("Registry %s status=%s", registry_id, status)
        last = resp
        if status == "READY":
            return resp
        if status in ("CREATE_FAILED", "UPDATE_FAILED", "DELETING", "DELETE_FAILED"):
            reason = resp.get("statusReason") or "(no statusReason)"
            raise RuntimeError(
                f"Registry {registry_id} entered terminal state {status}: {reason}"
            )
        time.sleep(READY_POLL_INTERVAL_SEC)
    raise TimeoutError(
        f"Registry {registry_id} did not reach READY within {READY_POLL_TIMEOUT_SEC}s "
        f"(last status={last.get('status')})"
    )


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    if req == "Create":
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

        # Block until the registry is actually usable.
        ready = _wait_for_registry_ready(registry_id)
        logger.info("Registry READY: %s", json.dumps(ready, default=str))

        return {
            "PhysicalResourceId": registry_id,
            "Data": {
                "registryId": registry_id,
                "registryArn": registry_arn,
                "name": props["name"],
            },
        }

    if req == "Update":
        return {
            "PhysicalResourceId": event["PhysicalResourceId"],
            "Data": {
                "registryId": event["PhysicalResourceId"],
                "registryArn": props.get("_existingArn", ""),
                "name": props.get("name", ""),
            },
        }

    if req == "Delete":
        registry_id = event["PhysicalResourceId"]
        try:
            _control.delete_registry(registryId=registry_id)
            logger.info("delete_registry succeeded for %s", registry_id)
        except ClientError as e:
            logger.warning("delete_registry failed (continuing): %s", e)
        return {"PhysicalResourceId": registry_id}

    raise ValueError(f"unknown request type: {req}")
