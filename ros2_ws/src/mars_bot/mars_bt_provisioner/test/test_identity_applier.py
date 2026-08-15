# SPDX-License-Identifier: Apache-2.0
# Copyright (c) 2026 Innate Inc
"""
Tests for the root identity helper (scripts/identity/).

The BLE layer validates a blob and then forwards it unparsed, so these cover the half
that actually writes: what /etc/innate.env ends up holding, what never lands in it, and
the refusal that gates a second write.
"""

import importlib.machinery
import importlib.util
import json
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest
from conftest import SERVICE_KEY, VALID_ENV

REPO_ROOT = Path(__file__).resolve().parents[5]
IDENTITY_DIR = REPO_ROOT / "scripts" / "identity"


def _load(name: str):
    """Import a dash-named, extension-less script as a module."""
    path = str(IDENTITY_DIR / name)
    loader = importlib.machinery.SourceFileLoader(name.replace("-", "_"), path)
    spec = importlib.util.spec_from_loader(loader.name, loader)
    module = importlib.util.module_from_spec(spec)
    loader.exec_module(module)
    return module


applier = _load("innate-identity")

MODULE_SERIAL = "1424523016164"
SHORT_ID = "872f"  # what applier.short_id derives from MODULE_SERIAL


@pytest.fixture
def robot(tmp_path):
    """An applier pointed at a throwaway rootfs, with root-only calls stubbed out.

    `systemctl` stands in for the real one and answers yes to everything, so ros-app reads
    as running — the state the ordering rules exist for.
    """
    repo = tmp_path / "innate-os"
    (repo / "data").mkdir(parents=True)
    (repo / "data" / "robot_info.json").write_text('{"robot_name": "MARS"}')
    system_env = tmp_path / "innate.env"

    with (
        patch.object(applier, "SYSTEM_ENV_PATH", system_env),
        patch.object(applier, "BACKUP_ENV_PATH", tmp_path / "innate.env.bak"),
        patch.object(applier, "MIGRATED_ENV_PATH", tmp_path / "innate_migrated.env"),
        patch.object(applier, "REPO_ROOT", repo),
        patch.object(applier, "systemctl", return_value=True) as systemctl,
        patch.object(applier, "schedule_reboot") as schedule_reboot,
        patch.object(applier, "module_serial", return_value=MODULE_SERIAL),
        patch.object(applier, "set_password") as set_password,
        patch.object(applier.os, "chown"),
    ):
        yield type(
            "Robot",
            (),
            {
                "repo": repo,
                "env_path": system_env,
                "set_password": set_password,
                "systemctl": systemctl,
                "reboot": schedule_reboot,
                "info": repo / "data" / "robot_info.json",
                "units": lambda: [call.args for call in systemctl.call_args_list],
            },
        )


# ---------------------------------------------------------------------------
# the short id
# ---------------------------------------------------------------------------


class TestShortId:
    def test_serial_derives_four_stable_hex_chars(self):
        first = applier.short_id(MODULE_SERIAL)
        assert first == applier.short_id(MODULE_SERIAL)
        assert len(first) == 4
        assert all(c in "0123456789abcdef" for c in first)

    def test_reads_the_device_tree_serial_stripping_the_nul(self, tmp_path):
        serial_file = tmp_path / "serial-number"
        serial_file.write_bytes(MODULE_SERIAL.encode() + b"\x00")

        with patch.object(applier, "SERIAL_PATH", serial_file):
            assert applier.module_serial() == MODULE_SERIAL

    def test_missing_device_tree_reads_as_no_serial(self, tmp_path):
        with patch.object(applier, "SERIAL_PATH", tmp_path / "absent"):
            assert applier.module_serial() is None


# ---------------------------------------------------------------------------
# --write
# ---------------------------------------------------------------------------


