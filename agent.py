import logging
import os
import asyncio
import json
import google.adk.flows.llm_flows.request_confirmation as rc
from google.adk.tools.tool_confirmation import ToolConfirmation

# 框架补丁：解决 ADK 2.0 在进行工具确认（Human-in-the-Loop）时，若 UI 回传 'yes' 或 'no' 等文本原始数据而非标准 JSON 字典，导致框架抛出 JSONDecodeError 崩溃的问题。
def patched_parse_tool_confirmation(response):
    if response and len(response.values()) == 1 and 'response' in response.keys():
        val = response['response']
        if isinstance(val, str):
            val_lower = val.strip().lower()
            if val_lower in ('yes', 'y', 'true', 'approve', 'confirm'):
                return ToolConfirmation(confirmed=True)
            elif val_lower in ('no', 'n', 'false', 'reject', 'cancel'):
                return ToolConfirmation(confirmed=False)
            try:
                return ToolConfirmation.model_validate(json.loads(val))
            except Exception:
                pass
    try:
        return ToolConfirmation.model_validate(response)
    except Exception:
        return ToolConfirmation(confirmed=False)

rc._parse_tool_confirmation = patched_parse_tool_confirmation

from google.adk.agents.llm_agent import LlmAgent
from google.adk.tools.function_tool import FunctionTool
from gcp_cdn_skill.scripts.gcp_cdn_diagnostics import (
    analyze_cloud_logging,
    get_load_balancer_config,
    get_backend_service_origin,
    simulate_http_request,
    list_load_balancers,
    invalidate_cdn_cache,
    get_backend_bucket_details,
    get_cdn_policy_details,
    update_backend_service_cdn_config,
    list_global_forwarding_rules,
    delete_global_forwarding_rule,
    update_url_map_redirect,
    update_backend_bucket_cdn_config,
    get_current_time
)

# Configure Logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# 初始化环境：获取并配置 GCP Project ID、API 凭证及 Vertex AI 模型区域信息
def initialize_environment():
    project_id = os.environ.get("GOOGLE_CLOUD_PROJECT")
    if not project_id:
        try:
            project_id = input("Enter Google Cloud Project ID: ").strip()
        except EOFError:
            project_id = ""
        if not project_id:
            raise ValueError("Google Cloud Project ID is required.")
        os.environ["GOOGLE_CLOUD_PROJECT"] = project_id

    api_key = os.environ.get("GEMINI_API_KEY")
    if not api_key:
        try:
            api_key = input("Enter Gemini API Key (press Enter to skip if using Vertex AI / ADC): ").strip()
        except EOFError:
            api_key = ""
        if api_key:
            os.environ["GEMINI_API_KEY"] = api_key

    # 默认设置区域为 'global'，适用于 Vertex AI 的全球模型访问端点
    region = os.environ.get("GEMINI_REGION", "global")
    os.environ["GEMINI_REGION"] = region
    os.environ["GOOGLE_CLOUD_LOCATION"] = region

    # 启用 Vertex AI 选项
    os.environ["GOOGLE_GENAI_USE_VERTEXAI"] = "TRUE"
    
    return project_id, region

# 初始化环境并获取默认项目及区域配置
default_project_id, gemini_region = initialize_environment()


# --- 专项智能体 Prompt 指令集定义（包含动态环境变量注入） ---

# Coordinator Agent (总协调智能体)：管理用户对话、处理全局路由与信息补全、编排下属智能体并将最终报告呈现给用户
coordinator_instruction = f"""
You are the Lead Coordinator for the Google Cloud CDN Diagnostic AI Team.
Your role is to manage the user conversation, gather necessary context, orchestrate the team, and **present all final reports directly to the user**.

You have full permission and tool access to analyze and manage CDN resources across ANY Google Cloud project specified by the user (including "{default_project_id}", "your-gcp-project-b", or any other).
The default project ID is "{default_project_id}".

CRITICAL: You must NEVER claim to be authorized or configured only for the default project "{default_project_id}". If the user specifies another project (like "your-gcp-project-b"), you have complete authorization and access to inspect and configure resources there using your tools/specialists. Simply pass the correct project ID down to the specialist agents.

Follow this approach:
1. **Detect/Verify Project ID**: Check the user's query or input context for a specific GCP Project ID (e.g., "your-gcp-project-b"). 
   - If a project ID is explicitly provided by the user, use that project ID.
   - If no project ID is specified, fallback to the default project ID: "{default_project_id}".
2. **Clarify Missing Information**: If the user didn't provide a domain, URL, or timeframe, ask the human for these details BEFORE delegating.
3. **Delegate Specialist Tasks**: If you identify a need for config analysis, logs, troubleshooting, or execution, transfer the question to the appropriate specialist agent (`get_cdn_agent`, `log_analysis_agent`, `troubleshooting_agent`, or `execution_agent`). Pass the identified project ID along to the specialist.
4. **Synthesize & Conclude**: Once a specialist transfers control back to you, **review the tool outputs/execution results in the conversation history, synthesize the findings, and write a comprehensive, structured response (with markdown tables/reports as needed) directly to the user.** Do not ask another agent to present the results; you are the sole presenter.
"""

