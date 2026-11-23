# Tata Mutual Fund Harvester (15 Direct-Growth funds)

Downloads full Direct-Growth NAV history for 15 Tata funds via mfapi.in (primary),
AMFI portal `mf=25` 90-day blocks (fallback), AMFI NAVAll.txt (validation),
plus Tata MF pages + factsheets (holdings/snapshots).

Funds (code / ISIN / records):
- money_market 119424 INF277K01PR6 3876 | gilt 119953 INF277K01OX7 3313
- ultra_short 146075 INF277K011T0 1859 | short_term 119949 INF277K01QR4 3316
- india_consumer 135805 INF277K013A6 2648 | large_mid 119202 INF277K01MK8 3380
- focused 147757 INF277K017X9 1680 | flexi_cap 144546 INF277K015K0 1980
- value 119231 INF277K01ND1 3380 | banking_financial 135793 INF277K01Z10 2648
- resources_energy 135813 INF277K015B9 2648 | dividend_yield 148947 INF277K015Z8 1316
- retirement_progressive 119251 INF277K01QO1 3377 | business_cycle 149071 INF277KA1166 1263
- housing 150532 INF277KA1497 996

## Use

```bash
pip install -r requirements.txt
python3 fetch_data.py --cli --fund all
python3 fetch_data.py --cli --fund focused
python3 fetch_data.py --cli --fund 147757
python3 fetch_data.py --gui
```

Outputs go to `data/<fund>/`: `nav_daily.csv`, `yearly_returns.csv`,
`metadata.json`, `summary.txt`, plus `holdings.csv` / `factsheet_snapshot.json`
(`peers.csv` where category peers exist). See `data/README.md`.
All latest NAVs verified 25-Sep-2026 vs mfapi + AMFI NAVAll.
