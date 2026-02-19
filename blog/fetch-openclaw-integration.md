# How Fetch Agents and OpenClaw Work Together to Run AI Tasks on Your Machine Safely

*Combining Fetch.ai's autonomous agent orchestration with OpenClaw's local execution runtime, without compromising user ownership or security.*

---

## The Problem: AI Agents Need Hands, Not Just Brains

Large Language Models can reason about objectives. Platforms like [ASI:One](https://asi1.ai) and [Agentverse](https://agentverse.ai) let users discover and talk to specialised AI agents. But when the task is **"generate my weekly dev report from local git repos and post a summary to Slack"**, those agents hit a wall.

They can *plan* the work. They can't *do* the work, because the data lives on your machine, not in the cloud.

The obvious fix, giving a remote agent shell access to your laptop, is a security nightmare. Cross-user misuse, uncontrolled command execution, leaked credentials. None of that is acceptable.

So we built something better.

---

## The Architecture: Plan Remotely, Execute Locally

Our integration connects three components:

| Component | Where it runs | What it does |
|---|---|---|
| **ASI:One** | Cloud (Fetch) | User sends a natural language objective |
| **Fetch Orchestrator Agent** | Agentverse (mailbox) | Plans the task using ASI:One LLM, enforces policy, dispatches work |
| **OpenClaw Connector** | Your machine | Verifies the request, executes it safely, returns results |

The key insight: **the agent that plans the work never touches your files**. And the service that executes the work **never accepts raw commands**, only declarative, policy-checked task plans.

```
User --> ASI:One --> Orchestrator Agent (Agentverse mailbox) --> [signed task plan] --> OpenClaw Connector --> Execution --> Results
```

---

## How We Built It: Step by Step

This section walks through the full technical implementation. If you are a developer looking to build something similar, this is your blueprint.

### Step 1: Create the Agent with uAgents

Everything starts with creating a [uAgent](https://github.com/fetchai/uAgents). The `uagents` Python framework gives you an `Agent` class that handles identity, messaging, protocol registration, and lifecycle.

```python
from uagents import Agent

agent = Agent(
    name="openclaw-orchestrator",
    seed="openclaw-orchestrator-dev-seed",   # deterministic identity
    port=8200,
    mailbox=True,                            # enable Agentverse mailbox
    network="testnet",                       # register on testnet Almanac
)
```

Key points:
- The `seed` generates a deterministic Ed25519 keypair. Same seed = same agent address every time.
- Setting `network="testnet"` registers the agent on the Fetch testnet Almanac, making it discoverable.
- The agent gets a permanent address like `agent1q<your-unique-address>`.

### Step 2: Add AgentChatProtocol for ASI:One Discovery

For the agent to be discoverable and usable from [ASI:One](https://asi1.ai), it needs to implement the standard [AgentChatProtocol](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents). This protocol defines how ASI:One sends chat messages and how agents respond.

```python
from uagents import Protocol
from uagents_core.contrib.protocols.chat import (
    ChatMessage,
    ChatAcknowledgement,
    TextContent,
    chat_protocol_spec,
)

# Create the protocol from the official spec
chat_proto = Protocol(spec=chat_protocol_spec)

@chat_proto.on_message(ChatMessage)
async def handle_chat_message(ctx, sender, msg):
    # 1. Acknowledge receipt immediately
    await ctx.send(sender, ChatAcknowledgement(
        acknowledged_msg_id=msg.msg_id,
        timestamp=datetime.now(timezone.utc),
    ))

    # 2. Extract the user's text objective
    for content in msg.content:
        if isinstance(content, TextContent):
            objective_text = content.text

    # 3. Plan the task, dispatch to connector, return results
    plan = plan_objective(objective_text)
    # ... (dispatch logic)

# Register it with the agent
agent.include(chat_proto, publish_manifest=True)
```

When you set `publish_manifest=True`, the protocol manifest is published to Agentverse. This is what makes ASI:One recognise and list your agent. Without it, ASI:One cannot find or communicate with your agent.

### Step 3: Run Through Agentverse Mailbox

Here is the critical piece for reachability. Your agent runs on your local machine (localhost:8200), but ASI:One is a cloud service. It cannot call `127.0.0.1`.

The solution is the **Agentverse mailbox**. When `mailbox=True` is set:

1. The agent registers on the Almanac with the Agentverse mailbox URL as its endpoint (not localhost).
2. When ASI:One sends a message, it goes to the Agentverse mailbox.
3. Your local agent **polls** Agentverse periodically and picks up messages.
4. Responses go back through the same relay.

To activate the mailbox, the agent calls Agentverse's `/connect` endpoint with your API key:

```python
import requests
requests.post('http://127.0.0.1:8200/connect', json={
    'user_token': '<your-agentverse-api-key>',
    'agent_type': 'mailbox',
})
```

After this, the agent is reachable from anywhere on the Fetch network, including ASI:One. No ngrok, no port forwarding, no public IP needed.

### Step 4: Define Custom Protocols for Pairing and Task Dispatch

Beyond the standard chat protocol, we define two custom protocols using the uAgents `Protocol` class and `Model` message types.

**Device Pairing Protocol** - lets the local connector register with the orchestrator:

```python
from uagents import Model, Protocol

class PairDeviceRequest(Model):
    user_id: str
    device_id: str
    public_key_hex: str       # Ed25519 public key (64 hex chars)
    capabilities: list[str]

class PairDeviceResponse(Model):
    user_id: str
    device_id: str
    status: str               # "paired" or "rejected"
    message: str

pairing_protocol = Protocol(name="device-pairing", version="0.1.0")

@pairing_protocol.on_message(PairDeviceRequest, replies={PairDeviceResponse})
async def handle_pairing(ctx, sender, msg):
    # Validate the public key, store the pairing record
    pairing_store.pair(msg.user_id, msg.device_id, msg.public_key_hex)
    # Remember the connector's agent address for dispatching later
    ctx.storage.set(f"connector:{msg.user_id}:{msg.device_id}", sender)
    await ctx.send(sender, PairDeviceResponse(status="paired", ...))
```

**Task Dispatch Protocol** - sends signed task plans to the connector:

```python
class TaskDispatchRequest(Model):
    user_id: str
    device_id: str
    task_plan_json: str       # JSON-encoded TaskPlan
    signature: str            # hex-encoded Ed25519 signature

class TaskExecutionResult(Model):
    task_id: str
    status: str               # completed | failed | rejected
    step_results_json: str
    outputs: dict
```

### Step 5: Intelligent Planning with ASI:One LLM

The planner converts natural-language objectives into structured task plans. It uses the [ASI:One LLM](https://docs.asi1.ai) through an OpenAI-compatible API:

```python
from openai import OpenAI

client = OpenAI(
    api_key=os.getenv("ASI_ONE_API_KEY"),
    base_url="https://api.asi1.ai/v1",
)

response = client.chat.completions.create(
    model="asi1",
    messages=[
        {"role": "system", "content": SYSTEM_PROMPT},      # defines available actions
        {"role": "user", "content": "scan my projects and email a summary"},
    ],
    temperature=0.1,
)
```

The system prompt tells the LLM which actions are available (`scan_directory`, `generate_report`, `post_summary`, etc.), what parameters they accept, and what constraints to enforce. The LLM returns a JSON task plan.

If the LLM is unavailable, the planner falls back to keyword matching. The system always works; it just plans more intelligently when the LLM is connected.

### Step 6: Authentication and Signature Verification

Every task dispatch is signed by the orchestrator using Ed25519. The connector verifies before executing.

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import json, hashlib

def sign_payload(private_key, payload_dict):
    canonical = json.dumps(payload_dict, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).digest()
    signature = private_key.sign(digest)
    return signature.hex()

def verify_signature(public_key_hex, payload, signature_hex):
    canonical = json.dumps(payload, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).digest()
    public_key.verify(bytes.fromhex(signature_hex), digest)
```

The connector's `RequestAuthenticator` class wraps this:

1. Deserialise the task plan JSON
2. Recompute the canonical hash
3. Verify the Ed25519 signature against the orchestrator's public key
4. Reject if the signature is missing, invalid, or the payload was tampered with

### Step 7: Local Policy Enforcement (Path Sandbox)

Even with valid signatures, the connector enforces its own local policies before executing anything:

```python
class LocalPolicy:
    allowed_actions = {"scan_directory", "generate_report", "post_summary"}
    allowed_paths = ["./demo_projects", "~/projects", "/tmp"]

    def validate_plan(self, plan):
        for step in plan.steps:
            if step.action not in self.allowed_actions:
                return RejectionReason.ACTION_NOT_ALLOWED
            if step has a path param and path not in allowed_paths:
                return RejectionReason.PATH_NOT_ALLOWED
        return None  # all good
```

This is the **dual policy** design:
- The **orchestrator** checks Fetch-side policies (max steps, rate limits, user quotas)
- The **connector** checks local policies (path sandboxing, action allowlists)
- The orchestrator **cannot bypass** local policies. Your machine always has the final say.

### Step 8: Task Execution Engine

The connector's executor runs each step in the task plan sequentially, chaining outputs between steps:

```python
def execute_plan(plan):
    results = []
    previous_output = {}
    for step in plan.steps:
        handler = ACTION_REGISTRY.get(step.action)
        result = handler(step.params, previous_output)
        previous_output = result.output
        results.append(result)
    return ExecutionResult(task_id=plan.task_id, step_results=results)
```

For the MVP, the action registry includes:
- `scan_directory` - walks the demo directory, finds git repos, gathers recent commits
- `generate_report` - compiles a Markdown dev report from scan results
- `post_summary` - (stub) prepares the summary for Slack/email delivery

### Step 9: Async Result Correlation

Because the orchestrator dispatches to the connector asynchronously (via agent messaging), results arrive later as a separate `TaskExecutionResult` message. The orchestrator needs to route results back to the correct requester.

It does this with a pending task store:

```python
# When dispatching (in the chat handler):
pending_dict[plan.task_id] = {"sender": sender, "objective": text}
ctx.storage.set("chat_pending", json.dumps(pending_dict))

# When results arrive (in the objective handler):
chat_meta = chat_dict.pop(msg.task_id, None)
if chat_meta:
    # This was a chat-originated task, reply as ChatMessage
    await send_chat_reply(ctx, chat_meta["sender"], formatted_result)
```

This lets the same orchestrator handle requests from both ASI:One (via `ChatMessage`) and custom agent clients (via `ObjectiveRequest`), routing results back to the correct sender.

---

## The Complete Data Flow

Here is what happens end-to-end when you type a message in ASI:One:

```
1. User types: "@agent1q... Generate my weekly dev report"
2. ASI:One sends a ChatMessage to the agent address
3. Agentverse mailbox holds the message
4. Local orchestrator polls and receives it
5. Chat handler extracts the text objective
6. Planner calls ASI:One LLM to produce a TaskPlan
7. Fetch-side policy validates the plan (max steps, allowed actions)
8. Plan is serialised, signed with Ed25519, wrapped in TaskDispatchRequest
9. Dispatch is sent to the paired connector's agent address
10. Connector receives the dispatch
11. Auth module verifies the Ed25519 signature
12. Local policy checks path sandboxing and action allowlist
13. Executor runs each step: scan repos, generate report, post summary
14. Results are sent back as TaskExecutionResult
15. Orchestrator receives results, correlates with pending task
16. Results are formatted and sent back as ChatMessage
17. ASI:One displays the weekly dev report to the user
```

See a real conversation in action: [**View Sample Chat on ASI:One**](https://asi1.ai/shared-chat/78ae5995-bdbb-4fee-a9b4-e1b335e5ff96)

---

## Security Model Summary

| Layer | What it checks | Where |
|---|---|---|
| **Device Pairing** | Ed25519 keypair registration | Orchestrator |
| **Request Signing** | Ed25519 signature on every task dispatch | Connector |
| **Fetch-side Policy** | Rate limits, max steps, action allowlists | Orchestrator |
| **Local Policy** | Path sandbox, action allowlist, no background execution | Connector |
| **Declarative Plans** | No shell commands, only named actions with parameters | Both |

The agent that plans never touches your files. The service that executes never accepts raw commands. Neither can bypass the other's policies.

---

## The MVP: Weekly Dev Report

For the first release, we ship one complete workflow:

1. **scan_directory** - walks project directories, finds git repos, gathers commit messages from the last 7 days
2. **generate_report** - compiles a Markdown dev report from the scan results
3. **post_summary** - (stub) prepares the summary for Slack or email delivery

It is simple, but it proves the full pipeline: objective to LLM plan to sign to dispatch to execute to return.

For safe testing, the included `scripts/setup_demo.py` generates a `demo_projects/` directory with fake git repos and sample commit history. No real system data is ever exposed.

---

## What We Are Using from Fetch.ai

| Technology | Version | Role |
|---|---|---|
| [uAgents](https://github.com/fetchai/uAgents) | `0.23.6` | Agent framework: identity, messaging, protocols, lifecycle |
| [uAgents-core](https://pypi.org/project/uagents-core/) | `0.4.1` | Core protocol specs including AgentChatProtocol |
| [Agentverse](https://agentverse.ai) | - | Hosting, discovery, mailbox relay, manifest publishing |
| [ASI:One Chat](https://asi1.ai) | - | User-facing chat interface for interacting with agents |
| [ASI:One LLM](https://docs.asi1.ai) | `asi1` | OpenAI-compatible API for intelligent task planning |
| [AgentChatProtocol](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents) | `0.3.0` | Standard protocol for ASI:One compatibility |
| [Almanac](https://docs.agentverse.ai) | testnet | Agent registration and discovery on the Fetch network |

---

## Try It Locally

```bash
# Clone and install
git clone <repo-url>
cd openclaw-fetch
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure (create .env with your API keys, see README.md for details)

# Set up demo data (safe fake repos)
python scripts/setup_demo.py

# Terminal 1: Orchestrator (with Agentverse mailbox)
python -m orchestrator.agent

# Terminal 2: Connector (auto-pairs with orchestrator)
ORCHESTRATOR_AGENT_ADDRESS=<address-from-terminal-1> python -m connector.server
```

All 42 tests pass. The orchestrator and connector auto-pair on startup. See the [README](../README.md) for full setup instructions including mailbox registration and environment variable reference.

---

## What's Next

- **Real Slack/email integration** to replace the `post_summary` stub
- **Multi-device support** to pair multiple machines to one agent account
- **Agentverse hosted deployment** for a fully cloud-hosted orchestrator
- **Paid workflow gating** using Fetch token economics
- **Output verification agent** that validates execution results before returning them
- **Additional workflows** like code review, CI status, and dependency audit

---

## The Bigger Picture

This is not just a dev report generator. It is a **reference architecture** for safe remote-to-local AI orchestration.

Any Fetch agent can coordinate local work (code analysis, file processing, system administration, data pipelines) through the same pattern: plan remotely, verify cryptographically, execute locally.

The user stays in control. The agent stays useful. And neither has to trust the other blindly.

---

*Built with [Fetch.ai uAgents](https://fetch.ai), [OpenClaw](https://openclaw.ai), and [ASI:One](https://asi1.ai).*
