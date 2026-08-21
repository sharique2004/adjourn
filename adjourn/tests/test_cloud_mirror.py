"""Tests for adjourn.cloud_mirror.

Two things actually matter about this module and both are tested against a real
FalkorDB rather than a mock:

  1. IDEMPOTENCY. Mirroring the same meeting or the same execution twice must
     not duplicate nodes, because that is the property that makes replaying the
     backlog safe. Verified with node counts on a live local instance.

  2. THE BACKLOG PATH. An unreachable cloud must not raise and must not stall
     the Mac. Verified by pointing the module at a blackholed address and
     asserting: returns False, writes a backlog line, and comes back in under
     three seconds.

The live tests use a throwaway graph, "adjourn_mirror_test", on localhost:6379
(the `falkordb-test` container) and drop it afterwards. They skip cleanly when
no local instance is listening, so this file is safe to run anywhere.

Run from the repo root:
    python -m unittest adjourn.tests.test_cloud_mirror -v
    python adjourn/tests/test_cloud_mirror.py -v
"""

from __future__ import annotations

import importlib.util
import json
import os
import socket
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

# Load cloud_mirror.py directly by path. The rest of the adjourn package is
# being built in parallel, so we deliberately avoid importing it.
_MODULE_PATH = Path(__file__).resolve().parent.parent / "cloud_mirror.py"
_spec = importlib.util.spec_from_file_location("adjourn_cloud_mirror_undertest", _MODULE_PATH)
assert _spec and _spec.loader
cloud_mirror = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = cloud_mirror
_spec.loader.exec_module(cloud_mirror)


LOCAL_HOST = "127.0.0.1"
LOCAL_PORT = 6379
TEST_GRAPH = "adjourn_mirror_test"

# RFC 5737 / non-routable: connections here hang until the timeout fires, which
# is what makes it a good stand-in for "the wifi just died".
BLACKHOLE_HOST = "10.255.255.1"


def _local_falkordb_available() -> bool:
    try:
        with socket.create_connection((LOCAL_HOST, LOCAL_PORT), timeout=1.0):
            return True
    except OSError:
        return False


LOCAL_AVAILABLE = _local_falkordb_available()
requires_local = unittest.skipUnless(
    LOCAL_AVAILABLE, f"no FalkorDB listening on {LOCAL_HOST}:{LOCAL_PORT}"
)


class _MirrorTestCase(unittest.TestCase):
    """Redirects the backlog into a temp dir so the real state/ is never touched."""

    env: dict = {}

    def setUp(self) -> None:
        self._temporary = tempfile.TemporaryDirectory()
        state = Path(self._temporary.name)
        self._patches = [
            mock.patch.object(cloud_mirror, "STATE_DIR", state),
            # ADJOURN_STATE_DIR is the real redirect now — cloud_mirror.state_dir()
            # reads it — so setting it here exercises the same door the rest of
            # the package uses instead of a patch only this suite knows about.
            mock.patch.dict(os.environ, {"ADJOURN_STATE_DIR": str(state)}, clear=False),
            # Pin the env exactly; never let a real adjourn/.env leak in.
            mock.patch.object(cloud_mirror, "_dotenv_loaded", True),
            mock.patch.dict(os.environ, self._environment(), clear=False),
        ]
        for patch in self._patches:
            patch.start()
        self.addCleanup(self._stop_patches)
        self.addCleanup(self._temporary.cleanup)
        cloud_mirror._flush_in_progress = False

    def _environment(self) -> dict:
        base = {
            "FALKORDB_CLOUD_HOST": "",
            "FALKORDB_CLOUD_PORT": "",
            "FALKORDB_CLOUD_USERNAME": "",
            "FALKORDB_CLOUD_PASSWORD": "",
            "FALKORDB_GRAPH": TEST_GRAPH,
        }
        base.update(self.env)
        return base

    def _stop_patches(self) -> None:
        for patch in reversed(self._patches):
            patch.stop()

    # -- helpers ------------------------------------------------------------

    def backlog_lines(self) -> list[dict]:
        path = cloud_mirror.backlog_path()
        if not path.exists():
            return []
        return [json.loads(line) for line in path.read_text().splitlines() if line.strip()]


# --------------------------------------------------------------------------
# Pure logic — no network, always runs.
# --------------------------------------------------------------------------


