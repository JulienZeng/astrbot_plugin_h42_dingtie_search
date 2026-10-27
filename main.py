"""将一梦江湖顶铁查询 API 暴露为 AstrBot 的两个 LLM 工具。"""

import json
import re
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.star import Context, Star

ROLE_ID = re.compile(r"[0-9]{1,128}")
SCIENTIFIC_NUMBER = re.compile(
    r"(?<![A-Za-z0-9])(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)e[+-]?[0-9]+",
    re.IGNORECASE,
)


def error_result(code: str, message: str, **extra) -> str:
    return json.dumps(
        {"success": False, "error": {"code": code, "message": message}, **extra},
        ensure_ascii=False,
    )


def filters(server_name: str, high_risk: int) -> dict:
    if not isinstance(server_name, str) or len(server_name.strip()) > 128:
        raise ValueError("server_name 必须是最多 128 个字符的字符串")
    if type(high_risk) is not int or high_risk not in (-1, 0, 1):
        raise ValueError("high_risk 必须为整数：-1 不筛选，0 非高危，1 高危")
    result = {}
    if server_name.strip():
        result["server_name"] = server_name.strip()
    if high_risk != -1:
        result["high_risk"] = high_risk
    return result


class H42DingtieSearchPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.config = config

    @filter.llm_tool(name="h42_dingtie_get")
    async def h42_dingtie_get(
        self,
        event: AstrMessageEvent,
        role_id: str,
        server_name: str = "",
        high_risk: int = -1,
    ) -> str:
        """按完整角色编号精确查询一梦江湖顶铁、顶拍奖励号的已通过记录。

        用户提供一个角色编号时优先调用此工具，只提取编号，不传整段提问。
        返回角色名、区服、描述、高危标记和收录时间等接口数据。
        仅 error.code=NOT_FOUND 表示该编号在当前筛选下没有已通过记录；
        其他错误表示查询失败，不能回答为未收录。未收录也不代表账号安全。

        Args:
            role_id(string): 1 到 128 位完整数字字符串，保留前导零，禁止数值类型、科学计数法或猜测补全。
            server_name(string): 区服精确筛选，默认空字符串表示不筛选；仅在用户明确要求时填写。
            high_risk(number): 整数高危筛选，默认 -1 表示不筛选，0 表示非高危，1 表示高危。
        """
        if not isinstance(role_id, str) or not ROLE_ID.fullmatch(role_id.strip()):
            return error_result(
                "INVALID_ARGUMENT",
                "role_id 必须是 1 到 128 位完整数字字符串，不能使用数值或科学计数法",
            )
        try:
            params = filters(server_name, high_risk)
        except ValueError as exc:
            return error_result("INVALID_ARGUMENT", str(exc))
        return await self._request("GET", f"/records/{role_id.strip()}", params=params)

    @filter.llm_tool(name="h42_dingtie_search")
    async def h42_dingtie_search(
        self,
        event: AstrMessageEvent,
        query: str,
        limit: int = 10,
        offset: int = 0,
        server_name: str = "",
        high_risk: int = -1,
    ) -> str:
        """搜索一梦江湖顶铁、顶拍奖励号的已通过记录，支持多个编号或关键词。

        有多个角色编号时只传完整编号并用空格分隔，一次最多 20 个；
        没有编号时传角色名、区服或描述关键词，最多 8 个关键词，以空格分隔。
        编号命中走精确查询；关键词匹配不等于确认是同一角色。
        返回 items、total、has_more 和分页信息；has_more 为 true 时可增加 offset 继续查询。
        只有 success=true 且 total=0 才表示当前条件下无已通过记录；
        空页不等于无记录，success=false 表示查询失败。未收录不代表账号安全。

        Args:
            query(string): 1 到 1024 字符，只传待查编号或关键词，不传整段提问；编号保留前导零，禁止数值和科学计数法。
            limit(number): 每页条数，1 到 100 的整数，默认 10。
            offset(number): 跳过的结果条数，非负整数，默认 0；下一页使用 offset 加 limit。
            server_name(string): 区服精确筛选，默认空字符串表示不筛选；仅在用户明确要求时填写。
            high_risk(number): 整数高危筛选，默认 -1 表示不筛选，0 表示非高危，1 表示高危。
        """
        if not isinstance(query, str) or not 1 <= len(query.strip()) <= 1024:
            return error_result(
                "INVALID_ARGUMENT", "query 必须是 1 到 1024 字符的字符串"
            )
        if SCIENTIFIC_NUMBER.search(query):
            return error_result(
                "INVALID_ARGUMENT", "编号不接受科学计数法，请提供完整数字字符串"
            )
        if type(limit) is not int or not 1 <= limit <= 100:
            return error_result("INVALID_ARGUMENT", "limit 必须是 1 到 100 之间的整数")
        if type(offset) is not int or not 0 <= offset <= 2147483647:
            return error_result(
                "INVALID_ARGUMENT", "offset 必须是 0 到 2147483647 之间的整数"
            )
        try:
            body = filters(server_name, high_risk)
        except ValueError as exc:
            return error_result("INVALID_ARGUMENT", str(exc))
        body.update(query=query.strip(), limit=limit, offset=offset)
        return await self._request("POST", "/search", json=body)

    async def _request(self, method: str, path: str, **kwargs) -> str:
        base_url = self.config.get("api_base_url", "")
        authorization = self.config.get("authorization", "")
        timeout = self.config.get("timeout_seconds", 15)
        try:
            if not isinstance(base_url, str) or not isinstance(authorization, str):
                raise TypeError
            base_url = base_url.strip().rstrip("/")
            authorization = authorization.strip()
            parts = urlsplit(base_url)
            if (
                parts.scheme not in ("http", "https")
                or not parts.hostname
                or parts.username is not None
                or parts.password is not None
                or parts.query
                or parts.fragment
            ):
                raise ValueError
            if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                raise ValueError
            if "\r" in authorization or "\n" in authorization:
                raise ValueError
        except (TypeError, ValueError):
            return error_result(
                "CONFIG_ERROR",
                "请配置有效的 api_base_url（包含 /accounts 路径，不含查询参数或账号密码）、"
                "单行 Authorization 和 1 到 120 秒的超时",
            )

        headers = {"Accept": "application/json"}
        if authorization:
            headers["Authorization"] = authorization
        try:
            async with (
                aiohttp.ClientSession(
                    timeout=aiohttp.ClientTimeout(total=timeout), headers=headers
                ) as session,
                session.request(
                    method, base_url + path, allow_redirects=False, **kwargs
                ) as response,
            ):
                status = response.status
                if status in (401, 403):
                    return error_result(
                        "AUTH_ERROR",
                        "接口认证失败，请检查 Authorization 或 API 访问权限",
                        http_status=status,
                    )
                if 300 <= status < 400:
                    return error_result(
                        "HTTP_ERROR",
                        "接口发生重定向，请配置最终的 API 地址",
                        http_status=status,
                    )
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    return error_result(
                        "INVALID_RESPONSE",
                        "接口未返回有效 JSON，请检查 API 地址和网关配置",
                        http_status=status,
                    )
        except TimeoutError:
            return error_result("TIMEOUT", "接口请求超时，无法判断是否收录，请稍后重试")
        except (aiohttp.ClientError, ValueError):
            return error_result(
                "NETWORK_ERROR", "无法连接查询接口，请检查 API 地址、网络及证书"
            )

        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error")
            if (
                isinstance(error, dict)
                and isinstance(error.get("code"), str)
                and isinstance(error.get("message"), str)
                # 只有精确查询接口约定的 404 才能解释为角色未收录。
                and (
                    error["code"] != "NOT_FOUND" or (method == "GET" and status == 404)
                )
            ):
                return error_result(error["code"], error["message"], http_status=status)
        if status >= 400:
            return error_result(
                "HTTP_ERROR",
                f"查询接口返回 HTTP {status}，无法判断是否收录",
                http_status=status,
            )
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or not isinstance(payload.get("data"), dict)
        ):
            return error_result("INVALID_RESPONSE", "接口响应不符合 success/data 协议")

        data = payload["data"]
        if method == "GET":
            records = [data]
            if data.get("id") != path.removeprefix("/records/"):
                return error_result(
                    "INVALID_RESPONSE", "接口返回的角色编号与请求不一致或不是字符串"
                )
        else:
            records = data.get("items")
            if (
                not isinstance(records, list)
                or type(data.get("total")) is not int
                or data["total"] < 0
            ):
                return error_result(
                    "INVALID_RESPONSE", "搜索接口缺少有效的 items/total"
                )
        for record in records:
            if (
                not isinstance(record, dict)
                or not isinstance(record.get("id"), str)
                or not ROLE_ID.fullmatch(record["id"])
            ):
                return error_result(
                    "INVALID_RESPONSE", "接口中的角色编号必须是完整数字字符串"
                )
            # 兼容尚未更新的后端，旧版图片字段也不返回给模型。
            record.pop("images", None)
        return json.dumps(payload, ensure_ascii=False)
