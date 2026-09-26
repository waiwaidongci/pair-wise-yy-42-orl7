import tempfile, unittest
from pathlib import Path
from src.domain import ConflictError, PermissionDenied, ValidationError
from src.repository import Repository
from src.service import Service
from src.rules import STATES, TRANSITION_ROLES


class MergeTest(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.item=self._item("merge item A","MERGE-A"); self.other=self._item("merge item B","MERGE-B")
    def tearDown(self): self.repo.close(); self.tmp.cleanup()
    def _item(self,title,ref):
        return self.service.create_item({"title":title,"description":"offline merge tests","severity":"moderate","quantity":1,"threshold":10,"external_ref":ref},"creator","field_commander")
    def _merge(self,item_id,records,actor="recorder",role="field_commander"):
        return self.service.merge_records(item_id,{"records":records},actor,role)
    def _record_audits(self,item_id):
        return [e for e in self.service.audit("viewer",item_id) if e["action"]=="record"]

    def test_created_and_duplicate_counts(self):
        first=self._merge(self.item["id"],[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"队员F-01驻守东线"}])
        self.assertEqual((first["created_count"],first["duplicate_count"],first["rejected_count"]),(1,0,0))
        record=first["created"][0]; self.assertEqual(record["external_ref"],"R-1"); self.assertEqual(record["resource_id"],"F-01")
        again=self._merge(self.item["id"],[
            {"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"重复补录，内容应被忽略"},
            {"client_ref":"R-2","resource_id":"F-02","kind":"assignment","detail":"队员F-02驻守西线","status":"closed"}])
        self.assertEqual((again["created_count"],again["duplicate_count"],again["rejected_count"]),(1,1,0))
        self.assertEqual(again["duplicates"][0]["id"],record["id"]); self.assertEqual(again["duplicates"][0]["detail"],"队员F-01驻守东线")
        self.assertEqual(len(self.service.list_records(self.item["id"],"viewer")),2)
        self.assertEqual(len(self._record_audits(self.item["id"])),2); self.assertTrue(self.repo.verify_audit_chain())

    def test_replay_is_idempotent(self):
        batch=[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"离线补录"}]
        first=self._merge(self.item["id"],batch); replay=self._merge(self.item["id"],batch)
        self.assertEqual((replay["created_count"],replay["duplicate_count"],replay["rejected_count"]),(0,1,0))
        self.assertEqual(replay["duplicates"][0]["id"],first["created"][0]["id"])
        self.assertEqual(len(self._record_audits(self.item["id"])),1)

    def test_conflict_rejects_whole_batch(self):
        self._merge(self.other["id"],[{"client_ref":"O-1","resource_id":"F-01","kind":"assignment","detail":"F-01在B线"}])
        with self.assertRaises(ConflictError) as ctx:
            self._merge(self.item["id"],[
                {"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"F-01又来A线"},
                {"client_ref":"R-2","resource_id":"F-02","kind":"assignment","detail":"F-02无冲突"}])
        exc=ctx.exception; self.assertIn("F-01",exc.message); self.assertIn(str(self.other["id"]),exc.message)
        payload=exc.payload
        self.assertEqual((payload["created_count"],payload["duplicate_count"],payload["rejected_count"]),(0,0,2))
        conflict=payload["conflicts"][0]
        self.assertEqual(conflict["resource_id"],"F-01"); self.assertEqual(conflict["item_id"],self.other["id"]); self.assertEqual(conflict["item_title"],self.other["title"])
        self.assertEqual(self.service.list_records(self.item["id"],"viewer"),[])
        self.assertEqual(self._record_audits(self.item["id"]),[])

    def test_closed_item_allocation_does_not_block(self):
        self._merge(self.other["id"],[{"client_ref":"O-1","resource_id":"F-01","kind":"assignment","detail":"F-01在B线"}])
        other=self.service.get_item(self.other["id"],"viewer")
        self.repo.transition_item(other["id"],STATES[-1],other["version"],"tester")
        result=self._merge(self.item["id"],[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"B线已关闭，可来A线"}])
        self.assertEqual(result["created_count"],1)

    def test_closed_record_and_same_item_do_not_block(self):
        self._merge(self.other["id"],[{"client_ref":"O-1","resource_id":"F-01","kind":"assignment","detail":"已撤离","status":"closed"}])
        result=self._merge(self.item["id"],[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"重新部署"}])
        self.assertEqual(result["created_count"],1)
        result=self._merge(self.item["id"],[{"client_ref":"R-2","resource_id":"F-01","kind":"assignment","detail":"同一事件内另一条记录"}])
        self.assertEqual(result["created_count"],1)

    def test_duplicate_bypasses_conflict_check(self):
        self._merge(self.item["id"],[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"F-01在A线"}])
        self._merge(self.other["id"],[{"client_ref":"O-1","resource_id":"F-02","kind":"assignment","detail":"F-02在B线"}])
        result=self._merge(self.item["id"],[{"client_ref":"R-1","resource_id":"F-02","kind":"assignment","detail":"重复条目以原记录为准"}])
        self.assertEqual((result["created_count"],result["duplicate_count"]),(0,1))
        self.assertEqual(result["duplicates"][0]["resource_id"],"F-01")

    def test_permission_and_validation(self):
        with self.assertRaises(PermissionDenied): self._merge(self.item["id"],[{"client_ref":"R-1","kind":"k","detail":"d"}],role="viewer")
        with self.assertRaises(ValidationError): self._merge(self.item["id"],[])
        with self.assertRaises(ValidationError): self._merge(self.item["id"],[{"resource_id":"F-01","kind":"k","detail":"d"}])
        with self.assertRaises(ValidationError): self._merge(self.item["id"],[{"client_ref":"R-1","kind":"k","detail":"d"},{"client_ref":"R-1","kind":"k","detail":"d2"}])


