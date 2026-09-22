# Phase 1 backtest report

_Generated 2026-09-23 09:51._

## DATA LABEL: **PLUMBING TEST - NOT A GO/NO-GO**

Price provider: `yfinance`. Survivorship-safe: **False**. Nothing in this report is a go/no-go unless the provider is Norgate Platinum with delisted stocks included.

> **Warning:** Universe small: announcement archive is incomplete (0/1525 codes have an archive file (0%)). Strategy A results cover only archived codes; rerun when the archive finishes.

## Setup

- Benchmark: STW.AX adjusted close from 2008-01-02 (total-return proxy); ^AXJO PRICE index before that (understates total return)
- Universe `asx300`: 300 codes. Source: PROXY: top 300 by market cap in ASX directory today (survivorship-biased)
- Universe `small`: 1525 codes. Source: ASX directory minus asx300 (current listings only)
- Announcement archive coverage: asx300: 300/300 codes have an archive file (100%), 415,751 rows 2002-01-02 to 2026-09-22; small: 0/1525 codes have an archive file (0%)
- Out-of-sample holdout starts 2023-09-22 (last 3 years).
- **IN-SAMPLE ONLY: every run stops at 2023-09-22.** The holdout years were not simulated, so nothing in this report has seen them.
- Parameters (frozen 2026-09-22, before any data was fetched):
  - gap_pct_vs_index (X): 5.0
  - volume_multiple (Y): 3.0
  - entry: next_open (main); same_day_open (upper bound only)
  - hold_days: 10
  - stop_loss_pct: 8.0
  - turnover_floor_aud: 250000
  - baseline: 12-1 momentum, top 4, 200d regime
  - costs: 0.088% min $6.6; slippage {'base_pct': 0.1, 'impact_coeff': 0.5, 'cap_pct': 1.0}
  - priority when slots are full: highest volume multiple first
  - minimum order: $500 (ASX minimum parcel)

## Universe: asx300

### Slippage x1

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 492 | 155 | 44.3 | 0.05 | -1.09 | 10.0 | -0.5 | -43.7 | 11.5 | 20.3 | 8,866 | 9,014 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 412 | 142 | 51.9 | 2.37 | 0.71 | 10.0 | 10.9 | -25.6 | 13.8 | 17.1 | 15,988 | 85,744 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 646 | 170 | 40.1 | -0.87 | -2.09 | 10.0 | -7.6 | -85.2 | 13.9 | 25.7 | 10,571 | 1,941 |  |
| momentum baseline | 257 | 105 | 50.2 | 11.52 | 0.18 | 23.0 | 24.5 | -51.5 | 6.1 | 65.1 | 46,494 | 932,704 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 492 | 155 | 44.3 | 0.05 | -986 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 20 | 10.0 | -3.21 |
| agm | 9 | 55.6 | 0.59 |
| capital_raising | 29 | 34.5 | -1.30 |
| clinical | 7 | 57.1 | 4.53 |
| contract | 31 | 38.7 | 1.39 |
| dividend | 2 | 0.0 | -5.14 |
| exploration | 39 | 28.2 | 0.21 |
| guidance | 38 | 42.1 | -0.67 |
| investor_presentation | 1 | 100.0 | 6.93 |
| other | 111 | 45.0 | -1.03 |
| response_to_asx | 6 | 33.3 | -0.79 |
| results | 181 | 53.6 | 0.90 |
| trading_halt | 18 | 44.4 | 1.28 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 412 | 142 | 51.9 | 2.37 | 75,744 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 16 | 43.8 | -0.74 |
| agm | 7 | 71.4 | 7.35 |
| capital_raising | 27 | 44.4 | -2.02 |
| clinical | 5 | 40.0 | 5.08 |
| contract | 24 | 41.7 | 4.74 |
| dividend | 1 | 0.0 | -5.69 |
| exploration | 33 | 48.5 | 9.69 |
| guidance | 35 | 65.7 | 1.72 |
| investor_presentation | 1 | 100.0 | 9.26 |
| other | 83 | 39.8 | -0.78 |
| response_to_asx | 3 | 33.3 | -1.78 |
| results | 170 | 60.6 | 3.38 |
| trading_halt | 7 | 14.3 | -5.31 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 646 | 170 | 40.1 | -0.87 | -8,059 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.1 | 1.9 | -1.2 | 0.0 | 9.0 |
| 2004 | 17.5 | 39.1 | 15.0 | 40.7 | 22.8 |
| 2005 | 0.8 | 0.6 | 4.1 | 107.2 | 17.6 |
| 2006 | 8.4 | 6.8 | -7.2 | 188.5 | 19.0 |
| 2007 | -12.2 | -1.6 | -13.7 | 42.3 | 11.8 |
| 2008 | -9.0 | -6.1 | -8.9 | -14.7 | -43.3 |
| 2009 | -5.5 | 20.4 | -7.1 | 78.3 | 31.8 |
| 2010 | 7.6 | 10.1 | 8.3 | 9.4 | -2.2 |
| 2011 | -5.1 | -2.0 | -2.8 | 27.5 | -15.3 |
| 2012 | -11.0 | 7.1 | -14.2 | 30.2 | 14.8 |
| 2013 | 11.6 | 1.3 | 6.4 | 69.1 | 19.6 |
| 2014 | -8.4 | 3.6 | -15.7 | -21.0 | 5.1 |
| 2015 | 2.4 | 32.3 | -6.8 | 13.5 | 2.5 |
| 2016 | -6.5 | 16.6 | -15.2 | -12.7 | 11.6 |
| 2017 | -9.2 | -4.7 | -20.1 | 28.7 | 11.5 |
| 2018 | -0.6 | 10.7 | 0.2 | -11.3 | -2.9 |
| 2019 | 27.2 | 55.0 | 23.2 | 11.6 | 23.2 |
| 2020 | 5.3 | 14.0 | -24.3 | 1.7 | 1.9 |
| 2021 | 13.1 | -3.7 | -6.9 | 165.6 | 16.7 |
| 2022 | -10.2 | 23.1 | -45.6 | -28.1 | -1.3 |
| 2023 | -15.0 | 22.7 | 0.0 | -13.8 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

