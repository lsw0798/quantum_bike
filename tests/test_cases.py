"""대여소 식별자, 재고 제약, 중복 이용 처리를 검증한다."""
import pandas as pd
from bike_research.cases import attach_inventory_constraints
from bike_research.data import station_id


def test_identifier_does_not_confuse_station_code():
    """숫자 번호는 통일하고 ST- 형식 코드는 거절한다."""
    assert station_id("00102") == "102"
    assert station_id("ST-102") is None


def test_replacement_cancels_old_departure_and_preserves_background():
    """대체 후보는 원래 출발을 취소하되 배경 이용은 그대로 남긴다."""
    start = pd.Timestamp("2025-12-01 08:00")

    # A→B는 대체 대상, C→B는 변하지 않는 배경 이용이다.
    trips = pd.DataFrame([
        {"origin": "A", "destination": "B", "start": start + pd.Timedelta(minutes=1),
         "end": start + pd.Timedelta(minutes=5)},
        {"origin": "C", "destination": "B", "start": start + pd.Timedelta(minutes=2),
         "end": start + pd.Timedelta(minutes=4)},
    ])

    # 출발지만 A에서 C로 바꾸는 후보
    case = {"metadata": {}, "candidates": [{
        "id": "v",
        "original_origin": "A",
        "original_destination": "B",
        "original_start": str(trips.iloc[0].start),
        "original_end": str(trips.iloc[0].end),
        "origin": "C",
        "destination": "B",
        "start": str(trips.iloc[0].start),
        "end": str(trips.iloc[0].end),
    }]}
    inv = pd.DataFrame(columns=["station", "time", "inventory"])
    attach_inventory_constraints(case, trips, inv, {"A": 1, "B": 0, "C": 1}, start)

    # 새 C 출발은 그대로 남은 C 배경 출발과 재고를 두고 경쟁한다.
    violated = [r for r in case["constraints"]
                if sum(r["coefficients"].values()) < r["lower"]]
    assert violated and any(r["station"] == "C" for r in violated)
    assert case["inventory_audit"]["baseline_feasible"]

    # A에서는 원래 출발이 취소되어 재고가 1 늘어난다.
    a_rows = [r for r in case["constraints"] if r["station"] == "A" and r["coefficients"]]
    assert any(r["coefficients"] == {"v": 1} for r in a_rows)


def test_duplicate_trip_retains_source_but_not_double_request():
    """중복 행은 남기되 유효 요청으로는 한 번만 센다."""
    from bike_research.data import mark_duplicate_trips

    # 같은 이용이 원본에 두 행으로 기록된 경우
    frame = pd.DataFrame([dict(bike_id="real", start="s", end="e", origin="A",
                               destination="B", valid=True, source_row=i) for i in [1, 2]])
    result = mark_duplicate_trips(frame)
    assert len(result) == 2
    assert result.valid.tolist() == [True, False]


def test_known_return_is_preserved_when_origin_unknown():
    """출발지를 모르는 이용이라도 알려진 반납은 재고에 반영한다."""
    start = pd.Timestamp("2025-12-01 08:00")
    case = {"metadata": {}, "candidates": [{
        "id": "v",
        "original_origin": "A",
        "original_destination": "B",
        "original_start": str(start + pd.Timedelta(minutes=2)),
        "original_end": str(start + pd.Timedelta(minutes=5)),
        "origin": "B",
        "destination": "B",
        "start": str(start + pd.Timedelta(minutes=2)),
        "end": str(start + pd.Timedelta(minutes=5)),
    }]}

    # 출발지가 없고 B로 반납만 알려진 배경 이용
    trips = pd.DataFrame([{"origin": None, "destination": "B", "start": start,
                           "end": start + pd.Timedelta(minutes=1)}])
    inv = pd.DataFrame(columns=["station", "time", "inventory"])
    attach_inventory_constraints(case, trips, inv, {"A": 1, "B": 0}, start)

    # B의 최종 기준 재고가 반납 1대만큼 늘어야 한다.
    assert next(
        s for s in case["inventory_audit"]["stations"] if s["station"] == "B"
    )["baseline_final"] == 1
