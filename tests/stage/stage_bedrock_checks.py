"""Shared AWS Bedrock and Pi/Hermes checks for persistent stage."""

from __future__ import annotations

import shlex
from typing import TYPE_CHECKING

from host.constants import PROXY_PORT
from tests.smoke.smoke_aws import SMOKE_BEDROCK_REGION, AwsSmoke
from tests.stage.stage_support import CHEAP_MODELS, RUNTIME_LABELS


class StageBedrockChecks(AwsSmoke):
    """Bedrock setup, credential-boundary, task, and shared lifecycle checks."""

    if TYPE_CHECKING:
        stage_bedrock_credential: tuple[str, str] | None

        def enforcement_policy(self) -> dict: ...
        def task_body(
            self,
            input_message: str,
            thread_id: str,
            *,
            runtime: str | None = None,
            model: str | None = None,
            effort: str | None = None,
        ) -> dict: ...
        def follow_up_body(self, input_message: str, thread_id: str) -> dict: ...
        def require_runtime_active(self, runtime: str) -> None: ...

    def autoconfigure_bedrock(self, suite: str) -> None:
        """Validate one CI credential and enable the shared provider."""
        selected = (
            ("pi", "hermes")
            if suite == "all"
            else (suite,) if suite in {"pi", "hermes"} else ()
        )
        if not selected or self.stage_bedrock_credential is None:
            return

        access_key_id, secret_access_key = self.stage_bedrock_credential
        self._step("validate and enable the shared AWS Bedrock stage credential")
        try:
            result = self._api(
                "POST",
                "/v1/agent-runtime/bedrock-credentials",
                {
                    "access_key_id": access_key_id,
                    "secret_access_key": secret_access_key,
                    "region": SMOKE_BEDROCK_REGION,
                },
            )
        except Exception as exc:  # noqa: BLE001 - preflight reports the integration independently
            self.bedrock_secret_error = (
                f"shared Bedrock stage credential validation failed: {type(exc).__name__}: {exc}"
            )
            return
        if result != {"status": "accepted"}:
            raise AssertionError(f"shared Bedrock stage credential was not accepted: {result}")

        controls = self._api("GET", "/v1/network/policy").get("network_controls") or {}
        integrations = dict(controls.get("network_integrations") or {})
        integrations["bedrock"] = {"enabled": True}
        controls["network_integrations"] = integrations
        self._api("PUT", "/v1/network/policy", controls)
        for runtime in selected:
            status = self._wait_for_runtime_status(
                {"active", "error"}, runtime=runtime, timeout=180
            )
            if status != "active":
                raise AssertionError(
                    f"shared Bedrock stage credential did not activate {runtime}: {status}"
                )
        self._ok(
            f"shared Bedrock credential validated and activated for {', '.join(selected)} "
            f"in {SMOKE_BEDROCK_REGION}"
        )

    def check_bedrock_auth_and_task(self) -> None:
        """Prove one Bedrock harness through the real proxy and model."""
        runtime = self.agent_runtime
        if runtime not in {"pi", "hermes"}:
            raise AssertionError(f"Bedrock check selected non-Bedrock runtime {runtime!r}")
        label = RUNTIME_LABELS[runtime]
        region = SMOKE_BEDROCK_REGION
        self._step(f"{label} credential boundary + real AWS Bedrock task")
        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        self.require_runtime_active(runtime)

        account = self._agent_account(runtime)
        if (
            account.get("status") != "active"
            or account.get("provider") != "bedrock"
            or account.get("agent_runtimes") != ["pi", "hermes"]
            or not account.get("account_id")
            or not account.get("arn")
        ):
            raise AssertionError(f"shared Bedrock account has unexpected shape: {account}")
        usage = account.get("bedrock_usage")
        if not isinstance(usage, dict) or set(usage) != {"pi", "hermes"}:
            raise AssertionError(f"shared Bedrock account omitted per-runtime usage: {account}")
        for runtime_usage in usage.values():
            estimate = runtime_usage.get("month_to_date")
            if not isinstance(estimate, (int, float)) or estimate < 0:
                raise AssertionError(f"Bedrock live usage estimate is invalid: {usage}")
            if runtime_usage.get("currency") != "USD":
                raise AssertionError(f"Bedrock live usage currency is invalid: {usage}")
        # The real model task below must advance this runtime's own meter.
        baseline_usage = dict(usage[runtime])
        self._assert_provider_metadata(runtime, account)

        encrypted_rows = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            + shlex.quote(
                "SELECT "
                "(SELECT count(*) FROM bedrock_credentials WHERE singleton = TRUE) || ':' || "
                "(SELECT count(*) FROM bedrock_credentials WHERE singleton = TRUE "
                "AND secret_access_key_encrypted LIKE 'enc:v1:%')"
            )
        ).strip()
        if encrypted_rows != "1:1":
            raise AssertionError(
                "Bedrock credential was not validated and encrypted in its single row: "
                f"{encrypted_rows}"
            )

        proxy = f"http://127.0.0.1:{PROXY_PORT}"
        url = f"https://bedrock-runtime.{region}.amazonaws.com/model/deepseek.v3.2/converse"

        def request_with_access_key(access_key_id: str) -> str:
            authorization = (
                "AWS4-HMAC-SHA256 "
                f"Credential={access_key_id}/20260718/{region}/bedrock/aws4_request, "
                "SignedHeaders=content-type;host;x-amz-date, "
                f"Signature={'0' * 64}"
            )
            return self._ssh_code(
                f"sudo -u trustyclaw-agent env HTTPS_PROXY={proxy} "
                "curl -s --max-time 20 -X POST -H 'Content-Type: application/json' "
                "-H 'X-Amz-Date: 20260718T000000Z' "
                f"-H {shlex.quote(f'Authorization: {authorization}')} "
                f"--data '{{\"messages\":[]}}' {shlex.quote(url)}"
            )

        foreign_key = request_with_access_key("AKIA0000000000000000")
        if "bedrock_access_key_mismatch" not in foreign_key:
            raise AssertionError(f"{label} accepted an agent-supplied AWS identity: {foreign_key!r}")

        token = f"{runtime.upper()}_STAGE_OK"
        model = CHEAP_MODELS[runtime]
        baseline_seq = max((event["seq"] for event in self._network_events()), default=0)
        task = self._api(
            "POST",
            "/v1/tasks",
            self.task_body(
                f"Reply with exactly the word {token} and nothing else.",
                f"{runtime}-bedrock",
                model=model,
            ),
        )
        if (task.get("model"), task.get("effort")) != (model, "high"):
            raise AssertionError(f"{label} task did not retain selected options: {task}")
        done = self._wait_for_task(task["task_id"], timeout=300)
        if done["status"] != "completed":
            raise AssertionError(
                f"{label} task ended {done['status']}: {self._task_failure_detail(task['task_id'])}"
            )
        if token not in (done.get("output_message") or "").upper():
            raise AssertionError(f"{label} task output omitted {token}: {done.get('output_message')!r}")

        bedrock_events = [
            event
            for event in self._network_events(since=baseline_seq)
            if event["host"] == f"bedrock-runtime.{region}.amazonaws.com"
        ]
        allowed = [event for event in bedrock_events if event["decision"] == "allowed"]
        denied = [event for event in bedrock_events if event["decision"] == "denied"]
        # A harness may probe an OpenAI-style model catalog (GET /models,
        # GET /v1/models) on the Bedrock runtime host. Only POST
        # .../converse[-stream] is an invocation route, so the guard fails a
        # bare catalog probe closed on the route check; that
        # network_policy_denied is correct and the Converse task still
        # completes. Tolerate only that exact benign probe: the guard emits
        # more specific reasons before the route check (for example
        # bedrock_query_auth_denied for presigned query auth), so require the
        # route-denial reason and an empty query and never swallow those. Any
        # other denial, including a denied invocation, is a real regression.
        unexpected = [
            event
            for event in denied
            if not (
                event["method"] == "GET"
                and event["path"] in {"/models", "/v1/models"}
                and not event.get("query")
                and event.get("reason_code") == "network_policy_denied"
            )
        ]
        if not allowed or unexpected:
            raise AssertionError(f"{label} task Bedrock traffic was not cleanly allowed: {bedrock_events}")
        if not any(event["path"].endswith(("/converse", "/converse-stream")) for event in allowed):
            raise AssertionError(f"{label} task used no Bedrock Converse path: {allowed}")

        follow_up = self._api(
            "POST",
            "/v1/tasks",
            self.follow_up_body(
                "Reply again with the exact uppercase word from your previous answer and nothing else.",
                f"{runtime}-bedrock",
            ),
        )
        follow_up_done = self._wait_for_task(follow_up["task_id"], timeout=300)
        if follow_up_done["status"] != "completed" or token not in (
            follow_up_done.get("output_message") or ""
        ).upper():
            raise AssertionError(
                f"{label} did not resume its provider session: {follow_up_done}; "
                f"{self._task_failure_detail(follow_up['task_id'])}"
            )

        # Live usage metering, proven against real Bedrock responses: the two
        # completed tasks must have advanced this runtime's own counters, and
        # metering (not just request counting) must have parsed real response
        # shapes.
        live_usage = self._agent_account(runtime).get("bedrock_usage", {}).get(runtime, {})
        for counter in ("requests", "metered_requests", "input_tokens", "output_tokens"):
            if live_usage.get(counter, 0) <= baseline_usage.get(counter, 0):
                raise AssertionError(
                    f"{label} live usage counter {counter!r} did not advance across a real task: "
                    f"{baseline_usage} -> {live_usage}"
                )
        if live_usage.get("month_to_date", 0) <= baseline_usage.get("month_to_date", 0):
            raise AssertionError(
                f"{label} live cost estimate did not grow with real metered usage: "
                f"{baseline_usage} -> {live_usage}"
            )
        self._ok(
            f"{label} used only the shared encrypted credential, rejected a foreign identity, "
            f"completed and resumed real {model} traffic, and its live usage meter advanced "
            f"({baseline_usage.get('input_tokens', 0)} -> {live_usage.get('input_tokens', 0)} input tokens)"
        )

    def check_bedrock_disable_stages_credential_for_reenable(self) -> None:
        """One provider toggle deactivates both runtimes and retains its credential."""
        self._step("shared Bedrock disable and re-enable")
        policy = self.enforcement_policy()
        policy["network_integrations"].pop("bedrock")
        self._api("PUT", "/v1/network/policy", policy)
        for runtime in ("pi", "hermes"):
            status = self._wait_for_runtime_status({"deactivated"}, runtime=runtime, timeout=90)
            if status != "deactivated":
                raise AssertionError(f"disabling Bedrock did not deactivate {runtime}: {status}")
        rows = self._ssh_code(
            "sudo -u postgres psql -tA -d trustyclaw_admin -c "
            "'SELECT count(*) FROM bedrock_credentials'"
        ).strip()
        if rows != "1":
            raise AssertionError(f"disabled Bedrock must retain its validated credential: {rows}")

        self._api("PUT", "/v1/network/policy", self.enforcement_policy())
        for runtime in ("pi", "hermes"):
            status = self._wait_for_runtime_status({"active", "error"}, runtime=runtime, timeout=180)
            if status != "active":
                raise AssertionError(f"re-enabling Bedrock did not reactivate {runtime}: {status}")
        self._ok("one provider toggle deactivated and reactivated both Bedrock harnesses")
