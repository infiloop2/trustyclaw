from __future__ import annotations

import io
import json
import os
from pathlib import Path
import tempfile
import unittest

import pg_harness
from unittest.mock import patch

from host.config import (
    ConfigError,
    expand_network_controls,
    managed_domain_owner,
    parse_input_config,
    parse_network_controls,
    runtime_operator_connections_from_input,
)
from host.cli.lifecycle import _subnet_has_public_ipv4_route
from host.runtime.network_policy import (
    anthropic_request_denied,
    decide_http_request,
    find_domain_rule,
    github_push_gate_response,
    github_request_denied,
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
                "operator_connections": [
                    {
                        "mode": "ssh",
                        "ssh_public_key": "ssh-ed25519 AAAATEST",
                    },
                    {
                        "mode": "cloudflare_access",
                        "hostname": "trustyclaw.example.com",
                        "tunnel_token_env": "TRUSTYCLAW_TUNNEL_TOKEN",
                    },
                ],
            }
        )

        self.assertEqual(config.agent_name, "trustyclaw-dev_1")
        self.assertIsNotNone(config.operator_connections)
        self.assertEqual(config.operator_connections[0].mode, "ssh")
        self.assertEqual(config.operator_connections[0].ssh_public_key, "ssh-ed25519 AAAATEST")
        self.assertEqual(config.operator_connections[1].mode, "cloudflare_access")
        self.assertEqual(config.operator_connections[1].hostname, "trustyclaw.example.com")

    def test_upgrade_config_does_not_require_ssh_access_fields(self) -> None:
        config = parse_input_config(
            {
                "agent_name": "trustyclaw-dev_1",
                "aws_region": "us-east-1",
                "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
                "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
            },
            require_operator_connections=False,
        )

        self.assertIsNone(config.operator_connections)

    def test_operator_connections_are_rejected_when_not_required(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported fields: operator_connections"):
            parse_input_config(
                {
                    "agent_name": "trustyclaw-dev_1",
                    "aws_region": "us-east-1",
                    "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
                    "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
                    "operator_connections": [
                        {
                            "mode": "ssh",
                            "ssh_public_key": "ssh-ed25519 AAAATEST",
                        }
                    ],
                },
                require_operator_connections=False,
            )

    def test_input_config_requires_ssh_access_and_rejects_network_controls(self) -> None:
        base = {
            "agent_name": "trustyclaw-dev_1",
            "aws_region": "us-east-1",
            "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
        }
        with self.assertRaisesRegex(ConfigError, "operator_connections must be an array"):
            parse_input_config(base)

        with self.assertRaisesRegex(ConfigError, "operator_connections\\[\\].mode must be 'ssh' or 'cloudflare_access'"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {
                            "mode": "future",
                            "ssh_public_key": "ssh-ed25519 AAAATEST",
                        }
                    ],
                }
            )

    def test_runtime_operator_connections_resolve_cloudflare_token_env(self) -> None:
        config = parse_input_config(
            {
                "agent_name": "trustyclaw-dev_1",
                "aws_region": "us-east-1",
                "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
                "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
                "operator_connections": [
                    {
                        "mode": "cloudflare_access",
                        "hostname": "trustyclaw.example.com",
                        "tunnel_token_env": "TRUSTYCLAW_TUNNEL_TOKEN",
                    }
                ],
            }
        )
        connections = runtime_operator_connections_from_input(
            config.operator_connections or (),
            {"TRUSTYCLAW_TUNNEL_TOKEN": "token.value"},
        )

        self.assertEqual(connections[0].mode, "cloudflare_access")
        self.assertEqual(connections[0].hostname, "trustyclaw.example.com")
        self.assertEqual(connections[0].tunnel_token, "token.value")

        with self.assertRaisesRegex(ConfigError, "TRUSTYCLAW_TUNNEL_TOKEN is not set"):
            runtime_operator_connections_from_input(config.operator_connections or (), {})

        with self.assertRaisesRegex(ConfigError, "single Cloudflare tunnel token"):
            runtime_operator_connections_from_input(
                config.operator_connections or (),
                {"TRUSTYCLAW_TUNNEL_TOKEN": "token with spaces"},
            )

        base = {
            "agent_name": "trustyclaw-dev_1",
            "aws_region": "us-east-1",
            "aws_access_key_id_env": "AWS_ACCESS_KEY_ID",
            "aws_secret_access_key_env": "AWS_SECRET_ACCESS_KEY",
        }
        with self.assertRaisesRegex(ConfigError, "operator_connections\\[\\].ssh_public_key must be an OpenSSH public key"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {
                            "mode": "ssh",
                            "ssh_public_key": "not-a-key",
                        }
                    ],
                }
            )

        with self.assertRaisesRegex(ConfigError, "operator_connections must contain at least one connection"):
            parse_input_config({**base, "operator_connections": []})

        with self.assertRaisesRegex(ConfigError, "operator_connections must not contain duplicate modes: ssh"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST"},
                        {"mode": "ssh", "ssh_public_key": "ssh-ed25519 AAAATEST2"},
                    ],
                }
            )

        with self.assertRaisesRegex(ConfigError, "hostname must be an exact domain"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {
                            "mode": "cloudflare_access",
                            "hostname": "*.example.com",
                            "tunnel_token_env": "TRUSTYCLAW_TUNNEL_TOKEN",
                        }
                    ],
                }
            )

        with self.assertRaisesRegex(ConfigError, "tunnel_token_env must be a valid environment variable name"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {
                            "mode": "cloudflare_access",
                            "hostname": "trustyclaw.example.com",
                            "tunnel_token_env": "not valid",
                        }
                    ],
                }
            )

        with self.assertRaisesRegex(ConfigError, "config has unsupported fields: ssh_public_key"):
            parse_input_config({**base, "ssh_public_key": "ssh-ed25519 AAAATEST"})

        with self.assertRaisesRegex(ConfigError, "config has unsupported fields: operator_connection"):
            parse_input_config(
                {
                    **base,
                    "operator_connection": {
                        "mode": "ssh",
                        "ssh_public_key": "ssh-ed25519 AAAATEST",
                    },
                }
            )

        with self.assertRaisesRegex(ConfigError, "config has unsupported fields: network_controls"):
            parse_input_config(
                {
                    **base,
                    "operator_connections": [
                        {
                            "mode": "ssh",
                            "ssh_public_key": "ssh-ed25519 AAAATEST",
                        }
                    ],
                    "network_controls": {
                        "managed_network_integrations": {"openai": {"enabled": True}},
                        "allowed_network_access": {},
                    },
                }
            )

    def test_agent_name_restrictions(self) -> None:
        with self.assertRaises(ConfigError):
            parse_network_controls({"managed_network_integrations": {"openai": {"enabled": True}}, "allowed_network_access": {"*": {}}})

    def test_managed_providers_are_independently_optional(self) -> None:
        for controls in (
            {"allowed_network_access": {}},
            {"managed_network_integrations": {}, "allowed_network_access": {}},
            {
                "managed_network_integrations": {"openai": {"enabled": False}, "claude": {"enabled": False}},
                "allowed_network_access": {},
            },
            {"managed_network_integrations": {"openai": {"enabled": True}}, "allowed_network_access": {}},
            {"managed_network_integrations": {"claude": {"enabled": True}}, "allowed_network_access": {}},
        ):
            with self.subTest(controls=controls):
                parsed = parse_network_controls(controls)
                self.assertIsInstance(parsed.managed_network_integrations.openai.enabled, bool)
                self.assertIsInstance(parsed.managed_network_integrations.claude.enabled, bool)

        disabled = parse_network_controls({"allowed_network_access": {}})
        self.assertEqual(disabled.to_json()["managed_network_integrations"], {})
        self.assertEqual(expand_network_controls(disabled)["allowed_network_access"], {})

    def test_disabled_github_rejects_write_repositories(self) -> None:
        # A disabled integration carries no other state: write repositories (or
        # require_dot_github_approval) require the integration to be enabled. An
        # enabled integration with an empty list stays valid (a read-only agent).
        with self.assertRaisesRegex(
            ConfigError, r"managed_network_integrations\.github\.write_repositories and require_dot_github_approval require enabled to be true"
        ):
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {"enabled": False, "write_repositories": [{"owner": "infiloop2", "repo": "trustyclaw"}]}
                    },
                    "allowed_network_access": {},
                }
            )
        read_only = parse_network_controls(
            {
                "managed_network_integrations": {"github": {"enabled": True, "write_repositories": []}},
                "allowed_network_access": {},
            }
        )
        self.assertEqual(read_only.to_json()["managed_network_integrations"], {"github": {"enabled": True}})
        # A disabled integration serializes away.
        bare = parse_network_controls(
            {"managed_network_integrations": {"github": {"enabled": False}}, "allowed_network_access": {}}
        )
        self.assertEqual(bare.to_json()["managed_network_integrations"], {})

    def test_runtime_network_controls_reject_ssh_port_field(self) -> None:
        with self.assertRaisesRegex(ConfigError, "network_controls has unsupported fields: ssh_port_opened"):
            parse_network_controls(
                {"ssh_port_opened": True, "managed_network_integrations": {}, "allowed_network_access": {}}
            )

    def test_parse_preserves_user_policy_and_expansion_adds_managed_domain_rules(self) -> None:
        controls = parse_network_controls(
            {
                "managed_network_integrations": {"openai": {"enabled": True}},
                "allowed_network_access": {},
            }
        )
        user_policy = controls.to_json()
        self.assertEqual(user_policy["managed_network_integrations"], {"openai": {"enabled": True}})
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

    def test_openai_domains_are_reserved_for_managed_integration(self) -> None:
        for domain in ("api.openai.com", "auth.openai.com", "chatgpt.com", "*.chatgpt.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConfigError, "managed_network_integrations.openai"):
                parse_network_controls(
                    {
                        "managed_network_integrations": {"openai": {"enabled": True}},
                        "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                    }
                )

        for field in ("openai_disable_live_web_search", "openai_account_guard"):
            with self.subTest(field=field), self.assertRaisesRegex(ConfigError, "unsupported fields"):
                parse_network_controls(
                    {
                        "managed_network_integrations": {"openai": {"enabled": True}},
                        "allowed_network_access": {"api.example.com": {"allow_http_methods": ["GET"], field: True}},
                    }
                )

    def test_claude_provider_expands_and_rejects_managed_domains(self) -> None:
        controls = parse_network_controls(
            {
                "managed_network_integrations": {"claude": {"enabled": True}},
                "allowed_network_access": {},
            }
        )
        self.assertEqual(controls.to_json()["managed_network_integrations"], {"claude": {"enabled": True}})
        rules = expand_network_controls(controls)["allowed_network_access"]
        self.assertTrue(rules["api.anthropic.com"]["anthropic_account_guard"])
        self.assertNotIn("claude.ai", rules)
        self.assertEqual(rules["platform.claude.com"]["allow_http_methods"], ["GET", "POST"])
        self.assertEqual(rules["platform.claude.com"]["path_guards"], ["^/v1/oauth(?:/.*)?$"])

        for domain in ("api.anthropic.com", "claude.ai", "platform.claude.com", "*.anthropic.com"):
            with self.subTest(domain=domain), self.assertRaisesRegex(
                ConfigError, "managed_network_integrations.claude"
            ):
                parse_network_controls(
                    {
                        "managed_network_integrations": {"claude": {"enabled": True}},
                        "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                    }
                )

    def test_managed_network_integrations_parse_expand_and_reserve_domains(self) -> None:
        controls = parse_network_controls(
            {
                "managed_network_integrations": {
                    "openai": {"enabled": True},
                    "github": {
                        "enabled": True,
                        "write_repositories": [
                            {"owner": "InfiverseHQ", "repo": "TrustyClaw"},
                            {"owner": "infiversehq", "repo": "trustyclaw-tools"},
                        ],
                    },
                    "python_packages": {"enabled": True},
                    "npm_packages": {"enabled": True},
                },
                "allowed_network_access": {},
            }
        )

        self.assertEqual(
            controls.to_json()["managed_network_integrations"]["github"]["write_repositories"],
            [
                {"owner": "infiversehq", "repo": "trustyclaw"},
                {"owner": "infiversehq", "repo": "trustyclaw-tools"},
            ],
        )
        rules = expand_network_controls(controls)["allowed_network_access"]
        self.assertIn("api.github.com", rules)
        self.assertIn("github_repo_guard", rules["api.github.com"])
        self.assertIn("uploads.github.com", rules)
        self.assertIn("github_repo_guard", rules["uploads.github.com"])
        # The signed-URL domains are plain GET/HEAD rules: presigned S3 paths
        # carry no owner/repo, so no repo guard rides them — structurally.
        for signed_domain in (
            "objects.githubusercontent.com",
            "github-cloud.githubusercontent.com",
            "release-assets.githubusercontent.com",
        ):
            self.assertEqual(
                rules[signed_domain],
                {"allow_http_methods": ["GET", "HEAD"]},
            )
        self.assertIn("pypi.org", rules)
        self.assertIn("registry.npmjs.org", rules)

        for domain, owner in (
            ("github.com", "github"),
            ("uploads.github.com", "github"),
            ("raw.githubusercontent.com", "github"),
            ("pypi.org", "python_packages"),
            ("registry.npmjs.org", "npm_packages"),
        ):
            with self.subTest(domain=domain), self.assertRaisesRegex(ConfigError, f"managed_network_integrations.{owner}"):
                parse_network_controls(
                    {
                        "managed_network_integrations": {},
                        "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                    }
                )

        with self.assertRaisesRegex(ConfigError, "duplicate repository"):
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiversehq", "repo": "trustyclaw"},
                                {"owner": "InfiverseHQ", "repo": "TrustyClaw"},
                            ],
                        }
                    },
                    "allowed_network_access": {},
                }
            )

    def test_broad_wildcards_covering_managed_domains_are_rejected(self) -> None:
        # Two layers: TLD-wide wildcards like *.com never parse (the wildcard
        # shape needs a concrete multi-label suffix), and the reservation
        # check independently owns any wildcard that would cover a managed
        # apex, so neither layer's future loosening exposes managed domains.
        for domain in ("*.com", "*.ai", "*.org"):
            with self.subTest(domain=domain):
                with self.assertRaises(ConfigError):
                    parse_network_controls(
                        {
                            "managed_network_integrations": {},
                            "allowed_network_access": {domain: {"allow_http_methods": ["GET"]}},
                        }
                    )
                self.assertIsNotNone(managed_domain_owner(domain))
        with self.assertRaisesRegex(ConfigError, "managed_network_integrations"):
            parse_network_controls(
                {
                    "managed_network_integrations": {},
                    "allowed_network_access": {"*.githubusercontent.com": {"allow_http_methods": ["GET"]}},
                }
            )
        # An unrelated wildcard still works.
        controls = parse_network_controls(
            {
                "managed_network_integrations": {},
                "allowed_network_access": {"*.example.com": {"allow_http_methods": ["GET"]}},
            }
        )
        self.assertIn("*.example.com", controls.allowed_network_access)
        self.assertIsNone(managed_domain_owner("*.example.com"))

    def test_github_repository_git_suffix_is_normalized_away(self) -> None:
        # The commonly pasted "repo.git" form must match requests, whose repo
        # segment is .git-stripped before lookup.
        controls = parse_network_controls(
            {
                "managed_network_integrations": {
                    "github": {
                        "enabled": True,
                        "write_repositories": [{"owner": "infiloop2", "repo": "TrustyClaw.git"}],
                    }
                },
                "allowed_network_access": {},
            }
        )
        self.assertEqual(
            controls.to_json()["managed_network_integrations"]["github"]["write_repositories"],
            [{"owner": "infiloop2", "repo": "trustyclaw"}],
        )
        policy = expand_network_controls(controls)
        # The write repo matches a push whose repo segment is .git-stripped.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "github.com", "/infiloop2/trustyclaw.git/git-receive-pack", "", b""
            )
        )

        with self.assertRaisesRegex(ConfigError, "duplicate repository"):
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "trustyclaw"},
                                {"owner": "infiloop2", "repo": "trustyclaw.git"},
                            ],
                        }
                    },
                    "allowed_network_access": {},
                }
            )

    def test_legacy_managed_ai_provider_field_is_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported fields: managed_ai_provider_network_access"):
            parse_network_controls(
                {
                    "managed_ai_provider_network_access": {"openai": True},
                    "allowed_network_access": {},
                }
            )

    def test_github_credential_field_is_rejected_in_policy(self) -> None:
        with self.assertRaisesRegex(ConfigError, "unsupported fields: credential"):
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [{"owner": "infiloop2", "repo": "demo"}],
                            "credential": {"mode": "token"},
                        }
                    },
                    "allowed_network_access": {},
                }
            )

    def test_overlapping_wildcard_domain_rules_are_rejected(self) -> None:
        with self.assertRaisesRegex(ConfigError, "wildcard domains must not overlap"):
            parse_network_controls(
                {
                    "managed_network_integrations": {"openai": {"enabled": True}},
                    "allowed_network_access": {
                        "*.example.com": {"allow_http_methods": ["GET"]},
                        "*.api.example.com": {"allow_http_methods": ["POST"]},
                    },
                }
            )

    def test_non_overlapping_wildcard_domain_rules_are_allowed(self) -> None:
        controls = parse_network_controls(
            {
                "managed_network_integrations": {"openai": {"enabled": True}},
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
                "managed_network_integrations": {"openai": {"enabled": True}},
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
                "managed_network_integrations": {"openai": {"enabled": True}},
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
                    "managed_network_integrations": {"openai": {"enabled": True}},
                    "allowed_network_access": {
                        "api.example.com": {"allow_http_methods": ["GET"]},
                        "API.EXAMPLE.COM": {"allow_http_methods": ["POST"]},
                    },
                }
            )


