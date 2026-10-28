"""
ATLAS-DIAG dashboard core -- everything the Streamlit UI needs that is NOT UI:
board communication, twin-vs-hardware analysis, automatic hints, fault
ranking, greedy-compaction trace, chip SVG drawing and result-log handling.

Source of truth for IC behaviour stays digital_twin.py -- nothing here
re-models the logic, it only calls the twin.
"""
import glob
import json
import os
import time

import serial
import serial.tools.list_ports

from digital_twin import IC_CLASSES

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")

IC_NAMES = [c.name for c in IC_CLASSES]
GATE_TYPE = {"74HCT00": "NAND", "74HCT02": "NOR", "74HCT04": "NOT",
             "74HCT08": "AND", "74HCT32": "OR", "74HCT86": "XOR"}
IC_DESC = {"74HCT00": "Quad 2-input NAND", "74HCT02": "Quad 2-input NOR",
           "74HCT04": "Hex inverter", "74HCT08": "Quad 2-input AND",
           "74HCT32": "Quad 2-input OR", "74HCT86": "Quad 2-input XOR"}
# Nucleo pin behind each DIP pin (index = DIP-1). DIP 7 / 14 = rails.
NUCLEO_PIN = ["PC2", "PC3", "PC4", "PC5", "PC6", "PC7", "GND",
              "PC9", "PC10", "PC11", "PC12", "PD2", "PB1", "+5V"]


# --------------------------------------------------------------------- twin
_twin_cache = {}


def twin(ic_name):
    """One cached twin instance per IC (response matrix is expensive-ish)."""
    if ic_name not in _twin_cache:
        cls = next(c for c in IC_CLASSES if c.name == ic_name)
        _twin_cache[ic_name] = cls()
    return _twin_cache[ic_name]


def response_row(ic_name, fault="Good"):
    """{vector -> output string} over all exhaustive vectors for one fault."""
    return twin(ic_name)._response_matrix()[fault]


def pin_roles(ic_name):
    """14 roles: 'I' (DUT input), 'O' (DUT output), 'GND', 'VCC'."""
    cfg = twin(ic_name).config_string()
    roles = []
    for i, c in enumerate(cfg):
        if i == 6:
            roles.append("GND")
        elif i == 13:
            roles.append("VCC")
        else:
            roles.append(c)
    return roles


def pin_levels(ic_name, vector, outputs):
    """Map DIP index -> 0/1 for one applied vector and its output string."""
    t = twin(ic_name)
    levels = {}
    for k, p in enumerate(t.input_pins):
        levels[p] = int(vector[k])
    if outputs:
        for k, p in enumerate(t.output_pins):
            if k < len(outputs) and outputs[k] in "01":
                levels[p] = int(outputs[k])
    return levels


def fault_label(ic_name, fault):
    return twin(ic_name).gate_map().get(fault, fault)


# -------------------------------------------------------------------- board
def list_ports():
    return [p.device for p in serial.tools.list_ports.comports()]


class Board:
    """Thin wrapper over the firmware's UART protocol (same as pc_tester.py)."""

    def __init__(self, port, baud=115200):
        self.ser = serial.Serial(port, baud, timeout=0.2)
        self.port = port
        time.sleep(0.3)
        self.ser.reset_input_buffer()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass

    def _send(self, line):
        self.ser.write((line + "\n").encode())

    def _read(self, timeout=1.0, prefix=None):
        deadline = time.time() + timeout
        while time.time() < deadline:
            line = self.ser.readline().decode(errors="ignore").strip()
            if not line:
                continue
            if prefix is None or line.startswith(prefix) or line.startswith("ERR"):
                return line
        return ""

    def ping(self):
        for _ in range(3):
            self._send("PING")
            if self._read(0.6, "PONG") == "PONG":
                return True
        return False

    def select(self, ic_name):
        idx = IC_NAMES.index(ic_name)
        self._send(f"SEL:{idx}")
        r1 = self._read(1.5, "SEL")
        self._send(f"CFG:{twin(ic_name).config_string()}")
        r2 = self._read(1.0, "CFG")
        if not r2.startswith("CFG_OK"):
            raise RuntimeError(f"Board refused configuration: {r1!r} / {r2!r}")

    def apply(self, vector):
        self._send(f"V:{vector}")
        line = self._read(1.0, "R:")
        if line.startswith("R:"):
            return line[2:]
        if line.startswith("ERR"):
            raise RuntimeError(f"Board error for V:{vector}: {line}")
        return None

    def send_result(self, cmd):
        self._send(cmd[:60])
        return self._read(1.5, "RES") == "RES_OK"


