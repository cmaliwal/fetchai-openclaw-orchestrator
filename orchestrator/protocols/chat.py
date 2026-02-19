"""
ASI:One Chat Protocol integration.

This protocol makes the Orchestrator Agent discoverable and usable
from ASI:One by implementing the standard ``AgentChatProtocol``.

Flow:
  1. ASI:One sends a ``ChatMessage`` containing the user's objective
  2. We acknowledge receipt immediately
  3. We run the objective through the planner
  4. If a paired connector exists → dispatch the task (async reply later)
  5. If no connector → execute locally for demo purposes
  6. Return results as a ``ChatMessage`` back to ASI:One

Reference:
  https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents
"""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from uuid import uuid4

from uagents import Context, Protocol
from uagents_core.contrib.protocols.chat import (
    ChatAcknowledgement,
    ChatMessage,
    StartSessionContent,
    TextContent,
    chat_protocol_spec,
)

from orchestrator.planner import plan_objective
from orchestrator.protocols.models import (
    TaskDispatchRequest,
)

logger = logging.getLogger(__name__)

# Create the protocol from the official spec so ASI:One recognises it
chat_proto = Protocol(spec=chat_protocol_spec)


# ---------------------------------------------------------------------------
# Helper – send a ChatMessage reply
# ---------------------------------------------------------------------------

async def send_chat_reply(ctx: Context, recipient: str, text: str):
    """Send a ChatMessage with text content back to the recipient."""
    await ctx.send(
        recipient,
        ChatMessage(
            timestamp=datetime.now(timezone.utc),
            msg_id=uuid4(),
            content=[TextContent(text=text)],
        ),
    )


# ---------------------------------------------------------------------------
# ChatMessage handler  (ASI:One → Orchestrator)
# ---------------------------------------------------------------------------

@chat_proto.on_message(ChatMessage)
async def handle_chat_message(ctx: Context, sender: str, msg: ChatMessage):
    """
    Receive a natural-language objective from ASI:One and process it.
    """
    from orchestrator.agent import (
        fetch_policy,
        orchestrator_private_key,
        pairing_store,
    )
    from shared.crypto import sign_payload

    # --- Acknowledge immediately ---------------------------------------------
    await ctx.send(
        sender,
        ChatAcknowledgement(
            acknowledged_msg_id=msg.msg_id,
            timestamp=datetime.now(timezone.utc),
        ),
    )

    # --- Extract text from message content -----------------------------------
    objective_text = ""
    for content in msg.content:
        if isinstance(content, StartSessionContent):
            ctx.logger.info("Chat session started by %s", sender)
            return  # nothing to process yet
        elif isinstance(content, TextContent):
            objective_text = content.text
        # Ignore other content types gracefully

    if not objective_text:
        await send_chat_reply(
            ctx, sender, "I didn't receive a text objective. Please send a message describing what you'd like me to do."
        )
        return

    ctx.logger.info("Chat objective from %s: %.120s", sender, objective_text)

    # --- Plan the objective --------------------------------------------------
    plan = plan_objective(objective_text)
    ctx.logger.info("Generated plan %s with %d steps", plan.task_id, len(plan.steps))

    # --- Fetch-side policy ---------------------------------------------------
    # Use sender address as user_id for ASI:One originated requests
    user_id = sender
    rejection = fetch_policy.validate(user_id, plan)
    if rejection is not None:
        await send_chat_reply(
            ctx,
            sender,
            f"Your request was rejected by policy: **{rejection.value}**\n\n"
            f"Task: `{plan.task_id}`",
        )
        return

    # --- Try to dispatch to a paired connector -------------------------------
    devices = pairing_store.devices_for_user(user_id)

    # Also check all devices as a fallback (for local testing)
    if not devices:
        devices = pairing_store.all_devices()

    if devices:
        device = devices[0]
        plan_dict = plan.model_dump(mode="json")
        plan_json = json.dumps(plan_dict, sort_keys=True, default=str)

        signature = ""
        if orchestrator_private_key is not None:
            signature = sign_payload(orchestrator_private_key, plan_dict)

        dispatch = TaskDispatchRequest(
            user_id=device.user_id,
            device_id=device.device_id,
            task_plan_json=plan_json,
            signature=signature,
        )

        # Store pending task for async result correlation (chat-originated)
        pending = ctx.storage.get("chat_pending") or "{}"
        pending_dict: dict = json.loads(pending)
        pending_dict[plan.task_id] = {
            "sender": sender,
            "objective": objective_text,
        }
        ctx.storage.set("chat_pending", json.dumps(pending_dict))

        connector_address = ctx.storage.get(
            f"connector:{device.user_id}:{device.device_id}"
        )
        if connector_address:
            ctx.logger.info(
                "Dispatching task %s to connector %s", plan.task_id, connector_address
            )
            await ctx.send(connector_address, dispatch)

            await send_chat_reply(
                ctx,
                sender,
                f"Your task has been planned and dispatched for execution.\n\n"
                f"**Task ID**: `{plan.task_id}`\n"
                f"**Steps**: {len(plan.steps)}\n"
                f"**Actions**: {', '.join(s.action for s in plan.steps)}\n\n"
                f"I'll send the results once execution completes.",
            )
            return
        else:
            ctx.logger.warning(
                "Connector address not found for %s:%s – falling back to local exec",
                device.user_id,
                device.device_id,
            )

    # --- Fallback: execute locally (demo / no connector paired) --------------
    ctx.logger.info("No connector available – executing plan locally")
    from connector.executor import execute_plan

    result = execute_plan(plan)

    # Format results as readable text
    result_lines = [
        f"## Task Completed: `{result.task_id}`",
        f"**Status**: {result.status.value}",
        "",
    ]
    for sr in result.step_results:
        emoji = "✅" if sr.status.value == "completed" else "❌"
        result_lines.append(f"{emoji} **{sr.action}**: {sr.status.value}")
        if sr.output and isinstance(sr.output, dict):
            for k, v in sr.output.items():
                if isinstance(v, str) and len(v) > 200:
                    result_lines.append(f"  - {k}: *(see below)*")
                else:
                    result_lines.append(f"  - {k}: {v}")
        if sr.error:
            result_lines.append(f"  - Error: {sr.error}")
        result_lines.append("")

    # Include the report text if available
    report_text = result.outputs.get("generate_report", {}).get("report_text")
    if report_text:
        result_lines.append("---")
        result_lines.append(report_text)

    await send_chat_reply(ctx, sender, "\n".join(result_lines))


# ---------------------------------------------------------------------------
# ChatAcknowledgement handler
# ---------------------------------------------------------------------------

@chat_proto.on_message(ChatAcknowledgement)
async def handle_chat_ack(ctx: Context, sender: str, msg: ChatAcknowledgement):
    ctx.logger.info(
        "Chat acknowledgement from %s for message %s",
        sender,
        msg.acknowledged_msg_id,
    )