class MergeHttpTest(unittest.TestCase):
    def setUp(self):
        import threading
        from http.server import ThreadingHTTPServer
        from src.http_api import make_handler
        self.tmp=tempfile.TemporaryDirectory(); self.repo=Repository(str(Path(self.tmp.name)/"test.db")); self.service=Service(self.repo)
        self.server=ThreadingHTTPServer(("127.0.0.1",0),make_handler(self.service,"static"))
        self.thread=threading.Thread(target=self.server.serve_forever,daemon=True); self.thread.start()
        self.port=self.server.server_address[1]
    def tearDown(self):
        self.server.shutdown(); self.server.server_close(); self.repo.close(); self.tmp.cleanup()
    def _post(self,path,payload,role="field_commander"):
        import http.client, json
        conn=http.client.HTTPConnection("127.0.0.1",self.port)
        conn.request("POST",path,json.dumps(payload),{"Content-Type":"application/json","X-Actor":"tester","X-Role":role})
        response=conn.getresponse(); body=json.loads(response.read().decode("utf-8")); conn.close()
        return response.status,body
    def test_merge_endpoint_counts_and_conflict_payload(self):
        _,a=self._post("/api/items",{"title":"A","description":"a","severity":"low","quantity":1,"threshold":10})
        _,b=self._post("/api/items",{"title":"B","description":"b","severity":"low","quantity":1,"threshold":10})
        status,body=self._post(f"/api/items/{b['id']}/records/merge",{"records":[{"client_ref":"O-1","resource_id":"F-01","kind":"assignment","detail":"d"}]})
        self.assertEqual(status,200); self.assertEqual(body["created_count"],1)
        status,body=self._post(f"/api/items/{a['id']}/records/merge",{"records":[{"client_ref":"R-1","resource_id":"F-01","kind":"assignment","detail":"d"}]})
        self.assertEqual(status,409); self.assertEqual(body["error"],"ConflictError")
        self.assertEqual((body["created_count"],body["duplicate_count"],body["rejected_count"]),(0,0,1))
        self.assertEqual(body["conflicts"][0]["resource_id"],"F-01"); self.assertEqual(body["conflicts"][0]["item_id"],b["id"])
        status,body=self._post(f"/api/items/{a['id']}/records/merge",{"records":[{"client_ref":"R-2","resource_id":"F-09","kind":"assignment","detail":"d"}]})
        self.assertEqual(status,200); self.assertEqual((body["created_count"],body["duplicate_count"],body["rejected_count"]),(1,0,0))


if __name__=="__main__": unittest.main()
