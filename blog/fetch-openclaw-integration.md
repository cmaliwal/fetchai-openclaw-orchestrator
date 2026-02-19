# How Fetch Agents and OpenClaw Work Together to Run AI Tasks on Your Machine Safely

*Combining Fetch.ai's autonomous agent orchestration with OpenClaw's local execution runtime, without compromising user ownership or security.*

---

## The Problem: AI Agents Need Hands, Not Just Brains

Large Language Models can reason about objectives. Platforms like [ASI:One](https://asi1.ai) and [Agentverse](https://agentverse.ai) let users discover and talk to specialised AI agents. But when the task is **"analyze this GitHub repo and give me a health report"** or **"generate my weekly dev report from local git repos"**, those agents hit a wall.

They can *plan* the work. They can't *do* the work, because real analysis requires running tools like `cloc`, `pip-audit`, and `git log` on actual files.

The obvious fix, giving a remote agent shell access to your server, is a security nightmare. Cross-user misuse, uncontrolled command execution, leaked credentials. None of that is acceptable.

So we built something better.

---

## The Use Case: GitHub Repo Health Analyzer

Anyone on [ASI:One](https://asi1.ai) can type:

> **"Analyze https://github.com/fastapi/fastapi"**

And get back a real health report:

```
Repo Health Report: fastapi/fastapi
Health Score: 8.7/10 (Grade: A)

Languages:
  Python: 82.3% (48,200 lines)
  Markdown: 12.1% (7,100 lines)

Git Activity:
  Total Commits: 3,456
  Commits (last 30 days): 124
  Contributors: 485

Testing:
  Test Files Found: 340
  Frameworks Detected: pytest

Best Practices:
  README: pass
  LICENSE: pass
  CI/CD Pipeline: pass
  SECURITY.md: pass
```

**Why an LLM alone cannot do this:** ChatGPT cannot clone a repository, run `cloc` to count real lines of code, or execute `git log` to check actual commit history. It would have to guess or hallucinate the numbers. Our agent uses OpenClaw to run the actual tools and return real data.

**Why it is safe:** The repo is cloned into a temporary sandbox. No code from the repo is ever executed, imported, or installed. The agent reads files as text, runs static analysis, and deletes the clone after. If someone points to a repo full of malware, the agent just reports its health score and moves on.

---

## The Architecture: Plan Remotely, Execute Locally

Our integration connects three components:

| Component | Where it runs | What it does |
|---|---|---|
| **ASI:One** | Cloud (Fetch) | User sends a natural language objective |
| **Fetch Orchestrator Agent** | Agentverse (mailbox) | Plans the task using ASI:One LLM, enforces policy, dispatches work |
| **OpenClaw Connector** | Your server | Verifies the request, executes it safely, returns results |

The key insight: **the agent that plans the work never touches the files**. And the service that executes the work **never accepts raw commands**, only declarative, policy-checked task plans.

```
User --> ASI:One --> Orchestrator Agent (Agentverse mailbox) --> [signed task plan] --> OpenClaw Connector --> Execution --> Results
```

**What each technology adds:**

| Technology | Role | Without it |
|---|---|---|
| **Fetch Agent** | Public discovery, standard protocol, agent-to-agent messaging | Your tool is invisible, requires custom API |
| **ASI:One** | Natural language interface, user reach, LLM for planning | Users need to learn your API or CLI |
| **OpenClaw** | Actual execution: `git clone`, `cloc`, analysis tools | LLM can only generate text, not run tools |
| **Agentverse** | Hosting, mailbox relay, manifest publishing | Your local agent is unreachable from the internet |

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
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "Analyze https://github.com/fastapi/fastapi"},
    ],
    temperature=0.1,
)
```

The system prompt tells the LLM which actions are available and what parameters they accept. For the repo analyzer, the available actions are:

- `clone_repo` - shallow-clone a public GitHub repo into a temp sandbox
- `analyze_repo` - run static analysis (line counts, git stats, dependency audit, test detection)
- `generate_health_report` - compile everything into a scored report

The LLM returns a JSON task plan. If the LLM is unavailable, the planner falls back to keyword matching. The system always works; it just plans more intelligently when the LLM is connected.

### Step 6: The Repo Analyzer Workflow

This is where OpenClaw demonstrates its value. The workflow runs three steps sequentially, chaining outputs:

**Step 1: `clone_repo`**
- Accepts only public GitHub HTTPS URLs (SSH and non-GitHub URLs rejected)
- Shallow clone (`--depth 1`) into a temporary directory
- Enforces a 500 MB size limit
- Fetches full history for accurate git stats

**Step 2: `analyze_repo`**
- Counts lines of code by language (uses `cloc` if installed, falls back to extension-based counting)
- Gathers git statistics: total commits, recent activity, contributors
- Detects test frameworks and counts test files
- Parses dependency files (`requirements.txt`, `package.json`, etc.)
- Checks for best practices: README, LICENSE, .gitignore, CI/CD, SECURITY.md
- Flags potentially sensitive files committed to the repo
- Computes a 0-10 health score

**Step 3: `generate_health_report`**
- Compiles all analysis data into a readable Markdown report
- Assigns a letter grade (A/B/C/D) based on the score
- Cleans up the temporary clone directory

**Critical safety point:** At no point does any code from the cloned repository get executed, imported, or installed. The repo is treated as data to be scanned, not code to be run. Same approach as GitHub's own dependency scanner.

### Step 7: Authentication and Signature Verification

Every task dispatch is signed by the orchestrator using Ed25519. The connector verifies before executing.

```python
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
import json, hashlib

