import asyncio
import tempfile
import unittest
from pathlib import Path

from loomcraft import EventLog, JsonStore, LocalArtifactStore, LocalUploadStore, SourceResolutionError, SourceResolver, UploadError, encode_sse
from loomcraft.events import iter_sse
from loomcraft.artifacts import ArtifactStoreError
from loomcraft.inspection import inspect_table_file


class EventsStorageTest(unittest.TestCase):
    def test_jsonl_replay_and_cursor(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "events.jsonl"
            log = EventLog(path)
            first = log.append("plan_published", {"revision": 1})
            second = log.append("done", {"status": "succeeded"})
            reopened = EventLog(path)
            self.assertEqual([item.seq for item in reopened.read()], [1, 2])
            self.assertEqual(reopened.read(1)[0].event, "done")
            self.assertIn(b"event: plan_published", encode_sse(first))
            self.assertIn(b"data:", encode_sse(second))

    def test_json_store_survives_reopen(self):
        with tempfile.TemporaryDirectory() as directory:
            first = JsonStore(directory)
            first.create_session("s")
            first.append_event("s", "note", {"ok": True})
            first.append_message("s", "user", "hello")
            first.publish_plan("s", {"goal": "x", "revision": 1, "steps": []})
            # The store intentionally does not validate plans; engine validators
            # remain the single source of truth.
            second = JsonStore(directory)
            second.create_session("s")
            self.assertEqual(second.read_events("s")[0].event, "note")
            self.assertEqual(second.get_current_plan("s")["goal"], "x")
            self.assertEqual(second.list_messages("s")[0]["text"], "hello")
            self.assertEqual(second.get_session("s")["session_id"], "s")

    def test_in_memory_session_deletion_is_scoped(self):
        from loomcraft import InMemoryStore
        store = InMemoryStore()
        store.create_session("one")
        store.create_session("two")
        self.assertTrue(store.delete_session("one"))
        self.assertIsNone(store.get_session("one"))
        self.assertIsNotNone(store.get_session("two"))

    def test_bounded_csv_inspection(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "table.csv"
            path.write_text("id,value\na,1\nb,2\n", encoding="utf-8")
            result = inspect_table_file(path, max_rows=1)
            self.assertEqual(result["shape"], {"sample_rows": 1, "columns": 2})
            self.assertTrue(result["truncated"])

    def test_local_upload_store_tracks_checksum_and_delete(self):
        with tempfile.TemporaryDirectory() as directory:
            uploads = LocalUploadStore(directory, max_file_bytes=10, max_session_bytes=20)
            row = uploads.save("session", "../data.csv", b"id\n1\n")
            self.assertEqual(row["filename"], "data.csv")
            self.assertEqual(uploads.path("session", row["id"]).read_bytes(), b"id\n1\n")
            self.assertEqual(len(uploads.list_uploads("session")), 1)
            self.assertIsNotNone(uploads.delete("session", row["id"]))
            self.assertEqual(uploads.list_uploads("session"), [])
            limited = LocalUploadStore(directory, max_file_bytes=4, max_session_bytes=4)
            limited.save("limited", "a", b"1234")
            with self.assertRaises(UploadError):
                limited.save("limited", "b", b"5")

    def test_local_artifact_store_rejects_escape_and_copies(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            scratch = root / "s" / "scratch"
            scratch.mkdir(parents=True)
            (scratch / "out.csv").write_text("a,b\n1,2\n", encoding="utf-8")
            artifacts = LocalArtifactStore(root)
            row = artifacts.register_scratch("s", "scratch/out.csv", step_id="step")
            self.assertEqual(row["step_id"], "step")
            self.assertTrue((root / "s" / "outputs" / row["id"] / "out.csv").exists())
            with self.assertRaises(ArtifactStoreError):
                artifacts.register_scratch("s", "../outside.csv")
            existing = [item.name for item in (root / "s" / "outputs").iterdir() if item.is_dir()]
            with self.assertRaises(ArtifactStoreError):
                artifacts.register_batch("s", [{"path": "out.csv"}, {"path": "missing.csv"}])
            self.assertEqual([item.name for item in (root / "s" / "outputs").iterdir() if item.is_dir()], existing)

    def test_source_resolver_verifies_owned_upload(self):
        with tempfile.TemporaryDirectory() as directory:
            uploads = LocalUploadStore(directory)
            row = uploads.save("s", "data.csv", b"id\n1\n")
            source = SourceResolver(upload_store=uploads).resolve("s", row["source_ref"])
            self.assertEqual(source["filename"], "data.csv")
            with self.assertRaises(SourceResolutionError):
                SourceResolver(upload_store=uploads).resolve("s", "/tmp/data.csv")
            uploads.path("s", row["id"]).write_bytes(b"tampered")
            with self.assertRaises(UploadError):
                uploads.verify("s", row["id"])

    def test_sse_heartbeat_does_not_close_subscription(self):
        async def scenario():
            log = EventLog()
            stream = iter_sse(log, heartbeat_seconds=1)
            self.assertEqual(await stream.__anext__(), b": loomcraft-heartbeat\n\n")
            log.append("done", {})
            frame = await stream.__anext__()
            await stream.aclose()
            return frame
        frame = asyncio.run(scenario())
        self.assertIn(b"event: done", frame)


if __name__ == "__main__":
    unittest.main()
