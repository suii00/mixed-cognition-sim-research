"""Synthetic evidence-chain regressions; no LLM calls or raw run mutations."""
import copy
import hashlib
import json
from pathlib import Path

import pytest

from tools import analyze_warning_retention_study as analysis
from tools.disaster_metric_v2_core import SourceRecord
from tools.render_warning_retention_study import render_html


def sources(rows, name):
    result = []
    for line, row in enumerate(rows, 1):
        raw = (json.dumps(row) + "\n").encode()
        result.append(SourceRecord(row, {"file": name, "line_number": line,
            "line_sha256": hashlib.sha256(raw).hexdigest(), "line_bytes": len(raw)}))
    return result


def fixture(policy="recent", context=2):
    meta = {"run_id": "synthetic-retention", "expected_steps": 4, "expected_agents": 3,
        "config": {"agents": {"message_history_limit": 3, "message_context_size": context,
            "message_selection_policy": analysis.POLICIES[policy]},
            "scenario": {"official_warning": {"warning_id": "warn-1", "issue_step": 2,
                "initial_recipient_ids": [0]}}}}
    warnings = [{"event_type": "warning_issued", "step": 2, "warning_id": "warn-1", "payload": "Official warn-1."},
        {"event_type": "warning_exposure", "source_type": "official", "step": 2, "recipient_id": 0, "warning_id": "warn-1"}]
    # Two same-step deliveries evict official selection; later deliveries evict raw history.
    messages = [{"step": s, "sender_id": a, "message": "peer " + str(a), "receiver_ids": [0, 3-a]}
                for s in range(2, 5) for a in (1, 2)]
    # Input file order is intentionally reversed; delivery chronology is ascending sender.
    rec = {"warning_events.jsonl": sources(warnings, "warning_events.jsonl"),
        "messages.jsonl": sources(list(reversed(messages)), "messages.jsonl")}
    return meta, rec


def add_observed(meta, rec):
    phases = analysis.replay_presentations(meta, rec, observed=False)
    rows = []
    for p in phases:
        # A memory ID tests any-prompt ID independently of selected official item.
        prompt = "Agent input\nMemory: warn-1\n" + analysis.format_messages_section(p["selected_messages"])
        rows.append({"step": p["step"], "phase": p["phase"], "agent_id": p["agent_id"],
            "message_selection_policy": meta["config"]["agents"]["message_selection_policy"],
            "messages": p["selected_messages"], "prompt": prompt,
            "prompt_sha256": hashlib.sha256(prompt.encode()).hexdigest()})
    rec["prompt_inputs.jsonl"] = sources(rows, "prompt_inputs.jsonl")
    return rows


def test_phase_barrier_recent_eviction_and_retention_beyond_history():
    ma, ra = fixture()
    mb, rb = fixture("retained")
    a = {(r["step"], r["phase"], r["agent_id"]): r for r in analysis.replay_presentations(ma, ra, observed=False)}
    b = {(r["step"], r["phase"], r["agent_id"]): r for r in analysis.replay_presentations(mb, rb, observed=False)}
    assert a[1, "phase1", 0]["selected_messages"] == []
    assert a[2, "phase1", 0]["official_selected"]
    assert not a[2, "phase3", 0]["official_selected"]
    assert [m["sender_id"] for m in a[2, "phase3", 0]["selected_messages"]] == [1, 2]
    assert b[4, "phase3", 0]["official_selected"]
    assert b[4, "phase3", 0]["selected_messages"][0]["step"] == 2
    assert b[4, "phase3", 0]["selected_messages"][1]["sender_id"] == 2
    for key in a:
        if key[2] in (1, 2):
            assert a[key] == b[key]
    assert all(r["exact_id_in_actual_prompt"] is None for r in a.values())
    assert a[2, "phase1", 0]["selected_source_references"][0][0]["file"] == "warning_events.jsonl"


def test_actual_prompt_presence_distinct_from_official_selection_and_immutability():
    meta, rec = fixture()
    add_observed(meta, rec)
    before = copy.deepcopy(rec)
    result = analysis.replay_presentations(meta, rec)
    assert rec == before
    row = next(r for r in result if (r["step"], r["phase"], r["agent_id"]) == (2, "phase3", 0))
    assert not row["official_selected"] and row["exact_id_in_actual_prompt"]
    assert row["evidence_class"] == "direct_request_input_observation"