def sign_payload(private_key, payload_dict):
    canonical = json.dumps(payload_dict, sort_keys=True, default=str)
    digest = hashlib.sha256(canonical.encode()).digest()
    signature = private_key.sign(digest)
    return signature.hex()
```

The connector's `RequestAuthenticator` deserialises the task plan JSON, recomputes the canonical hash, and verifies the Ed25519 signature against the orchestrator's public key. Tampered payloads are rejected.

### Step 8: Dual Policy Enforcement

Even with valid signatures, policies are checked at two independent layers:

```python
class LocalPolicy:
    allowed_actions = {
        "scan_directory", "generate_report", "post_summary",
        "clone_repo", "analyze_repo", "generate_health_report",
    }
    allowed_paths = ["./demo_projects", "~/projects", "/tmp"]

    def validate_plan(self, plan):
        for step in plan.steps:
            if step.action not in self.allowed_actions:
                return RejectionReason.ACTION_NOT_ALLOWED
        return None  # all good
```

- The **orchestrator** checks Fetch-side policies (max steps, rate limits, action allowlists)
- The **connector** checks local policies (path sandboxing, action allowlists)
- The orchestrator **cannot bypass** local policies. Your machine always has the final say.

### Step 9: Async Result Correlation

Because the orchestrator dispatches tasks asynchronously, results arrive later as a separate message. The orchestrator routes results back to the correct requester:

```python
# When dispatching (in the chat handler):
pending_dict[plan.task_id] = {"sender": sender, "objective": text}
ctx.storage.set("chat_pending", json.dumps(pending_dict))

# When results arrive (in the objective handler):
chat_meta = chat_dict.pop(msg.task_id, None)
if chat_meta:
    await send_chat_reply(ctx, chat_meta["sender"], formatted_result)
```

This lets the same orchestrator handle requests from both ASI:One (via `ChatMessage`) and custom agent clients (via `ObjectiveRequest`), routing results back to the correct sender.

### Step 10: Feedback Loop Protection

When integrating with ASI:One, a practical challenge emerges: ASI:One's LLM sometimes rewrites your agent's responses and sends them back as new objectives, creating an infinite feedback loop. For example, the agent sends "Task dispatched, standing by for results" and ASI:One turns that into "Report mode activated! Gen + Post -- mission running!" and sends it right back.

We solved this with multi-layered protection in the chat handler:

```python
# 1. Pattern-based echo detection (100+ known ASI:One rewrite patterns)
_ECHO_PATTERNS = [
    "task dispatched", "mission running", "report mode activated",
    "standing by!", "pipeline running", ...
]

# 2. Emoji density check (ASI:One adds lots of emoji to rewrites)
if len(emoji_re.findall(text)) >= 3:
    return True  # likely an echo

# 3. Per-sender cooldown (30 seconds between objectives from same sender)
if (now - last_dispatch_time) < 30:
    return  # too soon, ignore

# 4. Exact dedup within 120-second window
obj_hash = md5(text.encode()).hexdigest()[:12]
if seen_recently(obj_hash):
    return  # duplicate, ignore

# 5. Pending task cap (max 5 concurrent tasks, prune stale entries)
if pending_count > 5:
    prune_all_pending()
```

The most important design decision: **do not send intermediate status messages**. Instead of saying "Your task has been dispatched, please wait...", the agent stays silent until the final result arrives. This eliminates the primary trigger for ASI:One's echo behavior. The user receives exactly one message: the completed result.

---

## The Complete Data Flow

Here is what happens end-to-end when you analyze a GitHub repo from ASI:One:

```
 1. User types: "Analyze https://github.com/fastapi/fastapi" in ASI:One
 2. ASI:One sends a ChatMessage to the agent address
 3. Agentverse mailbox holds the message
 4. Local orchestrator polls and receives it
 5. Chat handler runs feedback loop detection (echo patterns, cooldown, dedup)
 6. Chat handler extracts the text and the GitHub URL
 7. Planner calls ASI:One LLM to produce a TaskPlan:
      [clone_repo, analyze_repo, generate_health_report]
 8. Fetch-side policy validates the plan (actions allowed, rate limit OK)
 9. Plan is serialised, signed with Ed25519, dispatched to the connector
