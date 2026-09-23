# Phase 1 backtest report

_Generated 2026-09-23 13:49._

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
| A post-announcement drift (entry next open, MAIN) | 492 | 155 | 45.7 | 0.37 | -0.74 | 10.0 | 1.4 | -33.8 | 12.3 | 20.3 | 12,330 | 26,748 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 412 | 142 | 51.9 | 2.41 | 0.81 | 10.0 | 11.1 | -25.7 | 13.9 | 17.1 | 32,178 | 178,642 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 725 | 180 | 43.7 | -0.15 | -1.38 | 10.0 | -2.6 | -61.7 | 16.3 | 28.9 | 15,636 | 11,658 |  |
| momentum baseline | 257 | 105 | 49.8 | 11.46 | -0.06 | 23.0 | 24.2 | -52.0 | 6.1 | 65.1 | 90,593 | 1,784,554 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 96,444 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 492 | 155 | 45.7 | 0.37 | 6,748 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 20 | 15.0 | -2.89 |
| agm | 9 | 55.6 | 0.93 |
| capital_raising | 29 | 34.5 | -0.97 |
| clinical | 7 | 57.1 | 4.89 |
| contract | 31 | 38.7 | 1.72 |
| dividend | 2 | 0.0 | -4.85 |
| exploration | 39 | 28.2 | 0.52 |
| guidance | 38 | 47.4 | -0.34 |
| investor_presentation | 1 | 100.0 | 7.26 |
| other | 111 | 45.9 | -0.73 |
| response_to_asx | 6 | 33.3 | -0.51 |
| results | 181 | 55.2 | 1.24 |
| trading_halt | 18 | 44.4 | 1.59 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 412 | 142 | 51.9 | 2.41 | 158,642 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 16 | 43.8 | -0.71 |
| agm | 7 | 71.4 | 7.35 |
| capital_raising | 27 | 44.4 | -2.00 |
| clinical | 5 | 40.0 | 5.07 |
| contract | 24 | 41.7 | 4.78 |
| dividend | 1 | 0.0 | -5.60 |
| exploration | 33 | 48.5 | 9.73 |
| guidance | 35 | 65.7 | 1.75 |
| investor_presentation | 1 | 100.0 | 9.26 |
| other | 83 | 39.8 | -0.71 |
| response_to_asx | 3 | 33.3 | -1.68 |
| results | 170 | 60.6 | 3.41 |
| trading_halt | 7 | 14.3 | -5.23 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 725 | 180 | 43.7 | -0.15 | -8,351 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.3 | 2.0 | -0.8 | 0.0 | 9.0 |
| 2004 | 18.2 | 39.7 | 16.1 | 42.1 | 22.8 |
| 2005 | 1.1 | 0.7 | 4.8 | 107.3 | 17.6 |
| 2006 | 8.9 | 7.1 | -6.2 | 188.4 | 19.0 |
| 2007 | -11.4 | -1.1 | -11.7 | 42.2 | 11.8 |
| 2008 | -8.4 | -5.8 | -6.7 | -14.7 | -43.3 |
| 2009 | -4.0 | 21.0 | -4.4 | 78.1 | 31.8 |
| 2010 | 9.0 | 10.7 | 11.2 | 9.3 | -2.2 |
| 2011 | -4.4 | -1.7 | -1.0 | 27.2 | -15.3 |
| 2012 | -9.4 | 7.6 | -11.7 | 29.8 | 14.8 |
| 2013 | 12.8 | 1.6 | 9.1 | 68.8 | 19.6 |
| 2014 | -7.3 | 3.8 | -13.3 | -21.6 | 5.1 |
| 2015 | 5.2 | 32.8 | -1.8 | 12.9 | 2.5 |
| 2016 | -4.0 | 16.6 | -9.3 | -13.1 | 11.6 |
| 2017 | -7.2 | -4.7 | -15.2 | 27.9 | 11.5 |
| 2018 | 2.2 | 10.7 | 9.2 | -11.7 | -2.9 |
| 2019 | 31.4 | 54.9 | 36.5 | 11.5 | 23.2 |
| 2020 | 9.9 | 13.9 | -11.4 | 1.5 | 1.9 |
| 2021 | 16.7 | -3.8 | 7.7 | 164.4 | 16.7 |
| 2022 | -6.4 | 23.0 | -35.6 | -28.4 | -1.3 |
| 2023 | -11.9 | 22.6 | 2.6 | -14.3 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

