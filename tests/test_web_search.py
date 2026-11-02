import importlib
import json
import unittest
from unittest.mock import patch

import httpx

import config
from core import llm, tools
from tools import web_search


class WebSearchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.enterContext(patch.dict(tools._tools))
        self.enterContext(patch.object(config, "DEEPSEEK_SEARCH_API_KEY", "search-test-key"))
        importlib.reload(web_search)

    async def mock_http(self, handler):
        client = await self.enterAsyncContext(httpx.AsyncClient(transport=httpx.MockTransport(handler)))
        self.enterContext(patch.object(llm, "http_client", client))

    async def test_registered_search_sends_only_query_and_count_and_returns_sources(self):
        def handle(request):
            self.assertEqual(request.method, "POST")
            self.assertEqual(str(request.url), "https://api.deepseek.com/v1/tools/search")
            self.assertEqual(request.headers["authorization"], "Bearer search-test-key")
            self.assertEqual(json.loads(request.content), {"query": "更新 site:example.org", "count": 5})
            rows = [
                {"url": "https://example.org/news", "date": "2026-10-30", "title": "公告",
                 "summary": "新" * 2000, "content": "整页正文"},
                {"url": "https://example.org/notes", "snippet": "短摘要", "date": None},
            ]
            return httpx.Response(200, json={"results": rows})

        await self.mock_http(handle)
        raw = await tools.call_tool("web_search", {"query": "  更新 site:example.org  "})
        first, second = json.loads(raw)
        self.assertEqual(first, {"url": "https://example.org/news", "date": "2026-10-30",
                                 "title": "公告", "summary": "新" * 800})
        self.assertEqual(second["summary"], "短摘要")

    async def test_invalid_query_does_not_make_a_request(self):
        await self.mock_http(lambda request: self.fail("无效搜索词发起了请求"))
        for query in (" ", "猫" * 129, None):
            with self.subTest(query=query):
                self.assertIn("搜索词必须", await web_search.web_search(query))

    async def test_empty_results_http_errors_and_timeouts(self):
        def handle(request):
            if status == 0:
                raise httpx.ReadTimeout("timeout")
            return httpx.Response(status, json={"results": []})

        await self.mock_http(handle)
        for status, expected in [(200, "没有找到"), (401, "鉴权失败"), (402, "余额不足"),
                                 (429, "请求过于频繁"), (503, "HTTP 503"), (0, "超时")]:
            with self.subTest(status=status):
                self.assertIn(expected, await web_search.web_search("测试"))

    def test_search_is_not_registered_without_key(self):
        tools._tools.pop("web_search")
        with patch.object(config, "DEEPSEEK_SEARCH_API_KEY", ""):
            importlib.reload(web_search)
        self.assertNotIn("web_search", {s["function"]["name"] for s in tools.get_tool_schemas()})
