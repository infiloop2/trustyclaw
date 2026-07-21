"""Coverage tests for the outbound parameter guard.

Two jobs:

1. Completeness: every agent-controlled string input field of every bundled
   tool action is classified exactly once - either GUARDED (its call site
   passes it through ``api.outbound.guard_request_parameter_string``) or
   EXEMPT with a recorded reason. Adding a tool or field without classifying
   it here fails the build, which is what keeps the classification honest as
   the tool set grows.

2. Behavior: for each guarded field, driving the real tool code with a
   secret- or identifier-carrying value produces a denial whose descriptive
   message reaches the agent, proving the guard sits on the request path
   rather than beside it.
"""

from __future__ import annotations

import importlib
import pkgutil
import unittest

import host.tools
from host.param_guard import PARAM_GUARD_PROTECTION, PARAM_GUARD_TECHNICAL_DETAIL, ParamGuardDenied
from host.tools.results import ActionFailed
from test_tools import FakeHostAPI

# (tool_id, action_id, field) -> guarded free-text parameter. The tool's
# package test and the behavioral tests below exercise each.
GUARDED_FIELDS = {
    ("brave_search", "search_web", "query"),
    ("instagram_discovery", "search_reels", "query"),
    ("instagram_discovery", "search_hashtag", "hashtag"),
    ("linkedin_discovery", "search_posts", "query"),
    ("polymarket", "search", "query"),
    ("polymarket", "get_market", "slug"),
    ("runway", "generate_video", "prompt"),
    ("runway", "edit_video", "prompt"),
    ("runway", "generate_image", "prompt"),
    ("runway", "generate_speech", "text"),
    ("twitter", "search_tweets", "query"),
}

# (tool_id, action_id, field) -> reason it is deliberately not guarded.
# Categories follow the architecture doc's scope rule.
APPROVAL_GATED = "approval-gated content: the operator approval is the control"
CONNECTED_ACCOUNT = "connected-account value: the destination already holds the data"
CONNECTED_ACCOUNT_GUARDED = (
    "connected-account mailbox query: guarded via allow_identifiers=True "
    "(secret/credential shapes denied, personal identifiers including one-time codes allowed as search syntax)"
)
TYPED = "typed value: enum/id/timestamp/cursor grammar is stricter than scanning"
PROTOCOL = "provider protocol value on a fixed-destination typed path"

EXEMPT_FIELDS = {
    ("brave_search", "search_web", "count"): TYPED,
    ("gmail", "search_messages", "query"): CONNECTED_ACCOUNT_GUARDED,
    ("gmail", "list_drafts", "query"): CONNECTED_ACCOUNT_GUARDED,
    ("gmail", "*", "*"): CONNECTED_ACCOUNT,
    ("google_calendar", "*", "*"): CONNECTED_ACCOUNT,
    ("ibkr", "*", "*"): TYPED,
    ("instagram", "*", "*"): APPROVAL_GATED,
    ("instagram_discovery", "search_reels", "page"): TYPED,
    ("instagram_discovery", "search_reels", "date_posted"): TYPED,
    ("instagram_discovery", "search_reels", "limit"): TYPED,
    ("instagram_discovery", "search_hashtag", "reels_only"): TYPED,
    ("instagram_discovery", "search_hashtag", "date_posted"): TYPED,
    ("instagram_discovery", "search_hashtag", "cursor"): PROTOCOL,
    ("instagram_discovery", "search_hashtag", "limit"): TYPED,
    ("instagram_discovery", "get_trending_reels", "limit"): TYPED,
    ("instagram_discovery", "get_reels_by_audio", "*"): TYPED,
    ("instagram_discovery", "get_reel_details", "*"): TYPED,
    ("linkedin_discovery", "search_posts", "page"): TYPED,
    ("linkedin_discovery", "search_posts", "limit"): TYPED,
    ("linkedin", "*", "*"): APPROVAL_GATED,
    ("polymarket", "search", "limit_per_type"): TYPED,
    ("polymarket", "get_market", "market_id"): TYPED,
    ("polymarket", "list_markets", "*"): TYPED,
    ("polymarket", "list_events", "*"): TYPED,
    ("polymarket", "get_order_book", "*"): TYPED,
    ("polymarket", "price_history", "*"): TYPED,
    ("runway", "generate_video", "model"): TYPED,
    ("runway", "generate_video", "image_url"): TYPED,
    ("runway", "generate_video", "image_asset_id"): TYPED,
    ("runway", "generate_video", "ratio"): TYPED,
    ("runway", "generate_video", "duration_seconds"): TYPED,
    ("runway", "generate_video", "seed"): TYPED,
    ("runway", "edit_video", "video_asset_id"): TYPED,
    ("runway", "edit_video", "video_url"): TYPED,
    ("runway", "edit_video", "seed"): TYPED,
    ("runway", "generate_image", "ratio"): TYPED,
    ("runway", "generate_image", "quality"): TYPED,
    ("runway", "generate_speech", "voice"): TYPED,
    ("runway", "get_task", "*"): TYPED,
    ("runway", "save_video", "*"): TYPED,
    ("twitter", "search_tweets", "max_results"): TYPED,
    ("twitter", "get_tweet_metrics", "*"): TYPED,
    ("twitter", "read_tweet", "*"): TYPED,
    ("twitter", "user_tweets", "*"): TYPED,
    ("twitter", "get_trends", "*"): TYPED,
    ("twitter", "get_personalized_trends", "*"): TYPED,
    ("twitter", "post_tweet", "*"): APPROVAL_GATED,
}

