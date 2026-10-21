from server.sync_logic import Member, match_pair, plan_group

V = "0sfDth4lMLc"


def at_edge(user, edge, mx, video=V):
    return Member(user, video, live_edge=edge, current_delay=edge, max_delay=mx)


# Design doc worked example.
A, B, C, D = at_edge("A", 3, 30), at_edge("B", 8, 10), at_edge("C", 6, 20), at_edge("D", 15, 20)


def test_doc_worked_example_abc_sync_at_8_d_left_out_by_gap():
    plan = plan_group([A, B, C, D], must_include="A")
    assert plan.ok and plan.target == 8 and plan.group == ["A", "B", "C"]
    assert plan.moves["A"].as_dict()["text"] == "pause your stream 5.0 s"
    assert plan.moves["C"].seconds == 2 and plan.moves["B"].action == "none"
    assert plan.left_out["D"].startswith("12.0 s behind the fastest (A), over the 10 s gap")


def test_without_gap_rule_everyone_would_sync_at_15():
    plan = plan_group([A, B, C, D], must_include="A", max_gap=100)
    assert plan.target == 15 and plan.group == ["A", "B", "C", "D"]


def test_offer_moves_are_from_current_position_rule_3():
    # B's live edge is 25 s but B is sitting at 41 s.
    a = Member("A", V, live_edge=20, current_delay=20, max_delay=30)
    b = Member("B", V, live_edge=25, current_delay=41, max_delay=30)
    plan = match_pair(a, b)
    assert plan.ok and plan.target == 25
    assert plan.moves["A"].as_dict() == {"action": "pause", "seconds": 5, "text": "pause your stream 5.0 s"}
    assert plan.moves["B"].as_dict() == {"action": "skip_forward", "seconds": 16,
                                         "text": "skip your stream forward 16.0 s"}


def test_pair_is_symmetric_and_slowest_waits_zero():
    for plan in (match_pair(A, B), match_pair(B, A)):
        assert plan.ok and plan.target == 8
        assert plan.moves["A"].seconds == 5 and plan.moves["B"].action == "none"


def test_pair_refused_over_max():
    c45 = at_edge("C", 45, 20)
    plan = match_pair(A, c45)  # 42 s apart: over the gap and over A's max
    assert not plan.ok and "C" in plan.left_out


def test_pair_refused_by_max_inside_gap():
    b = at_edge("B", 8, 2)      # B accepts only 2 s extra
    x = at_edge("X", 9.9 + 3, 30)  # 12.9 s edge -> B would need 4.9 s
    plan = match_pair(b, x, max_gap=10)
    assert not plan.ok and "X" in plan.left_out
    assert "B would need 4.9 s past live (max 2)" in plan.left_out["X"]


def test_requester_always_in_group_even_if_alone():
    c45 = at_edge("C", 45, 20)
    plan = plan_group([A, B, c45], must_include="C")
    assert not plan.ok and plan.group == ["C"] and plan.target == 45
    assert plan.left_out["A"] == "the group is 42.0 s behind you, over the 10 s gap"


def test_tie_goes_to_lower_target():
    # {A,B,C}@8 and {B,C,D}@15 are both size 3 and both within the gap (requester B).
    plan = plan_group([A, B, C, D], must_include="B")
    assert plan.target == 8 and plan.group == ["A", "B", "C"]


def test_different_videos_never_mix():
    y = at_edge("Y", 4, 60, video="other")
    assert not match_pair(A, y).ok
    plan = plan_group([A, B, y], must_include="A")
    assert plan.group == ["A", "B"] and plan.left_out["Y"] == "watching a different video"


def test_exactly_at_max_and_gap_is_allowed():
    a = at_edge("A", 0, 10)
    b = at_edge("B", 10, 0)
    assert match_pair(a, b, max_gap=10).ok
