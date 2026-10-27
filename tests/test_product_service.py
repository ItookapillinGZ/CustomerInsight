from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
import tempfile
import unittest
import warnings
from unittest.mock import patch
import threading
import time
from types import SimpleNamespace
from core.product_service import ProductLimitError, ProductRequestBudget, ProductRunControl, analyze_customer_rows, requires_access_password, estimate_customer_requests, run_customer_report_agent
from core.analysis_agent import AnalysisAgentResult, report_fingerprint
from core.customer_report import build_customer_report
from core.csv_io import prepare_feedback_rows
from ai.grouped_batch import plan_review_groups
from models.models import FeedbackResult
import pandas as pd

class ProductServiceTests(unittest.TestCase):
    def setUp(self):
        base = Path(__file__).resolve().parents[1] / ".test-tmp"
        base.mkdir(exist_ok=True)
        self.temp = tempfile.TemporaryDirectory(dir=base)
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / "usage.sqlite3"

    def test_concurrent_instances_share_one_hard_daily_cap(self):
        budget = ProductRequestBudget(self.path, daily_limit=5)
        def attempt(i):
            try:
                ProductRequestBudget(self.path, daily_limit=5).reserve(str(i))
                return True
            except ProductLimitError:
                return False
        with ThreadPoolExecutor(max_workers=6) as pool:
            self.assertEqual(sum(pool.map(attempt, range(20))), 5)
        self.assertEqual(budget.remaining(), 0)

    def test_attempts_fail_closed_and_record_only_usage(self):
        budget = ProductRequestBudget(self.path, daily_limit=10)
        control = ProductRunControl(budget, 1, run_id="random-run-only")
        trace = control.trace("row-2")
        trace.before_attempt()
        trace.response_received({"total_tokens": 3})
        with self.assertRaises(ProductLimitError):
            control.trace("row-3").before_attempt()
        self.assertEqual(budget.remaining(), 9)
        self.assertEqual(control.usage, {"row-2": [{"total_tokens": 3}]})

    def test_restarting_controller_does_not_reset_daily_usage(self):
        ProductRequestBudget(self.path, 2).reserve("a")
        self.assertEqual(ProductRequestBudget(self.path, 2).remaining(), 1)

    def test_preflight_and_public_access_stop_before_client_creation(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": [f"review {i}" for i in range(11)]}), "review")
        budget = ProductRequestBudget(self.path, 1)
        with patch("core.product_service.analyze_grouped_batch") as run:
            with self.assertRaises(ProductLimitError):
                analyze_customer_rows(rows, budget=budget)
            run.assert_not_called()
        with patch.dict("os.environ", {"APP_PUBLIC_MODE": "true", "APP_ACCESS_PASSWORD": ""}), patch("core.product_service.analyze_grouped_batch") as run:
            with self.assertRaises(ProductLimitError):
                analyze_customer_rows(rows, budget=budget)
            run.assert_not_called()

    def test_invalid_access_mode_does_not_disable_authentication(self):
        with patch.dict("os.environ", {"APP_PUBLIC_MODE": "treu"}):
            with self.assertRaises(ProductLimitError):
                requires_access_password()

    def test_request_slots_are_shared_between_user_runs(self):
        budget = ProductRequestBudget(self.path, 20)
        lock = threading.Lock()
        state = {"active": 0, "maximum": 0}
        def send(i):
            trace = ProductRunControl(budget, 1).trace(str(i))
            trace.before_attempt()
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            time.sleep(0.005)
            with lock:
                state["active"] -= 1
            trace.response_received("不可用")
        with ThreadPoolExecutor(max_workers=8) as pool:
            list(pool.map(send, range(12)))
        self.assertLessEqual(state["maximum"], 2)

    @staticmethod
    def fake_grouped(rows, **options):
        for index, group in enumerate(plan_review_groups(rows, batch_size=options["batch_size"], max_characters=options["max_characters"])):
            trace = options["request_control_factory"](f"group-{index + 1}")
            trace.before_attempt()
            trace.response_received({"total_tokens": 42})
        return [FeedbackResult.model_validate({**row.model_dump(), "processing_status": "success",
                    "sentiment": "positive", "category": "other", "priority": "low", "summary": "评价满意"})
                if row.processing_status.value == "pending" else row for row in rows]

    def test_one_hundred_short_reviews_use_ten_budget_units_with_metadata(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": [f"评价 {i} 很好" for i in range(100)]}), "review")
        budget = ProductRequestBudget(self.path, 10)
        with patch("core.product_service.analyze_grouped_batch", side_effect=self.fake_grouped) as run:
            results = analyze_customer_rows(rows, budget=budget, max_attempts=10, batch_size=10, max_characters=12000)
        self.assertEqual(len(results), 100)
        self.assertEqual(budget.remaining(), 0)
        self.assertEqual(results.analysis_meta["planned_requests"], 10)
        self.assertEqual(results.analysis_meta["actual_requests"], 10)
        self.assertEqual(results.analysis_meta["unique_pending_rows"], 100)
        self.assertEqual(results.analysis_meta["total_tokens"], 420)
        self.assertEqual(results.analysis_meta["usage_reported_requests"], 10)
        self.assertEqual(results.analysis_meta["prompt_version"], "v3_grouped_aspects_v2")
        self.assertEqual(run.call_args.kwargs["max_workers"], 2)
        self.assertEqual([row.source_row for row in results], list(range(2, 102)))

    def test_two_reviews_fit_one_remaining_request(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": ["a", "b"]}), "review")
        budget = ProductRequestBudget(self.path, 1)
        with patch("core.product_service.analyze_grouped_batch", side_effect=self.fake_grouped):
            result = analyze_customer_rows(rows, budget=budget, max_attempts=1)
        self.assertEqual(result.analysis_meta["actual_requests"], 1)
        self.assertEqual(budget.remaining(), 0)

    def test_long_reviews_and_duplicate_skips_affect_request_estimate(self):
        texts = [str(index) + "文" * 6999 for index in range(3)]
        rows = prepare_feedback_rows(pd.DataFrame({"review": texts + [texts[0], None]}), "review")
        self.assertEqual(estimate_customer_requests(rows, batch_size=10, max_characters=12000), 3)
        with patch("core.product_service.analyze_grouped_batch") as run:
            with self.assertRaises(ProductLimitError):
                analyze_customer_rows(rows, budget=ProductRequestBudget(self.path, 2))
        run.assert_not_called()

    def test_explicit_attempt_cap_counts_requests_instead_of_rows(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": [f"评价 {i}" for i in range(11)]}), "review")
        with patch("core.product_service.analyze_grouped_batch") as run:
            with self.assertRaises(ProductLimitError):
                analyze_customer_rows(rows, budget=ProductRequestBudget(self.path, 10), max_attempts=1)
        run.assert_not_called()

    def test_invalid_group_configuration_fails_before_transport(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": ["a"]}), "review")
        for values in ({"APP_LLM_BATCH_SIZE": "0"}, {"APP_LLM_BATCH_MAX_CHARACTERS": "bad"}):
            with self.subTest(values=values), patch.dict("os.environ", values), patch("core.product_service.analyze_grouped_batch") as run:
                with self.assertRaises(ProductLimitError):
                    analyze_customer_rows(rows, budget=ProductRequestBudget(self.path, 10))
                run.assert_not_called()

    def test_interrupted_run_keeps_actual_request_metadata_in_session_owned_state(self):
        rows = prepare_feedback_rows(pd.DataFrame({"review": ["a", "b"]}), "review")
        budget = ProductRequestBudget(self.path, 2)
        state = {}

        def interrupted(source, **options):
            trace = options["request_control_factory"]("group-0001")
            trace.before_attempt()
            trace.response_received({"total_tokens": 18})
            raise RuntimeError("simulated caller interruption")

        with patch("core.product_service.analyze_grouped_batch", side_effect=interrupted):
            with self.assertRaises(RuntimeError):
                analyze_customer_rows(rows, budget=budget, run_state=state)
        self.assertEqual(state["analysis_meta"]["actual_requests"], 1)
        self.assertEqual(state["analysis_meta"]["planned_requests"], 1)
        self.assertEqual(state["analysis_meta"]["total_tokens"], 18)
        self.assertEqual(state["analysis_meta"]["run_status"], "interrupted")
        self.assertEqual(budget.remaining(), 1)

    def test_authentication_stop_is_visible_before_a_waiter_acquires_capacity(self):
        budget = ProductRequestBudget(self.path, 10)
        control = ProductRunControl(budget, 10)
        failed = control.trace("failed-group")
        blocker = ProductRunControl(budget, 10).trace("another-run")
        failed.before_attempt()
        blocker.before_attempt()
        entered, finished = threading.Event(), threading.Event()
        outcome = []

        def wait_for_slot():
            trace = control.trace("waiting-group")
            entered.set()
            try:
                trace.before_attempt()
                outcome.append("sent")
                trace.response_received({"total_tokens": 1})
            except ProductLimitError:
                outcome.append("stopped")
            finally:
                finished.set()

        worker = threading.Thread(target=wait_for_slot)
        original_release = failed._release_slot

        def release_and_allow_waiter():
            original_release()
            self.assertTrue(finished.wait(2))

        worker.start()
        self.assertTrue(entered.wait(2))
        try:
            with patch.object(failed, "_release_slot", side_effect=release_and_allow_waiter):
                failed.request_failed(SimpleNamespace(status_code=401))
            self.assertEqual(outcome, ["stopped"])
            self.assertEqual(control.attempts, 1)
        finally:
            blocker.response_received({"total_tokens": 1})
            worker.join(timeout=2)

    @staticmethod
    def agent_result(model_calls=2, status="completed"):
        return AnalysisAgentResult(
            status=status, question="分析问题", conclusion="结论", conclusion_metrics=[],
            findings=[], actions=[], scope_note="仅依据当前报告。", tool_trace=["get_overview"],
            model_calls=model_calls, tool_calls=1, total_tokens=9,
            usage_reported_requests=model_calls, report_fingerprint="f" * 64,
            fallback_reason=None if status == "completed" else "offline",
        )

    @staticmethod
    def complete_report():
        source = pd.DataFrame({"评价": ["配送太慢", "包装很好"]})
        results = pd.DataFrame([
            {"source_row": 2, "processing_status": "success", "sentiment": "negative",
             "category": "delivery", "priority": "high", "summary": "物流延误"},
            {"source_row": 3, "processing_status": "success", "sentiment": "positive",
             "category": "product_quality", "priority": "low", "summary": "包装很好"},
        ])
        return build_customer_report(source, results, "评价")

    def test_each_followup_has_fresh_three_call_cap_and_shared_actual_charge(self):
        report = self.complete_report()
        fingerprint = report_fingerprint(report)
        budget = ProductRequestBudget(self.path, 6)
        controls = []

        def run(current_report, question, **options):
            control = options["control"]
            self.assertEqual(control.attempts, 0)
            controls.append(control)
            for step in ("agent-plan", "agent-write"):
                trace = control.trace(step)
                trace.before_attempt()
                trace.response_received({"total_tokens": 4})
            return self.agent_result().model_copy(update={
                "question": question, "report_fingerprint": fingerprint,
            })

        with patch("core.product_service.run_analysis_agent", side_effect=run) as agent:
            first = run_customer_report_agent(report, "先看配送问题", budget=budget)
            self.assertEqual(budget.remaining(), 4)
            second = run_customer_report_agent(report, "具体哪条？", history=[first], budget=budget)
            self.assertEqual(budget.remaining(), 2)
            self.assertEqual(second.status, "completed")
            self.assertEqual(agent.call_args.kwargs["history"][0].question, first.question)
            with self.assertRaises(ProductLimitError):
                run_customer_report_agent(report, "再追问一次", history=[first, second], budget=budget)
        self.assertEqual(agent.call_count, 2)
        self.assertEqual(len(controls), 2)
        self.assertIsNot(controls[0], controls[1])
        self.assertNotEqual(controls[0].run_id, controls[1].run_id)
        self.assertTrue(all(control.attempts == 2 for control in controls))
        self.assertTrue(all(control.max_attempts == 3 for control in controls))

    def test_foreign_or_malformed_history_is_rejected_before_quota_and_agent(self):
        report = self.complete_report()
        budget = ProductRequestBudget(self.path, 5)
        invalid_construct = AnalysisAgentResult.model_construct(**{
            **self.agent_result().model_dump(mode="json"),
            "report_fingerprint": report_fingerprint(report), "question": {"bad": "type"},
        })
        for history in ([self.agent_result()], "wrong type", [None], [invalid_construct]):
            with self.subTest(history_type=type(history).__name__):
                with patch("core.product_service.run_analysis_agent") as agent, \
                        patch.object(budget, "remaining", wraps=budget.remaining) as remaining, \
                        patch.object(budget, "reserve", wraps=budget.reserve) as reserve, warnings.catch_warnings():
                    warnings.filterwarnings("ignore", message="Pydantic serializer warnings:", category=UserWarning)
                    with self.assertRaises(ProductLimitError):
                        run_customer_report_agent(report, "继续分析", history=history, budget=budget)
                agent.assert_not_called()
                remaining.assert_not_called()
                reserve.assert_not_called()
        self.assertEqual(budget.remaining(), 5)

    def test_service_snapshots_valid_history_before_running_the_followup(self):
        report = self.complete_report()
        budget = ProductRequestBudget(self.path, 5)
        previous = self.agent_result().model_copy(update={
            "question": "先看配送问题", "report_fingerprint": report_fingerprint(report),
        })

        def run(current_report, question, **options):
            previous.question = "在执行时修改输入对象"
            self.assertEqual(options["history"][0].question, "先看配送问题")
            self.assertIsNot(options["history"][0], previous)
            return self.agent_result(model_calls=0)

        with patch("core.product_service.run_analysis_agent", side_effect=run):
            result = run_customer_report_agent(report, "具体哪条？", history=[previous], budget=budget)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(budget.remaining(), 5)

    def test_report_agent_provisions_three_call_cap_but_only_charges_two_actual_calls(self):
        budget = ProductRequestBudget(self.path, 3)

        def run(report, question, **options):
            for step in ("agent-plan", "agent-write"):
                trace = options["control"].trace(step)
                trace.before_attempt()
                trace.response_received({"total_tokens": 4})
            return self.agent_result()

        with patch("core.product_service.run_analysis_agent", side_effect=run) as agent:
            result = run_customer_report_agent({"metrics": {"success_rows": 2}}, "分析问题", budget=budget)
        self.assertEqual(result.status, "completed")
        self.assertEqual(result.model_calls, 2)
        self.assertEqual(budget.remaining(), 1)
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(agent.call_args.kwargs["max_model_calls"], 3)
        self.assertEqual(agent.call_args.kwargs["max_tool_calls"], 6)

    def test_report_agent_requires_three_remaining_requests_before_client_or_agent(self):
        budget = ProductRequestBudget(self.path, 2)
        with patch("core.product_service.run_analysis_agent") as agent:
            with self.assertRaises(ProductLimitError):
                run_customer_report_agent({"metrics": {"success_rows": 2}}, "分析问题", budget=budget)
        agent.assert_not_called()
        self.assertEqual(budget.remaining(), 2)

    def test_variable_agent_loop_charges_only_one_two_or_three_actual_requests(self):
        for attempts in (1, 2, 3):
            with self.subTest(attempts=attempts):
                budget = ProductRequestBudget(self.path.parent / f"agent-{attempts}.sqlite3", 3)

                def run(report, question, **options):
                    self.assertEqual(options["control"].max_attempts, 3)
                    for index in range(attempts):
                        trace = options["control"].trace(f"agent-turn-{index + 1}")
                        trace.before_attempt()
                        trace.response_received({"total_tokens": 4})
                    return self.agent_result(model_calls=attempts)

                with patch("core.product_service.run_analysis_agent", side_effect=run):
                    result = run_customer_report_agent({"metrics": {"success_rows": 2}}, "分析问题", budget=budget)
                self.assertEqual(result.status, "completed")
                self.assertEqual(result.model_calls, attempts)
                self.assertEqual(budget.remaining(), 3 - attempts)

    def test_report_agent_zero_coverage_uses_local_fallback_without_budget(self):
        budget = ProductRequestBudget(self.path, 1)
        expected = self.agent_result(model_calls=0, status="fallback")
        with patch("core.product_service.run_analysis_agent", return_value=expected) as agent:
            result = run_customer_report_agent({"metrics": {"success_rows": 0}}, "分析问题", budget=budget)
        self.assertEqual(result.model_calls, 0)
        self.assertEqual(budget.remaining(), 1)
        self.assertNotIn("control", agent.call_args.kwargs)

    def test_report_agent_request_count_mismatch_fails_closed_in_metadata(self):
        budget = ProductRequestBudget(self.path, 3)

        def one_transport(report, question, **options):
            trace = options["control"].trace("agent-plan")
            trace.before_attempt()
            trace.response_received({"total_tokens": 4})
            return self.agent_result(model_calls=2)

        with patch("core.product_service.run_analysis_agent", side_effect=one_transport):
            result = run_customer_report_agent({"metrics": {"success_rows": 1}}, "分析问题", budget=budget)
        self.assertEqual(result.status, "fallback")
        self.assertEqual(result.model_calls, 1)
        self.assertEqual(result.fallback_reason, "RequestCountMismatch")

    def test_report_agent_serializes_last_three_budget_units_as_one_complete_run(self):
        budget = ProductRequestBudget(self.path, 3)
        state = {"active": 0, "maximum": 0}
        lock = threading.Lock()

        def run(report, question, **options):
            with lock:
                state["active"] += 1
                state["maximum"] = max(state["maximum"], state["active"])
            # Gives a competing caller time to pass a stale preflight if the
            # complete Agent run is not protected by its own slot.
            time.sleep(0.05)
            try:
                for step in ("agent-turn-1", "agent-turn-2", "agent-turn-3"):
                    trace = options["control"].trace(step)
                    trace.before_attempt()
                    trace.response_received({"total_tokens": 4})
                return self.agent_result(model_calls=3)
            finally:
                with lock:
                    state["active"] -= 1

        def invoke():
            try:
                return run_customer_report_agent(
                    {"metrics": {"success_rows": 1}}, "分析问题", budget=budget,
                ).status
            except ProductLimitError:
                return "limited"

        with patch("core.product_service.run_analysis_agent", side_effect=run) as agent:
            with ThreadPoolExecutor(max_workers=2) as pool:
                outcomes = list(pool.map(lambda _: invoke(), range(2)))
        self.assertEqual(sorted(outcomes), ["completed", "limited"])
        self.assertEqual(state["maximum"], 1)
        self.assertEqual(agent.call_count, 1)
        self.assertEqual(budget.remaining(), 0)

    def test_report_agent_rejects_reused_or_mismatched_control(self):
        budget = ProductRequestBudget(self.path, 5)
        wrong_attempt_limit = ProductRunControl(budget, 1)
        with patch("core.product_service.run_analysis_agent") as agent:
            with self.assertRaises(ProductLimitError):
                run_customer_report_agent(
                    {"metrics": {"success_rows": 1}}, "分析问题",
                    budget=budget, control=wrong_attempt_limit,
                )
        agent.assert_not_called()

if __name__ == "__main__":
    unittest.main()
