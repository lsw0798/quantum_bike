"""모든 작은 후보 부분집합을 직접 검사하여 충돌그래프의 의미를 확인한다."""
from copy import deepcopy
from itertools import combinations
import unittest

from truck_mis.domain import Task, build_case, conflict_reasons, evaluate_counts, validate_selection


def small_input():
    def station(identifier, latitude, longitude, bikes):
        return {"station_id": identifier, "name": identifier,
                "latitude": latitude, "longitude": longitude, "bikes": bikes, "target": 10}
    rows = [station("1", 37.55, 126.91, 10),
            station("2", 37.5501, 126.91, 12), station("3", 37.55, 126.9101, 12),
            station("4", 37.5499, 126.91, 8), station("5", 37.55, 126.9099, 8)]
    config = {"origin_station_id": "1", "radius_km": 1,
              "bikes_per_task": 2, "truck_capacity": 2, "truck_speed_kmh": 60,
              "handling_min_per_bike": 0.25, "stop_overhead_min": 0,
              "horizon_min": 4, "start_step_min": 2, "max_tasks": 80, "candidate_seed": 7}
    return rows, config


class DomainTests(unittest.TestCase):
    def setUp(self):
        rows, config = small_input()
        self.case = build_case(rows, config, {})

    def test_all_subsets_graph_independence_iff_operational_feasibility(self):
        # 이 사례의 8개 후보에서 가능한 256개 부분집합을 모두 검사한다.
        n = len(self.case["tasks"])
        self.assertEqual(n, 8)
        for mask in range(1 << n):
            selected = [index for index in range(n) if mask & (1 << index)]
            independent = all(not (mask & (1 << edge["source"]) and mask & (1 << edge["target"]))
                              for edge in self.case["edges"])
            feasible = validate_selection(self.case, selected)["feasible"]
            self.assertEqual(independent, feasible, f"반례 부분집합: {selected}")

    def test_touching_intervals_without_station_reuse_are_compatible(self):
        first = Task("A", "2", "4", 2, 0, 2, 0.1)
        second = Task("B", "3", "5", 2, 2, 4, 0.1)
        self.assertEqual(conflict_reasons(first, second), [])
        overlapping = Task("C", "3", "5", 2, 1.999, 4, 0.1)
        self.assertEqual(conflict_reasons(first, overlapping), ["time_overlap"])

    def test_station_reuse_conflicts_even_without_time_overlap(self):
        first = Task("A", "2", "4", 2, 0, 1, 0.1)
        second = Task("B", "2", "5", 2, 2, 3, 0.1)
        self.assertEqual(conflict_reasons(first, second), ["shared_station"])
        # 생성한 실제 후보에서도 비중첩 동일 수거소 쌍은 운영 제약을 위반한다.
        pair = next((i, j) for i, j in combinations(range(len(self.case["tasks"])), 2)
                    if self.case["tasks"][i]["pickup_id"] == self.case["tasks"][j]["pickup_id"]
                    and self.case["tasks"][i]["end_min"] <= self.case["tasks"][j]["start_min"])
        result = validate_selection(self.case, list(pair))
        self.assertFalse(result["feasible"])
        self.assertIn("station_reuse", result["violations"])

    def test_single_task_preserves_targets_and_truck_load(self):
        result = validate_selection(self.case, [0])
        self.assertTrue(result["feasible"])
        self.assertEqual(result["bikes_delivered"], 2)
        self.assertEqual(result["shortage_reduction"], 2)
        self.assertEqual(result["surplus_reduction"], 2)
        self.assertEqual(set(result["final_inventory"].values()), {10})
        broken = deepcopy(self.case)
        broken["tasks"][0]["quantity"] = 3
        invalid = validate_selection(broken, [0])
        self.assertFalse(invalid["feasible"])
        self.assertTrue({"truck_load", "quantity", "pickup_below_target", "delivery_above_target"}
                        .issubset(invalid["violations"]))

    def test_raw_infeasible_samples_remain_infeasible_without_repair(self):
        n = len(self.case["tasks"])
        pair = next([i, j] for i, j in combinations(range(n), 2)
                    if validate_selection(self.case, [i, j])["feasible"])
        valid_bits = "".join("1" if index in pair else "0" for index in range(n))
        counts = {"1" * n: 10, "0" * n: 2, valid_bits: 3}
        original = deepcopy(counts)
        result = evaluate_counts(self.case, counts)
        self.assertEqual(counts, original)
        self.assertEqual(result["shots_received"], 15)
        self.assertEqual(result["feasible_shots"], 5)
        self.assertAlmostEqual(result["raw_feasible_rate"], 1 / 3)
        self.assertEqual(result["best_observed_feasible"]["bitstring"], valid_bits)
        self.assertFalse(result["optimum_proven"])
        rejected = next(row for row in result["samples"] if row["bitstring"] == "1" * n)
        self.assertFalse(rejected["feasible"])
        self.assertEqual(rejected["count"], 10)
        self.assertEqual(result["postprocessing"], "decode_validate_and_rank_only_no_repair")

    def test_malformed_counts_and_zero_shots_are_rejected(self):
        n = len(self.case["tasks"])
        for counts in ({"0" * (n - 1): 1}, {"X" * n: 1}, {"0" * n: -1},
                       {"0" * n: 1.5}, {"0" * n: True}, {"0" * n: 0}, {}):
            with self.subTest(counts=counts), self.assertRaises(ValueError):
                evaluate_counts(self.case, counts)

    def test_candidate_cap_is_reproducible_and_audited_before_embedding(self):
        rows, config = small_input()
        capped = {**config, "max_tasks": 3}
        one = build_case(rows, capped, {})
        two = build_case(list(reversed(rows)), capped, {})
        self.assertEqual(one["tasks"], two["tasks"])
        self.assertEqual(one["edges"], two["edges"])
        self.assertEqual(one["candidate_audit"]["enumerated_tasks"], 8)
        self.assertEqual(one["candidate_audit"]["retained_tasks"], 3)
        self.assertEqual(one["candidate_audit"]["retained_fraction"], 3 / 8)
        self.assertEqual(one["candidate_audit"]["selection"], "seeded_uniform_sample_before_embedding")
        self.assertEqual(one["objective"], "maximum_number_of_fixed_quantity_tasks")
        self.assertEqual({task["quantity"] for task in one["tasks"]}, {2})


if __name__ == "__main__":
    unittest.main()
