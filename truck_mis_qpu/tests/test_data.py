"""재고 시점·중복·목표값 산출의 의미 있는 경계조건을 검증한다."""
import csv
from pathlib import Path
import tempfile
import unittest
import zipfile

from truck_mis.data import load_stations


class DataTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.config = {"snapshot_timestamp": "2025-07-15T08:00:00", "history_days": 14,
                       "minimum_history_days": 2, "same_hour": True}
        self.write_metadata()

    def tearDown(self):
        self.temporary.cleanup()

    def write_metadata(self):
        content = '''<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main"><sheetData>
        <row r="1"><c r="A1"><v>102</v></c><c r="B1" t="inlineStr"><is><t>시작역</t></is></c>
        <c r="E1"><v>37.55</v></c><c r="F1"><v>126.91</v></c><c r="I1"><v>5</v></c></row>
        <row r="2"><c r="A2"><v>103</v></c><c r="B2" t="inlineStr"><is><t>다음역</t></is></c>
        <c r="E2"><v>37.56</v></c><c r="F2"><v>126.92</v></c><c r="I2"><v>10</v></c></row>
        </sheetData></worksheet>'''
        with zipfile.ZipFile(self.root / "대여소(25.6월 기준).xlsx", "w") as archive:
            archive.writestr("xl/worksheets/sheet1.xml", content)

    def write_inventory(self, rows):
        with (self.root / "data_2507.csv").open("w", encoding="cp949", newline="") as stream:
            writer = csv.writer(stream)
            writer.writerow(["일시", "대여소번호", "대여소명", "시간대", "거치대수량"])
            for day, station, hour, bikes in rows:
                writer.writerow([day, station, "대여소", hour, bikes])

    def test_target_excludes_snapshot_future_and_other_hours(self):
        self.write_inventory([
            ("2025-07-01", "00102", 8, 2), ("2025-07-02", "102.0", 8, 5),
            ("2025-07-03", "102", 7, 100), ("2025-07-15", "102", 8, 99),
            ("2025-07-16", "102", 8, 1000),
        ])
        stations, audit = load_stations(self.config, self.root)
        self.assertEqual(len(stations), 1)
        self.assertEqual(stations[0]["target"], 3)
        self.assertEqual(stations[0]["bikes"], 99)
        # 실제 관측 99를 설치 거치대 5에 맞춰 잘라내지 않는다.
        self.assertEqual(stations[0]["installed_qr_racks"], 5)
        self.assertEqual(audit["audit"]["excluded_missing_snapshot"], 1)
        self.assertTrue(all(len(source["sha256"]) == 64 for source in audit["sources"]))

    def test_conflicting_duplicate_invalidates_only_that_station_time(self):
        self.write_inventory([
            ("2025-07-01", "102", 8, 2), ("2025-07-01", "102", 8, 2),
            ("2025-07-02", "102", 8, 4), ("2025-07-03", "102", 8, 8),
            ("2025-07-03", "102", 8, 20), ("2025-07-15", "102", 8, 1),
        ])
        stations, audit = load_stations(self.config, self.root)
        self.assertEqual(stations[0]["target"], 3)
        self.assertEqual(stations[0]["history_day_count"], 2)
        self.assertEqual(audit["audit"]["identical_duplicate_rows"], 1)
        self.assertEqual(audit["audit"]["excluded_invalid_or_conflicting_keys"], 1)

    def test_missing_exact_snapshot_fails(self):
        self.write_inventory([("2025-07-01", "102", 8, 2), ("2025-07-02", "102", 8, 4)])
        with self.assertRaisesRegex(ValueError, "정확한 스냅샷"):
            load_stations(self.config, self.root)

    def test_fixed_target_requires_no_historical_observations(self):
        self.write_inventory([("2025-07-15", "102", 8, 19)])
        stations, audit = load_stations({**self.config, "mode": "fixed", "fixed_target": 10}, self.root)
        self.assertEqual(stations[0]["target"], 10)
        self.assertEqual(audit["mode"], "fixed")

    def test_future_metadata_fails(self):
        (self.root / "대여소(25.6월 기준).xlsx").rename(self.root / "대여소(25.12월 기준).xlsx")
        with self.assertRaisesRegex(ValueError, "스냅샷 이전"):
            load_stations(self.config, self.root)

    def test_manual_csv_validates_schema_and_timestamp(self):
        path = self.root / "stations.csv"
        path.write_text("station_id,name,latitude,longitude,bikes,target,timestamp\n"
                        "00102,역,37.5,126.9,3,5,2025-07-15T08:00:00\n", encoding="utf-8")
        stations, audit = load_stations({**self.config, "stations_csv": "stations.csv"}, self.root)
        self.assertEqual(stations[0]["station_id"], "102")
        self.assertEqual(audit["input_mode"], "manual_csv")
        with self.assertRaisesRegex(ValueError, "timestamp"):
            load_stations({**self.config, "snapshot_timestamp": "2025-07-15T09:00:00",
                           "stations_csv": "stations.csv"}, self.root)


if __name__ == "__main__":
    unittest.main()
