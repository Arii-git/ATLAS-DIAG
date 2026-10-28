# Software

Everything on the PC side is in `software/`. It needs Python 3 (developed on 3.12; the twin and the tests also run on 3.10) and four packages: `pyserial`, `streamlit`, `plotly` and `pandas`.

```powershell
cd software
python -m venv .venv
.venv\Scripts\activate          # Linux/macOS: source .venv/bin/activate
pip install -r requirements.txt
```

| File | Purpose |
|---|---|
| `digital_twin.py` | Model of each chip: pin-out, gate logic, every single stuck-at fault, greedy test compaction, fault dictionary |
| `generate_ic_tables.py` | Runs the twin once and writes `tables/*.json` and `firmware/Core/Inc/ic_table_*.h` |
| `pc_tester.py` | Command-line tester that drives the board over the serial port |
| `dashboard.py` | Streamlit dashboard (live tests, all-IC session, fault sandbox, compaction, history) |
| `atlas_core.py` | Non-UI code for the dashboard: serial link, simulated board, twin-vs-hardware analysis, hints, chip and OLED drawings, log handling |
| `test_digital_twin.py` | Unit tests for the twin |
| `verify_all_ics.py` | End-to-end check of every chip and every fault through the dashboard's diagnosis logic |
| `run_dashboard.bat` | Windows shortcut: installs the requirements and starts the dashboard |
| `tables/` | Exported JSON tables, one per chip, plus `compaction_stats.json` |

## Digital twin

`digital_twin.py` defines one class per chip (`IC74HCT00` to `IC74HCT86`), all built on a shared `DigitalTwin` base class. Each class has a 14-character pin configuration (`I` = chip input, `O` = chip output, `X` = power) and the gate topology, for example gate 1 of the 74HCT00 has inputs on pins 1 and 2 and its output on pin 3.

For each chip the twin:

1. lists every input combination: 2^8 = 256 for the quad 2-input gates, 2^6 = 64 for the hex inverter;
2. lists the faults: each of the 12 signal pins stuck at 0 (`PinN_SA0`) or stuck at 1 (`PinN_SA1`), 24 faults in all, plus `Good`, for 25 behaviours;
3. simulates every behaviour on every input combination to get a response matrix;
4. picks the test vectors with a greedy pair-separating search. With 25 behaviours there are 300 pairs to tell apart. Each step adds the vector that separates the most pairs that are still confused, and the search stops when no remaining vector separates any remaining pair. Pairs left over at that point are equivalent faults: no test at the pins can tell them apart;
5. builds the fault dictionary: the concatenated outputs of the selected vectors (the signature) mapped to the list of behaviours that give it;
6. gives each fault a short display name such as `G1 IN2 stuck-HI` or `G4 OUT stuck-LO`.

The result is three vectors for each quad gate chip (256 to 3, a 98.8 % reduction) and two for the inverter (64 to 2, 96.9 %). Searching all smaller vector sets confirms that no set with fewer vectors separates as many pairs, so the greedy result is minimal here. The remaining ambiguity is pin-level fault equivalence. On the 74HCT00, for instance, an input stuck low forces the output high, so `G1 IN1 stuck-LO`, `G1 IN2 stuck-LO` and `G1 OUT stuck-HI` give the same signature and are reported together.

The twin models logic values only. It does not model timing, bridging faults, multiple simultaneous faults, supply current or differences between individual chips. The full exhaustive check on the board catches behaviour outside the single stuck-at model, but cannot name it.

Main methods, used by the other scripts:

| Method | Returns |
|---|---|
| `exhaustive_test_vectors()` | every input combination as a bit string (first input = most significant bit) |
| `simulate(bits)` | output bits for one input vector |
| `compacted_test_vectors()` | the greedy vector set |
| `fault_dictionary()` | signature to list of fault names |
| `gate_map()` | fault name to display string |
| `compaction_stats()` | (exhaustive count, compacted count, number of equivalence classes) |

## Table generation

```powershell
python generate_ic_tables.py
```

This runs the twin once per chip and writes, from the same in-memory result:

- `software/tables/<chip>.json`: pin configuration, compacted vectors, fault dictionary and display names, read by `pc_tester.py`;
- `firmware/Core/Inc/ic_table_<chip>.h` and `ic_table_common.h`: the same data as C arrays, plus a golden table holding the fault-free response to every input combination, used by the on-board full check;
- `software/tables/compaction_stats.json`: exhaustive count, compacted count and equivalence classes per chip.

Run it only after changing `digital_twin.py`, then rebuild the firmware. Neither the firmware nor `pc_tester.py` computes a fault dictionary itself; both read the files this script writes.

## Command-line tester

```powershell
python pc_tester.py --ic 74HCT00 --port COM6 --verify
```

| Option | Meaning | Default |
|---|---|---|
| `--ic` | `74HCT00`, `74HCT02`, `74HCT04`, `74HCT08`, `74HCT32` or `74HCT86` | `74HCT00` |
| `--port` | serial port, e.g. `COM6` on Windows, `/dev/ttyACM0` on Linux | `COM3` |
| `--baudrate` | serial speed | `115200` |
| `--verify` | apply every input combination and compare with the twin before diagnosing | off |

