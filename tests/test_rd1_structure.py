"""사후진단 분류의 수학 조건만 검사한다. 진행 중 캠페인은 읽지 않는다."""
import pytest

from analyze_rd1_structure import aggregate, diagnose_case, require_complete


def example(upper=1, optimum=True, chosen=None, resources=None):
    case = {"candidates": [{"id": "a", "request_id": "r1"},
                            {"id": "b", "request_id": "r2"}],
            "constraints": [{"kind": "observed_inventory_lower",
                             "coefficients": {"a": 1, "b": 1}, "lower": 0, "upper": upper}],
            "resource_conflicts": resources or []}
    summary = {"case_id": "mathematical-unit-test", "variant": "primary", "split": "development",
               "encoding_status": "exact", "exact_nodes": 2}
    chosen = ["a"] if chosen is None else chosen
    benchmark = {"selected_candidate_ids": chosen, "lower_bound": len(chosen),
                 "upper_bound": upper, "optimality_proven": optimum, "runs": []}
    return diagnose_case(case, summary, benchmark)


def test_request_bound_requires_feasible_witness():
    row = example(2, chosen=["a", "b"])
    assert row["structure_category"] == "request_bound_certificate"
    assert row["request_bound_attained"]
    invalid = example(1, chosen=["a", "b"])
    assert invalid["structure_category"] == "no_validated_solution"
    assert not invalid["request_bound_attained"]


def test_below_bound_is_inventory_limiting_only_when_optimum_proven():
    assert example()["structure_category"] == "inventory_limits_optimum"
    row = example(2, optimum=False)
    assert row["structure_category"] == "unproven_below_request_bound"
    resource = example(resources=[{"source": "a", "target": "b"}])
    assert resource["structure_category"] == "other_or_combined_constraints_limit_optimum"


def test_zero_requests_are_excluded_from_rate_denominator():
    empty = diagnose_case({"candidates": [], "constraints": []},
                         {"variant": "primary", "split": "development"}, {})
    full = example(2, chosen=["a", "b"])
    result = aggregate([empty, full])
    assert result["completed_cases"] == 2
    assert result["proven_optimum_nonempty_cases"] == 1
    assert result["request_bound_rate_among_proven_nonempty"] == 1


def test_incomplete_campaign_is_rejected_before_reading_cases(tmp_path):
    with pytest.raises(ValueError, match="completion.json"):
        require_complete(tmp_path)


def test_generation_failure_is_not_counted_as_normal_empty_case():
    row = diagnose_case({"candidates": [], "constraints": []},
                        {"variant": "primary", "split": "development",
                         "generation_status": "generation_limit"}, {})
    assert row["structure_category"] == "generation_unavailable"
    assert not row["request_bound_available"]
