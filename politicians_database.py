"""
politicians_database.py — SQLAlchemy models for congressional + insider trade data
Separate DB (politicians.db) from stocks.db to keep concerns isolated.
"""
from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    Boolean, Date, DateTime, Text, UniqueConstraint, Index, event
)
from sqlalchemy.orm import declarative_base, sessionmaker

import config
_DB_PATH = config.POL_DB_PATH  # canonical resolution (under DATA_DIR unless POL_DB_PATH set)
DATABASE_URL = f"sqlite:///{_DB_PATH}"

engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
)

@event.listens_for(engine, "connect")
def _set_wal_mode(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")

SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Politician(Base):
    __tablename__ = "politicians"
    bioguide_id = Column(String, primary_key=True)
    first_name  = Column(String)
    last_name   = Column(String)
    chamber     = Column(String)   # 'house' | 'senate'
    party       = Column(String)
    state       = Column(String)
    district    = Column(String)   # house only
    active      = Column(Boolean, default=True)


class Committee(Base):
    __tablename__ = "committees"
    committee_id        = Column(String, primary_key=True)
    name                = Column(String)
    chamber             = Column(String)
    parent_committee_id = Column(String)   # NULL for full committees


class CommitteeMembership(Base):
    __tablename__ = "committee_memberships"
    bioguide_id  = Column(String, primary_key=True)
    committee_id = Column(String, primary_key=True)
    start_date   = Column(Date,   primary_key=True)
    role         = Column(String)   # 'Chair' | 'Ranking Member' | 'Member'
    end_date     = Column(Date)     # NULL = current


class CongressionalTrade(Base):
    __tablename__ = "congressional_trades"
    trade_id          = Column(String, primary_key=True)  # hash(bioguide+ticker+date+amount+type)
    bioguide_id       = Column(String, index=True)
    ticker            = Column(String, index=True)
    asset_description = Column(Text)
    transaction_date  = Column(Date,   index=True)
    disclosure_date   = Column(Date)
    transaction_type  = Column(String)  # 'purchase' | 'sale_full' | 'sale_partial' | 'exchange'
    amount_min        = Column(Integer)
    amount_max        = Column(Integer)
    owner             = Column(String)  # 'self' | 'spouse' | 'dependent' | 'joint'
    source            = Column(String)  # 'senate_stock_watcher'
    ingested_at       = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("ix_ctrade_ticker_date", "ticker", "transaction_date"),
        Index("ix_ctrade_bio_date",    "bioguide_id", "transaction_date"),
    )


class InsiderTrade(Base):
    __tablename__ = "insider_trades"
    filing_id        = Column(String, primary_key=True)   # SEC accession number
    ticker           = Column(String, index=True)
    company_name     = Column(String)
    insider_name     = Column(String)
    insider_title    = Column(String)
    transaction_date = Column(Date,   index=True)
    transaction_type = Column(String)  # 'P' purchase | 'S' sale | 'A' award etc
    shares           = Column(Float)
    price_per_share  = Column(Float)
    total_value      = Column(Float)
    shares_owned_after = Column(Float)
    form_type        = Column(String)  # '4' | '4/A'
    filed_date       = Column(Date)
    source_url       = Column(String)
    ingested_at      = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (
        Index("ix_insider_ticker_date", "ticker", "transaction_date"),
    )


class PolTickerMetadata(Base):
    __tablename__ = "pol_ticker_metadata"
    ticker       = Column(String, primary_key=True)
    company_name = Column(String)
    sector       = Column(String)
    industry     = Column(String)


def _migrate():
    """Idempotent migrations for already-created DBs. A UNIQUE index on the
    insider logical key stops the EDGAR + mirror sources from double-inserting
    the same trade. Creation is skipped (with a warning) if duplicates still
    exist — run `python validate.py --fix` first to collapse them."""
    from sqlalchemy import text, inspect
    insp = inspect(engine)
    if "insider_trades" not in insp.get_table_names():
        return
    existing = {ix["name"] for ix in insp.get_indexes("insider_trades")}
    if "uq_insider_logical" not in existing:
        with engine.begin() as conn:
            dupes = conn.execute(text("""
                SELECT COUNT(*) - COUNT(DISTINCT
                    ticker || '|' || transaction_date || '|' || insider_name
                          || '|' || transaction_type || '|' || shares)
                FROM insider_trades
            """)).scalar() or 0
            if dupes == 0:
                conn.execute(text("""
                    CREATE UNIQUE INDEX uq_insider_logical ON insider_trades
                    (ticker, transaction_date, insider_name, transaction_type, shares)
                """))
            else:
                print(f"[migrate] {dupes} insider duplicates present — "
                      f"run `python validate.py --fix` before the unique index can be created.")


def init_pol_db():
    Base.metadata.create_all(engine)
    _migrate()


def get_pol_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
