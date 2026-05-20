"""Worker process entrypoint.

Reads messages from the Azure Service Bus queue and dispatches
pipeline runs via the orchestrator.
"""
import asyncio

from lectora_backend.core.orchestrator import Orchestrator
from lectora_backend.core.logging_config import configure_logging


configure_logging()


async def main() -> None:
    orchestrator = Orchestrator()
    await orchestrator.listen()


if __name__ == "__main__":
    asyncio.run(main())