class PolicyTests(unittest.TestCase):
    def test_policy_matches_domain_method_and_path(self) -> None:
        policy = {
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

    def test_github_guard_allows_all_reads_and_scopes_writes(self) -> None:
        policy = expand_network_controls(
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiversehq", "repo": "trustyclaw-tools"},
                            ],
                        }
                    },
                    "allowed_network_access": {},
                }
            )
        )

        # Every read is allowed, whether or not the repo is a write target —
        # web pages, git fetch, raw blobs, archives, and any API GET/HEAD,
        # including unlisted repos and non-repo endpoints like search.
        for host, path in (
            ("github.com", "/infiversehq/trustyclaw-tools"),
            ("github.com", "/other/private-repo"),
            ("api.github.com", "/repos/other/private-repo"),
            ("api.github.com", "/repos/other/private-repo/contents/secret.env"),
            ("api.github.com", "/search/code"),
            ("api.github.com", "/user"),
            ("api.github.com", "/orgs/other-org/members"),
            ("raw.githubusercontent.com", "/other/private-repo/main/README.md"),
            ("codeload.github.com", "/other/private-repo/tar.gz/main"),
            ("objects.githubusercontent.com", "/github-production-release-asset"),
            ("github-cloud.githubusercontent.com", "/github-production-repository-file-5c1aeb"),
        ):
            with self.subTest(read=f"{host}{path}"):
                self.assertIsNone(github_request_denied(policy, "GET", host, path, "", b""))
        # Git fetch (upload-pack) on any repo is a read; compare views with
        # cross-repo fork-network refs are just reads too now.
        self.assertIsNone(
            github_request_denied(policy, "POST", "github.com", "/other/private-repo/git-upload-pack", "", b"")
        )
        for basehead in ("main...dev", "main...attacker:leak", "main...attacker/other-repo:leak"):
            with self.subTest(compare=basehead):
                self.assertIsNone(
                    github_request_denied(policy, "GET", "github.com", f"/other/repo/compare/{basehead}", "", b"")
                )

        # Push (git-receive-pack) is gated on a configured write repository.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "github.com", "/infiversehq/trustyclaw-tools.git/git-receive-pack", "", b""
            )
        )
        self.assertEqual(
            github_request_denied(policy, "POST", "github.com", "/other/repo.git/git-receive-pack", "", b""),
            "github_write_repo_required",
        )
        self.assertEqual(
            github_request_denied(
                policy, "GET", "github.com", "/other/repo.git/info/refs", "service=git-receive-pack", b""
            ),
            "github_write_repo_required",
        )

        # API writes: a repo-scoped mutation on a write repo passes; the same on
        # an unlisted repo needs a write repo.
        self.assertIsNone(
            github_request_denied(policy, "PATCH", "api.github.com", "/repos/infiversehq/trustyclaw-tools/issues/1", "", b"")
        )
        self.assertEqual(
            github_request_denied(policy, "PATCH", "api.github.com", "/repos/other/repo/issues/1", "", b""),
            "github_write_repo_required",
        )
        # Release-asset uploads are repo-scoped writes.
        self.assertIsNone(
            github_request_denied(
                policy, "POST", "uploads.github.com", "/repos/infiversehq/trustyclaw-tools/releases/1/assets", "", b""
            )
        )
        self.assertEqual(
            github_request_denied(
                policy, "POST", "uploads.github.com", "/repos/other/repo/releases/1/assets", "", b""
            ),
            "github_write_repo_required",
        )
        # A mutation that targets no repository (create a repo, create a gist)
        # is never a configured write repo.
        for non_repo_write in ("/user/repos", "/gists", "/orgs/other-org/repos"):
            with self.subTest(path=non_repo_write):
                self.assertEqual(
                    github_request_denied(policy, "POST", "api.github.com", non_repo_write, "", b""),
                    "github_write_repo_required",
                )

        # Repository administration is denied even on a write repo, under one
        # unified reason; reads of all of it stay plain repo reads. This covers
        # the repo root (settings/visibility, delete), boundary-escaping
        # mutations (fork/generate/transfer), and the admin sub-resources.
        for admin_method in ("PATCH", "DELETE", "PUT"):
            self.assertEqual(
                github_request_denied(
                    policy, admin_method, "api.github.com", "/repos/infiversehq/trustyclaw-tools", "", b""
                ),
                "github_repo_admin_write_denied",
            )
        for admin_path in (
            "forks",
            "generate",
            "transfer",
            "collaborators/attacker",
            "invitations/1",
            "keys",
            "hooks",
            "pages",
            "environments/prod",
            "codespaces",
            "dependabot/secrets/TOKEN",
            "rulesets",
            "rulesets/1",
            "properties/values",
            "interaction-limits",
            "releases",
            "immutable-releases",
            "autolinks",
            "topics",
            "vulnerability-alerts",
            "automated-security-fixes",
            "private-vulnerability-reporting",
            "security-advisories",
            "bypass-requests",
            "actions/secrets/TOKEN",
            "actions/variables/NAME",
            "actions/runners/registration-token",
            "actions/permissions",
            "actions/oidc/customization/sub",
            "actions/cache/usage",
            "actions/caches",
            "actions/workflows/12345/disable",
            "actions/workflows/ci.yml/enable",
            "actions/workflows/ci.yml/dispatches",
            "dispatches",
            "statuses/abc123",
            "check-runs",
            "check-suites/7/rerequest",
            "deployments",
            "attestations",
            "actions/runs/1/cancel",
            "actions/runs/1/force-cancel",
            "actions/runs/1/approve",
            "code-scanning/alerts/1",
            "secret-scanning/alerts/1",
            "actions/runs/1/pending_deployments",
            "actions/runs/1/deployment_protection_rule",
            "actions/artifacts/1",
            "branches/main/protection",
            "branches/main/protection/required_status_checks",
            "tags/protection",
            "lfs",
            "pulls/7/update-branch",
        ):
            with self.subTest(path=admin_path):
                self.assertEqual(
                    github_request_denied(
                        policy, "PUT", "api.github.com", f"/repos/infiversehq/trustyclaw-tools/{admin_path}", "", b""
                    ),
                    "github_repo_admin_write_denied",
                )
        # Reading those same admin sub-resources stays a plain repo read.
        for read_path in ("forks", "collaborators", "hooks"):
            with self.subTest(read=read_path):
                self.assertIsNone(
                    github_request_denied(
                        policy, "GET", "api.github.com", f"/repos/infiversehq/trustyclaw-tools/{read_path}", "", b""
                    )
                )
        # Deleting a run or its logs erases the automation record.
        for delete_path in ("actions/runs/1", "actions/runs/1/logs"):
            with self.subTest(path=delete_path):
                self.assertEqual(
                    github_request_denied(
                        policy, "DELETE", "api.github.com", f"/repos/infiversehq/trustyclaw-tools/{delete_path}", "", b""
                    ),
                    "github_repo_admin_write_denied",
                )
        # Normal repo-scoped writes (issues, contents, workflow re-runs, and
        # non-protection branch operations) on the write repo still pass.
        for write_path in (
            "issues",
            "contents/docs/README.md",
            "actions/runs/1/rerun",
            "branches/main/rename",
        ):
            with self.subTest(path=write_path):
                self.assertIsNone(
                    github_request_denied(
                        policy, "POST", "api.github.com", f"/repos/infiversehq/trustyclaw-tools/{write_path}", "", b""
                    )
                )

    def test_require_dot_github_approval_rides_into_guard(self) -> None:
        from host.runtime import github_push_gate

        controls = parse_network_controls(
            {
                "managed_network_integrations": {
                    "github": {
                        "enabled": True,
                        "require_dot_github_approval": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "trustyclaw-tools"}],
                    }
                },
                "allowed_network_access": {},
            }
        )
        self.assertEqual(controls.to_json()["managed_network_integrations"]["github"]["require_dot_github_approval"], True)
        policy = expand_network_controls(controls)
        guard = policy["allowed_network_access"]["github.com"]["github_repo_guard"]
        self.assertTrue(guard["require_dot_github_approval"])
        # should_inspect selects only receive-pack POSTs to a write repo.
        self.assertEqual(
            github_push_gate.should_inspect(guard, "github.com", "POST", "/infiversehq/trustyclaw-tools.git/git-receive-pack"),
            ("infiversehq", "trustyclaw-tools"),
        )
        self.assertIsNone(
            github_push_gate.should_inspect(guard, "github.com", "POST", "/other/repo.git/git-receive-pack")
        )
        self.assertIsNone(
            github_push_gate.should_inspect(guard, "github.com", "GET", "/infiversehq/trustyclaw-tools.git/info/refs")
        )
        # Off by default: no require_dot_github_approval means no gating.
        off = expand_network_controls(
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {"enabled": True, "write_repositories": [{"owner": "infiversehq", "repo": "trustyclaw-tools"}]}
                    },
                    "allowed_network_access": {},
                }
            )
        )
        off_guard = off["allowed_network_access"]["github.com"]["github_repo_guard"]
        self.assertNotIn("require_dot_github_approval", off_guard)
        self.assertIsNone(
            github_push_gate.should_inspect(off_guard, "github.com", "POST", "/infiversehq/trustyclaw-tools.git/git-receive-pack")
        )

        # Approval mode also closes REST content-write bypasses that can create
        # .github-changing commits without entering git-receive-pack.
        for blocked in (
            "/repos/infiversehq/trustyclaw-tools/contents/.github/workflows/ci.yml",
            "/repos/infiversehq/trustyclaw-tools/git/refs/heads/main",
            "/repos/infiversehq/trustyclaw-tools/git/trees",
            "/repos/infiversehq/trustyclaw-tools/git/commits",
            "/repos/infiversehq/trustyclaw-tools/merges",
            "/repos/infiversehq/trustyclaw-tools/merge-upstream",
            "/repos/infiversehq/trustyclaw-tools/pulls/12/merge",
        ):
            with self.subTest(blocked=blocked):
                self.assertEqual(
                    github_request_denied(policy, "PUT", "api.github.com", blocked, "", b""),
                    "github_dot_github_rest_write_denied",
                )
        self.assertIsNone(
            github_request_denied(
                policy, "PUT", "api.github.com",
                "/repos/infiversehq/trustyclaw-tools/contents/docs/README.md", "", b"",
            )
        )

    def test_github_push_gate_cleans_pending_refs_when_enqueue_fails(self) -> None:
        class FakeResult:
            touches_github = True
            ref_updates = [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}]
            paths = {".github/workflows/ci.yml"}

            def __init__(self) -> None:
                self.cleaned: list[str] = []

            def hold_for_approval(self, push_id: str) -> bytes:
                return b"HTTP/1.1 200 OK\r\n\r\n"

            def cleanup_pending(self, push_id: str) -> None:
                self.cleaned.append(push_id)

        result = FakeResult()
        policy = {
            "allowed_network_access": {
                "github.com": {
                    "github_repo_guard": {
                        "require_dot_github_approval": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "trustyclaw-tools"}],
                    }
                }
            }
        }
        with (
            patch("host.runtime.network_policy.read_proxy_github_token", return_value=None),
            patch("host.runtime.network_policy.github_push_gate.inspect", return_value=result),
            patch("host.runtime.network_policy.github_push_gate.new_push_id", return_value="abc123"),
            patch("host.runtime.network_policy.enqueue_pending_push", side_effect=RuntimeError("db down")),
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/trustyclaw-tools.git/git-receive-pack", b"body"
            )

        self.assertIsNone(response)
        self.assertEqual(reason, "github_push_gate_unavailable")
        self.assertEqual(result.cleaned, ["abc123"])

    def test_github_push_gate_cleans_pending_refs_when_hold_fails(self) -> None:
        class FakeResult:
            touches_github = True
            ref_updates = [{"old": "0" * 40, "new": "1" * 40, "ref": "refs/heads/main"}]
            paths = {".github/workflows/ci.yml"}

            def __init__(self) -> None:
                self.cleaned: list[str] = []

            def hold_for_approval(self, push_id: str) -> bytes:
                raise RuntimeError("stale lock")

            def cleanup_pending(self, push_id: str) -> None:
                self.cleaned.append(push_id)

        result = FakeResult()
        policy = {
            "allowed_network_access": {
                "github.com": {
                    "github_repo_guard": {
                        "require_dot_github_approval": True,
                        "write_repositories": [{"owner": "infiversehq", "repo": "trustyclaw-tools"}],
                    }
                }
            }
        }
        with (
            patch("host.runtime.network_policy.read_proxy_github_token", return_value=None),
            patch("host.runtime.network_policy.github_push_gate.inspect", return_value=result),
            patch("host.runtime.network_policy.github_push_gate.new_push_id", return_value="abc123"),
        ):
            response, reason = github_push_gate_response(
                policy, "POST", "github.com", "/infiversehq/trustyclaw-tools.git/git-receive-pack", b"body"
            )

        self.assertIsNone(response)
        self.assertEqual(reason, "github_push_gate_unavailable")
        self.assertEqual(result.cleaned, ["abc123"])

    def test_github_lfs_batch_allows_download_denies_upload(self) -> None:
        policy = expand_network_controls(
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [
                                {"owner": "infiloop2", "repo": "infibot"},
                            ],
                        }
                    },
                    "allowed_network_access": {},
                }
            )
        )
        download = json.dumps({"operation": "download", "objects": []}).encode()
        upload = json.dumps({"operation": "upload", "objects": []}).encode()
        batch = "/other/repo.git/info/lfs/objects/batch"
        write_batch = "/infiloop2/infibot.git/info/lfs/objects/batch"

        # Download (clone/fetch) is a read and passes for any repo.
        self.assertIsNone(github_request_denied(policy, "POST", "github.com", batch, "", download))
        # Uploads are denied even for write repos: the follow-up object PUTs go
        # to signed URLs whose opaque paths cannot be repo-checked, so the batch
        # fails closed with a crisp reason.
        for path in (batch, write_batch):
            with self.subTest(path=path):
                self.assertEqual(
                    github_request_denied(policy, "POST", "github.com", path, "", upload),
                    "github_lfs_push_unsupported",
                )
        for garbage in (b"", b"not json", b'{"operation": "mystery"}'):
            with self.subTest(body=garbage):
                self.assertEqual(
                    github_request_denied(policy, "POST", "github.com", batch, "", garbage),
                    "github_lfs_operation_unresolved",
                )

    def test_github_graphql_requests_fail_closed(self) -> None:
        policy = expand_network_controls(
            parse_network_controls(
                {
                    "managed_network_integrations": {
                        "github": {
                            "enabled": True,
                            "write_repositories": [{"owner": "infiversehq", "repo": "trustyclaw"}],
                        }
                    },
                    "allowed_network_access": {},
                }
            )
        )

        # GraphQL repository scope cannot be verified without a real parser
        # (argument order, aliased variables, and fragments all evade regex
        # extraction), so every GraphQL request is denied — including ones that
        # only reference allowed repositories.
        scoped_query = json.dumps(
            {
                "query": "query($owner:String!, $name:String!) { repository(owner:$owner, name:$name) { id } }",
                "variables": {"owner": "infiversehq", "name": "trustyclaw"},
            }
        ).encode()
        mutation = b'{"query":"mutation { createIssue(input:{}) { clientMutationId } }"}'

        for body in (scoped_query, mutation, b"", b"not json"):
            with self.subTest(body=body):
                self.assertEqual(
                    github_request_denied(policy, "POST", "api.github.com", "/graphql", "", body),
                    "github_graphql_denied",
                )

    def test_openai_guard_pins_account_and_blocks_live_web_search(self) -> None:
        pg_harness.reset_database()
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
        pg_harness.reset_database()
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
        pg_harness.reset_database()
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
        pg_harness.reset_database()
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
        pg_harness.reset_database()
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

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
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

        with patch("host.cli.lifecycle_aws._aws", side_effect=responses):
            self.assertFalse(_subnet_has_public_ipv4_route({}, "vpc-1", "subnet-1"))


if __name__ == "__main__":
    unittest.main()
