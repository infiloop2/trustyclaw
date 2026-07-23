from __future__ import annotations

import unittest

from host.session_options import SESSION_OPTIONS, public_session_options, session_config_error


class SessionOptionsTests(unittest.TestCase):
    def test_exposes_only_the_operator_session_options(self) -> None:
        self.assertEqual(
            SESSION_OPTIONS,
            {
                "codex": {
                    "gpt-5.6-terra": ("high", "max", "ultra"),
                    "gpt-5.6-sol": ("high", "max", "ultra"),
                    "gpt-5.6-luna": ("high", "max"),
                },
                "claude_code": {
                    "opus": ("high", "max", "ultracode"),
                    "fable": ("high", "max", "ultracode"),
                    "sonnet": ("high", "max", "ultracode"),
                },
                "hermes": {
                    "deepseek.v3.2": ("high",),
                    "qwen.qwen3-coder-next": ("high",),
                    "moonshotai.kimi-k2.5": ("high",),
                },
            },
        )

    def test_rejects_cross_runtime_and_luna_ultra_combinations(self) -> None:
        self.assertIsNone(session_config_error("codex", "gpt-5.6-sol", "ultra"))
        self.assertIsNone(session_config_error("claude_code", "fable", "ultracode"))
        self.assertIsNotNone(session_config_error("codex", "gpt-5.6-luna", "ultra"))
        self.assertIsNotNone(session_config_error("codex", "opus", "high"))
        self.assertIsNotNone(session_config_error("claude_code", "fable", "ultra"))
        self.assertIsNotNone(session_config_error("unsupported", "deepseek.v3.2", "max"))
        self.assertIsNone(session_config_error("hermes", "deepseek.v3.2", "high"))
        self.assertIsNotNone(session_config_error("hermes", "deepseek.v3.2", "max"))

    def test_public_options_are_json_facing_copies(self) -> None:
        options = public_session_options()
        self.assertEqual(options["codex"]["gpt-5.6-luna"], ["high", "max"])
        self.assertEqual(
            options["claude_code"]["fable"],
            ["high", "max", "ultracode"],
        )
        options["codex"]["gpt-5.6-luna"].append("invalid")
        self.assertEqual(SESSION_OPTIONS["codex"]["gpt-5.6-luna"], ("high", "max"))


if __name__ == "__main__":
    unittest.main()
