# Phase 1 backtest report

_Generated 2026-09-22 14:56._

## DATA LABEL: **PLUMBING TEST - NOT A GO/NO-GO**

Price provider: `yfinance`. Survivorship-safe: **False**. Nothing in this report is a go/no-go unless the provider is Norgate Platinum with delisted stocks included.

> **Warning:** Universe asx300: announcement archive is incomplete (6/300 codes have an archive file (2%)). Strategy A results cover only archived codes; rerun when the archive finishes.

> **Warning:** Universe small: announcement archive is incomplete (0/1525 codes have an archive file (0%)). Strategy A results cover only archived codes; rerun when the archive finishes.

## Setup

- Benchmark: STW.AX adjusted close from 2008-01-02 (total-return proxy); ^AXJO PRICE index before that (understates total return)
- Universe `asx300`: 300 codes. Source: PROXY: top 300 by market cap in ASX directory today (survivorship-biased)
- Universe `small`: 1525 codes. Source: ASX directory minus asx300 (current listings only)
- Announcement archive coverage: asx300: 6/300 codes have an archive file (2%); small: 0/1525 codes have an archive file (0%)
- Out-of-sample holdout starts 2023-09-22 (last 3 years).
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

| run | trades | win % | avg trade % | median % | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 43 | 53.5 | 4.31 | 2.24 | 1.7 | -18.9 | 1.0 | 1.5 | 811 | 15,091 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 40 | 65.0 | 8.07 | 3.18 | 3.1 | -16.4 | 1.0 | 1.5 | 811 | 20,581 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 646 | 40.1 | -0.87 | -2.09 | -6.7 | -85.2 | 13.4 | 22.5 | 10,571 | 1,941 |  |
| momentum baseline | 300 | 50.3 | 13.04 | 0.08 | 28.3 | -56.1 | 5.9 | 66.3 | 71,503 | 3,714,920 |  |
| benchmark (buy & hold) | 0 | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 24 | 54.2 | 0.35 | 143 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 19 | 52.6 | 9.31 | 4,948 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 1 | 0.0 | -1.12 |
| capital_raising | 1 | 0.0 | -0.72 |
| clinical | 2 | 100.0 | 61.67 |
| contract | 4 | 75.0 | 13.87 |
| exploration | 2 | 100.0 | 24.41 |
| guidance | 3 | 66.7 | 1.84 |
| other | 10 | 50.0 | 0.35 |
| results | 19 | 47.4 | -2.20 |
| trading_halt | 1 | 0.0 | -7.58 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 22 | 72.7 | 3.47 | 1,985 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 18 | 55.6 | 13.70 | 8,596 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 1 | 0.0 | -0.44 |
| capital_raising | 1 | 100.0 | 0.70 |
| clinical | 2 | 100.0 | 77.59 |
| contract | 4 | 75.0 | 17.03 |
| exploration | 2 | 100.0 | 27.90 |
| guidance | 3 | 100.0 | 5.55 |
| other | 8 | 37.5 | 0.03 |
| results | 19 | 63.2 | 1.40 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 646 | 40.1 | -0.87 | -8,059 |  |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -1.2 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | 15.0 | 40.7 | 22.8 |
| 2005 | 0.0 | 0.0 | 4.1 | 107.2 | 17.6 |
| 2006 | 0.0 | 0.0 | -7.2 | 188.5 | 19.0 |
| 2007 | 0.0 | 0.0 | -13.7 | 42.3 | 11.8 |
| 2008 | 0.0 | 0.0 | -8.9 | -14.7 | -43.3 |
| 2009 | 0.0 | 0.0 | -7.1 | 78.3 | 31.8 |
| 2010 | 0.0 | 0.0 | 8.3 | 9.4 | -2.2 |
| 2011 | 0.0 | 0.0 | -2.8 | 27.5 | -15.3 |
| 2012 | 0.0 | 0.0 | -14.2 | 30.2 | 14.8 |
| 2013 | 0.0 | 0.0 | 6.4 | 69.1 | 19.6 |
| 2014 | 0.0 | 0.0 | -15.7 | -21.0 | 5.1 |
| 2015 | 10.7 | 15.4 | -6.8 | 13.5 | 2.5 |
| 2016 | -0.5 | -2.4 | -15.2 | -12.7 | 11.6 |
| 2017 | -0.0 | 1.7 | -20.1 | 28.7 | 11.5 |
| 2018 | 0.0 | 6.6 | 0.2 | -11.3 | -2.9 |
| 2019 | 1.9 | 2.5 | 23.2 | 11.6 | 23.2 |
| 2020 | -1.9 | 0.0 | -24.3 | 1.7 | 1.9 |
| 2021 | -6.2 | -4.1 | -6.9 | 165.6 | 16.7 |
| 2022 | 4.1 | 0.9 | -45.6 | -28.1 | -1.3 |
| 2023 | -7.8 | -3.3 | 0.0 | -12.3 | 12.2 |
| 2024 | 0.6 | 12.5 | 0.0 | 14.9 | 12.6 |
| 2025 | 46.6 | 65.5 | 0.0 | 92.0 | 10.2 |
| 2026 | 3.2 | -5.6 | 0.0 | 77.3 | 3.3 |

