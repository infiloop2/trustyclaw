"""Shared selection, reporting, and diagnostics for persistent stage tests."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
import hashlib
import json
import os
from pathlib import Path
import time
import traceback

from host.runtime.tools.tools_host import BUNDLED_TOOLS


STAGE_AGENT_NAME = "trustyclaw-stage"
CHEAP_MODELS = {
    "codex": "gpt-5.6-luna",
    "claude_code": "sonnet",
    "hermes": "qwen.qwen3-coder-next",
}
CHEAP_EFFORT = "high"
TOOL_SUITES = tuple(sorted(BUNDLED_TOOLS))
STAGE_SUITES = ("all", *TOOL_SUITES, "claude", "codex", "hermes", "github")
RUNTIME_LABELS = {
    "codex": "Codex",
    "claude_code": "Claude Code",
    "hermes": "Hermes",
}

STAGE_BEDROCK_ENV = (
    "STAGE_BEDROCK_AWS_ACCESS_KEY_ID",
    "STAGE_BEDROCK_AWS_SECRET_ACCESS_KEY",
)

STAGE_GITHUB_APP_ENV = {
    "write_repo": "STAGE_GITHUB_WRITE_REPO",
    "app_id": "STAGE_GITHUB_APP_ID",
    "installation_id": "STAGE_GITHUB_APP_INSTALLATION_ID",
    "private_key": "STAGE_GITHUB_APP_PRIVATE_KEY",
}


def diagnostic_ref(value: object) -> str:
    """Stable short reference for correlating an opaque id without logging it."""
    return hashlib.sha256(str(value).encode("utf-8")).hexdigest()[:12]


@dataclass(frozen=True)
class IntegrationResult:
    name: str
    credential: str
    outcome: str
    detail: str


class CredentialUnavailable(RuntimeError):
    """A live probe proved that configured stage credentials cannot be used."""


class StageReport:
    """Independent stage results rendered into the final Actions summary."""

    def __init__(self, suite: str) -> None:
        self.suite = suite
        self.results: list[IntegrationResult] = []

    def add(self, name: str, credential: str, outcome: str, detail: str) -> None:
        self.results.append(IntegrationResult(name, credential, outcome, detail))

    def failed(self) -> bool:
        return any(result.outcome == "failed" for result in self.results)

    @staticmethod
    def _cell(value: str) -> str:
        return value.replace("|", "\\|").replace("\n", " ")

    def markdown(self) -> str:
        lines = [
            "## Stage integration results",
            "",
            f"Suite: `{self.suite}`",
            "",
            "| Integration | Credentials | Result | Detail |",
            "| --- | --- | --- | --- |",
        ]
        for result in self.results:
            lines.append(
                f"| {self._cell(result.name)} | {self._cell(result.credential)} | "
                f"{self._cell(result.outcome)} | {self._cell(result.detail)} |"
            )
        counts = {
            outcome: sum(result.outcome == outcome for result in self.results)
            for outcome in ("passed", "failed", "skipped")
        }
        lines.extend(
            [
                "",
                f"Result: {counts['passed']} passed, {counts['failed']} failed, "
                f"{counts['skipped']} skipped.",
                "",
            ]
        )
        return "\n".join(lines)


def suite_tools(suite: str) -> tuple[str, ...]:
    if suite == "all":
        return TOOL_SUITES
    return (suite,) if suite in TOOL_SUITES else ()


def github_app_config_from_env() -> tuple[dict[str, str] | None, str | None]:
    """Read optional GitHub App stage secrets without aborting other suites."""
    values = {
        key: (os.environ.get(env) or "").strip()
        for key, env in STAGE_GITHUB_APP_ENV.items()
    }
    if not any(values.values()):
        return None, None
    missing = [STAGE_GITHUB_APP_ENV[key] for key, value in values.items() if not value]
    if missing:
        return None, (
            "incomplete GitHub App stage secrets: set all of "
            + ", ".join(STAGE_GITHUB_APP_ENV.values())
            + " or none; missing "
            + ", ".join(missing)
        )
    repo = values["write_repo"]
    owner, _, name = repo.partition("/")
    if not owner or not name or "/" in name:
        return None, f"{STAGE_GITHUB_APP_ENV['write_repo']} must be 'owner/repo', got {repo!r}"
    return (
        {
            "owner": owner,
            "repo": name,
            "app_id": values["app_id"],
            "installation_id": values["installation_id"],
            "private_key_pem": values["private_key"],
        },
        None,
    )


def bedrock_credential_from_env() -> tuple[tuple[str, str] | None, str | None]:
    """Read the optional Bedrock stage credential."""
    access_key_env, secret_key_env = STAGE_BEDROCK_ENV
    access_key_id = (os.environ.get(access_key_env) or "").strip()
    secret_access_key = (os.environ.get(secret_key_env) or "").strip()
    if not access_key_id and not secret_access_key:
        return None, None
    if not access_key_id or not secret_access_key:
        missing = access_key_env if not access_key_id else secret_key_env
        return None, (
            f"incomplete Bedrock stage credential: set both {access_key_env} and "
            f"{secret_key_env}; missing {missing}"
        )
    return (access_key_id, secret_access_key), None


def integration_label(integration: str) -> str:
    if integration == "claude":
        return "Claude Code"
    if integration == "codex":
        return "Codex"
    if integration == "github":
        return "GitHub"
    if integration == "hermes":
        return "Hermes"
    if integration == "bedrock":
        return "AWS Bedrock"
    if integration == "runtime_interoperability":
        return "Runtime interoperability"
    return BUNDLED_TOOLS[integration].manifest.display_name


def selected_integrations(suite: str) -> tuple[str, ...]:
    if suite == "all":
        return ("codex", "claude", "hermes", "github", *TOOL_SUITES)
    return (suite,)


def agent_catalog_tool_ids(output: str) -> tuple[str, ...]:
    """Parse the exact JSON object requested from an agent catalog task."""
    start = output.find("{")
    end = output.rfind("}")
    if start < 0 or end < start:
        raise AssertionError(f"agent MCP catalog output was not a JSON object: {output!r}")
    try:
        parsed = json.loads(output[start : end + 1])
    except json.JSONDecodeError as exc:
        raise AssertionError(f"agent MCP catalog output was invalid JSON: {output!r}") from exc
    tools = parsed.get("tools") if isinstance(parsed, dict) else None
    if not isinstance(tools, list) or not all(isinstance(tool_id, str) for tool_id in tools):
        raise AssertionError(f"agent MCP catalog output had no string tools list: {parsed!r}")
    if len(tools) != len(set(tools)):
        raise AssertionError(f"agent MCP catalog output repeated tool ids: {tools!r}")
    return tuple(sorted(tools))


def record_check(
    report: StageReport,
    integration: str,
    check: Callable[[], None],
    success_detail: str,
    *,
    skip_unavailable: bool = True,
) -> bool:
    """Run one integration independently with timing and failure diagnostics."""
    label = integration_label(integration)
    started = time.monotonic()
    print(f"\n  [integration start] {label}", flush=True)
    try:
        check()
    except CredentialUnavailable as exc:
        elapsed = time.monotonic() - started
        outcome = "skipped" if skip_unavailable else "failed"
        report.add(label, "unavailable", outcome, str(exc))
        print(
            f"  [integration {outcome}] {label} after {elapsed:.1f}s: {exc}",
            flush=True,
        )
        return False
    except Exception as exc:  # noqa: BLE001 - one integration must not hide the rest
        elapsed = time.monotonic() - started
        detail = f"{type(exc).__name__}: {exc}"
        report.add(label, "available", "failed", detail)
        print(f"  [integration failed] {label} after {elapsed:.1f}s: {detail}", flush=True)
        traceback.print_exc()
        return False
    elapsed = time.monotonic() - started
    report.add(label, "available", "passed", success_detail)
    print(f"  [integration passed] {label} in {elapsed:.1f}s", flush=True)
    return True


def write_action_summary(report: StageReport, summary_file: Path | None = None) -> None:
    if summary_file is not None:
        summary_file.write_text(report.markdown(), encoding="utf-8")
        return
    action_summary = os.environ.get("GITHUB_STEP_SUMMARY")
    if not action_summary:
        return
    with Path(action_summary).open("a", encoding="utf-8") as summary:
        summary.write(report.markdown())
