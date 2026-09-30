from datetime import datetime
from sqlalchemy import (
    create_engine, Column, String, Float, Integer,
    DateTime, Text, UniqueConstraint, Index
)
from sqlalchemy.orm import declarative_base, sessionmaker

import config
_DB_PATH    = config.DB_PATH  # canonical resolution (under DATA_DIR unless DB_PATH set)
DATABASE_URL = f"sqlite:///{_DB_PATH}"
engine = create_engine(
    DATABASE_URL,
    connect_args={"check_same_thread": False},
    # WAL mode: readers don't block writers and vice versa
    # This prevents lock errors when pipeline runs while server is live
)

from sqlalchemy import event
@event.listens_for(engine, "connect")
def _set_wal_mode(dbapi_conn, _):
    dbapi_conn.execute("PRAGMA journal_mode=WAL")
    dbapi_conn.execute("PRAGMA busy_timeout=5000")
SessionLocal = sessionmaker(bind=engine, autocommit=False, autoflush=False)
Base = declarative_base()


class Stock(Base):
    __tablename__ = "stocks"
    ticker       = Column(String, primary_key=True)
    name         = Column(String)
    sector       = Column(String)
    industry     = Column(String)
    country      = Column(String)
    mkt_cap      = Column(Float)
    cap_size     = Column(String)
    price        = Column(Float)
    exchange     = Column(String)
    rank         = Column(Integer)
    last_updated = Column(DateTime, default=datetime.utcnow)


class Fundamentals(Base):
    __tablename__ = "fundamentals"
    ticker           = Column(String, primary_key=True)
    name             = Column(String)
    sector           = Column(String)
    industry         = Column(String)
    cap_size         = Column(String)
    rank             = Column(Integer)
    price            = Column(Float)
    # Income statement
    rev_now          = Column(Float)
    gross_profit     = Column(Float)
    operating_income = Column(Float)
    net_income       = Column(Float)
    ebitda           = Column(Float)
    eps_ttm          = Column(Float)
    eps_fwd          = Column(Float)
    # Cash flow
    operating_cf     = Column(Float)
    capex            = Column(Float)
    fcf              = Column(Float)
    fcf_3yr_avg_raw  = Column(Float)
    # Balance sheet
    total_debt       = Column(Float)
    cash             = Column(Float)
    equity           = Column(Float)
    shares           = Column(Float)
    bvps             = Column(Float)
    net_debt         = Column(Float)
    mkt_cap_raw      = Column(Float)
    # Valuation ratios
    pe               = Column(Float)
    fwd_pe           = Column(Float)
    ev_ebitda        = Column(Float)
    ps               = Column(Float)
    pb               = Column(Float)
    peg              = Column(Float)
    # Leverage / coverage
    d_to_e           = Column(Float)
    d_to_ebitda      = Column(Float)
    int_cov          = Column(Float)
    curr_ratio       = Column(Float)
    # Returns
    roic             = Column(Float)
    roc_greenblatt   = Column(Float)   # EBIT / (NWC + net PP&E), percent
    ebit_ev_yield    = Column(Float)   # EBIT / EV, percent
    # Magic Formula provenance (issue #17): which source supplied each leg above.
    magic_source     = Column(String)  # "xbrl" | "yfinance" | "mixed"
    magic_period_end = Column(String)  # fiscal-year end of the 10-K used (as-filed)
    magic_accession  = Column(String)  # SEC accession of that 10-K
    magic_derived    = Column(String)  # #259 derivation rules that fired, comma-joined
    roe              = Column(Float)
    roa              = Column(Float)
    # Margins
    gross_margin     = Column(Float)
    op_margin        = Column(Float)
    net_margin       = Column(Float)
    fcf_margin       = Column(Float)
    # CAGRs
    rev_cagr_1y      = Column(Float)
    rev_cagr_3y      = Column(Float)
    rev_cagr_5y      = Column(Float)
    rev_cagr_10y     = Column(Float)
    eps_cagr_1y      = Column(Float)
    eps_cagr_3y      = Column(Float)
    eps_cagr_5y      = Column(Float)
    eps_cagr_10y     = Column(Float)
    fcf_cagr_1y      = Column(Float)
    fcf_cagr_3y      = Column(Float)
    fcf_cagr_5y      = Column(Float)
    fcf_cagr_10y     = Column(Float)
    bvps_cagr_1y     = Column(Float)
    bvps_cagr_3y     = Column(Float)
    bvps_cagr_5y     = Column(Float)
    bvps_cagr_10y    = Column(Float)
    roic_avg_1y      = Column(Float)
    roic_avg_3y      = Column(Float)
    roic_avg_5y      = Column(Float)
    roic_avg_10y     = Column(Float)
    roic_improving   = Column(Integer)
    # Rule #1
    rule1_roic       = Column(Integer)
    rule1_eps        = Column(Integer)
    rule1_sales      = Column(Integer)
    rule1_equity     = Column(Integer)
    rule1_fcf        = Column(Integer)
    rule1_passes     = Column(Integer)
    longevity_score  = Column(Integer)
    longevity_rank   = Column(String)
    # Misc
    data_quality     = Column(Integer)
    next_earnings    = Column(String)
    eps_surprise     = Column(Float)
    analyst_mean     = Column(Float)
    analyst_rec      = Column(String)
    num_analysts     = Column(Integer)
    ret_1y           = Column(Float)
    rsi              = Column(Float)
    ma_ratio         = Column(Float)
    last_updated     = Column(DateTime, default=datetime.utcnow)


