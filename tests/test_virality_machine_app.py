"""Virality Machine app backend tests.

The pure-logic tests cover the domain contract layered on the shared engine: the
``upsert_render_job`` action shape (fixed enums, encoded-byte bounds, slug id),
the daily trend-scan timing, and the setup brief. The database-backed tests run
both app migrations on the scratch cluster and exercise the render queue
end-to-end: the seed inventory and schedule, upserts through the /agent/ surface,
the operator ``GET /render_jobs`` read route, the row cap, and the in-flight
digest section, against a stubbed host admin API.
"""

from __future__ import annotations

from http import HTTPStatus
import importlib.util
from pathlib import Path
from typing import Any
import unittest
from unittest.mock import patch

import pg_harness

from host.apps.workspace_kit import engine
from host.runtime import app_migrate, app_platform, db, migrate

REPO_ROOT = Path(__file__).resolve().parents[1]


def _load_virality_machine_backend() -> Any:
    spec = importlib.util.spec_from_file_location(
        "virality_machine_backend", REPO_ROOT / "host" / "apps" / "virality_machine" / "backend.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


rb = _load_virality_machine_backend()
engine.configure(rb.CONFIG)
backend = engine

CODEX_SETTINGS = {"agent_runtime": "codex", "model": "gpt-5.6-terra", "effort": "high"}


def first_message(content: str) -> dict[str, str]:
    return {"content": content, **CODEX_SETTINGS}


def _valid_job(**overrides: Any) -> dict[str, Any]:
    job = {
        "action": "upsert_render_job",
        "id": "shot_1",
        "kind": "video",
        "prompt": "A neon skyline timelapse, portrait 720x1280",
        "status": "running",
        "task_id": "runway_task_abc123",
    }
    job.update(overrides)
    return job


class UpsertRenderJobShapeTests(unittest.TestCase):
    def setUp(self) -> None:
        # The engine is a process-wide singleton; sibling app test modules
        # configure it during collection, so reassert Virality Machine's config
        # before each shape test rather than relying on the import-time call.
        backend.configure(rb.CONFIG)

    def test_valid_job_passes(self) -> None:
        self.assertIsNone(backend.validate_action_shape(_valid_job()))
        self.assertIsNone(backend.validate_action_shape(_valid_job(status="succeeded", output_url="https://cdn.example/x.mp4")))
        # task_id and output_url are optional.
        minimal = {"action": "upsert_render_job", "id": "shot_2", "kind": "image", "prompt": "poster", "status": "pending"}
        self.assertIsNone(backend.validate_action_shape(minimal))

    def test_action_is_registered_and_listed(self) -> None:
        self.assertIn("upsert_render_job", backend._DOMAIN_ACTIONS)
        error = backend.validate_action_shape({"action": "not_a_real_action"})
        self.assertIn("upsert_render_job", error or "")

    def test_missing_and_unknown_fields(self) -> None:
        self.assertIn("missing required field", backend.validate_action_shape({"action": "upsert_render_job", "id": "x"}) or "")
        self.assertIn("unsupported field", backend.validate_action_shape(_valid_job(surprise=1)) or "")

    def test_id_must_be_a_slug(self) -> None:
        self.assertIn("id must match", backend.validate_action_shape(_valid_job(id="Bad Slug")) or "")

    def test_kind_and_status_are_fixed_enums(self) -> None:
        self.assertIn("kind must be one of", backend.validate_action_shape(_valid_job(kind="gif")) or "")
        self.assertIn("status must be one of", backend.validate_action_shape(_valid_job(status="done")) or "")

    def test_text_fields_are_bounded_by_encoded_bytes(self) -> None:
        self.assertIn(
            "prompt must be at most",
            backend.validate_action_shape(_valid_job(prompt="x" * (rb.RENDER_JOB_PROMPT_BYTES + 1))) or "",
        )
        self.assertIn(
            "output_url must be at most",
            backend.validate_action_shape(_valid_job(output_url="https://x/" + "y" * rb.RENDER_JOB_URL_BYTES)) or "",
        )
        self.assertIn(
            "task_id must be at most",
            backend.validate_action_shape(_valid_job(task_id="t" * (rb.RENDER_JOB_TASK_ID_BYTES + 1))) or "",
        )
        # A multi-byte character counts by its encoded size, not one char.
        emoji_prompt = "\U0001f3ac" * ((rb.RENDER_JOB_PROMPT_BYTES // 12) + 5)
        self.assertIn("prompt must be at most", backend.validate_action_shape(_valid_job(prompt=emoji_prompt)) or "")

    def test_prompt_must_not_be_empty(self) -> None:
        self.assertIn("prompt must not be empty", backend.validate_action_shape(_valid_job(prompt="   ")) or "")


class TrendScanTests(unittest.TestCase):
    def test_next_trend_run_is_the_next_utc_trend_hour(self) -> None:
        import calendar

        before = calendar.timegm((2026, 7, 9, rb.TREND_HOUR_UTC - 1, 30, 0, 0, 0, 0))
        after = calendar.timegm((2026, 7, 9, rb.TREND_HOUR_UTC, 0, 1, 0, 0, 0))
        self.assertEqual(backend.format_utc(rb._next_trend_epoch(before)), f"2026-07-09T{rb.TREND_HOUR_UTC:02d}:00:00Z")
        self.assertEqual(backend.format_utc(rb._next_trend_epoch(after)), f"2026-07-10T{rb.TREND_HOUR_UTC:02d}:00:00Z")

    def test_trend_prompt_fits_the_schedule_prompt_cap(self) -> None:
        self.assertLessEqual(len(rb.TREND_PROMPT), backend.SCHEDULE_PROMPT_LIMIT)

    def test_setup_brief_is_domain_specific(self) -> None:
        self.assertIn("Virality Machine", rb.CONFIG.setup_brief)
        self.assertIn("existing creation goal", rb.CONFIG.setup_brief)


class RenderJobsDbTests(unittest.TestCase):
    DB_NAME = "trustyclaw_virality_machine_test"
    _initialized = False

    def setUp(self) -> None:
        backend.configure(rb.CONFIG)
        pg_harness.ensure_database()
        if not RenderJobsDbTests._initialized:
            pg_harness.create_database(self.DB_NAME)
        self.env_patch = patch.dict("os.environ", {"TRUSTYCLAW_DB_NAME": self.DB_NAME})
        self.env_patch.start()
        self.addCleanup(self.env_patch.stop)
        self.addCleanup(db.close_pool)
        if not RenderJobsDbTests._initialized:
            migrate.up(quiet=True)
            with db.transaction() as cur:
                cur.execute(
                    """
                    DO $$
                    BEGIN
                      IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname = 'trustyclaw-app-virality_machine') THEN
                        CREATE ROLE "trustyclaw-app-virality_machine" LOGIN;
                      END IF;
                    END
                    $$;
                    """
                )
                cur.execute('CREATE SCHEMA IF NOT EXISTS app_virality_machine AUTHORIZATION "trustyclaw-app-virality_machine"')
            app = app_platform.app_by_id("virality_machine")
            assert app is not None
            for version in app_migrate.pending(app.id):
                app_migrate.apply_sql(app.id, version, connection_user=app.db_role)
                app_migrate.record(app.id, version)
            RenderJobsDbTests._initialized = True
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_virality_machine")
            cur.execute(
                "TRUNCATE workspace, messages, runs, schedules, artifacts, memories, tools, render_jobs"
                " RESTART IDENTITY CASCADE"
            )
        backend._DISPATCH_ATTEMPTS.clear()
        backend._AGENT_ACTIONS_BY_TASK.clear()
        self.host_responses: dict[str, Any] = {}
        self.host_calls: list[tuple[str, str, Any]] = []

        def fake_admin_api(method: str, path: str, body: Any = None) -> dict[str, Any]:
            self.host_calls.append((method, path, body))
            key = f"{method} {path}"
            response = self.host_responses.get(key)
            if response is None:
                raise backend.AppError(HTTPStatus.NOT_FOUND, f"unstubbed host call: {key}")
            if isinstance(response, Exception):
                raise response
            return response

        self.api_patch = patch.object(backend, "call_admin_api", side_effect=fake_admin_api)
        self.api_patch.start()
        self.addCleanup(self.api_patch.stop)

    def _current_thread(self) -> str:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_virality_machine")
            cur.execute("SELECT thread_seq FROM workspace WHERE singleton = TRUE")
            row = cur.fetchone()
        return f"ws-{row[0] if row else 1}"

    def _start_turn(self, task_id: str = "task_1") -> None:
        backend.send_message(first_message("build a reel"))
        self.host_responses["POST /v1/tasks"] = {"task_id": task_id, "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()

    def _agent_action(self, action: dict[str, Any]) -> dict[str, Any]:
        return backend.route_agent_request("POST", "/agent/actions", action, thread_id=self._current_thread())

    def _agent_action_error(self, action: dict[str, Any]) -> tuple[int, str]:
        try:
            self._agent_action(action)
        except backend.AppError as exc:
            return int(exc.status), exc.message
        raise AssertionError(f"action unexpectedly applied: {action}")

    def _messages(self) -> list[tuple[str, str]]:
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_virality_machine")
            cur.execute("SELECT role, content FROM messages ORDER BY id ASC")
            return cur.fetchall()

    def test_seed_records_tools_inventory_and_daily_trend_scan(self) -> None:
        backend.send_message(first_message("hello"))
        snapshot = backend.workspace_snapshot()
        self.assertEqual(snapshot["workspace"]["goal"], rb.DEFAULT_GOAL)
        self.assertEqual(snapshot["workspace"]["measurement"], rb.DEFAULT_MEASUREMENT)
        tool_ids = {tool["tool_id"] for tool in snapshot["tools"]}
        self.assertEqual(tool_ids, {"runway", "instagram", "instagram_discovery", "twitter", "brave_search"})
        runway = next(tool for tool in snapshot["tools"] if tool["tool_id"] == "runway")
        self.assertEqual((runway["priority"], runway["status"]), ("must_have", "implemented"))
        brave = next(tool for tool in snapshot["tools"] if tool["tool_id"] == "brave_search")
        self.assertEqual((brave["priority"], brave["status"]), ("good_to_have", "implemented"))
        trend = next(s for s in snapshot["schedules"] if s["schedule_id"] == rb.TREND_SCHEDULE_ID)
        self.assertEqual(trend["every_minutes"], 24 * 60)
        # Enabled from activation: the activation screen disclosed the scan.
        self.assertTrue(trend["enabled"])
        self.assertTrue(trend["next_run_at"].endswith(f"T{rb.TREND_HOUR_UTC:02d}:00:00Z"))

    def test_upsert_render_job_records_advances_and_reads(self) -> None:
        self._start_turn()
        self._agent_action(_valid_job(id="shot_1", status="running", task_id="runway_1"))
        # A second upsert of the same id advances it to a terminal URL.
        applied = self._agent_action(
            _valid_job(id="shot_1", status="succeeded", task_id="runway_1", output_url="https://cdn.example/reel.mp4")
        )
        self.assertEqual(applied, {"applied": True, "action": "upsert_render_job"})
        result = backend.route_ui_request("GET", "/render_jobs", None)
        self.assertEqual(result["total"], 1)
        self.assertEqual(result["max"], rb.MAX_RENDER_JOBS)
        job = result["render_jobs"][0]
        self.assertEqual(job["id"], "shot_1")
        self.assertEqual(job["status"], "succeeded")
        self.assertEqual(job["output_url"], "https://cdn.example/reel.mp4")
        self.assertFalse(job["prompt_truncated"])
        # The upsert journaled both the record and the advance to the feed.
        contents = [content for _, content in self._messages()]
        self.assertTrue(any('Recorded render job "shot_1"' in c for c in contents))
        self.assertTrue(any('Updated render job "shot_1"' in c for c in contents))

    def test_partial_poll_update_preserves_task_output_and_saved_video_path(self) -> None:
        self._start_turn()
        self._agent_action(_valid_job(
            status="succeeded",
            task_id="runway_1",
            output_url="https://cdn.example/reel.mp4",
            video_path="/workspace/videos/shot-1.mp4",
        ))
        self._agent_action(_valid_job(status="succeeded", task_id=None, output_url=None))
        job = backend.route_ui_request("GET", "/render_jobs", None)["render_jobs"][0]
        self.assertEqual(job["task_id"], "runway_1")
        self.assertEqual(job["output_url"], "https://cdn.example/reel.mp4")
        self.assertEqual(job["video_path"], "/workspace/videos/shot-1.mp4")

        self._agent_action(_valid_job(status="failed", task_id="runway_2"))
        failed = backend.route_ui_request("GET", "/render_jobs", None)["render_jobs"][0]
        self.assertEqual(failed["task_id"], "runway_2")
        self.assertIsNone(failed["output_url"])
        self.assertIsNone(failed["video_path"])

    def test_render_queue_read_clips_long_prompts(self) -> None:
        self._start_turn()
        long_prompt = "p" * (rb.RENDER_QUEUE_PROMPT_CLIP + 50)
        self._agent_action(_valid_job(id="shot_1", prompt=long_prompt, status="pending"))
        job = backend.route_ui_request("GET", "/render_jobs", None)["render_jobs"][0]
        self.assertEqual(len(job["prompt"]), rb.RENDER_QUEUE_PROMPT_CLIP)
        self.assertTrue(job["prompt_truncated"])

    def test_render_job_bad_shape_is_rejected_and_journaled(self) -> None:
        self._start_turn()
        status, message = self._agent_action_error(_valid_job(status="done"))
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        self.assertIn("status must be one of", message)
        self.assertTrue(any(role == "error" and "status must be one of" in content for role, content in self._messages()))
        self.assertEqual(backend.route_ui_request("GET", "/render_jobs", None)["total"], 0)

    def test_render_job_row_cap_is_enforced(self) -> None:
        now = backend._utc_now()
        with db.transaction() as cur:
            cur.execute("SET LOCAL search_path TO app_virality_machine")
            for index in range(rb.MAX_RENDER_JOBS):
                cur.execute(
                    "INSERT INTO render_jobs (id, task_id, kind, prompt, status, created_at, updated_at)"
                    " VALUES (%s, %s, 'video', 'p', 'pending', %s, %s)",
                    (f"job_{index}", f"t{index}", now, now),
                )
        self._start_turn()
        # Advancing an existing id still works at the cap.
        self._agent_action(_valid_job(id="job_0", status="succeeded", output_url="https://cdn.example/a.mp4"))
        # A new distinct id is rejected.
        status, message = self._agent_action_error(_valid_job(id="brand_new", status="pending"))
        self.assertEqual(status, int(HTTPStatus.UNPROCESSABLE_ENTITY))
        self.assertIn("render job limit reached", message)

    def test_digest_lists_render_jobs_in_flight(self) -> None:
        self._start_turn()
        self._agent_action(_valid_job(id="shot_1", status="running", task_id="runway_1"))
        # A pending job with no task id yet exercises the "no task id" branch.
        self._agent_action({"action": "upsert_render_job", "id": "shot_2", "kind": "video", "prompt": "beach run", "status": "pending"})
        self._agent_action(_valid_job(id="shot_3", status="succeeded", output_url="https://cdn.example/done.mp4"))
        with db.transaction() as cur:
            backend._set_search_path(cur)
            sections = rb.digest_sections(cur)
        self.assertEqual(len(sections), 1)
        header, lines = sections[0]
        self.assertIn("Render jobs in flight (2)", header)
        joined = "\n".join(lines)
        self.assertIn(
            "shot_1: video, running, prompt 'A neon skyline timelapse, portrait 720x1280', task runway_1",
            joined,
        )
        self.assertIn("shot_2: video, pending, prompt 'beach run', no task id yet", joined)
        # The terminal job is not listed as in flight.
        self.assertNotIn("shot_3", joined)

    def test_dispatch_appends_the_render_digest_section(self) -> None:
        self._start_turn()
        self._agent_action(_valid_job(id="shot_1", status="running", task_id="runway_1"))
        self.host_responses["GET /v1/tasks/task_1"] = {"task_id": "task_1", "status": "completed", "output_message": "ok"}
        backend._reap_active_runs()
        backend.send_message({"content": "keep going"})
        self.host_responses["POST /v1/tasks"] = {"task_id": "task_2", "thread_id": "ws-1", "agent_runtime": "codex", "status": "queued"}
        backend._dispatch_pending_runs()
        _, _, body = self.host_calls[-1]
        # The composed input for the next turn carries the in-flight render job.
        self.assertIn("Render jobs in flight (1)", body["input_message"])
        self.assertIn(
            "shot_1: video, running, prompt 'A neon skyline timelapse, portrait 720x1280', task runway_1",
            body["input_message"],
        )
        self.assertLessEqual(len(body["input_message"]), backend.HOST_INPUT_LIMIT)

    def test_render_jobs_route_only_matches_its_path(self) -> None:
        self.assertIsNone(rb.domain_ui_routes("POST", "/render_jobs", None, {}))
        self.assertIsNone(rb.domain_ui_routes("GET", "/other", None, {}))
        with self.assertRaises(backend.AppError) as caught:
            backend.route_ui_request("GET", "/nope", None)
        self.assertEqual(caught.exception.status, HTTPStatus.NOT_FOUND)

    def test_agent_route_lists_every_active_job(self) -> None:
        self._start_turn()
        for index in range(15):
            self._agent_action(_valid_job(id=f"job_{index}", task_id=f"task_{index}"))
        result = rb.domain_agent_routes("GET", ["agent", "render_jobs"], None, "task_1")
        assert result is not None
        self.assertEqual(len(result["render_jobs"]), 15)
        self.assertEqual(
            result["render_jobs"][0]["prompt"],
            "A neon skyline timelapse, portrait 720x1280",
        )


if __name__ == "__main__":
    unittest.main()
