import uuid


class FakeDispatcher:
    def __init__(self) -> None:
        self.dispatched: list[uuid.UUID] = []

    def dispatch(self, job_id: uuid.UUID) -> str:
        self.dispatched.append(job_id)
        return f"task-{job_id}"
