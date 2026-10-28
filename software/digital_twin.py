import itertools


class DigitalTwin:
    """
    Base class for digital twins of 74HCT series ICs.

    Pipeline per IC:
      1. exhaustive_test_vectors() / exhaustive fault simulation (every SA0/SA1
         fault on every I/O pin, against every exhaustive input vector)
      2. greedy set-cover compaction -> compacted_test_vectors()
      3. fault_dictionary() built FROM the compacted vector set
      4. gate_map() gives a human-readable display string per fault name

    generate_ic_tables.py exports (2)-(4) once, offline, to JSON + C headers.
    Firmware and pc_tester.py only ever read those exported artifacts.
    """
    name = "Base"
    pin_config = "XXXXXXXXXXXXXX"

    # Per-subclass gate topology, used only to build gate_map(). Each entry:
    # {"num": <gate number, 1-based>, "inputs": [<DIP pin numbers>], "output": <DIP pin number>}
    _GATES = []

    def __init__(self):
        self.fault = None
        self.input_pins = [i for i, c in enumerate(self.pin_config) if c == 'I']
        self.output_pins = [i for i, c in enumerate(self.pin_config) if c == 'O']
        self._exhaustive_vectors_cache = None
        self._response_matrix_cache = None
        self._compacted_vectors_cache = None
        self._fault_dictionary_cache = None

    # ------------------------------------------------------------------
    # Fault injection / simulation primitives (unchanged behavior)
    # ------------------------------------------------------------------

    def inject_fault(self, fault_name):
        """
        Inject a fault into the simulation.
        fault_name format: 'Pin1_SA0', 'Pin3_SA1', or None for good behavior.
        """
        self.fault = fault_name

    def _apply_fault_to_inputs(self, pin_states):
        faulty_states = pin_states.copy()
        if self.fault:
            parts = self.fault.split('_')
            if len(parts) == 2 and parts[0].startswith("Pin"):
                pin_idx = int(parts[0][3:]) - 1
                fault_type = parts[1]
                if pin_idx in self.input_pins:
                    if fault_type == "SA0":
                        faulty_states[pin_idx] = 0
                    elif fault_type == "SA1":
                        faulty_states[pin_idx] = 1
        return faulty_states

    def _apply_fault_to_outputs(self, pin_states):
        faulty_states = pin_states.copy()
        if self.fault:
            parts = self.fault.split('_')
            if len(parts) == 2 and parts[0].startswith("Pin"):
                pin_idx = int(parts[0][3:]) - 1
                fault_type = parts[1]
                if pin_idx in self.output_pins:
                    if fault_type == "SA0":
                        faulty_states[pin_idx] = 0
                    elif fault_type == "SA1":
                        faulty_states[pin_idx] = 1
        return faulty_states

    def simulate(self, input_vector):
        """
        Simulate with current fault.
        input_vector is a list/tuple of bits corresponding to input_pins.
        Returns a list of bits corresponding to output_pins.
        """
        pin_states = {}
        for idx, pin in enumerate(self.input_pins):
            pin_states[pin] = input_vector[idx]

        pin_states = self._apply_fault_to_inputs(pin_states)
        out_states = self.calculate_outputs(pin_states)
        out_states = self._apply_fault_to_outputs(out_states)

        return [out_states[pin] for pin in self.output_pins]

    def calculate_outputs(self, pin_states):
        """
        To be implemented by subclasses.
        Returns a dictionary mapping output pin index (0-based, matching
        pin_config) to bit value.
        """
        raise NotImplementedError

    # ------------------------------------------------------------------
    # Public API (used by generate_ic_tables.py, pc_tester.py, dashboard)
    # ------------------------------------------------------------------

    def config_string(self):
        """Returns the 14-char I/O/X pin_config string, e.g. 'IIOIIOXOIIOIIX'."""
        return self.pin_config

    def all_fault_names(self):
        """['Good', 'Pin1_SA0', 'Pin1_SA1', ...] for every I/O pin, in pin order."""
        names = ["Good"]
        for pin in sorted(self.input_pins + self.output_pins):
            names.append(f"Pin{pin + 1}_SA0")
            names.append(f"Pin{pin + 1}_SA1")
        return names

    def exhaustive_test_vectors(self):
        """
        Keep the existing exhaustive vector generator under this name -- used
        only for the golden-run verification mode, never for normal diagnosis.
        Returns a list of input-vector strings, e.g. ['00', '01', '10', '11'].
        """
        if self._exhaustive_vectors_cache is None:
            num_inputs = len(self.input_pins)
            self._exhaustive_vectors_cache = [
                "".join(str(bit) for bit in v)
                for v in itertools.product([0, 1], repeat=num_inputs)
            ]
        return self._exhaustive_vectors_cache

    def _response_matrix(self):
        """
        fault_name -> {vector_string -> output_string}, over the EXHAUSTIVE
        vector set. Computed once and cached; this is the Fault x Vector
        matrix the compaction algorithm and the exhaustive fault dictionary
        are both built from.
        """
        if self._response_matrix_cache is not None:
            return self._response_matrix_cache

        vectors = self.exhaustive_test_vectors()
        matrix = {}
        for fault_name in self.all_fault_names():
            self.inject_fault(None if fault_name == "Good" else fault_name)
            row = {}
            for v in vectors:
                bits = [int(c) for c in v]
                out = self.simulate(bits)
                row[v] = "".join(str(b) for b in out)
            matrix[fault_name] = row
        self.inject_fault(None)

        self._response_matrix_cache = matrix
        return matrix

    def compacted_test_vectors(self):
        """
        Returns the MINIMUM set of input-vector strings (greedy set-cover
        result) that preserves full single-stuck-at fault coverage AND full
        pairwise diagnostic resolution among the faults this IC models.
        This is what actually gets sent to hardware -- NOT the exhaustive set.

        Algorithm: repeatedly pick the candidate
        vector that resolves the most currently-undistinguished fault pairs,
        until either every distinguishable pair is distinguished or no
        candidate vector resolves anything more.
        """
        if self._compacted_vectors_cache is not None:
            return self._compacted_vectors_cache

        matrix = self._response_matrix()
        fault_names = self.all_fault_names()
        candidate_vectors = list(self.exhaustive_test_vectors())

        undistinguished_pairs = set()
        for i in range(len(fault_names)):
            for j in range(i + 1, len(fault_names)):
                undistinguished_pairs.add((fault_names[i], fault_names[j]))

        selected = []
        while undistinguished_pairs and candidate_vectors:
            best_vector = None
            best_resolved = set()
            for v in candidate_vectors:
                resolved = set()
                for (fa, fb) in undistinguished_pairs:
                    if matrix[fa][v] != matrix[fb][v]:
                        resolved.add((fa, fb))
                if len(resolved) > len(best_resolved):
                    best_resolved = resolved
                    best_vector = v

            if best_vector is None or not best_resolved:
                # No remaining candidate distinguishes any remaining pair --
                # those faults are truly indistinguishable (aliased). Stop.
                break

            selected.append(best_vector)
            candidate_vectors.remove(best_vector)
            undistinguished_pairs -= best_resolved

        self._compacted_vectors_cache = selected
        return selected

    def fault_dictionary(self):
        """
        Maps an output-signature string (concatenated output bits across the
        compacted vectors, in vector order) -> list of fault names that
        produce that signature ('Good' = no fault). Built FROM the compacted
        vector set, not the exhaustive one -- signatures must match what the
        real hardware will actually produce when driven with
        compacted_test_vectors().
        """
        if self._fault_dictionary_cache is not None:
            return self._fault_dictionary_cache

        matrix = self._response_matrix()
        vectors = self.compacted_test_vectors()

        fault_dict = {}
        for fault_name in self.all_fault_names():
            signature = "".join(matrix[fault_name][v] for v in vectors)
            fault_dict.setdefault(signature, []).append(fault_name)

        self._fault_dictionary_cache = fault_dict
        return fault_dict

    def gate_map(self):
        """
        Maps a fault name ('Pin2_SA0') to a short, human-readable location
        string for the display, e.g. 'G1 OUT stuck-LO'. Derived from the
        subclass's _GATES topology (which mirrors calculate_outputs()'s pin
        groupings) -- this is what actually shows up on the LCD, truncated/
        abbreviated to fit a 16-character line.
        """
        gmap = {"Good": "PASS"}
        for gate in self._GATES:
            gnum = gate["num"]
            out_pin = gate["output"]
            for level, suffix in (("SA0", "LO"), ("SA1", "HI")):
                gmap[f"Pin{out_pin}_{level}"] = f"G{gnum} OUT stuck-{suffix}"
            for k, in_pin in enumerate(gate["inputs"], start=1):
                for level, suffix in (("SA0", "LO"), ("SA1", "HI")):
                    gmap[f"Pin{in_pin}_{level}"] = f"G{gnum} IN{k} stuck-{suffix}"
        return gmap

    # ------------------------------------------------------------------
    # Legacy / diagnostic helper -- kept for compatibility, now delegates
    # to the exhaustive response matrix instead of recomputing from scratch.
    # ------------------------------------------------------------------

    def generate_fault_dictionary(self):
        """
        Exhaustive-vector fault dictionary (NOT the compacted one). Used for
        golden-run verification and for measuring how much compaction saved.
        Returns (exhaustive_vectors, exhaustive_fault_dict).
        """
        matrix = self._response_matrix()
        vectors = self.exhaustive_test_vectors()

        fault_dict = {}
        for fault_name in self.all_fault_names():
            signature = "".join(matrix[fault_name][v] for v in vectors)
            fault_dict.setdefault(signature, []).append(fault_name)

        return vectors, fault_dict

    def compaction_stats(self):
        """
        (len(exhaustive_test_vectors), len(compacted_test_vectors), num_aliased_fault_groups)
        -- the three headline numbers per IC for the paper's Results table.
        num_aliased_fault_groups counts fault-dictionary entries (built from
        the compacted set) that still map to more than one fault name.
        """
        exhaustive = self.exhaustive_test_vectors()
        compacted = self.compacted_test_vectors()
        fdict = self.fault_dictionary()
        aliased = sum(1 for names in fdict.values() if len(names) > 1)
        return len(exhaustive), len(compacted), aliased


