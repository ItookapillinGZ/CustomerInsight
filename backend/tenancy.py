"""Server-selected workspaces. Client supplied IDs never select a data root."""
from functools import lru_cache

from backend.database import get_engine, workspace_root
from backend.job_service import JobService
from backend.pg_history import PgHistoryStore
from backend.pg_jobs import PgJobStore
from backend.service import WorkspaceService, _root_directory
from core.product_service import ProductRequestBudget


class TenantServices:
    def __init__(self, engine=None, *, data_dir=None, budget=None,
                 analysis_runner=None, agent_runner=None, dispatcher=None):
        self.engine = engine or get_engine()
        self.data_dir = data_dir or _root_directory()
        self.budget = budget or ProductRequestBudget()
        self.analysis_runner, self.agent_runner = analysis_runner, agent_runner
        self.dispatcher = dispatcher

    @lru_cache(maxsize=128)
    def for_workspace(self, workspace_id):
        root = workspace_root(workspace_id, self.data_dir)
        service = WorkspaceService(data_dir=root, budget=self.budget,
                                   analysis_runner=self.analysis_runner, agent_runner=self.agent_runner)
        service.history = PgHistoryStore(self.engine, workspace_id=workspace_id, data_dir=self.data_dir)
        store = PgJobStore(self.engine, workspace_id=workspace_id, data_dir=self.data_dir)
        return service, JobService(service, store=store, dispatcher=self.dispatcher)


class TenantJobWorker:
    def run(self, job_id):
        from backend.job_worker import JobWorker
        store = PgJobStore()
        workspace_id = store.get_job(job_id)["scope"]
        return JobWorker(
            PgJobStore(workspace_id=workspace_id),
            history=PgHistoryStore(workspace_id=workspace_id),
            budget=ProductRequestBudget(),
        ).run(job_id)
