# Gemini & Google ADK 2.0 Developer Reference Guide

This reference guide documents the design patterns, configuration best practices, troubleshooting lessons, and official documentation sources for maintaining and extending the **Google Cloud CDN Diagnostic Agent** built on Google ADK 2.0.

---

## 1. Core Documentation & Resources

When adding features, modifying agents, or updating the SDK, refer to these official guides:

* **Google ADK 2.0 Overview & SDK Links:** [https://adk.dev/2.0/](https://adk.dev/2.0/)
* **ADK Collaborative Multi-Agent Guide:** [https://adk.dev/workflows/collaboration/](https://adk.dev/workflows/collaboration/)
* **Google ADK Python GitHub:** [https://github.com/google/adk-python](https://github.com/google/adk-python)
* **ADK Lab Reference Repository:** [https://github.com/zken-cloud/adk2-lab](https://github.com/zken-cloud/adk2-lab)
* **Google Agents CLI:** [https://github.com/google/agents-cli](https://github.com/google/agents-cli)

---

## 2. Architectural Insights & Best Practices

### Multi-Agent Orchestration (Hub-and-Spoke)
The system is structured as a collaborative multi-agent team, with a **Lead Coordinator Agent** routing requests to four specialist agents:
* `get_cdn_agent`: Retrieves CDN and Load Balancer configurations.
* `log_analysis_agent`: Analyzes access metrics and error distributions in Cloud Logging.
* `troubleshooting_agent`: Conducts active HTTP simulations.
* `execution_agent`: Executes operations such as Cache Invalidation or config changes.

### Crucial Lesson: Transfer Tools Conflict Resolution
> [!IMPORTANT]
> **Do NOT manually define or register a `transfer_to_agent` function or tool on any of the sub-agents.**
>
> In Google ADK 2.0, the framework automatically generates and injects transfer tools (`transfer_to_agent_...`) into the agents at runtime based on the `sub_agents` hierarchy. Manually declaring transfer functions will result in the following error:
> `Duplicate function declaration found: transfer_to_agent`

### Human-in-the-Loop (HITL) Confirmation
For sensitive or destructive execution actions (such as flushing a CDN cache, deleting global forwarding rules, updating URL Map redirection, or modifying backend configurations), the agent must get explicit human confirmation before execution.
* **Implementation**: Use the `require_confirmation=True` flag on the ADK `FunctionTool` initializer:
  ```python
  FunctionTool(func=invalidate_cdn_cache, require_confirmation=True)
  ```
* This tells the ADK web UI or CLI to halt and prompt the user for consent before the tool runs.
* The execution agent enforces HITL on `invalidate_cdn_cache`, `update_backend_service_cdn_config`, `delete_global_forwarding_rule`, `update_url_map_redirect`, and `update_backend_bucket_cdn_config`.

### Flexible Multi-Project Diagnostic Capability
Rather than hardcoding a single project name, the agent supports querying resources across any project (e.g., `"your-gcp-project-b"` or `"your-default-gcp-project-id"`):
* **Logic**: The system instructions prompt the agent to detect project IDs from the user query. If present, the agent overrides the default and passes the specified project ID to the tools.
* **Default Fallback**: If no project is specified, the agent falls back to the default project ID configured via the environment variable `GOOGLE_CLOUD_PROJECT`.

---

## 3. Model & Environment Configurations

* **Recommended Model**: `gemini-3.5-flash` (User preferred) or `gemini-2.5-flash`.
* **Vertex AI Enablement**:
  To run Gemini models via the Vertex AI backend rather than the developer API, configure these environment variables:
  ```bash
  os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
  os.environ["GEMINI_REGION"] = "global"
  ```
* **Authentication**: Active user context relies on Application Default Credentials (ADC) configured via the Google Cloud CLI (`gcloud auth application-default login`).
