---
name: gcp_cdn_skill
description: Google Cloud CDN 日志与配置分析及全套 CDN 服务配置管理工具。用于获取指定项目及域名在指定时间段内的访问日志，自动分析错误率、配置详情与源站状态，并支持对负载均衡前端转发规则、URL Maps 路由跳转、后端服务 (Backend Services) 与后端存储桶 (Backend Buckets) 的 CDN 缓存策略实施一站式配置。触发场景：'分析GCP CDN日志'、'诊断GCP CDN问题'、'查询GCP CDN配置'、'配置GCP CDN服务'。
---

# Google Cloud CDN 日志与分析

## 工作流程

1. **收集参数**：项目ID (`vicdemo`)、域名、时间范围、客户端IP、请求URL
2. **日志分析**：使用 Google Cloud Logging API 获取指定时间段内的 HTTP Load Balancing 日志
3. **配置分析**：使用 Google Compute API 查询 Load Balancer, URL Maps, Backend Buckets, Backend Services 及 Frontend Forwarding Rules 的详细 CDN 缓存策略与前端端口
4. **缓存操作**：支持对指定的 Load Balancer 路径触发 CDN 缓存刷新 (Cache Invalidation)
5. **服务配置**：支持启用/禁用 Backend Service/Backend Bucket 的 CDN 与配置缓存模式、启用/禁用 URL Map 的 HTTP 到 HTTPS 强制跳转，以及删除前端 HTTP 转发规则（如禁用 Port 80）
6. **源站验证**：对识别出的源站 (Backend) 模拟 HTTP 请求，验证源站返回
7. **输出报告**：结合日志、详细配置与源站状态给出最终诊断与操作结论

## 快速开始

在 `agent.py` 中引用此 Skill 的脚本函数作为 ADK Tools：

```python
from gcp_cdn_skill.scripts.gcp_cdn_diagnostics import (
    analyze_cloud_logging, 
    get_load_balancer_config,
    invalidate_cdn_cache,
    get_cdn_policy_details,
    list_load_balancers,
    update_backend_service_cdn_config,
    list_global_forwarding_rules,
    delete_global_forwarding_rule,
    update_url_map_redirect,
    update_backend_bucket_cdn_config
)
```

## 参数说明

| 参数 | 必填 | 默认值 | 说明 |
|------|------|--------|------|
| project_id | ✅ | `vicdemo` | GCP 项目 ID |
| domain | ✅ | - | CDN 域名 / Load Balancer 域名 |
| start_time | ✅ | - | 开始时间 (ISO 8601) |
| end_time | ✅ | - | 结束时间 (ISO 8601) |
| ip | ❌ | - | 客户端 IP |
| url | ❌ | - | 请求 URL |

## 环境要求

```bash
# 安装依赖
pip install google-cloud-logging google-api-python-client requests
```

## 文件结构

```
gcp_cdn_skill/
├── SKILL.md
├── requirements.txt
└── scripts/
    ├── __init__.py
    └── gcp_cdn_diagnostics.py
```
