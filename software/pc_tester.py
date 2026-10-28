"""
PC-assisted test/diagnose driver.

Board-agnostic: drives the ATLAS-DIAG firmware on the NUCLEO-F446RE (or any
board that runs the same firmware) live over USB-serial -- this script only
speaks the PING/SEL/CFG/V/RES UART protocol described at the top of
firmware/Core/Src/atlas_app.c. Selects an IC, sends the
pre-compacted test vectors from the exported JSON table (software/tables/),
reads back the physical responses, and looks the resulting signature up in
the exported fault dictionary. Never recomputes the fault dictionary itself --
that's a build-time step (generate_ic_tables.py).
"""
import argparse
import json
import os
import sys
import time

import serial

SCRIPT_DIR = os.path.dirname(os.path.abspath(__file__))
TABLES_DIR = os.path.join(SCRIPT_DIR, "tables")
RESULTS_DIR = os.path.join(SCRIPT_DIR, "results")

IC_NAMES = ["74HCT00", "74HCT02", "74HCT04", "74HCT08", "74HCT32", "74HCT86"]

PING_RETRIES = 5
PING_TIMEOUT_S = 1.0
VECTOR_TIMEOUT_S = 1.0


class BoardError(Exception):
    pass


def load_ic_table(ic_name):
    path = os.path.join(TABLES_DIR, f"{ic_name}.json")
    if not os.path.exists(path):
        raise BoardError(
            f"No exported table for {ic_name} at {path}. "
            f"Run generate_ic_tables.py first."
        )
    with open(path) as f:
        return json.load(f)


def send_line(ser, line):
    ser.write((line + "\n").encode("utf-8"))


def read_line(ser, timeout_s):
    """Read one line (up to timeout_s), ignoring blank lines. Returns '' on timeout."""
    deadline = time.time() + timeout_s
    while time.time() < deadline:
        line = ser.readline().decode("utf-8", errors="ignore").strip()
        if line:
            return line
    return ""


def do_ping(ser, log):
    for attempt in range(1, PING_RETRIES + 1):
        send_line(ser, "PING")
        reply = read_line(ser, PING_TIMEOUT_S)
        if reply == "PONG":
            return True
        print(f"PING attempt {attempt}/{PING_RETRIES}: no PONG (got {reply!r})")
    return False


def select_and_configure(ser, ic_index, pin_config):
    send_line(ser, f"SEL:{ic_index}")
    reply = read_line(ser, 1.0)
    if not reply.startswith("SEL_OK") and not reply.startswith("OK"):
        print(f"Warning: unexpected reply to SEL:{ic_index} -> {reply!r}")

    send_line(ser, f"CFG:{pin_config}")
    reply = read_line(ser, 1.0)
    if not reply.startswith("CFG_OK"):
        raise BoardError(f"CFG failed for pin_config={pin_config}: board replied {reply!r}")


def apply_vector(ser, vector):
    """Send V:<vector>, return (response_bits_or_None, round_trip_seconds)."""
    start = time.time()
    send_line(ser, f"V:{vector}")
    while True:
        line = read_line(ser, VECTOR_TIMEOUT_S - (time.time() - start))
        if not line:
            return None, time.time() - start
        if line.startswith("R:"):
            return line[2:], time.time() - start
        if line.startswith("ERR:"):
            raise BoardError(f"Board reported error for V:{vector} -> {line}")
        # Anything else is a debug/log line from the board -- print and keep waiting.
        print(f"STM32: {line}")
        if time.time() - start > VECTOR_TIMEOUT_S:
            return None, time.time() - start


def run_vectors(ser, vectors, log):
    """Send each vector in order, return (concatenated_signature, per_vector_log)."""
    signature = ""
    per_vector = []
    for vector in vectors:
        response, rtt = apply_vector(ser, vector)
        if response is None:
            print(f"Timeout waiting for response to V:{vector}")
            per_vector.append({"vector": vector, "response": None, "round_trip_s": rtt})
            signature += "?" * 1  # placeholder; a real timeout invalidates the run
        else:
            print(f"Sent: V:{vector} -> Received: R:{response}")
            per_vector.append({"vector": vector, "response": response, "round_trip_s": rtt})
            signature += response
    return signature, per_vector


def golden_run_verify(ser, ic_name, table):
    """
    Runs exhaustive_test_vectors() against a DUT assumed known-good and diffs
    every response against digital_twin's predicted golden output. This is
    the defense against silently misreading a wiring bug as a chip fault.
    """
    # Import here so pc_tester.py's normal (non-verify) path never has to
    # pull in the heavy simulation module -- verify is the one path allowed
    # to talk to digital_twin.py directly.
    sys.path.insert(0, SCRIPT_DIR)
    from digital_twin import IC_CLASSES

    ic_cls = next(c for c in IC_CLASSES if c.name == ic_name)
    ic = ic_cls()

    exhaustive_vectors = ic.exhaustive_test_vectors()
    diffs = []
    per_vector = []

    print(f"Golden-run verification: {len(exhaustive_vectors)} exhaustive vectors...")
    for vector in exhaustive_vectors:
        response, rtt = apply_vector(ser, vector)
        expected = "".join(str(b) for b in ic.simulate([int(c) for c in vector]))
        ok = response == expected
        if not ok:
            diffs.append({"vector": vector, "expected": expected, "got": response})
        per_vector.append({"vector": vector, "expected": expected, "got": response, "match": ok})

    result = {
        "num_vectors": len(exhaustive_vectors),
        "num_diffs": len(diffs),
        "diffs": diffs,
        "per_vector": per_vector,
        "passed": len(diffs) == 0,
    }
    if result["passed"]:
        print("Golden-run verification: MATCH (0 diffs). Wiring/pin-mapping looks correct.")
    else:
        print(
            f"Golden-run verification: MISMATCH -- {len(diffs)}/{len(exhaustive_vectors)} "
            f"vectors disagree with the digital twin's prediction. This points at a "
            f"wiring/pin-mapping problem, NOT a chip fault. Stopping before diagnosis."
        )
        for d in diffs[:10]:
            print(f"  V:{d['vector']} expected R:{d['expected']} got R:{d['got']}")
    return result


