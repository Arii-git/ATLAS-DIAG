"""
ATLAS-DIAG Digital Twin Dashboard (Streamlit)

    pip install streamlit plotly pandas pyserial
    streamlit run dashboard.py

Tabs:
  Live test      -- run quick / full / identify on the real chip over COM, twin-vs-hardware
                    heatmap, animated chip view, automatic hints, fault ranking
  Fault sandbox  -- inject SA0/SA1 on any pin in the twin, toggle inputs, see what the
                    tester would report
  Compaction     -- why 256 vectors shrink to 3: greedy set-cover steps, fault matrix, aliasing
  History        -- every results/*.json run (pc_tester.py + dashboard), charts, CSV export

Works offline (twin only) when no board is connected. Close the Arduino Serial
Monitor / pc_tester.py first -- only one program can hold the COM port.
"""
import time

import pandas as pd
import plotly.graph_objects as go
import streamlit as st

import atlas_core as core

st.set_page_config(page_title="ATLAS-DIAG Digital Twin", layout="wide")

GREEN, RED, GREY, BLUE, AMBER = "#16a34a", "#dc2626", "#d1d5db", "#3b82f6", "#f59e0b"
PLOT_CFG = {"displaylogo": False,
            "toImageButtonOptions": {"format": "png", "scale": 3}}  # camera icon = paper-ready PNG

st.markdown("""
<style>
.block-container {padding-top: 3rem;}
div[data-testid="stMetricValue"] {font-size: 1.6rem;}
.atlas-card {border:1px solid #e5e7eb;border-radius:12px;padding:14px 16px;background:#fafafa;}
.atlas-legend span {display:inline-block;margin-right:14px;font-size:0.85rem;}
.atlas-legend i {display:inline-block;width:12px;height:12px;border-radius:3px;margin-right:5px;vertical-align:-1px;}
</style>""", unsafe_allow_html=True)

ss = st.session_state
ss.setdefault("board", None)
ss.setdefault("runs", {})        # ic -> last live run
ss.setdefault("identify", None)
ss.setdefault("session", {})     # ic -> result of the All-IC session


# ============================================================ helpers
def show_svg(svg, target=None):
    (target or st).markdown(f'<div style="overflow-x:auto">{svg}</div>', unsafe_allow_html=True)


def legend():
    st.markdown(
        '<div class="atlas-legend">'
        f'<span><i style="background:{BLUE}"></i>DUT input (board drives)</span>'
        f'<span><i style="background:{AMBER}"></i>DUT output (board reads)</span>'
        '<span><i style="background:#ef4444"></i>VCC</span><span><i style="background:#6b7280"></i>GND</span>'
        '<span>bright = HIGH &nbsp; faded = LOW &nbsp; red outline = wrong reading</span></div>',
        unsafe_allow_html=True)


def grid_shape(n_inputs):
    """Vector index -> heatmap cell: rows = high bits, cols = low bits."""
    lo = n_inputs // 2
    return 2 ** (n_inputs - lo), 2 ** lo, lo


def heatmap(ic, hw, title, compare_to="Good"):
    """Green = matches, red = differs, grey = not run. Hover shows the numbers."""
    t = core.twin(ic)
    n = len(t.input_pins)
    rows, cols, lo = grid_shape(n)
    ref = core.response_row(ic, compare_to)
    z = [[None] * cols for _ in range(rows)]
    text = [[""] * cols for _ in range(rows)]
    for i, v in enumerate(t.exhaustive_test_vectors()):
        r, c = i >> lo, i & ((1 << lo) - 1)
        if v in hw and hw[v] is not None:
            ok = hw[v] == ref[v]
            z[r][c] = 1 if ok else 0
            text[r][c] = f"V:{v}<br>expected R:{ref[v]}<br>got R:{hw[v]}<br>{'OK' if ok else 'MISMATCH'}"
        else:
            z[r][c] = 0.5
            text[r][c] = f"V:{v}<br>expected R:{ref[v]}<br>(not run)"
    fig = go.Figure(go.Heatmap(
        z=z, text=text, hoverinfo="text", showscale=False, xgap=2, ygap=2, zmin=0, zmax=1,
        colorscale=[[0, RED], [0.49, RED], [0.5, GREY], [0.51, GREEN], [1, GREEN]]))
    hi_bits = n - lo
    fig.update_layout(
        title=title, height=420, margin=dict(l=10, r=10, t=40, b=10),
        xaxis=dict(title=f"last {lo} inputs", tickmode="array", tickvals=list(range(cols)),
                   ticktext=[format(c, f"0{lo}b") for c in range(cols)], side="top"),
        yaxis=dict(title=f"first {hi_bits} inputs", tickmode="array", tickvals=list(range(rows)),
                   ticktext=[format(r, f"0{hi_bits}b") for r in range(rows)], autorange="reversed"))
    return fig


