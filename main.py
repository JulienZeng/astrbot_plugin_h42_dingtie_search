"""一梦江湖顶铁人员查询、待审核申请与消息附件上传工具。"""

import asyncio
import io
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from astrbot.api import AstrBotConfig
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import File, Image, Reply
from astrbot.api.star import Context, Star

ROLE_ID = re.compile(r"[0-9]{1,128}")
MAX_FILE_BYTES = 10 * 1024 * 1024
WRITE_STATE_KEY = "h42_dingtie_write_state"
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


def text_field(value: str, name: str, maximum: int, required: bool = True) -> str:
    if not isinstance(value, str):
        raise TypeError(f"{name} 必须是字符串")
    value = value.strip()
    # Java String.length() 按 UTF-16 计数，与后端的字段长度保持一致。
    if (required and not value) or len(value.encode("utf-16-le")) // 2 > maximum:
        raise ValueError(f"{name} {'必填，' if required else ''}长度不能超过 {maximum}")
    return value


def validate_role_id(value: str) -> str:
    if not isinstance(value, str) or not ROLE_ID.fullmatch(value.strip()):
        raise ValueError(
            "role_id 必须是 1 到 128 位完整数字字符串，不能使用数值或科学计数法"
        )
    return value.strip()


def validate_filename(value: str) -> str:
    name = text_field(value, "filename", 128)
    if (
        name in (".", "..")
        or "@@@@" in name
        or re.search(r'[\\/<>:"|?*]', name)
        or any(ord(char) < 32 or 127 <= ord(char) <= 159 for char in name)
    ):
        raise ValueError("filename 不能包含路径、控制字符、非法符号或 @@@@")
    return name


def message_attachments(event: AstrMessageEvent) -> list:
    """只从本条消息及其直接引用的消息选择文件，模型不能指定本地路径或 URL。"""
    result = []
    for component in event.get_messages():
        if isinstance(component, (File, Image)):
            result.append(component)
        elif isinstance(component, Reply):
            result.extend(
                item
                for item in (component.chain or [])
                if isinstance(item, (File, Image))
            )
    return result


def write_state(event: AstrMessageEvent) -> dict:
    state = event.get_extra(WRITE_STATE_KEY)
    if state is None:
        state = {
            "submissions": {},
            "uploads": {},
            "submit_lock": asyncio.Lock(),
            "upload_lock": asyncio.Lock(),
        }
        event.set_extra(WRITE_STATE_KEY, state)
    return state


def remember_write(result: dict) -> bool:
    return (
        result.get("success") is True
        or result.get("outcome_unknown") is True
        or result.get("error", {}).get("code")
        in ("ALREADY_EXISTS", "ATTACHMENT_CONFLICT")
    )


