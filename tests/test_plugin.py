"""使用真实本地 HTTP 请求验证接口契约，不依赖安装 AstrBot 本体。"""

import asyncio
import importlib.util
import json
import sys
import types
import unittest
from pathlib import Path
from unittest.mock import patch

import aiohttp
from aiohttp import web
from aiohttp.test_utils import TestServer


def load_plugin():
    modules = {
        name: types.ModuleType(name)
        for name in ("astrbot", "astrbot.api", "astrbot.api.event", "astrbot.api.star")
    }

    class Star:
        def __init__(self, context):
            self.context = context

    def llm_tool(name):
        def decorator(function):
            function.tool_name = name
            return function

        return decorator

    modules["astrbot.api"].AstrBotConfig = dict
    modules["astrbot.api.event"].AstrMessageEvent = object
    modules["astrbot.api.event"].filter = types.SimpleNamespace(llm_tool=llm_tool)
    modules["astrbot.api.star"].Context = object
    modules["astrbot.api.star"].Star = Star
    spec = importlib.util.spec_from_file_location(
        "h42_plugin_under_test", Path(__file__).resolve().parents[1] / "main.py"
    )
    module = importlib.util.module_from_spec(spec)
    with patch.dict(sys.modules, modules):
        spec.loader.exec_module(module)
    return module


plugin_module = load_plugin()
ROLE_ID = "00123456789012345678901234567890"


class PluginTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.requests = []
        self.status = 200
        self.raw_body = None
        self.delay = 0
        self.record = {
            "record_id": "record-1",
            "id": ROLE_ID,
            "name": "测试角色",
            "server_name": "测试区服",
            "description": "接口原始描述",
            "high_risk": True,
            "review_status": 1,
            "inclusion_time": "2026-10-27 16:00:00",
            "images": ["/r/df?file=proof", "https://images.example.com/proof.png"],
            "match_type": "exact",
            "score": 1.0,
        }
        self.payload = {"success": True, "data": self.record}
        app = web.Application()
        app.router.add_route("*", "/{path:.*}", self.handle)
        self.server = TestServer(app)
        await self.server.start_server()
        self.base_url = str(self.server.make_url("/published/accounts"))
        self.plugin = plugin_module.H42DingtieSearchPlugin(
            None, {"api_base_url": self.base_url, "timeout_seconds": 5}
        )

    async def asyncTearDown(self):
        await self.server.close()

    async def handle(self, request):
        body = await request.json() if request.method == "POST" else None
        self.requests.append(
            {
                "method": request.method,
                "path": request.path,
                "params": dict(request.query),
                "authorization": request.headers.get("Authorization"),
                "body": body,
            }
        )
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.raw_body is not None:
            return web.Response(text=self.raw_body, status=self.status)
        return web.json_response(self.payload, status=self.status)

    async def get(self, role_id=ROLE_ID, **kwargs):
        return json.loads(await self.plugin.h42_dingtie_get(None, role_id, **kwargs))

    async def search(self, query="角色名", **kwargs):
        return json.loads(await self.plugin.h42_dingtie_search(None, query, **kwargs))

    def search_payload(self, items, total, offset=0, has_more=False):
        self.payload = {
            "success": True,
            "data": {
                "query": "角色名",
                "match_type": "keyword",
                "requested_ids": [],
                "items": items,
                "total": total,
                "limit": 10,
                "offset": offset,
                "has_more": has_more,
            },
        }

    async def test_exact_id_preserves_digits_and_record_fields(self):
        result = await self.get()
        self.assertTrue(result["success"])
        expected = dict(self.record)
        expected.pop("images")
        self.assertEqual(result["data"], expected)
        self.assertNotIn("images", result["data"])
        self.assertEqual(
            self.requests[0]["path"], "/published/accounts/records/" + ROLE_ID
        )
        self.assertEqual(self.requests[0]["params"], {})
        self.assertEqual(self.requests[0]["method"], "GET")

    async def test_exact_filters_auth_and_trailing_slash(self):
        self.plugin.config.update(
            authorization="Basic test-credential", api_base_url=self.base_url + "/"
        )
        result = await self.get(
            " " + ROLE_ID + " ", server_name=" 测试区服 ", high_risk=0
        )
        self.assertTrue(result["success"])
        self.assertEqual(
            self.requests[0]["params"], {"server_name": "测试区服", "high_risk": "0"}
        )
        self.assertEqual(self.requests[0]["authorization"], "Basic test-credential")
        self.assertNotIn("test-credential", json.dumps(result))

    async def test_search_posts_json_preserves_batch_ids_and_pagination(self):
        query = ROLE_ID + " 998877665544332211"
        self.search_payload([self.record], 21, has_more=True)
        self.payload["data"].update(
            query=query, requested_ids=query.split(), match_type="exact"
        )
        result = await self.search(
            query, limit=20, offset=1, server_name="测试区服", high_risk=1
        )
        self.assertTrue(result["data"]["has_more"])
        self.assertEqual(result["data"]["requested_ids"], query.split())
        self.assertNotIn("images", result["data"]["items"][0])
        self.assertEqual(self.requests[0]["method"], "POST")
        self.assertEqual(self.requests[0]["path"], "/published/accounts/search")
        self.assertEqual(
            self.requests[0]["body"],
            {
                "query": query,
                "limit": 20,
                "offset": 1,
                "server_name": "测试区服",
                "high_risk": 1,
            },
        )

    async def test_keyword_search_defaults_omit_filters(self):
        self.search_payload([self.record], 1)
        result = await self.search("  测试 角色  ")
        self.assertTrue(result["success"])
        self.assertEqual(
            self.requests[0]["body"], {"query": "测试 角色", "limit": 10, "offset": 0}
        )

    async def test_empty_page_preserves_total(self):
        for total, offset in ((0, 0), (1, 100)):
            with self.subTest(total=total):
                self.search_payload([], total, offset)
                result = await self.search(offset=offset)
                self.assertTrue(result["success"])
                self.assertEqual(result["data"]["total"], total)
                self.assertEqual(result["data"]["items"], [])

    async def test_only_backend_record_not_found_is_not_found(self):
        self.status = 404
        self.payload = {
            "success": False,
            "error": {"code": "NOT_FOUND", "message": "未找到该角色编号的已通过记录"},
        }
        result = await self.get()
        self.assertEqual(result["error"]["code"], "NOT_FOUND")
        self.assertEqual(result["http_status"], 404)
        result = await self.search()
        self.assertEqual(result["error"]["code"], "HTTP_ERROR")
        self.raw_body = "<html>404: missing route</html>"
        result = await self.get()
        self.assertEqual(result["error"]["code"], "INVALID_RESPONSE")

    async def test_http_and_business_errors_are_not_empty_results(self):
        for status, code in ((400, "INVALID_ARGUMENT"), (500, "QUERY_FAILED")):
            with self.subTest(status=status):
                self.status = status
                self.payload = {
                    "success": False,
                    "error": {"code": code, "message": "后端错误"},
                }
                result = await self.search()
                self.assertFalse(result["success"])
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("data", result)
        self.status, self.payload = 502, {"message": "gateway error"}
        self.assertEqual((await self.get())["error"]["code"], "HTTP_ERROR")

    async def test_auth_failure_and_redirect_are_not_followed(self):
        self.raw_body = "<html>login</html>"
        for status, code in (
            (401, "AUTH_ERROR"),
            (403, "AUTH_ERROR"),
            (302, "HTTP_ERROR"),
        ):
            with self.subTest(status=status):
                self.status = status
                result = await self.get()
                self.assertEqual(result["error"]["code"], code)
                self.assertNotIn("<html>", json.dumps(result))
        self.assertEqual(len(self.requests), 3)

    async def test_invalid_responses_and_numeric_ids_are_rejected(self):
        for payload in (
            [],
            {"success": True},
            {"success": "true", "data": self.record},
            {"success": True, "data": {"id": int(ROLE_ID)}},
            {"success": True, "data": {"id": "123456"}},
        ):
            with self.subTest(payload=payload):
                self.payload = payload
                self.assertEqual(
                    (await self.get())["error"]["code"], "INVALID_RESPONSE"
                )
        for items, total in ((None, 0), ([], True), ([], -1), ([{"id": 123}], 1)):
            self.search_payload(items, total)
            self.assertEqual((await self.search())["error"]["code"], "INVALID_RESPONSE")
        self.raw_body = "not JSON"
        self.assertEqual((await self.get())["error"]["code"], "INVALID_RESPONSE")

    async def test_invalid_inputs_never_make_requests(self):
        for role_id in (
            123456,
            1.23e18,
            "1.23E+18",
            "",
            "123.0",
            "１２３",
            "1" * 129,
            "../search",
        ):
            with self.subTest(role_id=role_id):
                self.assertEqual(
                    (await self.get(role_id))["error"]["code"], "INVALID_ARGUMENT"
                )
        for query in (123456, "", "x" * 1025, "编号 1.234e+18", ".5e9"):
            with self.subTest(query=query):
                self.assertEqual(
                    (await self.search(query))["error"]["code"], "INVALID_ARGUMENT"
                )
        for kwargs in (
            {"limit": 0},
            {"limit": 101},
            {"limit": True},
            {"limit": 1.5},
            {"offset": -1},
            {"offset": 2147483648},
            {"high_risk": 2},
            {"high_risk": False},
            {"server_name": None},
            {"server_name": "x" * 129},
        ):
            with self.subTest(kwargs=kwargs):
                self.assertEqual(
                    (await self.search(**kwargs))["error"]["code"], "INVALID_ARGUMENT"
                )
        self.assertEqual(self.requests, [])

    async def test_bad_config_is_reported(self):
        for url in (
            "",
            "ftp://example.com/accounts",
            "https://user:password@example.com/accounts",
            self.base_url + "?sid=secret",
        ):
            with self.subTest(url=url):
                self.plugin.config["api_base_url"] = url
                self.assertEqual((await self.get())["error"]["code"], "CONFIG_ERROR")
        self.plugin.config["api_base_url"] = self.base_url
        self.plugin.config["timeout_seconds"] = 0
        self.assertEqual((await self.get())["error"]["code"], "CONFIG_ERROR")
        self.assertEqual(self.requests, [])

    async def test_timeout_and_connection_failure_do_not_report_not_found(self):
        self.delay = 0.05
        self.plugin.config["timeout_seconds"] = 0.01
        result = await self.get()
        self.assertEqual(result["error"]["code"], "TIMEOUT")
        with patch.object(
            aiohttp.ClientSession,
            "request",
            side_effect=aiohttp.ClientConnectionError("secret"),
        ):
            result = await self.get()
        self.assertEqual(result["error"]["code"], "NETWORK_ERROR")
        self.assertNotIn("secret", json.dumps(result))


if __name__ == "__main__":
    unittest.main()
