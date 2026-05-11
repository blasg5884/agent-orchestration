"""Custom resource provider for Bedrock AgentCore Agent Registry.

Schema verified against boto3 1.43.6 service model
(botocore/data/bedrock-agentcore-control/2023-06-05/service-2.json):

  CreateRegistry input:
    name*               RegistryName pattern [a-zA-Z0-9][a-zA-Z0-9_\\-\\.\\/]*
    description         Description
    authorizerType      enum ['CUSTOM_JWT', 'AWS_IAM']
    authorizerConfiguration  structure (only required for CUSTOM_JWT)
    clientToken         ClientToken
    approvalConfiguration    structure { autoApproval: bool }
  CreateRegistry output:
    registryArn*        arn:aws...:registry/[a-zA-Z0-9]{12,16}
    (NOTE: registryId is NOT returned — must be extracted from the ARN)

  DeleteRegistry input:
    registryId*         (arn:aws...:registry/)?[a-zA-Z0-9]{12,16}
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

# Matches the trailing segment of a registry ARN: ".../registry/<id>"
_REGISTRY_ID_FROM_ARN = re.compile(r":registry/([a-zA-Z0-9]{12,16})$")


def _extract_registry_id(arn: str) -> str:
    """Pull the short registry ID out of the ARN suffix."""
    m = _REGISTRY_ID_FROM_ARN.search(arn or "")
    return m.group(1) if m else ""


def _truthy(v: Any) -> bool:
    return str(v).lower() in ("true", "1", "yes")


def handler(event: dict, _context: Any) -> dict:
    logger.info("REQUEST EVENT: %s", json.dumps(event, default=str))
    req: str = event["RequestType"]
    props: dict = event.get("ResourceProperties", {}) or {}

    if req == "Create":
        resp = _client.create_registry(
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
            raise RuntimeError(
                f"could not extract registryId from arn={registry_arn!r}"
            )
        logger.info("Extracted registryId=%r from registryArn=%r", registry_id, registry_arn)

        return {
            "PhysicalResourceId": registry_id,
            "Data": {
                "registryId": registry_id,
                "registryArn": registry_arn,
                "name": props["name"],
            },
        }

    if req == "Update":
        # Registries are effectively immutable in this prototype — return existing.
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
            _client.delete_registry(registryId=registry_id)
            logger.info("delete_registry succeeded for %s", registry_id)
        except ClientError as e:
            # Don't block stack rollback on delete failures.
            logger.warning("delete_registry failed (continuing): %s", e)
        return {"PhysicalResourceId": registry_id}

    raise ValueError(f"unknown request type: {req}")
