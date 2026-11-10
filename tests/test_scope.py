"""Run scopes: parsing extra_data.couchy, the store, and dropping a scope when
the queue is done with its prompt."""

import threading
import types
import unittest

from couchy_agent import scope as s


class ParseTest(unittest.TestCase):
    def test_parse(self):
        graph = {"1": {"class_type": "CouchyLoadImage", "inputs": {}}, "2": {"inputs": {}}}
        sc = s.parse({"runId": "r1", "owner": "u1", "inputs": ["up1", "up2"]}, "NS_ab", graph)
        self.assertEqual((sc.namespace, sc.owner, sc.run_id), ("ns_ab", "u1", "r1"))
        self.assertEqual(sc.inputs, {"up1", "up2"})
        self.assertTrue(sc.is_run and sc.metadata)
        self.assertEqual(sc.class_of("1"), "CouchyLoadImage")
        self.assertEqual(sc.class_of("1.0.0.3"), "CouchyLoadImage")  # made by expansion
        self.assertIsNone(sc.class_of("2"))
        self.assertFalse(s.parse({"runId": "r1", "owner": "u1", "metadata": False}, "ns_ab", None).metadata)

    def test_refuses_bad_shapes(self):
        for bad in ("x", {"owner": "u1"}, {"runId": "r1"}, {"runId": "../r", "owner": "u1"},
                    {"runId": "r1", "owner": "u/1"}, {"runId": "r1", "owner": "u1", "inputs": "up1"},
                    {"runId": "r1", "owner": "u1", "inputs": ["../x"]},
                    {"runId": "r1", "owner": "u1", "metadata": "no"}):
            with self.assertRaises(s.BadScope, msg=bad):
                s.parse(bad, "ns_ab", {})

    def test_namespace_scope(self):
        sc = s.namespace_scope("ns_ab", {})
        self.assertFalse(sc.is_run)

    def test_lines(self):
        self.assertEqual(str(s.NotInRun("Couchy Load Image")), "This file isn't part of this run. (Couchy Load Image)")
        self.assertEqual(str(s.OutsideCouchy()), "This node only runs in runs Couchy starts.")


class StoreTest(unittest.TestCase):
    def test_bind_once_and_drop(self):
        st = s.Scopes()
        a = s.namespace_scope("ns_a", {})
        self.assertTrue(st.bind("p1", a))
        self.assertFalse(st.bind("p1", s.namespace_scope("ns_b", {})))
        self.assertIs(st.get("p1"), a)
        st.drop("p1", "unknown")
        self.assertIsNone(st.get("p1"))

    def test_current(self):
        st = s.Scopes()
        a = s.namespace_scope("ns_a", {})
        st.bind("p1", a)
        ctx = types.SimpleNamespace(prompt_id="p1", node_id="3")
        self.assertEqual(s.current(st, lambda: ctx), (a, "3"))
        self.assertEqual(s.current(st, lambda: None), (None, None))


class FakeQueue:
    """The parts of ComfyUI's PromptQueue the wrap uses, with its semantics
    (v0.42.5): items are (number, prompt_id, prompt, extra_data, outputs, sensitive)."""

    def __init__(self):
        self.mutex = threading.RLock()
        self.queue = []
        self.currently_running = {}

    def put(self, pid):
        self.queue.append((len(self.queue), pid, {}, {}, [], {}))

    def get(self):
        item = self.queue.pop(0)
        self.currently_running[len(self.currently_running)] = item
        return item, len(self.currently_running) - 1

    def task_done(self, item_id, history_result, status, process_item=None):
        self.currently_running.pop(item_id)

    def wipe_queue(self):
        self.queue = []

    def delete_queue_item(self, function):
        for x in range(len(self.queue)):
            if function(self.queue[x]):
                if len(self.queue) == 1:
                    self.wipe_queue()
                else:
                    self.queue.pop(x)
                return True
        return False


class WatchQueueTest(unittest.TestCase):
    def setUp(self):
        self.st = s.Scopes()
        self.q = FakeQueue()
        s.watch_queue(self.q, self.st)
        for p in ("p1", "p2", "p3", "p4"):
            self.st.bind(p, s.namespace_scope("ns_a", {}))
            self.q.put(p)

    def test_finished_prompt_drops_its_scope(self):
        _, item_id = self.q.get()
        self.q.task_done(item_id, {}, None)
        self.assertIsNone(self.st.get("p1"))
        self.assertIsNotNone(self.st.get("p2"))

    def test_deleted_prompt_drops_its_scope(self):
        self.assertTrue(self.q.delete_queue_item(lambda item: item[1] == "p3"))
        self.assertFalse(self.q.delete_queue_item(lambda item: item[1] == "nope"))
        self.assertIsNone(self.st.get("p3"))
        self.assertEqual(len(self.st), 3)

    def test_wipe_drops_queued_not_running(self):
        self.q.get()  # p1 runs
        self.q.wipe_queue()
        self.assertIsNotNone(self.st.get("p1"))
        self.assertEqual(len(self.st), 1)

    def test_deleting_the_last_item_wipes(self):
        for p in ("p1", "p2", "p3"):
            self.q.delete_queue_item(lambda item, p=p: item[1] == p)
        self.q.delete_queue_item(lambda item: item[1] == "p4")
        self.assertEqual(len(self.st), 0)


if __name__ == "__main__":
    unittest.main()
