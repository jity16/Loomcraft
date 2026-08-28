"""Session zones, source-ref safety, and event-log integrity."""

from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

import loomcraft as lc
from loomcraft.errors import EventLogError, SourceError, SourceIntegrityError
from loomcraft.events import EventLog


class SessionCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.root = Path(self._tmp.name)
        self.store = lc.SessionStore(self.root / "sessions")
        self.session = self.store.create("s1")

    def tearDown(self):
        self._tmp.cleanup()


class TestUploads(SessionCase):
    def test_upload_records_size_and_checksum(self):
        payload = b"id,value\n1,2\n"
        record = self.session.save_upload("data.csv", payload)
        self.assertEqual(record["size"], len(payload))
        self.assertEqual(len(record["checksum"]), 64)
        self.assertTrue(record["source_ref"].startswith("upload:"))

    def test_upload_accepts_a_stream(self):
        import io

        record = self.session.save_upload("big.bin", io.BytesIO(b"x" * 5000))
        self.assertEqual(record["size"], 5000)

    def test_empty_upload_is_rejected(self):
        with self.assertRaises(SourceError):
            self.session.save_upload("empty.txt", b"")

    def test_oversized_upload_is_rejected_and_cleaned_up(self):
        session = lc.Session("small", self.root / "small", max_upload_bytes=10)
        with self.assertRaises(SourceError):
            session.save_upload("big.txt", b"x" * 100)
        self.assertEqual(session.list_uploads(), [])

    def test_filename_is_sanitised(self):
        record = self.session.save_upload("../../etc/passwd", b"root:x")
        self.assertNotIn("/", record["filename"])
        self.assertNotIn("..", record["filename"])

    def test_delete_removes_the_record_and_the_bytes(self):
        record = self.session.save_upload("gone.txt", b"bye")
        self.session.delete_upload(record["id"])
        self.assertEqual(self.session.list_uploads(), [])
        with self.assertRaises(SourceError):
            self.session.resolve_source(record["source_ref"])


class TestSourceResolution(SessionCase):
    def test_resolves_an_upload(self):
        record = self.session.save_upload("a.txt", b"hello")
        resolved = self.session.resolve_source(record["source_ref"])
        self.assertEqual(resolved.kind, "upload")
        self.assertEqual(resolved.path.read_bytes(), b"hello")

    def test_resolves_scratch_files(self):
        (self.session.scratch_dir / "note.md").write_text("# note")
        resolved = self.session.resolve_source("scratch:note.md")
        self.assertEqual(resolved.kind, "scratch")

    def test_rejects_traversal_in_scratch(self):
        with self.assertRaises(SourceError):
            self.session.resolve_source("scratch:../control/plan.json")

    def test_rejects_absolute_scratch_paths(self):
        with self.assertRaises(SourceError):
            self.session.resolve_source("scratch:/etc/passwd")

    def test_rejects_a_symlink_escaping_the_session(self):
        link = self.session.scratch_dir / "escape.txt"
        try:
            link.symlink_to("/etc/hostname")
        except (OSError, NotImplementedError):
            self.skipTest("symlinks unavailable on this platform")
        with self.assertRaises(SourceError):
            self.session.resolve_source("scratch:escape.txt")

    def test_rejects_malformed_refs(self):
        for value in ["", "nope", "upload:", "wat:thing", 42]:
            with self.assertRaises(SourceError):
                self.session.resolve_source(value)  # type: ignore[arg-type]

    def test_detects_content_swapped_after_ingest(self):
        record = self.session.save_upload("a.txt", b"original")
        path = self.session.uploads_dir / record["id"] / record["filename"]
        path.write_bytes(b"tampered!")
        with self.assertRaises(SourceIntegrityError):
            self.session.resolve_source(record["source_ref"])