### Slippage x2

| run | trades | win % | avg trade % | median % | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 43 | 53.5 | 4.20 | 2.03 | 1.7 | -19.2 | 1.0 | 1.5 | 1,053 | 14,914 |  |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 40 | 65.0 | 7.90 | 2.97 | 3.0 | -16.4 | 1.0 | 1.4 | 1,087 | 20,254 |  |
| B volume-confirmed surprise (entry next open, MAIN) | 517 | 40.2 | -1.11 | -2.07 | -6.6 | -84.6 | 11.4 | 18.3 | 9,871 | 1,965 |  |
| momentum baseline | 300 | 48.7 | 12.72 | -0.22 | 27.3 | -57.7 | 5.9 | 66.3 | 62,519 | 3,060,490 |  |
| benchmark (buy & hold) | 0 | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 24 | 54.2 | 0.31 | 120 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 19 | 52.6 | 9.11 | 4,793 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 1 | 0.0 | -1.33 |
| capital_raising | 1 | 0.0 | -0.93 |
| clinical | 2 | 100.0 | 61.34 |
| contract | 4 | 75.0 | 13.64 |
| exploration | 2 | 100.0 | 24.16 |
| guidance | 3 | 66.7 | 1.64 |
| other | 10 | 50.0 | 0.54 |
| results | 19 | 47.4 | -2.39 |
| trading_halt | 1 | 0.0 | -7.77 |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 22 | 72.7 | 3.35 | 1,908 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 18 | 55.6 | 13.47 | 8,346 | SMALL SAMPLE |

**A post-announcement drift** by announcement type (mechanical headline classes)

| announcement type | trades | win % | avg trade % |
|---|---:|---:|---:|
| acquisition | 1 | 0.0 | -0.64 |
| capital_raising | 1 | 100.0 | 0.50 |
| clinical | 2 | 100.0 | 77.23 |
| contract | 4 | 75.0 | 16.80 |
| exploration | 2 | 100.0 | 27.65 |
| guidance | 3 | 100.0 | 5.34 |
| other | 8 | 37.5 | 0.07 |
| results | 19 | 63.2 | 1.19 |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 517 | 40.2 | -1.11 | -8,035 |  |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**Results by year (% return on equity)**

