"""Orchestrator agent.

Discovers sub-agents via the AgentCore Agent Registry MCP endpoint
(https://bedrock-agentcore.<region>.amazonaws.com/registry/<registryId>/mcp),
which exposes the `search_registry_records` tool with semantic search.

Dispatch flow:
  1. LLM calls `search_registry_records` (provided by the Registry MCP)
     with a natural-language query like "weather" or "japan postal code".
     It can additionally filter by descriptorType / version / name.
  2. From the returned record summaries the LLM picks an agent and calls
     our custom `invoke_subagent(record_id, prompt)` tool, which:
       - resolves the record's agent card via get_registry_record
       - extracts the runtime ARN from the card
       - calls bedrock-agentcore:InvokeAgentRuntime to dispatch.

This agent itself performs NO domain work — it only routes.
"""
from __future__ import annotations

import json
import logging
import os
import sys
import traceback
import uuid
from typing import Any

import boto3
from bedrock_agentcore.runtime import BedrockAgentCoreApp

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s | %(message)s",
    stream=sys.stdout,
    force=True,
)
logger = logging.getLogger("orchestrator")
logger.setLevel(logging.INFO)

REGION = os.environ.get("AWS_REGION", "ap-northeast-1")
REGISTRY_ID = os.environ.get("AGENT_REGISTRY_ID", "")
REGISTRY_ARN = os.environ.get("AGENT_REGISTRY_ARN", "")
MODEL_ID = os.environ.get(
    "ORCHESTRATOR_MODEL_ID",
    # Global cross-region inference profile for Claude Sonnet 4.6.
    # Override with ORCHESTRATOR_MODEL_ID env var if you have a different
    # model enabled in Bedrock.
    "global.anthropic.claude-sonnet-4-6",
)
REGISTRY_MCP_URL = (
    f"https://bedrock-agentcore.{REGION}.amazonaws.com/registry/{REGISTRY_ID}/mcp"
)

logger.info(
    "boot: REGION=%s REGISTRY_ID=%r REGISTRY_ARN=%r MODEL_ID=%r MCP_URL=%r",
    REGION, REGISTRY_ID, REGISTRY_ARN, MODEL_ID, REGISTRY_MCP_URL,
)

# Defer heavy imports until after logging is configured so import-time
# failures appear in the same stream.
try:
    from strands import Agent, tool
    from strands.tools.mcp.mcp_client import MCPClient
    from mcp_proxy_for_aws.client import aws_iam_streamablehttp_client
    logger.info("imports OK: strands.Agent + MCPClient + aws_iam_streamablehttp_client")
except Exception:
    logger.exception("FATAL: import failed")
    raise

_app = BedrockAgentCoreApp()
_agentcore = boto3.client("bedrock-agentcore", region_name=REGION)
_agentcore_control = boto3.client("bedrock-agentcore-control", region_name=REGION)


# Starlette-style middleware: log every incoming HTTP request to confirm
# AgentCore reaches us and to show the body shape.
@_app.middleware("http")
async def _log_requests(request, call_next):
    try:
        body = await request.body()
    except Exception:
        body = b"(could not read body)"
    logger.info(
        "HTTP %s %s ct=%r len=%d body=%r",
        request.method, request.url.path, request.headers.get("content-type"),
        len(body) if isinstance(body, (bytes, bytearray)) else -1,
        body[:500] if isinstance(body, (bytes, bytearray)) else body,
    )

    async def _replay():
        return {"type": "http.request", "body": body, "more_body": False}
    request._receive = _replay

    try:
        response = await call_next(request)
    except Exception:
        logger.exception("middleware: unhandled exception during request")
        raise
    logger.info("HTTP %s %s -> %d", request.method, request.url.path, getattr(response, "status_code", "?"))
    return response


@_app.exception_handler(Exception)
async def _global_exception_handler(request, exc):
    logger.exception("UNHANDLED %s %s: %s", request.method, request.url.path, exc)
    from starlette.responses import JSONResponse
    return JSONResponse(status_code=500, content={"error": f"{type(exc).__name__}: {exc}"})