class Valuation(Base):
    __tablename__ = "valuations"
    ticker           = Column(String, primary_key=True)
    dcf_fair_value   = Column(Float)
    dcf_mos_price    = Column(Float)
    dcf_upside_pct   = Column(Float)
    dcf_signal       = Column(String)
    dcf_wacc         = Column(Float)
    dcf_growth       = Column(Float)
    comps_fair_value = Column(Float)
    comps_upside_pct = Column(Float)
    comps_signal     = Column(String)
    comps_peers      = Column(Integer)
    m3_fair_value    = Column(Float)
    m3_upside_pct    = Column(Float)
    m3_signal        = Column(String)
    score_composite  = Column(Integer)
    score_label      = Column(String)
    score_stars      = Column(String)
    avg_upside       = Column(Float)
    last_updated     = Column(DateTime, default=datetime.utcnow)


class News(Base):
    __tablename__ = "news"
    id           = Column(Integer, primary_key=True, autoincrement=True)
    ticker       = Column(String, index=True)
    title        = Column(Text)
    url          = Column(Text)
    publisher    = Column(String)
    published_at = Column(DateTime)
    sentiment    = Column(Float)
    summary      = Column(Text)
    last_updated = Column(DateTime, default=datetime.utcnow)
    __table_args__ = (UniqueConstraint("ticker", "url", name="uq_news_ticker_url"),)


class PriceHistory(Base):
    __tablename__ = "price_history"
    id     = Column(Integer, primary_key=True, autoincrement=True)
    ticker = Column(String)
    date   = Column(String)
    close  = Column(Float)
    volume = Column(Float)
    __table_args__ = (UniqueConstraint("ticker", "date", name="uq_price_ticker_date"),)


class ETF(Base):
    __tablename__ = "etfs"
    ticker         = Column(String, primary_key=True)
    name           = Column(String)
    category       = Column(String)   # yfinance fund category, e.g. "Large Blend"
    asset_class    = Column(String)   # equity | bond | commodity | ...
    aum            = Column(Float)    # total net assets
    expense_ratio  = Column(Float)    # annual %, e.g. 0.09
    yield_pct      = Column(Float)    # trailing 12m yield %
    ytd_return     = Column(Float)    # %
    price          = Column(Float)
    weighted_score = Column(Float)    # Σ(weight·holding score)/Σ(weight covered)
    covered_weight = Column(Float)    # fraction of top-holdings weight we had scores for
    holdings_count = Column(Integer)  # number of holdings stored (top-N)
    last_updated   = Column(DateTime, default=datetime.utcnow)


class ETFHolding(Base):
    __tablename__ = "etf_holdings"
    etf_ticker     = Column(String, primary_key=True)
    holding_ticker = Column(String, primary_key=True)
    holding_name   = Column(String)
    weight         = Column(Float)    # portfolio weight fraction (0-1)
    __table_args__ = (Index("ix_etf_holdings_etf", "etf_ticker"),)


def _migrate():
    """Idempotent SQLite migrations. create_all() won't ALTER an existing
    table, so add new columns on already-created DBs here."""
    from sqlalchemy import inspect, text
    insp = inspect(engine)
    if "news" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("news")}
        if "summary" not in cols:
            with engine.begin() as conn:
                conn.execute(text("ALTER TABLE news ADD COLUMN summary TEXT"))
    if "fundamentals" in insp.get_table_names():
        cols = {c["name"] for c in insp.get_columns("fundamentals")}
        for col in ("roc_greenblatt", "ebit_ev_yield"):
            if col not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE fundamentals ADD COLUMN {col} FLOAT"))
        for col in ("magic_source", "magic_period_end", "magic_accession", "magic_derived"):
            if col not in cols:
                with engine.begin() as conn:
                    conn.execute(text(f"ALTER TABLE fundamentals ADD COLUMN {col} VARCHAR"))


def init_db():
    Base.metadata.create_all(engine)
    _migrate()


def get_db():
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()


def upsert(db, model, data: dict):
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert
    stmt = sqlite_insert(model).values(**data)
    update_cols = {k: v for k, v in data.items() if k != "ticker"}
    stmt = stmt.on_conflict_do_update(index_elements=["ticker"], set_=update_cols)
    db.execute(stmt)