class TestWrite:
    def test_writes_identity_and_a_byte_identical_backup(self, robot, capsys):
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0

        written = robot.env_path.read_text()
        assert f"INNATE_SERVICE_KEY={SERVICE_KEY}" in written
        assert f"MODULE_SERIAL={MODULE_SERIAL}" in written
        assert written == applier.BACKUP_ENV_PATH.read_text()
        assert robot.env_path.stat().st_mode & 0o777 == 0o640

        # The password rides its own field and is applied, never stored.
        assert "goodbot41" not in written
        robot.set_password.assert_called_once()
        assert robot.set_password.call_args[0][1] == "goodbot41"

        applied = json.loads(capsys.readouterr().out.strip().splitlines()[-1])
        assert applied == {"robot_id": "R7-41", "robot_name": "MARS the 41st"}

    def test_it_provisions_outright(self, robot):
        """One command is the whole job — identity, teardown, wipe, reboot — so the pipe
        over ssh leaves the same robot the BLE flow does."""
        (robot.repo / "data" / "maps").mkdir()
        (robot.repo / "data" / "maps" / "bench.yaml").write_text("bench")

        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41", "wipe_data": True}) == 0

        assert not robot.info.exists()
        assert list((robot.repo / "data" / "maps").iterdir()) == []
        robot.reboot.assert_called_once()

    def test_ros_app_goes_down_before_the_derived_file_and_stays_down(self, robot):
        """A live ros-app re-creates robot_info.json from its launch-time environment
        within a second, so a reset that does not stop it first is undone — and the
        reboot, not a restart, is what brings the robot back under the new name."""
        seen = []
        robot.systemctl.side_effect = lambda *args: seen.append((args[0], robot.info.exists())) or True

        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0

        assert ("stop", True) in seen
        assert not robot.info.exists()
        assert "start" not in [action for action, _ in seen]

    def test_the_identity_is_on_stdout_before_the_reboot_is_scheduled(self, robot, capsys):
        """The caller's answer — a BLE notification, ssh's exit status — has to be out
        before the link dies."""
        printed = []
        robot.reboot.side_effect = lambda: printed.append(capsys.readouterr().out)

        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        assert json.loads(printed[0].strip().splitlines()[-1])["robot_id"] == "R7-41"

    def test_second_write_is_refused(self, robot):
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        before = robot.env_path.read_text()

        other = VALID_ENV.replace("R7-41", "R7-99")
        assert applier.run_write({"env": other, "password": "goodbot99"}) == applier.EXIT_ALREADY_PROVISIONED
        assert robot.env_path.read_text() == before

    def test_a_keyless_file_still_accepts_a_write(self, robot):
        """Deleting the key is how a robot is de-provisioned — the seeded defaults it
        leaves behind must not look provisioned."""
        robot.env_path.write_text(f"ROBOT_NAME=MARS-{SHORT_ID.upper()}\nROBOT_ID=unprovisioned-{SHORT_ID}\n")

        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        assert "R7-41" in robot.env_path.read_text()

    @pytest.mark.parametrize(
        "env",
        [
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nLD_PRELOAD=/tmp/evil.so\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nDYLD_INSERT_LIBRARIES=/tmp/evil\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nROBOT_ID=R7-41\x07\n",
            f"INNATE_SERVICE_KEY={SERVICE_KEY}\nrobot id: R7-41\n",
            "ROBOT_ID=R7-41\n",
        ],
        ids=["ld_preload", "dyld", "control_char", "malformed_line", "no_service_key"],
    )
    def test_bad_blobs_write_nothing(self, robot, env):
        with pytest.raises(applier.PayloadError):
            applier.run_write({"env": env, "password": "goodbot41"})
        assert not robot.env_path.exists()

    def test_a_failed_write_leaves_the_robot_provisionable(self, robot):
        """Writing /etc/innate.env is the commit point, so a failure short of it must
        leave nothing that would make this helper refuse the retry."""
        real_write = applier.write_root_file

        def fail_on_system_env(path, text, group):
            if path == applier.SYSTEM_ENV_PATH:
                raise OSError("no space left on device")
            real_write(path, text, group)

        with patch.object(applier, "write_root_file", side_effect=fail_on_system_env):
            with pytest.raises(OSError):
                applier.run_write({"env": VALID_ENV, "password": "goodbot41"})

        # The .bak lands first as a canary — it is why a failure here is a surprise —
        # and the retry is what repairs the run, not a rollback.
        assert applier.BACKUP_ENV_PATH.exists()
        assert not applier.has_service_key(applier.read_system_env())
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0

    def test_oversized_env_is_refused(self, robot):
        env = VALID_ENV + "PADDING=" + "x" * applier.MAX_ENV_BYTES + "\n"

        with pytest.raises(applier.PayloadError):
            applier.run_write({"env": env, "password": "goodbot41"})

    def test_stale_repo_key_is_commented_out(self, robot):
        repo_env = robot.repo / ".env"
        repo_env.write_text("INNATE_SERVICE_KEY=innate-test-key-stale\nTELEMETRY_URL=https://logs.svc.innate.bot\n")

        applier.run_write({"env": VALID_ENV, "password": "goodbot41"})

        # The repo .env outranks /etc/innate.env, so a stale key would shadow the new one.
        text = repo_env.read_text()
        assert "\n# superseded" in "\n" + text
        assert "\nINNATE_SERVICE_KEY=innate-test-key-stale" not in "\n" + text
        assert "TELEMETRY_URL=https://logs.svc.innate.bot" in text


