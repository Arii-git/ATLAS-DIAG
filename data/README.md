# Data

Measurements from the ATLAS-DIAG bench prototype (NUCLEO-F446RE, firmware in `../firmware/`), recorded on 30 September, 1 October and 28 October 2026.

| Path | Contents |
|---|---|
| `results/` | 165 JSON run logs: 8 from `pc_tester.py`, 157 from the dashboard (file names ending in `_dash.json`) |
| `sessions/` | All-IC session reports exported from the dashboard (28 October 2026, two sessions; CSV and Markdown) |
| `atlas_history.csv` | History table exported from the dashboard's History tab: 162 runs (three 74HCT00 runs from the evening of 28 October were logged after the export) |
| `compaction_stats.csv` | Compaction table exported from the dashboard's Compaction tab |

The software writes new logs to `software/results/`. To browse these archived runs in the dashboard's History tab, copy the files from `results/` into `software/results/`.

## File names

`<chip>_<YYYYMMDD>_<HHMMSS>.json` for `pc_tester.py`, and `<chip>_<YYYYMMDD>_<HHMMSS>_dash.json` for the dashboard. Times are the PC's local time.

## Bit strings

- A vector (`vector`) has one character per chip input, in DIP pin order, lowest pin first. For the quad gates that is pins 1, 2, 4, 5, 9, 10, 12, 13 (74HCT02: 2, 3, 5, 6, 8, 9, 11, 12); for the 74HCT04, pins 1, 3, 5, 9, 11, 13.
- A response (`expected`, `got`, `response`) has one character per chip output, in DIP pin order.
- A signature is the responses to the compacted vectors joined together, e.g. `000011111111` for a good 74HCT00 (3 vectors x 4 outputs).

## Logs from `pc_tester.py`

| Field | Type | Meaning |
|---|---|---|
| `ic` | string | chip profile, e.g. `74HCT00` |
| `ic_index` | int | index sent in `SEL:` (0 = 74HCT00 ... 5 = 74HCT86) |
| `port`, `baudrate` | string, int | serial settings |
| `timestamp` | string | ISO 8601 local time |
| `verify_requested` | bool | `--verify` was given |
| `golden_run` | object | only with `--verify`: `num_vectors`, `num_diffs`, `diffs` (list of `vector`, `expected`, `got`), `per_vector` (same plus `match`), `passed` |
| `vectors_sent` | list | compacted vectors: `vector`, `response`, `round_trip_s` (seconds) |
| `diagnosis` | object or null | `signature`, `matched` (found in the fault dictionary), `fault_names` (e.g. `["Good"]` or `["Pin11_SA0"]`), `displays` (e.g. `["G4 OUT stuck-LO"]`); null when verification failed |

## Logs from the dashboard

Every dashboard log has `ic`, `timestamp`, `source` (always `dashboard`), `mode`, `verdict` and `detail`. The other fields depend on `mode`:

| `mode` | Extra fields |
|---|---|
| `quick` | `signature`, `candidates`, `repeat_diffs`, `seconds`, `full`, `per_vector` (3 or 2 entries) |
| `full` | as `quick`, with all 2^n vectors in `per_vector` |
| `session` | `signature`, `seconds`, `full`, `per_vector` (one test from the All-IC session) |
| `identify` | `identify`: list of `IC`, `match %`, `matching vectors` for all six profiles, best first |

| Field | Meaning |
|---|---|
| `verdict` | `PASS` or `FAULT`; for `identify`, `IDENTIFIED` or `NO MATCH` |
| `detail` | text shown on the OLED or in the session table, e.g. `256/256 OK`, `quick test OK`, `G4 OUT stuck-LO`, `Bad 175/256`, `Wrong chip - behaves like 74HCT00` |
| `signature` | responses to the compacted vectors, joined |
| `candidates` | fault names in the dictionary for that signature (several for an equivalence class) |
| `repeat_diffs` | number of vectors that changed between repeated full runs (0 = stable) |
| `seconds` | wall-clock time of the run on the PC |
| `full.total`, `full.mismatches` | vectors applied and vectors that disagreed with the twin |
| `per_vector` | list of `vector`, `expected` (twin), `got` (hardware), `match`, `ones` (number of 1s in the vector) |

## `atlas_history.csv`

Columns: `file`, `ic`, `timestamp`, `source` (`pc_tester` or `dashboard`), `mode`, `verdict`, `full_total`, `full_bad` (mismatching vectors), `detail`.

## Session reports

Columns: `IC`, `function`, `exhaustive vectors`, `compacted vectors`, `result`, `mismatches`, `detail`, `test time (s)`, `tested at`. In both sessions on 28 October 2026 a good 74HCT00 was in the socket for all six tests, which is why the other five profiles fail.

## Notes on the early logs

The first two `pc_tester.py` logs on 30 September (`74HCT00_20260930_095448.json` and `_095719.json`) failed verification with 183 of 256 vectors wrong. They were recorded during bring-up and are kept as part of the record; the next log, at 10:39 the same morning, passes 256/256.
