# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 Vaibhav Agarwal

"""
TATA Money Market Fund - Direct Growth - 13-Year Data Harvester (FIXED)
========================================================================
Target : https://www.tatamutualfund.com/mutual-funds/tata-money-market-fund-direct-growth
Scheme : Tata Money Market Fund - Direct Plan - Growth Option
Scheme Code (AMFI/mfapi) : 119424
ISIN Growth : INF277K01PR6
Direct Inception : 01-Jan-2013  (~13 years to Sep-2026)
Regular Inception: 22-May-2003 (Tata site shows 23y since 2003 for Regular plan)
Benchmark : CRISIL Money Market A-I Index
Category : Debt - Money Market Fund

WHAT WAS BROKEN IN THE OLD SCRIPT (diagnosis):
----------------------------------------------
1. Wrong endpoint : `http://amfiindia.com?mf=3...` returns an HTML landing page
   (Next.js, ~244KB), NOT NAV text. So zero rows were ever found.
   Correct portal is `https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx`.
2. Wrong fund-house code : `mf=3` = Aditya Birla Sun Life, NOT Tata.
   Tata Mutual Fund = `mf=25` (verified live: mf=25 returns Tata Money Market rows).
3. Wrong column indices : old code assumed
     0=Code, 1=Name, 4=NAV, 7=Date (old 2019 history format).
   Current AMFI `NAVAll.txt` format is 8 fields:
     0=SchemeCode, 1=ISIN-Growth, 2=ISIN-DivReinv, 3=BaseName,
     4=Plan, 5=Option, 6=NAV, 7=Date
   e.g. `119424;INF277K01PR6;-;Tata Money Market Fund;Direct Plan;Growth Option;5217.8998;25-Sep-2026`
   So old code saved ISIN as Name and "Direct Plan" as NAV - garbage even if it worked.
   Portal history format is ALSO different again (Name in col 1). Fixed with robust parser.
4. Too-broad filter : ["Tata Money Market Fund","Tata Money Market"] matches
   Regular + Direct + Daily-IDCW variants mixed together. Fixed to isolate
   SchemeCode==119424 / ISIN==INF277K01PR6 / Direct+Growth only.
5. No headers/retries (portal times out often), http instead of https,
   GUI-only (crashes headless/no DISPLAY), only 4 columns (loses ISIN etc.),
   no dedup/sort/validation/stats, date fallback used chunk-start instead of real date.

HOW THIS FIX WORKS (super-robust):
-----------------------------------
Primary   : https://api.mfapi.in/mf/119424  (single JSON call, full history
            01-01-2013 -> today, ~3876 daily NAVs, no 90-day chunking needed)
Fallback 1: AMFI portal mf=25 in 90-day blocks (correct URL + retries + robust parse)
Fallback 2: https://www.amfiindia.com/spages/NAVAll.txt (today only, for validation)
Outputs   : CSV (full daily NAV, ISO dates, sorted, deduped)
            + METADATA.json (all fund facts from Tata site + web research)
            + YEARLY_RETURNS.csv (year-end NAV + annual %)
            + SUMMARY.txt (human-readable factsheet + stats + sources)
Usage     :
  GUI : python3 fetch_data.py
  CLI : python3 fetch_data.py --cli  (auto-used if no DISPLAY)
        python3 fetch_data.py --cli --out-dir ./data
Deps      : requests  (pip install requests)
"""

import os
import sys
import csv
import json
import time
import re
import argparse
from datetime import datetime, timedelta
from collections import OrderedDict

try:
    import requests
except ImportError:
    print("ERROR: 'requests' not installed. Run: pip install requests")
    sys.exit(1)

