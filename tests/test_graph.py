"""작은 논리 반례를 이용한 검증. 연구용 실측 사례와 구분한다."""
import unittest
import itertools
import random

from bike_research.graph import (
    build_graph, benchmark_graph, solve_operational, validate_selection,
)


def case_with(coefficients, lower=None, upper=None):
    """후보마다 요청이 하나씩이고 선형 제약이 하나인 사례를 만든다."""
    return {
        "case_id": "unit-test",
        "candidates": [{"id": key, "request_id": key} for key in coefficients],
        "constraints": [{"id": "inventory", "kind": "inventory",
                         "coefficients": coefficients, "lower": lower, "upper": upper}],
    }


class GraphTests(unittest.TestCase):
    """충돌 그래프와 운영 모델의 관계를 검증한다."""

    def test_capacity_two_is_not_three_pairwise_conflicts(self):
        """용량 2 제약은 쌍 충돌로 표현되지 않으므로 정확하다고 표시하지 않는다."""
        case = case_with({"a": 1, "b": 1, "c": 1}, upper=2)
        graph = build_graph(case)
        self.assertEqual(graph["edges"], [])
        self.assertFalse(graph["metadata"]["is_exact_for_declared_model"])

        # 그래프에는 엣지가 없어도 원래 모델은 3개 선택을 금지한다.
        self.assertFalse(validate_selection(case, ["a", "b", "c"])["feasible"])
        self.assertEqual(solve_operational(case, time_limit=5)["objective"], 2)

    def test_supply_can_rescue_a_pair(self):
        """다른 후보의 반납이 구제할 수 있는 쌍은 충돌 엣지로 만들지 않는다."""
        case = case_with({"rent_a": 1, "rent_b": 1, "return": -1}, upper=1)
        graph = build_graph(case)
        self.assertEqual(graph["edges"], [])
        self.assertTrue(validate_selection(case, ["rent_a", "rent_b", "return"])["feasible"])
        self.assertFalse(validate_selection(case, ["rent_a", "rent_b"])["feasible"])

    def test_same_request_is_a_clique(self):
        """같은 요청의 후보끼리는 항상 연결된다."""
        case = {"case_id": "request", "candidates": [
            {"id": "a", "request_id": "r"},
            {"id": "b", "request_id": "r"},
            {"id": "c", "request_id": "other"},
        ], "constraints": []}
        graph = build_graph(case)
        self.assertEqual(len(graph["edges"]), 1)

        # a·b 중 하나와 c를 고르는 것이 최적이다.
        result = benchmark_graph(graph, time_limit=5)
        self.assertEqual(result["objective"], 2)
        self.assertTrue(result["optimal"])
        self.assertFalse(validate_selection(case, ["a", "b"])["feasible"])

    def test_unit_capacity_is_exact(self):
        """용량 1 제약은 쌍 충돌과 동치이므로 정확하다고 표시한다."""
        graph = build_graph(case_with({"a": 1, "b": 1, "c": 1}, upper=1))
        self.assertEqual(len(graph["edges"]), 3)
        self.assertTrue(graph["metadata"]["is_exact_for_declared_model"])

    def test_forbidden_singleton_is_removed_with_reason(self):
        """단독으로도 불가능한 후보는 사유와 함께 노드에서 제외한다."""
        case = case_with({"a": 2, "b": 1}, upper=1)
        graph = build_graph(case)
        self.assertEqual([node["id"] for node in graph["nodes"]], ["b"])
        self.assertEqual(graph["metadata"]["excluded_candidates"][0]["id"], "a")
        self.assertTrue(graph["metadata"]["is_exact_for_declared_model"])

    def test_positive_requirement_remains_a_general_constraint(self):
        """a가 있어야 b가 가능한 제약은 쌍 충돌로 표현되지 않는다."""
        case = case_with({"a": 1, "b": -1}, lower=0)
        graph = build_graph(case)
        self.assertFalse(graph["metadata"]["is_exact_for_declared_model"])
        self.assertFalse(validate_selection(case, ["b"])["feasible"])
        self.assertTrue(validate_selection(case, ["a", "b"])["feasible"])

    def test_duplicates_and_unknown_ids_are_not_silently_accepted(self):
        """중복 선택과 없는 후보의 선택을 위반으로 보고한다."""
        case = case_with({"a": 1}, upper=1)
        self.assertFalse(validate_selection(case, ["a", "a"])["feasible"])
        self.assertFalse(validate_selection(case, ["missing"])["feasible"])

    def test_infeasible_empty_row_is_reported(self):
        """계수가 없어 만족할 수 없는 행을 불가능으로 보고한다."""
        case = case_with({}, lower=1)
        graph = build_graph(case)
        self.assertFalse(graph["metadata"]["is_exact_for_declared_model"])
        self.assertEqual(solve_operational(case, time_limit=5)["status"], "infeasible")

    def test_negative_upper_bound_does_not_delete_helpful_variable(self):
        """음수 상한에서도 제약을 돕는 변수를 지우지 않는다."""
        case = case_with({"a": -1, "b": -1}, upper=-1)
        graph = build_graph(case)
        self.assertEqual(len(graph["nodes"]), 2)
        self.assertTrue(validate_selection(case, ["a"])["feasible"])
        self.assertFalse(graph["metadata"]["is_exact_for_declared_model"])

    def test_pair_deduction_budget_never_claims_exactness(self):
        """추론 예산으로 중단되면 정확하다고 표시하지 않는다."""
        case = case_with({"a": 1, "b": 1, "c": 1}, upper=1)
        graph = build_graph(case, max_pair_deductions=1)
        self.assertEqual(len(graph["edges"]), 1)
        self.assertTrue(graph["metadata"]["pair_inference_truncated"])
        self.assertFalse(graph["metadata"]["is_exact_for_declared_model"])

    def test_soundness_by_exhaustive_small_signed_models(self):
        """무작위 작은 모델을 전수 조사해 그래프가 가능한 해를 금지하지 않음을 확인한다."""
        # 수학적 단위 테스트 입력이며 연구 데이터에는 절대 넣지 않는다.
        rng = random.Random(41)
        identifiers = ["a", "b", "c", "d", "e"]

        # 무작위 모델별 반복
        for trial in range(40):
            coefficients = {key: rng.randint(-3, 3) for key in identifiers}
            lower = rng.choice([None, -3, -1, 0, 1])
            upper = rng.choice([None, 1, 2, 4])
            case = case_with(coefficients, lower, upper)
            graph = build_graph(case)
            nodes = {node["id"] for node in graph["nodes"]}

            # 가능한 모든 선택을 전수 비교한다.
            for bits in itertools.product([False, True], repeat=len(identifiers)):
                chosen = {key for key, bit in zip(identifiers, bits) if bit}
                feasible = validate_selection(case, chosen)["feasible"]
                graph_feasible = chosen <= nodes and not any(
                    edge["source"] in chosen and edge["target"] in chosen
                    for edge in graph["edges"])

                # 원래 모델에서 가능한 해는 그래프에서도 가능해야 한다.
                if feasible:
                    self.assertTrue(graph_feasible, (trial, chosen))

                # 정확하다고 표시한 그래프는 판정이 완전히 일치해야 한다.
                if graph["metadata"]["is_exact_for_declared_model"]:
                    self.assertEqual(feasible, graph_feasible, (trial, chosen))


# 스크립트로 직접 실행될 때만 진입한다.
if __name__ == "__main__":
    unittest.main()
