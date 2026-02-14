"""
UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server

An MCP server for querying the UCSF OMOP electronic health records database
for rapid clinical data retrieval.
"""
import json
import logging
import os
import re
import sys
from typing import Any, Literal, Optional

import pymssql
from fastmcp.exceptions import ToolError
from fastmcp.server import FastMCP
from fastmcp.tools.tool import ToolResult, TextContent
from mcp.types import ToolAnnotations
from pydantic import BaseModel, Field

logger = logging.getLogger("UCSFOMOPAgent")

# Hardcoded UCSF OMOP configuration (server and database are fixed)
OMOP_SERVER = "QCDIDDWDB001.ucsfmedicalcenter.org"
OMOP_DATABASE = "OMOP_DEID"


class UCSFOMOPConfig(BaseModel):
    """UCSF OMOP clinical database configuration"""
    server: str = Field(default=OMOP_SERVER, description="EHR database server host")
    database: str = Field(default=OMOP_DATABASE, description="EHR database name")
    username: str = Field(..., description="EHR database username (must be provided)")
    password: str = Field(..., description="EHR database password (must be provided)")
    log_level: str = Field("INFO", description="Logging level (DEBUG, INFO, WARNING, ERROR)")


def _is_write_query(query: str) -> bool:
    """Check if the query contains write operations"""
    return re.search(r"\b(MERGE|CREATE|SET|DELETE|REMOVE|ADD|INSERT|UPDATE|DROP|ALTER|TRUNCATE|GRANT|REVOKE|EXEC|EXECUTE|SP_)\b", query, re.IGNORECASE) is not None


class ClinicalQueryValidator:
    """Clinical record query validator for read-only operations"""

    @staticmethod
    def is_read_only_clinical_query(query: str) -> bool:
        clean_query = query.strip().upper()

        # Allowed statements for clinical record queries
        allowed_statements = ['SELECT', 'WITH', 'DECLARE']

        # Check if starts with allowed statement
        starts_with_allowed = any(clean_query.startswith(stmt) for stmt in allowed_statements)
        if not starts_with_allowed:
            return False

        # Check for forbidden statements
        if _is_write_query(query):
            return False

        # Check for SQL injection patterns
        has_dangerous_chars = re.search(r';\s*\w+', clean_query)
        if has_dangerous_chars:
            return False

        return True


def create_ucsf_omop_server(config: UCSFOMOPConfig) -> FastMCP:
    """Create UCSFOMOPAgent server with UCSF OMOP clinical database tools"""

    # Set up logging
    logging.basicConfig(level=getattr(logging, config.log_level.upper()))

    mcp = FastMCP("UCSFOMOPAgent")

    def get_clinical_records_connection():
        """Get clinical records database connection"""
        try:
            return pymssql.connect(
                server=config.server,
                user=config.username,
                password=config.password,
                database=config.database
            )
        except Exception as e:
            logger.error(f"Clinical records connection failed: {e}")
            raise ToolError(f"Clinical records connection failed: {e}")

    @mcp.tool(
        name="query_ucsf_omop",
        annotations=ToolAnnotations(
            title="Query UCSF OMOP Electronic Health Records",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False
        )
    )
    def query_ucsf_omop(
        sql_query: str = Field(..., description="SQL SELECT query for rapid clinical record retrieval (read-only)")
    ) -> ToolResult:
        """Execute a READ-ONLY SQL query on UCSF OMOP electronic health records for rapid clinical data retrieval."""

        # Validate query is read-only
        if not ClinicalQueryValidator.is_read_only_clinical_query(sql_query):
            raise ToolError("Only SELECT queries are allowed for clinical record queries")

        try:
            conn = get_clinical_records_connection()
            cursor = conn.cursor()
            cursor.execute(sql_query)

            # Get column names
            columns = [desc[0] for desc in cursor.description] if cursor.description else []

            # Get all rows
            rows = cursor.fetchall()

            # Format as CSV
            if columns:
                csv_lines = [",".join(columns)]
                csv_lines.extend([",".join(map(str, row)) for row in rows])
                result_text = "\n".join(csv_lines)
            else:
                result_text = "Clinical query executed successfully (no results returned)"

            cursor.close()
            conn.close()

            logger.debug(f"Clinical records query returned {len(rows) if rows else 0} rows")

            return ToolResult(content=[TextContent(type="text", text=result_text)])

        except Exception as e:
            logger.error(f"Clinical records query error: {e}")
            raise ToolError(f"Electronic health records error: {e}")

    @mcp.tool(
        name="list_ucsf_omop_tables",
        annotations=ToolAnnotations(
            title="List UCSF OMOP Clinical Data Tables",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=False
        )
    )
    def list_ucsf_omop_tables() -> ToolResult:
        """List all available clinical data tables in the UCSF OMOP electronic health records database."""

        query = """
        SELECT TABLE_SCHEMA, TABLE_NAME, TABLE_TYPE
        FROM INFORMATION_SCHEMA.TABLES
        WHERE TABLE_TYPE = 'BASE TABLE'
        ORDER BY TABLE_SCHEMA, TABLE_NAME
        """

        try:
            conn = get_clinical_records_connection()
            cursor = conn.cursor()
            cursor.execute(query)

            tables = cursor.fetchall()

            # Format as JSON for better structure
            table_list = [
                {
                    "schema": table[0],
                    "table_name": table[1],
                    "type": table[2],
                    "full_name": f"{table[0]}.{table[1]}"
                }
                for table in tables
            ]

            cursor.close()
            conn.close()

            return ToolResult(content=[TextContent(type="text", text=json.dumps(table_list, indent=2))])

        except Exception as e:
            logger.error(f"Error listing clinical tables: {e}")
            raise ToolError(f"Error listing clinical data tables: {e}")

    return mcp


def main(
    transport: Literal["stdio", "sse", "http"] = "stdio",
    username: Optional[str] = None,
    password: Optional[str] = None,
    log_level: str = "INFO",
    host: str = "127.0.0.1",
    port: int = 8000,
    path: str = "/mcp/",
) -> None:
    """Main entry point for the UCSFOMOPAgent server"""

    # Validate that username and password are provided
    if not username or not password:
        raise ValueError("CLINICAL_RECORDS_USERNAME and CLINICAL_RECORDS_PASSWORD must be provided")

    # Create config
    config = UCSFOMOPConfig(
        username=username,
        password=password,
        log_level=log_level
    )

    logger.info("Starting UCSFOMOPAgent - UCSF OMOP Clinical Database MCP Server")
    logger.info(f"OMOP Server: {config.server}")
    logger.info(f"OMOP Database: {config.database}")
    logger.info(f"Username: {config.username}")

    mcp = create_ucsf_omop_server(config)
    mcp.run()


if __name__ == "__main__":
    # Configuration provided by MCP client through environment variables
    main(
        username=os.getenv("CLINICAL_RECORDS_USERNAME"),
        password=os.getenv("CLINICAL_RECORDS_PASSWORD"),
        log_level=os.getenv("OMOP_LOG_LEVEL", "INFO")
    )
