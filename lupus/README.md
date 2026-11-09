# LUPUS CLI guide

[Project overview](../README.md) · [한국어 소개](../README.ko.md)

This guide covers the current CLI configuration. LUPUS is alpha; a configured provider is not necessarily a backend whose execution, permissions or model compatibility have been confirmed.

## Install and start

From the repository root, use Python 3.12+ with SQLite 3.51.3+ and FTS5 on macOS:

```bash
python3 -m venv .venv
source .venv/bin/activate
python -m pip install -e ./lupus
lupus
```

The initial provider is Codex unless a saved preference or `LUPUS_DRIVER` chooses another. Native providers need their own installed, authenticated CLI. Hosted APIs need credentials; local providers need a running server and an available model.

## Terminal controls

See the [CLI home screen and slash picker](../README.md#terminal-cli), captured from a live LUPUS process in a local terminal emulator with a public demo workspace.

| Command | Purpose |
|---|---|
| `/provider` | List providers, then choose one with `/provider ID`. |
| `/model` | Choose a model; `/model default` restores the provider default. |
| `/models` | Show configured models. `--discover` explicitly requests provider discovery where supported. |
| `/mode` | Choose `auto`, `ask`, `agent` or `verify`. |
| `/skills` | List discovered local skills and bundled guidance. |
| `/skill ID TASK` | Request a particular skill. |
| `/view` | Browse goals, knowledge and documents in the terminal. |
| `/graph` | Generate the viewer and request that it open in a browser. |
| `/parallel` | Show coordination state; `/parallel auto` or `/parallel 2` starts existing open goals in the background. |
| `/supervisor` | Inspect the optional on-failure role. |
| `/jobs` | Show tracked background jobs. |
| `/help` | Show commands and usage. |
| `/quit` | Leave the interactive CLI. |

`/` opens suggestions; arrows select, Tab inserts and Enter confirms. A command such as `/mode` opens its choices. Esc closes suggestions and preserves the current input. Ctrl+C exits an idle prompt; during work it requests cancellation before returning to the prompt. Check `/jobs` for separately started background work.

`ask` uses read-only workspace access. `agent` allows edits without asserting verification. `verify` uses acceptance checks and recorded evidence. `auto` uses local rules to select a suitable path, including short local operations.

## Provider configuration

The configuration directory is `LUPUS_HOME`, or `~/.lupus` by default. `providers.json` is a **public configuration file**: store environment-variable names there, never credentials.

| ID | Connection | Credential reference |
|---|---|---|
| `codex` | Installed Codex CLI | Native CLI authentication |
| `claude` | Installed Claude Code CLI | Native CLI authentication |
| `glm` | Z.AI API | `ZAI_API_KEY` |
| `openrouter` | OpenRouter API | `OPENROUTER_API_KEY` |
| `deepseek` | DeepSeek API | `DEEPSEEK_API_KEY` |
| `gemini` | Gemini OpenAI-compatible API | `GEMINI_API_KEY` |
| `ollama` | `http://127.0.0.1:11434/v1` | No key in the built-in profile |
| `lmstudio` | `http://localhost:1234/v1` | No key in the built-in profile |
| `openai-compatible` | Endpoint required | Set an environment-variable reference |

A custom profile example, with placeholders to replace:

```json
{
  "providers": [
    {
      "id": "openai-compatible",
      "name": "My API",
      "protocol": "openai-chat",
      "base_url": "https://api.example.com/v1",
      "api_key_env": "MY_API_KEY",
      "default_model": "YOUR_MODEL_ID",
      "models": ["YOUR_MODEL_ID"]
    }
  ]
}
```

Use HTTPS for remote endpoints; loopback HTTP is permitted for local servers. Native `codex` and `claude` definitions cannot be replaced in this file. A listed or selected provider has not necessarily been contacted or authenticated.

For an already running Ollama server with a model you have installed:

```text
/provider ollama
/model YOUR_LOCAL_MODEL_ID
/mode ask
Explain this repository.
```

Selecting the provider or model does not download anything. Model discovery is separate from generation. API and native workers expose different tools and permission boundaries.

## Optional supervision and skills

```text
/supervisor codex YOUR_SUPERVISOR_MODEL_ID
/supervisor off
```

The optional role is used on eligible failures, not on every request. Selecting it does not prove the model exists or that the backend works. Set the primary worker independently with `/provider` and `/model`.

`/skills` discovers available skill IDs. `/skill show ID` displays the guidance; `/skill ID TASK` requests it. Bundled OMC and Superpowers guidance is adapted, not a complete installation of either project. Native skill access follows the native profile. `/skill-mode auto|off` controls skill access for API primary workers.

## Verified work and parallel goals

```bash
lupus auto "Fix the pagination offset"   --check "python3 -m unittest test_inventory" --driver codex
lupus status GOAL_ID
lupus jobs
```

Replace the request and check with your own. Acceptance checks cover their stated conditions, not every aspect of correctness or security.

```bash
lupus alpha-run --drivers codex --parallel auto
lupus alpha-status
```

This runs existing open goals. Concurrent writers need different projects or isolated checkouts. It does not split a single prompt into an agent team. In the interactive CLI, `/parallel auto` starts the coordinator in the background; keep track of it with `/jobs`.

## Memory and viewer

LUPUS retrieves relevant structured notes within an input budget. Stored knowledge, the generated Markdown vault and local job logs are broader than the context sent to a model. Explicit corrections and observed changes retain history; unknown authorship remains unknown, and candidate notes need review.

```bash
lupus graph --open
lupus graph --serve --open
lupus graph --serve --port 8767
```

The first command generates the viewer and opens the local file. `--serve` starts a foreground loopback server, with knowledge chat that can call Codex or Claude Code as selected in the viewer. Stop that server when finished. The viewer includes the dashboard, graph, knowledge, documents and goals.

For the current screens, see the [project overview](../README.md#current-ui). README screenshots use public example data, not real user state or performance results.

## Boundaries

LUPUS tracks owned processes, cancellation and background jobs, and uses protected verification inputs and execution policies. These mechanisms do not imply VM isolation, universal cleanup or identical sandboxing across native, user-profile, MCP and API tools. Review the effective permissions for your environment.

See the [verification contract](docs/CONTRACT.md) and [third-party notices](THIRD_PARTY_NOTICES.md). Broad cost, speed, accuracy and security superiority over other CLIs remains unproven.