### Slippage x2

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 492 | 155 | 44.3 | 0.08 | -1.15 | 10.0 | -0.3 | -41.2 | 11.6 | 20.2 | 16,225 | 18,785 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 412 | 142 | 51.7 | 2.19 | 0.60 | 10.0 | 10.0 | -26.8 | 13.4 | 17.0 | 42,991 | 143,923 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 726 | 180 | 42.3 | -0.53 | -1.78 | 10.0 | -5.8 | -78.4 | 15.2 | 28.7 | 19,160 | 5,846 |  |
| momentum baseline | 256 | 105 | 48.8 | 11.11 | -0.56 | 23.0 | 23.0 | -53.3 | 6.1 | 65.2 | 79,543 | 1,452,612 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 96,444 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 492 | 155 | 44.3 | 0.08 | -1,215 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 20 | 10.0 | -3.47 |
| agm | 9 | 44.4 | -0.28 |
| capital_raising | 29 | 34.5 | -1.21 |
| clinical | 7 | 57.1 | 4.62 |
| contract | 31 | 38.7 | 1.57 |
| dividend | 2 | 0.0 | -5.06 |
| exploration | 39 | 28.2 | 0.28 |
| guidance | 39 | 46.2 | -0.47 |
| investor_presentation | 1 | 100.0 | 6.98 |
| other | 111 | 45.0 | -0.93 |
| response_to_asx | 6 | 33.3 | -0.73 |
| results | 180 | 53.3 | 0.89 |
| trading_halt | 18 | 44.4 | 1.35 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 412 | 142 | 51.7 | 2.19 | 123,923 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 16 | 43.8 | -0.91 |
| agm | 7 | 71.4 | 7.13 |
| capital_raising | 27 | 40.7 | -2.15 |
| clinical | 5 | 40.0 | 4.85 |
| contract | 24 | 41.7 | 4.56 |
| dividend | 1 | 0.0 | -5.80 |
| exploration | 33 | 48.5 | 9.51 |
| guidance | 35 | 65.7 | 1.54 |
| investor_presentation | 1 | 100.0 | 9.04 |
| other | 83 | 39.8 | -0.85 |
| response_to_asx | 3 | 33.3 | -1.89 |
| results | 170 | 60.6 | 3.15 |
| trading_halt | 7 | 14.3 | -5.43 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | companies | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---:|---|
| in-sample | 726 | 180 | 42.3 | -0.53 | -14,163 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.2 | 1.9 | -1.1 | 0.0 | 9.0 |
| 2004 | 15.5 | 39.1 | 13.2 | 41.1 | 22.8 |
| 2005 | 0.8 | 0.6 | 4.2 | 106.4 | 17.6 |
| 2006 | 8.4 | 6.7 | -7.0 | 186.9 | 19.0 |
| 2007 | -12.1 | -1.7 | -13.8 | 41.0 | 11.8 |
| 2008 | -8.9 | -6.1 | -8.0 | -14.9 | -43.3 |
| 2009 | -5.2 | 20.1 | -6.4 | 77.3 | 31.8 |
| 2010 | 7.9 | 9.8 | 9.8 | 8.4 | -2.2 |
| 2011 | -5.0 | -2.2 | -2.2 | 26.1 | -15.3 |
| 2012 | -10.6 | 6.7 | -13.3 | 28.1 | 14.8 |
| 2013 | 11.9 | 1.0 | 7.3 | 67.2 | 19.6 |
| 2014 | -8.1 | 3.4 | -14.1 | -23.2 | 5.1 |
| 2015 | 3.2 | 30.7 | -4.4 | 11.2 | 2.5 |
| 2016 | -5.8 | 15.0 | -12.0 | -14.0 | 11.6 |
| 2017 | -8.5 | -4.9 | -17.2 | 25.6 | 11.5 |
| 2018 | 1.3 | 9.4 | 6.7 | -12.9 | -2.9 |
| 2019 | 28.5 | 48.8 | 30.9 | 10.7 | 23.2 |
| 2020 | 3.5 | 12.1 | -22.7 | 0.8 | 1.9 |
| 2021 | 12.4 | -5.7 | 0.9 | 159.0 | 16.7 |
| 2022 | -9.4 | 20.0 | -40.8 | -28.8 | -1.3 |
| 2023 | -15.0 | 20.8 | -5.7 | -15.6 | 12.2 |
| 2024 | n/a | n/a | n/a | n/a | 12.6 |
| 2025 | n/a | n/a | n/a | n/a | 10.2 |
| 2026 | n/a | n/a | n/a | n/a | 3.3 |

## Universe: small

### Slippage x1