def oled_for(ic, verdict, detail):
    return core.oled_svg(f"IC:{ic}", verdict, detail)


def show_hints(items):
    for level, title, text in items:
        getattr(st, level)(f"**{title}** - {text}")


def signature_of(ic, hw):
    vecs = core.twin(ic).compacted_test_vectors()
    if all(v in hw and hw[v] is not None for v in vecs):
        return "".join(hw[v] for v in vecs)
    return None


def diagnose(ic, hw):
    """Same decision logic as the firmware's standalone mode."""
    cmp = core.compare(ic, hw)
    sig = signature_of(ic, hw)
    names = core.twin(ic).fault_dictionary().get(sig, []) if sig else []
    gmap = core.twin(ic).gate_map()
    if cmp["mismatches"] == 0 and cmp["total"] > 0:
        full = cmp["total"] == 2 ** len(core.twin(ic).input_pins)
        return "PASS", (f"{cmp['total']}/{cmp['total']} OK" if full else "quick test OK"), sig, names, cmp
    if names and names != ["Good"]:
        return "FAULT", gmap[names[0]], sig, names, cmp
    return "FAULT", f"Bad {cmp['mismatches']}/{cmp['total']}", sig, names, cmp


# ============================================================ sidebar
with st.sidebar:
    st.title("ATLAS-DIAG")
    st.caption("Digital twin + hardware tester  \nNUCLEO-F446RE · 74HCT family")
    ic = st.selectbox("IC under test", core.IC_NAMES,
                      format_func=lambda n: f"{n} - {core.IC_DESC[n]}")
    st.divider()
    st.subheader("Board")
    ports = core.list_ports()
    SIM = "Simulated board (demo)"
    port = st.selectbox("COM port", ports + ["(type manually)", SIM], index=0)
    if port == "(type manually)":
        port = st.text_input("Port name", "COM6")
    if port == SIM:
        sim_chip = st.selectbox("Simulated chip in socket", core.IC_NAMES, key="sim_chip")
        st_sim = core.twin(sim_chip)
        sim_fault = st.selectbox("Simulated fault", st_sim.all_fault_names() + list(core.SIM_CONDITIONS),
                                 key="sim_fault",
                                 format_func=lambda f: "none (good chip)" if f == "Good" else
                                 core.SIM_CONDITIONS.get(f, f"{f} - {core.fault_label(sim_chip, f)}"))
        st.caption("The simulated chip/fault is applied when you click Connect "
                   "(Disconnect + Connect to change it).")
    c1, c2 = st.columns(2)
    if c1.button("Connect", width="stretch", disabled=ss.board is not None):
        try:
            b = core.SimBoard(sim_chip, sim_fault) if port == SIM else core.Board(port)
            if b.ping():
                ss.board = b
                st.success(f"Connected on {port}")
            else:
                b.close()
                st.error("No PONG - is the ATLAS firmware running? Close Serial Monitor / pc_tester.")
        except Exception as e:
            st.error(f"Cannot open {port}: {e}")
    if c2.button("Disconnect", width="stretch", disabled=ss.board is None):
        ss.board.close()
        ss.board = None
    if ss.board:
        st.markdown(f":green[**● Online**] on `{ss.board.port}`")
    else:
        st.markdown(":gray[**○ Offline**] - twin-only features still work")
    st.divider()
    st.caption("Rule: unplug USB before swapping chips.")

tab_live, tab_sess, tab_sand, tab_comp, tab_hist = st.tabs(
    ["Live test", "All-IC session", "Fault sandbox", "Compaction", "History"])