class TestShaping(_MirrorTestCase):
    def test_undo_payload_is_flattened_to_a_json_string(self):
        captured = {}

        class FakeGraph:
            def query(self, text, params=None):
                captured.update(params or {})

        cloud_mirror._write_execution(
            FakeGraph(),
            {
                "ok": True,
                "kind": "linear_create",
                "meeting_id": "m1",
                "fired_at": "2026-08-20T10:00:00+00:00",
                "undo_payload": {"issue_id": "ENG-4", "nested": {"a": 1}},
            },
        )
        undo = captured["props"]["undo_payload"]
        self.assertIsInstance(undo, str)
        self.assertEqual(json.loads(undo), {"issue_id": "ENG-4", "nested": {"a": 1}})

    def test_missing_undo_payload_becomes_an_empty_json_object(self):
        captured = {}

        class FakeGraph:
            def query(self, text, params=None):
                captured.update(params or {})

        cloud_mirror._write_execution(
            FakeGraph(),
            {"kind": "recap_page", "meeting_id": "m1", "fired_at": "t0"},
        )
        self.assertEqual(captured["props"]["undo_payload"], "{}")

    def test_execution_without_an_idempotency_key_is_rejected(self):
        class FakeGraph:
            def query(self, text, params=None):
                raise AssertionError("should not have reached the network")

        with self.assertRaises(ValueError):
            cloud_mirror._write_execution(FakeGraph(), {"kind": "slack_send"})

    def test_statements_without_a_segment_id_are_skipped(self):
        rows = cloud_mirror._statement_rows(
            [{"segment_id": "s1", "kind": "decision"}, {"kind": "question"}, "junk"],
            "m1",
        )
        self.assertEqual([row["segment_id"] for row in rows], ["s1"])

    def test_supersedes_accepts_a_list_or_a_bare_value(self):
        edges = cloud_mirror._supersedes_rows(
            [
                {"segment_id": "s2", "supersedes": "s1"},
                {"segment_id": "s3", "supersedes": ["s1", "s2"]},
                {"segment_id": "s4", "supersedes": "s4"},  # self-edge dropped
            ]
        )
        self.assertEqual(
            edges,
            [
                {"from_id": "s2", "to_id": "s1"},
                {"from_id": "s3", "to_id": "s1"},
                {"from_id": "s3", "to_id": "s2"},
            ],
        )

    def test_tls_is_off_for_loopback_and_on_for_the_cloud(self):
        self.assertFalse(cloud_mirror.uses_tls("localhost"))
        self.assertFalse(cloud_mirror.uses_tls("127.0.0.1"))
        self.assertTrue(cloud_mirror.uses_tls("something.falkordb.cloud"))

    def test_the_timeout_budget_cannot_exceed_three_seconds(self):
        self.assertLessEqual(
            cloud_mirror.CONNECT_TIMEOUT_SECONDS + cloud_mirror.SOCKET_TIMEOUT_SECONDS,
            cloud_mirror.TOTAL_BUDGET_SECONDS,
        )


class TestUnconfigured(_MirrorTestCase):
    """No credentials yet is the normal state today: queue, never crash."""

    def test_unconfigured_queues_instead_of_failing(self):
        self.assertFalse(cloud_mirror.is_configured())
        self.assertFalse(cloud_mirror.mirror_meeting({"id": "m1", "title": "Sync"}, []))
        self.assertFalse(cloud_mirror.mirror_execution({"kind": "recap_page", "meeting_id": "m1", "fired_at": "t"}))
        self.assertEqual([entry["op"] for entry in self.backlog_lines()],
                         ["mirror_meeting", "mirror_execution"])

    def test_probe_reports_demo_mode_without_raising(self):
        reachable, message = cloud_mirror.probe()
        self.assertFalse(reachable)
        self.assertIn("not configured", message)


# --------------------------------------------------------------------------
# The backlog path — an unreachable cloud must not stall the Mac.
# --------------------------------------------------------------------------


