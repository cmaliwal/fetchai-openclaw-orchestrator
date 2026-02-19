# OpenClaw × Fetch.ai - Secure Local Execution via Autonomous Agents

A reference architecture for **safe remote-to-local AI orchestration**.
A public Fetch agent on [Agentverse](https://agentverse.ai) plans work; a local
connector on your machine executes it - without granting remote shell access or
leaking sensitive data.

```
User --> ASI:One --> Orchestrator Agent --> [signed task plan] --> OpenClaw Connector --> Execution --> Results
```

| Component | Where it runs | What it does |
|---|---|---|
| **ASI:One** | Cloud ([asi1.ai](https://asi1.ai)) | Natural-language objective input |
| **Orchestrator Agent** | Agentverse / local | Plans tasks, enforces policy, dispatches work |
| **OpenClaw Connector** | Your machine | Verifies, policy-checks, executes, returns results |

> **New to the project?** Read the [Technical Blog Post](blog/fetch-openclaw-integration.md) for
> a step-by-step walkthrough of how each piece was built, from agent creation through
> mailbox configuration to ASI:One integration.

---

## Quick Start

### 1. Prerequisites

- Python 3.10+
- A [Fetch Agentverse](https://agentverse.ai) account & API key
- An [ASI:One](https://asi1.ai) API key (for LLM-powered planning)

### 2. Install

```bash
git clone https://github.com/cmaliwal/fetchai-openclaw-orchestrator.git
cd fetchai-openclaw-orchestrator

python3 -m venv .venv && source .venv/bin/activate
pip install -e ".[dev]"
```

### 3. Configure

Copy the example and fill in your keys:

```bash
cp .env.example .env
# Then edit .env and add your real API keys
```

See [Environment Variables](#environment-variables) below for the full list.
You will need at minimum:
- `AGENTVERSE_API_KEY` from [agentverse.ai/profile/api-keys](https://agentverse.ai/profile/api-keys)
- `ASI_ONE_API_KEY` from [asi1.ai](https://asi1.ai) (for LLM-powered planning)

### 4. Set Up Demo Data

Create safe sample repositories (fake git history) for testing:

```bash
python scripts/setup_demo.py
```

This generates a `demo_projects/` directory with 3 sample repos and ~12 fake
commits - **no real system data is ever touched during testing**.

### 5. Run

```bash
# Terminal 1 - Orchestrator Agent
python -m orchestrator.agent

# Terminal 2 - OpenClaw Connector (auto-pairs with orchestrator)
ORCHESTRATOR_AGENT_ADDRESS=<address-from-terminal-1> python -m connector.server
```

On startup the connector sends a pairing request to the orchestrator.
You should see `✅ Paired successfully` in the connector logs.

### 6. Register Mailbox (for ASI:One access)

With the orchestrator running, register its mailbox on Agentverse so ASI:One
can deliver messages to it:

```bash
python -c "
import requests, os
from dotenv import load_dotenv
load_dotenv()
resp = requests.post('http://127.0.0.1:8200/connect', json={
    'user_token': os.getenv('AGENTVERSE_API_KEY'),
    'agent_type': 'mailbox',
}, timeout=30)
print(resp.json())
"
```

You should see `{'success': True, 'detail': None}` and the orchestrator logs
will show `Successfully registered as mailbox agent in Agentverse`.

---

## Testing from ASI:One

Once both agents are running and the mailbox is registered, go to
**[ASI:One](https://asi1.ai)** and send a message to the agent:

```
@<your-agent-address> Generate my weekly dev report
```

### Example Prompts

| Prompt | What it does |
|---|---|
| `Generate my weekly dev report` | Scans demo repos → generates Markdown report |
| `Scan my projects and create a summary, then post to Slack` | 3-step: scan → report → post (Slack) |
| `Look at my repos and email the report to my team` | 3-step: scan → report → post (email) |
| `Summarize: we shipped 3 features this week` | Runs text summarisation |

### Sample Chat

See a real conversation with the agent on ASI:One:
[**View Sample Chat**](https://asi1.ai/shared-chat/78ae5995-bdbb-4fee-a9b4-e1b335e5ff96)

### What You'll See

The agent responds with structured results:

```
## Task Completed: task_abc123
Status: completed

✅ scan_directory: completed
✅ generate_report: completed

---
# Weekly Dev Report
**Period**: 2026-02-12 --> 2026-02-19

## weather-agent
  - feat: add temperature forecasting endpoint
  - fix: handle missing API key gracefully
  ...
```

---

## Running Tests

```bash
pytest                # run all 42 tests
pytest -v             # verbose
pytest --cov          # with coverage
```

### End-to-End Local Test

```bash
python scripts/local_test.py
```

This simulates the full flow (pair → plan → dispatch → execute → result)
in a single process without needing the agents to be running.

---

## Project Structure

```
openclaw-fetch/
├── orchestrator/                 # Fetch Orchestrator Agent (Agentverse)
│   ├── agent.py                  #   Main entry point, agent construction
│   ├── planner.py                #   Objective → TaskPlan (ASI:One LLM + fallback)
│   ├── policy.py                 #   Fetch-side policy engine
│   ├── storage.py                #   In-memory device pairing store
│   └── protocols/
│       ├── chat.py               #   AgentChatProtocol for ASI:One
│       ├── objective.py          #   Objective intake + task dispatch
│       ├── pairing.py            #   Device pairing protocol
│       └── models.py             #   uAgents message models
│
├── connector/                    # OpenClaw Connector (local machine)
│   ├── server.py                 #   Main entry point, auto-pairing
│   ├── executor.py               #   Task plan execution engine
│   ├── auth.py                   #   Signature verification
│   ├── policy.py                 #   Local policy engine (path sandbox, etc.)
│   └── workflows/
│       └── weekly_report.py      #   scan_directory, generate_report, post_summary
│
├── shared/                       # Shared between orchestrator & connector
│   ├── schemas.py                #   Pydantic models (TaskPlan, TaskStep, etc.)
│   └── crypto.py                 #   Ed25519 key management & signing
│
├── scripts/
│   ├── local_test.py             #   End-to-end local integration test
│   └── setup_demo.py             #   Generate demo_projects/ with fake repos
│
├── tests/                        #   42 unit tests
├── blog/
│   └── fetch-openclaw-integration.md  #   Technical blog post (step-by-step walkthrough)
├── pyproject.toml                #   Project metadata & dependencies
├── requirements.txt              #   Pinned dependencies
└── .env                          #   Environment variables (not committed)
```

---

## Architecture Deep Dive

For a full step-by-step walkthrough with code samples, see the
[Technical Blog Post](blog/fetch-openclaw-integration.md).

### How It Works

**1. Agent Creation (uAgents)**

The orchestrator is a [uAgent](https://github.com/fetchai/uAgents) built with `uagents==0.23.6`.
The `Agent` class handles identity (Ed25519 keypair from the seed), messaging, protocol
registration, and Almanac registration on `testnet`.

**2. ASI:One Compatibility (AgentChatProtocol)**

The agent implements the standard `AgentChatProtocol` from `uagents-core==0.4.1`.
When included with `publish_manifest=True`, the protocol manifest is published to
Agentverse, which makes ASI:One able to discover and communicate with the agent.

- ASI:One sends `ChatMessage` with user text
- Agent sends `ChatAcknowledgement` immediately
- Agent extracts the text, plans the task, dispatches to the connector
- Results come back as a `ChatMessage` reply

**3. Agentverse Mailbox (Local Agent, Global Reach)**

The agent runs on your machine but uses an Agentverse mailbox for inbound messages.
Messages from ASI:One are delivered to Agentverse, and the local agent polls for them.
No public IP, no port forwarding, no ngrok needed.

- Set `USE_MAILBOX=true` in `.env` (default)
- Register via the `/connect` endpoint (see step 6 above)
- Set `USE_MAILBOX=false` for purely local agent-to-agent testing

**4. Device Pairing (Ed25519 Keypair)**

Before any task can run, the local connector generates an Ed25519 keypair and
registers its public key with the orchestrator. The connector's agent address is
stored so that task dispatches can be routed correctly. No pairing = no execution.

**5. Signed Task Plans**

Every task dispatch carries an Ed25519 signature over the full task plan. The
connector's `RequestAuthenticator` verifies the signature against the orchestrator's
public key before executing anything. Tampered payloads are rejected.

**6. Dual Policy Enforcement**

Policies are checked at two independent layers:

| Layer | When | What it checks |
|---|---|---|
| Fetch-side (`orchestrator/policy.py`) | Planning time | Max steps, action allowlists, rate limits |
| Local (`connector/policy.py`) | Execution time | Path sandboxing, action allowlists, no background execution |

The orchestrator **cannot** bypass local policies. Your machine always has the final say.

**7. Intelligent Planning (ASI:One LLM)**

When `ASI_ONE_API_KEY` is set, the orchestrator calls the [ASI:One LLM](https://docs.asi1.ai)
(OpenAI-compatible API at `https://api.asi1.ai/v1`, model `asi1`) to convert
natural-language objectives into structured task plans. If the LLM is unavailable,
the planner falls back to keyword matching.

**8. Declarative Task Plans (Not Shell Commands)**

The orchestrator never sends bash commands. It sends structured JSON task plans:

```json
{
  "task_id": "task_5d24184eac1a",
  "steps": [
    { "type": "local",    "action": "scan_directory",  "params": { "path": "./demo_projects" } },
    { "type": "local",    "action": "generate_report", "params": { "format": "markdown" } },
    { "type": "external", "action": "post_summary",    "params": { "target": "slack" } }
  ],
  "constraints": { "no_delete": true, "require_user_confirmation": true }
}
```

The connector maps each action to a known, bounded implementation in the action registry.

**9. Async Result Correlation**

Because the orchestrator dispatches tasks asynchronously via agent messaging, it
stores a pending-task map keyed by `task_id`. When results arrive from the connector,
the orchestrator looks up whether the task came from an ASI:One chat session or a
custom agent client and routes the result back to the correct sender.

### End-to-End Data Flow

```
 1. User types: "@agent1q... Generate my weekly dev report" in ASI:One
 2. ASI:One sends ChatMessage to the agent address
 3. Agentverse mailbox holds the message
 4. Local orchestrator polls and receives it
 5. Chat handler extracts the text objective
 6. Planner calls ASI:One LLM to produce a TaskPlan (or falls back to keywords)
 7. Fetch-side policy validates the plan
 8. Plan is serialised, signed with Ed25519, dispatched to the connector
 9. Connector verifies signature, checks local policy
10. Executor runs each step: scan repos, generate report, post summary
11. Results are sent back as TaskExecutionResult
12. Orchestrator correlates with pending task, formats as ChatMessage
13. ASI:One displays the weekly dev report to the user
```

---

## Key Technologies

| Technology | Version | Role |
|---|---|---|
| [uAgents](https://github.com/fetchai/uAgents) | `0.23.6` | Agent framework: identity, messaging, protocols, lifecycle |
| [uAgents-core](https://pypi.org/project/uagents-core/) | `0.4.1` | Core protocol specs including AgentChatProtocol |
| [Agentverse](https://agentverse.ai) | | Agent hosting, discovery, mailbox relay, manifest publishing |
| [ASI:One Chat](https://asi1.ai) | | User-facing chat interface for interacting with agents |
| [ASI:One LLM](https://docs.asi1.ai) | model: `asi1` | OpenAI-compatible API for intelligent task planning |
| [AgentChatProtocol](https://innovationlab.fetch.ai/resources/docs/examples/chat-protocol/asi-compatible-uagents) | `0.3.0` | Standard protocol for ASI:One discoverability |
| [Ed25519](https://en.wikipedia.org/wiki/EdDSA) | | Asymmetric signing for pairing and request authentication |
| [Pydantic](https://docs.pydantic.dev) | | Schema validation for task plans and messages |

---

## Environment Variables

| Variable | Default | Description |
|---|---|---|
| `ORCHESTRATOR_AGENT_SEED` | `openclaw-orchestrator-dev-seed` | Seed for orchestrator agent identity |
| `ORCHESTRATOR_PORT` | `8200` | Orchestrator local server port |
| `CONNECTOR_AGENT_SEED` | `openclaw-connector-dev-seed` | Seed for connector agent identity |
| `CONNECTOR_PORT` | `8199` | Connector local server port |
| `CONNECTOR_USER_ID` | `u_dev` | User ID for pairing |
| `CONNECTOR_DEVICE_ID` | `dev_local` | Device ID for pairing |
| `ORCHESTRATOR_AGENT_ADDRESS` | *(none)* | Set to auto-pair connector on startup |
| `AGENT_NETWORK` | `testnet` | `testnet` or `mainnet` |
| `AGENTVERSE_API_KEY` | *(none)* | Agentverse API key for mailbox registration |
| `ASI_ONE_API_KEY` | *(none)* | ASI:One API key for LLM planning |
| `ASI_ONE_BASE_URL` | `https://api.asi1.ai/v1` | ASI:One API base URL |
| `ASI_ONE_MODEL` | `asi1` | ASI:One model name |
| `DEMO_PROJECTS_DIR` | `./demo_projects` | Safe demo directory for testing |
| `USE_MAILBOX` | `true` | Enable Agentverse mailbox relay |
| `LOG_LEVEL` | `INFO` | Logging level |

---

## Roadmap

- [ ] Real Slack / email integration (replace `post_summary` stub)
- [ ] Multi-device support (pair multiple machines to one account)
- [ ] Agentverse hosted deployment
- [ ] Paid workflow gating via Fetch token economics
- [ ] Output verification agent (validate results before returning)
- [ ] Additional workflows (code review, CI status, dependency audit)

---

## License

MIT

---

*Built with [Fetch.ai uAgents](https://fetch.ai), [OpenClaw](https://openclaw.ai),
and [ASI:One](https://asi1.ai).*
