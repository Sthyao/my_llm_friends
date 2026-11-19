import asyncio

from .database import connect
from .observer import observer_worker
from .sides import SideConversations


class JournalRuntime:
    def __init__(self):
        self.chats = set()
        self.summaries = set()
        self.sides = SideConversations()
        self.observer_wake = asyncio.Event()
        self.observer_task = None
        self.api_requests = 0
        self.restoring = False

    def start_observer(self):
        self.observer_task = asyncio.create_task(observer_worker(self.observer_wake))

    async def pause_observer(self):
        task = self.observer_task
        if task is not None:
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
            self.observer_task = None

    async def close(self):
        await asyncio.gather(self.sides.close(), self.pause_observer())

    def resume_observer(self):
        # A job cancelled during restore may still be marked as running.
        with connect() as db:
            db.execute("UPDATE observer_jobs SET state = 'pending', ready_at = 0 WHERE state = 'running'")
        self.start_observer()