class TestArtifacts(SessionCase):
    def test_scratch_promotion_copies_bytes_out(self):
        (self.session.scratch_dir / "out.csv").write_text("id\n1\n")
        registered = self.session.register_scratch_artifacts([{"path": "out.csv"}])
        self.assertEqual(len(registered), 1)
        stored = self.session.root / registered[0]["relpath"]
        self.assertTrue(stored.is_file())
        self.assertNotEqual(stored.parent, self.session.scratch_dir)

    def test_scratch_prefix_is_accepted(self):
        (self.session.scratch_dir / "a.txt").write_text("a")
        registered = self.session.register_scratch_artifacts([{"path": "scratch/a.txt"}])
        self.assertEqual(registered[0]["filename"], "a.txt")

    def test_batch_size_is_bounded(self):
        for index in range(13):
            (self.session.scratch_dir / f"f{index}.txt").write_text("x")
        with self.assertRaises(lc.ArtifactError):
            self.session.register_scratch_artifacts(
                [{"path": f"f{index}.txt"} for index in range(13)]
            )

    def test_empty_batch_is_rejected(self):
        with self.assertRaises(lc.ArtifactError):
            self.session.register_scratch_artifacts([])

    def test_registered_artifact_is_resolvable_as_a_source(self):
        (self.session.scratch_dir / "r.txt").write_text("result")
        registered = self.session.register_scratch_artifacts([{"path": "r.txt"}])
        resolved = self.session.resolve_source(registered[0]["source_ref"])
        self.assertEqual(resolved.path.read_text(), "result")


class TestPlanPersistence(SessionCase):
    def test_history_retains_every_revision(self):
        self.session.publish_plan({"goal": "g", "revision": 1, "steps": []})
        self.session.publish_plan({"goal": "g", "revision": 2, "steps": []})
        self.assertEqual(
            [row["revision"] for row in self.session.plan_history()], [1, 2]
        )
        self.assertEqual(self.session.current_plan()["revision"], 2)

    def test_state_updates_rewrite_the_matching_revision_only(self):
        self.session.publish_plan({"goal": "g", "revision": 1, "steps": ["a"]})
        self.session.publish_plan({"goal": "g", "revision": 2, "steps": ["b"]})
        self.session.update_current_plan({"goal": "g", "revision": 2, "steps": ["b2"]})
        history = {row["revision"]: row for row in self.session.plan_history()}
        self.assertEqual(history[1]["steps"], ["a"])
        self.assertEqual(history[2]["steps"], ["b2"])


