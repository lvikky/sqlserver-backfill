from datetime import date
from unittest.mock import Mock

from backfill.config import Environment, Table
from backfill.metadata import Column

SOURCE = date(2026, 9, 18)
TARGET = date(2026, 9, 21)
TABLE = Table("banking_sfts", "LST_SF_EXTRACT", "BANKING_SFTS", "AS_OF_DATE")
COLUMNS = (Column("ENTITY", 1, "varchar"), Column("AS_OF_DATE", 2, "date"),
           Column("DT_EL_EXECUTIONTIMESTAMP", 3, "datetime2"))


def environment(name="dev"):
    sections = {"dev": "SQLSERVER_DEV", "uat": "SQLSERVER_UAT", "prod": "SQLSERVER_PRD"}
    return Environment(name, sections[name], f"test-{name}", f"TEST_{name.upper()}",
                       "ODBC Driver 18 for SQL Server")


def ini():
    return "\n".join(f"""[{section}]
DRIVER = {{ODBC Driver 18 for SQL Server}}
SERVER = test-{env}
DATABASE = TEST_{env.upper()}
Trusted_Connection = yes
Encrypt = yes
TrustServerCertificate = no
""" for env, section in (("dev", "SQLSERVER_DEV"), ("uat", "SQLSERVER_UAT"), ("prod", "SQLSERVER_PRD")))


def client(counts=(0, 10, 0, 10, 10)):
    fake = Mock()
    fake.session_id = 42
    fake.metadata.return_value = COLUMNS
    fake.count.side_effect = counts
    fake.insert.return_value = 10
    return fake


class MemoryAudit:
    def __init__(self):
        self.context = {}
        self.events = []

    def emit(self, event, **fields):
        self.events.append({**self.context, "event": event, **fields})