def read_attachment(path: str) -> bytes:
    file = Path(path)
    if not path or not file.is_file():
        raise ValueError("附件未解析为可读取的普通文件")
    with file.open("rb") as content:
        return content.read(MAX_FILE_BYTES + 1)


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
        try:
            role_id = validate_role_id(role_id)
            params = filters(server_name, high_risk)
        except (TypeError, ValueError) as exc:
            return error_result("INVALID_ARGUMENT", str(exc))
        return await self._request("GET", f"/records/{role_id}", params=params)

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
        except (TypeError, ValueError) as exc:
            return error_result("INVALID_ARGUMENT", str(exc))
        body.update(query=query.strip(), limit=limit, offset=offset)
        return await self._request("POST", "/search", json=body)

    @filter.llm_tool(name="h42_dingtie_submit")
    async def h42_dingtie_submit(
        self,
        event: AstrMessageEvent,
        role_id: str,
        name: str,
        server_name: str,
        title: str,
        description: str = "",
        upload_attachments: bool = False,
    ) -> str:
        """仅 AstrBot 管理员明确要求提交时调用，创建待审核申请，可随后上传本条消息附件。

        调用权限按当前消息发送者的 AstrBot 管理员身份检查，普通用户不能代管理员提交。
        必须取得完整编号、角色名、区服和申请意图；title 可按本次申请生成，description 选填。
        成功只代表申请已提交、等待审核，不是已通过或已进入公开查询。
        申请中记录查不到是正常的；不得因查询 NOT_FOUND 而自动新增或重复提交。
        人员重复 ALREADY_EXISTS 不覆盖原记录；outcome_unknown=true 时必须让管理员核对，禁止自动重试。
        附件逐个上传，partial_success=true 表示申请已提交但部分附件失败，应列出失败文件，不能重新创建人员。

        Args:
            role_id(string): 完整数字字符串，1 到 128 位，保留前导零，禁止科学计数法或数值类型。
            name(string): 用户提供的角色名，必填，最多 128 字符。
            server_name(string): 用户提供的区服，必填，最多 128 字符。
            title(string): 本次申请的流程标题，必填，最多 128 字符，例如顶铁人员申请 - 区服 / 角色名。
            description(string): 可选申请说明，最多 2000 字符，默认空字符串，不编造缺失说明。
            upload_attachments(boolean): 默认 false；用户要求把本条消息及直接引用消息中的全部文件或图片作为证据时设为 true。仅选部分附件时保持 false，随后逐个调用上传工具。
        """
        if event is None or not event.is_admin():
            return error_result(
                "PERMISSION_DENIED", "仅 AstrBot 管理员可以提交顶铁人员申请"
            )
        try:
            role_id = validate_role_id(role_id)
            body = {
                "id": role_id,
                "name": text_field(name, "name", 128),
                "server_name": text_field(server_name, "server_name", 128),
                "title": text_field(title, "title", 128),
            }
            description = text_field(description, "description", 2000, required=False)
            if description:
                body["description"] = description
            if type(upload_attachments) is not bool:
                raise ValueError("upload_attachments 必须是布尔值")
            attachments = message_attachments(event) if upload_attachments else []
            if upload_attachments and not attachments:
                raise ValueError(
                    "本条消息及直接引用的消息没有可上传的文件或图片，请发送实际附件"
                )
        except (TypeError, ValueError) as exc:
            return error_result("INVALID_ARGUMENT", str(exc))

        state = write_state(event)
        async with state["submit_lock"]:
            cached = state["submissions"].get(role_id)
            if cached is not None:
                return json.dumps({**cached, "reused_result": True}, ensure_ascii=False)
            result = json.loads(
                await self._request(
                    "POST", "/records", operation="submit", role_id=role_id, json=body
                )
            )
            if remember_write(result):
                state["submissions"][role_id] = result
            if result.get("success") is not True:
                return json.dumps(result, ensure_ascii=False)

            result["message"] = "申请已提交，等待审核；申请中不会出现在公开查询中"
            if attachments:
                uploads = []
                for index in range(1, len(attachments) + 1):
                    uploads.append(
                        json.loads(
                            await self.h42_dingtie_upload_attachment(
                                event, role_id, index
                            )
                        )
                    )
                result["attachments"] = uploads
                result["partial_success"] = any(
                    item.get("success") is not True for item in uploads
                )
                if result["partial_success"]:
                    result["message"] = (
                        "人员申请已提交，部分附件上传失败；请核对失败文件，不要重复新增人员"
                    )
            return json.dumps(result, ensure_ascii=False)

    @filter.llm_tool(name="h42_dingtie_upload_attachment")
    async def h42_dingtie_upload_attachment(
        self,
        event: AstrMessageEvent,
        role_id: str,
        attachment_index: int = 1,
        filename: str = "",
    ) -> str:
        """仅 AstrBot 管理员可向已提交且待审核的申请上传本条消息中的一个实际附件。

        调用权限按当前消息发送者的 AstrBot 管理员身份检查，普通用户不能代管理员上传。
        仅在用户要求上传证据时调用，使用角色编号而不是 record_id、bind_id 或 file_id。
        文件来源只限本条消息及直接引用消息中的文件/图片；不接受本地路径、URL、Base64 或文件 ID。
        请求发送原始字节，单文件非空且不超过 10 MiB；多文件按序逐个调用。
        附件失败不会回滚人员申请，不能因此重复创建人员。
        NOT_FOUND 表示上传目标不存在，ATTACHMENT_CONFLICT 表示同名附件或状态冲突，均不能当作成功。
        outcome_unknown=true 表示保存状态不确定，请管理员核对，禁止自动重试或改名重复上传。
        file_id 只是附件标识，不能拼造下载地址；查询接口不返回图片。

        Args:
            role_id(string): 已提交申请的完整角色编号字符串，保留全部数字和前导零。
            attachment_index(number): 文件或图片在当前消息中的序号，从 1 开始，默认 1；引用消息中的附件在引用段位置展开计数。
            filename(string): 可选上传文件名，默认保留文件原名或为图片生成文件名；最多 128 字符，不允许路径、控制字符或 @@@@。用户明确指定名称时才填写。
        """
        if event is None or not event.is_admin():
            return error_result(
                "PERMISSION_DENIED", "仅 AstrBot 管理员可以上传申请附件"
            )
        try:
            role_id = validate_role_id(role_id)
            attachments = message_attachments(event)
            if type(attachment_index) is not int or not 1 <= attachment_index <= len(
                attachments
            ):
                raise ValueError(
                    f"attachment_index 必须是 1 到 {len(attachments)} 的整数；没有附件时请发送实际文件"
                )
            filename = text_field(filename, "filename", 128, required=False)
            component = attachments[attachment_index - 1]
            if filename or (isinstance(component, File) and component.name):
                filename = validate_filename(filename or component.name)
        except (TypeError, ValueError) as exc:
            return error_result(
                "INVALID_ARGUMENT",
                str(exc),
                role_id=role_id,
                attachment_index=attachment_index,
            )

        state = write_state(event)
        async with state["upload_lock"]:
            submission = state["submissions"].get(role_id, {})
            if submission.get("outcome_unknown"):
                return error_result(
                    "SUBMISSION_UNCERTAIN",
                    "人员申请状态尚未确认，请管理员核对后再上传",
                    role_id=role_id,
                    attachment_index=attachment_index,
                )
            component = attachments[attachment_index - 1]
            # 图片扩展名从框架解析后的文件读取，文件消息保留原名。
            try:
                timeout = self.config.get("upload_timeout_seconds", 60)
                if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                    return error_result(
                        "CONFIG_ERROR", "upload_timeout_seconds 必须在 1 到 120 秒之间"
                    )
                async with asyncio.timeout(timeout):
                    path = (
                        await component.get_file()
                        if isinstance(component, File)
                        else await component.convert_to_file_path()
                    )
                    filename = validate_filename(
                        filename
                        or (component.name if isinstance(component, File) else "")
                        or f"evidence_{attachment_index}{Path(path).suffix or '.bin'}"
                    )
                    key = (role_id, filename)
                    cached = state["uploads"].get(key)
                    if cached is not None:
                        return json.dumps(
                            {**cached, "reused_result": True}, ensure_ascii=False
                        )
                    content = await asyncio.to_thread(read_attachment, path)
            except Exception as exc:  # noqa: BLE001 -- 各平台附件适配器的异常类型不统一
                # 适配器解析失败不代表服务端写入失败；不回显本地路径或下载链接。
                return error_result(
                    "ATTACHMENT_READ_FAILED",
                    "无法读取消息附件或文件名不合法，请检查附件后重新发送",
                    role_id=role_id,
                    attachment_index=attachment_index,
                    file_name=filename,
                    error_type=type(exc).__name__,
                )

            if not content or len(content) > MAX_FILE_BYTES:
                return error_result(
                    "FILE_TOO_LARGE" if content else "INVALID_ARGUMENT",
                    "单个附件必须非空且不能超过 10 MiB",
                    role_id=role_id,
                    attachment_index=attachment_index,
                    file_name=filename,
                )
            names = [
                item["data"]["file_name"]
                for (rid, _), item in state["uploads"].items()
                if rid == role_id and item.get("success") is True
            ]
            if len("@@@@".join([*names, filename]).encode("utf-16-le")) // 2 > 128:
                return error_result(
                    "INVALID_ARGUMENT",
                    "附件文件名合计长度（含 @@@@ 分隔符）不能超过 128",
                    role_id=role_id,
                    attachment_index=attachment_index,
                    file_name=filename,
                )

            with io.BytesIO(content) as content_stream:
                result = json.loads(
                    await self._request(
                        "POST",
                        f"/records/{role_id}/attachments",
                        operation="upload",
                        role_id=role_id,
                        params={"filename": filename},
                        data=content_stream,
                        headers={"Content-Type": "application/octet-stream"},
                    )
                )
            result.update(
                role_id=role_id, attachment_index=attachment_index, file_name=filename
            )
            if result.get("success") is True and (
                result["data"].get("file_name") != filename
                or result["data"].get("file_size") != len(content)
            ):
                result = json.loads(
                    error_result(
                        "INVALID_RESPONSE",
                        "附件响应与上传的文件名或大小不一致，请管理员核对，禁止自动重试",
                        role_id=role_id,
                        attachment_index=attachment_index,
                        file_name=filename,
                        outcome_unknown=True,
                    )
                )
            if result.get("success") is not True:
                result["message"] = (
                    "本次附件未确认上传成功；人员申请不会回滚，请勿因此重复新增人员"
                )
            if remember_write(result):
                state["uploads"][key] = result
            return json.dumps(result, ensure_ascii=False)

    async def _request(
        self,
        method: str,
        path: str,
        *,
        operation: str = "query",
        role_id: str = "",
        **kwargs,
    ) -> str:
        is_write = operation in ("submit", "upload")

        def failure(
            code: str,
            message: str,
            *,
            status: int | None = None,
            uncertain: bool = False,
        ) -> str:
            extra = {"http_status": status} if status is not None else {}
            if is_write:
                extra.update(
                    operation=operation, role_id=role_id, outcome_unknown=uncertain
                )
                if uncertain:
                    message += "；写入状态不确定，请管理员核对，禁止自动重试"
            return error_result(code, message, **extra)

        base_url = self.config.get("api_base_url", "")
        authorization = self.config.get("authorization", "")
        timeout = (
            self.config.get("upload_timeout_seconds", 60)
            if operation == "upload"
            else self.config.get("timeout_seconds", 15)
        )
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
            # 兼容旧配置（以 /accounts 结尾）和交接文档的服务发布基址。
            if not parts.path.rstrip("/").endswith("/accounts"):
                base_url += "/accounts"
            if type(timeout) not in (int, float) or not 0 < timeout <= 120:
                raise ValueError
            if "\r" in authorization or "\n" in authorization:
                raise ValueError
        except (TypeError, ValueError):
            return failure(
                "CONFIG_ERROR",
                "请配置有效的 api_base_url（服务基址或包含 /accounts 的地址，不含查询参数或账号密码）、"
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
                    return failure(
                        "AUTH_ERROR",
                        "接口认证失败，请检查 Authorization 或 API 访问权限",
                        status=status,
                    )
                if 300 <= status < 400:
                    return failure(
                        "HTTP_ERROR",
                        "接口发生重定向，请配置最终的 API 地址",
                        status=status,
                    )
                try:
                    payload = await response.json(content_type=None)
                except (ValueError, UnicodeError):
                    return failure(
                        "FILE_TOO_LARGE" if status == 413 else "INVALID_RESPONSE",
                        "接口拒绝文件大小，单文件不能超过 10 MiB"
                        if status == 413
                        else "接口未返回有效 JSON，请检查 API 地址、后端部署及发布配置",
                        status=status,
                        uncertain=is_write and (200 <= status < 300 or status >= 500),
                    )
        except TimeoutError:
            return failure(
                "TIMEOUT",
                "接口请求超时"
                if is_write
                else "接口请求超时，无法判断是否收录，请稍后重试",
                uncertain=is_write,
            )
        except (aiohttp.ClientError, ValueError):
            return failure(
                "NETWORK_ERROR",
                "接口通信失败，请检查 API 地址、网络及证书",
                uncertain=is_write,
            )

        if isinstance(payload, dict) and payload.get("success") is False:
            error = payload.get("error")
            if (
                isinstance(error, dict)
                and isinstance(error.get("code"), str)
                and isinstance(error.get("message"), str)
                # 只有精确查询接口约定的 404 才能解释为角色未收录。
                and (
                    error["code"] != "NOT_FOUND"
                    or ((method == "GET" or operation == "upload") and status == 404)
                )
            ):
                return failure(
                    error["code"],
                    error["message"],
                    status=status,
                    uncertain=is_write and status >= 500,
                )
        if status >= 400:
            return failure(
                "FILE_TOO_LARGE" if status == 413 else "HTTP_ERROR",
                f"接口返回 HTTP {status}，本次操作未确认成功",
                status=status,
                uncertain=is_write and status >= 500,
            )
        if (
            not isinstance(payload, dict)
            or payload.get("success") is not True
            or not isinstance(payload.get("data"), dict)
        ):
            return failure(
                "INVALID_RESPONSE",
                "接口响应不符合 success/data 协议",
                status=status,
                uncertain=is_write,
            )

        data = payload["data"]
        if is_write:
            valid = (
                status == 201
                and data.get("id") == role_id
                and isinstance(data.get("record_id"), str)
                and bool(data["record_id"])
            )
            if operation == "submit":
                valid = (
                    valid
                    and type(data.get("review_status")) is int
                    and data["review_status"] == 0
                    and isinstance(data.get("bind_id"), str)
                    and bool(data["bind_id"])
                )
            else:
                valid = (
                    valid
                    and isinstance(data.get("file_id"), str)
                    and bool(data["file_id"])
                    and isinstance(data.get("file_name"), str)
                    and type(data.get("file_size")) is int
                    and 0 < data["file_size"] <= MAX_FILE_BYTES
                )
            if not valid:
                return failure(
                    "INVALID_RESPONSE",
                    "提交或上传响应不符合 201 及返回字段约定",
                    status=status,
                    uncertain=True,
                )
            data.pop("images", None)
            return json.dumps(payload, ensure_ascii=False)
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