def _make_session_id() -> str:
    """invoke_agent_runtime requires runtimeSessionId length in [33, 100]."""
    return uuid.uuid4().hex + uuid.uuid4().hex[:1]  # 33 chars


def _resolve_runtime_arn_for_record(record_id: str) -> str:
    """Look up the AgentCore Runtime ARN stored in a registry record's agent card."""
    rec = _agentcore_control.get_registry_record(
        registryId=REGISTRY_ARN, recordId=record_id,
    )
    descriptors = rec.get("descriptors") or {}
    a2a = descriptors.get("a2a") or {}
    card_def = a2a.get("agentCard") or {}
    inline = card_def.get("inlineContent")
    if not inline:
        raise RuntimeError(
            f"record {record_id} has no descriptors.a2a.agentCard.inlineContent"
        )
    card = json.loads(inline) if isinstance(inline, str) else inline
    url = card.get("url")
    if not url:
        raise RuntimeError(f"agent card for record {record_id} has no 'url' field")
    return url


def _build_a2a_send_message_request(prompt: str) -> dict:
    """Construct an A2A protocol JSON-RPC 2.0 `message/send` request body.

    Sub-agents run a Strands A2AServer (a2a-sdk JSON-RPC server) which
    rejects raw payloads — its JSONRPCRequest validator requires the
    standard {jsonrpc, id, method, params} envelope. The `message/send`
    method's params shape is documented by the A2A spec:
      params.message: { kind: 'message', messageId, role, parts: [{kind:'text',text}] }
    """
    return {
        "jsonrpc": "2.0",
        "id": str(uuid.uuid4()),
        "method": "message/send",
        "params": {
            "message": {
                "kind": "message",
                "messageId": str(uuid.uuid4()),
                "role": "user",
                "parts": [{"kind": "text", "text": prompt}],
            }
        },
    }


def _extract_text_from_a2a_response(parsed: dict) -> str:
    """Pull the agent's textual reply out of an A2A JSON-RPC response.

    The `result` field can either be a `Task` object (with `artifacts` /
    `status.message`) or a `Message` object directly. We try the most
    common shapes and fall back to a JSON dump.
    """
    if "error" in parsed:
        return f"A2A error: {parsed['error']}"
    result = parsed.get("result") or {}

    # Direct Message result.
    parts = result.get("parts")
    if parts:
        return "".join(p.get("text", "") for p in parts if p.get("kind") == "text")

    # Task result: status.message.parts
    status = result.get("status") or {}
    status_msg = status.get("message") or {}
    parts = status_msg.get("parts")
    if parts:
        return "".join(p.get("text", "") for p in parts if p.get("kind") == "text")

    # Task result: artifacts[*].parts[*].text
    artifacts = result.get("artifacts") or []
    pieces: list[str] = []
    for art in artifacts:
        for p in art.get("parts", []) or []:
            if p.get("kind") == "text" and p.get("text"):
                pieces.append(p["text"])
    if pieces:
        return "\n".join(pieces)

    return json.dumps(result, ensure_ascii=False)


@tool
def invoke_subagent(record_id: str, prompt: str) -> str:
    """Invoke a sub-agent (registered in the Agent Registry) by its record id.

    Use this AFTER calling `search_registry_records` to find an appropriate
    agent. Pass the `recordId` you got from search and the user's prompt
    (in natural language). The sub-agent's response is returned as a string.

    Args:
        record_id: The recordId field from a search_registry_records result.
        prompt: The natural-language request to forward to the sub-agent.
    """
    logger.info("invoke_subagent record_id=%s prompt=%r", record_id, prompt)
    try:
        runtime_arn = _resolve_runtime_arn_for_record(record_id)
    except Exception as e:
        logger.exception("invoke_subagent: could not resolve runtime ARN")
        return f"failed to resolve sub-agent: {type(e).__name__}: {e}"

    session_id = _make_session_id()
    a2a_request = _build_a2a_send_message_request(prompt)
    logger.info(
        "invoke_subagent dispatching to %s session=%s a2a_id=%s",
        runtime_arn, session_id, a2a_request["id"],
    )
    try:
        resp = _agentcore.invoke_agent_runtime(
            agentRuntimeArn=runtime_arn,
            qualifier="DEFAULT",
            runtimeSessionId=session_id,
            payload=json.dumps(a2a_request).encode("utf-8"),
            contentType="application/json",
            accept="application/json",
        )
    except Exception as e:
        logger.exception("invoke_subagent: invoke_agent_runtime failed")
        return f"sub-agent invocation failed: {type(e).__name__}: {e}"

    body = resp["response"].read()
    text = body.decode("utf-8", errors="replace")
    logger.info("invoke_subagent response (first 500): %s", text[:500])
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError:
        return text
    if not isinstance(parsed, dict):
        return text
    return _extract_text_from_a2a_response(parsed)