class IC74HCT00(DigitalTwin):
    name = "74HCT00"
    pin_config = "IIOIIOXOIIOIIX"
    # Quad 2-input NAND
    _GATES = [
        {"num": 1, "inputs": [1, 2], "output": 3},
        {"num": 2, "inputs": [4, 5], "output": 6},
        {"num": 3, "inputs": [9, 10], "output": 8},
        {"num": 4, "inputs": [12, 13], "output": 11},
    ]

    def calculate_outputs(self, p):
        return {
            2: 1 - (p[0] & p[1]),
            5: 1 - (p[3] & p[4]),
            7: 1 - (p[8] & p[9]),
            10: 1 - (p[11] & p[12])
        }


class IC74HCT02(DigitalTwin):
    name = "74HCT02"
    pin_config = "OIIOIIXIIOIIOX"
    # Quad 2-input NOR
    _GATES = [
        {"num": 1, "inputs": [2, 3], "output": 1},
        {"num": 2, "inputs": [5, 6], "output": 4},
        {"num": 3, "inputs": [8, 9], "output": 10},
        {"num": 4, "inputs": [11, 12], "output": 13},
    ]

    def calculate_outputs(self, p):
        return {
            0: 1 - (p[1] | p[2]),
            3: 1 - (p[4] | p[5]),
            9: 1 - (p[7] | p[8]),
            12: 1 - (p[10] | p[11])
        }