### Slippage x2

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 492 | 155 | 42.3 | -0.33 | -1.54 | 10.0 | -2.7 | -55.9 | 10.7 | 20.2 | 10,418 | 5,693 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 412 | 142 | 51.5 | 2.15 | 0.50 | 10.0 | 9.8 | -26.7 | 13.3 | 17.0 | 21,116 | 68,968 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 517 | 150 | 40.2 | -1.11 | -2.07 | 10.0 | -7.6 | -84.6 | 12.0 | 20.9 | 9,871 | 1,965 |  |
| momentum baseline | 257 | 105 | 49.0 | 11.21 | -0.22 | 23.0 | 23.5 | -52.5 | 6.1 | 65.1 | 41,702 | 794,150 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 492 | 155 | 42.3 | -0.33 | -4,307 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 20 | 10.0 | -3.87 |
| agm | 9 | 44.4 | -0.73 |
| capital_raising | 29 | 34.5 | -1.62 |
| clinical | 7 | 42.9 | 4.11 |
| contract | 31 | 38.7 | 1.15 |
| dividend | 2 | 0.0 | -5.39 |
| exploration | 39 | 28.2 | -0.11 |
| guidance | 39 | 41.0 | -0.90 |
| investor_presentation | 1 | 100.0 | 6.51 |
| other | 111 | 43.2 | -1.31 |
| response_to_asx | 6 | 33.3 | -1.04 |
| results | 180 | 50.6 | 0.46 |
| trading_halt | 18 | 44.4 | 0.98 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 412 | 142 | 51.5 | 2.15 | 58,968 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 16 | 43.8 | -0.95 |
| agm | 7 | 71.4 | 7.13 |
| capital_raising | 27 | 40.7 | -2.18 |
| clinical | 5 | 40.0 | 4.87 |
| contract | 24 | 41.7 | 4.52 |
| dividend | 1 | 0.0 | -5.90 |
| exploration | 33 | 45.5 | 9.48 |
| guidance | 35 | 65.7 | 1.51 |
| investor_presentation | 1 | 100.0 | 9.04 |
| other | 83 | 39.8 | -0.92 |
| response_to_asx | 3 | 33.3 | -1.99 |
| results | 170 | 60.6 | 3.11 |
| trading_halt | 7 | 14.3 | -5.51 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 517 | 150 | 40.2 | -1.11 | -8,035 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 1.8 | -1.5 | 0.0 | 9.0 |
| 2004 | 14.7 | 38.6 | 12.2 | 39.7 | 22.8 |
| 2005 | 0.5 | 0.4 | 3.5 | 106.4 | 17.6 |
| 2006 | 7.9 | 6.4 | -8.1 | 187.1 | 19.0 |
| 2007 | -12.9 | -2.2 | -16.0 | 41.3 | 11.8 |
| 2008 | -9.6 | -6.4 | -10.3 | -14.9 | -43.3 |
| 2009 | -6.7 | 19.4 | -9.3 | 77.6 | 31.8 |
| 2010 | 6.4 | 9.2 | 6.6 | 8.7 | -2.2 |
| 2011 | -5.7 | -2.5 | -4.3 | 26.5 | -15.3 |
| 2012 | -12.3 | 6.2 | -16.2 | 28.8 | 14.8 |
| 2013 | 10.6 | 0.7 | 4.2 | 67.8 | 19.6 |
| 2014 | -9.3 | 3.1 | -17.1 | -22.1 | 5.1 |
| 2015 | 0.1 | 30.2 | -10.5 | 12.2 | 2.5 |
| 2016 | -8.6 | 14.9 | -19.7 | -13.4 | 11.6 |
| 2017 | -10.9 | -5.0 | -24.0 | 27.0 | 11.5 |
| 2018 | -2.1 | 9.4 | -6.5 | -12.3 | -2.9 |
| 2019 | 23.4 | 48.9 | 9.8 | 10.9 | 23.2 |
| 2020 | -2.2 | 12.2 | -34.1 | 1.2 | 1.9 |
| 2021 | 7.3 | -5.6 | 0.0 | 163.0 | 16.7 |
| 2022 | -14.9 | 20.2 | 0.0 | -28.7 | -1.3 |
| 2023 | -19.9 | 20.9 | 0.0 | -14.8 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

