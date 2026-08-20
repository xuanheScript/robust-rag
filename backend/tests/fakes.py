import uuid


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch(self, job_id: uuid.UUID) -> str:
        self.dispatched.append(job_id)
        return f"task-{job_id}"


class FakeGraphDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch(self, graph_build_request_id: uuid.UUID) -> str:
        self.dispatched.append(graph_build_request_id)
        return f"graph-task-{graph_build_request_id}"
