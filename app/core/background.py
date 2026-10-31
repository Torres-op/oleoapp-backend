import asyncio
import logging
from collections.abc import Coroutine
from typing import Any

logger = logging.getLogger(__name__)

# Referência às tarefas em andamento: sem ela, o coletor de lixo pode descartar uma
# tarefa criada com asyncio.create_task antes de ela terminar.
_tasks: set[asyncio.Task[None]] = set()


def spawn(coro: Coroutine[Any, Any, None]) -> asyncio.Task[None]:
    """Roda `coro` em segundo plano. Quem chama trata as próprias exceções dentro dela."""
    task = asyncio.create_task(coro)
    _tasks.add(task)
    task.add_done_callback(_tasks.discard)
    return task


async def drain(timeout: float | None = None) -> None:
    """Aguarda as tarefas em segundo plano (no encerramento da API e nos testes).

    As que não terminarem dentro de `timeout` segundos são canceladas.
    """
    tasks = {task for task in _tasks if not task.done()}
    if not tasks:
        return
    _, pending = await asyncio.wait(tasks, timeout=timeout)
    for task in pending:
        task.cancel()
    if pending:
        logger.warning("%d tarefa(s) em segundo plano cancelada(s) no encerramento.", len(pending))
