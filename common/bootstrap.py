"""Local-only: create DynamoDB tables in DynamoDB Local. ``python -m common.bootstrap``"""

from common.dynamo import ensure_tables
from common.settings import get_settings

if __name__ == "__main__":
    ensure_tables(get_settings())
