import uuid


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch(self, job_id: uuid.UUID) -> str:
        self.dispatched.append(job_id)
        return f"task-{job_id}"


class FakeGraphDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[tuple[uuid.UUID, bool]] = []

    def dispatch(self, document_version_id: uuid.UUID, *, force: bool = False) -> str:
        self.dispatched.append((document_version_id, force))
        return f"graph-task-{document_version_id}"
