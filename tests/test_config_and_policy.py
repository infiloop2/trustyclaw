from __future__ import annotations

import io
import os
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from host.config import (
    ConfigError,
    expand_network_controls,
    parse_input_config,
    parse_network_controls,
)
from host.deploy import _subnet_has_public_ipv4_route
from host.runtime.network_policy import (
    anthropic_request_denied,
    decide_http_request,
    find_domain_rule,
    host_allowed,
    openai_request_denied,
)
from host.runtime.network_proxy import read_request_head
from host.runtime.state import save_proxy_claude_account, save_proxy_openai_account_id


class ConfigTests(unittest.TestCase):
    def test_valid_config(self) -> None:
        config = parse_input_config(
            {
                "agent_name": "trustyclaw-dev_1",
                "aws_region": "us-east-1",
                "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
                "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
                "ssh_public_key": "ssh-ed25519 AAAATEST",
                "network_controls": {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": {"openai": True, "claude": True},
                    "allowed_network_access": {
                        "api.github.com": {
                            "allow_http_methods": ["GET", "HEAD"],
                            "path_guards": ["^/repos/[^/]+/[^/]+(?:\\?.*)?$"],
                        }
                    },
                },
            }
        )

        self.assertEqual(config.agent_name, "trustyclaw-dev_1")
        self.assertEqual(config.network_controls.allowed_network_access["api.github.com"].allow_http_methods, ("GET", "HEAD"))

    def test_input_config_requires_ssh_access_and_allows_disabled_managed_providers(self) -> None:
        base = {
            "agent_name": "trustyclaw-dev_1",
            "aws_region": "us-east-1",
            "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
            "ssh_public_key": "ssh-ed25519 AAAATEST",
            "network_controls": {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": True, "claude": True},
                "allowed_network_access": {},
            },
        }

        invalid = dict(base)
        invalid["network_controls"] = {
            "ssh_port_opened": False,
            "managed_ai_provider_network_access": {"openai": True, "claude": True},
            "allowed_network_access": {},
        }
        with self.assertRaisesRegex(ConfigError, "ssh_port_opened must be true"):
            parse_input_config(invalid)

        for managed in (
            {},
            {"openai": False, "claude": False},
            {"openai": True},
            {"claude": True},
            {"openai": False, "claude": True},
            {"openai": True, "claude": False},
        ):
            with self.subTest(managed=managed):
                config = parse_input_config(
                    {
                        **base,
                        "network_controls": {
                            "ssh_port_opened": True,
                            "managed_ai_provider_network_access": managed,
                            "allowed_network_access": {},
                        },
                    }
                )
                self.assertEqual(config.network_controls.managed_ai_provider_network_access.to_json(), {
                    key: value for key, value in managed.items() if value
                })

    def test_agent_name_restrictions(self) -> None:
        with self.assertRaises(ConfigError):
            parse_network_controls({"ssh_port_opened": True, "managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {"*": {}}})

    def test_managed_providers_are_independently_optional(self) -> None:
        for controls in (
            {"ssh_port_opened": True, "allowed_network_access": {}},
            {"ssh_port_opened": True, "managed_ai_provider_network_access": {}, "allowed_network_access": {}},
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": False, "claude": False},
                "allowed_network_access": {},
            },
            {"ssh_port_opened": True, "managed_ai_provider_network_access": {"openai": True}, "allowed_network_access": {}},
            {"ssh_port_opened": True, "managed_ai_provider_network_access": {"claude": True}, "allowed_network_access": {}},
        ):
            with self.subTest(controls=controls):
                parsed = parse_network_controls(controls)
                self.assertIsInstance(parsed.managed_ai_provider_network_access.openai, bool)
                self.assertIsInstance(parsed.managed_ai_provider_network_access.claude, bool)

        disabled = parse_network_controls({"ssh_port_opened": True, "allowed_network_access": {}})
        self.assertEqual(disabled.to_json()["managed_ai_provider_network_access"], {})
        self.assertEqual(expand_network_controls(disabled)["allowed_network_access"], {})

    def test_parse_preserves_user_policy_and_expansion_adds_managed_domain_rules(self) -> None:
        controls = parse_network_controls(
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": True},
                "allowed_network_access": {},
            }
        )
        user_policy = controls.to_json()
        self.assertEqual(user_policy["managed_ai_provider_network_access"], {"openai": True})
        self.assertEqual(user_policy["allowed_network_access"], {})

        policy = expand_network_controls(controls)
        rules = policy["allowed_network_access"]
        self.assertEqual(rules["api.openai.com"]["allow_http_methods"], ["POST"])
        self.assertTrue(rules["api.openai.com"]["openai_account_guard"])
        self.assertTrue(rules["api.openai.com"]["openai_disable_live_web_search"])
        self.assertEqual(rules["auth.openai.com"]["allow_http_methods"], ["GET", "POST"])
        self.assertNotIn("openai_account_guard", rules["auth.openai.com"])
        self.assertNotIn("openai_disable_live_web_search", rules["auth.openai.com"])
        self.assertEqual(rules["chatgpt.com"]["allow_http_methods"], ["GET", "POST"])
        self.assertTrue(rules["chatgpt.com"]["openai_account_guard"])
        self.assertTrue(rules["chatgpt.com"]["openai_disable_live_web_search"])

    def test_openai_domains_are_managed_by_ai_provider(self) -> None:
        for domain in ("api.openai.com", "auth.openai.com", "chatgpt.com", "*.chatgpt.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConfigError, "managed_ai_provider_network_access.openai"):
                parse_network_controls(
                    {
                        "ssh_port_opened": True,
                        "managed_ai_provider_network_access": {"openai": True},
                        "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                    }
                )

        for field in ("openai_disable_live_web_search", "openai_account_guard"):
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, "unsupported fields"):
                parse_network_controls(
                    {
                        "ssh_port_opened": True,
                        "managed_ai_provider_network_access": {"openai": True},
                        "allowed_network_access": {"github.com": {"allow_http_methods": ["GET"], field: True}},
                    }
                )

    def test_claude_provider_expands_and_rejects_managed_domains(self) -> None:
        controls = parse_network_controls(
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"claude": True},
                "allowed_network_access": {},
            }
        )
        self.assertEqual(controls.to_json()["managed_ai_provider_network_access"], {"claude": True})
        rules = expand_network_controls(controls)["allowed_network_access"]
        self.assertTrue(rules["api.anthropic.com"]["anthropic_account_guard"])
        self.assertNotIn("claude.ai", rules)
        self.assertEqual(rules["platform.claude.com"]["allow_http_methods"], ["GET", "POST"])
        self.assertEqual(rules["platform.claude.com"]["path_guards"], ["^/v1/oauth(?:/.*)?$"])

        for domain in ("api.anthropic.com", "claude.ai", "platform.claude.com", "*.anthropic.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(
                ConfigError, "managed_ai_provider_network_access.claude"
            ):
                parse_network_controls(
                    {
                        "ssh_port_opened": True,
                        "managed_ai_provider_network_access": {"claude": True},
                        "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                    }
                )

    def test_overlapping_wildcard_domain_rules_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "wildcard domains must not overlap"):
            parse_network_controls(
                {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": {"openai": True},
                    "allowed_network_access": {
                        "*.example.com": {"allow_http_methods": ["GET"]},
                        "*.api.example.com": {"allow_http_methods": ["POST"]},
                    },
                }
            )

    def test_non_overlapping_wildcard_domain_rules_are_allowed(self) -> None:
        controls = parse_network_controls(
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": True},
                "allowed_network_access": {
                    "*.api.example.com": {"allow_http_methods": ["GET"]},
                    "*.static.example.com": {"allow_http_methods": ["POST"]},
                },
            }
        )

        self.assertEqual(controls.allowed_network_access["*.api.example.com"].allow_http_methods, ("GET",))
        self.assertEqual(controls.allowed_network_access["*.static.example.com"].allow_http_methods, ("POST",))

    def test_exact_domain_override_under_wildcard_is_allowed(self) -> None:
        controls = parse_network_controls(
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": True},
                "allowed_network_access": {
                    "*.example.com": {"allow_http_methods": ["GET"]},
                    "api.example.com": {"allow_http_methods": ["POST"]},
                },
            }
        )

        self.assertEqual(controls.allowed_network_access["api.example.com"].allow_http_methods, ("POST",))

    def test_domain_keys_are_normalized_and_case_duplicates_rejected(self) -> None:
        controls = parse_network_controls(
            {
                "ssh_port_opened": True,
                "managed_ai_provider_network_access": {"openai": True},
                "allowed_network_access": {
                    "API.Example.COM": {"allow_http_methods": ["GET"]},
                },
            }
        )
        self.assertIn("api.example.com", controls.allowed_network_access)
        self.assertNotIn("API.Example.COM", controls.allowed_network_access)

        with self.assertRaisesRegex(ConfigError, "duplicate domain rules"):
            parse_network_controls(
                {
                    "ssh_port_opened": True,
                    "managed_ai_provider_network_access": {"openai": True},
                    "allowed_network_access": {
                        "api.example.com": {"allow_http_methods": ["GET"]},
                        "API.EXAMPLE.COM": {"allow_http_methods": ["POST"]},
                    },
                }
            )


