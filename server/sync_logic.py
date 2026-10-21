"""Pure sync rules (docs/DESIGN.md, "Sync rules"). No networking. Delays are seconds
behind the real game.

Each user has a range: earliest = live edge (can't see the game sooner),
latest = live edge + max extra delay. The group syncs at the highest live edge in it
(the slowest member's earliest moment), must fit everyone's range, and nobody may be more
than `max_gap` behind the group's fastest member. Moves are worded from where each user
is sitting *now*, not from their live edge.

Worked example from the design doc (everyone at their live edge):
    A edge 3 (max 30), B edge 8 (max 10), C edge 6 (max 20), D edge 15 (max 20)
    -> A, B, C sync at 8 s (A waits 5, C waits 2, B 0).
    D's range overlaps everyone's, but D is 12 s behind A, over the 10 s gap.
Offer example (rule 3): B's live edge is 25 s but B sits at 41 s, A sits at its edge 20 s
    -> target 25: A pauses 5 s, B skips forward 16 s.
"""

from __future__ import annotations

from dataclasses import dataclass, field

EPS = 1e-6
DEFAULT_MAX_DELAY = 30.0
DEFAULT_MAX_GAP = 10.0
NO_MOVE = 0.05   # a move smaller than this is "no change"


@dataclass(frozen=True)
class Member:
    user: str
    video_id: str
    live_edge: float          # lowest delay this user can reach
    current_delay: float      # what the user sees now
    max_delay: float = DEFAULT_MAX_DELAY

    @property
    def latest(self) -> float:
        return self.live_edge + self.max_delay

    def fits(self, target: float) -> bool:
        return self.live_edge - EPS <= target <= self.latest + EPS


@dataclass(frozen=True)
class Move:
    action: str      # "pause", "skip_forward" or "none"
    seconds: float

    def text(self) -> str:
        if self.action == "pause":
            return f"pause your stream {self.seconds:.1f} s"
        if self.action == "skip_forward":
            return f"skip your stream forward {self.seconds:.1f} s"
        return "no change to your stream"

    def as_dict(self) -> dict:
        return {"action": self.action, "seconds": round(self.seconds, 3), "text": self.text()}


def move_for(m: Member, target: float) -> Move:
    d = target - m.current_delay
    if abs(d) < NO_MOVE:
        return Move("none", 0.0)
    return Move("pause", d) if d > 0 else Move("skip_forward", -d)


@dataclass
class Plan:
    ok: bool
    target: float | None
    group: list[str] = field(default_factory=list)
    moves: dict[str, Move] = field(default_factory=dict)
    left_out: dict[str, str] = field(default_factory=dict)   # user -> reason


def _choose(members: list[Member], must_include: str | None, max_gap: float):
    """Largest group sharing one target; ties go to the lower target (earlier moment).

    Only a member's live edge can be the target (the slowest member sets it), and the
    group's fastest member is some member's live edge too, so trying every
    (target, fastest) pair of edges covers all groups. O(n^3), fine for 10 users.
    """
    edges = sorted({m.live_edge for m in members})
    best = None
    for target in edges:
        for fastest in edges:
            if fastest > target + EPS or target - fastest > max_gap + EPS:
                continue
            inside = [m for m in members
                      if fastest - EPS <= m.live_edge <= target + EPS and m.fits(target)]
            if must_include and must_include not in {m.user for m in inside}:
                continue
            key = (len(inside), -target)
            if best is None or key > best[0]:
                best = (key, target, inside)
    return best


def _reason(m: Member, target: float, inside: list[Member], max_gap: float) -> str:
    fastest = min(inside, key=lambda o: o.live_edge)
    if m.live_edge > target + EPS:
        behind = m.live_edge - fastest.live_edge
        if behind > max_gap + EPS:
            return (f"{behind:.1f} s behind the fastest ({fastest.user}), over the {max_gap:g} s gap: "
                    "try a lower video quality or check your connection")
        blockers = [f"{o.user} would need {m.live_edge - o.live_edge:.1f} s past live (max {o.max_delay:g})"
                    for o in inside if not o.fits(m.live_edge)]
        return "joining would move the group to your live edge, but " + ", ".join(blockers)
    if target - m.live_edge > max_gap + EPS:
        return f"the group is {target - m.live_edge:.1f} s behind you, over the {max_gap:g} s gap"
    return f"would need {target - m.live_edge:.1f} s past your live edge (max {m.max_delay:g})"


def plan_group(members: list[Member], must_include: str | None = None,
               max_gap: float = DEFAULT_MAX_GAP) -> Plan:
    if not members:
        return Plan(False, None)
    video = next((m.video_id for m in members if m.user == must_include), None)
    candidates = []
    for vid in {m.video_id for m in members} if video is None else {video}:
        best = _choose([m for m in members if m.video_id == vid], must_include, max_gap)
        if best:
            candidates.append(best)
    if not candidates:
        return Plan(False, None, left_out={m.user: "no feasible group" for m in members})
    _, target, inside = max(candidates, key=lambda c: c[0])
    names = {m.user for m in inside}
    plan = Plan(len(inside) > 1, target, sorted(names),
                {m.user: move_for(m, target) for m in inside})
    for m in members:
        if m.user in names:
            continue
        plan.left_out[m.user] = ("watching a different video" if m.video_id != inside[0].video_id
                                 else _reason(m, target, inside, max_gap))
    return plan


def match_pair(requester: Member, other: Member, max_gap: float = DEFAULT_MAX_GAP) -> Plan:
    """`requester` clicked `other`. OK only if both fit one target."""
    plan = plan_group([requester, other], must_include=requester.user, max_gap=max_gap)
    plan.ok = plan.ok and len(plan.group) == 2
    return plan