# ---------------------------------------------------------------------------
# --unprovision
# ---------------------------------------------------------------------------


class TestUnprovision:
    def test_removes_the_key_reseeds_and_resets_the_password(self, robot, capsys):
        assert applier.run_write({"env": VALID_ENV, "password": "goodbot41"}) == 0
        robot.set_password.reset_mock()
        robot.reboot.reset_mock()

        assert applier.run_unprovision() == 0

        # The .bak holds the same key, so leaving it would not be a de-provision.
        assert not applier.BACKUP_ENV_PATH.exists()
        env = applier.read_effective_env()
        assert not applier.has_service_key(env)
        assert env["ROBOT_ID"] == f"unprovisioned-{SHORT_ID}"
        assert env["ROBOT_NAME"] == f"MARS-{SHORT_ID.upper()}-unprovisioned"
        assert not (robot.repo / "data" / "robot_info.json").exists()

        assert robot.set_password.call_args[0][1] == "goodbot"
        assert "PASSWORD" in capsys.readouterr().err

        # What comes back is a robot in the factory state, not one still running the
        # identity it just lost.
        robot.reboot.assert_called_once()

    def test_an_identity_in_the_repo_env_cannot_survive_it(self, robot):
        """The repo .env outranks /etc/innate.env, so a copy left there would keep the
        robot answering to its old name after the delete."""
        repo_env = robot.repo / ".env"
        repo_env.write_text(f"INNATE_SERVICE_KEY={SERVICE_KEY}\nROBOT_ID=R7-41\nTELEMETRY_URL=https://logs.svc\n")

        assert applier.run_unprovision() == 0

        assert "TELEMETRY_URL=https://logs.svc" in repo_env.read_text()
        env = applier.read_effective_env()
        assert not applier.has_service_key(env)
        assert env["ROBOT_ID"] == f"unprovisioned-{SHORT_ID}"

    def test_migrated_hardware_facts_survive(self, robot):
        """De-provisioning changes who the robot is, not which hardware it is."""
        applier.MIGRATED_ENV_PATH.write_text("HARDWARE_REVISION=R7\n")

        assert applier.run_unprovision() == 0
        assert applier.read_effective_env()["HARDWARE_REVISION"] == "R7"

    def test_a_robot_that_cannot_reseed_says_so_and_fails(self, robot, capsys):
        """With no serial there is no /etc/innate.env left at all, so the robot falls
        back to a bare 'MARS' — reporting success would hide that until a scan."""
        with patch.object(applier, "module_serial", return_value=None):
            assert applier.run_unprovision() == 1

        assert not robot.env_path.exists()
        # The de-provision still has to finish: the key is gone either way.
        assert robot.set_password.call_args[0][1] == "goodbot"
        assert "COULD NOT RE-SEED" in capsys.readouterr().err

    def test_a_failed_password_reset_says_so_and_fails(self, robot, capsys):
        robot.set_password.side_effect = RuntimeError("chpasswd failed")

        assert applier.run_unprovision() == 1

        # The identity is gone either way — it is deleted before the password is touched,
        # so a robot that dies here is unreachable-by-cloud, not open on a known password.
        assert not applier.has_service_key(applier.read_effective_env())
        assert "PASSWORD RESET FAILED" in capsys.readouterr().err


# ---------------------------------------------------------------------------
# --reset-info / --wipe-data
# ---------------------------------------------------------------------------


