import logging
import requests
from google.cloud import logging as cloud_logging
from googleapiclient import discovery
from google.auth import default as auth_default
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

def get_current_time() -> str:
    """获取当前 UTC 日期和时间，返回 ISO 8601 格式的字符串（例如：'2026-06-02T18:00:00Z'）。

    Returns:
         str: 当前 UTC 时间字符串。
    """
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def analyze_cloud_logging(project_id: str, domain: str, start_time: str, end_time: str, ip: str = None, url: str = None) -> dict:
    """查询 Google Cloud Logging 日志以获取符合条件的 HTTP 负载均衡器请求。

    Args:
        project_id: GCP 项目 ID (例如 "vicdemo")。
        domain: 过滤 CDN 或负载均衡器域名的条件。
        start_time: ISO 8601 格式的时间范围起始值 (例如 "2026-05-29T10:00:00Z")。
        end_time: ISO 8601 格式的时间范围截止值。
        ip: 可选，过滤特定客户端 IP。
        url: 可选，过滤特定的请求 URL 或路径。

    Returns:
        dict: 包含请求统计、HTTP 状态码分布以及缓存命中状态的报告字典。
    """
    try:
        # Initialize client with specified project and ADC
        client = cloud_logging.Client(project=project_id)
        
        # Build the log filter
        filter_str = (
            'resource.type="http_load_balancer" '
            f'timestamp >= "{start_time}" AND timestamp <= "{end_time}" '
        )
        
        if url:
            filter_str += f'AND httpRequest.requestUrl:"{url}" '
        elif domain:
            filter_str += f'AND httpRequest.requestUrl:"{domain}" '
            
        if ip:
            filter_str += f'AND httpRequest.remoteIp="{ip}" '

        logger.info(f"Querying Cloud Logging with filter: {filter_str}")
        
        entries = client.list_entries(filter_=filter_str, order_by=cloud_logging.DESCENDING)
        
        total_requests = 0
        status_codes = {}
        cache_statuses = {"HIT": 0, "MISS": 0, "OTHER": 0}
        error_samples = []
        
        for entry in entries:
            total_requests += 1
            http_request = entry.http_request
            if not http_request:
                continue
                
            status = str(http_request.get("status", "UNKNOWN"))
            status_codes[status] = status_codes.get(status, 0) + 1
            
            # Extract Cache info from status details or custom headers if available in labels
            # Cloud CDN logs often put cache status in statusDetails or populate cacheLookup/cacheHit
            cache_hit = http_request.get("cacheHit", False)
            cache_lookup = http_request.get("cacheLookup", False)
            
            if cache_hit:
                cache_statuses["HIT"] += 1
            elif cache_lookup:
                cache_statuses["MISS"] += 1
            else:
                cache_statuses["OTHER"] += 1
                
            # Sample errors (4xx and 5xx)
            if status.startswith("4") or status.startswith("5"):
                if len(error_samples) < 5:
                    error_samples.append({
                        "timestamp": entry.timestamp.isoformat() if entry.timestamp else "",
                        "status": status,
                        "url": http_request.get("requestUrl", ""),
                        "ip": http_request.get("remoteIp", "")
                    })

        return {
            "status": "success",
            "project_id": project_id,
            "filter": filter_str,
            "summary": {
                "total_requests": total_requests,
                "status_codes": status_codes,
                "cache_status": cache_statuses,
                "error_samples": error_samples
            }
        }
    except Exception as e:
        logger.error(f"Error querying Cloud Logging: {e}")
        return {"status": "error", "message": str(e)}