# Simulated setup faults (not chip faults) -- modelled on what the real
# bench showed during bring-up, so the hints can be demonstrated offline.
SIM_CONDITIONS = {
    "COND_NOVCC": "No VCC on pin 14 (phantom power, as seen with the series capacitor)",
    "COND_WEAKGND": "Weak ground on pin 7 (fails when many inputs are HIGH)",
    "COND_LOOSE": "Loose contact on one output (random ~5 % errors)",
}


class SimBoard:
    """Simulated board for demos / presentations without hardware: answers
    exactly like a real chip `chip` with an optional injected `fault`
    (a single stuck-at name from the twin, or one of SIM_CONDITIONS)."""

    def __init__(self, chip="74HCT00", fault="Good", auto_swap=False):
        self.chip, self.fault, self.port = chip, fault, "SIMULATED"
        self.home_chip = chip        # the chip chosen in the sidebar
        self.sel = chip
        self.auto_swap = auto_swap   # session mode: pretend the user inserted the selected IC

    def insert(self, ic_name=None):
        """Session helper: put `ic_name` in the simulated socket, or restore
        the sidebar's chip when called with None."""
        self.chip = ic_name or self.home_chip

    def close(self):
        pass

    def ping(self):
        return True

    def select(self, ic_name):
        self.sel = ic_name

    def apply(self, vector):
        import random
        time.sleep(0.001)
        n_out = len(twin(self.sel).output_pins)
        if twin(self.sel).config_string() != twin(self.chip).config_string():
            # wrong pin-out: the board reads the chip's input pins -> pulled low
            return "0" * n_out
        if not self.fault.startswith("COND_"):
            return response_row(self.chip, self.fault)[vector]
        good = response_row(self.chip)[vector]
        if self.fault == "COND_NOVCC":
            if set(vector) == {"0"}:
                return "0" * n_out
            return good if set(vector) == {"1"} else "1" * n_out
        if self.fault == "COND_WEAKGND":
            if vector.count("1") >= len(vector) - 1:
                return "".join("1" if (k >= n_out // 2 and c == "0") else c for k, c in enumerate(good))
            return good
        if self.fault == "COND_LOOSE":
            if random.random() < 0.05:
                k = n_out - 1
                return good[:k] + ("1" if good[k] == "0" else "0")
            return good
        return good

    def send_result(self, cmd):
        return True


def run_vectors(board, vectors, progress=None):
    """Apply vectors in order -> {vector: response}; progress(i, n) callback."""
    out = {}
    n = len(vectors)
    for i, v in enumerate(vectors):
        out[v] = board.apply(v)
        if progress and (i % 8 == 0 or i == n - 1):
            progress(i + 1, n)
    return out


# ----------------------------------------------------------------- analysis
def compare(ic_name, hw):
    """Twin (Good) vs hardware over whatever vectors were run."""
    good = response_row(ic_name)
    rows = []
    for v, got in hw.items():
        exp = good[v]
        rows.append({"vector": v, "expected": exp, "got": got,
                     "match": got == exp,
                     "ones": v.count("1")})
    mism = sum(1 for r in rows if not r["match"])
    return {"rows": rows, "total": len(rows), "mismatches": mism}


def per_output_errors(ic_name, cmp):
    """For each DUT output pin: how many vectors read wrong, split 0->1 / 1->0."""
    t = twin(ic_name)
    stats = []
    for k, p in enumerate(t.output_pins):
        read_hi = read_lo = 0
        for r in cmp["rows"]:
            g = r["got"] or ""
            if k >= len(g):
                continue
            e, a = r["expected"][k], g[k]
            if e == "0" and a == "1":
                read_hi += 1
            elif e == "1" and a == "0":
                read_lo += 1
        stats.append({"dip": p + 1, "nucleo": NUCLEO_PIN[p], "read_high_should_low": read_hi,
                      "read_low_should_high": read_lo})
    return stats


def rank_faults(ic_name, hw, top=6):
    """Every single stuck-at fault (plus Good), ranked by agreement with the
    hardware over the vectors actually run. 100 % = the chip behaves exactly
    like that fault."""
    t = twin(ic_name)
    m = t._response_matrix()
    ranking = []
    n = len(hw)
    for fault in t.all_fault_names():
        agree = sum(1 for v, got in hw.items() if got == m[fault][v])
        ranking.append({"fault": fault, "label": fault_label(ic_name, fault),
                        "agree": agree, "pct": 100.0 * agree / n if n else 0.0})
    ranking.sort(key=lambda r: -r["agree"])
    return ranking[:top]


def other_ic_matches(ic_name, hw):
    """Does the hardware match another IC that uses the SAME pin configuration
    (00/08/32/86 share one)? -> wrong-chip detection."""
    cfg = twin(ic_name).config_string()
    res = []
    for other in IC_NAMES:
        if other == ic_name or twin(other).config_string() != cfg:
            continue
        good = response_row(other)
        agree = sum(1 for v, got in hw.items() if got == good[v])
        res.append({"ic": other, "agree": agree, "pct": 100.0 * agree / len(hw) if hw else 0})
    res.sort(key=lambda r: -r["agree"])
    return res


def hints(ic_name, hw, cmp, repeat_diffs=None):
    """Rule-based explanations of a failed run -- the reasoning we did by
    hand during bring-up, automated. Returns [(level, title, text)]."""
    out = []
    t = twin(ic_name)
    n_out = len(t.output_pins)
    rows = cmp["rows"]
    if not rows:
        return out
    if any(r["got"] is None for r in rows):
        out.append(("error", "No reply from board",
                    "Some vectors timed out. Check USB, COM port and that the ATLAS firmware is flashed."))
        return out
    if cmp["mismatches"] == 0:
        out.append(("success", "Matches the digital twin",
                    f"All {cmp['total']} vectors agree with the fault-free model."))
        if cmp["total"] < 16:
            out.append(("info", "Quick test only",
                        "The compacted vectors find single stuck pins on a correctly powered chip. "
                        "Run Full verify to also rule out power, ground, wiring and wrong-chip problems."))
        if repeat_diffs:
            out.append(("warning", "Unstable between runs",
                        f"{repeat_diffs} vectors gave different answers on repeated runs - a contact is marginal."))
        return out

    # Wrong chip first: an exact match with another IC explains everything
    # (only trusted on a full run -- 2-3 quick vectors can match by chance).
    for o in other_ic_matches(ic_name, hw):
        if o["agree"] == len(hw) and len(hw) >= 16:
            out.append(("error", f"Wrong chip: behaves exactly like a {o['ic']}",
                        f"Every vector matches the {o['ic']} ({IC_DESC[o['ic']]}) model. Check the chip marking "
                        "or select that IC."))
            return out

    got_all = [r["got"] for r in rows]
    zero_in = "0" * len(t.input_pins)
    if zero_in in hw and set(hw[zero_in]) == {"0"} and "1" in response_row(ic_name)[zero_in]:
        out.append(("error", "Chip probably has no VCC (pin 14)",
                    "With every input LOW the outputs read all 0, which a powered gate cannot do. "
                    "The chip is only being fed through its input protection diodes (phantom power). "
                    "Check the pin 14 wire goes straight to +5 V - no capacitor in series."))
    # Phantom power, general form (works for AND/OR/XOR too): outputs read
    # all-HIGH in most mixed-input vectors, far more often than a good chip does.
    if len(rows) >= 16:
        good = response_row(ic_name)
        mixed = [r for r in rows if set(r["vector"]) == {"0", "1"}]
        hw_hi = sum(1 for r in mixed if r["got"] == "1" * n_out) / max(1, len(mixed))
        tw_hi = sum(1 for r in mixed if good[r["vector"]] == "1" * n_out) / max(1, len(mixed))
        if hw_hi >= 0.75 and hw_hi - tw_hi >= 0.4 and not any(h[1].startswith("Chip probably has no VCC") for h in out):
            out.append(("error", "Outputs float HIGH - check VCC (pin 14) and GND (pin 7)",
                        f"{hw_hi:.0%} of mixed-input vectors read all outputs HIGH (a good chip: {tw_hi:.0%}). "
                        "That is what an unpowered chip fed through its inputs looks like. "
                        "Wire pin 14 straight to +5 V and pin 7 straight to GND - no capacitor in series."))
    if all(g == "0" * n_out for g in got_all):
        out.append(("error", "Outputs never go HIGH",
                    "Chip missing, not powered, or inserted the wrong way round (notch must face left)."))
    elif all(g == "1" * n_out for g in got_all):
        out.append(("error", "Outputs never go LOW",
                    "Ground (pin 7) is probably open, or the chip is missing and the lines are floating high."))

    # ground bounce: failures are 'reads HIGH, should be LOW' and grow with the number of HIGH inputs
    bad = [r for r in rows if not r["match"]]
    hi_fail = sum(1 for r in bad for k in range(n_out) if r["expected"][k] == "0" and r["got"][k] == "1")
    lo_fail = sum(1 for r in bad for k in range(n_out) if r["expected"][k] == "1" and r["got"][k] == "0")
    avg_ones_bad = sum(r["ones"] for r in bad) / len(bad)
    avg_ones_all = sum(r["ones"] for r in rows) / len(rows)
    if len(rows) >= 16 and hi_fail > 0 and lo_fail == 0 and avg_ones_bad > avg_ones_all + 1:
        out.append(("warning", "Looks like a weak ground (pin 7)",
                    f"Every error is an output reading HIGH when it should be LOW, and failing vectors have "
                    f"{avg_ones_bad:.1f} HIGH inputs on average vs {avg_ones_all:.1f} overall. More HIGH inputs = "
                    "more supply current = the chip's ground lifts. Use a short direct wire from pin 7 to a Nucleo GND pin."))

    ranking = rank_faults(ic_name, hw, top=3)
    best = ranking[0]
    if best["fault"] != "Good" and best["agree"] == len(hw):
        out.append(("warning", f"Single stuck-at fault: {best['label']}",
                    f"The chip behaves exactly like {best['fault']} on all {len(hw)} vectors. "
                    "Check that pin's wire/resistor first; if the wiring is fine the chip is faulty."))
    elif best["fault"] != "Good" and best["pct"] >= 90:
        out.append(("info", f"Closest single fault: {best['label']} ({best['pct']:.0f} %)",
                    "Behaviour is close to one stuck pin but not exact - often an intermittent contact on that pin."))

    # errors concentrated on one output pin
    per = per_output_errors(ic_name, cmp)
    worst = max(per, key=lambda p: p["read_high_should_low"] + p["read_low_should_high"])
    tot = sum(p["read_high_should_low"] + p["read_low_should_high"] for p in per)
    if tot and (worst["read_high_should_low"] + worst["read_low_should_high"]) == tot and len(per) > 1:
        out.append(("info", f"All errors on DIP pin {worst['dip']} ({worst['nucleo']})",
                    "Only this output line is wrong - inspect its jumper, its 1 kOhm resistor and the header pin."))
    if repeat_diffs:
        out.append(("warning", "Unstable between runs",
                    f"{repeat_diffs} vectors changed between repeated runs - a loose contact, not a clean fault."))
    if len(out) == 0:
        out.append(("info", "Multiple / unusual fault",
                    "Does not match a single stuck pin, a power fault or another IC. Likely bridged pins or several faults."))
    return out


# ------------------------------------------------------------ compaction trace
def greedy_trace(ic_name):
    """Re-run the twin's greedy set-cover while recording each step, so the
    UI can show WHY those few vectors were chosen. Mirrors
    DigitalTwin.compacted_test_vectors() exactly (and asserts it)."""
    t = twin(ic_name)
    m = t._response_matrix()
    faults = t.all_fault_names()
    cand = list(t.exhaustive_test_vectors())
    pairs = {(faults[i], faults[j]) for i in range(len(faults)) for j in range(i + 1, len(faults))}
    total_pairs = len(pairs)
    steps = []
    while pairs and cand:
        best, best_res = None, set()
        for v in cand:
            res = {(a, b) for (a, b) in pairs if m[a][v] != m[b][v]}
            if len(res) > len(best_res):
                best, best_res = v, res
        if best is None or not best_res:
            break
        cand.remove(best)
        pairs -= best_res
        steps.append({"step": len(steps) + 1, "vector": best, "resolved": len(best_res),
                      "remaining": len(pairs)})
    assert [s["vector"] for s in steps] == t.compacted_test_vectors()
    aliased = [names for names in t.fault_dictionary().values() if len(names) > 1]
    return {"total_pairs": total_pairs, "steps": steps, "unresolvable": len(pairs),
            "aliased_groups": aliased, "faults": faults}


# --------------------------------------------------------------------- SVG
def chip_svg(ic_name, levels=None, bad_pins=(), fault_pin=None, fault_type=None, width=560):
    """14-pin DIP as seen on the breadboard: notch LEFT, pins 1-7 along the
    bottom (left->right), 8-14 along the top (right->left)."""
    levels = levels or {}
    roles = pin_roles(ic_name)
    W, H = 560, 250
    bx, by, bw, bh = 70, 70, 420, 110
    pitch = bw / 7
    colors = {"I": "#3b82f6", "O": "#f59e0b", "GND": "#6b7280", "VCC": "#ef4444"}
    parts = [f'<svg viewBox="0 0 {W} {H}" width="{width}" xmlns="http://www.w3.org/2000/svg" '
             'style="font-family:Inter,Segoe UI,Arial,sans-serif">']
    parts.append(f'<rect x="{bx}" y="{by}" width="{bw}" height="{bh}" rx="10" fill="#1f2937" stroke="#111827" stroke-width="2"/>')
    parts.append(f'<path d="M{bx} {by + bh/2 - 16} A16 16 0 0 1 {bx} {by + bh/2 + 16}" fill="#111827"/>')
    parts.append(f'<circle cx="{bx + 22}" cy="{by + bh - 20}" r="5" fill="#374151"/>')
    parts.append(f'<text x="{bx + bw/2}" y="{by + bh/2 - 4}" fill="#e5e7eb" font-size="22" font-weight="700" text-anchor="middle">{ic_name}</text>')
    parts.append(f'<text x="{bx + bw/2}" y="{by + bh/2 + 20}" fill="#9ca3af" font-size="13" text-anchor="middle">{IC_DESC[ic_name]}</text>')
    for dip in range(1, 15):
        i = dip - 1
        if dip <= 7:
            cx = bx + pitch * (i + 0.5)
            py, ly, ny = by + bh, by + bh + 42, by + bh + 58
        else:
            cx = bx + pitch * (14 - dip + 0.5)
            py, ly, ny = by - 26, by - 32, by - 48
        role = roles[i]
        lvl = levels.get(i)
        fill = colors[role]
        opacity = "1" if (lvl == 1 or role in ("GND", "VCC")) else ("0.35" if lvl == 0 else "0.7")
        stroke = "#dc2626" if dip in bad_pins else "#111827"
        sw = 4 if dip in bad_pins else 1.5
        parts.append(f'<rect x="{cx - 11}" y="{py}" width="22" height="26" rx="3" fill="{fill}" '
                     f'fill-opacity="{opacity}" stroke="{stroke}" stroke-width="{sw}"/>')
        txt = str(lvl) if lvl is not None and role in ("I", "O") else ""
        parts.append(f'<text x="{cx}" y="{py + 18}" fill="#fff" font-size="13" font-weight="700" text-anchor="middle">{txt}</text>')
        tag = {"I": "IN", "O": "OUT", "GND": "GND", "VCC": "VCC"}[role]
        parts.append(f'<text x="{cx}" y="{ly}" fill="#374151" font-size="11" text-anchor="middle">{dip} {tag}</text>')
        parts.append(f'<text x="{cx}" y="{ny}" fill="#9ca3af" font-size="10" text-anchor="middle">{NUCLEO_PIN[i]}</text>')
        if fault_pin == dip:
            mark_y = py + 13
            parts.append(f'<circle cx="{cx + 16}" cy="{mark_y - 16}" r="10" fill="#dc2626"/>')
            parts.append(f'<text x="{cx + 16}" y="{mark_y - 12}" fill="#fff" font-size="9" font-weight="700" text-anchor="middle">{fault_type}</text>')
    parts.append('</svg>')
    return "".join(parts)


def oled_svg(line1, status, detail, width=260):
    """Mock of the board's 128x64 OLED."""
    return (f'<svg viewBox="0 0 256 128" width="{width}" xmlns="http://www.w3.org/2000/svg" '
            'style="font-family:Consolas,monospace">'
            '<rect x="0" y="0" width="256" height="128" rx="8" fill="#0b132b" stroke="#1e3a8a" stroke-width="3"/>'
            '<text x="12" y="24" fill="#7dd3fc" font-size="16">ATLAS-DIAG</text>'
            f'<text x="12" y="50" fill="#7dd3fc" font-size="16">{line1}</text>'
            f'<text x="12" y="86" fill="#e0f2fe" font-size="30" font-weight="700">{status}</text>'
            f'<text x="12" y="114" fill="#7dd3fc" font-size="15">{detail}</text></svg>')


# ------------------------------------------------------------------- logs
def save_run(record):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{record['ic']}_{ts}_dash.json")
    with open(path, "w") as f:
        json.dump(record, f, indent=2)
    return path


def load_history():
    """Read every results/*.json (pc_tester.py and dashboard runs)."""
    runs = []
    for path in sorted(glob.glob(os.path.join(RESULTS_DIR, "*.json"))):
        try:
            with open(path) as f:
                d = json.load(f)
        except Exception:
            continue
        ic = d.get("ic", "?")
        ts = d.get("timestamp", "")
        src = d.get("source", "pc_tester")
        full_total = full_bad = None
        if "golden_run" in d and d["golden_run"]:
            full_total = d["golden_run"].get("num_vectors")
            full_bad = d["golden_run"].get("num_diffs")
        if "full" in d:
            full_total = d["full"].get("total")
            full_bad = d["full"].get("mismatches")
        diag = d.get("diagnosis") or {}
        if diag.get("matched") and diag.get("fault_names") == ["Good"]:
            verdict = "PASS"
        elif full_bad == 0 and full_total:
            verdict = "PASS"
        elif diag.get("matched"):
            verdict = "FAULT"
        elif full_bad:
            verdict = "FAULT"
        else:
            verdict = d.get("verdict", "UNKNOWN")
        runs.append({"file": os.path.basename(path), "ic": ic, "timestamp": ts, "source": src,
                     "mode": d.get("mode", "full" if full_total and full_total >= 16 else "quick"),
                     "verdict": d.get("verdict", verdict), "full_total": full_total,
                     "full_bad": full_bad,
                     "detail": ", ".join(diag.get("displays", [])) if diag else d.get("detail", "")})
    return runs
