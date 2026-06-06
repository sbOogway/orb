-- ============================================================================
--  IBKR Data — SQLite schema & seed data
--  Usage:  sqlite3 market_data.db < init.sql
-- ============================================================================

-- ---------------------------------------------------------------------------
--  1. Metadata tables
-- ---------------------------------------------------------------------------

-- Tracks which days have been scraped per ticker/source/timeframe
CREATE TABLE IF NOT EXISTS cache (
    ticker    TEXT NOT NULL,
    timeframe TEXT NOT NULL,
    day       TEXT NOT NULL,
    source    TEXT NOT NULL DEFAULT 'ibkr',
    PRIMARY KEY (ticker, timeframe, day, source)
);

-- Reference universe of tradeable tickers
CREATE TABLE IF NOT EXISTS tickers (
    symbol TEXT PRIMARY KEY,
    name   TEXT,
    sector TEXT
);

-- ---------------------------------------------------------------------------
--  2. Seed the ticker universe  (32 major US stocks, 6 sectors)
-- ---------------------------------------------------------------------------

-- Technology
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AAPL', 'Apple Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('MSFT', 'Microsoft Corp.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('GOOGL', 'Alphabet Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AMZN', 'Amazon.com Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('NVDA', 'NVIDIA Corp.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('META', 'Meta Platforms Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('TSLA', 'Tesla Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AVGO', 'Broadcom Inc.', 'Technology');

-- Finance
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('JPM', 'JPMorgan Chase & Co.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('BAC', 'Bank of America Corp.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('WFC', 'Wells Fargo & Co.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('GS', 'Goldman Sachs Group Inc.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('MS', 'Morgan Stanley', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('V', 'Visa Inc.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('MA', 'Mastercard Inc.', 'Finance');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('BLK', 'BlackRock Inc.', 'Finance');

-- Consumer
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AMGN', 'Amgen Inc.', 'Healthcare');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AMAT', 'Applied Materials Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('AMD', 'Advanced Micro Devices Inc.', 'Technology');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('EEM', 'iShares MSCI Emerging Markets ETF', 'Finance');

-- Healthcare
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('JNJ', 'Johnson & Johnson', 'Healthcare');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('PFE', 'Pfizer Inc.', 'Healthcare');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('UNH', 'UnitedHealth Group Inc.', 'Healthcare');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('ABBV', 'AbbVie Inc.', 'Healthcare');

-- Energy
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('XOM', 'Exxon Mobil Corp.', 'Energy');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('CVX', 'Chevron Corp.', 'Energy');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('COP', 'ConocoPhillips', 'Energy');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('SLB', 'Schlumberger NV', 'Energy');

-- Industrial
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('CAT', 'Caterpillar Inc.', 'Industrial');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('GE', 'General Electric Co.', 'Industrial');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('BA', 'The Boeing Co.', 'Industrial');
INSERT OR REPLACE INTO tickers (symbol, name, sector) VALUES ('HON', 'Honeywell International Inc.', 'Industrial');
