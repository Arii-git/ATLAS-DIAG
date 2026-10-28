"""
Unit tests for digital_twin.py.

Run with:  python -m unittest test_digital_twin -v
"""
import unittest

from digital_twin import IC_CLASSES

# Boolean function each IC's gates implement, keyed by class name.
GATE_FUNCTIONS = {
    "74HCT00": lambda bits: 1 - (bits[0] & bits[1]) if len(bits) == 2 else None,  # NAND
    "74HCT02": lambda bits: 1 - (bits[0] | bits[1]) if len(bits) == 2 else None,  # NOR
    "74HCT04": lambda bits: 1 - bits[0] if len(bits) == 1 else None,              # NOT
    "74HCT08": lambda bits: (bits[0] & bits[1]) if len(bits) == 2 else None,      # AND
    "74HCT32": lambda bits: (bits[0] | bits[1]) if len(bits) == 2 else None,      # OR
    "74HCT86": lambda bits: (bits[0] ^ bits[1]) if len(bits) == 2 else None,      # XOR
}


class TestTruthTables(unittest.TestCase):
    """calculate_outputs() must match the datasheet truth table per gate."""

    def test_all_ics(self):
        for cls in IC_CLASSES:
            with self.subTest(ic=cls.name):
                self._check_ic(cls)

    def _check_ic(self, cls):
        ic = cls()
        gate_fn = GATE_FUNCTIONS[cls.name]
        num_inputs = len(ic.input_pins)

        # Global all-0 / all-1 vectors, checked gate-by-gate.
        for global_bits in ([0] * num_inputs, [1] * num_inputs):
            out = ic.simulate(global_bits)
            for gate in cls._GATES:
                in_bits = [global_bits[0]] * len(gate["inputs"])
                out_idx = ic.output_pins.index(gate["output"] - 1)
                self.assertEqual(
                    out[out_idx], gate_fn(in_bits),
                    f"{cls.name} gate {gate['num']} wrong for global "
                    f"{'all-1' if global_bits[0] else 'all-0'} vector"
                )

        # Per-gate spot checks: every input combination for that gate, other
        # inputs held low (gates are independent combinational blocks).
        for gate in cls._GATES:
            in_idxs = [ic.input_pins.index(p - 1) for p in gate["inputs"]]
            out_idx = ic.output_pins.index(gate["output"] - 1)
            n = len(gate["inputs"])
            for combo in range(2 ** n):
                bits = [(combo >> k) & 1 for k in range(n)]
                vector = [0] * num_inputs
                for idx, b in zip(in_idxs, bits):
                    vector[idx] = b
                out = ic.simulate(vector)
                expected = gate_fn(bits)
                self.assertEqual(
                    out[out_idx], expected,
                    f"{cls.name} gate {gate['num']} wrong for inputs {bits}"
                )


class TestCompaction(unittest.TestCase):
    """compacted_test_vectors() must be smaller (or equal) and lossless."""

    def test_all_ics(self):
        for cls in IC_CLASSES:
            with self.subTest(ic=cls.name):
                self._check_ic(cls)

    def _check_ic(self, cls):
        ic = cls()

        exhaustive = ic.exhaustive_test_vectors()
        compacted = ic.compacted_test_vectors()

        self.assertTrue(len(compacted) > 0, f"{cls.name}: compacted set is empty")
        self.assertLessEqual(
            len(compacted), len(exhaustive),
            f"{cls.name}: compacted set is not smaller than exhaustive"
        )

        # Compaction must not lose diagnostic resolution: any two faults
        # distinguishable by the exhaustive vector set must still be
        # distinguishable by the compacted vector set.
        _, exhaustive_fdict = ic.generate_fault_dictionary()
        exh_sig = {}
        for sig, names in exhaustive_fdict.items():
            for n in names:
                exh_sig[n] = sig

        compacted_fdict = ic.fault_dictionary()
        comp_sig = {}
        for sig, names in compacted_fdict.items():
            for n in names:
                comp_sig[n] = sig

        all_faults = ic.all_fault_names()
        for i in range(len(all_faults)):
            for j in range(i + 1, len(all_faults)):
                fa, fb = all_faults[i], all_faults[j]
                if exh_sig[fa] != exh_sig[fb]:
                    self.assertNotEqual(
                        comp_sig[fa], comp_sig[fb],
                        f"{cls.name}: compaction lost resolution between {fa} and {fb}"
                    )


class TestConfigAndGateMap(unittest.TestCase):
    def test_config_string_length(self):
        for cls in IC_CLASSES:
            ic = cls()
            self.assertEqual(len(ic.config_string()), 14)

    def test_gate_map_covers_every_fault(self):
        for cls in IC_CLASSES:
            with self.subTest(ic=cls.name):
                ic = cls()
                gmap = ic.gate_map()
                for fault_name in ic.all_fault_names():
                    self.assertIn(fault_name, gmap, f"{cls.name}: {fault_name} missing from gate_map()")
                    self.assertLessEqual(
                        len(gmap[fault_name]), 16,
                        f"{cls.name}: display string for {fault_name} exceeds 16 chars"
                    )


if __name__ == "__main__":
    unittest.main()
