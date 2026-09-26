import tempfile, unittest
from pathlib import Path
from src.domain import BatchConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


def entry(client_ref, resource_id=None, kind="assignment", detail="offline note",
          status="open"):
    return {"client_ref": client_ref, "resource_id": resource_id,
            "kind": kind, "detail": detail, "status": status}


class MergeRecordsTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.repo = Repository(str(Path(self.tmp.name) / "test.db"))
        self.service = Service(self.repo)
        self.a = self.service.create_item(
            {"title": "fireline A", "description": "north ridge", "severity": "high",
             "quantity": 5, "threshold": 10, "external_ref": "MRG-A"},
            "creator", "field_commander")
        self.b = self.service.create_item(
            {"title": "fireline B", "description": "south valley", "severity": "moderate",
             "quantity": 2, "threshold": 10, "external_ref": "MRG-B"},
            "creator", "field_commander")

    def tearDown(self):
        self.repo.close()
        self.tmp.cleanup()

    def audit_count(self):
        return len(self.service.audit("viewer"))

    def test_merge_inserts_then_deduplicates_by_client_ref(self):
        payload = {"records": [entry("C-1", "R-1"), entry("C-2", "R-2")]}
        result = self.service.merge_records(self.b["id"], payload, "scout",
                                            "field_commander")
        self.assertEqual(result["inserted_count"], 2)
        self.assertEqual(result["duplicate_count"], 0)
        self.assertEqual(result["rejected_count"], 0)
        audit_after_insert = self.audit_count()
        self.assertTrue(self.repo.verify_audit_chain())

        # 队员回营再次补录同一批：全部命中原记录，不新增审计
        replay = self.service.merge_records(self.b["id"], payload, "scout",
                                            "field_commander")
        self.assertEqual(replay["inserted_count"], 0)
        self.assertEqual(replay["duplicate_count"], 2)
        self.assertEqual(replay["rejected_count"], 0)
        self.assertEqual(self.audit_count(), audit_after_insert)
        original_ids = {r["id"] for r in result["inserted"]}
        self.assertEqual({r["id"] for r in replay["duplicates"]}, original_ids)

    def test_duplicate_client_ref_within_one_batch_counts_once(self):
        result = self.service.merge_records(
            self.b["id"], {"records": [entry("C-9", "R-9"), entry("C-9", "R-9")]},
            "scout", "field_commander")
        self.assertEqual(result["inserted_count"], 1)
        self.assertEqual(result["duplicate_count"], 1)

    def test_resource_on_another_open_event_rejects_whole_batch(self):
        self.service.add_record(
            self.a["id"], {"kind": "assignment", "detail": "guarding A",
                           "status": "open", "resource_id": "R-1"},
            "chief", "field_commander")
        before_records = self.service.list_records(self.b["id"], "viewer")
        before_audit = self.audit_count()

        payload = {"records": [entry("C-3", None, detail="note"),
                               entry("C-4", "R-1")]}
        with self.assertRaises(BatchConflictError) as caught:
            self.service.merge_records(self.b["id"], payload, "scout",
                                       "field_commander")
        body = caught.exception.payload
        self.assertEqual(body["inserted_count"], 0)
        self.assertEqual(body["duplicate_count"], 0)
        self.assertEqual(body["rejected_count"], 2)
        conflict = body["conflicts"][0]
        self.assertEqual(conflict["resource_id"], "R-1")
        self.assertEqual(conflict["conflict_item_id"], self.a["id"])
        self.assertEqual(conflict["conflict_item_title"], "fireline A")

        # 整批原子退回：B无新增记录、审计链无新增
        self.assertEqual(self.service.list_records(self.b["id"], "viewer"),
                         before_records)
        self.assertEqual(self.audit_count(), before_audit)
        self.assertTrue(self.repo.verify_audit_chain())

    def test_conflict_batch_still_reports_existing_duplicates(self):
        self.service.merge_records(
            self.b["id"], {"records": [entry("C-5", "R-1")]}, "scout",
            "field_commander")
        self.service.add_record(
            self.a["id"], {"kind": "assignment", "detail": "guarding A",
                           "status": "open", "resource_id": "R-2"},
            "chief", "field_commander")
        before_audit = self.audit_count()
        with self.assertRaises(BatchConflictError) as caught:
            self.service.merge_records(
                self.b["id"],
                {"records": [entry("C-5", "R-1"), entry("C-6", "R-2")]},
                "scout", "field_commander")
        body = caught.exception.payload
        self.assertEqual(body["duplicate_count"], 1)
        self.assertEqual(body["rejected_count"], 1)
        self.assertEqual(self.audit_count(), before_audit)

    def test_same_resource_on_same_event_is_allowed(self):
        result = self.service.merge_records(
            self.b["id"], {"records": [entry("C-7", "R-3"), entry("C-8", "R-3")]},
            "scout", "field_commander")
        self.assertEqual(result["inserted_count"], 2)

    def test_closed_assignment_elsewhere_and_closed_event_do_not_block(self):
        # 队员在另一事件只有已关闭的分配，不构成冲突
        self.service.add_record(
            self.a["id"], {"kind": "assignment", "detail": "relieved",
                           "status": "closed", "resource_id": "R-4"},
            "chief", "field_commander")
        result = self.service.merge_records(
            self.b["id"], {"records": [entry("C-10", "R-4")]}, "scout",
            "field_commander")
        self.assertEqual(result["inserted_count"], 1)

        # 已关闭事件（无open记录）上的open残留也不再阻止
        self.service.add_record(
            self.a["id"], {"kind": "assignment", "detail": "legacy",
                           "status": "open", "resource_id": "R-5"},
            "chief", "field_commander")
        current = self.service.get_item(self.a["id"], "viewer")
        # 需要先关闭open记录才能关闭事件
        with self.repo.conn:
            self.repo.conn.execute(
                "UPDATE records SET status='closed' WHERE item_id=? AND resource_id='R-5'",
                (self.a["id"],))
        for target in STATES[1:]:
            current = self.service.transition(
                current["id"], target, current["version"], "ic",
                TRANSITION_ROLES[target][0])
        self.assertEqual(current["status"], "closed")
        result = self.service.merge_records(
            self.b["id"], {"records": [entry("C-11", "R-5")]}, "scout",
            "field_commander")
        self.assertEqual(result["inserted_count"], 1)

    def test_validation_and_permission(self):
        with self.assertRaises(ValidationError):
            self.service.merge_records(self.b["id"], {"records": []}, "scout",
                                       "field_commander")
        with self.assertRaises(ValidationError):
            self.service.merge_records(
                self.b["id"], {"records": [{"resource_id": "R-1"}]}, "scout",
                "field_commander")
        with self.assertRaises(PermissionDenied):
            self.service.merge_records(
                self.b["id"], {"records": [entry("C-12", "R-1")]}, "scout",
                "viewer")


if __name__ == "__main__":
    unittest.main()