## Universe: small

### Slippage x1

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 93 | 58 | 19.4 | -6.79 | -10.44 | 3.0 | -7.6 | -81.5 | 3.2 | 2.2 | 1,418 | 1,940 |  |
| momentum baseline | 149 | 101 | 38.3 | -2.56 | -5.53 | 23.0 | -7.6 | -81.5 | 4.3 | 35.1 | 1,967 | 1,951 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 93 | 58 | 19.4 | -6.79 | -8,060 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -6.9 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | -27.9 | -42.8 | 22.8 |
| 2005 | 0.0 | 0.0 | -32.3 | -1.4 | 17.6 |
| 2006 | 0.0 | 0.0 | -15.0 | -41.6 | 19.0 |
| 2007 | 0.0 | 0.0 | -35.4 | 13.9 | 11.8 |
| 2008 | 0.0 | 0.0 | -22.3 | -29.3 | -43.3 |
| 2009 | 0.0 | 0.0 | 0.0 | 35.9 | 31.8 |
| 2010 | 0.0 | 0.0 | 0.0 | -16.8 | -2.2 |
| 2011 | 0.0 | 0.0 | 0.0 | 15.7 | -15.3 |
| 2012 | 0.0 | 0.0 | 0.0 | 4.3 | 14.8 |
| 2013 | 0.0 | 0.0 | 0.0 | -0.1 | 19.6 |
| 2014 | 0.0 | 0.0 | 0.0 | -46.1 | 5.1 |
| 2015 | 0.0 | 0.0 | 0.0 | 0.0 | 2.5 |
| 2016 | 0.0 | 0.0 | 0.0 | 0.0 | 11.6 |
| 2017 | 0.0 | 0.0 | 0.0 | 0.0 | 11.5 |
| 2018 | 0.0 | 0.0 | 0.0 | 0.0 | -2.9 |
| 2019 | 0.0 | 0.0 | 0.0 | 0.0 | 23.2 |
| 2020 | 0.0 | 0.0 | 0.0 | 0.0 | 1.9 |
| 2021 | 0.0 | 0.0 | 0.0 | 0.0 | 16.7 |
| 2022 | 0.0 | 0.0 | 0.0 | 0.0 | -1.3 |
| 2023 | 0.0 | 0.0 | 0.0 | 0.0 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

