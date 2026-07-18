from __future__ import annotations

import subprocess
import unittest
from unittest.mock import patch

from host.runtime.admin_api import upgrade_check


class UpgradeCheckTests(unittest.TestCase):
    def setUp(self) -> None:
        upgrade_check._latest_version = None

    def test_refresh_publishes_a_newer_valid_version(self) -> None:
        proc = subprocess.CompletedProcess(upgrade_check.HELPER_COMMAND, 0, "2.4.0\n", "")
        with (
            patch("host.runtime.admin_api.upgrade_check.subprocess.run", return_value=proc) as run,
            patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value="2.3.1"),
        ):
            upgrade_check.refresh()
            result = upgrade_check.status()

        self.assertEqual(result, {"available": True, "latest": "2.4.0"})
        run.assert_called_once_with(
            upgrade_check.HELPER_COMMAND,
            check=False,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            text=True,
            timeout=upgrade_check.HELPER_TIMEOUT_SECONDS,
        )

    def test_equal_public_version_is_not_an_upgrade(self) -> None:
        upgrade_check._latest_version = "2.3.1"
        with patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value="2.3.1"):
            self.assertEqual(
                upgrade_check.status(),
                {"available": False, "latest": "2.3.1"},
            )

    def test_private_build_ahead_of_public_version_is_current(self) -> None:
        upgrade_check._latest_version = "2.2.9"
        with patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value="2.3.1"):
            self.assertEqual(
                upgrade_check.status(),
                {"available": False, "latest": "2.2.9"},
            )

    def test_failed_or_invalid_check_preserves_the_last_successful_result(self) -> None:
        upgrade_check._latest_version = "2.4.0"
        for proc in (
            subprocess.CompletedProcess(upgrade_check.HELPER_COMMAND, 22, "", "request failed"),
            subprocess.CompletedProcess(upgrade_check.HELPER_COMMAND, 0, "not-a-version\n", ""),
        ):
            with self.subTest(stdout=proc.stdout, returncode=proc.returncode):
                upgrade_check._latest_version = "2.4.0"
                with patch("host.runtime.admin_api.upgrade_check.subprocess.run", return_value=proc):
                    upgrade_check.refresh()
                with patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value="2.3.1"):
                    self.assertEqual(
                        upgrade_check.status(),
                        {"available": True, "latest": "2.4.0"},
                    )

    def test_timed_out_check_preserves_the_last_successful_result(self) -> None:
        upgrade_check._latest_version = "2.4.0"
        with patch(
            "host.runtime.admin_api.upgrade_check.subprocess.run",
            side_effect=subprocess.TimeoutExpired(upgrade_check.HELPER_COMMAND, 15),
        ):
            upgrade_check.refresh()

        with patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value="2.3.1"):
            self.assertEqual(
                upgrade_check.status(),
                {"available": True, "latest": "2.4.0"},
            )

    def test_missing_running_version_hides_a_successful_public_check(self) -> None:
        upgrade_check._latest_version = "2.4.0"
        with patch("host.runtime.admin_api.upgrade_check.read_root_version", return_value=None):
            self.assertEqual(upgrade_check.status(), {"available": False, "latest": None})

    def test_poller_checks_immediately_then_waits_four_hours(self) -> None:
        with (
            patch("host.runtime.admin_api.upgrade_check.refresh") as refresh,
            patch("host.runtime.admin_api.upgrade_check.time.sleep", side_effect=RuntimeError("stop")) as sleep,
            self.assertRaisesRegex(RuntimeError, "stop"),
        ):
            upgrade_check.poll()

        refresh.assert_called_once_with()
        sleep.assert_called_once_with(4 * 60 * 60)


if __name__ == "__main__":
    unittest.main()
