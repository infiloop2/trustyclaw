"""Unit tests for public LinkedIn discovery (all provider calls mocked)."""

from __future__ import annotations

import unittest
from typing import Any
from unittest.mock import patch

from host.tools import linkedin_discovery
from host.tools.linkedin_discovery import LinkedInDiscoveryTool
from host.tools.results import ActionExecuted, ActionFailed
from host.tools.shared.web import WebRequestError

from test_tools import FakeHostAPI


def configured_api() -> FakeHostAPI:
    api = FakeHostAPI()
    api.config["SERPAPI_API_KEY"] = "serp-key"
    return api


class LinkedInDiscoveryToolTests(unittest.TestCase):
    def test_manifest_is_separate_read_only_tool(self) -> None:
        manifest = LinkedInDiscoveryTool().manifest
        self.assertEqual(manifest.tool_id, "linkedin_discovery")
        self.assertEqual(manifest.connection, "enable_only")
        self.assertEqual([action.id for action in manifest.actions], ["search_posts"])
        self.assertEqual(manifest.actions[0].approval, "direct")
        self.assertIn("not a LinkedIn feed", manifest.actions[0].description)
        self.assertEqual(len(manifest.setup_steps), 3)
        self.assertEqual(
            [card.title for card in manifest.data_summary.cards],
            [
                "What leaves this host",
                "Where it can go",
                "What SerpApi can do with it",
                "How long SerpApi retains it",
            ],
        )
        policy_urls = {link.url for card in manifest.data_summary.cards for link in card.links}
        self.assertIn("https://serpapi.com/legal", policy_urls)
        self.assertIn("https://policies.google.com/privacy", policy_urls)

    def test_search_scopes_query_and_normalizes_only_linkedin_posts(self) -> None:
        seen: dict[str, Any] = {}

        def fake_json_request(method: str, url: str, **kwargs: Any):
            seen["method"] = method
            seen["url"] = url
            return {
                "organic_results": [
                    {"position": 1, "title": "Agent post", "link": "https://www.linkedin.com/posts/alice_agents-123?utm_source=google#comments", "snippet": "Useful text", "date": "2 days ago", "source": "Alice"},
                    {"position": 2, "title": "Profile", "link": "https://www.linkedin.com/in/alice", "snippet": "drop"},
                    {"position": 3, "title": "Duplicate", "link": "https://linkedin.com/posts/alice_agents-123#comments", "snippet": "drop"},
                    {"position": 4, "title": "Other", "link": "https://example.com/post", "snippet": "drop"},
                ]
            }

        with patch.object(linkedin_discovery, "json_request", fake_json_request):
            result = LinkedInDiscoveryTool().execute("search_posts", {"query": "AI agents", "limit": "5", "page": "2"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(seen["method"], "GET")
        self.assertIn("site%3Alinkedin.com%2Fposts+AI+agents", seen["url"])
        self.assertIn("start=10", seen["url"])
        self.assertIn("api_key=serp-key", seen["url"])
        self.assertEqual(len(result.result["results"]), 1)
        self.assertEqual(result.result["results"][0]["source"], "Alice")
        self.assertEqual(result.result["results"][0]["url"], "https://www.linkedin.com/posts/alice_agents-123")

    def test_rejects_scope_override_and_invalid_pagination(self) -> None:
        tool = LinkedInDiscoveryTool()
        for tool_input in (
            {},
            {"query": " "},
            {"query": "site:example.com agents"},
            {"query": "agents OR site:example.com"},
            {"query": "agents", "limit": "²"},
            {"query": "x" * 501},
            {"query": "agents", "limit": "0"},
            {"query": "agents", "limit": 1.5},
            {"query": "agents", "page": "11"},
        ):
            with self.subTest(tool_input=tool_input):
                self.assertIsInstance(tool.execute("search_posts", tool_input, configured_api()), ActionFailed)

    def test_no_results_error_is_a_valid_empty_result_not_a_failure(self) -> None:
        # SerpApi returns HTTP 200 with this error text when nothing matched; it
        # must not surface as a failure (which would make the agent retry).
        def fake_json_request(method: str, url: str, **kwargs: Any):
            return {"error": "Google hasn't returned any results for this query."}

        with patch.object(linkedin_discovery, "json_request", fake_json_request):
            result = LinkedInDiscoveryTool().execute("search_posts", {"query": "obscure topic"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["results"], [])

    def test_accepts_regional_linkedin_hosts(self) -> None:
        def fake_json_request(method: str, url: str, **kwargs: Any):
            return {"organic_results": [{"position": 1, "title": "UK post", "link": "https://uk.linkedin.com/posts/bob_ai-9", "snippet": "text"}]}

        with patch.object(linkedin_discovery, "json_request", fake_json_request):
            result = LinkedInDiscoveryTool().execute("search_posts", {"query": "ai"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(len(result.result["results"]), 1)
        self.assertEqual(result.result["results"][0]["url"], "https://www.linkedin.com/posts/bob_ai-9")

    def test_drops_non_https_credentialed_or_traversal_result_urls(self) -> None:
        links = (
            "http://www.linkedin.com/posts/alice_ai-1",
            "https://user@www.linkedin.com/posts/alice_ai-1",
            "https://www.linkedin.com:444/posts/alice_ai-1",
            "https://www.linkedin.com/posts/../in/alice",
            "https://www.linkedin.com/posts/%2e%2e/in/alice",
        )
        with patch.object(
            linkedin_discovery,
            "json_request",
            return_value={"organic_results": [{"link": link} for link in links]},
        ):
            result = LinkedInDiscoveryTool().execute(
                "search_posts", {"query": "ai"}, configured_api()
            )
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["results"], [])

    def test_drops_result_urls_with_embedded_controls(self) -> None:
        with patch.object(
            linkedin_discovery,
            "json_request",
            return_value={
                "organic_results": [
                    {"link": "https://evil.example\t.linkedin.com/posts/alice_ai-1"}
                ]
            },
        ):
            result = LinkedInDiscoveryTool().execute("search_posts", {"query": "ai"}, configured_api())
        assert isinstance(result, ActionExecuted)
        self.assertEqual(result.result["results"], [])

    def test_maps_auth_and_credit_errors_without_provider_body(self) -> None:
        def fail(method: str, url: str, **kwargs: Any):
            raise WebRequestError("raw", status=429, body=b"secret provider detail")

        with patch.object(linkedin_discovery, "json_request", fail):
            result = LinkedInDiscoveryTool().execute("search_posts", {"query": "agents"}, configured_api())
        assert isinstance(result, ActionFailed)
        self.assertIn("credits", result.error)
        self.assertNotIn("secret", result.error)

    def test_missing_config_is_operator_actionable(self) -> None:
        result = LinkedInDiscoveryTool().execute("search_posts", {"query": "agents"}, FakeHostAPI())
        assert isinstance(result, ActionFailed)
        self.assertIn("SERPAPI_API_KEY", result.error)


if __name__ == "__main__":
    unittest.main()