class TestBacklogOnUnreachableHost(_MirrorTestCase):
    env = {"FALKORDB_CLOUD_HOST": BLACKHOLE_HOST, "FALKORDB_CLOUD_PORT": str(LOCAL_PORT)}

    def test_unreachable_host_writes_a_backlog_line_fast_and_without_raising(self):
        started = time.monotonic()
        result = cloud_mirror.mirror_meeting(
            {"id": "m-offline", "title": "Wifi died", "date": "2026-08-20"},
            [{"segment_id": "s1", "kind": "decision", "speaker": "Ada", "text": "ship"}],
        )
        elapsed = time.monotonic() - started

        self.assertFalse(result, "an unreachable cloud must report failure, not success")
        # Bound stated against the CONSTANT, not a copy of its value. The two drifted
        # apart once already: the budget was sized to a round 3s while the real round
        # trip measured 2.6s, so every write failed into the backlog and the graph was
        # never current. A test that hardcodes the number cannot notice that.
        self.assertLess(elapsed, cloud_mirror.TOTAL_BUDGET_SECONDS + 0.5,
                        f"blocked the pipeline for {elapsed:.2f}s")

        entries = self.backlog_lines()
        self.assertEqual(len(entries), 1)
        self.assertEqual(entries[0]["op"], "mirror_meeting")
        self.assertEqual(entries[0]["payload"]["meta"]["id"], "m-offline")
        self.assertIn("queued_at", entries[0])

    def test_execution_takes_the_same_path(self):
        started = time.monotonic()
        result = cloud_mirror.mirror_execution(
            {
                "ok": True,
                "kind": "slack_send",
                "meeting_id": "m-offline",
                "fired_at": "2026-08-20T10:00:00+00:00",
                "undo_payload": {"ts": "1"},
            }
        )
        elapsed = time.monotonic() - started
        self.assertFalse(result)
        self.assertLess(elapsed, cloud_mirror.TOTAL_BUDGET_SECONDS + 0.5,
                        f"blocked the pipeline for {elapsed:.2f}s")
        self.assertEqual(self.backlog_lines()[0]["op"], "mirror_execution")

    def test_a_queued_backlog_does_not_double_the_stall(self):
        """The opportunistic flush must not cost a second connection timeout."""
        cloud_mirror.mirror_meeting({"id": "m1"}, [])
        self.assertEqual(len(self.backlog_lines()), 1)

        started = time.monotonic()
        cloud_mirror.mirror_meeting({"id": "m2"}, [])
        elapsed = time.monotonic() - started

        self.assertLess(elapsed, cloud_mirror.TOTAL_BUDGET_SECONDS + 0.5,
                        f"flush + write cost {elapsed:.2f}s; budget is "
                        f"{cloud_mirror.TOTAL_BUDGET_SECONDS}s")
        self.assertEqual(len(self.backlog_lines()), 2)


# --------------------------------------------------------------------------
# Live writes — idempotency verified with real node counts.
# --------------------------------------------------------------------------