class TestEventLog(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.path = Path(self._tmp.name) / "events.jsonl"
        self.log = EventLog(self.path)

    def tearDown(self):
        self._tmp.cleanup()

    def test_sequence_numbers_are_dense_and_monotonic(self):
        for index in range(5):
            self.log.append("notice", {"index": index})
        self.assertEqual([event.seq for event in self.log.read()], [1, 2, 3, 4, 5])

    def test_after_seq_resumes_cleanly(self):
        for index in range(5):
            self.log.append("notice", {"index": index})
        resumed = self.log.read(after_seq=3)
        self.assertEqual([event.seq for event in resumed], [4, 5])

    def test_chain_verifies(self):
        for index in range(3):
            self.log.append("notice", {"index": index})
        self.assertTrue(self.log.verify())

    def test_tampering_with_a_line_breaks_verification(self):
        self.log.append("notice", {"index": 0})
        self.log.append("notice", {"index": 1})
        lines = self.path.read_text().splitlines()
        row = json.loads(lines[0])
        row["data"]["index"] = 999
        lines[0] = json.dumps(row, ensure_ascii=False)
        self.path.write_text("\n".join(lines) + "\n")
        self.assertFalse(self.log.verify())

    def test_cursor_is_rebuilt_when_the_sidecar_is_lost(self):
        self.log.append("notice", {"index": 0})
        self.log.append("notice", {"index": 1})
        self.log.cursor_path.unlink()
        third = self.log.append("notice", {"index": 2})
        self.assertEqual(third.seq, 3, "recovery must not restart numbering")
        self.assertTrue(self.log.verify())

    def test_non_contiguous_sequence_fails_closed(self):
        self.log.append("notice", {"index": 0})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"seq": 99, "event": "notice", "data": {}, "ts": ""}) + "\n")
        self.log.cursor_path.unlink()
        with self.assertRaises(EventLogError):
            self.log.append("notice", {"index": 1})

    def test_partial_trailing_line_fails_closed(self):
        self.log.append("notice", {"index": 0})
        with self.path.open("a", encoding="utf-8") as handle:
            handle.write('{"seq": 2, "event": "notice"')
        self.log.cursor_path.unlink()
        with self.assertRaises(EventLogError):
            self.log.append("notice", {})

    def test_subscribers_see_appended_events(self):
        seen: list[lc.Event] = []
        unsubscribe = self.log.subscribe(seen.append)
        self.log.append("notice", {"a": 1})
        unsubscribe()
        self.log.append("notice", {"a": 2})
        self.assertEqual(len(seen), 1)

    def test_a_raising_subscriber_does_not_break_the_log(self):
        def bad(_: lc.Event) -> None:
            raise RuntimeError("listener exploded")

        self.log.subscribe(bad)
        event = self.log.append("notice", {})
        self.assertEqual(event.seq, 1)
        self.assertTrue(self.log.verify())

    def test_sse_frame_shape(self):
        event = self.log.append("plan_published", {"plan": {"revision": 1}})
        frame = event.sse()
        self.assertTrue(frame.startswith("event: plan_published\ndata: {"))
        self.assertTrue(frame.endswith("\n\n"))

    def test_memory_log_matches_the_durable_api(self):
        log = lc.MemoryEventLog()
        log.append("notice", {"a": 1})
        log.append("notice", {"a": 2})
        self.assertEqual([event.seq for event in log.read()], [1, 2])
        self.assertEqual(log.last_seq, 2)
        self.assertTrue(log.verify())


class TestSessionStore(SessionCase):
    def test_session_ids_are_collision_safe(self):
        with self.assertRaises(SourceError):
            self.store.create("../s1")
        with self.assertRaises(SourceError):
            self.store.create("s1")
        self.assertIsNone(self.store.get("../s1"))

    def test_get_or_create_is_idempotent(self):
        first = self.store.get_or_create("shared")
        second = self.store.get_or_create("shared")
        self.assertEqual(first.id, second.id)

    def test_history_snapshot_has_every_section(self):
        self.session.save_upload("a.txt", b"a")
        self.session.publish_plan({"goal": "g", "revision": 1, "steps": []})
        self.session.emit("notice", {"message": "hi"})
        history = self.session.history()
        for key in ("session", "current_plan", "plans", "events", "uploads", "artifacts"):
            self.assertIn(key, history)
        self.assertEqual(len(history["uploads"]), 1)
        self.assertEqual(len(history["events"]), 1)

    def test_history_artifacts_do_not_expose_storage_relative_paths(self):
        (self.session.scratch_dir / "out.txt").write_text("out")
        artifact = self.session.register_scratch_artifacts([{"path": "out.txt"}])[0]
        self.session.update_current_plan(
            {
                "goal": "g",
                "revision": 1,
                "steps": [
                    {
                        "id": "out",
                        "title": "Out",
                        "kind": "dynamic",
                        "execution": {"artifacts": [artifact]},
                    }
                ],
            }
        )
        history = self.session.history()
        self.assertNotIn("relpath", history["artifacts"][0])
        self.assertNotIn("relpath", history["current_plan"]["steps"][0]["execution"]["artifacts"][0])

    def test_delete_removes_the_directory(self):
        self.assertTrue(self.store.delete("s1"))
        self.assertIsNone(self.store.get("s1"))


if __name__ == "__main__":
    unittest.main()
