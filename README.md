# UCSFOMOPAgent

An MCP (Model Context Protocol) server for querying the UCSF OMOP electronic health records database for rapid clinical data retrieval.

## BioRouter Extension

**[Download ucsfomopagent.brxt](https://github.com/BaranziniLab/UCSFOMOPAgent/releases/latest/download/ucsfomopagent.brxt)**

Drag the `.brxt` file into BioRouter's **Extensions → Add extension** dialog. BioRouter will install the virtual environment automatically and prompt for required credentials.

| Variable | Required | Default | Description |
|----------|----------|---------|-------------|
| `CLINICAL_RECORDS_USERNAME` | ✅ | — | UCSF network username |
| `CLINICAL_RECORDS_PASSWORD` | ✅ | — | UCSF network password |
| `OMOP_LOG_LEVEL` | optional | `INFO` | Logging level |

## Features

- **Query UCSF OMOP Database**: Execute SQL queries on the UCSF OMOP de-identified clinical database
- **List Available Tables**: Discover all available clinical data tables
- **Pre-configured Server**: Server and database endpoints are pre-configured - only provide your credentials!

## Installation

### From GitHub (using uvx)

```bash
uvx --from git+https://github.com/BaranziniLab/UCSFOMOPAgent ucsfomopagent
```

### Local Installation

```bash
cd UCSFOMOPAgent
pip install -e .
```

## Usage

### As an MCP Server

Add to your MCP client configuration (e.g., Claude Desktop):

```json
{
  "mcpServers": {
    "ucsfomopagent": {
      "command": "uvx",
      "args": ["--from", "git+https://github.com/BaranziniLab/UCSFOMOPAgent", "ucsfomopagent"],
      "env": {
        "CLINICAL_RECORDS_USERNAME": "CAMPUS\\YourUsername",
        "CLINICAL_RECORDS_PASSWORD": "YourPassword"
      }
    }
  }
}
```

### Direct Command Line

Set environment variables and run:

```bash
export CLINICAL_RECORDS_USERNAME="CAMPUS\\YourUsername"
export CLINICAL_RECORDS_PASSWORD="YourPassword"
ucsfomopagent
```

## Configuration

Pre-configured server settings:
- **Server**: QCDIDDWDB001.ucsfmedicalcenter.org
- **Database**: OMOP_DEID

Required environment variables (you must provide):
- `CLINICAL_RECORDS_USERNAME`: Your UCSF database username (e.g., "CAMPUS\\username")
- `CLINICAL_RECORDS_PASSWORD`: Your UCSF database password

Optional environment variable:
- `OMOP_LOG_LEVEL`: Set logging level (DEBUG, INFO, WARNING, ERROR) - defaults to INFO

## Available Tools

### 1. `query_ucsf_omop`

Execute a READ-ONLY SQL query on the UCSF OMOP electronic health records database.

**Parameters:**
- `sql_query` (string, required): SQL SELECT query for rapid clinical record retrieval

**Example:**
```sql
SELECT TOP 10 person_id, gender_concept_id, year_of_birth
FROM dbo.person
WHERE year_of_birth > 1980
```

### 2. `list_ucsf_omop_tables`

List all available clinical data tables in the UCSF OMOP electronic health records database.

**Returns:** JSON list of tables with schema, name, type, and full name.

## Security

This server enforces read-only access to the UCSF OMOP database. Write operations (INSERT, UPDATE, DELETE, etc.) are not permitted.

**Important:**
- Only SELECT queries are allowed
- The database contains de-identified patient data
- Follow all UCSF data use policies and HIPAA regulations

## OMOP Common Data Model

The UCSF OMOP database follows the OMOP Common Data Model (CDM) standard, which includes standardized tables such as:

- `person`: Patient demographics
- `condition_occurrence`: Diagnosis and conditions
- `drug_exposure`: Medication records
- `procedure_occurrence`: Medical procedures
- `measurement`: Lab results and vital signs
- `observation`: Clinical observations
- `visit_occurrence`: Healthcare visits

## License

MIT

## Authors

- Wanjun Gu (wanjun.gu@ucsf.edu)
- Gianmarco Bellucci (gianmarco.bellucci@ucsf.edu)

## About OMOP

The Observational Medical Outcomes Partnership (OMOP) Common Data Model (CDM) is designed to standardize the structure and content of observational data to enable efficient analyses across disparate datasets.