What it does:

1. opens the port, waits 2 s for the board, then sends `PING` (up to five attempts);
2. sends `SEL:<index>` and `CFG:<pin configuration>`;
3. with `--verify`, applies all 2^n vectors and compares each response with the twin. On any mismatch it prints the first ten differences, sends `RES:VERIFY_FAIL:<n>/<N>` to the board, saves the log and stops. Many mismatches on a chip you know is good point to a wiring or pin-order problem;
4. applies the compacted vectors, joins the responses into a signature and looks it up in `tables/<chip>.json`;
5. prints PASS, the fault name (and how many faults share the signature), or UNKNOWN, and sends the matching `RES:` command so the OLED and LEDs show the result;
6. saves a JSON log to `software/results/<chip>_<YYYYMMDD_HHMMSS>.json` (fields are described in [data/README.md](../data/README.md)).

Only one program can hold the COM port, so close the dashboard and any serial monitor first.

## Dashboard

```powershell
streamlit run dashboard.py
```

On Windows, `run_dashboard.bat` does the same and installs the requirements first. The dashboard opens in the browser at `http://localhost:8501`.

### Sidebar

Choose the chip under test, then the COM port, and click Connect. The sidebar shows Online or Offline; everything that only needs the twin works while offline. Choosing `Simulated board (demo)` as the port adds two more fields: the chip that is "in the socket" and a fault to give it. The fault list holds every single stuck-at fault of that chip and three bench conditions:

| Condition | Simulated behaviour |
|---|---|
| No VCC on pin 14 | outputs stay high on most mixed input vectors (phantom power through the inputs) |
| Weak ground on pin 7 | some outputs read high when nearly all inputs are high |
| Loose contact on one output | about 5 % of reads on one output pin are flipped at random |

If the selected profile has a different pin-out from the simulated chip, the simulated board reads every output as 0, which is a simplification of what a real board sees with the wrong chip. The simulated chip and fault are applied when you click Connect; disconnect and connect again to change them.

### Live test

- Quick test runs the 2 or 3 compacted vectors and names a fault if there is one. A pass from this button is shown as `quick test OK`.
- Full verify runs all 2^n vectors (repeat 1 to 3 times to spot loose contacts) and gives the same verdict the standalone firmware would.
- Identify chip runs the full set of vectors under each chip's pin-out and ranks the six profiles by the share of vectors that match.

After a run the tab shows the verdict, the number of vectors, mismatches, the signature and the time taken; a drawing of the chip with each pin's level for any vector you pick (or an animation through all of them); a preview of what the OLED shows; automatic hints; a twin-vs-hardware heatmap of every input combination (green = match, red = mismatch, grey = not run); errors per output pin; and the single faults whose behaviour is closest to what was measured. The hints recognise a wrong chip, a missing VCC, outputs stuck high or low, a weak ground, a single stuck-at fault, errors confined to one pin, and results that change between repeated runs.

### All-IC session

One Test button per chip. Insert each chip in turn (unplug USB before every swap) and press its button. Each test runs the full exhaustive check and also compares the result with the other chips' truth tables, so a chip that is really a different part is reported as `Wrong chip - behaves like ...`. The session report lists every chip with its vector counts, verdict, mismatches, detail, time and timestamp, and can be downloaded as CSV or Markdown. With the simulated board, a toggle makes the simulated socket follow the chip being tested.

### Fault sandbox

Pick a chip, a DIP pin and SA0 or SA1, and toggle the inputs. The tab draws the good chip and the faulty chip side by side, shows the predicted signature, how many of the 2^n vectors expose the fault, which other faults look identical, the OLED screen the tester would show, and a heatmap of the input combinations where the fault is visible. No board is needed.

### Compaction

For the chosen chip: exhaustive and compacted vector counts, the number of fault pairs to separate, the number of equivalence classes, a step-by-step chart of the greedy search (pairs separated at each step, pairs still confused), the signature of every fault under the compacted vectors, and the members of each equivalence class. A table for all six chips can be downloaded as `compaction_stats.csv`.

### History

Reads every JSON log in `software/results/`, from both the dashboard and `pc_tester.py`. Filter by chip and verdict, see runs per chip and mismatches per full run over time, and export the table as `atlas_history.csv`. The logs from this project are archived in `data/results/`; copy them into `software/results/` to browse them here.

## Tests

```powershell
python -m unittest test_digital_twin -v
python verify_all_ics.py
```

`test_digital_twin.py` checks each chip's truth table, that any two faults told apart by the exhaustive set are still told apart by the compacted set, that every pin configuration has 14 characters, and that every fault has a display name short enough for the OLED. `verify_all_ics.py` puts each chip, with each of its 24 faults and with no fault, into the simulated board, runs the full exhaustive test, and checks that the dashboard's diagnosis names the fault; it also checks every truth table against a hand-written datasheet reference and that no chip passes when a different chip is in the socket. The expected output ends with `PROBLEMS: 0`.
