"""Database connection setup for the SaaS starter example."""

import os

from sqlmodel import Session, create_engine

DATABASE_URL = os.environ.get("DATABASE_URL", "postgresql://alter:alter@localhost:5433/alter_test")

engine = create_engine(DATABASE_URL)


def get_session():
    """Yield a database session."""
    with Session(engine) as session:
        yield session