class IC74HCT04(DigitalTwin):
    name = "74HCT04"
    pin_config = "IOIOIOXOIOIOIX"
    # Hex INVERTER
    _GATES = [
        {"num": 1, "inputs": [1], "output": 2},
        {"num": 2, "inputs": [3], "output": 4},
        {"num": 3, "inputs": [5], "output": 6},
        {"num": 4, "inputs": [9], "output": 8},
        {"num": 5, "inputs": [11], "output": 10},
        {"num": 6, "inputs": [13], "output": 12},
    ]

    def calculate_outputs(self, p):
        return {
            1: 1 - p[0],
            3: 1 - p[2],
            5: 1 - p[4],
            7: 1 - p[8],
            9: 1 - p[10],
            11: 1 - p[12]
        }


class IC74HCT08(DigitalTwin):
    name = "74HCT08"
    pin_config = "IIOIIOXOIIOIIX"
    # Quad 2-input AND
    _GATES = [
        {"num": 1, "inputs": [1, 2], "output": 3},
        {"num": 2, "inputs": [4, 5], "output": 6},
        {"num": 3, "inputs": [9, 10], "output": 8},
        {"num": 4, "inputs": [12, 13], "output": 11},
    ]

    def calculate_outputs(self, p):
        return {
            2: p[0] & p[1],
            5: p[3] & p[4],
            7: p[8] & p[9],
            10: p[11] & p[12]
        }


class IC74HCT32(DigitalTwin):
    name = "74HCT32"
    pin_config = "IIOIIOXOIIOIIX"
    # Quad 2-input OR
    _GATES = [
        {"num": 1, "inputs": [1, 2], "output": 3},
        {"num": 2, "inputs": [4, 5], "output": 6},
        {"num": 3, "inputs": [9, 10], "output": 8},
        {"num": 4, "inputs": [12, 13], "output": 11},
    ]

    def calculate_outputs(self, p):
        return {
            2: p[0] | p[1],
            5: p[3] | p[4],
            7: p[8] | p[9],
            10: p[11] | p[12]
        }


class IC74HCT86(DigitalTwin):
    name = "74HCT86"
    pin_config = "IIOIIOXOIIOIIX"
    # Quad 2-input XOR
    _GATES = [
        {"num": 1, "inputs": [1, 2], "output": 3},
        {"num": 2, "inputs": [4, 5], "output": 6},
        {"num": 3, "inputs": [9, 10], "output": 8},
        {"num": 4, "inputs": [12, 13], "output": 11},
    ]

    def calculate_outputs(self, p):
        return {
            2: p[0] ^ p[1],
            5: p[3] ^ p[4],
            7: p[8] ^ p[9],
            10: p[11] ^ p[12]
        }


# Registry used by generate_ic_tables.py and pc_tester.py -- index order is
# the bring-up priority order and the SEL:<0-5> command's index.
IC_CLASSES = [
    IC74HCT00,
    IC74HCT02,
    IC74HCT04,
    IC74HCT08,
    IC74HCT32,
    IC74HCT86,
]