class TestHousekeeping:
    """Steps of --write, and modes of their own: re-deriving robot_info.json is equally
    useful after a rename, a hand-edited env file or a de-provision. Neither may fail a
    provisioning run, and neither reboots — that is the provisioning modes' job."""

    def test_reset_info_drops_the_derived_file_between_a_stop_and_a_start(self, robot):
        assert applier.run_reset_info() == 0

        assert not robot.info.exists()
        assert ("stop", applier.ROS_APP) in robot.units()
        assert ("start", applier.ROS_APP) in robot.units()
        # It reads robot_info.json before the environment, so it would keep advertising
        # the old name.
        assert ("restart", applier.BLE_PROVISIONER) in robot.units()
        robot.reboot.assert_not_called()

    def test_a_stopped_ros_app_is_left_stopped(self, robot):
        robot.systemctl.return_value = False

        assert applier.run_reset_info() == 0

        assert not robot.info.exists()
        assert [action for action, *_ in robot.units()] == ["is-active"]

    def test_a_failure_to_clear_never_fails_the_caller(self, robot):
        """--write calls this after the commit point, where a raise would leave a robot
        no retry can repair."""
        with patch.object(applier.Path, "unlink", side_effect=OSError("read-only file system")):
            assert applier.run_reset_info() == 0

    def test_wipe_data_takes_the_derived_identity_with_it(self, robot):
        assert applier.run_wipe_data() == 0
        assert not robot.info.exists()

    def test_wipe_data_clears_the_bench_leftovers(self, robot):
        (robot.repo / "data" / "maps").mkdir()
        (robot.repo / "data" / "maps" / "bench.yaml").write_text("bench")
        (robot.repo / "data" / ".last_map").write_text("bench")
        (robot.repo / "workspace" / "custom_skills" / "scratch").mkdir(parents=True)
        (robot.repo / "workspace" / "innate_skills").mkdir(parents=True)
        (robot.repo / "workspace" / "innate_skills" / "pick.h5").write_text("shipped")

        assert applier.run_wipe_data() == 0

        assert list((robot.repo / "data" / "maps").iterdir()) == []
        assert not (robot.repo / "data" / ".last_map").exists()
        assert list((robot.repo / "workspace" / "custom_skills").iterdir()) == []
        # Shipped assets are not user data and nothing re-downloads them post-provision.
        assert (robot.repo / "workspace" / "innate_skills" / "pick.h5").exists()


class TestPassword:
    def test_plaintext_never_reaches_argv(self):
        with patch.object(applier.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout="$6$hashed\n", stderr="")
            applier.set_password("jetson1", "goodbot41")

        openssl, chpasswd = mock_run.call_args_list
        assert "goodbot41" not in " ".join(openssl.args[0])
        assert openssl.kwargs["input"] == "goodbot41"
        assert "goodbot41" not in " ".join(chpasswd.args[0])
        assert chpasswd.kwargs["input"] == "jetson1:$6$hashed\n"


# ---------------------------------------------------------------------------
# --seed
# ---------------------------------------------------------------------------


class TestSeed:
    def test_seeds_serial_defaults_on_a_fresh_robot(self, robot):
        assert applier.run_seed() == 0

        env = applier.parse_env_text(robot.env_path.read_text())
        assert env["ROBOT_NAME"] == f"MARS-{SHORT_ID.upper()}-unprovisioned"
        assert env["ROBOT_ID"] == f"unprovisioned-{SHORT_ID}"
        assert env["MODULE_SERIAL"] == MODULE_SERIAL

    def test_leaves_a_provisioned_robot_alone(self, robot):
        """An R7.1 robot's key-only /etc/innate.env must survive the update."""
        robot.env_path.write_text(f"INNATE_SERVICE_KEY={SERVICE_KEY}\n")

        assert applier.run_seed() == 0
        assert robot.env_path.read_text() == f"INNATE_SERVICE_KEY={SERVICE_KEY}\n"

    def test_leaves_an_r7_0_robot_alone(self, robot):
        """Its key lives only in the repo .env, which outranks /etc/innate.env for
        readers — seeding unprovisioned defaults would make it lie about itself and
        reopen BLE provisioning on a robot that is already keyed."""
        (robot.repo / ".env").write_text(f"INNATE_SERVICE_KEY={SERVICE_KEY}\n")

        assert applier.run_seed() == 0
        assert not robot.env_path.exists()

    def test_never_triggers_on_the_robot_name(self, robot):
        """A user who renames their robot to exactly MARS must not be clobbered."""
        robot.env_path.write_text("ROBOT_NAME=MARS\nROBOT_ID=R7-41\n")

        assert applier.run_seed() == 0
        assert "R7-41" in robot.env_path.read_text()

    def test_refuses_to_run_without_a_module_serial(self, robot):
        """Keeps a dev machine from growing an /etc/innate.env."""
        with patch.object(applier, "module_serial", return_value=None):
            assert applier.run_seed() == 1
        assert not robot.env_path.exists()

    def test_reseeds_after_de_provisioning(self, robot):
        robot.env_path.write_text(f"ROBOT_NAME=MARS the 41st\nROBOT_ID=unprovisioned-{SHORT_ID}\n")

        assert applier.run_seed() == 0

        assert applier.parse_env_text(robot.env_path.read_text())["ROBOT_NAME"] == f"MARS-{SHORT_ID.upper()}-unprovisioned"