get_cdn_instruction = f"""
You are the Get CDN Configuration Specialist.
Your role is to retrieve Load Balancer, URL Maps, Backend Buckets, and CDN configuration details in the background.

You have complete tool access across ANY Google Cloud project specified.
The default project ID is "{default_project_id}".
- If a project ID is explicitly provided in the task or user query, use that project ID for your tools.
- Otherwise, use the default project ID: "{default_project_id}".

CRITICAL: Never claim that you only have access to the default project "{default_project_id}". You are authorized to query any project requested by the user.

Follow this workflow to retrieve the CDN configuration for the project:
1. Call `list_load_balancers` and `list_global_forwarding_rules` to get the complete list of Load Balancers, their associated default services, and forwarding rules.
2. From the output of `list_load_balancers`, identify all unique backend service and backend bucket resources referenced (e.g., in `default_service` or host rules/path matchers).
3. For each backend service identified, call `get_cdn_policy_details` to fetch its detailed CDN settings (including `enable_cdn`, `cache_mode`, TTLs, and cache key policies).
4. For each backend bucket identified, call `get_backend_bucket_details` to fetch its CDN settings.
5. Once you have retrieved the CDN configuration details for all backend services and buckets, **write a brief, one-sentence status update to the coordinator** (e.g., "I have successfully retrieved all load balancers and their associated backend CDN policy configurations.") and **immediately call `transfer_to_agent` with `agent_name="coordinator_agent"`**.
6. Do not generate any detailed tables or reports yourself; the coordinator will present the final report to the user.
"""

log_analysis_instruction = f"""
You are the Log Analysis Specialist.
Your role is to query Cloud Logging data in the background.

You have complete tool access across ANY Google Cloud project specified.
The default project ID is "{default_project_id}".
- If a project ID is explicitly provided in the task or user query, use that project ID for your tools.
- Otherwise, use the default project ID: "{default_project_id}".

CRITICAL: Never claim that you only have access to the default project "{default_project_id}". You are authorized to query any project requested by the user.

Follow this workflow:
1. If the user query refers to a relative timeframe (e.g., "past 3 days", "yesterday", "last 24 hours"), you MUST first call `get_current_time` to obtain the anchor UTC datetime string.
2. Use the returned current UTC time to calculate the absolute `start_time` and `end_time` strings (in ISO 8601 format: "YYYY-MM-DDTHH:MM:SSZ") during your thinking/reasoning steps.
3. NEVER write or invoke raw Python code (e.g. `from datetime import datetime...`) to calculate these times. You do not have a Python interpreter. Calculate the dates step-by-step in text.
4. Execute `analyze_cloud_logging` using the calculated absolute `start_time` and `end_time` ISO strings.
5. Once the tool returns the log metrics, **write a brief, one-sentence status update to the coordinator** (e.g., "I have completed the log analysis and retrieved the status codes and error samples.") and **immediately call `transfer_to_agent` with `agent_name="coordinator_agent"`**. Do not generate any detailed tables or reports; the coordinator will present the final report.
"""

troubleshooting_instruction = f"""
You are the Performance Troubleshooting Specialist.
Your role is to simulate active requests to CDN or Origin endpoints in the background.

You have complete tool access across ANY Google Cloud project specified.
The default project ID is "{default_project_id}".
- If a project ID is explicitly provided in the task or user query, use that project ID for your tools.
- Otherwise, use the default project ID: "{default_project_id}".

CRITICAL: Never claim that you only have access to the default project "{default_project_id}". You are authorized to query any project requested by the user.

Follow this workflow:
1. Execute `simulate_http_request` to test current response behavior.
2. Once the tool returns the headers and status, **write a brief, one-sentence status update to the coordinator** (e.g., "I have simulated HTTP requests to the origin and retrieved the status codes and caching headers.") and **immediately call `transfer_to_agent` with `agent_name="coordinator_agent"`**. Do not generate any detailed tables or reports; the coordinator will present the final report.
"""

execution_instruction = f"""
You are the Execution & Configuration Specialist.
Your role is to execute operational actions in the background.

You have complete tool access across ANY Google Cloud project specified.
The default project ID is "{default_project_id}".
- If a project ID is explicitly provided in the task or user query, use that project ID for your tools.
- Otherwise, use the default project ID: "{default_project_id}".

CRITICAL: Never claim that you only have access to the default project "{default_project_id}". You are authorized to modify any project requested by the user.

Follow this workflow:
1. Execute the configuration tools as requested.
2. You MUST request human confirmation BEFORE taking action on `invalidate_cdn_cache`, `update_backend_service_cdn_config`, `delete_global_forwarding_rule`, `update_url_map_redirect`, or `update_backend_bucket_cdn_config` tools.
3. Once the operation is completed, **write a brief, one-sentence status update to the coordinator** (e.g., "I have successfully executed the configuration change/cache invalidation.") and **immediately call `transfer_to_agent` with `agent_name="coordinator_agent"`**. Do not generate any detailed tables or reports; the coordinator will present the final report.
"""

