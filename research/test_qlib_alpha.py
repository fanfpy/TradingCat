from research.qlib_alpha import QlibAlphaModel
from shared import db as dbm


class Predictor:
    def __init__(self):
        self.rows = []

    def predict(self, rows):
        self.rows = rows
        return {row["symbol"]: row["momentum"] for row in rows}


def test_qlib_alpha_is_bound_to_historical_universe_and_pit_data():
    conn = dbm.get_conn(":memory:")
    dbm.snapshot_universe(
        conn, "all-us", ["AAA.US", "BBB.US"], as_of_date="2025-01-01")
    dbm.upsert_fundamental(
        conn, "AAA.US", "2024-Q4", "2025-01-20", "2025-01-21",
        {"roe": 0.12}, revision=0)
    dbm.upsert_fundamental(
        conn, "AAA.US", "2024-Q4", "2025-02-10", "2025-02-11",
        {"roe": 0.18}, revision=1)
    predictor = Predictor()
    model = QlibAlphaModel(conn, predictor, "qlib-demo-v1", "all-us", top_n=1)
    scores = model.rank(
        "2025-02-01", {"AAA.US": {"momentum": 0.2},
                       "BBB.US": {"momentum": 0.1},
                       "SURVIVOR.US": {"momentum": 9.9}},
    )
    assert [item.symbol for item in scores] == ["AAA.US"]
    assert {row["symbol"] for row in predictor.rows} == {"AAA.US", "BBB.US"}
    aaa = next(row for row in predictor.rows if row["symbol"] == "AAA.US")
    assert aaa["fundamental_roe"] == 0.12  # 2 月 11 日修订不可提前看到
    persisted = dbm.list_alpha_scores(conn, "qlib-demo-v1", "2025-02-01")
    assert len(persisted) == 1
    assert persisted[0]["snapshot_id"] == scores[0].snapshot_id
