from collections.abc import Iterator
from contextlib import contextmanager

import psycopg
from psycopg.rows import dict_row

from realestate_crm.config import get_settings


@contextmanager
def connection() -> Iterator[psycopg.Connection]:
    """Open a short-lived connection; no app state depends on instance reuse."""
    settings = get_settings()
    with psycopg.connect(
        settings.supabase_database_url.get_secret_value(),
        row_factory=dict_row,
        connect_timeout=8,
        application_name="real-estate-telegram-crm",
    ) as conn:
        yield conn