10. Connector verifies Ed25519 signature
11. Connector checks local policy (actions in allowlist)
12. Executor runs clone_repo: shallow-clone into temp sandbox
13. Executor runs analyze_repo: cloc, git stats, deps, tests, security
14. Executor runs generate_health_report: compile scored Markdown report
15. Temp directory deleted, results sent back as TaskExecutionResult
16. Orchestrator correlates with pending task, formats as ChatMessage
17. ASI:One displays the health report to the user (no intermediate messages)
```

See a real conversation in action: [**View Sample Chat on ASI:One**](https://asi1.ai/chat/f7ccb160-88bc-46a0-bd44-041483eca338)

---

## Why This Combination Matters

| Question | Answer |
|---|---|
| **Can an LLM do this alone?** | No. ChatGPT cannot clone a repo, run `cloc`, or execute `git log`. It would hallucinate the numbers. |
| **Can OpenClaw do this alone?** | Yes, but only from your terminal. Nobody else can use it. |
| **Can a Fetch agent do this alone?** | No. It has no execution engine to run analysis tools. |
| **Why all three together?** | Real analysis (OpenClaw) made discoverable to anyone (Fetch/Agentverse) through natural language (ASI:One). |

The pattern extends beyond repo analysis. The same architecture supports any workflow where you need real tool execution combined with public accessibility:
- Dependency audits
- Infrastructure health checks
- Data pipeline monitoring
- Document processing
- CI/CD status dashboards

---

## Security Model Summary

| Layer | What it checks | Where |
|---|---|---|
| **URL Validation** | Only public GitHub HTTPS URLs accepted | Connector |
| **Sandbox Execution** | Temp directory, no code execution, auto-cleanup | Connector |
| **Size Limits** | Repos over 500 MB rejected | Connector |
| **Device Pairing** | Ed25519 keypair registration | Orchestrator |
| **Request Signing** | Ed25519 signature on every task dispatch | Connector |
| **Fetch-side Policy** | Rate limits, max steps, action allowlists | Orchestrator |
| **Local Policy** | Path sandbox, action allowlist, no background execution | Connector |
| **Declarative Plans** | No shell commands, only named actions with parameters | Both |
| **Feedback Loop Protection** | Echo detection, sender cooldown, dedup, pending cap | Orchestrator |

The agent that plans never touches the files. The service that executes never accepts raw commands. Neither can bypass the other's policies.

---

## Two Workflows, Same Architecture

### 1. GitHub Repo Health Analyzer (public, anyone can use)
- `clone_repo` - shallow-clone from GitHub into a temp sandbox
- `analyze_repo` - static analysis: languages, git stats, deps, tests, security
- `generate_health_report` - scored Markdown report with grade (A/B/C/D)

### 2. Weekly Dev Report (paired users only)
- `scan_directory` - walks project directories, finds git repos, gathers commit messages
- `generate_report` - compiles a Markdown dev report from the scan results
- `post_summary` - (stub) prepares the summary for Slack or email delivery

Both use the same pipeline: objective to plan to sign to dispatch to execute to return.

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

**GitHub Repo:** [cmaliwal/fetchai-openclaw-orchestrator](https://github.com/cmaliwal/fetchai-openclaw-orchestrator)

```bash
# Clone and install
git clone https://github.com/cmaliwal/fetchai-openclaw-orchestrator.git
cd fetchai-openclaw-orchestrator
python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"

# Configure (create .env with your API keys, see README.md for details)
cp .env.example .env

# Set up demo data (safe fake repos for weekly report workflow)
python scripts/setup_demo.py

# Terminal 1: Orchestrator (with Agentverse mailbox)
python -m orchestrator.agent

# Terminal 2: Connector (auto-pairs with orchestrator)
ORCHESTRATOR_AGENT_ADDRESS=<address-from-terminal-1> python -m connector.server
```

All 68 tests pass. The orchestrator and connector auto-pair on startup. See the [README](../README.md) for full setup instructions including mailbox registration and environment variable reference.

### Test from ASI:One

Once both agents are running and the mailbox is registered, go to [ASI:One](https://asi1.ai) and try:

```
Analyze https://github.com/fastapi/fastapi
```

```
Check the health of https://github.com/pallets/flask
```

```
Review https://github.com/cmaliwal/fetchai-openclaw-orchestrator
```

**See a real conversation:** [View Sample Chat on ASI:One](https://asi1.ai/chat/f7ccb160-88bc-46a0-bd44-041483eca338)

---

## What's Next

- **More analysis tools** - integrate `pip-audit`, `npm audit`, `bandit` for security scanning
- **Comparative analysis** - "compare repo A vs repo B"
- **Scheduled monitoring** - "check this repo every week and alert me if the score drops"
- **Multi-agent composition** - one agent clones, another analyzes, another reports
- **Real Slack/email integration** to replace the `post_summary` stub
- **PyPI package** - `pip install fetch-openclaw` for easy integration

---

## The Bigger Picture

This is not just a repo analyzer. It is a **reference architecture** for safe remote-to-local AI orchestration.

The pattern: a Fetch agent handles discovery and planning, OpenClaw handles execution, and the user never has to choose between AI capability and data safety.

Any Fetch agent can coordinate local work (code analysis, file processing, system administration, data pipelines) through the same design: plan remotely, verify cryptographically, execute locally.

The user stays in control. The agent stays useful. And neither has to trust the other blindly.

---

*Built with [Fetch.ai uAgents](https://fetch.ai), [OpenClaw](https://openclaw.ai), and [ASI:One](https://asi1.ai).*
