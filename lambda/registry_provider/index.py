"""Custom resource provider for Bedrock AgentCore Agent Registry.

We bypass CDK's AwsCustomResource (JS) and use boto3 directly because:

1. The preview API's response field names need to be observed at runtime —
   we log the full response to CloudWatch so we can verify what's returned.
2. AwsCustomResource serialises parameters into a JSON string in the CFn
   template; cross-stack tokens may not always be resolved cleanly here.
   Using cdk.CustomResource + our own Lambda passes properties through
   the standard CFn custom-resource ResourceProperties contract, which
   CloudFormation always fully resolves before invoking the Lambda.
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

    if req == "Create":
        # Parameter shape per the preview boto3 model (validated against the
        # service via "Unknown parameter" errors):
        #   name, description, authorizerType, authorizerConfiguration,
        #   clientToken, approvalConfiguration
        resp = _client.create_registry(
            name=props["name"],
            description=props.get("description", ""),
            authorizerType="IAM",
            approvalConfiguration={
                "autoApprovalEnabled": _truthy(props.get("autoApprove", "true")),
            },
        )
        logger.info("CREATE_REGISTRY RESPONSE: %s", json.dumps(resp, default=str))

        registry_id = resp.get("registryId") or ""
        registry_arn = resp.get("registryArn") or ""
        # PhysicalResourceId must be non-empty and stable. Use registryId if
        # present, else registryArn, else the registry name (which we control).
        physical_id = registry_id or registry_arn or props["name"]
        return {
            "PhysicalResourceId": physical_id,
            "Data": {
                "registryId": registry_id,
                "registryArn": registry_arn,
                "name": props["name"],
            },
        }

    if req == "Update":
        # Registries are effectively immutable in this prototype; return the
        # existing physical resource ID so CloudFormation considers it unchanged.
        old_props = event.get("OldResourceProperties", {}) or {}
        return {
            "PhysicalResourceId": event["PhysicalResourceId"],
            "Data": {
                "registryId": event["PhysicalResourceId"],
                "registryArn": old_props.get("_existingArn", ""),
                "name": props.get("name", old_props.get("name", "")),
            },
        }

    if req == "Delete":
        physical_id = event["PhysicalResourceId"]
        try:
            _client.delete_registry(registryId=physical_id)
            logger.info("delete_registry succeeded for %s", physical_id)
        except ClientError as e:
            # Don't fail stack rollback on delete errors (registry might already
            # be gone, or the physical id might be a fallback value).
            logger.warning("delete_registry failed (continuing): %s", e)
        return {"PhysicalResourceId": physical_id}

    raise ValueError(f"unknown request type: {req}")
