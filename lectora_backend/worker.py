"""Worker process entrypoint.

Reads messages from the Azure Service Bus queue and dispatches
pipeline runs via the orchestrator.
"""
import asyncio

from lectora_backend.core.orchestrator import Orchestrator
from lectora_backend.core.logging_config import configure_logging
from lectora_backend.pipeline.shared_llm_config import flush_langfuse, shutdown_langfuse


configure_logging()


async def main() -> None:
    orchestrator = Orchestrator()
    try:
        await orchestrator.listen()
    finally:
        flush_langfuse()
        shutdown_langfuse()


if __name__ == "__main__":
    asyncio.run(main())
