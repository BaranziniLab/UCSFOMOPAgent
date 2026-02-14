"""
UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server

An MCP server for querying the UCSF OMOP electronic health records database
for rapid clinical data retrieval.
"""

__version__ = "0.1.0"

from ucsfomopagent.server import create_ucsf_omop_server, main, UCSFOMOPConfig

__all__ = ["create_ucsf_omop_server", "main", "UCSFOMOPConfig", "__version__"]
