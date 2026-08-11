"""本地纸面券商：只生成可回放的模拟订单，不读取凭证、不触网。"""

from typing import Dict

from execution.broker_live import BrokerAck, LiveBrokerSafetyError
from shared import db as dbm


class PaperBroker:
    """PAPER 模式唯一路由；显式拒绝 DRY_RUN/LIVE 计划。"""

    def __init__(self, conn):
        self.conn = conn

    def submit_order(self, intent: Dict, confirmation=None, plan=None) -> BrokerAck:
        if confirmation is None or plan is None:
            raise LiveBrokerSafetyError("PAPER 提交必须携带 Confirmation + ExecutionPlan")
        if plan.execution_mode != "PAPER":
            raise LiveBrokerSafetyError("PaperBroker 只接受 PAPER 计划")
        if confirmation.status != "APPROVED":
            raise LiveBrokerSafetyError("PAPER 提交需要 APPROVED confirmation")
        row = dbm.get_confirmation(self.conn, confirmation.confirmation_id)
        if row is None or row["status"] != "CONSUMED":
            raise LiveBrokerSafetyError("PAPER 提交必须先完成原子消费")
        paper_id = f"paper_{plan.plan_id}_{intent.get('plan_order_id', 'x')}"
        dbm.audit(self.conn, "BROKER_PAPER_SUBMIT", entity_type="intent",
                  entity_id=str(intent.get("client_request_id")),
                  payload={"broker_order_id": paper_id, "plan_id": plan.plan_id,
                           "symbol": intent.get("symbol"), "side": intent.get("side"),
                           "quantity": intent.get("quantity"), "mode": "PAPER"})
        return BrokerAck(broker_order_id=paper_id, status="PAPER_SUBMITTED",
                         is_live=False, raw={"mode": "PAPER", "paper_order_id": paper_id})