# ============================================================ LIVE TEST
with tab_live:
    t = core.twin(ic)
    n_in = len(t.input_pins)
    n_ex = 2 ** n_in
    st.subheader(f"Live test - {ic}")

    a, b, c, d = st.columns([1.2, 1.2, 1.2, 1])
    repeat = d.selectbox("Repeat full test", [1, 2, 3], help="Run the full test several times to catch loose contacts")
    run_quick = a.button(f"Quick test ({len(t.compacted_test_vectors())} vectors)", type="secondary",
                         width="stretch", disabled=ss.board is None)
    run_full = b.button(f"Full verify ({n_ex} vectors)", type="primary",
                        width="stretch", disabled=ss.board is None)
    run_ident = c.button("Identify chip", width="stretch", disabled=ss.board is None,
                         help="Tries every pin-out and reports which 74HCT chip is actually inserted")
    if ss.board is None:
        st.info("Connect the board in the sidebar to run tests. The Fault sandbox and Compaction tabs work offline.")

    def do_run(vectors, repeats=1):
        board = ss.board
        bar = st.progress(0.0, text="Configuring board...")
        board.select(ic)
        first, diffs = None, 0
        t0 = time.time()
        for r in range(repeats):
            res = core.run_vectors(board, vectors, lambda i, n: bar.progress(
                (r + i / n) / repeats, text=f"Run {r + 1}/{repeats}: vector {i}/{n}"))
            if first is None:
                first = res
            else:
                diffs += sum(1 for v in vectors if res[v] != first[v])
        bar.empty()
        return first, diffs, time.time() - t0

    try:
        if run_quick or run_full:
            vecs = t.compacted_test_vectors() if run_quick else t.exhaustive_test_vectors()
            hw, rdiff, dt = do_run(vecs, 1 if run_quick else repeat)
            verdict, detail, sig, names, cmp = diagnose(ic, hw)
            if not ss.board.send_result("RES:PASS" if verdict == "PASS" else f"RES:FAULT:{detail}"):
                st.warning("Board did not confirm the result display (RES_OK) - flash the latest firmware "
                           "to see results on the OLED/LEDs.")
            rec = {"ic": ic, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"), "source": "dashboard",
                   "mode": "quick" if run_quick else "full", "verdict": verdict, "detail": detail,
                   "signature": sig, "candidates": names, "repeat_diffs": rdiff, "seconds": round(dt, 3),
                   "full": {"total": cmp["total"], "mismatches": cmp["mismatches"]},
                   "per_vector": cmp["rows"]}
            core.save_run(rec)
            ss.runs[ic] = {"hw": hw, "rdiff": rdiff, "dt": dt, "mode": rec["mode"],
                           "repeat": 1 if run_quick else repeat}
        if run_ident:
            results = []
            done_cfg = {}
            bar = st.progress(0.0, text="Identifying...")
            for k, cand in enumerate(core.IC_NAMES):
                cfg = core.twin(cand).config_string()
                if cfg not in done_cfg:
                    ss.board.select(cand)
                    done_cfg[cfg] = core.run_vectors(ss.board, core.twin(cand).exhaustive_test_vectors())
                hw = done_cfg[cfg]
                good = core.response_row(cand)
                agree = sum(1 for v in hw if hw[v] == good[v])
                results.append({"IC": cand, "match %": round(100 * agree / len(hw), 1),
                                "matching vectors": f"{agree}/{len(hw)}"})
                bar.progress((k + 1) / len(core.IC_NAMES), text=f"Tried {cand}")
            bar.empty()
            ss.identify = sorted(results, key=lambda r: -r["match %"])
            core.save_run({"ic": ss.identify[0]["IC"], "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                           "source": "dashboard", "mode": "identify",
                           "verdict": "IDENTIFIED" if ss.identify[0]["match %"] == 100 else "NO MATCH",
                           "detail": ", ".join(f"{r['IC']} {r['match %']}%" for r in ss.identify),
                           "identify": ss.identify})
    except Exception as e:
        st.error(f"Board error: {e}")

    if ss.identify:
        best = ss.identify[0]
        with st.container(border=True):
            if best["match %"] == 100:
                st.success(f"**Detected chip: {best['IC']}** ({core.IC_DESC[best['IC']]}) - matches all vectors.")
            else:
                st.warning(f"No exact match. Closest: {best['IC']} at {best['match %']} %. "
                           "Could be a faulty chip, a wiring problem, or an unsupported part.")
            fig = go.Figure(go.Bar(x=[r["IC"] for r in ss.identify], y=[r["match %"] for r in ss.identify],
                                   marker_color=[GREEN if r["match %"] == 100 else BLUE for r in ss.identify],
                                   text=[r["matching vectors"] for r in ss.identify], textposition="outside"))
            fig.update_layout(height=300, yaxis=dict(range=[0, 110], title="match %"),
                              margin=dict(l=10, r=10, t=20, b=10))
            st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
            st.caption("Driving another chip's pin-out is safe here: every DUT line has a 1 kΩ series resistor (≤ 5 mA).")

    run = ss.runs.get(ic)
    if run:
        hw = run["hw"]
        verdict, detail, sig, names, cmp = diagnose(ic, hw)
        m1, m2, m3, m4, m5 = st.columns(5)
        m1.metric("Verdict", verdict)
        m2.metric("Vectors run", cmp["total"])
        m3.metric("Mismatches", cmp["mismatches"])
        m4.metric("Signature", sig or "-")
        m5.metric("Time", f"{run['dt']:.2f} s")
        if len(names) > 1 and names != ["Good"]:
            st.caption("**Aliased signature** - these faults look identical from the pins, the tester shows the "
                       "first and the board OLED cycles through all: "
                       + " · ".join(f"{n} ({core.fault_label(ic, n)})" for n in names))
        if run.get("mode") == "full" and run["rdiff"] == 0 and run.get("repeat", 1) > 1:
            st.caption(f"Repeated runs were identical - the connections look stable.")

        left, right = st.columns([1.35, 1])
        with left:
            vec_list = list(hw.keys())
            bad = [r["vector"] for r in cmp["rows"] if not r["match"]]
            default = vec_list.index(bad[0]) if bad else 0
            idx = st.select_slider("Inspect vector", options=list(range(len(vec_list))), value=default,
                                   format_func=lambda i: vec_list[i])
            chip_slot = st.empty()

            def draw(i):
                v = vec_list[i]
                exp = core.response_row(ic)[v]
                got = hw[v] or ""
                bad_pins = [p + 1 for k, p in enumerate(t.output_pins) if k < len(got) and got[k] != exp[k]]
                show_svg(core.chip_svg(ic, core.pin_levels(ic, v, got), bad_pins=bad_pins), chip_slot)
                return v, exp, got

            v, exp, got = draw(idx)
            legend()
            st.markdown(f"`V:{v}` → expected `R:{exp}` · got `R:{got}` "
                        + (":green[**OK**]" if got == exp else ":red[**MISMATCH**]"))
            if st.button("▶ Animate all vectors"):
                step = max(1, len(vec_list) // 64)
                for i in range(0, len(vec_list), step):
                    draw(i)
                    time.sleep(0.06)
                draw(idx)
        with right:
            show_svg(oled_for(ic, verdict, detail))
            st.caption("What the board's OLED shows for this result")
            st.markdown("##### Automatic hints")
            show_hints(core.hints(ic, hw, cmp, run["rdiff"]))

        st.plotly_chart(heatmap(ic, hw, "Twin vs hardware - every input combination"),
                        width="stretch", config=PLOT_CFG)

        c_left, c_right = st.columns(2)
        with c_left:
            st.markdown("##### Errors per output pin")
            per = core.per_output_errors(ic, cmp)
            labels = [f"DIP {p['dip']} ({p['nucleo']})" for p in per]
            fig = go.Figure([
                go.Bar(name="reads HIGH, should be LOW", x=labels, y=[p["read_high_should_low"] for p in per],
                       marker_color=RED),
                go.Bar(name="reads LOW, should be HIGH", x=labels, y=[p["read_low_should_high"] for p in per],
                       marker_color=BLUE)])
            fig.update_layout(barmode="stack", height=300, margin=dict(l=10, r=10, t=10, b=10),
                              legend=dict(orientation="h", y=-0.25))
            st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
        with c_right:
            st.markdown("##### Closest single faults")
            rank = core.rank_faults(ic, hw)
            st.dataframe(pd.DataFrame([{"fault": r["fault"], "meaning": r["label"],
                                        "agreement": r["pct"] / 100} for r in rank]),
                         column_config={"agreement": st.column_config.ProgressColumn(
                             "agreement", format="percent", min_value=0, max_value=1)},
                         hide_index=True, width="stretch")
            st.caption("How well the chip's behaviour matches each single stuck-at fault model. "
                       "100 % = exactly that fault.")
        with st.expander("All vectors (table)"):
            st.dataframe(pd.DataFrame(cmp["rows"]), hide_index=True, width="stretch")


# ============================================================ ALL-IC SESSION
with tab_sess:
    st.subheader("All-IC session - test every chip, get one report")
    st.markdown("Insert each chip in turn (**unplug USB before every swap**), then press its **Test** button. "
                "Each test runs the full exhaustive check and also checks the chip really is that IC.")
    if ss.board is not None and isinstance(ss.board, core.SimBoard):
        ss.board.auto_swap = st.toggle("Simulated board: pretend I inserted the matching chip for each test",
                                       value=True, help="Demo helper - with this off, the simulated chip "
                                       "chosen in the sidebar stays in the socket for every test.")
    done = sum(1 for n in core.IC_NAMES if n in ss.session)
    st.progress(done / len(core.IC_NAMES), text=f"{done}/{len(core.IC_NAMES)} ICs tested")

    for name in core.IC_NAMES:
        tw = core.twin(name)
        res = ss.session.get(name)
        c1, c2, c3, c4, c5 = st.columns([1.1, 1.6, 1.1, 2.4, 1])
        c1.markdown(f"**{name}**")
        c2.markdown(f"{core.IC_DESC[name]}  \n`{len(tw.exhaustive_test_vectors())}` vectors · "
                    f"quick `{len(tw.compacted_test_vectors())}`")
        if res is None:
            c3.markdown(":gray[not tested]")
        elif res["verdict"] == "PASS":
            c3.markdown(":green[**PASS**]")
        else:
            c3.markdown(":red[**FAULT**]")
        c4.markdown(res["detail"] if res else "")
        if c5.button("Test", key=f"sess_{name}", disabled=ss.board is None, width="stretch"):
            try:
                with st.spinner(f"Testing {name}..."):
                    t0 = time.time()
                    sim = isinstance(ss.board, core.SimBoard)
                    if sim and ss.board.auto_swap:
                        ss.board.insert(name)          # pretend the matching chip was inserted
                    ss.board.select(name)
                    hw = core.run_vectors(ss.board, tw.exhaustive_test_vectors())
                    verdict, detail, sig, names, cmp = diagnose(name, hw)
                    other = [o for o in core.other_ic_matches(name, hw) if o["agree"] == len(hw)]
                    if other:
                        detail = f"Wrong chip - behaves like {other[0]['ic']}"
                    dt = time.time() - t0
                    ss.board.send_result("RES:PASS" if verdict == "PASS" else f"RES:FAULT:{detail[:40]}")
                    ss.session[name] = {"verdict": verdict, "detail": detail, "mismatches": cmp["mismatches"],
                                        "total": cmp["total"], "signature": sig, "seconds": round(dt, 2),
                                        "time": time.strftime("%H:%M:%S")}
                    core.save_run({"ic": name, "timestamp": time.strftime("%Y-%m-%dT%H:%M:%S"),
                                   "source": "dashboard", "mode": "session", "verdict": verdict,
                                   "detail": detail, "signature": sig, "seconds": round(dt, 3),
                                   "full": {"total": cmp["total"], "mismatches": cmp["mismatches"]},
                                   "per_vector": cmp["rows"]})
                    ss.runs[name] = {"hw": hw, "rdiff": 0, "dt": dt, "mode": "full", "repeat": 1}
                    if sim:
                        ss.board.insert(None)          # put the sidebar's chip back
                st.rerun()
            except Exception as e:
                st.error(f"Board error while testing {name}: {e}")

    if ss.session:
        st.divider()
        st.markdown("##### Session report")
        rows = []
        for name in core.IC_NAMES:
            tw = core.twin(name)
            e, cpt, al = tw.compaction_stats()
            r = ss.session.get(name, {})
            rows.append({"IC": name, "function": core.IC_DESC[name], "exhaustive vectors": e,
                         "compacted vectors": cpt, "result": r.get("verdict", "not tested"),
                         "mismatches": f"{r['mismatches']}/{r['total']}" if r else "",
                         "detail": r.get("detail", ""), "test time (s)": r.get("seconds"),
                         "tested at": r.get("time", "")})
        rep_df = pd.DataFrame(rows)
        st.dataframe(rep_df, hide_index=True, width="stretch")
        passed = sum(1 for r in ss.session.values() if r["verdict"] == "PASS")
        md = [f"# ATLAS-DIAG test session - {time.strftime('%Y-%m-%d %H:%M')}", "",
              f"Board: NUCLEO-F446RE · {'simulated' if isinstance(ss.board, core.SimBoard) else 'hardware'} · "
              f"{len(ss.session)}/6 ICs tested · {passed} PASS", "",
              "| " + " | ".join(rep_df.columns) + " |", "|" + "---|" * len(rep_df.columns)]
        md += ["| " + " | ".join(str(v) for v in row) + " |" for row in rep_df.values.tolist()]
        d1, d2, d3 = st.columns(3)
        d1.download_button("Download report (CSV)", rep_df.to_csv(index=False), "atlas_session.csv", "text/csv")
        d2.download_button("Download report (Markdown)", "\n".join(md) + "\n", "atlas_session.md", "text/markdown")
        if d3.button("Clear session"):
            ss.session = {}
            st.rerun()


# ============================================================ SANDBOX
with tab_sand:
    st.subheader("Fault sandbox - what would the tester see?")
    s_ic = st.selectbox("IC", core.IC_NAMES, index=core.IC_NAMES.index(ic), key="sand_ic",
                        format_func=lambda n: f"{n} - {core.IC_DESC[n]}")
    st_ = core.twin(s_ic)
    io_pins = sorted(st_.input_pins + st_.output_pins)
    c1, c2 = st.columns([1, 1])
    fault_pin = c1.selectbox("Inject fault on DIP pin", ["none"] + [p + 1 for p in io_pins],
                             format_func=lambda p: "No fault (good chip)" if p == "none" else
                             f"Pin {p} ({'input' if p - 1 in st_.input_pins else 'output'})")
    fault_type = c2.radio("Fault type", ["SA0", "SA1"], horizontal=True, disabled=fault_pin == "none",
                          help="SA0 = stuck at 0 (LOW), SA1 = stuck at 1 (HIGH)")
    fault = "Good" if fault_pin == "none" else f"Pin{fault_pin}_{fault_type}"

    st.markdown("**Set the inputs** (click to toggle)")
    cols = st.columns(len(st_.input_pins))
    bits = []
    for k, p in enumerate(st_.input_pins):
        bits.append(1 if cols[k].toggle(f"pin {p + 1}", key=f"in_{s_ic}_{p}") else 0)
    vec = "".join(map(str, bits))
    good_out = core.response_row(s_ic)[vec]
    faulty_out = core.response_row(s_ic, fault)[vec]

    g, f_ = st.columns(2)
    with g:
        st.markdown("**Good chip (twin)**")
        show_svg(core.chip_svg(s_ic, core.pin_levels(s_ic, vec, good_out), width=460))
    with f_:
        st.markdown(f"**With fault: {core.fault_label(s_ic, fault) if fault != 'Good' else 'none'}**")
        diff_pins = [p + 1 for k, p in enumerate(st_.output_pins) if good_out[k] != faulty_out[k]]
        show_svg(core.chip_svg(s_ic, core.pin_levels(s_ic, vec, faulty_out), bad_pins=diff_pins,
                               fault_pin=None if fault == "Good" else fault_pin,
                               fault_type=fault_type, width=460))
    legend()
    st.markdown(f"Inputs `{vec}` → good `R:{good_out}` · faulty `R:{faulty_out}` "
                + (":green[same]" if good_out == faulty_out else ":red[**different - this vector detects it**]"))

    fdict = st_.fault_dictionary()
    sig = "".join(core.response_row(s_ic, fault)[v] for v in st_.compacted_test_vectors())
    group = fdict.get(sig, [])
    detect = sum(1 for v in st_.exhaustive_test_vectors()
                 if core.response_row(s_ic, fault)[v] != core.response_row(s_ic)[v])
    m1, m2, m3 = st.columns(3)
    m1.metric("Predicted signature", sig)
    m2.metric("Vectors that expose it", f"{detect}/{2 ** len(st_.input_pins)}")
    m3.metric("Look-alike faults", len(group) - 1 if fault != "Good" else 0)
    oc, info = st.columns([1, 1.6])
    with oc:
        show_svg(core.oled_svg(f"IC:{s_ic}", "PASS" if fault == "Good" else "FAULT",
                               core.fault_label(s_ic, group[0]) if group and fault != "Good" else
                               f"{2 ** len(st_.input_pins)}/{2 ** len(st_.input_pins)} OK"))
        st.caption("What the standalone tester would display")
    with info:
        if fault != "Good" and len(group) > 1:
            st.warning("**Aliased fault.** These faults produce the *same* signature, so no external test can "
                       "tell them apart - the OLED cycles through them:\n\n" +
                       "\n".join(f"- {n}: {core.fault_label(s_ic, n)}" for n in group))
        elif fault != "Good":
            st.success("This fault has a **unique** signature - the tester names it exactly.")
        else:
            st.info("Pick a pin and a fault type above to see how the tester would react.")
    st.plotly_chart(heatmap(s_ic, core.response_row(s_ic, fault),
                            "Where this fault is visible (red = output differs from a good chip)"),
                    width="stretch", config=PLOT_CFG)


# ============================================================ COMPACTION
with tab_comp:
    st.subheader("Test compaction - why a few vectors are enough")
    c_ic = st.selectbox("IC", core.IC_NAMES, index=core.IC_NAMES.index(ic), key="comp_ic",
                        format_func=lambda n: f"{n} - {core.IC_DESC[n]}")
    tr = core.greedy_trace(c_ic)
    ct = core.twin(c_ic)
    n_ex = len(ct.exhaustive_test_vectors())
    n_cp = len(tr["steps"])
    m1, m2, m3, m4 = st.columns(4)
    m1.metric("Exhaustive vectors", n_ex)
    m2.metric("Compacted vectors", n_cp, delta=f"-{100 * (1 - n_cp / n_ex):.1f} %", delta_color="inverse")
    m3.metric("Fault pairs to separate", tr["total_pairs"])
    m4.metric("Aliased groups", len(tr["aliased_groups"]))
    st.markdown(
        f"Every single stuck-at fault on every pin (**{len(tr['faults']) - 1} faults** + the good chip) must give a "
        "different output signature. The greedy algorithm repeatedly picks the vector that separates the most "
        "still-confused pairs of faults.")

    left, right = st.columns([1, 1])
    with left:
        steps = tr["steps"]
        fig = go.Figure()
        fig.add_bar(x=[f"#{s['step']} V:{s['vector']}" for s in steps], y=[s["resolved"] for s in steps],
                    name="pairs separated", marker_color=GREEN)
        fig.add_scatter(x=[f"#{s['step']} V:{s['vector']}" for s in steps], y=[s["remaining"] for s in steps],
                        name="still confused", mode="lines+markers", line=dict(color=RED, width=3))
        fig.update_layout(height=360, title="Greedy set-cover, step by step",
                          margin=dict(l=10, r=10, t=40, b=10), legend=dict(orientation="h", y=-0.2))
        st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
        st.dataframe(pd.DataFrame(steps), hide_index=True, width="stretch")
        if tr["unresolvable"]:
            st.caption(f"{tr['unresolvable']} pairs can never be separated by any vector - those are the aliased faults.")
    with right:
        faults = tr["faults"]
        m = ct._response_matrix()
        vecs = [s["vector"] for s in steps]
        n_out = len(ct.output_pins)
        z, xlab = [], []
        for v in vecs:
            for k, p in enumerate(ct.output_pins):
                xlab.append(f"V{vecs.index(v) + 1}·pin{p + 1}")
        for fname in faults:
            row = []
            for v in vecs:
                row += [int(b_) for b_ in m[fname][v]]
            z.append(row)
        fig = go.Figure(go.Heatmap(z=z, x=xlab, y=[f"{f} ({core.fault_label(c_ic, f)})" for f in faults],
                                   colorscale=[[0, "#1e293b"], [1, "#facc15"]], showscale=False,
                                   xgap=1, ygap=1, hovertemplate="%{y}<br>%{x} = %{z}<extra></extra>"))
        fig.update_layout(height=max(360, 22 * len(faults)), title="Fault signatures (yellow = 1)",
                          margin=dict(l=10, r=10, t=40, b=10), yaxis=dict(autorange="reversed"))
        st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
    if tr["aliased_groups"]:
        with st.expander(f"Aliased fault groups ({len(tr['aliased_groups'])})", expanded=True):
            for gi, grp in enumerate(tr["aliased_groups"], 1):
                st.markdown(f"**Group {gi}:** " + " · ".join(f"{n} ({core.fault_label(c_ic, n)})" for n in grp))
            st.caption("An input stuck at the gate's controlling value looks exactly like its output stuck - "
                       "physically indistinguishable from the pins, so every tester has this limit.")

    st.markdown("##### All ICs (paper table)")
    rows = []
    for name in core.IC_NAMES:
        e, cpt, al = core.twin(name).compaction_stats()
        rows.append({"IC": name, "function": core.IC_DESC[name], "exhaustive": e, "compacted": cpt,
                     "reduction %": round(100 * (1 - cpt / e), 1), "aliased groups": al})
    df = pd.DataFrame(rows)
    st.dataframe(df, hide_index=True, width="stretch")
    st.download_button("Download table (CSV)", df.to_csv(index=False), "compaction_stats.csv", "text/csv")


# ============================================================ HISTORY
with tab_hist:
    st.subheader("Test history")
    runs = core.load_history()
    if not runs:
        st.info("No results yet - run a test (here or with pc_tester.py) and it will appear.")
    else:
        df = pd.DataFrame(runs)
        f1, f2 = st.columns(2)
        pick_ic = f1.multiselect("IC", sorted(df["ic"].unique()), default=sorted(df["ic"].unique()))
        pick_v = f2.multiselect("Verdict", sorted(df["verdict"].unique()), default=sorted(df["verdict"].unique()))
        df = df[df["ic"].isin(pick_ic) & df["verdict"].isin(pick_v)]
        m1, m2, m3 = st.columns(3)
        m1.metric("Runs", len(df))
        m2.metric("PASS", int((df["verdict"] == "PASS").sum()))
        m3.metric("FAULT / other", int((df["verdict"] != "PASS").sum()))
        c1, c2 = st.columns(2)
        with c1:
            counts = df.groupby(["ic", "verdict"]).size().reset_index(name="runs")
            fig = go.Figure()
            for vd, colr in (("PASS", GREEN), ("FAULT", RED), ("UNKNOWN", GREY)):
                sub = counts[counts["verdict"] == vd]
                if len(sub):
                    fig.add_bar(x=sub["ic"], y=sub["runs"], name=vd, marker_color=colr)
            fig.update_layout(barmode="stack", height=320, title="Runs per IC",
                              margin=dict(l=10, r=10, t=40, b=10))
            st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
        with c2:
            full = df.dropna(subset=["full_bad"])
            full = full[(full["full_total"] >= 16) & (full["mode"] != "identify")]
            if len(full):
                fig = go.Figure(go.Scatter(x=full["timestamp"], y=full["full_bad"], mode="lines+markers",
                                           text=full["ic"], line=dict(color=BLUE)))
                fig.update_layout(height=320, title="Mismatching vectors per full run (0 = perfect)",
                                  margin=dict(l=10, r=10, t=40, b=10))
                st.plotly_chart(fig, width="stretch", config=PLOT_CFG)
        st.dataframe(df, hide_index=True, width="stretch")
        st.download_button("Download history (CSV)", df.to_csv(index=False), "atlas_history.csv", "text/csv")