def get_load_balancer_config(project_id: str, domain: str) -> dict:
    """获取 Google Cloud CDN 和负载均衡器的配置详情。

    Args:
        project_id: GCP 项目 ID。
        domain: 需要在 URL Map 中查找的目标域名。

    Returns:
        dict: 包含匹配的 URL Map、目标代理和关联后端服务的详细配置。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        # 1. List URL Maps to find the one matching the domain
        url_maps_response = compute.urlMaps().list(project=project_id).execute()
        target_url_map = None
        
        for url_map in url_maps_response.get('items', []):
            # Check host rules
            for host_rule in url_map.get('hostRules', []):
                if domain in host_rule.get('hosts', []):
                    target_url_map = url_map
                    break
            # Fallback to default service if no host rule matches but it's the only one, or use it as heuristic
            if not target_url_map and domain in url_map.get('name', ''):
                 target_url_map = url_map
                 
        if not target_url_map:
            # If still not found, try to return the first one as default guess or return error
            items = url_maps_response.get('items', [])
            if items:
                target_url_map = items[0]
            else:
                return {"status": "error", "message": f"No URL Map found for domain {domain}"}

        # 2. Get Backend Services linked in the URL Map
        backend_services = []
        default_service_url = target_url_map.get('defaultService')
        if default_service_url:
             backend_services.append(_get_backend_details(compute, project_id, default_service_url))
             
        for path_matcher in target_url_map.get('pathMatchers', []):
             svc_url = path_matcher.get('defaultService')
             if svc_url and svc_url not in [b['selfLink'] for b in backend_services if 'selfLink' in b]:
                  backend_services.append(_get_backend_details(compute, project_id, svc_url))
             for route_rule in path_matcher.get('routeRules', []):
                  svc_url = route_rule.get('service')
                  if svc_url and svc_url not in [b['selfLink'] for b in backend_services if 'selfLink' in b]:
                       backend_services.append(_get_backend_details(compute, project_id, svc_url))
                       
        return {
            "status": "success",
            "url_map_name": target_url_map.get('name'),
            "backend_services": backend_services
        }
    except Exception as e:
         logger.error(f"Error retrieving Compute config: {e}")
         return {"status": "error", "message": str(e)}


def _get_backend_details(compute_service, project_id: str, service_url: str) -> dict:
    """Helper to parse backend service or backend bucket details."""
    try:
        # service_url format: https://www.googleapis.com/compute/v1/projects/<proj>/global/backendServices/<name>
        parts = service_url.split('/')
        resource_type = parts[-2]
        resource_name = parts[-1]
        
        config = {}
        if resource_type == 'backendServices':
             res = compute_service.backendServices().get(project=project_id, backendService=resource_name).execute()
             config = {
                 "type": "backendService",
                 "name": resource_name,
                 "enableCDN": res.get('enableCDN', False),
                 "cacheMode": res.get('cdnPolicy', {}).get('cacheMode', 'UNKNOWN'),
                 "selfLink": service_url,
                 "backends": [{"group": b.get('group')} for b in res.get('backends', [])]
             }
        elif resource_type == 'backendBuckets':
             res = compute_service.backendBuckets().get(project=project_id, backendBucket=resource_name).execute()
             config = {
                 "type": "backendBucket",
                 "name": resource_name,
                 "enableCDN": res.get('enableCDN', False),
                 "bucketName": res.get('bucketName'),
                 "selfLink": service_url
             }
        return config
    except Exception as e:
         return {"type": "unknown", "url": service_url, "error": str(e)}


def get_backend_service_origin(project_id: str, backend_service_name: str) -> dict:
    """获取后端服务的源站服务器详细信息（例如：实例组中的实例详情）。

    Args:
        project_id: GCP 项目 ID。
        backend_service_name: 后端服务名称。

    Returns:
        dict: 包含源站实例及 IP 信息的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        res = compute.backendServices().get(project=project_id, backendService=backend_service_name).execute()
        backends = res.get('backends', [])
        
        origin_infos = []
        for backend in backends:
             group_url = backend.get('group')
             if group_url:
                  # group_url format: .../zones/<zone>/instanceGroups/<name>
                  parts = group_url.split('/')
                  zone = parts[-3]
                  group_name = parts[-1]
                  
                  # List instances in the group
                  instances_res = compute.instanceGroups().listInstances(
                       project=project_id, zone=zone, instanceGroup=group_name, body={}
                  ).execute()
                  
                  for item in instances_res.get('items', []):
                       inst_url = item.get('instance')
                       inst_name = inst_url.split('/')[-1]
                       
                       # Get instance details for External IP
                       inst_details = compute.instances().get(project=project_id, zone=zone, instance=inst_name).execute()
                       ext_ip = None
                       for net_if in inst_details.get('networkInterfaces', []):
                            for acc_cfg in net_if.get('accessConfigs', []):
                                 if 'natIP' in acc_cfg:
                                      ext_ip = acc_cfg['natIP']
                                      break
                       
                       origin_infos.append({
                            "instance_name": inst_name,
                            "zone": zone,
                            "external_ip": ext_ip,
                            "status": inst_details.get('status')
                       })
                       
        return {
            "status": "success",
            "backend_service_name": backend_service_name,
            "origins": origin_infos
        }
    except Exception as e:
         logger.error(f"Error retrieving Origin details: {e}")
         return {"status": "error", "message": str(e)}


