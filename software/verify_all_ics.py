"""End-to-end check for every IC and every single stuck-at fault:
SimBoard(chip, fault) -> full exhaustive run -> dashboard diagnosis logic.
Also independently checks each twin's truth table against a hand-written
datasheet reference (not derived from digital_twin.py)."""
import sys; import os; sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import itertools, atlas_core as core

REF = {  # datasheet functions per gate: (inputs DIP pins, output DIP pin, fn)
 "74HCT00": ([((1,2),3),((4,5),6),((9,10),8),((12,13),11)], lambda a,b: 1-(a&b)),
 "74HCT02": ([((2,3),1),((5,6),4),((8,9),10),((11,12),13)], lambda a,b: 1-(a|b)),
 "74HCT04": ([((1,),2),((3,),4),((5,),6),((9,),8),((11,),10),((13,),12)], lambda a: 1-a),
 "74HCT08": ([((1,2),3),((4,5),6),((9,10),8),((12,13),11)], lambda a,b: a&b),
 "74HCT32": ([((1,2),3),((4,5),6),((9,10),8),((12,13),11)], lambda a,b: a|b),
 "74HCT86": ([((1,2),3),((4,5),6),((9,10),8),((12,13),11)], lambda a,b: a^b),
}
problems = 0
for ic in core.IC_NAMES:
    t = core.twin(ic); gates, fn = REF[ic]
    for v in t.exhaustive_test_vectors():
        lv = {p+1: int(v[k]) for k, p in enumerate(t.input_pins)}
        exp = {o: fn(*[lv[i] for i in ins]) for ins, o in gates}
        want = "".join(str(exp[p+1]) for p in t.output_pins)
        if core.response_row(ic)[v] != want:
            problems += 1; print("TRUTH MISMATCH", ic, v); break
    # every fault -> diagnosis must name it (or an alias of it)
    fdict = t.fault_dictionary()
    ok = total = 0
    for fault in t.all_fault_names():
        b = core.SimBoard(ic, fault); b.select(ic)
        hw = core.run_vectors.__wrapped__(b, t.exhaustive_test_vectors()) if hasattr(core.run_vectors, "__wrapped__") else {v: b.apply(v) for v in t.exhaustive_test_vectors()}
        cmp = core.compare(ic, hw)
        sig = "".join(hw[v] for v in t.compacted_test_vectors())
        names = fdict.get(sig, [])
        rank = core.rank_faults(ic, hw, top=30)
        top = [r["fault"] for r in rank if r["agree"] == len(hw)]
        total += 1
        good = (fault in names) and (fault in top) and ((cmp["mismatches"] == 0) == (fault == "Good"))
        hints = core.hints(ic, hw, cmp)
        if good: ok += 1
        else: problems += 1; print("DIAG PROBLEM", ic, fault, names, top[:3], cmp["mismatches"])
    # wrong chip detection
    wrong = []
    for other in core.IC_NAMES:
        if other == ic: continue
        b = core.SimBoard(other); b.select(ic)
        hw = {v: b.apply(v) for v in t.exhaustive_test_vectors()}
        if core.compare(ic, hw)["mismatches"] == 0: wrong.append(other)
    print(f"{ic}: truth OK, {ok}/{total} faults diagnosed, false-pass with wrong chip: {wrong or 'none'}")
print("PROBLEMS:", problems)