def create_multi_agent_system():
    """初始化并返回多智能体协同诊断团队。"""
    
    # 专项智能体定义 (无需手动定义 TransferToAgentTool，ADK 框架在运行时会自动根据 sub_agents 拓扑结构注入转移工具)
    
    # 1. CDN 配置查询专家智能体
    get_cdn_agent = LlmAgent(
        name="get_cdn_agent",
        description="Retrieves URL Maps, Backend Buckets, and CDN Policies configuration.",
        model="gemini-3.5-flash",
        instruction=get_cdn_instruction,
        tools=[
            FunctionTool(func=list_load_balancers),
            FunctionTool(func=get_load_balancer_config),
            FunctionTool(func=get_backend_bucket_details),
            FunctionTool(func=get_cdn_policy_details),
            FunctionTool(func=list_global_forwarding_rules)
        ]
    )

    # 2. 日志分析专家智能体
    log_analysis_agent = LlmAgent(
        name="log_analysis_agent",
        description="Queries and analyzes Google Cloud Logging metrics and error distributions.",
        model="gemini-3.5-flash",
        instruction=log_analysis_instruction,
        tools=[
            FunctionTool(func=analyze_cloud_logging),
            FunctionTool(func=get_current_time)
        ]
    )

    # 3. 性能与路由故障排查专家智能体
    troubleshooting_agent = LlmAgent(
        name="troubleshooting_agent",
        description="Simulates HTTP requests and isolates root cause at the Origin or CDN level.",
        model="gemini-3.5-flash",
        instruction=troubleshooting_instruction,
        tools=[
            FunctionTool(func=simulate_http_request)
        ]
    )

    # 4. 配置与敏感操作执行专家智能体 (启用 require_confirmation 进行人工介入二次确认)
    execution_agent = LlmAgent(
        name="execution_agent",
        description="Executes cache management and CDN configuration operations.",
        model="gemini-3.5-flash",
        instruction=execution_instruction,
        tools=[
            FunctionTool(func=invalidate_cdn_cache, require_confirmation=True), # 刷新 CDN 缓存 (需要二次确认)
            FunctionTool(func=update_backend_service_cdn_config, require_confirmation=True), # 更改后端服务 CDN 配置 (需要二次确认)
            FunctionTool(func=delete_global_forwarding_rule, require_confirmation=True), # 删除全局转发规则 (需要二次确认)
            FunctionTool(func=update_url_map_redirect, require_confirmation=True), # 更改 URL Map 重定向配置 (需要二次确认)
            FunctionTool(func=update_backend_bucket_cdn_config, require_confirmation=True) # 更改后端存储桶 CDN 配置 (需要二次确认)
        ]
    )

    # Coordinator (总协调智能体，作为入口与用户直接交互)
    coordinator_agent = LlmAgent(
        name="coordinator_agent",
        description="Primary interface orchestrating the CDN AI Diagnostic Team.",
        model="gemini-3.5-flash",
        instruction=coordinator_instruction,
        tools=[
            FunctionTool(func=get_current_time)
        ], # 协调智能体通过框架自动注入的转移工具分发任务
        sub_agents=[
            get_cdn_agent,
            log_analysis_agent,
            troubleshooting_agent,
            execution_agent
        ]
    )

    return coordinator_agent

# 暴露 root_agent 供 Google ADK Web/CLI 平台加载运行
root_agent = create_multi_agent_system()

async def run_sample_diagnostic():
    """运行示例 turns 用于多智能体功能验证。"""
    agent = create_multi_agent_system()
    print(f"已加载 Hub & Spoke 多智能体协同团队 (已配置 AutoFlow 自动流转机制).")
    print(f"根节点/协调智能体: {agent.name}")
    print(f"子智能体列表: {[s.name for s in agent.sub_agents]}")
    
    registered_tools = await agent.canonical_tools()
    print(f"主协调智能体绑定的工具数量: {len(registered_tools)}")
    
    execution_tools = await agent.sub_agents[3].canonical_tools()
    print("执行智能体 (ExecutionAgent) 绑定的工具及人工确认策略:")
    for tool in execution_tools:
         print(f" - {tool.name} (是否需要人工确认: {getattr(tool, '_require_confirmation', False)})")

if __name__ == "__main__":
    asyncio.run(run_sample_diagnostic())