def simulate_http_request(url: str) -> dict:
    """模拟 HTTP 请求以验证返回的状态码和响应头信息。

    Args:
        url: 待测试的请求 URL。

    Returns:
        dict: 包含响应状态码、Headers 头部字典以及请求耗时（毫秒）的字典。
    """
    try:
        start = datetime.now(timezone.utc)
        response = requests.get(url, timeout=10)
        end = datetime.now(timezone.utc)
        duration_ms = (end - start).total_seconds() * 1000
        
        return {
            "status": "success",
            "url": url,
            "status_code": response.status_code,
            "headers": dict(response.headers),
            "duration_ms": duration_ms,
            "text_preview": response.text[:200]
        }
    except Exception as e:
         logger.error(f"Error simulating HTTP request to {url}: {e}")
         return {"status": "error", "url": url, "message": str(e)}


def list_load_balancers(project_id: str) -> dict:
    """列出指定项目下的所有 Google Cloud 负载均衡器（URL Maps）。

    Args:
        project_id: GCP 项目 ID。

    Returns:
        dict: 包含所有负载均衡器名称及其绑定域名列表的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        response = compute.urlMaps().list(project=project_id).execute()
        items = response.get('items', [])
        
        load_balancers = []
        for item in items:
            domains = []
            for host_rule in item.get('hostRules', []):
                domains.extend(host_rule.get('hosts', []))
            
            load_balancers.append({
                "name": item.get('name'),
                "description": item.get('description', ''),
                "domains": domains,
                "default_service": item.get('defaultService')
            })
            
        return {
            "status": "success",
            "project_id": project_id,
            "load_balancers": load_balancers
        }
    except Exception as e:
        logger.error(f"Error listing Load Balancers: {e}")
        return {"status": "error", "message": str(e)}


def invalidate_cdn_cache(project_id: str, url_map_name: str, path: str, host: str = None) -> dict:
    """请求刷新/清除 Google Cloud CDN 缓存。

    Args:
        project_id: GCP 项目 ID。
        url_map_name: 负载均衡器（URL Map）名称。
        path: 需要清除缓存的目标路径（例如 "/*" 或 "/images/*"）。
        host: 可选，限制特定 Host/域名的缓存清除。

    Returns:
        dict: 缓存刷新操作的提交状态及任务 ID。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        body = {"path": path}
        if host:
            body["host"] = host
            
        logger.info(f"Invalidating cache for {url_map_name} path '{path}' and host '{host}'")
        
        operation = compute.urlMaps().invalidateCache(
            project=project_id, urlMap=url_map_name, body=body
        ).execute()
        
        return {
            "status": "success",
            "project_id": project_id,
            "url_map_name": url_map_name,
            "operation_id": operation.get('name'),
            "operation_status": operation.get('status')
        }
    except Exception as e:
        logger.error(f"Error invalidating cache: {e}")
        return {"status": "error", "message": str(e)}


def get_backend_bucket_details(project_id: str, backend_bucket_name: str) -> dict:
    """获取 GCS 后端存储桶（Backend Bucket）的详细 CDN 配置。

    Args:
        project_id: GCP 项目 ID。
        backend_bucket_name: 后端存储桶名称。

    Returns:
        dict: 描述 CDN 以及 Google Cloud Storage 存储桶配置的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        res = compute.backendBuckets().get(project=project_id, backendBucket=backend_bucket_name).execute()
        
        return {
            "status": "success",
            "name": backend_bucket_name,
            "bucket_name": res.get('bucketName'),
            "enable_cdn": res.get('enableCDN', False),
            "cdn_policy": res.get('cdnPolicy', {}),
            "compression_mode": res.get('compressionMode', 'UNKNOWN')
        }
    except Exception as e:
        logger.error(f"Error retrieving backend bucket config: {e}")
        return {"status": "error", "message": str(e)}


def get_cdn_policy_details(project_id: str, backend_service_name: str) -> dict:
    """获取后端服务（Backend Service）的详细 CDN 缓存策略与配置设置。

    Args:
        project_id: GCP 项目 ID。
        backend_service_name: 后端服务名称。

    Returns:
        dict: 描述缓存模式、签名 URL 以及缓存键策略的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        res = compute.backendServices().get(project=project_id, backendService=backend_service_name).execute()
        cdn_policy = res.get('cdnPolicy', {})
        
        return {
            "status": "success",
            "name": backend_service_name,
            "enable_cdn": res.get('enableCDN', False),
            "cache_mode": cdn_policy.get('cacheMode', 'UNKNOWN'),
            "client_ttl": cdn_policy.get('clientTtl'),
            "default_ttl": cdn_policy.get('defaultTtl'),
            "max_ttl": cdn_policy.get('maxTtl'),
            "negative_caching": cdn_policy.get('negativeCaching', False),
            "negative_caching_policy": cdn_policy.get('negativeCachingPolicy', []),
            "cache_key_policy": cdn_policy.get('cacheKeyPolicy', {}),
            "signed_url_key_names": cdn_policy.get('signedUrlKeyNames', [])
        }
    except Exception as e:
        logger.error(f"Error retrieving CDN policy: {e}")
        return {"status": "error", "message": str(e)}


