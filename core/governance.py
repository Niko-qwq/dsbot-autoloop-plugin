from decimal import Decimal
from uuid import uuid4

from pydantic import TypeAdapter

from core.bus import EventBus
from core.schema import BudgetPolicy, NonnegativeCost, ProjectBudgetAlert, ProjectStatus, TaskDAG
from core.state_manager import DAGManager


class BudgetExceededError(RuntimeError):
    pass


_cost = TypeAdapter(NonnegativeCost)


class GovernanceGate:
    def __init__(self, manager: DAGManager, bus: EventBus):
        if manager.workspace != bus.workspace or manager.lock is not bus.lock:
            raise ValueError("DAG and event bus must share a workspace and lock")
        self.manager = manager
        self.bus = bus

    def load_budget(self) -> BudgetPolicy:
        return self.manager.load().budget

    def _enforce_budget(self, dag: TaskDAG) -> None:
        policy = dag.budget
        if policy.current_cost_usd < policy.max_total_cost_usd:
            return
        message = f"Project {dag.project_id} exhausted its budget (${policy.current_cost_usd:g} / ${policy.max_total_cost_usd:g})"
        if dag.project_status != ProjectStatus.ALERT or dag.budget_alert is None:
            self.mark_budget_alert(dag, message)
            self.manager._save(dag)
        raise BudgetExceededError(message)

    @staticmethod
    def mark_budget_alert(dag: TaskDAG, summary: str) -> None:
        dag.project_status = ProjectStatus.ALERT
        if dag.budget_alert is None:
            dag.budget_alert = ProjectBudgetAlert(event_id=f"budget-alert-{uuid4().hex}", summary=summary)

    @staticmethod
    def charge(dag: TaskDAG, cost_usd: float) -> None:
        amount = _cost.validate_python(cost_usd, strict=True)
        policy = dag.budget
        policy.current_cost_usd = float(Decimal(str(policy.current_cost_usd)) + Decimal(str(amount)))

    def _record_cost(self, cost_usd: float) -> None:
        dag = self.manager.load()
        self.charge(dag, cost_usd)
        self.manager._save(dag)
