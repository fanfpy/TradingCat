"""P4 横截面 AlphaScore 入口。

Qlib 在本系统中只负责 ``features -> score``。股票池、PIT 可见性、TopN 持久化
由本模块控制；仓位、组合风控和执行继续走现有生产链。
"""

import json
from dataclasses import dataclass
from typing import Dict, List, Protocol

from shared import db as dbm


@dataclass(frozen=True)
class AlphaScore:
    symbol: str
    score: float
    rank: int
    as_of: str
    model_id: str
    snapshot_id: str
    features: Dict


class CrossSectionalPredictor(Protocol):
    def predict(self, rows: List[Dict]) -> Dict[str, float]:
        ...


class QlibModelPredictor:
    """已训练 Qlib model 的薄适配器。

    ``dataset_factory(rows)`` 由具体研究项目提供并返回 Qlib Dataset；这样本仓库
    不把长桥 PIT 数据偷偷转换为带未来信息的全局 Dataset。
    """

    def __init__(self, model, dataset_factory):
        try:
            import qlib  # noqa: F401
        except ImportError as exc:
            raise RuntimeError(
                "pyqlib 未安装；请安装可选 research-qlib 依赖后再启用 Qlib") from exc
        self.model = model
        self.dataset_factory = dataset_factory

    def predict(self, rows: List[Dict]) -> Dict[str, float]:
        prediction = self.model.predict(self.dataset_factory(rows))
        result: Dict[str, float] = {}
        if hasattr(prediction, "items"):
            for key, value in prediction.items():
                symbol = key[-1] if isinstance(key, tuple) else key
                result[str(symbol)] = float(value)
        return result


class QlibAlphaModel:
    """PIT Universe + PIT fundamentals 驱动的横截面评分器。"""

    def __init__(self, conn, predictor: CrossSectionalPredictor,
                 model_id: str, universe_source: str, top_n: int = 20):
        if not model_id or not universe_source:
            raise ValueError("model_id/universe_source 必填")
        if top_n <= 0:
            raise ValueError("top_n 必须 > 0")
        self.conn = conn
        self.predictor = predictor
        self.model_id = model_id
        self.universe_source = universe_source
        self.top_n = top_n

    def rank(self, as_of: str,
             features_by_symbol: Dict[str, Dict]) -> List[AlphaScore]:
        members = dbm.universe_as_of(
            self.conn, as_of, source=self.universe_source)
        if not members:
            raise RuntimeError(
                f"{self.universe_source} 在 {as_of} 无 UniverseSnapshot，拒绝评分")
        snapshot_id = members[0]["snapshot_id"]
        rows: List[Dict] = []
        for member in members:
            symbol = member["symbol"]
            if member["status"] != "active" or symbol not in features_by_symbol:
                continue
            features = dict(features_by_symbol[symbol])
            # 只读取 available_at <= as_of 的修订版本，防 look-ahead。
            fundamentals = dbm.fundamentals_as_of(self.conn, symbol, as_of)
            for fundamental in fundamentals:
                payload = json.loads(fundamental["payload_json"])
                for key, value in payload.items():
                    features[f"fundamental_{key}"] = value
            rows.append({"symbol": symbol, "as_of": as_of, **features})
        if not rows:
            raise RuntimeError("PIT Universe 中没有具备当期特征的标的")

        raw_scores = self.predictor.predict(rows)
        row_map = {row["symbol"]: row for row in rows}
        ranked = sorted(
            ((symbol, float(score)) for symbol, score in raw_scores.items()
             if symbol in row_map),
            key=lambda item: (-item[1], item[0]),
        )[:self.top_n]
        output = [AlphaScore(
            symbol=symbol, score=score, rank=index + 1, as_of=as_of,
            model_id=self.model_id, snapshot_id=snapshot_id,
            features={k: v for k, v in row_map[symbol].items()
                      if k not in ("symbol", "as_of")},
        ) for index, (symbol, score) in enumerate(ranked)]
        dbm.replace_alpha_scores(
            self.conn, self.model_id, as_of, snapshot_id,
            [{"symbol": item.symbol, "score": item.score, "rank": item.rank,
              "features": item.features} for item in output],
        )
        dbm.audit(
            self.conn, "ALPHA_SCORE", entity_type="model", entity_id=self.model_id,
            payload={"as_of": as_of, "snapshot_id": snapshot_id,
                     "universe_source": self.universe_source,
                     "scored": len(raw_scores), "top_n": len(output)},
        )
        return output