def update_backend_service_cdn_config(project_id: str, backend_service_name: str, enable_cdn: bool = None, cache_mode: str = None) -> dict:
    """启用、禁用或更新后端服务（Backend Service）的 Google Cloud CDN 配置。

    Args:
        project_id: GCP 项目 ID (例如 "vicdemo")。
        backend_service_name: 后端服务名称 (例如 "sg-intl-neg")。
        enable_cdn: 可选，布尔值，用于开启 (True) 或关闭 (False) Cloud CDN。
        cache_mode: 可选，字符串，用于设置缓存模式。支持的值包括：'CACHE_ALL_STATIC', 'USE_ORIGIN_HEADERS', 'FORCE_CACHE_ALL'。

    Returns:
        dict: 描述更新操作结果状态的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        body = {}
        if enable_cdn is not None:
            body["enableCDN"] = enable_cdn
            
        if cache_mode:
            valid_modes = ['CACHE_ALL_STATIC', 'USE_ORIGIN_HEADERS', 'FORCE_CACHE_ALL']
            if cache_mode not in valid_modes:
                return {
                    "status": "error", 
                    "message": f"Invalid cache_mode '{cache_mode}'. Supported values: {', '.join(valid_modes)}"
                }
            
            # Fetch current backend service to preserve other cdnPolicy fields or merge safely
            try:
                 current_res = compute.backendServices().get(project=project_id, backendService=backend_service_name).execute()
                 current_policy = current_res.get('cdnPolicy', {})
                 current_policy["cacheMode"] = cache_mode
                 body["cdnPolicy"] = current_policy
            except Exception as e:
                 logger.warning(f"Failed to fetch current cdnPolicy for merging: {e}. Using clean cdnPolicy dict.")
                 body["cdnPolicy"] = {"cacheMode": cache_mode}
                 
        if not body:
            return {"status": "error", "message": "No configuration fields specified to update."}

        logger.info(f"Patching Backend Service '{backend_service_name}' with body: {body}")
        
        operation = compute.backendServices().patch(
            project=project_id, backendService=backend_service_name, body=body
        ).execute()
        
        return {
            "status": "success",
            "project_id": project_id,
            "backend_service_name": backend_service_name,
            "operation_id": operation.get('name'),
            "operation_status": operation.get('status'),
            "updated_fields": list(body.keys())
        }
    except Exception as e:
        logger.error(f"Error updating CDN config for {backend_service_name}: {e}")
        return {"status": "error", "message": str(e)}


def list_global_forwarding_rules(project_id: str) -> dict:
    """列出指定 GCP 项目下的所有全局转发规则（Forwarding Rules）。

    Args:
        project_id: GCP 项目 ID。

    Returns:
        dict: 包含转发规则名称、IP 地址、端口范围和目标代理等配置列表的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        response = compute.globalForwardingRules().list(project=project_id).execute()
        items = response.get('items', [])
        
        rules = []
        for item in items:
            rules.append({
                "name": item.get('name'),
                "IPAddress": item.get('IPAddress'),
                "portRange": item.get('portRange'),
                "target": item.get('target'),
                "loadBalancingScheme": item.get('loadBalancingScheme')
            })
            
        return {
            "status": "success",
            "project_id": project_id,
            "forwarding_rules": rules
        }
    except Exception as e:
        logger.error(f"Error listing global forwarding rules: {e}")
        return {"status": "error", "message": str(e)}


