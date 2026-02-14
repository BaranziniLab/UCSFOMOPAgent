"""
UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server

Command-line interface for the UCSFOMOPAgent server.
"""

import logging
import os
from typing import Optional

from ucsfomopagent.server import main as server_main


logger = logging.getLogger("UCSFOMOPAgent")


def main() -> None:
    """
    Main entry point for the UCSFOMOPAgent CLI.

    Reads configuration from environment variables and starts the server.
    Environment variables are typically set by the MCP client.
    """

    # Set up logging
    log_level = os.getenv("OMOP_LOG_LEVEL", "INFO")
    logging.basicConfig(level=getattr(logging, log_level.upper()))

    logger.info("Starting UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server")

    # Run the server with configuration from environment variables
    server_main(
        username=os.getenv("CLINICAL_RECORDS_USERNAME"),
        password=os.getenv("CLINICAL_RECORDS_PASSWORD"),
        log_level=log_level
    )


if __name__ == "__main__":
    main()
