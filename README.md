# 一梦江湖顶铁查询

一个只提供大模型工具调用的 AstrBot 插件，直接读取
`com.awspaas.user.apps.neteasy.h42.ymjh` 中 `HikingAccountWebApi` 的查询结果。
适配 AstrBot 4.29.1 的插件接口，不绑定聊天平台。

只包含两个查询工具，没有知识库同步、向量检索、定时任务、导入写库或聊天指令。
角色编号始终作为字符串传递，保留长编号和前导零，拒绝科学计数法。

## 安装与配置

1. 将整个插件目录放到 AstrBot 的 `data/plugins/astrbot_plugin_h42_dingtie_search`，
   或在插件管理中上传发布 ZIP。
2. 安装 `requirements.txt` 中的依赖；通过 AstrBot 插件安装流程安装时由框架处理依赖。
3. 重启或重载插件，在插件配置中填写以下项目并保存。
4. 使用支持工具调用的模型，并确保当前会话/人格允许使用本插件的两个工具。

| 配置项            | 说明                                                                        |
| ----------------- | --------------------------------------------------------------------------- |
| `api_base_url`    | 实际发布的查询基础地址，包含 `/accounts`，不含 `/search` 或 `/records/编号` |
| `authorization`   | 可选，完整 Authorization 值，例如 `Basic <base64值>` 或 `Bearer <令牌>`     |
| `timeout_seconds` | 超时秒数，默认 15，范围 1–120                                               |

例如部署地址为 `https://example.com/published-api/accounts`，则填写该地址，插件会请求：

```text
GET  https://example.com/published-api/accounts/records/{role_id}
POST https://example.com/published-api/accounts/search
```

此处域名及 `published-api` 仅为占位示例。实际前缀取决于 AWS PaaS 的 API 发布配置，
不能仅凭 Java 的 `@Path("/accounts")` 推断完整外部地址。
该插件使用 REST 查询 API，不使用 `/r/jd` 导入命令或控制台登录 SID。

## 模型调用方式

### 单个角色编号：`h42_dingtie_get`

```json
{
  "role_id": "00123456789012345678"
}
```

可选参数：`server_name`（默认空字符串，不筛选），`high_risk`（默认 `-1` 不筛选，
`0` 非高危，`1` 高危）。只有用户明确要求筛选时才传，避免因角色转服或风险过滤漏查。
编号必须放在引号内，不能传 JSON 数值。

### 多个编号或关键词：`h42_dingtie_search`

```json
{
  "query": "00123456789012345678 998877665544332211",
  "limit": 10,
  "offset": 0
}
```

```json
{
  "query": "角色名关键词",
  "server_name": "用户指定的区服",
  "high_risk": 1,
  "limit": 10,
  "offset": 0
}
```

`query` 最长 1024 字符，`limit` 为 1–100，`offset` 为非负整数。
后端一次最多提取 20 个编号；多编号提取规则为至少 6 位连续数字。
少于 6 位的编号请逐个调用 `h42_dingtie_get`。
如果包含编号，后端优先精确匹配编号；没有编号才对角色名、区服和描述进行关键词匹配，
最多 8 个空格分隔的关键词，所有关键词都要匹配。

工具返回 JSON 字符串，保留后端的 `success/data` 或 `success/error` 结构。
角色数据包含 `id`、`name`、`server_name`、`description`、`high_risk`、
`inclusion_time`、`match_type`、`score` 等原始字段，不包含图片。
搜索结果同时保留 `requested_ids`、`items`、`total`、`has_more`、`limit` 和 `offset`。
接口只返回审核已通过的记录。

## 验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试通过本地 HTTP 服务检查请求路径、请求体、长编号、筛选、分页、旧图片字段过滤及错误处理。
测试中替代了 AstrBot 的加载入口，因此不等于完整 AstrBot 实机验证。
实际部署还需填写可访问的 API 地址和认证信息，并在 AstrBot 中确认模型触发工具调用。

开发接口参考：[AstrBot 工具注册](https://docs.astrbot.app/dev/star/guides/ai.html)、
[插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)。