def delete_global_forwarding_rule(project_id: str, forwarding_rule_name: str) -> dict:
    """删除指定的全局转发规则（Forwarding Rule）。
    
    通常用于关闭特定前端端口（例如禁用 HTTP/80 端口以强制 HTTPS）。

    Args:
        project_id: GCP 项目 ID。
        forwarding_rule_name: 待删除的全局转发规则名称。

    Returns:
        dict: 描述删除操作执行状态的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        logger.info(f"Deleting global Forwarding Rule '{forwarding_rule_name}' in project '{project_id}'")
        operation = compute.globalForwardingRules().delete(
            project=project_id, forwardingRule=forwarding_rule_name
        ).execute()
        
        return {
            "status": "success",
            "project_id": project_id,
            "forwarding_rule_name": forwarding_rule_name,
            "operation_id": operation.get('name'),
            "operation_status": operation.get('status')
        }
    except Exception as e:
        logger.error(f"Error deleting global forwarding rule {forwarding_rule_name}: {e}")
        return {"status": "error", "message": str(e)}


def update_url_map_redirect(project_id: str, url_map_name: str, https_redirect: bool) -> dict:
    """配置 URL Map 以开启 HTTP 到 HTTPS 的重定向，或禁用该重定向。

    Args:
        project_id: GCP 项目 ID。
        url_map_name: URL Map 的名称。
        https_redirect: True 开启重定向所有流量至 HTTPS，False 移除默认重定向。

    Returns:
        dict: 执行结果及重定向配置状态字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        # Fetch current URL Map first
        current_url_map = compute.urlMaps().get(project=project_id, urlMap=url_map_name).execute()
        
        # We patch the defaultUrlRedirect field
        if https_redirect:
            current_url_map["defaultUrlRedirect"] = {
                "httpsRedirect": True,
                "stripQuery": False
            }
        else:
            # If False, pop the defaultUrlRedirect or set to None/empty
            current_url_map.pop("defaultUrlRedirect", None)
            
        logger.info(f"Updating URL Map '{url_map_name}' HTTPS redirect to {https_redirect}")
        operation = compute.urlMaps().update(
            project=project_id, urlMap=url_map_name, body=current_url_map
        ).execute()
        
        return {
            "status": "success",
            "project_id": project_id,
            "url_map_name": url_map_name,
            "operation_id": operation.get('name'),
            "operation_status": operation.get('status'),
            "https_redirect_enabled": https_redirect
        }
    except Exception as e:
        logger.error(f"Error updating URL Map redirect for {url_map_name}: {e}")
        return {"status": "error", "message": str(e)}


def update_backend_bucket_cdn_config(project_id: str, backend_bucket_name: str, enable_cdn: bool = None, cache_mode: str = None) -> dict:
    """启用、禁用或更新后端存储桶（Backend Bucket）的 Google Cloud CDN 配置。

    Args:
        project_id: GCP 项目 ID (例如 "vicdemo")。
        backend_bucket_name: 后端存储桶的名称。
        enable_cdn: 可选，布尔值，用于开启 (True) 或关闭 (False) Cloud CDN。
        cache_mode: 可选，字符串，用于设置缓存模式。支持的值包括：'CACHE_ALL_STATIC', 'USE_ORIGIN_HEADERS', 'FORCE_CACHE_ALL'。

    Returns:
        dict: 描述更新操作结果状态的字典。
    """
    try:
        credentials, _ = auth_default()
        compute = discovery.build('compute', 'v1', credentials=credentials)
        
        body = {}
        if enable_cdn is not None:
            body["enableCDN"] = enable_cdn
            
        if cache_mode:
            valid_modes = ['CACHE_ALL_STATIC', 'USE_ORIGIN_HEADERS', 'FORCE_CACHE_ALL']
            if cache_mode not in valid_modes:
                return {
                    "status": "error", 
                    "message": f"Invalid cache_mode '{cache_mode}'. Supported values: {', '.join(valid_modes)}"
                }
            
            try:
                 current_res = compute.backendBuckets().get(project=project_id, backendBucket=backend_bucket_name).execute()
                 current_policy = current_res.get('cdnPolicy', {})
                 current_policy["cacheMode"] = cache_mode
                 body["cdnPolicy"] = current_policy
            except Exception as e:
                 logger.warning(f"Failed to fetch current cdnPolicy for merging: {e}. Using clean cdnPolicy dict.")
                 body["cdnPolicy"] = {"cacheMode": cache_mode}
                 
        if not body:
            return {"status": "error", "message": "No configuration fields specified to update."}

        logger.info(f"Patching Backend Bucket '{backend_bucket_name}' with body: {body}")
        
        operation = compute.backendBuckets().patch(
            project=project_id, backendBucket=backend_bucket_name, body=body
        ).execute()
        
        return {
            "status": "success",
            "project_id": project_id,
            "backend_bucket_name": backend_bucket_name,
            "operation_id": operation.get('name'),
            "operation_status": operation.get('status'),
            "updated_fields": list(body.keys())
        }
    except Exception as e:
        logger.error(f"Error updating CDN config for Backend Bucket {backend_bucket_name}: {e}")
        return {"status": "error", "message": str(e)}