| year | A post-announcement drift (entry next open, MAIN) | A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | B volume-confirmed surprise (entry next open, MAIN) | baseline | benchmark |
|---|---:|---:|---:|---:|---:|
| 2003 | 0.0 | 0.0 | -1.5 | 0.0 | 9.0 |
| 2004 | 0.0 | 0.0 | 12.2 | 39.7 | 22.8 |
| 2005 | 0.0 | 0.0 | 3.5 | 106.4 | 17.6 |
| 2006 | 0.0 | 0.0 | -8.1 | 187.1 | 19.0 |
| 2007 | 0.0 | 0.0 | -16.0 | 41.3 | 11.8 |
| 2008 | 0.0 | 0.0 | -10.3 | -14.9 | -43.3 |
| 2009 | 0.0 | 0.0 | -9.3 | 77.6 | 31.8 |
| 2010 | 0.0 | 0.0 | 6.6 | 8.7 | -2.2 |
| 2011 | 0.0 | 0.0 | -4.3 | 26.5 | -15.3 |
| 2012 | 0.0 | 0.0 | -16.2 | 28.8 | 14.8 |
| 2013 | 0.0 | 0.0 | 4.2 | 67.8 | 19.6 |
| 2014 | 0.0 | 0.0 | -17.1 | -22.1 | 5.1 |
| 2015 | 10.5 | 15.2 | -10.5 | 12.2 | 2.5 |
| 2016 | -0.7 | -2.5 | -19.7 | -13.4 | 11.6 |
| 2017 | -0.1 | 1.7 | -24.0 | 27.0 | 11.5 |
| 2018 | -0.1 | 6.5 | -6.5 | -12.3 | -2.9 |
| 2019 | 1.7 | 2.4 | 9.8 | 10.9 | 23.2 |
| 2020 | -1.9 | 0.0 | -34.1 | 1.2 | 1.9 |
| 2021 | -5.4 | -4.2 | 0.0 | 163.0 | 16.7 |
| 2022 | 3.9 | 0.7 | 0.0 | -28.7 | -1.3 |
| 2023 | -8.0 | -3.1 | 0.0 | -13.4 | 12.2 |
| 2024 | 0.4 | 12.2 | 0.0 | 13.3 | 12.6 |
| 2025 | 45.9 | 64.7 | 0.0 | 89.5 | 10.2 |
| 2026 | 3.0 | -5.8 | 0.0 | 76.5 | 3.3 |

## Universe: small

### Slippage x1

| run | trades | win % | avg trade % | median % | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 0 | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 93 | 19.4 | -6.79 | -10.44 | -6.7 | -81.5 | 2.9 | 2.0 | 1,418 | 1,940 |  |
| momentum baseline | 149 | 38.3 | -2.56 | -5.53 | -6.7 | -81.5 | 4.0 | 30.7 | 1,967 | 1,951 |  |
| benchmark (buy & hold) | 0 | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 93 | 19.4 | -6.79 | -8,060 |  |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

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
| 2024 | 0.0 | 0.0 | 0.0 | 0.0 | 12.6 |
| 2025 | 0.0 | 0.0 | 0.0 | 0.0 | 10.2 |
| 2026 | 0.0 | 0.0 | 0.0 | 0.0 | 3.3 |

### Slippage x2

| run | trades | win % | avg trade % | median % | CAGR % | max DD % | turnover x/yr | exposure % | costs $ | final $ | flag |
|---|---:|---:|---:|---:|---:|---:|---:|---:|---:|---:|---|
| A post-announcement drift (entry next open, MAIN) | 0 | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) | 0 | n/a | n/a | n/a | 0.0 | 0.0 | 0.0 | 0.0 | 0 | 10,000 | SMALL SAMPLE |
| B volume-confirmed surprise (entry next open, MAIN) | 90 | 20.0 | -6.92 | -10.64 | -6.6 | -81.1 | 2.8 | 1.9 | 1,557 | 1,984 |  |
| momentum baseline | 140 | 36.4 | -3.20 | -6.13 | -7.2 | -83.6 | 3.9 | 29.7 | 1,848 | 1,684 |  |
| benchmark (buy & hold) | 0 | n/a | n/a | n/a | 6.9 | -55.1 | 0.0 | 100.0 | 0 | 48,222 |  |

