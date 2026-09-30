from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import unittest
from unittest.mock import patch


MODULE_PATH = Path(__file__).with_name("plugin.py")
SPEC = importlib.util.spec_from_file_location("gigasearch_plugin", MODULE_PATH)
plugin = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(plugin)


class _Response:
    def __init__(self, payload: dict):
        self.payload = json.dumps(payload).encode()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def read(self, _limit: int):
        return self.payload


class GigaSearchTests(unittest.TestCase):
    def test_normalises_results_dates_and_model_summary(self):
        payload = {
            "answer": "Пересказ ответа",
            "data": {
                "items": [
                    {
                        "name": "Источник",
                        "link": "https://example.test/article",
                        "content": "Фрагмент",
                        "published_date": "2026-09-30",
                    }
                ]
            },
        }
        result = plugin._normalise_response("запрос", payload)
        self.assertEqual(result["status"], "ok")
        self.assertEqual(result["summary_kind"], "model_generated")
        self.assertEqual(result["results"][0]["published_at"], "2026-09-30")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/article")

    def test_empty_results_are_not_an_error(self):
        result = plugin._normalise_response("ничего", {"results": []})
        self.assertEqual(result, {"status": "empty", "query": "ничего", "results": [], "count": 0})

    def test_service_error_is_not_reported_as_empty(self):
        result = plugin._normalise_response("запрос", {"status": "error", "message": "quota exceeded"})
        self.assertEqual(result["status"], "error")
        self.assertIn("quota exceeded", result["error"])

    def test_citations_are_preserved_for_model_summary(self):
        result = plugin._normalise_response(
            "запрос",
            {
                "summary": "Пересказ",
                "citations": [{"title": "Источник", "url": "https://example.test/source"}],
            },
        )
        self.assertEqual(result["summary_kind"], "model_generated")
        self.assertEqual(result["sources"][0]["url"], "https://example.test/source")

    @patch.object(plugin.urllib.request, "urlopen")
    def test_posts_configured_shape_and_bearer_key(self, urlopen):
        urlopen.return_value = _Response(
            {"results": [{"title": "T", "url": "https://example.test", "snippet": "S"}]}
        )
        result = plugin._search("test", 3, "https://search.example.test/v1/search", "secret")
        self.assertEqual(result["status"], "ok")
        request = urlopen.call_args.args[0]
        self.assertEqual(json.loads(request.data), {"query": "test", "limit": 3})
        self.assertEqual(request.get_header("Authorization"), "Bearer secret")

    def test_registers_preferred_search_description(self):
        class API:
            def __init__(self):
                self.tool = None

            def register_tool(self, **tool):
                self.tool = tool

            def log(self, *_args):
                pass

        api = API()
        plugin.register(api)
        required = (
            "Основной веб-поиск этой установки. Для обычного поиска внешней "
            "информации сначала используй GigaSearch."
        )
        self.assertEqual(api.tool["name"], "gigasearch_search")
        self.assertTrue(api.tool["description"].startswith(required))
        self.assertLessEqual(len(required), 120)

    def test_configuration_errors_are_explicit(self):
        self.assertEqual(plugin._search("q", 5, "http://example.test", "key")["status"], "error")
        self.assertEqual(plugin._search("q", 5, "https://example.test", "")["status"], "error")


if __name__ == "__main__":
    unittest.main()
