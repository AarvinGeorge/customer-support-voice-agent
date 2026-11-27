"""What: an asyncio event loop with a virtual clock: loop.time() starts at 0 and jumps to the next timer whenever
the loop would otherwise sleep, so asyncio.sleep, asyncio.timeout and wait_for run instantly in virtual seconds.
Use when: timing tests of the turn budget (doc 07 s8.2, s8.3) that must prove which call runs in the 3 s reserve
without waiting real seconds: `run_virtual(coro)` runs a coroutine on this loop and returns (result, loop).
Inputs: a coroutine. Output: VirtualClockLoop, run_virtual.
Side effects: none outside the loop it creates. While an executor job is running the loop waits in real time
(it never jumps past a thread that has not finished), so thread pool work cannot be overtaken by the clock.
Role: the injectable clock for the reserve proofs (TurnControl.clock reads the running loop's time).
"""
from __future__ import annotations

import asyncio
import selectors
from typing import Any, Coroutine, TypeVar

T = TypeVar("T")


class _VirtualSelector(selectors.DefaultSelector):
    """What: a selector that advances the loop's virtual time instead of blocking. Use when: VirtualClockLoop.
    Inputs: the owning loop. Output: select(). Side effects: moves loop._now. Role: clock driver."""

    def __init__(self) -> None:
        """What: empty selector. Use when: loop init. Inputs: none. Output: none. Side effects: none.
        Role: constructor."""
        super().__init__()
        self.loop: VirtualClockLoop | None = None

    def select(self, timeout: float | None = None) -> list[Any]:
        """What: poll real I/O; when nothing is ready and no executor job runs, jump the clock by `timeout`.
        Use when: every loop iteration. Inputs: timeout (seconds to the next timer, None for no timer).
        Output: ready events. Side effects: virtual time moves. Role: the jump."""
        loop = self.loop
        busy = loop is not None and loop.executor_jobs > 0
        events = super().select(0 if not busy else min(timeout if timeout is not None else 0.01, 0.01))
        if events or loop is None or busy:
            return events
        if timeout is not None and timeout > 0:
            loop.now += timeout
        return events


class VirtualClockLoop(asyncio.SelectorEventLoop):
    """What: SelectorEventLoop whose time() is virtual. Use when: run_virtual. Inputs: none.
    Output: the loop (`now` is the virtual time in seconds). Side effects: none. Role: test loop."""

    def __init__(self) -> None:
        """What: build the loop on the virtual selector. Use when: loop_factory. Inputs: none. Output: none.
        Side effects: none. Role: constructor."""
        selector = _VirtualSelector()
        self.now = 0.0
        self.executor_jobs = 0
        super().__init__(selector)
        selector.loop = self

    def time(self) -> float:
        """What: the virtual time. Use when: asyncio schedules timers. Inputs: none. Output: seconds.
        Side effects: none. Role: clock."""
        return self.now

    def run_in_executor(self, executor: Any, func: Any, *args: Any) -> asyncio.Future[Any]:
        """What: count running executor jobs so the clock waits for them. Use when: any thread offload.
        Inputs: as BaseEventLoop. Output: future. Side effects: counter. Role: thread safety of the jump."""
        self.executor_jobs += 1
        fut = super().run_in_executor(executor, func, *args)

        def _done(_: Any) -> None:
            """What: decrement. Use when: job finished. Inputs: future. Output: none. Side effects: counter.
            Role: helper."""
            self.executor_jobs -= 1

        fut.add_done_callback(_done)
        return fut

    async def shutdown_default_executor(self, timeout: float | None = None) -> None:
        """What: join the default executor synchronously (the base version waits on a timer the virtual clock
        would skip). Use when: asyncio.Runner closes. Inputs: ignored timeout. Output: none.
        Side effects: joins threads. Role: clean shutdown."""
        executor = getattr(self, "_default_executor", None)
        if executor is not None:
            executor.shutdown(wait=True)


def run_virtual(coro: Coroutine[Any, Any, T]) -> tuple[T, VirtualClockLoop]:
    """What: run one coroutine on a fresh VirtualClockLoop. Use when: a timing test. Inputs: coroutine.
    Output: (result, loop) so the test can read loop.now. Side effects: creates and closes a loop.
    Role: entry point."""
    holder: dict[str, VirtualClockLoop] = {}

    def factory() -> VirtualClockLoop:
        """What: make and remember the loop. Use when: asyncio.Runner. Inputs: none. Output: loop.
        Side effects: none. Role: helper."""
        holder["loop"] = VirtualClockLoop()
        return holder["loop"]

    with asyncio.Runner(loop_factory=factory) as runner:
        result = runner.run(coro)
    return result, holder["loop"]