@pytest.mark.parametrize("mutation", ["missing", "duplicate", "selection", "hash", "prompt_without_selected"])
def test_observed_stream_corruption_rejected(mutation):
    meta, rec = fixture()
    rows = add_observed(meta, rec)
    if mutation == "missing":
        rows.pop()
    elif mutation == "duplicate":
        rows.append(rows[-1])
    elif mutation == "selection":
        rows[0]["messages"] = [{"sender_id": 0, "message": "not delivered", "step": 1}]
    elif mutation == "hash":
        rows[0]["prompt_sha256"] = "0" * 64
    else:
        target = next(r for r in rows if r["messages"])
        target["prompt"] = "missing selected items"
        target["prompt_sha256"] = hashlib.sha256(target["prompt"].encode()).hexdigest()
    rec["prompt_inputs.jsonl"] = sources(rows, "prompt_inputs.jsonl")
    with pytest.raises(ValueError):
        analysis.replay_presentations(meta, rec)


def test_one_item_context_and_peer_official_text_do_not_create_direct_reception():
    meta, rec = fixture("retained", context=1)
    rec["messages.jsonl"][0].value["message"] = "Official warn-1."
    rows = analysis.replay_presentations(meta, rec, observed=False)
    assert all(r["selected_message_count"] <= 1 for r in rows)
    assert all(not r["official_selected"] and not r["official_received_before_phase"]
               for r in rows if r["agent_id"] in (1, 2))


def test_fixed_lexical_flags_are_not_semantic_reuse():
    assert analysis.lexical_flags("FLOODING near shelters", "warn-1") == {
        "exact_warning_id": False, "hazard_word": True, "refuge_word": True, "non_id_review_candidate": True}
    assert not analysis.lexical_flags("sheltered warningly", "warn-1")["non_id_review_candidate"]
    assert not analysis.lexical_flags("warn-1", "warn-1")["non_id_review_candidate"]
    assert not analysis.lexical_flags("warn-1-extra", "warn-1")["exact_warning_id"]


def test_full_metrics_distinguish_same_step_generation_delivery_and_later_own_output():
    from tests.test_refuge_layout_study_analysis import fixture as geometry_fixture
    meta, rec = geometry_fixture(duration=60)
    meta["config"]["agents"] = {"message_history_limit": 30, "message_context_size": 5,
        "message_selection_policy": analysis.POLICIES["recent"]}
    add_observed(meta, rec)
    calculated = analysis.calculate(meta, rec)
    primary = calculated["primary"]
    assert primary["noninitial_agent_count"] == 2
    assert primary["noninitial_exact_id_peer_recipient_count"] == 1
    assert primary["noninitial_exact_id_peer_delivery_edges"] == 1
    # Agent 1 emits at step 11 before its same-step reception and again at step 12.
    # Only the own step-12 message is later-step reuse.
    assert primary["noninitial_later_step_reused_agents"] == 1
    assert primary["noninitial_later_step_reuse_outputs"] == 1
    assert primary["post_issue_phase_denominator"] == 51
    assert primary["phase1_official_selected"] == 51
    assert len(calculated["agents"]) == 3
    assert all(a["first_arrival_step"] is None and a["arrival_censor_step"] == 60 for a in calculated["agents"])


def test_missing_pair_null_and_pair_mismatch():
    assert analysis.paired_comparisons([], [7301]) == [{"seed": 7301, "eligible": False, "retained_minus_recent": None}]
    a = {"seed": 7301, "condition": "recent", "calculated": {"geometry": {"initial_signature": [0]}}}
    b = {"seed": 7301, "condition": "retained", "calculated": {"geometry": {"initial_signature": [1]}}}
    with pytest.raises(ValueError, match="paired initial"):
        analysis.paired_comparisons([a, b], [7301])


def test_html_encodes_model_text_as_inert_data_and_keeps_nulls():
    hostile = "</script><img src=x onerror=alert(1)>"
    summary = {"runs": [{"seed": 7301, "condition": "recent", "run_id": hostile, "eligible": False}], "pairs": []}
    html = render_html(summary, []).decode()
    assert hostile not in html and "\\u003c/script>" in html
    assert "No eligible completed observations" in html
    assert ".textContent" in html and ".innerHTML" not in html
    assert "for(let a=0;a<24;a++)" in html


def test_derived_path_collision_and_raw_ancestry_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(analysis, "REPO_ROOT", tmp_path)
    existing = tmp_path / "derived" / (analysis.METRIC_VERSION + "_20260910T090000Z")
    existing.mkdir(parents=True)
    with pytest.raises(ValueError, match="collision"):
        analysis.safe_output(existing, tmp_path / "runs", analysis.METRIC_VERSION)
    bad = tmp_path / "runs" / (analysis.METRIC_VERSION + "_20260910T090001Z")
    with pytest.raises(ValueError):
        analysis.safe_output(bad, tmp_path / "runs", analysis.METRIC_VERSION)