@_app.entrypoint
def invoke(payload):
    logger.info("============ INVOKE CALLED ============")
    logger.info("INVOKE raw payload: %r (type=%s)", payload, type(payload).__name__)
    if not isinstance(payload, dict):
        try:
            payload = json.loads(payload) if isinstance(payload, (str, bytes, bytearray)) else dict(payload)
        except Exception:
            logger.exception("INVOKE could not coerce payload to dict")
            return {"error": f"unsupported payload type: {type(payload).__name__}"}
    prompt = payload.get("prompt") or payload.get("input") or ""
    if not prompt:
        return {"error": "missing 'prompt' in payload"}

    # Build the Registry MCP client per-invoke (its lifecycle is tied to a
    # context manager, and Strands lazy-initialises tool connections inside
    # the same scope as the Agent call).
    try:
        mcp_factory = lambda: aws_iam_streamablehttp_client(  # noqa: E731
            endpoint=REGISTRY_MCP_URL,
            aws_region=REGION,
            aws_service="bedrock-agentcore",
        )
    except Exception as e:
        logger.exception("could not build MCP factory")
        return {"error": f"mcp factory build failed: {type(e).__name__}: {e}"}

    try:
        with MCPClient(mcp_factory) as mcp_client:
            mcp_tools = mcp_client.list_tools_sync()
            logger.info("Registry MCP exposed %d tool(s): %s", len(mcp_tools), [getattr(t, "name", t) for t in mcp_tools])

            agent = Agent(
                name="orchestrator",
                description="ユーザーのリクエストを適切なサブエージェントに振り分けるオーケストレーター。",
                system_prompt=(
                    "あなたはオーケストレーターエージェントです。あなた自身は処理を実行しません。\n"
                    "Agent Registry の `search_registry_records` ツール（自然言語による検索）で"
                    "リクエストに最も適したサブエージェントを探してください。\n"
                    "filter で {\"descriptorType\": {\"$eq\": \"A2A\"}} を指定すると A2A サブエージェントだけに絞り込めます。\n"
                    "見つかったレコードの `recordId` を使って `invoke_subagent(record_id, prompt)` を呼び、"
                    "その回答をそのままユーザーに返してください。\n"
                    "適切なサブエージェントが見つからない場合は、その旨を日本語で説明してください。"
                ),
                model=MODEL_ID,
                tools=[*mcp_tools, invoke_subagent],
                callback_handler=None,
            )
            logger.info("Agent built — invoking with prompt: %r", prompt)
            result = agent(prompt)
            return {"result": str(result)}
    except Exception as e:
        tb = traceback.format_exc()
        logger.error("orchestrator invoke failure:\n%s", tb)
        return {
            "error": f"orchestrator failed: {type(e).__name__}: {e}",
            "trace": tb,
        }


if __name__ == "__main__":
    try:
        from importlib.metadata import version as _pkg_version
        logger.info("bedrock-agentcore version: %s", _pkg_version("bedrock-agentcore"))
        logger.info("mcp-proxy-for-aws version: %s", _pkg_version("mcp-proxy-for-aws"))
    except Exception:
        logger.exception("could not read package versions")

    port = int(os.environ.get("PORT", "8080"))
    host_override = os.environ.get("HOST")
    logger.info("starting BedrockAgentCoreApp (port=%d, host_override=%r)", port, host_override)
    try:
        if host_override:
            _app.run(port=port, host=host_override)
        else:
            _app.run(port=port)
    except Exception:
        logger.exception("_app.run() raised unexpectedly")
        raise