# ---------------------------------------------------------------------------
# FUND REGISTRY (verified via web search + mfapi.in + Tata site, Sep-2026)
# ---------------------------------------------------------------------------
FUNDS = {
    "money_market": {
        "key": "money_market",
        "data_subdir": "tata-money-market-direct-growth",
        "file_prefix": "tata_money_market_direct_growth_13Y",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Money Market Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Money Market Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-money-market-fund-direct-growth",
        "amfi_scheme_code": "119424",
        "mfapi_scheme_code": 119424,
        "isin_growth": "INF277K01PR6",
        "amfi_fund_house_code": "25",  # mf=25 on portal.amfiindia.com
        "category": "Debt Scheme - Money Market Fund",
        "benchmark": "CRISIL Money Market A-I Index",
        "additional_benchmark": "CRISIL 1 Year T-Bill Index",
        "direct_inception": "2013-01-01",   # Direct Plan launch (all sources agree)
        "regular_inception": "2003-05-22",  # Tata site shows "since 22 May 2003" (Regular)
        "risk": "Moderate",
        "benchmark_risk": "Low to Moderate",
        "exit_load": "Nil",
        "min_sip": 500,
        "min_lumpsum": 5000,
        "managers": ["Amit Somani (since 16 Oct 2013)", "Harsh Dave (since 01 Jun 2026)"],
        "prc_matrix": "B-I (Relatively Low Interest-Rate Risk, Moderate Credit Risk)",
        "objective": "Generate returns with reasonable liquidity by investing in money market instruments.",
        # Snapshots (with dates - NAV moves daily)
        "snapshot_nav": "5217.90 as on 25-Sep-2026 (Tata site); 5195.96 as on 31-Aug-2026 (Groww); 5192.77 as on 28-Aug-2026 (ET Money/Paytm)",
        "snapshot_aum_cr": "32346.66 (24-Sep-2026 Tata site) / 34138.15 (31-Jul-2026 factsheet)",
        "snapshot_expense_pct": "0.15 (Tata site Aug-2026) / 0.18 incl. GST (fact sheets)",
        "snapshot_ytm_pct": "6.74",
        "snapshot_duration_months": "4.97-4.98 (Modified/Macaulay/Avg Maturity)",
        "snapshot_returns": "1Y 6.61% / 3Y 7.54% / 5Y 6.81% / Since-inception ~6.77% (as on 31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-money-market-fund-direct-growth",
            "https://api.mfapi.in/mf/119424 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt (official daily NAV)",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25 (official history, 90-day max)",
            "https://coin.zerodha.com/mf/fund/INF277K01PR6/tata-money-market-fund-direct-growth (ISIN + launch 2013-01-01)",
            "https://www.advisorkhoj.com/mutual-funds-research/Tata-Money-Market-Fund-Direct-Plan-Growth-Option (Direct 01-01-2013)",
            "https://www.etmoney.com/mutual-funds/tata-money-market-fund-direct-growth/16274 (13y7m, returns)",
            "https://groww.in/mutual-funds/tata-money-market-fund-direct-growth",
            "https://www.paytmmoney.com/mutual-funds/scheme/tata-money-market-fund-direct-growth/inf277k01pr6",
        ],
    },
    "ultra_short": {
        "key": "ultra_short",
        "data_subdir": "tata-ultra-short-term-direct-growth",
        "file_prefix": "tata_ultra_short_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Ultra Short Term Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Ultra Short Term Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-ultra-short-term-fund-direct-growth",
        "amfi_scheme_code": "146075",
        "mfapi_scheme_code": 146075,
        "isin_growth": "INF277K011T0",
        "amfi_fund_house_code": "25",
        "category": "Debt Scheme - Ultra Short Duration Fund",
        "benchmark": "CRISIL Ultra Short Duration Debt A-I Index",
        "additional_benchmark": "CRISIL 1 Year T-Bill Index",
        "direct_inception": "2019-01-23",
        "regular_inception": "2019-01-22",
        "risk": "Moderate",
        "benchmark_risk": "Low to Moderate",
        "exit_load": "Nil",
        "min_sip": 500,
        "min_lumpsum": 5000,
        "managers": ["Amit Somani (since 15 Oct 2025)", "Dhawal Joshi (since 15 Oct 2025)"],
        "prc_matrix": "B-I",
        "objective": "Generate regular income and capital appreciation via short-term debt/money market instruments with Macaulay duration 3-6 months.",
        "snapshot_nav": "16.19 as on 25-Sep-2026",
        "snapshot_aum_cr": "6116.80 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.26 (31-Aug-2026)",
        "snapshot_ytm_pct": "7.14",
        "snapshot_duration_months": "Macaulay 5.67M / Modified 5.44M / Avg Maturity 7.18M",
        "snapshot_returns": "Since 6.47% / 1Y 6.93% / 3Y 7.50% / 5Y 6.73% (as on 31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-ultra-short-term-fund-direct-growth",
            "https://api.mfapi.in/mf/146075 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "short_term": {
        "key": "short_term",
        "data_subdir": "tata-short-term-direct-growth",
        "file_prefix": "tata_short_term_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Short Term Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Short Term Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-short-term-fund-direct-growth",
        "amfi_scheme_code": "119949",
        "mfapi_scheme_code": 119949,
        "isin_growth": "INF277K01QR4",
        "amfi_fund_house_code": "25",
        "category": "Income/Debt Oriented Schemes - Short Term Fund",
        "benchmark": "CRISIL Short Duration Debt A-II Index",
        "additional_benchmark": "CRISIL 10 Year Gilt Index",
        "direct_inception": "2013-01-02",
        "regular_inception": "2002-08-08",
        "risk": "Moderate",
        "benchmark_risk": "Low to Moderate",
        "exit_load": "Nil (w.e.f 24 Jan 2019)",
        "min_sip": 500,
        "min_lumpsum": 5000,
        "managers": ["Amit Somani (since 11 Jun 2025)", "Dhawal Joshi (since 01 Jun 2026)"],
        "prc_matrix": "B-III",
        "objective": "Generate regular income/appreciation over short term via debt/money market instruments with Macaulay duration 1-3 years.",
        "snapshot_nav": "56.44 as on 25-Sep-2026",
        "snapshot_aum_cr": "2922.24 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.30 (31-Aug-2026)",
        "snapshot_ytm_pct": "7.51",
        "snapshot_duration_months": "Macaulay 2.27Y / Modified 2.15Y / Avg Maturity 2.73Y",
        "snapshot_returns": "Since 7.43% / 1Y 5.56% / 3Y 7.23% / 5Y 6.31% (as on 31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-short-term-fund-direct-growth",
            "https://api.mfapi.in/mf/119949 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "india_consumer": {
        "key": "india_consumer",
        "data_subdir": "tata-india-consumer-direct-growth",
        "file_prefix": "tata_india_consumer_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata India Consumer Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata India Consumer Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-india-consumer-fund-direct-growth",
        "amfi_scheme_code": "135805",
        "mfapi_scheme_code": 135805,
        "isin_growth": "INF277K013A6",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Sectoral/Thematic (Consumption)",
        "benchmark": "Nifty India Consumption TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2015-12-29",
        "regular_inception": "2015-12-28",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.25% if redeemed/switched before 30 days",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Sonam Udasi (since 01 Apr 2016)", "Aditya Bagul (since 03 Oct 2023)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Long-term capital appreciation by investing >=80% in consumption-oriented equity sectors in India.",
        "snapshot_nav": "54.98 as on 25-Sep-2026",
        "snapshot_aum_cr": "2810.85 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.62 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 17.63% / 1Y 9.47% / 3Y 16.36% / 5Y 15.26% (as on 31-Aug-2026)",
        "snapshot_holdings": "Eternal 9.36% / Titan 6.97% / Nestle 5.90% / Radico Khaitan 5.34% / CarTrade Tech 4.74% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-india-consumer-fund-direct-growth",
            "https://api.mfapi.in/mf/135805 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "large_mid": {
        "key": "large_mid",
        "data_subdir": "tata-large-and-mid-cap-direct-growth",
        "file_prefix": "tata_large_mid_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Large & Mid Cap Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Large & Mid Cap Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-large-and-mid-cap-fund-direct-growth",
        "amfi_scheme_code": "119202",
        "mfapi_scheme_code": 119202,
        "isin_growth": "INF277K01MK8",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Large & Mid Cap Fund",
        "benchmark": "Nifty Large Midcap 250 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2013-01-02",
        "regular_inception": "1993-02-25",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% <=30 days, Nil after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Chandraprakash Padiyar (since 03 Sep 2018)", "Kapil Malhotra (since 01 Jul 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Capital appreciation via large and midcap equities using bottom-up GARP stock picking (25-35 stocks).",
        "snapshot_nav": "561.82 as on 25-Sep-2026",
        "snapshot_aum_cr": "7470.58 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.61 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 14.60% / 1Y -1.59% / 3Y 8.22% / 5Y 10.08% (as on 31-Aug-2026)",
        "snapshot_holdings": "HDFC Bank 7.94% / SBI 5.80% / IDFC First Bank 5.40% / Godrej Properties 4.46% / ICICI Bank 4.35% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-large-and-mid-cap-fund-direct-growth",
            "https://api.mfapi.in/mf/119202 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "focused": {
        "key": "focused",
        "data_subdir": "tata-focused-direct-growth",
        "file_prefix": "tata_focused_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Focused Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Focused Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-focused-fund-direct-growth",
        "amfi_scheme_code": "147757",
        "mfapi_scheme_code": 147757,
        "isin_growth": "INF277K017X9",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Focused Fund",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2019-12-06",
        "regular_inception": "2019-12-05",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% on/before 30 days, NIL after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Anand Sharma (since 03 Oct 2025)", "Hasmukh Vishariya (since 01 Mar 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Concentrated max-30-stock GARP bottom-up portfolio across market caps/sectors for long-term wealth.",
        "snapshot_nav": "24.57 as on 25-Sep-2026",
        "snapshot_aum_cr": "1661.41 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.62 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 15.00% / 1Y 1.67% / 3Y 11.52% / 5Y 10.87% (as on 31-Aug-2026)",
        "snapshot_holdings": "ICICI Bank 7.72% / HDFC Bank 7.02% / Axis Bank 6.18% / UltraTech 5.04% / Reliance 4.90% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-focused-fund-direct-growth",
            "https://api.mfapi.in/mf/147757 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "flexi_cap": {
        "key": "flexi_cap",
        "data_subdir": "tata-flexi-cap-direct-growth",
        "file_prefix": "tata_flexi_cap_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Flexi Cap Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Flexi Cap Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-flexi-cap-fund-direct-growth",
        "amfi_scheme_code": "144546",
        "mfapi_scheme_code": 144546,
        "isin_growth": "INF277K015K0",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Flexi Cap Fund",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2018-09-10",
        "regular_inception": "2018-09-06",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% on/before 30 days, NIL after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Anand Sharma (since 03 Oct 2025)", "Aditya Bagul (since 03 Oct 2023)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Diversified go-anywhere large/mid/small-cap portfolio for medium-to-long-term capital growth.",
        "snapshot_nav": "25.78 as on 25-Sep-2026",
        "snapshot_aum_cr": "3520.58 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.58 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 13.04% / 1Y 0.25% / 3Y 11.28% / 5Y 9.98% (as on 31-Aug-2026)",
        "snapshot_holdings": "ICICI Bank 6.36% / HDFC Bank 5.83% / Reliance 4.29% / Axis Bank 3.87% / Bharti Airtel 3.69% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-flexi-cap-fund-direct-growth",
            "https://api.mfapi.in/mf/144546 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "value": {
        "key": "value",
        "data_subdir": "tata-value-direct-growth",
        "file_prefix": "tata_value_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Value Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Value Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-value-fund-direct-growth",
        "amfi_scheme_code": "119231",
        "mfapi_scheme_code": 119231,
        "isin_growth": "INF277K01ND1",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Value Fund",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2013-01-02",
        "regular_inception": "2004-06-29",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% on/before 30 days, NIL after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Sonam Udasi (since 01 Apr 2016)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Growth-oriented value strategy: good stocks at cheap valuations (min 70% with 12M trailing P/E below BSE Sensex).",
        "snapshot_nav": "393.47 as on 25-Sep-2026",
        "snapshot_aum_cr": "8370.04 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.68 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 16.44% / 1Y 8.05% / 3Y 14.88% / 5Y 14.56% (as on 31-Aug-2026)",
        "snapshot_holdings": "ICICI Bank 9.37% / Shriram Finance 6.46% / Motilal Oswal Fin 5.30% / Coal India 4.87% / Muthoot Finance 4.46% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-value-fund-direct-growth",
            "https://api.mfapi.in/mf/119231 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "banking_financial": {
        "key": "banking_financial",
        "data_subdir": "tata-banking-and-financial-services-direct-growth",
        "file_prefix": "tata_banking_financial_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Banking & Financial Services Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Banking & Financial Services Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-banking-and-financial-services-fund-direct-growth",
        "amfi_scheme_code": "135793",
        "mfapi_scheme_code": 135793,
        "isin_growth": "INF277K01Z10",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Sectoral/Thematic",
        "benchmark": "Nifty Financial Services TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2015-12-29",
        "regular_inception": "2015-12-28",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.25% if redeemed/switched before 30 days",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Kapil Malhotra (since 31 Oct 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Long-term appreciation by investing min 80% in Banking & Financial Services equity.",
        "snapshot_nav": "50.30 as on 25-Sep-2026",
        "snapshot_aum_cr": "3095.45 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.49 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 17.01% / 1Y 11.20% / 3Y 13.79% / 5Y 13.10% (as on 31-Aug-2026)",
        "snapshot_holdings": "ICICI Bank 10.57% / HDFC Bank 9.15% / SBI 8.81% / Axis Bank 8.21% / Kotak 5.22% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-banking-and-financial-services-fund-direct-growth",
            "https://api.mfapi.in/mf/135793 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "resources_energy": {
        "key": "resources_energy",
        "data_subdir": "tata-resources-and-energy-direct-growth",
        "file_prefix": "tata_resources_energy_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Resources & Energy Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Resources & Energy Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-resources-and-energy-fund-direct-growth",
        "amfi_scheme_code": "135813",
        "mfapi_scheme_code": 135813,
        "isin_growth": "INF277K015B9",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Sectoral/Thematic",
        "benchmark": "Nifty Commodities TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2015-12-29",
        "regular_inception": "2015-12-28",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.25% if redeemed/switched before 30 days",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Satish Chandra Mishra (since 09 Nov 2018)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Long-term appreciation by investing min 80% in Resources & Energy equity.",
        "snapshot_nav": "58.00 as on 25-Sep-2026",
        "snapshot_aum_cr": "1419.02 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.58 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 17.88% / 1Y 11.93% / 3Y 16.92% / 5Y 13.24% (as on 31-Aug-2026)",
        "snapshot_holdings": "UltraTech 5.69% / Vedanta Aluminium 5.25% / Adani Energy 4.91% / Tata Steel 4.32% / Ambuja 3.99% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-resources-and-energy-fund-direct-growth",
            "https://api.mfapi.in/mf/135813 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "dividend_yield": {
        "key": "dividend_yield",
        "data_subdir": "tata-dividend-yield-direct-growth",
        "file_prefix": "tata_dividend_yield_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Dividend Yield Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Dividend Yield Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-dividend-yield-fund-direct-growth",
        "amfi_scheme_code": "148947",
        "mfapi_scheme_code": 148947,
        "isin_growth": "INF277K015Z8",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Dividend Yield Fund",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2021-05-27",
        "regular_inception": "2021-05-20",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% on/before 30 days, NIL after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Sailesh Jain (since 03 May 2021)", "Murthy Nagarajan (since 03 May 2021)", "Hasmukh Vishariya (since 01 Mar 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Capital appreciation and/or dividend distribution via dividend-yielding equities (paid dividend/buyback in 1 of last 3 FYs).",
        "snapshot_nav": "21.29 as on 25-Sep-2026",
        "snapshot_aum_cr": "1126.27 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.56 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 16.04% / 1Y 17.15% / 3Y 16.33% / 5Y 14.82% (as on 31-Aug-2026)",
        "snapshot_holdings": "ICICI Bank 3.21% / L&T 2.89% / Radico Khaitan 2.86% / HDFC Bank 2.61% / PNB Housing 2.61% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-dividend-yield-fund-direct-growth",
            "https://api.mfapi.in/mf/148947 (NAV history JSON; old 119234 ended 01-Jun-2018 ignored)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "retirement_progressive": {
        "key": "retirement_progressive",
        "data_subdir": "tata-retirement-savings-progressive-direct-growth",
        "file_prefix": "tata_retirement_progressive_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Retirement Savings Fund-Progressive Plan - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Retirement Savings Progressive Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-retirement-savings-fund-progressive-plan-direct-growth",
        "amfi_scheme_code": "119251",
        "mfapi_scheme_code": 119251,
        "isin_growth": "INF277K01QO1",
        "amfi_fund_house_code": "25",
        "category": "Solution Oriented Scheme - Retirement Fund",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2013-01-04",
        "regular_inception": "2011-11-01",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "Nil if >=60yrs or auto-switch trigger; else 1% if redeemed before 61 months (w.e.f 03-May-2019)",
        "min_sip": 150,
        "min_lumpsum": 5000,
        "managers": ["Sonam Udasi (since 01 Apr 2016)", "Murthy Nagarajan (since 01 Apr 2017)"],
        "prc_matrix": "n/a (equity-oriented solution)",
        "objective": "Long-term retirement planning via equity-oriented portfolio (85-100% equity), no assured returns.",
        "snapshot_nav": "83.06 as on 25-Sep-2026",
        "snapshot_aum_cr": "2161.06 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.54 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity-oriented)",
        "snapshot_duration_months": "n/a (equity-oriented)",
        "snapshot_returns": "Since 15.39% / 1Y 12.21% / 3Y 14.77% / 5Y 11.85% (as on 31-Aug-2026)",
        "snapshot_holdings": "Solar Industries 5.38% / ICICI Bank 5.11% / Eternal 3.93% / Reliance 3.67% / MCX 3.66% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-retirement-savings-fund-progressive-plan-direct-growth",
            "https://api.mfapi.in/mf/119251 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "business_cycle": {
        "key": "business_cycle",
        "data_subdir": "tata-business-cycle-direct-growth",
        "file_prefix": "tata_business_cycle_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Business Cycle Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Business Cycle Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-business-cycle-fund-direct-growth",
        "amfi_scheme_code": "149071",
        "mfapi_scheme_code": 149071,
        "isin_growth": "INF277KA1166",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Sectoral/Thematic (Business Cycle)",
        "benchmark": "Nifty 500 TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2021-08-11",
        "regular_inception": "2021-08-04",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "0.50% on/before 30 days, NIL after",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Rahul Singh (since 16 Jul 2021)", "Murthy Nagarajan (since 16 Jul 2021)", "Sailesh Jain (since 16 Dec 2021)", "Hasmukh Vishariya (since 01 Mar 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Long-term capital appreciation by riding business cycles via dynamic sector/stock allocation.",
        "snapshot_nav": "20.13 as on 25-Sep-2026",
        "snapshot_aum_cr": "2485.37 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.60 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 15.56% / 1Y 6.64% / 3Y 13.75% / 5Y 15.40% (as on 31-Aug-2026)",
        "snapshot_holdings": "Reliance 4.79% / Metropolis Healthcare 4.57% / Axis Bank 3.89% / Jindal Steel 3.82% / HDFC Bank 3.47% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-business-cycle-fund-direct-growth",
            "https://api.mfapi.in/mf/149071 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "housing": {
        "key": "housing",
        "data_subdir": "tata-housing-opportunities-direct-growth",
        "file_prefix": "tata_housing_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "TATA HOUSING OPPORTUNITIES FUND - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Housing Opportunities Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-housing-opportunities-fund-direct-growth",
        "amfi_scheme_code": "150532",
        "mfapi_scheme_code": 150532,
        "isin_growth": "INF277KA1497",
        "amfi_fund_house_code": "25",
        "category": "Equity Scheme - Sectoral/Thematic (Housing)",
        "benchmark": "NIFTY Housing Index TRI",
        "additional_benchmark": "Nifty 50 TRI",
        "direct_inception": "2022-09-09",
        "regular_inception": "2022-09-02",
        "risk": "Very High",
        "benchmark_risk": "Very High",
        "exit_load": "365d: <=12% cost NIL, >12% 1%; >365d NIL",
        "min_sip": 100,
        "min_lumpsum": 5000,
        "managers": ["Murthy Nagarajan (since 16 Aug 2022)", "Hasmukh Vishariya (since 01 Mar 2025)", "Kapil Malhotra (since 01 Jul 2025)", "Aditya Bagul (since 01 Jul 2025)"],
        "prc_matrix": "n/a (equity)",
        "objective": "Long-term wealth from housing theme: allied materials/products/services benefiting from real-estate cycle.",
        "snapshot_nav": "15.52 as on 25-Sep-2026",
        "snapshot_aum_cr": "458.45 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.75 (31-Aug-2026)",
        "snapshot_ytm_pct": "n/a (equity)",
        "snapshot_duration_months": "n/a (equity)",
        "snapshot_returns": "Since 12.32% / 1Y 1.96% / 3Y 8.64% / 5Y NA (as on 31-Aug-2026)",
        "snapshot_holdings": "HDFC Bank 8.85% / UltraTech 8.32% / ICICI Bank 7.29% / L&T 5.65% / Prestige Estates 5.29% (31-Aug-2026)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-housing-opportunities-fund-direct-growth",
            "https://api.mfapi.in/mf/150532 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25",
        ],
    },
    "gilt": {
        "key": "gilt",
        "data_subdir": "tata-gilt-direct-growth",
        "file_prefix": "tata_gilt_direct_growth",
        "fund_house": "Tata Mutual Fund",
        "scheme_name_full": "Tata Gilt Fund - Direct Plan - Growth Option",
        "scheme_name_short": "Tata Gilt Fund Direct Growth",
        "tata_page_url": "https://www.tatamutualfund.com/mutual-funds/tata-gilt-fund-direct-growth",
        "amfi_scheme_code": "119953",
        "mfapi_scheme_code": 119953,
        "isin_growth": "INF277K01OX7",
        "amfi_fund_house_code": "25",
        "category": "Debt Scheme - Gilt Fund",
        "benchmark": "CRISIL Dynamic Gilt Index (AIII)",
        "additional_benchmark": "CRISIL 10 Year Gilt Index",
        "direct_inception": "2013-01-07",
        "regular_inception": "1999-09-06",
        "risk": "Moderate",
        "benchmark_risk": "Moderate",
        "exit_load": "Nil (w.e.f. 2 Nov 2018)",
        "min_sip": 500,
        "min_lumpsum": 5000,
        "managers": ["Akhil Mittal (since 01 Mar 2022)", "Puja Kasat (since 01 Jun 2026)"],
        "prc_matrix": "A-III (Relatively High Interest-Rate Risk, Relatively Low Credit Risk)",
        "objective": "Generate medium to long term capital appreciation and income by investing predominantly in Government Securities.",
        "snapshot_nav": "90.26 as on 25-Sep-2026 (Tata site); 90.2578 mfapi/AMFI",
        "snapshot_aum_cr": "775.90 (24-Sep-2026 Tata site)",
        "snapshot_expense_pct": "0.24 (31-Aug-2026)",
        "snapshot_ytm_pct": "7.44",
        "snapshot_duration_months": "Macaulay 10.52Y / Modified 10.14Y / Avg Maturity 24.84Y; StdDev 4.43, Beta 1.23, R2 0.91",
        "snapshot_returns": "Since-inception 7.80% / 1Y 5.40% / 3Y 6.69% / 5Y 6.09% (as on 31-Aug-2026, Tata site)",
        "sources": [
            "https://www.tatamutualfund.com/mutual-funds/tata-gilt-fund-direct-growth",
            "https://api.mfapi.in/mf/119953 (NAV history JSON)",
            "https://www.amfiindia.com/spages/NAVAll.txt (official daily NAV)",
            "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx?mf=25 (official history, 90-day max)",
        ],
    },
}

# Default (backward compat): money market
FUND = FUNDS["money_market"]

def get_fund(key):
    k = (key or "money_market").lower().strip().replace("-", "_").replace(" ", "_")
    # direct key match
    if k in FUNDS:
        return FUNDS[k]
    # friendly aliases
    aliases = {
        "gilt": "gilt", "tata_gilt": "gilt",
        "ultra_short": "ultra_short", "ultrashort": "ultra_short", "ultra": "ultra_short",
        "short_term": "short_term", "short": "short_term",
        "india_consumer": "india_consumer", "consumer": "india_consumer",
        "large_mid": "large_mid", "large_and_mid": "large_mid", "large_and_mid_cap": "large_mid",
        "focused": "focused", "focus": "focused",
        "flexi_cap": "flexi_cap", "flexi": "flexi_cap", "flexicap": "flexi_cap",
        "value": "value",
        "banking_financial": "banking_financial", "banking": "banking_financial", "banking_and_financial": "banking_financial",
        "resources_energy": "resources_energy", "resources": "resources_energy", "energy": "resources_energy",
        "dividend_yield": "dividend_yield", "dividend": "dividend_yield",
        "retirement_progressive": "retirement_progressive", "retirement": "retirement_progressive", "progressive": "retirement_progressive",
        "business_cycle": "business_cycle", "business": "business_cycle",
        "housing": "housing", "housing_opportunities": "housing",
        "money_market": "money_market", "money": "money_market",
    }
    if k in aliases:
        return FUNDS[aliases[k]]
    # numeric scheme code match
    for fk, f in FUNDS.items():
        if k == str(f["amfi_scheme_code"]) or k == str(f["mfapi_scheme_code"]):
            return f
    return FUNDS["money_market"]

def all_fund_keys():
    return sorted(FUNDS.keys())

def mfapi_url_for(fund):
    return f"https://api.mfapi.in/mf/{fund['mfapi_scheme_code']}"
AMFI_PORTAL_URL = "https://portal.amfiindia.com/DownloadNAVHistoryReport_Po.aspx"
AMFI_NAVALL_URL = "https://www.amfiindia.com/spages/NAVAll.txt"
HEADERS = {"User-Agent": "Mozilla/5.0 (X11; Linux x86_64) TataResearch/1.0"}

DATE_FMTS = ("%d-%b-%Y", "%d-%m-%Y", "%Y-%m-%d", "%d/%m/%Y")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------
def parse_date_any(s):
    s = (s or "").strip()
    for f in DATE_FMTS:
        try:
            return datetime.strptime(s, f).date()
        except ValueError:
            continue
    # try with timestamp suffix e.g. "01-Sep-2022 20:11:27" or extra spaces
    m = re.search(r"(\d{1,2}-[A-Za-z]{3}-\d{4})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d-%b-%Y").date()
        except ValueError:
            pass
    m = re.search(r"(\d{2}-\d{2}-\d{4})", s)
    if m:
        try:
            return datetime.strptime(m.group(1), "%d-%m-%Y").date()
        except ValueError:
            pass
    return None


def to_float(s):
    try:
        return float(str(s).strip().replace(",", ""))
    except (ValueError, AttributeError):
        return None


def is_target_row(code, name_joined, isin_joined, fund=None):
    """True only for the selected fund's Direct-Plan Growth (code/ISIN match)."""
    fund = fund or FUND
    code = (code or "").strip()
    blob = f"{name_joined} {isin_joined}".lower()
    if code == fund["amfi_scheme_code"]:
        return True
    if fund["isin_growth"].lower() in blob:
        return True
    # generic fallback: derive distinctive words from the fund's own full name
    # e.g. "Tata Ultra Short Term Fund - Direct Plan - Growth Option"
    #   -> require ultra+short+direct+growth, forbid idcw/dividend variants
    name_words = [w for w in re.findall(r"[a-z]+", fund["scheme_name_full"].lower())
                  if w not in ("tata", "fund", "plan", "option", "direct", "growth")]
    has_all = all(w in blob for w in (["tata", "direct", "growth"] + name_words))
    has_bad = any(w in blob for w in ["idcw", "dividend", "reinvest", "payout", "daily", "weekly", "monthly", "periodic", "bonus"])
    # extra exclusions for confusable families
    fam_bad = {
        "gilt": ["mid", "short maturity", "retirement"],
        "ultra_short": ["short term fund -"],
        "short_term": ["ultra short"],
        "value": ["series"],
        "banking_financial": ["psu", "debt"],
        "dividend_yield": [],
    }.get(fund["key"], [])
    if any(w in blob for w in fam_bad):
        return False
    return has_all and not has_bad


def parse_amfi_line(line, fund=None):
    """
    Robustly parse one AMFI ';'-delimited line in EITHER format:
      NEW NAVAll : code;isinG;isinD;base;plan;option;nav;date        (8 cols)
      OLD history: code;name;isinG;isinD;nav;repo;sale;date          (8 cols, nav@4)
      PORTAL Tata: code;name;plan;option;?;isin;nav;date-ish         (seen live)
    Returns dict or None.
    Strategy: code = col0 if numeric; date = first field matching a date;
              nav = float field right before date (or largest plausible float);
              isin = first INF... field; name = join of non-numeric non-date text fields.
    """
    if not line or ";" not in line:
        return None
    if line.strip().startswith("Scheme Code"):
        return None
    parts = [p.strip() for p in line.split(";")]
    if len(parts) < 5:
        return None
    code = parts[0]
    if not re.fullmatch(r"\d{4,7}", code):
        return None  # header/category lines like "Open Ended Schemes..."

    # find date: scan from right
    date_obj = None
    date_idx = -1
    for i in range(len(parts) - 1, -1, -1):
        d = parse_date_any(parts[i])
        if d:
            date_obj = d
            date_idx = i
            break
    if date_obj is None:
        return None

    # find NAV: float field nearest-left of date that looks like NAV (>5, <200000)
    nav = None
    for i in list(range(date_idx - 1, -1, -1)) + list(range(date_idx + 1, len(parts))):
        if 0 <= i < len(parts):
            f = to_float(parts[i])
            if f is not None and 1 < f < 200000 and parts[i] not in ("", "-"):
                # avoid picking the scheme code itself (integer, leftmost)
                if i == 0:
                    continue
                nav = f
                break
    if nav is None:
        return None

    isins = [p for p in parts if p.upper().startswith("INF")]
    fund = fund or FUND
    isin_g = next((x for x in isins if x.upper() == fund["isin_growth"].upper()), (isins[0] if isins else ""))
    name_bits = [p for idx, p in enumerate(parts)
                 if idx != 0 and idx != date_idx and p not in ("", "-")
                 and to_float(p) is None or (p and not p.upper().startswith("INF") and parse_date_any(p) is None and to_float(p) is None)]
    # simpler: join text-ish fields
    name_bits = []
    for idx, p in enumerate(parts):
        if idx == 0 or idx == date_idx:
            continue
        if p in ("", "-"):
            continue
        if p.upper().startswith("INF"):
            continue
        if to_float(p) is not None:
            continue
        if parse_date_any(p) is not None:
            continue
        name_bits.append(p)
    full_name = " ".join(name_bits).strip(" -")
    if not is_target_row(code, full_name, " ".join(isins), fund=fund):
        return None
    return {
        "date": date_obj,
        "nav": nav,
        "code": code,
        "name": (fund or FUND)["scheme_name_full"],
        "raw_name": full_name,
        "isin": isin_g or (fund or FUND)["isin_growth"],
    }


# ---------------------------------------------------------------------------
# Fetchers
# ---------------------------------------------------------------------------
def fetch_mfapi(progress_cb=None, fund=None):
    """Primary source: mfapi.in full history JSON. Returns list of dicts."""
    fund = fund or FUND
    r = requests.get(mfapi_url_for(fund), timeout=30, headers=HEADERS)
    r.raise_for_status()
    j = r.json()
    rows = []
    for d in j.get("data", []):
        dt = parse_date_any(d.get("date", ""))
        nav = to_float(d.get("nav", ""))
        if dt and nav:
            rows.append({
                "date": dt,
                "nav": nav,
                "code": str(j.get("meta", {}).get("scheme_code", fund["amfi_scheme_code"])),
                "name": j.get("meta", {}).get("scheme_name", fund["scheme_name_full"]),
                "isin": j.get("meta", {}).get("isin_growth", fund["isin_growth"]) or fund["isin_growth"],
            })
        if progress_cb and len(rows) % 500 == 0:
            progress_cb(len(rows), None)
    return rows


def fetch_amfi_portal_chunk(fr_dt, to_dt, retries=3, fund=None):
    """One 90-day portal download for Tata (mf=25). Returns raw text."""
    fund = fund or FUND
    params = {
        "mf": fund["amfi_fund_house_code"],
        "frmdt": fr_dt.strftime("%d-%b-%Y"),
        "todt": to_dt.strftime("%d-%b-%Y"),
    }
    last_err = None
    for attempt in range(retries):
        try:
            r = requests.get(AMFI_PORTAL_URL, params=params, timeout=30, headers=HEADERS)
            r.raise_for_status()
            if "Scheme Code" in r.text or fund["scheme_name_short"].split()[0] in r.text or ";" in r.text:
                return r.text
            last_err = f"unexpected content ({len(r.text)} chars, no Scheme Code)"
        except Exception as e:
            last_err = str(e)
        time.sleep(1.5 * (attempt + 1))
    raise RuntimeError(f"AMFI portal failed {params}: {last_err}")


def fetch_amfi_history_range(start, end, progress_cb=None, fund=None):
    """Fallback: loop 90-day blocks over mf=25 portal, robust-parse, filter target code."""
    fund = fund or FUND
    rows = []
    cursor = start
    block = timedelta(days=90)
    chunks = []
    while cursor <= end:
        nxt = min(cursor + block, end)
        chunks.append((cursor, nxt))
        cursor = nxt + timedelta(days=1)
    for i, (a, b) in enumerate(chunks):
        if progress_cb:
            progress_cb(i, len(chunks), f"AMFI {a:%d-%b-%Y} -> {b:%d-%b-%Y}")
        try:
            txt = fetch_amfi_portal_chunk(a, b, fund=fund)
        except Exception as e:
            print(f"  [warn] chunk {a}..{b} failed: {e}")
            time.sleep(0.5)
            continue
        for line in txt.splitlines():
            rec = parse_amfi_line(line, fund=fund)
            if rec and start.date() <= rec["date"] <= end.date():
                rows.append(rec)
        time.sleep(0.4)  # be nice to shared AMFI portal
    return rows


def fetch_navall_validate(fund=None):
    """Light validation: today's NAVAll row for target code."""
    fund = fund or FUND
    try:
        r = requests.get(AMFI_NAVALL_URL, timeout=25, headers=HEADERS)
        r.raise_for_status()
        for line in r.text.splitlines():
            rec = parse_amfi_line(line, fund=fund)
            if rec:
                return rec  # first target match = today's row
    except Exception as e:
        print(f"  [warn] NAVAll check failed: {e}")
    return None


# ---------------------------------------------------------------------------
# Stats / outputs
# ---------------------------------------------------------------------------
def dedup_sort(rows):
    best = OrderedDict()
    for r in rows:
        k = r["date"].isoformat()
        # keep last (mfapi is newest-first; portal oldest-first - either way keep max nav precision)
        best[k] = r
    return sorted(best.values(), key=lambda x: x["date"])


def compute_stats(rows):
    if not rows:
        return {}
    navs = [r["nav"] for r in rows]
    first, last = rows[0], rows[-1]
    days = (last["date"] - first["date"]).days or 1
    years = days / 365.25
    cagr = (last["nav"] / first["nav"]) ** (1 / years) - 1 if first["nav"] > 0 and years > 0 else 0
    # year-end table
    by_year = OrderedDict()
    for r in rows:
        by_year.setdefault(r["date"].year, []).append(r)
    yearly = []
    prev_close = None
    for y in sorted(by_year):
        close = by_year[y][-1]
        ret = (close["nav"] / prev_close["nav"] - 1) if prev_close else None
        yearly.append({"year": y, "date": close["date"].isoformat(),
                       "year_end_nav": round(close["nav"], 4),
                       "annual_return_pct": round(ret * 100, 2) if ret is not None else None,
                       "observations": len(by_year[y])})
        prev_close = close
    # trailing returns vs latest
    def trailing(years_back):
        target = last["date"] - timedelta(days=int(years_back * 365.25))
        past = min(rows, key=lambda r: abs((r["date"] - target).days))
        y = (last["date"] - past["date"]).days / 365.25
        return round(((last["nav"] / past["nav"]) ** (1 / y) - 1) * 100, 2) if y > 0.2 else None
    return {
        "records": len(rows),
        "from": first["date"].isoformat(),
        "to": last["date"].isoformat(),
        "first_nav": first["nav"],
        "last_nav": last["nav"],
        "min_nav": min(navs),
        "max_nav": max(navs),
        "cagr_since_direct_inception_pct": round(cagr * 100, 2),
        "trailing_1Y_pct": trailing(1),
        "trailing_3Y_pct": trailing(3),
        "trailing_5Y_pct": trailing(5),
        "trailing_10Y_pct": trailing(10),
        "yearly": yearly,
    }


def default_out_dir_for(fund_key="money_market"):
    base = os.path.dirname(os.path.abspath(__file__))
    fund = get_fund(fund_key)
    return os.path.join(base, "data", fund.get("data_subdir", fund["key"]))


def save_outputs(rows, out_dir, fund=None):
    fund = fund or FUND
    os.makedirs(out_dir, exist_ok=True)
    stats = compute_stats(rows)

    # Super-organized clean names inside data/<fund>/ subfolder
    csv_path = os.path.join(out_dir, "nav_daily.csv")
    yearly_path = os.path.join(out_dir, "yearly_returns.csv")
    with open(csv_path, "w", newline="", encoding="utf-8") as f:
        w = csv.writer(f)
        w.writerow(["Date", "NAV_Rs", "Scheme_Code", "Scheme_Name", "ISIN_Growth",
                    "Fund_House", "Category", "Benchmark", "Source"])
        for r in rows:
            w.writerow([r["date"].isoformat(), f"{r['nav']:.4f}", fund["amfi_scheme_code"],
                        fund["scheme_name_full"], fund["isin_growth"],
                        fund["fund_house"], fund["category"], fund["benchmark"],
                        r.get("source", "mfapi")])

    with open(yearly_path, "w", newline="", encoding="utf-8") as f:
        w = csv.DictWriter(f, fieldnames=["year", "date", "year_end_nav", "annual_return_pct", "observations"])
        w.writeheader()
        for y in stats.get("yearly", []):
            w.writerow(y)

    meta = dict(fund)
    meta["download"] = {
        "downloaded_at": datetime.now().isoformat(timespec="seconds"),
        "records": stats.get("records"),
        "date_range": [stats.get("from"), stats.get("to")],
        "files": [os.path.basename(csv_path), os.path.basename(yearly_path)],
        "primary_source": mfapi_url_for(fund),
        "fallbacks": [AMFI_PORTAL_URL + "?mf=25 (90-day blocks)", AMFI_NAVALL_URL],
        "stats": stats,
    }
    meta_path = os.path.join(out_dir, "metadata.json")
    with open(meta_path, "w", encoding="utf-8") as f:
        json.dump(meta, f, indent=2, ensure_ascii=False)

    summary_path = os.path.join(out_dir, "summary.txt")
    with open(summary_path, "w", encoding="utf-8") as f:
        f.write(f"{fund['scheme_name_full'].upper()} - DATASET\n")
        f.write("=" * 62 + "\n")
        f.write(f"Scheme      : {fund['scheme_name_full']}\n")
        f.write(f"Code / ISIN : {fund['amfi_scheme_code']} / {fund['isin_growth']}\n")
        f.write(f"Page        : {fund['tata_page_url']}\n")
        f.write(f"Category    : {fund['category']}\n")
        f.write(f"Benchmark   : {fund['benchmark']} (+ {fund['additional_benchmark']})\n")
        f.write(f"Direct since: {fund['direct_inception']} | Regular since: {fund['regular_inception']}\n")
        f.write(f"Managers    : {', '.join(fund['managers'])}\n")
        f.write(f"Risk/Exit   : {fund['risk']} / {fund['exit_load']} | SIP {fund['min_sip']} / Lumpsum {fund['min_lumpsum']}\n")
        f.write(f"AUM/Expense : {fund['snapshot_aum_cr']} Cr / {fund['snapshot_expense_pct']}%\n")
        f.write(f"YTM/Duration: {fund['snapshot_ytm_pct']}% / {fund['snapshot_duration_months']}\n")
        f.write("\nDOWNLOAD\n")
        f.write(f"  Records : {stats.get('records')}  ({stats.get('from')} -> {stats.get('to')})\n")
        f.write(f"  First NAV: {stats.get('first_nav')} | Last NAV: {stats.get('last_nav')}\n")
        f.write(f"  Min/Max  : {stats.get('min_nav')} / {stats.get('max_nav')}\n")
        f.write(f"  CAGR since Direct inception: {stats.get('cagr_since_direct_inception_pct')}%\n")
        f.write(f"  Trailing 1Y/3Y/5Y/10Y %    : {stats.get('trailing_1Y_pct')}/{stats.get('trailing_3Y_pct')}/{stats.get('trailing_5Y_pct')}/{stats.get('trailing_10Y_pct')}\n")
        f.write(f"  Published returns: {fund['snapshot_returns']}\n")
        f.write("\nFILES\n")
        f.write(f"  1. {os.path.basename(csv_path)} - daily NAV, ISO dates, sorted, deduped\n")
        f.write(f"  2. {os.path.basename(yearly_path)} - year-end NAV + annual %\n")
        f.write(f"  3. {os.path.basename(meta_path)} - full fund facts + sources + stats\n")
        f.write(f"  4. {os.path.basename(summary_path)} - this file\n")
        f.write("\nSOURCES\n")
        for s in fund["sources"]:
            f.write(f"  - {s}\n")
        if fund["key"] == "money_market":
            f.write("\nNOTE: Tata site 'Fund Age 23y since 22 May 2003' = Regular plan age. "
                    "Direct plan started 01-Jan-2013, so this file covers the FULL Direct-Growth "
                    "history (~13 years). NAV/returns move daily; snapshots above are dated.\n")
        else:
            f.write("\nNOTE: Tata site 'Fund Age 27y since 06 Sep 1999' = Regular plan age. "
                    "Direct plan started Jan-2013 (first NAV 07-Jan-2013), so this file covers the FULL Direct-Growth "
                    "history. NAV/returns move daily; snapshots above are dated.\n")
    return {"csv": csv_path, "yearly": yearly_path, "meta": meta_path,
            "summary": summary_path, "stats": stats}


def run_pipeline(out_dir=None, progress_cb=None, fund=None, fund_key="money_market"):
    """Primary mfapi -> fallback AMFI portal. Returns (rows, stats, paths)."""
    fund = fund or get_fund(fund_key)
    out_dir = out_dir or default_out_dir_for(fund["key"])
    rows = []
    # --- primary ---
    try:
        if progress_cb:
            progress_cb(0, 1, f"Contacting mfapi.in for {fund['amfi_scheme_code']} ({fund['scheme_name_short']})...")
        rows = fetch_mfapi(progress_cb=progress_cb, fund=fund)
        for r in rows:
            r["source"] = f"mfapi.in/{fund['amfi_scheme_code']}"
        print(f"  [ok] mfapi.in returned {len(rows)} rows for {fund['key']}")
    except Exception as e:
        print(f"  [warn] mfapi.in failed ({e}); falling back to AMFI portal mf=25...")
        rows = []

    # --- fallback: AMFI 90-day blocks from Direct inception to today ---
    if len(rows) < 100:
        try:
            start = datetime.strptime(fund["direct_inception"], "%Y-%m-%d")
        except ValueError:
            start = datetime(2013, 1, 1)
        end = datetime.now()
        print(f"  [info] AMFI portal fallback {start.date()} -> {end.date()} (90-day blocks)...")
        rows = fetch_amfi_history_range(start, end, progress_cb=progress_cb, fund=fund)
        for r in rows:
            r.setdefault("source", "amfi-portal-mf25")

    rows = dedup_sort(rows)
    if not rows:
        raise RuntimeError("No rows from mfapi.in NOR AMFI portal. Check internet / try again later.")
    # light cross-check with NAVAll
    latest_check = fetch_navall_validate(fund=fund)
    if latest_check:
        print(f"  [check] AMFI NAVAll today: {latest_check['date']} NAV {latest_check['nav']}")

    paths = save_outputs(rows, out_dir, fund=fund)
    print(f"  [ok] saved {paths['stats']['records']} rows "
          f"{paths['stats']['from']}->{paths['stats']['to']} to {out_dir}")
    return rows, paths["stats"], paths


# ---------------------------------------------------------------------------
# GUI (fixed backend, same premium look) + CLI
# ---------------------------------------------------------------------------
class AMFIDataFetcherPro:
    def __init__(self, root, fund_key="money_market"):
        self.root = root
        self.fund_key = fund_key
        self.root.title("TATA Funds - Harvester (FIXED)")
        self.root.geometry("620x420")
        self.root.resizable(False, False)

        self.bg_main = "#0f172a"
        self.bg_card = "#1e293b"
        self.fg_text = "#f8fafc"
        self.fg_muted = "#94a3b8"
        self.accent_sky = "#38bdf8"

        self.root.configure(bg=self.bg_main)
        self.build_user_interface()

    def build_user_interface(self):
        import tkinter as tk
        from tkinter import ttk
        tk.Label(self.root, text="TATA Fund Data Harvester (FIXED - 15 funds)",
                 font=("Helvetica", 14, "bold"), bg=self.bg_main, fg=self.accent_sky).pack(pady=(18, 4))
        tk.Label(self.root, text="15 Direct-Growth funds - full history via mfapi.in + AMFI mf=25",
                 font=("Helvetica", 8, "italic"), bg=self.bg_main, fg=self.fg_muted,
                 wraplength=540, justify="center").pack(pady=(0, 10))

        # fund selector
        sel_frame = tk.Frame(self.root, bg=self.bg_main)
        sel_frame.pack(pady=(0, 10))
        tk.Label(sel_frame, text="Fund:", bg=self.bg_main, fg=self.fg_muted,
                 font=("Helvetica", 10)).pack(side="left", padx=(0, 8))
        self.fund_var = tk.StringVar(value=self.fund_key)
        fund_menu = ttk.Combobox(sel_frame, textvariable=self.fund_var, state="readonly", width=28,
                                 values=["all", "money_market", "gilt", "ultra_short", "short_term",
                                         "india_consumer", "large_mid", "focused", "flexi_cap",
                                         "value", "banking_financial", "resources_energy",
                                         "dividend_yield", "retirement_progressive",
                                         "business_cycle", "housing"])
        fund_menu.pack(side="left")
        tk.Label(self.root, text="all = all 15 funds (money_market + gilt + 13 new)",
                 font=("Helvetica", 7), bg=self.bg_main, fg=self.fg_muted).pack(pady=(0, 6))

        self.container_box = tk.Frame(self.root, bg=self.bg_card, padx=20, pady=15, bd=0)
        self.container_box.pack(fill="x", padx=40)

        self.engine_status = tk.Label(self.container_box, text="Engine Status: Idle & Ready to Initialize",
                                      font=("Helvetica", 10), bg=self.bg_card, fg=self.fg_text, anchor="w")
        self.engine_status.pack(fill="x", pady=(0, 12))

        ui_style = ttk.Style()
        try:
            ui_style.theme_use("default")
        except Exception:
            pass
        ui_style.configure("Custom.Horizontal.TProgressbar", thickness=14, troughcolor=self.bg_main,
                           background=self.accent_sky, bordercolor=self.bg_main,
                           lightcolor=self.accent_sky, darkcolor=self.accent_sky)
        self.progress_bar = ttk.Progressbar(self.container_box, style="Custom.Horizontal.TProgressbar",
                                            orient="horizontal", mode="determinate")
        self.progress_bar.pack(fill="x", pady=5)

        self.action_btn = tk.Button(self.root, text="Initialize Pipeline",
                                    command=self.lock_and_execute, font=("Helvetica", 10, "bold"),
                                    bg=self.accent_sky, fg=self.bg_main, activebackground="#7dd3fc",
                                    activeforeground=self.bg_main, relief="flat", bd=0,
                                    padx=25, pady=10, cursor="hand2")
        self.action_btn.pack(pady=14)
        tk.Label(self.root, text="Saves CSV + YEARLY + METADATA.json + SUMMARY.txt next to this script.",
                 font=("Helvetica", 8), bg=self.bg_main, fg=self.fg_muted).pack()

    def lock_and_execute(self):
        self.action_btn.config(state="disabled", bg="#475569")
        self.root.after(50, self.run_historical_download_pipeline)

    def run_historical_download_pipeline(self):
        import tkinter as tk
        from tkinter import messagebox
        fund_key = self.fund_var.get().strip() if hasattr(self, "fund_var") else self.fund_key
        keys = all_fund_keys() if fund_key == "all" else [get_fund(fund_key)["key"]]

        def gui_progress(a, b=None, msg=None):
            if msg:
                self.engine_status.config(text=f"Pipeline: {msg}")
            elif b:
                self.engine_status.config(text=f"Pipeline Fetching block {a + 1}/{b}")
                self.progress_bar["maximum"] = b
                self.progress_bar["value"] = a
            self.root.update()

        try:
            all_msgs = []
            last_dir = ""
            for k in keys:
                fund = get_fund(k)
                self.engine_status.config(text=f"Pipeline: contacting mfapi.in for {fund['amfi_scheme_code']}...")
                self.root.update()
                _, stats, paths = run_pipeline(progress_cb=gui_progress, fund_key=k)
                last_dir = os.path.dirname(paths["csv"])
                all_msgs.append(f"{fund['scheme_name_short']}: {stats['records']} rows {stats['from']}->{stats['to']}")
            self.progress_bar["maximum"] = 100
            self.progress_bar["value"] = 100
            self.engine_status.config(text="Done: " + " | ".join(all_msgs))
            messagebox.showinfo("Pipeline Complete",
                                f"Success! Full Direct-Growth history harvested.\n\n"
                                + "\n".join(all_msgs) +
                                f"\n\nFiles in data/ subfolders under:\n{last_dir}")
        except Exception as e:
            self.engine_status.config(text="Status: failed - see console")
            messagebox.showerror("Pipeline Failed", f"{e}")
        finally:
            self.action_btn.config(state="normal", bg=self.accent_sky)


def cli_main(argv=None):
    ap = argparse.ArgumentParser(description="Tata Direct-Growth harvester: 15 funds (money_market+gilt+13)")
    ap.add_argument("--cli", action="store_true", help="run in terminal (no GUI)")
    ap.add_argument("--no-gui", action="store_true", help="same as --cli")
    ap.add_argument("--out-dir", default=None, help="output folder (default: data/<fund>/ subfolder)")
    ap.add_argument("--fund", default="all",
                    choices=["all", "money_market", "gilt", "ultra_short", "short_term",
                             "india_consumer", "large_mid", "focused", "flexi_cap",
                             "value", "banking_financial", "resources_energy",
                             "dividend_yield", "retirement_progressive",
                             "business_cycle", "housing"],
                    help="which fund to download (default: all 15)")
    args = ap.parse_args(argv)

    def cli_progress(a, b=None, msg=None):
        if msg:
            print(f"  ... {msg}")
        elif b:
            print(f"  ... block {a + 1}/{b}", end="\r", flush=True)

    keys = all_fund_keys() if args.fund == "all" else [get_fund(args.fund)["key"]]
    for k in keys:
        fund = get_fund(k)
        print(f"\n=== {fund['scheme_name_full']} ({fund['amfi_scheme_code']}) ===")
        out = args.out_dir or default_out_dir_for(k)
        _, stats, paths = run_pipeline(out_dir=out, progress_cb=cli_progress, fund_key=k)
        print("\nDONE")
        print(f"  Records : {stats['records']} ({stats['from']} -> {stats['to']})")
        print(f"  CAGR    : {stats['cagr_since_direct_inception_pct']}%")
        for kk in ("csv", "yearly", "meta", "summary"):
            print(f"  {kk:7s}: {paths[kk]}")
    return 0


if __name__ == "__main__":
    # auto-CLI when headless or --cli/--no-gui passed
    want_cli = "--cli" in sys.argv or "--no-gui" in sys.argv or not os.environ.get("DISPLAY")
    # also allow explicit GUI: `python fetch_data.py --gui`
    if "--gui" in sys.argv:
        want_cli = False
        sys.argv.remove("--gui")
    if want_cli:
        sys.exit(cli_main())
    else:
        import tkinter as tk
        ui_context_window = tk.Tk()
        runtime_app_instance = AMFIDataFetcherPro(ui_context_window)
        ui_context_window.mainloop()
