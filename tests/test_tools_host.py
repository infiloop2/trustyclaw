"""Host tool runtime tests: the HostAPI implementation backed by admin state,
manifest schema validation, and the single-use approval lifecycle."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import tempfile
import unittest
from unittest.mock import patch

import pg_harness

import host.tools as tool_packages
from host.runtime.core import db, state
from host.runtime.tools import tools_host
from host.tools import (
    ActionExecuted,
    ActionFailed,
    ActionPendingApproval,
    ActionResult,
    ActionSpec,
    ApprovalExecuted,
    ApprovalResult,
    ConfigRequirement,
    ConnectionStatus,
    CredentialFlow,
    DataSummary,
    DataSummaryCard,
    HostAPI,
    JSONObject,
    OAuthCompleteConnectParams,
    OAuthCompleteConnectResult,
    OAuthStartConnectParams,
    OAuthStartConnectResult,
    StoredCredential,
    ToolManifest,
)

FAKE_OUTPUT_SCHEMA: JSONObject = {
    "type": "object",
    "required": ["status"],
    "properties": {"status": {"type": "string"}},
    "additionalProperties": True,
}

FAKE_DATA_SUMMARY = DataSummary(
    cards=(
        DataSummaryCard(title="What leaves this host", description="Test data leaves the host."),
        DataSummaryCard(title="Where it can go", description="Only to the test provider."),
        DataSummaryCard(title="What the provider can do with it", description="Test use policy."),
        DataSummaryCard(title="How long the provider retains it", description="Test retention policy."),
    ),
)

FAKE_MANIFEST = ToolManifest(
    tool_id="fake_notes",
    display_name="Fake Notes",
    description="Test-only note tool.",
    connection="oauth",
    data_summary=FAKE_DATA_SUMMARY,
    actions=(
        ActionSpec(id="read_note",
            description="Read the stored note.",
            data_policy="Reads the note from the connected account. Runs directly.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=FAKE_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="crash_note",
            description="Crash before returning a result.",
            data_policy="Test-only crashing action.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            output_schema=FAKE_OUTPUT_SCHEMA,
        ),
        ActionSpec(id="write_note",
            description="Replace the stored note.",
            data_policy="Writes the note. Approval-gated by the tool.",
            input_schema={
                "type": "object",
                "properties": {"text": {"type": "string"}},
                "required": ["text"],
                "additionalProperties": False,
            },
            approval="operator",
        ),
    ),
    config=(ConfigRequirement(key="FAKE_NOTES_TOKEN", description="Test-only token."),),
)


def _fake_account() -> JSONObject:
    return {"id": "acct-1", "label": "notes@example.com", "scopes": ["notes"]}


class FakeCredentialFlow(CredentialFlow):
    def start_connect(self, params: OAuthStartConnectParams, api: HostAPI) -> OAuthStartConnectResult:
        del params, api
        return {"authorization_url": "https://example.test/auth", "state": "state-1"}

    def complete_connect(self, params: OAuthCompleteConnectParams, api: HostAPI) -> OAuthCompleteConnectResult:
        del params
        credential: StoredCredential = {
            "account": {"id": "acct-1", "label": "notes@example.com", "scopes": ["notes"]},
            "secret": {"text": ""},
            "metadata": {},
        }
        api.credentials.save(credential)
        return {"account": credential["account"]}

    def disconnect(self, api: HostAPI) -> None:
        api.credentials.clear()

    def connection_status(self, api: HostAPI) -> ConnectionStatus:
        credential = api.credentials.load()
        if credential is None:
            return {"connected": False}
        return {"connected": True, "account": credential["account"]}


class FakeTool:
    """Minimal in-test tool exercising the credential store, config, and approvals."""

    @property
    def manifest(self) -> ToolManifest:
        return FAKE_MANIFEST

    @property
    def credentials(self) -> CredentialFlow:
        return FakeCredentialFlow()

    def _note_text(self, api: HostAPI) -> str:
        credential = api.credentials.load()
        if credential is None:
            return ""
        secret = credential["secret"]
        text = secret.get("text")
        return text if isinstance(text, str) else ""

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        if action == "read_note":
            return ActionExecuted({"status": "success_executed", "text": self._note_text(api), "token": api.config["FAKE_NOTES_TOKEN"]})
        if action == "crash_note":
            raise RuntimeError("unexpected tool crash")
        record = api.approvals.request(
            action_id=action, summary=f"Write the note ({len(str(tool_input.get('text')))} chars).",
            payload={"text": tool_input["text"]},
        )
        return ActionPendingApproval(record.approval_id, record.summary)

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        text = approval.payload.get("text")
        if text == "fail":
            return ActionFailed("Note write failed.")
        if text == "raise":
            raise RuntimeError("unexpected tool crash")
        api.credentials.save({"account": _fake_account(), "secret": {"text": text}, "metadata": {}})
        return ApprovalExecuted(f"Wrote the note ({len(str(text))} chars).")


class BadOutputTool:
    @property
    def manifest(self) -> ToolManifest:
        return ToolManifest(
            tool_id="bad_output_tool",
            display_name="Bad Output Tool",
            description="Test-only bad output tool.",
            connection="enable_only",
            data_summary=FAKE_DATA_SUMMARY,
            actions=(
                ActionSpec(
                    id="run",
                    description="Return an invalid result.",
                    data_policy="Test data.",
                    input_schema={"type": "object", "properties": {}, "additionalProperties": False},
                    output_schema=FAKE_OUTPUT_SCHEMA,
                ),
            ),
        )

    @property
    def credentials(self) -> None:
        return None

    def execute(self, action: str, tool_input: JSONObject, api: HostAPI) -> ActionResult:
        del action, tool_input, api
        return ActionExecuted({"message": "missing status"})

    def execute_approved(self, approval: ApprovalRecord, api: HostAPI) -> ApprovalResult:
        del approval, api
        return ActionFailed("Not used.")


class ToolRegistryTests(unittest.TestCase):
    def test_bundled_tool_ids_match_package_directories(self) -> None:
        package_names = set(tools_host._tool_package_names(Path(tool_packages.__path__[0])))

        self.assertEqual(set(tools_host.BUNDLED_TOOLS), package_names)
        for package_name, tool in tools_host.BUNDLED_TOOLS.items():
            with self.subTest(package_name=package_name):
                self.assertEqual(tool.manifest.tool_id, package_name)

    def test_released_tool_ids_remain_installed(self) -> None:
        # These ids key persisted config, credentials, approvals, and audit
        # records. New packages need no edit here; released ids may not vanish.
        self.assertTrue(
            {
                "brave_search",
                "gmail",
                "google_calendar",
                "ibkr",
                "instagram",
                "instagram_discovery",
                "linkedin",
                "linkedin_discovery",
                "polymarket",
                "runway",
                "twitter",
            }.issubset(tools_host.BUNDLED_TOOLS)
        )

    def test_bundled_action_inputs_are_self_describing(self) -> None:
        for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
            for action in tool.manifest.actions:
                properties = action.input_schema.get("properties", {})
                if not isinstance(properties, dict):
                    self.fail(f"{tool_id}.{action.id}.input_schema.properties must be an object")
                for name, schema in properties.items():
                    with self.subTest(tool_id=tool_id, action=action.id, property=name):
                        if not isinstance(schema, dict):
                            self.fail(f"{tool_id}.{action.id}.{name} must be a schema object")
                        self.assertTrue(str(schema.get("description", "")).strip())

    def test_bundled_tools_have_complete_operator_guides(self) -> None:
        for tool_id, tool in tools_host.BUNDLED_TOOLS.items():
            manifest = tool.manifest
            with self.subTest(tool_id=tool_id):
                self.assertTrue(manifest.protections)
                self.assertTrue(manifest.setup_steps)
                self.assertEqual(len(manifest.data_summary.cards), 4)
                self.assertEqual(manifest.data_summary.cards[0].title, "What leaves this host")
                self.assertEqual(manifest.data_summary.cards[1].title, "Where it can go")
                self.assertTrue(all(step.title and step.description for step in manifest.setup_steps))
                self.assertTrue(all(card.title and (card.description or card.points) for card in manifest.data_summary.cards))
                self.assertTrue(
                    any(
                        "privacy" in f"{link.label}".lower() or "policy" in f"{link.label}".lower()
                        for card in manifest.data_summary.cards
                        for link in card.links
                    ),
                    f"{tool_id} must link an authoritative privacy policy",
                )

    def test_tool_package_directory_requires_an_init_file(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            (root / "bad_tool").mkdir()

            with self.assertRaisesRegex(RuntimeError, "must contain a regular __init__.py"):
                tools_host._tool_package_names(root)

    def test_tool_package_directory_rejects_invalid_ids(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "bad-tool"
            package.mkdir()
            (package / "__init__.py").write_text("")

            with self.assertRaisesRegex(RuntimeError, "package directory must match"):
                tools_host._tool_package_names(root)

    def test_tool_package_rejects_id_drift(self) -> None:
        with tempfile.TemporaryDirectory() as temp:
            root = Path(temp)
            package = root / "expected_tool"
            package.mkdir()
            (package / "__init__.py").write_text("")
            mismatched = ToolManifest(
                tool_id="changed_tool",
                display_name="Changed Tool",
                description="Test.",
                connection="enable_only",
                data_summary=FAKE_DATA_SUMMARY,
                actions=(),
            )
            module = SimpleNamespace(BUNDLED_TOOL=SimpleNamespace(manifest=mismatched))

            with patch("host.runtime.tools.tools_host.importlib.import_module", return_value=module):
                with self.assertRaisesRegex(RuntimeError, "tool_id must match the package directory"):
                    tools_host._discover_bundled_tools(root, "test.tools")

    def test_bundled_tool_registration_rejects_duplicate_tool_ids(self) -> None:
        with self.assertRaisesRegex(RuntimeError, "Duplicate bundled tool_id: fake_notes"):
            tools_host._bundled_tool_map((FakeTool(), FakeTool()))

    def test_manifest_rejects_invalid_or_duplicate_action_ids(self) -> None:
        with self.assertRaisesRegex(ValueError, "ActionSpec.id"):
            ToolManifest(
                tool_id="bad_actions",
                display_name="Bad Actions",
                description="Test.",
                connection="enable_only",
                data_summary=FAKE_DATA_SUMMARY,
                actions=(ActionSpec(id="bad action", description="Bad.", data_policy="Test.", input_schema={}),),
            )
        with self.assertRaisesRegex(ValueError, "Duplicate ActionSpec.id"):
            ToolManifest(
                tool_id="duplicate_actions",
                display_name="Duplicate Actions",
                description="Test.",
                connection="enable_only",
                data_summary=FAKE_DATA_SUMMARY,
                actions=(
                    ActionSpec(id="same", description="One.", data_policy="Test.", input_schema={}),
                    ActionSpec(id="same", description="Two.", data_policy="Test.", input_schema={}),
                ),
            )

    def test_manifest_requires_per_action_data_policy(self) -> None:
        with self.assertRaisesRegex(ValueError, "data_policy"):
            ToolManifest(
                tool_id="no_policy",
                display_name="No Policy",
                description="Test.",
                connection="enable_only",
                data_summary=FAKE_DATA_SUMMARY,
                actions=(ActionSpec(id="run", description="Run.", data_policy="   ", input_schema={}),),
            )


class ToolsHostTestCase(unittest.TestCase):
    def setUp(self) -> None:
        pg_harness.reset_database()
        registry_patch = patch.dict(tools_host.BUNDLED_TOOLS, {"fake_notes": FakeTool()})
        registry_patch.start()
        self.addCleanup(registry_patch.stop)

    def prepare_fake_tool(self, enabled: bool = True, configured: bool = True) -> None:
        with state.mutation() as cur:
            if configured:
                state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "token-1")
            if enabled:
                state.set_tool_enabled(cur, "fake_notes", True)


class HostCredentialsTests(ToolsHostTestCase):
    def test_round_trip_and_partition_isolation(self) -> None:
        credentials = tools_host.HostCredentials("fake_notes")
        self.assertIsNone(credentials.load())
        stored: StoredCredential = {"account": _fake_account(), "secret": {"text": "hello"}, "metadata": {}}
        credentials.save(stored)
        self.assertEqual(credentials.load(), stored)
        self.assertIsNone(tools_host.HostCredentials("gmail").load())
        credentials.clear()
        credentials.clear()  # absent clear is a no-op
        self.assertIsNone(credentials.load())

    def test_value_validation(self) -> None:
        credentials = tools_host.HostCredentials("fake_notes")
        with self.assertRaises(ValueError):
            credentials.save({"secret": {"bad": float("nan")}})  # type: ignore[typeddict-item]
        with self.assertRaises(ValueError):
            credentials.save({"secret": {"big": "x" * (tools_host.CREDENTIAL_VALUE_MAX_BYTES + 1)}})  # type: ignore[typeddict-item]


class HostApprovalsTests(ToolsHostTestCase):
    def test_request_validates_action_summary_and_payload(self) -> None:
        approvals = tools_host.HostApprovals(FAKE_MANIFEST)
        with self.assertRaises(ValueError):
            approvals.request(action_id="not_in_manifest", summary="s", payload={})
        with self.assertRaises(ValueError):
            approvals.request(action_id="write_note", summary="", payload={})
        with self.assertRaises(ValueError):
            approvals.request(action_id="write_note", summary="s" * 501, payload={})
        with self.assertRaises(ValueError):
            approvals.request(action_id="write_note", summary="s", payload={"big": "x" * 70_000})

    def test_request_returns_the_pending_record(self) -> None:
        approvals = tools_host.HostApprovals(FAKE_MANIFEST)
        record = approvals.request(action_id="write_note", summary="Write.", payload={"text": "hi"})
        self.assertEqual(record.status, "pending")
        self.assertEqual(record.payload, {"text": "hi"})


class ExecuteActionTests(ToolsHostTestCase):
    def test_rejects_unknown_disabled_and_invalid_calls(self) -> None:
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown tool"):
            tools_host.execute_action("missing_tool", "read_note", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "not enabled"):
            tools_host.execute_action("fake_notes", "read_note", {})
        self.prepare_fake_tool()
        with self.assertRaisesRegex(tools_host.ToolCallError, "no action"):
            tools_host.execute_action("fake_notes", "missing_action", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "text is required"):
            tools_host.execute_action("fake_notes", "write_note", {})
        with self.assertRaisesRegex(tools_host.ToolCallError, "unsupported fields"):
            tools_host.execute_action("fake_notes", "read_note", {"bogus": 1})

    def test_read_action_executes_with_configured_values(self) -> None:
        self.prepare_fake_tool()
        result = tools_host.execute_action("fake_notes", "read_note", {})
        self.assertEqual(result, {"status": "executed", "result": {"status": "success_executed", "text": "", "token": "token-1"}})
        events = state.page_tool_events_before(None)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_id"], "fake_notes")
        self.assertEqual(events[0]["action_id"], "read_note")
        self.assertEqual(events[0]["outcome"], "executed")
        self.assertTrue(events[0]["has_arguments"])
        self.assertNotIn("arguments", events[0])
        self.assertEqual(state.tool_event(events[0]["seq"])["arguments"], {})

    def test_call_needing_unset_config_reaches_the_tool_and_fails(self) -> None:
        # Config is not gated: an enabled tool with no config still takes the call,
        # and the action fails only because the tool reads a key that is not set —
        # with an operator-actionable message, not a bare KeyError or the generic
        # crash sanitizer.
        self.prepare_fake_tool(configured=False)
        result = tools_host.execute_action("fake_notes", "read_note", {})
        self.assertEqual(result["status"], "failed")
        self.assertEqual(
            result["error"],
            "Tool config FAKE_NOTES_TOKEN is not set. The operator must set it in the admin UI's Tools tab.",
        )

    def test_write_action_queues_exact_payload(self) -> None:
        self.prepare_fake_tool()
        result = tools_host.execute_action("fake_notes", "write_note", {"text": "ship it"})
        self.assertEqual(result["status"], "pending_approval")
        # The id carries the poll token: "approval_<number>.<token>".
        self.assertRegex(result["approval_id"], r"^approval_\d+\.[A-Za-z0-9_-]{20,}$")
        self.assertNotIn("approval_check_token", result)
        record = state.tool_approval(result["approval_id"])
        self.assertIsNotNone(record)
        assert record is not None
        self.assertEqual(record["status"], "pending")
        self.assertEqual(record["payload"], {"text": "ship it"})
        self.assertNotIn("check_token", record)
        self.assertEqual(record["tool_id"], "fake_notes")
        # A guessed sequential number without the token never resolves.
        number = result["approval_id"].split(".", 1)[0]
        self.assertIsNone(state.tool_approval(number))
        self.assertIsNone(state.tool_approval(number + ".wrong-token"))

    def test_action_arguments_are_capped_before_execution_or_audit(self) -> None:
        self.prepare_fake_tool()
        with self.assertRaisesRegex(tools_host.ToolCallError, "Tool input exceeds 65536 bytes"):
            tools_host.execute_action("fake_notes", "write_note", {"text": "x" * 70_000})
        self.assertEqual(state.page_tool_events_before(None), [])

    def test_pending_approvals_are_capped(self) -> None:
        self.prepare_fake_tool()
        with patch("host.runtime.tools.tools_host.PENDING_APPROVAL_LIMIT", 2):
            first = tools_host.execute_action("fake_notes", "write_note", {"text": "one"})
            second = tools_host.execute_action("fake_notes", "write_note", {"text": "two"})
            third = tools_host.execute_action("fake_notes", "write_note", {"text": "three"})

        self.assertEqual(first["status"], "pending_approval")
        self.assertEqual(second["status"], "pending_approval")
        self.assertEqual(
            third,
            {
                "status": "failed",
                "error": "Too many pending tool approvals. Decide or deny existing approvals before queuing more.",
                "reconnect_required": False,
            },
        )
        self.assertEqual(len(state.list_tool_approvals(10)), 2)
        events = state.page_tool_events_before(None)
        self.assertEqual(events[0]["outcome"], "failed")
        self.assertIn("Too many pending tool approvals", events[0]["detail"])

    def test_tool_crash_is_audited_as_failed_call(self) -> None:
        self.prepare_fake_tool()
        result = tools_host.execute_action("fake_notes", "crash_note", {})
        self.assertEqual(result, {"status": "failed", "error": "Tool call failed.", "reconnect_required": False})
        events = state.page_tool_events_before(None)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]["tool_id"], "fake_notes")
        self.assertEqual(events[0]["action_id"], "crash_note")
        self.assertEqual(events[0]["outcome"], "failed")
        self.assertEqual(events[0]["detail"], "Tool call failed.")
        self.assertEqual(state.tool_event(events[0]["seq"])["arguments"], {})

    def test_executed_output_must_match_manifest_output_schema(self) -> None:
        with patch.dict(tools_host.BUNDLED_TOOLS, {"bad_output_tool": BadOutputTool()}):
            with state.mutation() as cur:
                state.set_tool_enabled(cur, "bad_output_tool", True)
            result = tools_host.execute_action("bad_output_tool", "run", {})
        self.assertEqual(result["status"], "failed")
        self.assertIn("Invalid output", result["error"])
        self.assertEqual(state.page_tool_events_before(None)[0]["outcome"], "failed")


class ApprovalLifecycleTests(ToolsHostTestCase):
    def queue_write(self, text: str) -> str:
        self.prepare_fake_tool()
        return tools_host.execute_action("fake_notes", "write_note", {"text": text})["approval_id"]

    def test_approve_executes_once_and_records_the_outcome(self) -> None:
        approval_id = self.queue_write("hello")
        decision = tools_host.decide_approval(approval_id, "approve")
        self.assertEqual(decision["approval"]["status"], "executed")
        self.assertEqual(decision["result"], {"status": "executed", "message": "Wrote the note (5 chars)."})
        self.assertEqual(state.tool_approval(approval_id)["result"], "Wrote the note (5 chars).")
        self.assertEqual(tools_host.HostCredentials("fake_notes").load()["secret"], {"text": "hello"})
        with self.assertRaisesRegex(tools_host.ToolCallError, "not pending"):
            tools_host.decide_approval(approval_id, "approve")

    def test_deny_is_terminal_and_never_executes(self) -> None:
        approval_id = self.queue_write("secret")
        decision = tools_host.decide_approval(approval_id, "deny")
        self.assertEqual(decision["approval"]["status"], "denied")
        self.assertIsNone(tools_host.HostCredentials("fake_notes").load())
        with self.assertRaisesRegex(tools_host.ToolCallError, "not pending"):
            tools_host.decide_approval(approval_id, "deny")

    def test_audit_log_records_the_call_and_the_approval_decision(self) -> None:
        approval_id = self.queue_write("hello")
        tools_host.decide_approval(approval_id, "approve")
        # Newest first: the executed decision, then the queued write call.
        events = state.page_tool_events_before(None)
        self.assertEqual([(e["tool_id"], e["action_id"], e["outcome"]) for e in events], [
            ("fake_notes", "write_note", "executed"),
            ("fake_notes", "write_note", "pending_approval"),
        ])
        self.assertEqual(events[0]["detail"], approval_id)
        self.assertEqual(events[1]["detail"], approval_id)
        self.assertEqual(state.tool_event(events[0]["seq"])["arguments"], {"text": "hello"})
        self.assertEqual(state.tool_event(events[1]["seq"])["arguments"], {"text": "hello"})

    def test_tool_events_paginate_newest_first(self) -> None:
        self.prepare_fake_tool()
        for _ in range(5):
            tools_host.execute_action("fake_notes", "read_note", {})
        first = state.page_tool_events_before(None, limit=2)
        self.assertEqual([e["seq"] for e in first], sorted((e["seq"] for e in first), reverse=True))
        self.assertEqual(len(first), 2)
        older = state.page_tool_events_before(first[-1]["seq"], limit=2)
        self.assertTrue(all(e["seq"] < first[-1]["seq"] for e in older))

    def test_failed_execution_spends_the_approval(self) -> None:
        approval_id = self.queue_write("fail")
        decision = tools_host.decide_approval(approval_id, "approve")
        self.assertEqual(decision["approval"]["status"], "failed")
        self.assertEqual(decision["result"]["status"], "failed")
        self.assertEqual(state.tool_approval(approval_id)["result"], "Note write failed.")
        self.assertEqual(state.page_tool_events_before(None)[0]["detail"], f"{approval_id}: Note write failed.")
        with self.assertRaisesRegex(tools_host.ToolCallError, "not pending"):
            tools_host.decide_approval(approval_id, "approve")

    def test_crashing_execution_spends_the_approval(self) -> None:
        approval_id = self.queue_write("raise")
        decision = tools_host.decide_approval(approval_id, "approve")
        self.assertEqual(decision["approval"]["status"], "failed")
        self.assertEqual(decision["result"], {"status": "failed", "error": "Tool call failed.", "reconnect_required": False})
        self.assertEqual(state.tool_approval(approval_id)["status"], "failed")
        self.assertEqual(state.tool_approval(approval_id)["result"], "Tool call failed.")
        self.assertEqual(state.page_tool_events_before(None)[0]["detail"], f"{approval_id}: Tool call failed.")

    def test_unknown_approval_is_rejected(self) -> None:
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_host.decide_approval("approval_999", "approve")
        with self.assertRaisesRegex(tools_host.ToolCallError, "Unknown approval"):
            tools_host.decide_approval("not-an-id", "deny")

    def test_restart_recovery_spends_interrupted_approvals(self) -> None:
        approval_id = self.queue_write("hello")
        self.assertTrue(state.transition_tool_approval(approval_id, "pending", "approved", 1))
        tools_host.recover_interrupted_approvals()
        record = state.tool_approval(approval_id)
        self.assertEqual(record["status"], "failed")
        # A failure result is persisted so check_tool_approval reports the
        # interrupted execution rather than an empty outcome.
        self.assertIn("restarted", record["result"])

    def test_maintenance_expires_old_pending_approvals(self) -> None:
        fresh_id = self.queue_write("fresh")
        stale = state.insert_tool_approval("fake_notes", "write_note", "Old write.", {"text": "old"}, created_at=1, pending_limit=tools_host.PENDING_APPROVAL_LIMIT)
        tools_host.maintain_approvals()
        self.assertEqual(state.tool_approval(stale["approval_id"])["status"], "expired")
        self.assertNotEqual(state.tool_approval(stale["approval_id"])["decided_at"], 0)
        self.assertEqual(state.tool_approval(fresh_id)["status"], "pending")


class SchemaValidationTests(unittest.TestCase):
    def test_object_rules(self) -> None:
        schema = {
            "type": "object",
            "properties": {"to": {"type": "string"}, "flag": {"type": "boolean"}},
            "required": ["to"],
            "additionalProperties": False,
        }
        self.assertEqual(tools_host.validate_against_schema({"to": "a@b.c"}, schema), "")
        self.assertIn("required", tools_host.validate_against_schema({}, schema))
        self.assertIn("unsupported fields", tools_host.validate_against_schema({"to": "x", "y": 1}, schema))
        self.assertIn("must be a string", tools_host.validate_against_schema({"to": 5}, schema))
        self.assertIn("must be a boolean", tools_host.validate_against_schema({"to": "x", "flag": "yes"}, schema))
        self.assertIn("must be an object", tools_host.validate_against_schema([], schema))

    def test_array_enum_and_one_of_rules(self) -> None:
        schema = {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["a", "b"]},
                "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 2},
                "blocks": {
                    "type": "array",
                    "items": {
                        "oneOf": [
                            {"type": "object", "properties": {"text": {"type": "string"}}, "required": ["text"], "additionalProperties": False},
                            {"type": "object", "properties": {"lines": {"type": "array", "items": {"type": "string"}}}, "required": ["lines"], "additionalProperties": False},
                        ]
                    },
                },
            },
            "additionalProperties": False,
        }
        self.assertEqual(
            tools_host.validate_against_schema(
                {"kind": "a", "ids": ["one"], "blocks": [{"text": "hi"}, {"lines": ["a", "b"]}]}, schema
            ),
            "",
        )
        self.assertIn("one of", tools_host.validate_against_schema({"kind": "c"}, schema))
        self.assertIn("at least 1", tools_host.validate_against_schema({"ids": []}, schema))
        self.assertIn("at most 2", tools_host.validate_against_schema({"ids": ["1", "2", "3"]}, schema))
        self.assertIn("none of the allowed shapes", tools_host.validate_against_schema({"blocks": [{"nope": 1}]}, schema))

    def test_real_manifest_schemas_validate(self) -> None:
        gmail = tools_host.BUNDLED_TOOLS["gmail"].manifest
        send = gmail.action("send_email")
        good = {"to": "a@b.c", "subject": "Hi", "blocks": [{"type": "paragraph", "text": "Hello"}]}
        self.assertEqual(tools_host.validate_against_schema(good, send.input_schema), "")
        self.assertNotEqual(tools_host.validate_against_schema({"to": "a@b.c"}, send.input_schema), "")


class SchemaDeclarationTests(unittest.TestCase):
    def test_all_bundled_tool_schemas_pass_declaration_validation(self) -> None:
        # This is the explicit pre-merge gate. Runtime registration uses the
        # same path, but CI must catch a bad package before it is deployed.
        discovered = tools_host._discover_bundled_tools()
        self.assertEqual(set(tools_host._bundled_tool_map(discovered)), set(tools_host.BUNDLED_TOOLS))

    def test_declared_subset_passes(self) -> None:
        schema = {
            "type": "object",
            "description": "example",
            "properties": {
                "to": {"type": "string", "description": "recipient"},
                "mode": {"enum": ["a", "b"]},
                "ids": {"type": "array", "items": {"type": "string"}, "minItems": 1, "maxItems": 50},
                "flag": {"type": "boolean"},
                "shape": {"oneOf": [{"type": "string"}, {"type": "boolean"}]},
            },
            "required": ["to"],
            "additionalProperties": False,
        }
        self.assertEqual(tools_host.unsupported_schema_error(schema), "")
        # A whole output_schema may be empty (approval-gated actions return a
        # message, not a JSON result), but an input schema may not validate nothing.
        self.assertEqual(tools_host.unsupported_schema_error({}, allow_empty=True), "")
        self.assertNotEqual(tools_host.unsupported_schema_error({}), "")

    def test_out_of_subset_declarations_are_rejected(self) -> None:
        # Every shape the call-time validator would silently skip must fail at
        # declaration, so declaration and enforcement cannot drift.
        cases = [
            {"type": "integer"},
            {"type": "number"},
            {"type": "string", "minLength": 3},
            {"type": "string", "pattern": "^x"},
            {"type": "array"},
            {"type": "array", "items": {"type": "string"}, "minItems": -1},
            {"type": "array", "items": {"type": "string"}, "maxItems": True},
            {"type": "array", "items": {"type": "string"}, "minItems": 2, "maxItems": 1},
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["b"]},
            {"type": "object", "properties": {"a": {"type": "string"}}, "required": ["a", "a"]},
            {"type": "object", "properties": {1: {"type": "string"}}},
            {"type": "object", "properties": {"a": {}}},
            {"type": "object", "properties": {"a": {"description": "typeless"}}},
            {"type": "string", "description": 1},
            {"oneOf": [{"type": "string"}], "type": "string"},
            "not-a-dict",
        ]
        for schema in cases:
            with self.subTest(schema=schema):
                self.assertNotEqual(tools_host.unsupported_schema_error(schema), "")

    def test_registration_rejects_out_of_subset_manifests(self) -> None:
        bad = ToolManifest(
            tool_id="bad_schema_tool",
            display_name="Bad Schema Tool",
            description="Test-only.",
            connection="enable_only",
            data_summary=FAKE_DATA_SUMMARY,
            actions=(
                ActionSpec(
                    id="run",
                    description="Uses a keyword the validator does not enforce.",
                    data_policy="Test data.",
                    input_schema={"type": "object", "properties": {"n": {"type": "integer"}}, "additionalProperties": False},
                ),
            ),
        )

        class BadSchemaTool:
            manifest = bad

        with self.assertRaisesRegex(RuntimeError, "bad_schema_tool.run.input_schema"):
            tools_host._bundled_tool_map((BadSchemaTool(),))


class ToolConfigStateTests(ToolsHostTestCase):
    def test_host_api_config_is_scoped_to_the_tool(self) -> None:
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "token-1")
            # The same key name for a different tool is a separate value.
            state.save_tool_config_value(cur, "gmail", "GOOGLE_OAUTH_CLIENT_ID", "other")
        api = tools_host.host_api_for(tools_host.BUNDLED_TOOLS["fake_notes"])
        self.assertEqual(dict(api.config), {"FAKE_NOTES_TOKEN": "token-1"})
        # Clearing a value removes the key from the tool's config view.
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "")
        cleared = tools_host.host_api_for(tools_host.BUNDLED_TOOLS["fake_notes"])
        self.assertEqual(dict(cleared.config), {})

    def test_config_and_credentials_are_encrypted_at_rest(self) -> None:
        with state.mutation() as cur:
            state.save_tool_config_value(cur, "fake_notes", "FAKE_NOTES_TOKEN", "super-secret-key")
        credential = {
            "account": {"id": "acct-1", "label": "notes@example.com", "scopes": ["notes"]},
            "secret": {"refresh_token": "rt-secret"},
            "metadata": {"created_at": "2026-07-09T00:00:00Z"},
        }
        state.put_tool_credential("fake_notes", credential)
        # The secret columns must be secretbox ciphertext, never the plaintext;
        # the connected-account columns are non-secret by contract and stay
        # readable in place.
        with db.transaction() as cur:
            cur.execute("SELECT value FROM tool_config WHERE tool_id = 'fake_notes' AND key = 'FAKE_NOTES_TOKEN'")
            config_value = cur.fetchone()[0]
            cur.execute(
                "SELECT account_id, account_label, secret FROM tool_credentials WHERE tool_id = 'fake_notes'"
            )
            account_id, account_label, credential_secret = cur.fetchone()
        self.assertTrue(config_value.startswith("enc:v1:"))
        self.assertNotIn("super-secret-key", config_value)
        self.assertEqual(account_id, "acct-1")
        self.assertEqual(account_label, "notes@example.com")
        self.assertTrue(credential_secret.startswith("enc:v1:"))
        self.assertNotIn("rt-secret", credential_secret)
        # Round-trips transparently for the tool call and credential layers.
        api = tools_host.host_api_for(tools_host.BUNDLED_TOOLS["fake_notes"])
        self.assertEqual(dict(api.config), {"FAKE_NOTES_TOKEN": "super-secret-key"})
        self.assertEqual(state.tool_credential("fake_notes"), credential)

    def test_malformed_credential_is_rejected(self) -> None:
        # A credential missing its contract fields is refused outright instead
        # of stored partially.
        with self.assertRaises(ValueError):
            state.put_tool_credential("fake_notes", {"secret": {"refresh_token": "rt-secret"}})
        self.assertIsNone(state.tool_credential("fake_notes"))


if __name__ == "__main__":
    unittest.main()
