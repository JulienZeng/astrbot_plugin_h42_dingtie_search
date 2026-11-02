# 一梦江湖顶铁查询

一个提供大模型工具调用的 AstrBot 插件，直接对接
`com.awspaas.user.apps.neteasy.h42.ymjh` 中 `HikingAccountWebApi`。
适配 AstrBot 4.29.1 的插件接口，不绑定聊天平台。

当前版本 **1.0.1**，提供精确查询、关键词搜索、待审核申请和实际附件上传四个工具。
没有知识库同步、向量检索、定时任务、旧数据导入或聊天指令。
角色编号始终作为字符串传递，保留长编号和前导零，拒绝科学计数法。

## 安装与配置

1. 将整个插件目录放到 AstrBot 的 `data/plugins/astrbot_plugin_h42_dingtie_search`，
   或在插件管理中上传发布 ZIP。
2. 安装 `requirements.txt` 中的依赖；通过 AstrBot 插件安装流程安装时由框架处理依赖。
3. 重启或重载插件，在插件配置中填写以下项目并保存。
4. 使用支持工具调用的模型，并确保当前会话/人格允许使用所需工具；新增的两个写入工具只应在用户明确要求提交或上传时调用。

| 配置项            | 说明                                                                        |
| ----------------- | --------------------------------------------------------------------------- |
| `api_base_url`    | 实际服务发布基址，也兼容原先以 `/accounts` 结尾的地址；不含 `/search` 或 `/records/编号` |
| `authorization`   | 可选，完整 Authorization 值，例如 `Basic <base64值>` 或 `Bearer <令牌>`     |
| `timeout_seconds` | 查询和申请请求超时秒数，默认 15，范围 1–120 |
| `upload_timeout_seconds` | 每个附件读取和上传各自的超时秒数，默认 60，范围 1–120 |

例如填写 `https://example.com/published-api` 或 `https://example.com/published-api/accounts`，
插件都会使用以下地址，不会重复拼接 `/accounts`：

```text
GET  https://example.com/published-api/accounts/records/{role_id}
POST https://example.com/published-api/accounts/search
POST https://example.com/published-api/accounts/records
POST https://example.com/published-api/accounts/records/{role_id}/attachments?filename=...
```

此处域名及 `published-api` 仅为占位示例。实际前缀取决于 AWS PaaS 的 API 发布配置，
不能仅凭 Java 的 `@Path("/accounts")` 推断完整外部地址。
该插件使用 REST Web API，不使用 `/r/jd` 导入命令或控制台登录 SID。
1.0.0 的地址和 Authorization 配置可以继续使用，升级只需补上默认的附件超时配置。
Basic 认证填写完整的 `Basic <Base64(用户名:密码)>`；凭据仅保存在 AstrBot 插件配置中，
不写入源码、工具参数、日志或安装包。

## 管理员权限

