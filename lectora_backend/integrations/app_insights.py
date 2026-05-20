"""Application Insights integration for infrastructure monitoring."""
import logging
import os

logger = logging.getLogger(__name__)

try:
    from azure.monitor.opentelemetry import configure_azure_monitor
    connection_string = os.getenv("APPLICATIONINSIGHTS_CONNECTION_STRING", "")
    if connection_string:
        configure_azure_monitor(connection_string=connection_string)
        logger.info("Azure Monitor / App Insights configured")
except ImportError:
    logger.warning("azure-monitor-opentelemetry not installed – App Insights disabled")
