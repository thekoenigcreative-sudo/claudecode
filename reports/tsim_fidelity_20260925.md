# Fidelity: the simulator against the live 10-day paper test

The live paper test is the ground truth. Each live order is sent to this simulator's broker at its live time; fills and costs are compared. `sim` = this simulator's fill model (next bar's open, half a modelled spread, impact); `live-style` = the same broker pricing like the live arena (bar close, 0.10% slippage) - the gap between them is the fill model's own effect.

## Fri 25 Sep 2026

### asx_announcements_v2__bot

Live: realised $220.82, brokerage $26.40, net $194.42. Simulator: net $114.50 (brokerage $26.40); live-style $191.71.

| order | stock | side | type | decided | live min | sim min | live px | sim px | diff bps | live-style px | live qty | sim qty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ARN-000004 | NWL | short | limit | 10:53:52 | 10:54 | 10:54 | 17.8656 | 17.8511 | -8.1 | 17.8657 | 285 | 285 |
| ARN-000005 | HLS | buy | limit | 10:53:53 | 10:54 | 10:54 | 0.3891 | 0.3941 | 129.6 | 0.3895 | 6554 | 6554 |
| ARN-000008 | HLS | sell | stop | 11:23:08 | 11:02 | 11:02 | 0.3846 | 0.3799 | -121.7 | 0.3846 | 6554 | 6554 |
| ARN-000018 | NWL | cover | limit | 15:55:51 | 15:56 | 15:56 | 16.987 | 17.0297 | 25.1 | 16.987 | 285 | 285 |

### asx_daytrader_v1__agent

Live: realised $41.16, brokerage $26.40, net $14.76. Simulator: net $27.33 (brokerage $26.40); live-style $13.94.

| order | stock | side | type | decided | live min | sim min | live px | sim px | diff bps | live-style px | live qty | sim qty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ARN-000010 | REG | short | limit | 11:28:33 | 11:31 | 11:31 | 4.5165 | 4.5221 | 12.3 | 4.5163 | 1111 | 1111 |
| ARN-000011 | EOS | buy | limit | 13:40:00 | 13:41 | 13:41 | 11.4214 | 11.3877 | -29.5 | 11.4214 | 437 | 437 |
| ARN-000019 | EOS | sell | stop | 16:00:25 | 15:38 | 15:38 | 11.41 | 11.4098 | -0.1 | 11.4086 | 437 | 437 |
| ARN-000012 | REG | cover | limit | 15:50:16 | 15:51 | 15:51 | 4.4749 | 4.4824 | 16.8 | 4.4749 | 1111 | 1111 |
| ARN-000013 | EOS | sell | limit | 15:50:16 |  |  | None | None | None | None | 0 | 0 |

### asx_daytrader_v1__bot

Live: realised $163.85, brokerage $46.20, net $117.65. Simulator: net $125.29 (brokerage $46.20); live-style $119.19.

| order | stock | side | type | decided | live min | sim min | live px | sim px | diff bps | live-style px | live qty | sim qty |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| ARN-000006 | NWL | short | limit | 11:20:47 | 11:21 | 11:21 | 17.6223 | 17.704 | 46.3 | 17.6223 | 208 | 208 |
| ARN-000007 | DOW | buy | limit | 11:20:48 | 11:22 | 11:22 | 6.7004 | 6.7099 | 14.2 | 6.6967 | 737 | 737 |
| ARN-000017 | NWL | cover | target | 15:51:21 | 15:30 | 15:35 | 16.7437 | 16.72 | -14.2 | 16.72 | 104 | 104 |
| ARN-000009 | CWY | short | limit | 11:24:36 | 11:25 | 11:25 | 2.5974 | 2.5878 | -37.0 | 2.5974 | 1428 | 1428 |
| ARN-000014 | NWL | cover | limit | 15:50:17 | 15:51 | 15:51 | 17.047 | 16.9812 | -38.6 | 17.047 | 104 | 104 |
| ARN-000015 | DOW | sell | limit | 15:50:17 | 15:51 | 15:51 | 6.7083 | 6.7064 | -2.8 | 6.7033 | 737 | 737 |
| ARN-000016 | CWY | cover | limit | 15:50:17 | 15:51 | 15:51 | 2.5926 | 2.5902 | -9.2 | 2.5926 | 1428 | 1428 |

### Decisions: the frozen rule bots replayed vs live

```
{
 "gaps": [],
 "daytrader": {
  "lab_trades": [
   [
    "PMV",
    "buy",
    "10:32",
    -83.98
   ],
   [
    "DYL",
    "short",
    "10:34",
    -71.67
   ],
   [
    "PNI",
    "short",
    "10:36",
    -18.12
   ],
   [
    "CQR",
    "short",
    "10:57",
    3.38
   ],
   [
    "RPL",
    "buy",
    "14:11",
    37.42
   ]
  ],
  "live_entries": [
   [
    "NWL",
    "short",
    "11:21"
   ],
   [
    "DOW",
    "buy",
    "11:22"
   ],
   [
    "CWY",
    "short",
    "11:25"
   ]
  ],
  "lab_pnl": -132.98,
  "live": {
   "realised": 163.85,
   "fees": 46.2,
   "net": 117.65
  }
 },
 "v2": {
  "lab_trades": [
   [
    "NWL",
    "short",
    "10:31",
    244.74
   ],
   [
    "HLS",
    "buy",
    "10:33",
    -119.16
   ]
  ],
  "live_entries": [
   [
    "NWL",
    "short",
    "10:54"
   ],
   [
    "HLS",
    "buy",
    "10:54"
   ]
  ],
  "lab_pnl": 125.6,
  "live": {
   "realised": 220.82,
   "fees": 26.4,
   "net": 194.42
  }
 }
}
```
