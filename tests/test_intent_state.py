"""Intent persistence and provenance tests; fixtures do not prove model semantics."""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "plugins/navi/scripts"))
import task_state as tasks
import review


class IntentStateTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="navi-intent-")
        self.data = Path(self.tmp.name)
        self.session = "intent-fixture"
        self.turn = 0
        tasks.workspace_policy(self.data, str(self.data), True)
        self.send("按新版接口说明调整职位页面布局。本阶段只改 UI，不接真实接口。职位筛选必须保留。集成以后再做。")

    def tearDown(self):
        self.tmp.cleanup()

    def send(self, prompt):
        self.turn += 1
        tasks.on_hook(self.data, {"session_id": self.session, "turn_id": str(self.turn),
            "hook_event_name": "UserPromptSubmit", "cwd": str(self.data), "prompt": prompt})

    def state(self):
        return tasks.read_state(tasks.paths(self.data, self.session)[1])

    def citation(self, index=-1):
        source = self.state()["sources"][index]
        return {"source_id": source["id"], "quote": source["text"]}

    def item(self, key, kind, statement, status="active", index=-1, origin="user_requirement"):
        return {"id": key, "kind": kind, "text": statement, "status": status,
                "origin": origin, "citations": [self.citation(index)]}

    def effect(self, role, ids, index=-1):
        return dict(self.citation(index), role=role, item_ids=ids)

    def payload(self, changes, effects, uncertainties=None):
        state = self.state()
        memory = tasks.context_view(state)["intent_memory"]
        return {"user_context_digest": tasks.user_context_digest(state),
                "contract": {"goal": "完成当前有效任务"}, "citations": [self.citation()],
                "uncertainties": uncertainties or [], "intent_update": {
                    "base_interpretation_id": memory["interpretation_id"] if memory else None,
                    "changes": changes, "source_effects": effects}}

    def submit(self, changes, effects, uncertainties=None):
        return tasks.agent_write(self.data, self.session, "interpret", self.payload(changes, effects, uncertainties))

    def seed(self):
        return self.submit([
            self.item("goal", "goal", "调整职位页面"),
            self.item("phase", "phase", "仅 UI 修改"),
            self.item("filter", "acceptance", "保留筛选"),
            self.item("no-wire", "constraint", "不接真实接口"),
            self.item("wire", "operation", "集成真实接口", "deferred"),
        ], [self.effect("requirement", ["goal", "phase", "filter", "no-wire", "wire"])])

    def items(self):
        return {i["id"]: i for i in tasks.context_view(self.state())["intent_memory"]["items"]}

    def test_amendment_preserves_other_requirements_and_background_is_not_copy(self):
        self.seed()
        before = self.state()
        self.send("匹配时用户要去其他页面，所以匹配工作默认在后台执行；这里只解释原因，不增加弹窗。")
        view = tasks.context_view(self.state())
        self.assertEqual(view["intent_memory"]["freshness"], "stale")
        self.assertIsNone(view["latest_interpretation"])
        self.submit([self.item("reason", "context", "用户需要切换页面"),
                     self.item("background", "acceptance", "匹配在后台执行"),
                     self.item("no-popup", "constraint", "不增加弹窗")],
                    [self.effect("rationale", ["reason"]),
                     self.effect("addition", ["background", "no-popup"])])
        items = self.items()
        self.assertEqual(items["filter"]["status"], "active")
        self.assertEqual(items["wire"]["status"], "deferred")
        self.assertFalse(any(i["kind"] == "presentation" for i in items.values()))
        state = self.state()
        self.assertEqual(tasks.context_view(state)["unprocessed_source_ids"], [])
        for key in ("task_id", "contract", "budget"):
            self.assertEqual(state[key], before[key])
        self.assertEqual(state["sources"][:1], before["sources"])

    def test_explicit_stage_change_retires_only_affected_constraints(self):
        old = self.seed()
        self.send("现在进入集成阶段，允许接真实接口，筛选要求仍然保留。")
        self.submit([self.item("phase", "phase", "真实接口集成"),
                     self.item("no-wire", "constraint", "不接真实接口", "superseded"),
                     self.item("wire", "operation", "集成真实接口")],
                    [self.effect("replacement", ["phase", "no-wire", "wire"])])
        self.assertEqual(self.items()["filter"]["status"], "active")
        self.assertEqual(self.items()["wire"]["status"], "active")
        self.assertEqual(self.items()["no-wire"]["status"], "superseded")
        old_wire = next(i for i in self.state()["interpretations"][old]["intent"]["items"] if i["id"] == "wire")
        self.assertEqual(old_wire["status"], "deferred")

    def test_goal_replacement_is_possible_without_deleting_history(self):
        self.seed()
        self.send("取消职位页面任务，改为调查通知丢失原因，仅调查不修改。")
        changes = [self.item(k, v["kind"], v["text"], "cancelled") for k, v in self.items().items()]
        changes += [self.item("new-goal", "goal", "调查通知丢失"), self.item("read-only", "operation", "仅调查")]
        self.submit(changes, [self.effect("replacement", [i["id"] for i in changes])])
        self.assertEqual(self.items()["goal"]["status"], "cancelled")
        self.assertEqual(self.items()["new-goal"]["status"], "active")
        self.assertIsNone(tasks.context_view(self.state())["intent_memory"]["current_phase_id"])

    def test_pause_resume_and_continuation_do_not_activate_future_work(self):
        self.seed()
        for prompt, status, role in [("先搁置界面工作。", "paused", "pause"),
                                     ("恢复刚才的界面工作。", "active", "resume")]:
            self.send(prompt)
            self.submit([self.item("phase", "phase", "仅 UI 修改", status)],
                        [self.effect(role, ["phase"])])
            self.assertEqual(self.items()["wire"]["status"], "deferred")
        self.send("接下来按原来约定完成即可。")
        self.submit([], [self.effect("continuation", ["goal", "phase"])])
        self.assertEqual(self.items()["wire"]["status"], "deferred")
        self.assertEqual(self.state()["status"], "active")  # Task pause != recorder pause.

    def test_same_message_can_hold_rationale_and_requested_presentation(self):
        self.seed()
        self.send("用户经常切页，这是后台匹配的原因；请在按钮旁显示运行中状态。")
        self.submit([self.item("why", "context", "用户切页"), self.item("status", "presentation", "按钮旁运行状态")],
                    [self.effect("rationale", ["why"]), self.effect("presentation", ["status"])])
        roles = {e["role"] for e in tasks.context_view(self.state())["intent_memory"]["source_effects"]}
        self.assertTrue({"rationale", "presentation"} <= roles)

    def test_agent_plan_stays_proposal_even_when_progress_changes(self):
        self.seed()
        self.submit([self.item("plan", "plan", "先改布局再接后端", origin="agent_proposal")],
                    [self.effect("clarification", ["plan"])])
        memory = tasks.context_view(self.state())["intent_memory"]
        self.assertEqual(memory["verification"], "unverified")
        self.assertEqual(self.items()["plan"]["origin"], "agent_proposal")
        self.assertEqual(self.items()["wire"]["status"], "deferred")
        # Reusing an earlier quote for a proposal must not erase that quote's other relationships.
        self.assertTrue(any("goal" in e["item_ids"] for e in memory["source_effects"]))
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "progress", {"base_revision": 1, "steps": {"1": "completed"}})

    def test_ambiguous_source_is_explicitly_unresolved(self):
        self.seed()
        self.send("那个也顺便处理一下。")
        with self.assertRaises(ValueError):
            self.submit([], [self.effect("uncertain", [])])
        self.submit([], [self.effect("uncertain", [])], ["那个指代不明，无法确定影响哪些条目"])
        self.assertEqual(self.items()["wire"]["status"], "deferred")
        self.assertTrue(tasks.context_view(self.state())["latest_interpretation"]["uncertainties"])
        self.send("接着做。")
        with self.assertRaises(ValueError):
            self.submit([], [self.effect("continuation", ["goal"])])
        self.submit([], [self.effect("continuation", ["goal"])], ["上一条的指代仍未解决"])

    def test_invalid_updates_are_atomic(self):
        self.seed()
        self.send("补充：保留键盘导航。")
        valid = self.payload([self.item("keyboard", "acceptance", "键盘导航")], [self.effect("addition", ["keyboard"])])
        invalid = []
        missing_source = copy.deepcopy(valid); missing_source["intent_update"]["source_effects"] = []; invalid.append(missing_source)
        forged = copy.deepcopy(valid); forged["intent_update"]["changes"][0]["citations"][0]["quote"] = "可随意接线"; invalid.append(forged)
        missing_ref = copy.deepcopy(valid); missing_ref["intent_update"]["source_effects"][0]["item_ids"] = ["invented"]; invalid.append(missing_ref)
        duplicate = copy.deepcopy(valid); duplicate["intent_update"]["changes"] *= 2; invalid.append(duplicate)
        two_phases = self.payload([self.item("other-phase", "phase", "另一个当前阶段")], [self.effect("addition", ["other-phase"])]); invalid.append(two_phases)
        change_kind = self.payload([self.item("goal", "presentation", "把目标变成文案")], [self.effect("clarification", ["goal"])]); invalid.append(change_kind)
        before = self.state()
        for value in invalid:
            with self.subTest(value=value), self.assertRaises(ValueError):
                tasks.agent_write(self.data, self.session, "interpret", value)
            self.assertEqual(self.state(), before)

    def test_same_source_concurrency_and_exact_retry(self):
        self.seed()
        a = self.payload([], [])
        b = self.payload([self.item("plan", "plan", "调整布局", origin="agent_proposal")],
                         [self.effect("clarification", ["plan"])])
        key = tasks.agent_write(self.data, self.session, "interpret", a)
        before = self.state()
        self.assertEqual(tasks.agent_write(self.data, self.session, "interpret", a), key)
        self.assertEqual(self.state(), before)
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "interpret", b)

    def test_capacity_and_gap_never_drop_intent(self):
        self.seed()
        before = self.state()
        with patch.object(tasks, "MAX_INTENT_ITEMS", 5), self.assertRaises(ValueError):
            self.submit([self.item("extra", "plan", "检查页面")], [self.effect("clarification", ["extra"])])
        self.assertEqual(before, self.state())
        tasks.paths(self.data, self.session)[1].with_suffix(".gap").touch()
        with self.assertRaises(ValueError):
            self.submit([], [])
        self.assertEqual(before, self.state())

    def test_legacy_upgrade_but_no_silent_downgrade(self):
        legacy = self.payload([], [])
        legacy.pop("intent_update")
        tasks.agent_write(self.data, self.session, "interpret", legacy)
        self.assertIsNone(tasks.context_view(self.state())["intent_memory"])
        self.seed()
        with self.assertRaises(ValueError):
            tasks.agent_write(self.data, self.session, "interpret", legacy)

    def test_restart_and_compaction_events_preserve_memory_and_budget(self):
        self.seed()
        before = self.state()
        for reason in ("resume", "compact"):
            payload = {"hook_event_name": "SessionStart", "session_id": self.session, "source": reason}
            code = "import json,sys; from pathlib import Path; import task_state; task_state.on_hook(Path(sys.argv[1]),json.loads(sys.argv[2]))"
            subprocess.run([sys.executable, "-c", code, str(self.data), json.dumps(payload)],
                           cwd=str(Path(tasks.__file__).parent), check=True, capture_output=True)
        for key in ("sources", "interpretations", "latest_interpretation_id", "task_id", "budget"):
            self.assertEqual(self.state()[key], before[key])

    def test_review_packet_gets_only_fresh_memory(self):
        self.seed()
        evidence = {"actions": [{"id": "a", "kind": "write", "path": "ui", "evidence": "layout diff", "origin": "test"}],
                    "coverage": "complete_for_checkpoint"}
        packet = review.make_packet(self.state(), evidence)
        self.assertIn("intent", packet["interpretation"])
        self.send("补充：也保留排序。")
        packet = review.make_packet(self.state(), evidence)
        self.assertIsNone(packet["interpretation"])
        self.assertEqual(len(packet["user_sources"]), 2)


if __name__ == "__main__":
    unittest.main()