@requires_local
class TestLiveMirror(_MirrorTestCase):
    env = {"FALKORDB_CLOUD_HOST": LOCAL_HOST, "FALKORDB_CLOUD_PORT": str(LOCAL_PORT)}

    def setUp(self) -> None:
        super().setUp()
        self._drop_test_graph()
        self.addCleanup(self._drop_test_graph)

    @staticmethod
    def _graph():
        from falkordb import FalkorDB

        return FalkorDB(
            host=LOCAL_HOST, port=LOCAL_PORT, socket_connect_timeout=2, socket_timeout=2
        ).select_graph(TEST_GRAPH)

    def _drop_test_graph(self) -> None:
        # Belt and braces: only ever drop the throwaway graph, never "adjourn".
        assert TEST_GRAPH == "adjourn_mirror_test"
        try:
            from falkordb import FalkorDB

            db = FalkorDB(host=LOCAL_HOST, port=LOCAL_PORT, socket_connect_timeout=2)
            if TEST_GRAPH in db.list_graphs():
                db.select_graph(TEST_GRAPH).delete()
        except Exception:
            pass

    def counts(self) -> dict:
        graph = self._graph()

        def scalar(query: str) -> int:
            return int(graph.query(query).result_set[0][0])

        return {
            "Meeting": scalar("MATCH (n:Meeting) RETURN count(n)"),
            "Statement": scalar("MATCH (n:Statement) RETURN count(n)"),
            "Person": scalar("MATCH (n:Person) RETURN count(n)"),
            "Issue": scalar("MATCH (n:Issue) RETURN count(n)"),
            "Execution": scalar("MATCH (n:Execution) RETURN count(n)"),
            "SAID": scalar("MATCH ()-[r:SAID]->() RETURN count(r)"),
            "ABOUT": scalar("MATCH ()-[r:ABOUT]->() RETURN count(r)"),
            "IN_MEETING": scalar("MATCH ()-[r:IN_MEETING]->() RETURN count(r)"),
            "SUPERSEDES": scalar("MATCH ()-[r:SUPERSEDES]->() RETURN count(r)"),
            "FROM_MEETING": scalar("MATCH ()-[r:FROM_MEETING]->() RETURN count(r)"),
        }

    # -- fixtures -----------------------------------------------------------

    MEETING = {"id": "m-live-1", "title": "Weekly sync", "date": "2026-08-20"}
    STATEMENTS = [
        {
            "segment_id": "seg-1",
            "kind": "decision",
            "speaker": "Ada",
            "text": "We ship Thursday.",
            "issue": "ENG-101",
        },
        {
            "segment_id": "seg-2",
            "kind": "assignment",
            "speaker": "Bo",
            "text": "Bo takes the migration.",
            "issue": "ENG-101",
            "supersedes": ["seg-1"],
        },
        {
            "segment_id": "seg-3",
            "kind": "question",
            "speaker": "Ada",
            "text": "Who owns rollout?",
        },
    ]
    EXECUTION = {
        "ok": True,
        "kind": "linear_create",
        "external_id": "ENG-101",
        "url": "https://linear.app/x/ENG-101",
        "human_summary": "Created ENG-101",
        "mode": "sim",
        "undo_payload": {"issue_id": "ENG-101", "action": "archive"},
        "quote": "We ship Thursday.",
        "speaker": "Ada",
        "meeting_id": "m-live-1",
        "fired_at": "2026-08-20T10:00:00+00:00",
    }

    # -- tests --------------------------------------------------------------

    def test_meeting_mirror_is_idempotent(self):
        self.assertTrue(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
        first = self.counts()

        self.assertEqual(first["Meeting"], 1)
        self.assertEqual(first["Statement"], 3)
        self.assertEqual(first["Person"], 2)  # Ada, Bo
        self.assertEqual(first["Issue"], 1)  # ENG-101
        self.assertEqual(first["SAID"], 3)
        self.assertEqual(first["ABOUT"], 2)
        self.assertEqual(first["IN_MEETING"], 3)
        self.assertEqual(first["SUPERSEDES"], 1)

        self.assertTrue(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
        self.assertEqual(self.counts(), first, "second mirror duplicated graph data")

        # A third pass, to be certain nothing accumulates slowly.
        self.assertTrue(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
        self.assertEqual(self.counts(), first)

    def test_execution_mirror_is_idempotent_on_meeting_kind_fired_at(self):
        self.assertTrue(cloud_mirror.mirror_execution(self.EXECUTION))
        self.assertTrue(cloud_mirror.mirror_execution(self.EXECUTION))

        counts = self.counts()
        self.assertEqual(counts["Execution"], 1, "same receipt mirrored twice")
        self.assertEqual(counts["FROM_MEETING"], 1)
        self.assertEqual(counts["Meeting"], 1)

    def test_a_different_fired_at_is_a_different_execution(self):
        cloud_mirror.mirror_execution(self.EXECUTION)
        cloud_mirror.mirror_execution({**self.EXECUTION, "fired_at": "2026-08-20T11:00:00+00:00"})
        self.assertEqual(self.counts()["Execution"], 2)

    def test_execution_properties_survive_the_round_trip(self):
        cloud_mirror.mirror_execution(self.EXECUTION)
        row = self._graph().query(
            "MATCH (e:Execution)-[:FROM_MEETING]->(m:Meeting) "
            "RETURN e.kind, e.mode, e.ok, e.url, e.undo_payload, e.speaker, m.id"
        ).result_set[0]
        kind, mode, ok, url, undo, speaker, meeting_id = row
        self.assertEqual(kind, "linear_create")
        self.assertEqual(mode, "sim")
        self.assertTrue(ok)
        self.assertEqual(url, "https://linear.app/x/ENG-101")
        self.assertEqual(speaker, "Ada")
        self.assertEqual(meeting_id, "m-live-1")
        self.assertEqual(json.loads(undo), {"issue_id": "ENG-101", "action": "archive"})

    def test_updated_meeting_metadata_overwrites_rather_than_duplicating(self):
        cloud_mirror.mirror_meeting({"id": "m-live-1", "title": "Old"}, [])
        cloud_mirror.mirror_meeting({"id": "m-live-1", "title": "New", "date": "2026-08-20"}, [])
        rows = self._graph().query("MATCH (m:Meeting) RETURN m.id, m.title").result_set
        self.assertEqual(rows, [["m-live-1", "New"]])

    def test_meeting_with_no_statements_still_lands(self):
        self.assertTrue(cloud_mirror.mirror_meeting({"id": "m-empty", "title": "Cancelled"}, []))
        self.assertEqual(self.counts()["Meeting"], 1)

    def test_probe_reports_reachable(self):
        reachable, message = cloud_mirror.probe()
        self.assertTrue(reachable, message)
        self.assertIn("plaintext", message)


@requires_local
class TestBacklogReplay(TestLiveMirror):
    """Queue while offline, then flush once the cloud comes back."""

    env = {"FALKORDB_CLOUD_HOST": BLACKHOLE_HOST, "FALKORDB_CLOUD_PORT": str(LOCAL_PORT)}

    def _go_online(self):
        return mock.patch.dict(os.environ, {"FALKORDB_CLOUD_HOST": LOCAL_HOST})

    def test_queued_writes_land_once_the_cloud_returns(self):
        self.assertFalse(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
        self.assertFalse(cloud_mirror.mirror_execution(self.EXECUTION))
        self.assertEqual(len(self.backlog_lines()), 2)

        with self._go_online():
            self.assertEqual(cloud_mirror.flush_backlog(), 2)

        self.assertEqual(self.backlog_lines(), [], "backlog should be drained")
        counts = self.counts()
        self.assertEqual(counts["Meeting"], 1)
        self.assertEqual(counts["Statement"], 3)
        self.assertEqual(counts["Execution"], 1)

    def test_replaying_data_that_already_landed_is_a_no_op(self):
        with self._go_online():
            self.assertTrue(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
            before = self.counts()

        # Same meeting queued again while offline, then replayed.
        self.assertFalse(cloud_mirror.mirror_meeting(self.MEETING, self.STATEMENTS))
        with self._go_online():
            self.assertEqual(cloud_mirror.flush_backlog(), 1)

        self.assertEqual(self.counts(), before, "replay duplicated graph data")

    def test_flush_is_bounded_per_pass(self):
        for index in range(5):
            cloud_mirror.mirror_meeting({"id": f"m-{index}"}, [])
        self.assertEqual(len(self.backlog_lines()), 5)

        with self._go_online():
            self.assertEqual(cloud_mirror.flush_backlog(limit=2), 2)
        self.assertEqual(len(self.backlog_lines()), 3, "unflushed entries must be kept")

        with self._go_online():
            cloud_mirror.flush_backlog()
        self.assertEqual(self.backlog_lines(), [])
        self.assertEqual(self.counts()["Meeting"], 5)

    def test_flush_while_still_offline_keeps_everything(self):
        cloud_mirror.mirror_meeting(self.MEETING, [])
        self.assertEqual(cloud_mirror.flush_backlog(), 0)
        self.assertEqual(len(self.backlog_lines()), 1, "a failed flush must not drop data")

    def test_an_undeliverable_entry_does_not_wedge_the_queue(self):
        """A malformed payload gets dropped so good entries behind it still land."""
        cloud_mirror._append_backlog("mirror_meeting", {"meta": {}, "statements": []})  # no id
        cloud_mirror._append_backlog("who_knows", {"anything": 1})  # unknown op
        cloud_mirror._append_backlog("mirror_meeting", {"meta": {"id": "m-good"}, "statements": []})

        with self._go_online():
            self.assertEqual(cloud_mirror.flush_backlog(), 3)

        self.assertEqual(self.backlog_lines(), [])
        rows = self._graph().query("MATCH (m:Meeting) RETURN m.id").result_set
        self.assertEqual(rows, [["m-good"]])

    # Inherited live tests would re-run against the blackhole host; skip them.
    test_meeting_mirror_is_idempotent = None
    test_execution_mirror_is_idempotent_on_meeting_kind_fired_at = None
    test_a_different_fired_at_is_a_different_execution = None
    test_execution_properties_survive_the_round_trip = None
    test_updated_meeting_metadata_overwrites_rather_than_duplicating = None
    test_meeting_with_no_statements_still_lands = None
    test_probe_reports_reachable = None


class TestContract(unittest.TestCase):
    """The shared data contract with the Mac side, pinned so drift is loud."""

    def test_execution_kinds_match_the_contract(self):
        self.assertEqual(
            set(cloud_mirror.EXECUTION_KINDS),
            {
                "github_update",
                "linear_create",
                "linear_move",
                "pull_request_stub",
                "slack_send",
                "email_send",
                "calendar_hold",
                "recap_page",
            },
        )

    def test_statement_kinds_match_the_contract(self):
        self.assertEqual(
            set(cloud_mirror.STATEMENT_KINDS),
            {
                "decision",
                "update",
                "assignment",
                "question",
                "ticket_request",
                "progress_report",
                "message_commitment",
                "email_commitment",
                "deadline",
                "pr_intent",
            },
        )

    def test_every_executor_result_field_is_mirrored(self):
        for field in (
            "ok", "kind", "external_id", "url", "human_summary",
            "mode", "quote", "speaker", "meeting_id", "fired_at",
        ):
            self.assertIn(field, cloud_mirror.EXECUTION_FIELDS)

    def test_default_graph_name(self):
        self.assertEqual(cloud_mirror.DEFAULT_GRAPH, "adjourn")


if __name__ == "__main__":
    unittest.main(verbosity=2)