`h42_dingtie_submit` 和 `h42_dingtie_upload_attachment` **仅限 AstrBot 管理员执行**。
在 AstrBot 的“配置文件 → 访问控制 → 管理员 → 管理员 ID”中指定允许的用户，
可通过 `/sid` 获取用户 ID，参见 [AstrBot 管理员配置说明](https://docs.astrbot.app/use/command.html)。

插件在两个工具函数入口检查当前消息的 `event.is_admin()`。普通用户或缺少消息上下文时，
返回 `PERMISSION_DENIED`，不会读取附件、返回本轮写入缓存或发送后端请求。
自动上传和单独补传都执行同样的检查；权限不取自模型参数或聊天文本中的管理员声明。
原有 `h42_dingtie_get`、`h42_dingtie_search` 两个查询工具不增加管理员限制。
后端固定使用 `admin` 创建流程，与聊天用户是否具备调用权限是两回事。

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

### 提交申请：`h42_dingtie_submit`

管理员明确要求新增人员时调用。角色编号、角色名、区服和标题必填；说明选填。
标题可按本次申请生成，角色名和区服应来自用户提供的信息。

```json
{
  "role_id": "00123456789012345678",
  "name": "示例角色",
  "server_name": "江湖大梦",
  "title": "顶铁人员申请 - 江湖大梦 / 示例角色"
}
```

后端收到的编号字段为 `id`。插件显式组装请求，只发送 `id`、`name`、`server_name`、`title`，
有说明时再发送 `description`（最多 2000 字符）。不发送 `uid`、高危、审核状态、数据库 ID 或 `attachment`。
服务端固定使用 `admin` 创建待审核申请，成功响应必须为 HTTP `201`、`review_status=0`。
向用户反馈“申请已提交，等待审核”；申请中在公开查询里查不到，不能因此再次新增。
`409/ALREADY_EXISTS` 表示编号已经存在，包含待审核记录，不会覆盖原记录。

如用户要求连同当前消息中的全部附件提交，在同一调用中增加：

```json
{
  "role_id": "00123456789012345678",
  "name": "示例角色",
  "server_name": "江湖大梦",
  "title": "顶铁人员申请 - 示例角色",
  "description": "用户提供的申请说明",
  "upload_attachments": true
}
```

默认 `upload_attachments=false`，只发送一次人员申请请求。
设为 `true` 时，先确认申请成功，再按消息顺序逐个上传文件或图片，不并发上传。
没有可读取的附件时会提示发送实际文件。若只上传部分附件，先提交申请，再使用下面的单附件工具。

### 补传一个附件：`h42_dingtie_upload_attachment`

```json
{
  "role_id": "00123456789012345678",
  "attachment_index": 1,
  "filename": "证据截图.png"
}
```

- `attachment_index` 默认 1，仅对消息中的文件和图片计数，从 1 开始；直接引用消息中的附件在引用段位置展开。
- `filename` 可省略。文件消息保留原名；图片使用 `evidence_序号` 加实际本地文件的扩展名。只有用户要求改名时才指定。
- 文件由 AstrBot 的消息组件解析，只读取本条或直接引用消息的附件。模型不能指定任意服务器路径、下载 URL、Base64 或文件 ID。
- 适配器必须提供可读取的 `File` / `Image` 消息段；引用消息只有文本或消息 ID 时，请重新发送实际文件。
- 文件必须非空、最多 10 MiB（10,485,760 字节）；文件名最多 128 个 UTF-16 单元，不含路径、控制字符、`@@@@` 或 `\ / < > : " | ? *`。
- 同一人员的全部文件名及 `@@@@` 分隔符总长度不得超过 128。插件检查本轮已成功上传的文件名，服务端检查全部历史附件。
- 上传请求为 `application/octet-stream` 原始字节流，文件名通过 URL 参数编码；不使用 JSON、Base64 或 multipart。
- 只有已有有效流程绑定的待审核申请允许上传。`409/ATTACHMENT_CONFLICT` 不视为成功，也不自动覆盖或改名重传。
- `file_id` 只是附件标识，不能拼成下载地址。查询接口继续不返回图片。

## 部分成功与重复调用

人员申请和附件上传是独立操作。自动上传过程中某个附件失败时，顶层 `success=true` 仍表示人员申请成功，
另有 `partial_success=true`，`attachments` 按文件列出成功或失败详情。应明确回答
“人员申请已提交，部分附件上传失败”，列出失败文件，不能把整条申请说成失败。

同一条消息内，同一编号的成功申请以及同名附件的成功上传会复用已有结果，返回 `reused_result=true`，
避免模型重复调用造成重复写入；并发调用也会按序处理。
网络超时、服务端 `500` 或成功响应格式异常时，写入工具返回 `outcome_unknown=true`，
并保留编号、操作类型及文件名；本轮重复调用不会重新写入，应交管理员核对后再处理。
这种消息内保护不跨重启或新消息持久化；跨消息由服务端编号唯一性、同名附件检查继续保护，
不能据此把网络失败自动当作可重试。

申请成功、附件失败后补传应调用上传工具，不再调用申请工具。
如果上一轮的文件不在当前消息里，请直接引用带附件的消息，或重新发送文件。

## 1.0.1 变更

- 保留原有两个查询工具，新增 `h42_dingtie_submit`、`h42_dingtie_upload_attachment`。
- 提交申请和上传附件仅限 AstrBot 管理员执行，查询权限不变。
- 支持待审核申请、消息附件顺序上传、部分成功反馈及本轮重复写入保护。
- 兼容服务发布基址和原 `/accounts` 配置，新增附件超时配置。
- 保持编号字符串校验、查询图片字段过滤；不实现审核、附件下载或删除接口。

## 验证

```bash
python -m pip install -r requirements.txt
python -m unittest discover -s tests -v
```

测试通过本地 HTTP 服务检查查询、待审核申请、原始字节上传、中文文件名编码、10 MiB 边界、
字段白名单、部分成功、重复调用、写入状态不确定、管理员权限及错误响应。
测试中替代了 AstrBot 的加载入口，因此不等于完整 AstrBot 实机验证。
实际部署还需填写可访问的 API 地址和认证信息，并在 AstrBot 中确认模型触发工具调用。

2026-10-28 在此前提供的实际服务地址做了无写入探测：搜索返回 `200/success=true`，
申请空对象与空附件校验请求均返回非 JSON 的 `404`。没有创建测试人员或上传文件。
因此新版申请/上传 API 的部署、发布和实际附件存储尚待联调；本地测试通过不表示线上写入已可用。

开发接口参考：[AstrBot 工具注册](https://docs.astrbot.app/dev/star/guides/ai.html)、
[插件配置](https://docs.astrbot.app/dev/star/guides/plugin-config.html)。