### Slippage x2

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 90 | 57 | 20.0 | -6.92 | -10.64 | 3.0 | -7.5 | -81.1 | 3.1 | 2.2 | 1,557 | 1,984 |  |
| momentum baseline | 140 | 96 | 36.4 | -3.20 | -6.13 | 31.0 | -8.2 | -83.6 | 4.3 | 34.0 | 1,848 | 1,684 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 90 | 57 | 20.0 | -6.92 | -8,016 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -7.2 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | -28.2 | -43.3 | 22.8 |
| 2005 | 0.0 | 0.0 | -32.7 | -2.2 | 17.6 |
| 2006 | 0.0 | 0.0 | -15.5 | -42.3 | 19.0 |
| 2007 | 0.0 | 0.0 | -36.7 | 13.5 | 11.8 |
| 2008 | 0.0 | 0.0 | -17.2 | -29.4 | -43.3 |
| 2009 | 0.0 | 0.0 | 0.0 | 35.3 | 31.8 |
| 2010 | 0.0 | 0.0 | 0.0 | -17.7 | -2.2 |
| 2011 | 0.0 | 0.0 | 0.0 | 15.1 | -15.3 |
| 2012 | 0.0 | 0.0 | 0.0 | 3.5 | 14.8 |
| 2013 | 0.0 | 0.0 | 0.0 | -14.1 | 19.6 |
| 2014 | 0.0 | 0.0 | 0.0 | -42.2 | 5.1 |
| 2015 | 0.0 | 0.0 | 0.0 | 0.0 | 2.5 |
| 2016 | 0.0 | 0.0 | 0.0 | 0.0 | 11.6 |
| 2017 | 0.0 | 0.0 | 0.0 | 0.0 | 11.5 |
| 2018 | 0.0 | 0.0 | 0.0 | 0.0 | -2.9 |
| 2019 | 0.0 | 0.0 | 0.0 | 0.0 | 23.2 |
| 2020 | 0.0 | 0.0 | 0.0 | 0.0 | 1.9 |
| 2021 | 0.0 | 0.0 | 0.0 | 0.0 | 16.7 |
| 2022 | 0.0 | 0.0 | 0.0 | 0.0 | -1.3 |
| 2023 | 0.0 | 0.0 | 0.0 | 0.0 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

## Verdict

**PLUMBING TEST - NOT A GO/NO-GO.** The price data has no delisted stocks and no point-in-time index membership, so every number above is biased upward by survivorship. This run proves the pipeline works end to end; it says nothing reliable about the edge. Rerun on Norgate Platinum before any decision.
- asx300 @ x1: the momentum baseline shows CAGR 24.5%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 @ x2: the momentum baseline shows CAGR 23.5%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x1: 492 trades, avg 0.05% per trade after costs, CAGR -0.5% vs benchmark 6.9% and baseline 24.5%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: 412 trades, avg 2.37% per trade after costs, CAGR 10.9% vs benchmark 6.9% and baseline 24.5%. Beats benchmark: True. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x1: 646 trades, avg -0.87% per trade after costs, CAGR -7.6% vs benchmark 6.9% and baseline 24.5%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x2: 492 trades, avg -0.33% per trade after costs, CAGR -2.7% vs benchmark 6.9% and baseline 23.5%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: 412 trades, avg 2.15% per trade after costs, CAGR 9.8% vs benchmark 6.9% and baseline 23.5%. Beats benchmark: True. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x2: 517 trades, avg -1.11% per trade after costs, CAGR -7.6% vs benchmark 6.9% and baseline 23.5%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x1: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x1: 93 trades, avg -6.79% per trade after costs, CAGR -7.6% vs benchmark 6.9% and baseline -7.6%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x2: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x2: 90 trades, avg -6.92% per trade after costs, CAGR -7.5% vs benchmark 6.9% and baseline -8.2%. Beats benchmark: False. Beats baseline: True.

