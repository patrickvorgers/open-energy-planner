"""Bounded native worker process. No solver execution on the HTTP/HA event loop."""

import asyncio
import multiprocessing
from os import environ
from time import monotonic

from open_energy_planner.optimization.domain import (
    DEFAULT_SOLVER_TIME_LIMIT_SECONDS,
    SolveOptions,
    SolveResult,
    Status,
    Strategy,
)

WORKER_OVERHEAD_SECONDS = 5
DEFAULT_WORKER_TIMEOUT_SECONDS = DEFAULT_SOLVER_TIME_LIMIT_SECONDS + WORKER_OVERHEAD_SECONDS
GRACEFUL_JOIN_SECONDS = 0.2
TERMINATE_JOIN_SECONDS = 1
WORKER_STOP_POLL_SECONDS = 0.2


def configure_worker_threads():
    """Bound numerical libraries inside each isolated native child before their imports."""
    for name in (
        "OPENBLAS_NUM_THREADS",
        "OMP_NUM_THREADS",
        "MKL_NUM_THREADS",
        "NUMEXPR_NUM_THREADS",
        "BLIS_NUM_THREADS",
    ):
        environ[name] = "1"


def solve_in_child(problem, sender, solver_time_limit, strategy=Strategy.COST):
    """Run whole-watt HiGHS optimization in a spawned child with bounded numeric thread counts."""
    configure_worker_threads()
    from open_energy_planner.optimization.adapters.highs import HighsSolver

    try:
        sender.send(HighsSolver().solve(problem, SolveOptions(solver_time_limit, strategy)))
    finally:
        sender.close()


class ProcessSolver:
    """Isolate native optimization behind a timed child process and asynchronous solver port."""

    def __init__(
        self,
        timeout_s=DEFAULT_WORKER_TIMEOUT_SECONDS,
        solver_time_limit_s=DEFAULT_SOLVER_TIME_LIMIT_SECONDS,
    ):
        """Set positive parent/solver deadlines in seconds. The parent also bounds worker transport."""
        self.timeout_s, self.solver_time_limit_s = timeout_s, solver_time_limit_s
        if timeout_s <= 0 or solver_time_limit_s <= 0:
            raise ValueError("Worker deadlines must be positive")

    async def solve(self, problem, *, strategy=Strategy.COST):
        """Offload child-process orchestration to a thread instead of blocking the event loop."""
        return await asyncio.to_thread(self._run, problem, strategy)

    def _run(self, problem, strategy=Strategy.COST):
        """Use the shared bounded child lifecycle and retain the existing solver failure contract."""
        try:
            result = run_child(
                solve_in_child, problem, self.timeout_s, self.solver_time_limit_s, strategy
            )
            if not isinstance(result, SolveResult):
                raise ValueError("Invalid worker result")  # noqa: TRY004 - Preserve the existing ValueError validation contract.
            return result
        except TimeoutError:
            return SolveResult(Status.ERROR, None, "worker_timeout", 0, 0, None)
        except (OSError, RuntimeError, ValueError):
            return SolveResult(Status.ERROR, None, "worker_failed", 0, 0, None)


def run_child(target, payload, timeout_s, *options, cancel=None):
    """Run one trusted local numerical operation, receiving before bounded child cleanup.

    Call only off the event loop. Payloads contain plain domain values, never
    live engines or credentials. Native failure/timeout cannot kill the app.
    Callers validate the returned operation-specific value.
    """
    if cancel is not None and cancel.is_set():
        raise RuntimeError("worker_cancelled")
    context = multiprocessing.get_context("spawn")
    receiver, sender = context.Pipe(duplex=False)
    process = context.Process(target=target, args=(payload, sender, *options), daemon=True)
    try:
        process.start()
        sender.close()
        deadline = monotonic() + timeout_s
        while True:
            if cancel is not None and cancel.is_set():
                raise RuntimeError("worker_cancelled")
            remaining = deadline - monotonic()
            if remaining <= 0:
                raise TimeoutError("worker_timeout")
            if receiver.poll(
                min(remaining, WORKER_STOP_POLL_SECONDS) if cancel is not None else remaining
            ):
                break
        try:
            return receiver.recv()
        except EOFError:
            raise RuntimeError("worker_failed") from None
    finally:
        sender.close()
        receiver.close()
        if process.pid is not None:
            process.join(timeout=GRACEFUL_JOIN_SECONDS)
            if process.is_alive():
                process.terminate()
                process.join(timeout=TERMINATE_JOIN_SECONDS)
            if process.is_alive():
                process.kill()
                process.join()
            process.close()