**A post-announcement drift (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 0 | n/a | n/a | 0 | SMALL SAMPLE |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

**B volume-confirmed surprise (entry next open, MAIN)** in-sample vs out-of-sample

| period | trades | win % | avg trade % | total P&L $ | flag |
|---|---:|---:|---:|---:|---|
| in-sample | 90 | 20.0 | -6.92 | -8,016 |  |
| out-of-sample (from 2023-09-22) | 0 | n/a | n/a | 0 | SMALL SAMPLE |

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
| 2024 | 0.0 | 0.0 | 0.0 | 0.0 | 12.6 |
| 2025 | 0.0 | 0.0 | 0.0 | 0.0 | 10.2 |
| 2026 | 0.0 | 0.0 | 0.0 | 0.0 | 3.3 |

## Verdict

**PLUMBING TEST - NOT A GO/NO-GO.** The price data has no delisted stocks and no point-in-time index membership, so every number above is biased upward by survivorship. This run proves the pipeline works end to end; it says nothing reliable about the edge. Rerun on Norgate Platinum before any decision.
- asx300 @ x1: the momentum baseline shows CAGR 28.3%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 @ x2: the momentum baseline shows CAGR 27.3%, which is implausible for a 4-stock ASX momentum portfolio. On a universe built from today's largest companies, momentum simply buys the stocks that went on to become large. This is what survivorship bias looks like; treat the baseline as broken until point-in-time membership is available.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x1: 43 trades, avg 4.31% per trade after costs, CAGR 1.7% vs benchmark 6.9% and baseline 28.3%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: 40 trades, avg 8.07% per trade after costs, CAGR 3.1% vs benchmark 6.9% and baseline 28.3%. Beats benchmark: False. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x1: 646 trades, avg -0.87% per trade after costs, CAGR -6.7% vs benchmark 6.9% and baseline 28.3%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (entry next open, MAIN) @ x2: 43 trades, avg 4.20% per trade after costs, CAGR 1.7% vs benchmark 6.9% and baseline 27.3%. Beats benchmark: False. Beats baseline: False.
- asx300 / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: 40 trades, avg 7.90% per trade after costs, CAGR 3.0% vs benchmark 6.9% and baseline 27.3%. Beats benchmark: False. Beats baseline: False.
- asx300 / B volume-confirmed surprise (entry next open, MAIN) @ x2: 517 trades, avg -1.11% per trade after costs, CAGR -6.6% vs benchmark 6.9% and baseline 27.3%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x1: SMALL SAMPLE, 0 trades, avg n/a% per trade after costs, CAGR 0.0% vs benchmark 6.9% and baseline -6.7%. Beats benchmark: False. Beats baseline: True.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x1: SMALL SAMPLE, 0 trades, avg n/a% per trade after costs, CAGR 0.0% vs benchmark 6.9% and baseline -6.7%. Beats benchmark: False. Beats baseline: True.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x1: 93 trades, avg -6.79% per trade after costs, CAGR -6.7% vs benchmark 6.9% and baseline -6.7%. Beats benchmark: False. Beats baseline: False.
- small / A post-announcement drift (entry next open, MAIN) @ x2: SMALL SAMPLE, 0 trades, avg n/a% per trade after costs, CAGR 0.0% vs benchmark 6.9% and baseline -7.2%. Beats benchmark: False. Beats baseline: True.
- small / A post-announcement drift (same-day open, UPPER BOUND: look-ahead on volume) @ x2: SMALL SAMPLE, 0 trades, avg n/a% per trade after costs, CAGR 0.0% vs benchmark 6.9% and baseline -7.2%. Beats benchmark: False. Beats baseline: True.
- small / B volume-confirmed surprise (entry next open, MAIN) @ x2: 90 trades, avg -6.92% per trade after costs, CAGR -6.6% vs benchmark 6.9% and baseline -7.2%. Beats benchmark: False. Beats baseline: True.