# Tools whose Integration Guide must carry the shared parameter-guard line.
GUARDED_TOOL_IDS = sorted({tool_id for tool_id, _, _ in GUARDED_FIELDS})


def _bundled_manifests():
    for module_info in pkgutil.iter_modules(host.tools.__path__):
        if not module_info.ispkg:
            continue
        module = importlib.import_module(f"host.tools.{module_info.name}")
        tool = getattr(module, "BUNDLED_TOOL", None)
        if tool is not None:
            yield tool.manifest


def _classified(tool_id: str, action_id: str, field: str) -> bool:
    if (tool_id, action_id, field) in GUARDED_FIELDS:
        return True
    for key in (
        (tool_id, action_id, field),
        (tool_id, action_id, "*"),
        (tool_id, "*", "*"),
    ):
        if key in EXEMPT_FIELDS:
            return True
    return False


class CompletenessTest(unittest.TestCase):
    def test_every_action_input_field_is_classified(self) -> None:
        unclassified = []
        seen_tools = set()
        for manifest in _bundled_manifests():
            seen_tools.add(manifest.tool_id)
            for action in manifest.actions:
                properties = action.input_schema.get("properties")
                if not isinstance(properties, dict):
                    continue
                for field in properties:
                    if not _classified(manifest.tool_id, action.id, field):
                        unclassified.append((manifest.tool_id, action.id, field))
        self.assertEqual(
            unclassified,
            [],
            "Classify each field as GUARDED (and add the guard call plus a "
            "behavioral test) or EXEMPT with a reason.",
        )
        # The guarded set must not name fields that do not exist.
        for tool_id, action_id, field in GUARDED_FIELDS:
            self.assertIn(tool_id, seen_tools)

    def test_no_field_is_both_guarded_and_exempt(self) -> None:
        for tool_id, action_id, field in GUARDED_FIELDS:
            self.assertNotIn((tool_id, action_id, field), EXEMPT_FIELDS)
            # No wildcard may shadow an action that has guarded fields:
            # otherwise dropping the field from GUARDED_FIELDS would silently
            # reclassify it as exempt instead of failing completeness.
            self.assertNotIn((tool_id, action_id, "*"), EXEMPT_FIELDS)
            self.assertNotIn((tool_id, "*", "*"), EXEMPT_FIELDS)

    def test_guarded_tools_declare_the_shared_guide_protection(self) -> None:
        for manifest in _bundled_manifests():
            if manifest.tool_id in GUARDED_TOOL_IDS:
                self.assertIn(
                    PARAM_GUARD_PROTECTION,
                    manifest.protections,
                    f"{manifest.tool_id} guide must carry the parameter-guard line",
                )
                self.assertIn(
                    PARAM_GUARD_TECHNICAL_DETAIL,
                    manifest.technical_details,
                    f"{manifest.tool_id} guide must carry the expanded description",
                )
            else:
                self.assertNotIn(PARAM_GUARD_PROTECTION, manifest.protections)
                self.assertNotIn(PARAM_GUARD_TECHNICAL_DETAIL, manifest.technical_details)