| run | trades | companies | win % | avg trade % | median % | median hold | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 20,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 20,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 160 | 77 | 21.2 | -5.79 | -9.71 | 4.0 | -10.7 | -90.9 | 5.3 | 4.1 | 2,619 | 1,908 |  |
| momentum baseline | 188 | 119 | 36.7 | -3.08 | -6.30 | 23.0 | -10.6 | -90.9 | 5.5 | 41.0 | 2,482 | 1,974 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 96,444 |  |

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
| in-sample | 160 | 77 | 21.2 | -5.79 | -18,092 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -6.6 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | -27.3 | -41.6 | 22.8 |
| 2005 | 0.0 | 0.0 | -31.6 | 0.1 | 17.6 |
| 2006 | 0.0 | 0.0 | -13.5 | -42.8 | 19.0 |
| 2007 | 0.0 | 0.0 | -33.4 | 13.8 | 11.8 |
| 2008 | 0.0 | 0.0 | -22.3 | -28.9 | -43.3 |
| 2009 | 0.0 | 0.0 | -33.8 | 33.5 | 31.8 |
| 2010 | 0.0 | 0.0 | -30.7 | -14.3 | -2.2 |
| 2011 | 0.0 | 0.0 | 0.0 | 15.0 | -15.3 |
| 2012 | 0.0 | 0.0 | 0.0 | 7.6 | 14.8 |
| 2013 | 0.0 | 0.0 | 0.0 | 3.3 | 19.6 |
| 2014 | 0.0 | 0.0 | 0.0 | -52.3 | 5.1 |
| 2015 | 0.0 | 0.0 | 0.0 | -27.1 | 2.5 |
| 2016 | 0.0 | 0.0 | 0.0 | -28.3 | 11.6 |
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
| A post-announcement drift (entry next open, MAIN) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 20,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | 0 | n/a | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 20,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 155 | 76 | 21.9 | -5.86 | -9.91 | 4.0 | -10.6 | -90.6 | 5.1 | 4.0 | 3,026 | 1,981 |  |
| momentum baseline | 187 | 118 | 35.3 | -3.23 | -6.79 | 23.0 | -10.8 | -91.4 | 5.5 | 40.9 | 2,468 | 1,854 |  |
| benchmark (buy & hold) | 0 | 0 | n/a | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 96,444 |  |

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
| in-sample | 155 | 76 | 21.9 | -5.86 | -18,019 |  |
| out-of-sample (from 2023-09-22) | 0 | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -6.9 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | -27.7 | -42.2 | 22.8 |
| 2005 | 0.0 | 0.0 | -32.0 | -0.8 | 17.6 |
| 2006 | 0.0 | 0.0 | -14.0 | -43.6 | 19.0 |
| 2007 | 0.0 | 0.0 | -33.4 | 13.3 | 11.8 |
| 2008 | 0.0 | 0.0 | -23.6 | -29.0 | -43.3 |
| 2009 | 0.0 | 0.0 | -35.5 | 34.7 | 31.8 |
| 2010 | 0.0 | 0.0 | -23.4 | -14.9 | -2.2 |
| 2011 | 0.0 | 0.0 | 0.0 | 14.6 | -15.3 |
| 2012 | 0.0 | 0.0 | 0.0 | 7.0 | 14.8 |
| 2013 | 0.0 | 0.0 | 0.0 | 1.8 | 19.6 |
| 2014 | 0.0 | 0.0 | 0.0 | -51.1 | 5.1 |
| 2015 | 0.0 | 0.0 | 0.0 | -27.8 | 2.5 |
| 2016 | 0.0 | 0.0 | 0.0 | -29.2 | 11.6 |
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
- asx300 @ x1: the momentum baseline shows CAGR 24.2%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 @ x2: the momentum baseline shows CAGR 23.0%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x1: 492 trades, avg 0.37% per trade after costs, CAGR 1.4% vs benchmark 6.9% and baseline 24.2%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: 412 trades, avg 2.41% per trade after costs, CAGR 11.1% vs benchmark 6.9% and baseline 24.2%. Beats benchmark: True. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x1: 725 trades, avg -0.15% per trade after costs, CAGR -2.6% vs benchmark 6.9% and baseline 24.2%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x2: 492 trades, avg 0.08% per trade after costs, CAGR -0.3% vs benchmark 6.9% and baseline 23.0%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: 412 trades, avg 2.19% per trade after costs, CAGR 10.0% vs benchmark 6.9% and baseline 23.0%. Beats benchmark: True. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x2: 726 trades, avg -0.53% per trade after costs, CAGR -5.8% vs benchmark 6.9% and baseline 23.0%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x1: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x1: 160 trades, avg -5.79% per trade after costs, CAGR -10.7% vs benchmark 6.9% and baseline -10.6%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x2: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: NO TRADES - nothing to compare. Check whether the archive covers this universe at all.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x2: 155 trades, avg -5.86% per trade after costs, CAGR -10.6% vs benchmark 6.9% and baseline -10.8%. Beats benchmark: False. Beats baseline: True.