def diagnose(signature, table):
    fault_dictionary = table["fault_dictionary"]
    gate_map = table["gate_map"]
    names = fault_dictionary.get(signature)
    if names is None:
        return {"signature": signature, "matched": False, "fault_names": [], "displays": []}
    displays = [gate_map.get(n, n) for n in names]
    return {"signature": signature, "matched": True, "fault_names": names, "displays": displays}


def send_result(ser, result_cmd):
    """Tell the board the final verdict so its OLED + PASS/FAULT LEDs show it
    too (firmware RES: command). Best-effort: older firmware answers ERR:CMD,
    which is only a warning -- the PC-side diagnosis is unaffected."""
    result_cmd = result_cmd[:60]  # board RX buffer is 64 bytes
    send_line(ser, result_cmd)
    reply = read_line(ser, 1.5)   # board redraws the OLED (~0.3 s) before replying
    if reply != "RES_OK":
        print(f"Note: board did not confirm result display ({reply!r}) -- "
              f"flash the latest firmware to see results on the OLED/LEDs.")


def write_log(ic_name, run_record):
    os.makedirs(RESULTS_DIR, exist_ok=True)
    ts = time.strftime("%Y%m%d_%H%M%S")
    path = os.path.join(RESULTS_DIR, f"{ic_name}_{ts}.json")
    with open(path, "w") as f:
        json.dump(run_record, f, indent=2)
    print(f"Log written to {path}")
    return path


def main():
    parser = argparse.ArgumentParser(description="ATLAS-DIAG PC-assisted tester")
    parser.add_argument("--ic", choices=IC_NAMES, default="74HCT00",
                         help="Which of the 6 supported ICs to test (default: 74HCT00)")
    parser.add_argument("--port", default="COM3", help="Serial port (e.g., COM3, /dev/ttyUSB0)")
    parser.add_argument("--baudrate", type=int, default=115200, help="Baud rate (default: 115200)")
    parser.add_argument("--verify", action="store_true",
                         help="Golden-run verification: check wiring against a known-good DUT "
                              "before treating any result as a real diagnosis")
    args = parser.parse_args()

    ic_index = IC_NAMES.index(args.ic)
    table = load_ic_table(args.ic)

    run_record = {
        "ic": args.ic,
        "ic_index": ic_index,
        "port": args.port,
        "baudrate": args.baudrate,
        "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "verify_requested": args.verify,
    }

    try:
        ser = serial.Serial(args.port, args.baudrate, timeout=1.0)
        print(f"Connected to STM32 on {args.port} at {args.baudrate} baud.")
        time.sleep(2)  # let the board finish reset / USB-CDC enumeration
    except Exception as e:
        print(f"Failed to open serial port {args.port}: {e}")
        sys.exit(1)

    try:
        print("Pinging board...")
        if not do_ping(ser, run_record):
            print(f"ERROR: board did not respond with PONG after {PING_RETRIES} attempts. "
                  f"Check the port, baud rate, and that firmware is flashed.")
            sys.exit(1)
        print("PONG received -- board is alive.")

        print(f"Selecting IC {args.ic} (index {ic_index}), pin_config={table['pin_config']}")
        select_and_configure(ser, ic_index, table["pin_config"])

        if args.verify:
            verify_result = golden_run_verify(ser, args.ic, table)
            run_record["golden_run"] = verify_result
            if not verify_result["passed"]:
                send_result(ser, f"RES:VERIFY_FAIL:{verify_result['num_diffs']}/{verify_result['num_vectors']}")
                run_record["diagnosis"] = None
                write_log(args.ic, run_record)
                sys.exit(1)

        vectors = table["vectors"]
        print(f"Starting diagnosis with {len(vectors)} compacted test vectors...")
        signature, per_vector = run_vectors(ser, vectors, run_record)
        run_record["vectors_sent"] = per_vector

        print(f"\n--- Test Completed ---")
        print(f"Physical Signature: {signature}")

        diagnosis = diagnose(signature, table)
        run_record["diagnosis"] = diagnosis

        if not diagnosis["matched"]:
            print(f"Diagnosis Result: UNKNOWN -- signature {signature!r} not found in fault dictionary")
            send_result(ser, f"RES:UNKNOWN:{signature}")
        elif diagnosis["fault_names"] == ["Good"]:
            print("Diagnosis Result: PASS (Good)")
            send_result(ser, "RES:PASS")
        else:
            print(f"Diagnosis Result: FAULT -- {', '.join(diagnosis['displays'])}")
            if len(diagnosis["fault_names"]) > 1:
                print(f"  (ambiguous -- {len(diagnosis['fault_names'])} candidate faults produce this signature)")
            send_result(ser, f"RES:FAULT:{diagnosis['displays'][0]}")

    finally:
        write_log(args.ic, run_record)
        ser.close()


if __name__ == "__main__":
    main()