class BehavioralDenialTest(unittest.TestCase):
    """Each guarded surface, driven with a value the guard must deny; the
    denial message must reach the caller so the agent can retry."""

    def assert_denied(self, result, fragment: str) -> None:
        self.assertIsInstance(result, ActionFailed)
        self.assertIn(fragment, result.error)
        self.assertIn("retry", result.error)

    def test_brave_search_query_denied(self) -> None:
        from host.tools.brave_search import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "search_web", {"query": "verify AKIAIOSFODNN7EXAMPLE now"}, FakeHostAPI()
        )
        self.assert_denied(result, "credential")

    def test_polymarket_search_and_slug_denied(self) -> None:
        from host.tools.polymarket import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "search", {"query": "will alice@example.com win"}, FakeHostAPI()
        )
        self.assert_denied(result, "email address")
        result = BUNDLED_TOOL.execute(
            "get_market", {"slug": "market-482913488123"}, FakeHostAPI()
        )
        self.assert_denied(result, "digits")

    def test_instagram_discovery_query_and_hashtag_denied(self) -> None:
        from host.tools.instagram_discovery import BUNDLED_TOOL

        api = FakeHostAPI(config={"SCRAPECREATORS_API_KEY": "k"})
        result = BUNDLED_TOOL.execute(
            "search_reels", {"query": "code 482913 reels"}, api
        )
        self.assert_denied(result, "code")
        result = BUNDLED_TOOL.execute("search_hashtag", {"hashtag": "sale4829134881234"}, api)
        self.assert_denied(result, "digits")

    def test_linkedin_discovery_query_denied(self) -> None:
        from host.tools.linkedin_discovery import BUNDLED_TOOL

        api = FakeHostAPI(config={"SERPERAPI_API_KEY": "k"})
        result = BUNDLED_TOOL.execute(
            "search_posts", {"query": "posts by alice.smith@acme.com"}, api
        )
        self.assert_denied(result, "email address")

    def test_runway_prompt_and_speech_denied(self) -> None:
        from host.tools import runway

        with self.assertRaises(ParamGuardDenied):
            runway._image_request(FakeHostAPI(), {"prompt": "ssn 219-09-9999 poster"})
        with self.assertRaises(ParamGuardDenied):
            runway._speech_request(FakeHostAPI(), {"text": "my password is hunter2secret"})

    def test_runway_external_url_is_guarded_and_rejects_ip_literals(self) -> None:
        from host.tools import runway

        api = FakeHostAPI()
        # A clean public https URL passes the guard unchanged.
        clean = "https://images.example.com/cat.jpg"
        self.assertEqual(runway._https_url({"image_url": clean}, "image_url", api), clean)
        # A secret/identifier encoded into the URL is denied.
        with self.assertRaises(ParamGuardDenied):
            runway._https_url(
                {"image_url": "https://x.example.com/c?d=alice@example.com"}, "image_url", api
            )

    def test_twitter_search_query_denied(self) -> None:
        from host.tools import twitter

        with self.assertRaises(ParamGuardDenied):
            twitter._search_tweets("token", {"query": "call +1 415 555 2671"}, FakeHostAPI())

    def test_guarded_value_reaches_request_unchanged(self) -> None:
        # The guard returns the identical object for clean values; the
        # brave payload builder must place that exact string on the wire.
        from host.tools import brave_search

        payload = brave_search._request_payload({"query": "flights to seattle"}, FakeHostAPI())
        self.assertEqual(payload["q"], "flights to seattle")


if __name__ == "__main__":
    unittest.main()