class PolicyTests(unittest.TestCase):
    def test_policy_matches_domain_method_and_path(self) -> None:
        policy = {
            "ssh_port_opened": True,
            "allowed_network_access": {
                "*.example.com": {
                    "allow_http_methods": ["GET"],
                    "path_guards": ["^/dist(?:/.*)?$"],
                }
            },
        }

        self.assertTrue(decide_http_request(policy, "https", "GET", "cdn.example.com", "/dist/app.js", ""))
        self.assertFalse(decide_http_request(policy, "https", "POST", "cdn.example.com", "/dist/app.js", ""))
        self.assertFalse(decide_http_request(policy, "https", "GET", "cdn.example.com", "/admin", ""))

    def test_path_guard_resists_traversal_and_encoding(self) -> None:
        policy = {
            "allowed_network_access": {
                "api.example.com": {"allow_http_methods": ["GET"], "path_guards": ["^/v1/threads(?:/.*)?$"]}
            }
        }
        # A legitimate guarded path is allowed.
        self.assertTrue(decide_http_request(policy, "https", "GET", "api.example.com", "/v1/threads/abc", ""))
        # ../ traversal that the upstream would resolve to /admin is denied,
        # both raw and percent-encoded.
        self.assertFalse(
            decide_http_request(policy, "https", "GET", "api.example.com", "/v1/threads/../../admin", "")
        )
        self.assertFalse(
            decide_http_request(policy, "https", "GET", "api.example.com", "/v1/threads/%2e%2e/%2e%2e/admin", "")
        )

    def test_exact_domain_rule_wins_over_wildcard(self) -> None:
        policy = {
            "allowed_network_access": {
                "*.example.com": {"allow_http_methods": ["GET", "POST"]},
                "api.example.com": {"allow_http_methods": ["GET"]},
            }
        }

        self.assertIs(find_domain_rule(policy, "api.example.com"), policy["allowed_network_access"]["api.example.com"])
        self.assertFalse(decide_http_request(policy, "https", "POST", "api.example.com", "/", ""))
        self.assertTrue(decide_http_request(policy, "https", "POST", "other.example.com", "/", ""))

    def test_host_allowed_requires_listed_domain_with_methods(self) -> None:
        policy = {
            "allowed_network_access": {
                "allowed.example.com": {"allow_http_methods": ["GET"]},
                "closed.example.com": {"allow_http_methods": []},
            }
        }

        self.assertTrue(host_allowed(policy, "allowed.example.com"))
        self.assertFalse(host_allowed(policy, "closed.example.com"))
        self.assertFalse(host_allowed(policy, "unlisted.example.com"))

    def test_openai_guard_pins_account_and_blocks_live_web_search(self) -> None:
        policy = {
            "allowed_network_access": {
                "chatgpt.com": {
                    "allow_http_methods": ["POST"],
                    "openai_account_guard": True,
                    "openai_disable_live_web_search": True,
                }
            }
        }
        host = "chatgpt.com"
        # Subsequent web-search checks carry the valid account header so they
        # isolate the web-search logic from the account pin.
        json_header = [("Content-Type", "application/json"), ("ChatGPT-Account-Id", "acct_good")]

        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
            # Without a stored account id, OpenAI data-plane requests fail
            # closed even if the presented header would otherwise match.
            self.assertIsNotNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_good")], b"{}"))
            save_proxy_openai_account_id("acct_good")
            # Account pinning on the ChatGPT-Account-Id header.
            self.assertIsNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_good")], b"{}"))
            self.assertIsNotNone(openai_request_denied(policy, host, [("ChatGPT-Account-Id", "acct_evil")], b"{}"))
            # A missing account header is denied, not allowed — otherwise the pin is
            # bypassable by omission.
            self.assertIsNotNone(openai_request_denied(policy, host, [("Content-Type", "application/json")], b"{}"))
            self.assertIsNone(openai_request_denied(policy, "auth.openai.com", [], b"{}"))
            self.assertIsNone(openai_request_denied({"allowed_network_access": {}}, "github.com", [], b"{}"))
            unpinned_policy = {
                "allowed_network_access": {
                    "chatgpt.com": {"allow_http_methods": ["POST"], "openai_disable_live_web_search": True}
                }
            }
            self.assertIsNone(openai_request_denied(unpinned_policy, host, [], b'{"input": "hello"}'))

            # Live web search (external access on, or unset) is denied; cached is allowed.
            live = b'{"tools": [{"type": "web_search", "external_web_access": true}]}'
            unset = b'{"tools": [{"type": "web_search"}]}'
            cached = b'{"tools": [{"type": "web_search", "external_web_access": false}]}'
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, live))
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, unset))
            self.assertIsNone(openai_request_denied(policy, host, json_header, cached))

            # The legacy preview tool is always denied; so is a bare marker with no tool.
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, b'{"tools": [{"type": "web_search_preview"}]}'))
            self.assertIsNotNone(openai_request_denied(policy, host, json_header, b'{"note": "web_search"}'))

            # Evasion: a non-JSON content-type (or a leading non-brace byte) must not
            # skip inspection — the markers are still caught.
            text_header = [("Content-Type", "text/plain"), ("ChatGPT-Account-Id", "acct_good")]
            self.assertIsNotNone(openai_request_denied(policy, host, text_header, b'x' + live))
            self.assertIsNotNone(
                openai_request_denied(policy, host, text_header, b'{"tools": [{"type": "web_search_preview"}]}')
            )

            # A request with no web search and no marker is fine.
            self.assertIsNone(openai_request_denied(policy, host, json_header, b'{"input": "hello"}'))

            # gzip-encoded live request is decoded and still denied (no evasion).
            import gzip
            gz_headers = [
                ("Content-Type", "application/json"),
                ("Content-Encoding", "gzip"),
                ("ChatGPT-Account-Id", "acct_good"),
            ]
            self.assertIsNotNone(openai_request_denied(policy, host, gz_headers, gzip.compress(live)))

            # The guard only applies where the flag is set.
            self.assertIsNone(openai_request_denied({"allowed_network_access": {}}, "github.com", json_header, live))

    def test_web_search_guard_caps_gzip_and_deflate_decoded_size(self) -> None:
        import gzip
        import zlib

        from host.runtime import network_policy

        policy = {
            "allowed_network_access": {
                "chatgpt.com": {
                    "allow_http_methods": ["POST"],
                    "openai_account_guard": True,
                    "openai_disable_live_web_search": True,
                }
            }
        }
        with (
            tempfile.TemporaryDirectory() as tmp,
            patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}),
            patch.object(network_policy, "MAX_DECODED_BODY_BYTES", 32),
        ):
            save_proxy_openai_account_id("acct_good")
            for encoding, compressed in (
                ("gzip", gzip.compress(b'{"input":"' + b"x" * 40 + b'"}')),
                ("deflate", zlib.compress(b'{"input":"' + b"x" * 40 + b'"}')),
            ):
                headers = [
                    ("Content-Type", "application/json"),
                    ("Content-Encoding", encoding),
                    ("ChatGPT-Account-Id", "acct_good"),
                ]
                with self.subTest(encoding=encoding):
                    self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, compressed))

    def test_web_search_guard_decodes_zstd_and_brotli_bodies(self) -> None:
        # Codex compresses request bodies (and the proxy must not be evadable
        # through an encoding): zstd and brotli go through the system binaries.
        import shutil
        import subprocess

        from host.runtime import network_policy

        policy = {
            "allowed_network_access": {
                "chatgpt.com": {
                    "allow_http_methods": ["POST"],
                    "openai_account_guard": True,
                    "openai_disable_live_web_search": True,
                }
            }
        }
        live = b'{"tools": [{"type": "web_search", "external_web_access": true}]}'
        cached = b'{"tools": [{"type": "web_search", "external_web_access": false}]}'
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
            save_proxy_openai_account_id("acct_good")
            for encoding, binary in (("zstd", network_policy.ZSTD_BIN), ("br", network_policy.BROTLI_BIN)):
                with self.subTest(encoding=encoding):
                    tool = shutil.which(binary) or shutil.which(Path(binary).name)
                    if tool is None:
                        self.skipTest(f"{binary} is not installed")
                    headers = [
                        ("Content-Type", "application/json"),
                        ("Content-Encoding", encoding),
                        ("ChatGPT-Account-Id", "acct_good"),
                    ]
                    with patch.object(network_policy, {"zstd": "ZSTD_BIN", "br": "BROTLI_BIN"}[encoding], tool):
                        compress = subprocess.run([tool, "-c"], input=live, stdout=subprocess.PIPE, check=True)
                        self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, compress.stdout))
                        compress = subprocess.run([tool, "-c"], input=cached, stdout=subprocess.PIPE, check=True)
                        self.assertIsNone(openai_request_denied(policy, "chatgpt.com", headers, compress.stdout))
                        # A corrupt stream fails closed.
                        self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, b"not compressed"))

    def test_unsupported_content_encoding_fails_closed(self) -> None:
        policy = {
            "allowed_network_access": {
                "chatgpt.com": {
                    "allow_http_methods": ["POST"],
                    "openai_account_guard": True,
                    "openai_disable_live_web_search": True,
                }
            }
        }
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
            save_proxy_openai_account_id("acct_good")
            headers = [
                ("Content-Type", "application/json"),
                ("Content-Encoding", "lzma"),
                ("ChatGPT-Account-Id", "acct_good"),
            ]
            self.assertIsNotNone(openai_request_denied(policy, "chatgpt.com", headers, b'{"input": "hello"}'))

    def test_anthropic_guard_pins_oauth_bearer_hash(self) -> None:
        policy = {
            "allowed_network_access": {
                "api.anthropic.com": {"allow_http_methods": ["GET", "POST"], "anthropic_account_guard": True}
            }
        }
        headers = [("Authorization", "Bearer token-good")]
        with tempfile.TemporaryDirectory() as tmp, patch.dict(os.environ, {"TRUSTYCLAW_STATE_DIR": tmp}):
            self.assertEqual(
                anthropic_request_denied(policy, "POST", "api.anthropic.com", "/v1/messages", headers),
                "Claude account token is not available",
            )
            for path in (
                "/api/oauth/profile",
                "/api/oauth/claude_cli/roles",
                "/api/organization/claude_code_first_token_date",
                "/api/claude_code/policy_limits",
                "/api/claude_code/settings",
            ):
                with self.subTest(pre_pin_bootstrap_path=path):
                    self.assertIsNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", path, headers))
            self.assertIsNotNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/oauth/profile", []))
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "POST", "api.anthropic.com", "/api/event_logging/v2/batch", headers
                )
            )
            save_proxy_claude_account(
                {
                    "account_id": "acct",
                    "organization_id": "org",
                    "access_token_sha256": "2743594a82ad13b481caa0b87bea69906b4888c10e25fa1dc95020166edd5a67",
                }
            )
            self.assertIsNone(anthropic_request_denied(policy, "POST", "api.anthropic.com", "/v1/messages", headers))
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "POST", "api.anthropic.com", "/v1/messages", [("Authorization", "Bearer wrong")]
                )
            )
            self.assertIsNotNone(anthropic_request_denied(policy, "POST", "api.anthropic.com", "/v1/messages", []))
            self.assertIsNone(anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/hello", []))
            self.assertIsNotNone(
                anthropic_request_denied(policy, "GET", "api.anthropic.com", "/api/oauth/profile", [])
            )
            self.assertIsNotNone(
                anthropic_request_denied(
                    policy, "GET", "api.anthropic.com", "/api/oauth/profile", [("Authorization", "Bearer wrong")]
                )
            )

    def test_parse_https_request_head(self) -> None:
        method, target, headers = read_request_head(
            io.BytesIO(b"GET /v1/health?check=1 HTTP/1.1\r\nHost: api.example.com\r\nUpgrade: websocket\r\n\r\n")
        )

        self.assertEqual(method, "GET")
        self.assertEqual(target, "/v1/health?check=1")
        self.assertEqual(headers, [("Host", "api.example.com"), ("Upgrade", "websocket")])


class DeployNetworkTests(unittest.TestCase):
    def test_subnet_requires_active_internet_gateway_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "GatewayId": "igw-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.deploy._aws", side_effect=responses):
            self.assertTrue(_subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))

    def test_subnet_rejects_nat_default_route(self) -> None:
        responses = [
            {
                "RouteTables": [
                    {
                        "Routes": [
                            {
                                "DestinationCidrBlock": "0.0.0.0/0",
                                "NatGatewayId": "nat-123",
                                "State": "active",
                            }
                        ]
                    }
                ]
            }
        ]

        with patch("host.deploy._aws", side_effect=responses):
            self.assertFalse(_subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