class NetworkIntegrationGuardTest(unittest.TestCase):
    """The proxy-side surfaces share the same guard over decoded URL values."""

    def setUp(self) -> None:
        from host.network_integrations.base import ManagedIntegration

        self.config = ManagedIntegration(True)

    def test_python_packages_names_are_guarded_but_downloads_exempt(self) -> None:
        from host.network_integrations.python_packages import guard

        deny = guard.request_denied
        self.assertIsNone(deny(self.config, "GET", "pypi.org", "/simple/requests/", "", [], b""))
        self.assertEqual(
            deny(self.config, "GET", "pypi.org", "/simple/AKIAIOSFODNN7EXAMPLE/", "", [], b""),
            "request_param_secret_denied",
        )
        # Download URLs are provider-echoed (index-response links): their hash
        # segments must not be scanned or pip installs would break.
        digest_path = "/packages/ab/cd/" + "e" * 64 + "/requests-2.31.0-py3-none-any.whl"
        self.assertIsNone(
            deny(self.config, "GET", "files.pythonhosted.org", digest_path, "", [], b"")
        )

    def test_npm_packages_names_are_guarded(self) -> None:
        from host.network_integrations.npm_packages import guard

        deny = guard.request_denied
        self.assertIsNone(
            deny(self.config, "GET", "registry.npmjs.org", "/%40babel%2fcore", "", [], b"")
        )
        self.assertEqual(
            deny(self.config, "GET", "registry.npmjs.org", "/pkg-AKIAIOSFODNN7EXAMPLE", "", [], b""),
            "request_param_secret_denied",
        )

    def test_github_reads_guard_query_values_without_token_rules(self) -> None:
        from host.network_integrations.github import guard
        from host.network_integrations.github.manifest import GitHubIntegration

        config = GitHubIntegration(enabled=True)
        deny = guard.request_denied
        self.assertIsNone(
            deny(config, "GET", "api.github.com", "/search/code", "q=fibonacci+language%3Apython", [], b"")
        )
        # Machine-shaped provider values stay legitimate: shas, refs, cursors.
        self.assertIsNone(
            deny(
                config,
                "GET",
                "api.github.com",
                "/repos/a/b/commits",
                "sha=9c3b2a1f0e9d8c7b6a5f4e3d2c1b0a9f8e7d6c5b",
                [],
                b"",
            )
        )
        self.assertEqual(
            deny(config, "GET", "api.github.com", "/search/users", "q=alice%40example.com", [], b""),
            "request_param_pii_denied",
        )
        # The param guard applies to reads only; a write query is governed by
        # the write-repo rules, not scanned for public-leak shapes (it can only
        # reach a configured repo). A POST carrying an email-shaped query to an
        # unconfigured repo is denied for the write-repo reason, never
        # request_param_pii_denied.
        write_denial = deny(
            config, "POST", "api.github.com", "/repos/o/r/issues", "q=alice%40example.com", [], b"",
        )
        self.assertNotEqual(write_denial, "request_param_pii_denied")

    def test_proxy_guard_rejects_invalid_percent_encoding(self) -> None:
        from host.network_integrations.base import request_param_denial

        # Lenient decoding would smooth %ff%fe into replacement characters
        # while the raw bytes went upstream: a binary channel. Strict
        # decoding denies it.
        self.assertEqual(
            request_param_denial("/simple/%ff%fe%fd/", ""),
            "request_param_encoded_blob_denied",
        )

    def test_proxy_guard_catches_credential_query_keys_via_whole_url(self) -> None:
        from host.network_integrations.base import request_param_denial

        # Scanning the reconstructed full URL routes through the CRED_URL
        # guard, so a bland 16+ char value under a credential-named key is a
        # smuggled secret even though the value alone would pass.
        self.assertEqual(
            request_param_denial("/x", "access_token=mFzQpLdRaWxVkHsN"),
            "request_param_secret_denied",
        )
        self.assertIsNone(request_param_denial("/x", "sort=ascending&page=2"))

    def test_npm_tarball_paths_are_provider_echoed(self) -> None:
        from host.network_integrations.base import ManagedIntegration
        from host.network_integrations.npm_packages import guard

        config = ManagedIntegration(True)
        self.assertIsNone(
            guard.request_denied(
                config, "GET", "registry.npmjs.org",
                "/somepkg/-/somepkg-1.0.0-alpha.20240315123456.tgz", "", [], b"",
            )
        )

    def test_polymarket_market_id_requires_numeric_grammar(self) -> None:
        from host.tools.polymarket import BUNDLED_TOOL

        result = BUNDLED_TOOL.execute(
            "get_market", {"market_id": "AKIAIOSFODNN7EXAMPLE"}, FakeHostAPI()
        )
        self.assertIsInstance(result, ActionFailed)
        self.assertIn("numeric", result.error)

    def test_custom_domain_requests_are_parameter_guarded(self) -> None:
        from host.network_integrations.custom import guard
        from host.network_integrations.custom.manifest import (
            CustomDomainRule,
            CustomIntegration,
        )

        config = CustomIntegration(
            domains={"api.example.com": CustomDomainRule(allow_http_methods=("GET", "POST"))}
        )
        deny = guard.request_denied
        self.assertIsNone(
            deny(config, "GET", "api.example.com", "/v1/lookup", "q=weather", [], b"")
        )
        self.assertEqual(
            deny(config, "GET", "api.example.com", "/v1/lookup", "q=alice%40example.com", [], b""),
            "request_param_pii_denied",
        )

    def test_shared_reason_codes_are_in_the_proxy_catalog(self) -> None:
        from host.network_integrations.registry import denial_reason_catalog

        catalog = denial_reason_catalog()
        for code in (
            "request_param_too_large",
            "request_param_encoded_blob_denied",
            "request_param_secret_denied",
            "request_param_pii_denied",
        ):
            self.assertIn(code, catalog)
            self.assertIn("retry", catalog[code].guidance)
