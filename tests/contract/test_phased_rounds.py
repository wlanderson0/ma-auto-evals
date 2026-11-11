"""Phased rounds (``phased_rounds``): a round of named phases, kept by the schedule, and
resolved once its last phase has run, under the audit runtime.

A two-player game of up to three rounds, each round three phases: ``talk`` (alice then
bob post), ``propose`` (each records a proposal) and ``vote`` (each accepts or not). A
round where both accept ends the run with ``end_run``; the referee posts each round's
result and announces each phase on the shared channel. The scripted players say what
each test needs: a move made in the wrong phase, a vote left out, a deal, no deal.
"""

from __future__ import annotations

from collections.abc import Callable

from inspect_ai import eval as inspect_eval
from inspect_ai.model import ChatMessage, ModelOutput, get_model
from inspect_ai.tool import Tool, tool
from inspect_ai.util import StoreModel, store_as
from orbit import ChannelConfig
from orbit.ids import id_scope, sequential_ids

from experiments.scenarios import (
    RUN_END_KEY,
    Phase,
    RoundResult,
    build_team,
    current_phase,
    in_phase,
    phased_rounds,
    team_setup,
)
from orbit_audit.policy import AuditPolicy
from orbit_audit.task import audit_task
from tests.support.runs import model_events, sample
from tests.support.scripted import call, post, reply

PLAYERS = ("alice", "bob")
ROSTER = (*PLAYERS, "referee")
TABLE = (ChannelConfig(id="table", readers=list(ROSTER), writers=list(ROSTER)),)


class Game(StoreModel):
    proposals: dict[str, str] = {}
    """``"<round>:<agent>"`` -> proposal."""
    votes: dict[str, bool] = {}
    """``"<round>:<agent>"`` -> accepted."""
    seen: list[str] = []
    """``"<agent>:<round>-<phase>"`` for every tool call, from ``current_phase``."""


@tool
def propose(agent: str) -> Tool:
    async def execute(offer: str) -> str:
        """Propose a split.

        Args:
            offer: The split you propose.
        """
        n = in_phase("propose")
        store_as(Game).proposals[f"{n}:{agent}"] = offer
        return "Proposal recorded."

    return execute


@tool
def vote(agent: str) -> Tool:
    async def execute(accept: bool) -> str:
        """Vote on the other player's proposal.

        Args:
            accept: Whether you accept it.
        """
        n = in_phase("vote")
        store_as(Game).votes[f"{n}:{agent}"] = accept
        return "Vote recorded."

    return execute


@tool
def where(agent: str) -> Tool:
    async def execute() -> str:
        """Say which phase this is."""
        now = current_phase()
        store_as(Game).seen.append(f"{agent}:r{now[0]}-{now[1]}" if now else f"{agent}:none")
        return str(now)

    return execute


def resolve(n: int) -> RoundResult:
    game = store_as(Game)
    votes = [game.votes.get(f"{n}:{agent}") for agent in PLAYERS]
    if all(votes):
        return RoundResult(announce=f"Round {n}: deal.", end=f"deal in round {n}")
    return RoundResult(announce=f"Round {n}: no deal (votes {votes}).")


def on_the_table(n: int) -> str:
    proposals = store_as(Game).proposals
    return "Proposals: " + "; ".join(f"{agent} {proposals.get(f'{n}:{agent}', 'none')}"
                                     for agent in PLAYERS) + ". Call vote now."


PHASES = (Phase("talk"), Phase("propose", announce="Call propose now."),
          Phase("vote", announce=on_the_table))


def speaker(messages: list[ChatMessage]) -> str:
    system = next(m.text for m in messages if m.role == "system")
    return next(agent for agent in ROSTER if system.startswith(f"You are {agent} at "))


Script = Callable[[str, int], ModelOutput]


def round_of(turn: int) -> tuple[int, str]:
    """The (round, phase) of a player's ``turn``-th generation when each player
    generates once per phase (``model_step``)."""
    return turn // 3 + 1, ("talk", "propose", "vote")[turn % 3]


def honest(accept_in: int | None) -> Script:
    """Posts, proposes and votes in the right phases; accepts only in ``accept_in``."""
    def script(agent: str, turn: int) -> ModelOutput:
        n, phase = round_of(turn)
        if phase == "talk":
            return post(f"{agent} talks in round {n}", "table")
        if phase == "propose":
            return call("propose", offer=f"{agent}'s split {n}")
        return call("vote", accept=n == accept_in)
    return script


def run(tmp_path, script: Script, *, rounds: int = 3):
    team = team_setup(
        {}, personas={agent: agent for agent in ROSTER}, roster=ROSTER, round_order=PLAYERS,
        channels=TABLE, output_agent="bob", rounds=rounds,
        tools={"propose": propose, "vote": vote, "where": where},
        phased=phased_rounds(PHASES, resolve, referee="referee", channel="table"),
    )

    def models(messages, tools, tool_choice, config) -> ModelOutput:
        agent = speaker(messages)
        turn = sum(1 for m in messages if m.role == "assistant")
        return script(agent, turn)

    task = audit_task(policy=lambda: AuditPolicy(), tool_replay={
        "propose": "pure", "vote": "pure", "where": "pure"}, **build_team("game", team))
    model = get_model("mockllm/model", custom_outputs=models, memoize=False)
    with id_scope(sequential_ids()):
        [log] = inspect_eval(task, model=model, log_dir=str(tmp_path), display="none")
    assert log.status == "success", log.samples[0].error
    return log


def speakers(log) -> list[str]:
    return [speaker(e.input) for e in model_events(log)]


def referee_posts(log) -> list[str]:
    return [m["content"] for m in sample(log).store["CommunicationState:messages"]
            if m["sender"] == "referee"]


def tool_errors(log) -> list[str]:
    return [e.error.message for e in sample(log).events
            if e.event == "tool" and e.error is not None]


def test_build_names_each_step_after_its_round_and_phase():
    team = team_setup({}, personas={agent: agent for agent in ROSTER}, roster=ROSTER,
                      round_order=PLAYERS, channels=TABLE, output_agent="bob", rounds=2,
                      phased=phased_rounds(PHASES, resolve, referee="referee",
                                           channel="table"))
    plan = build_team("game", team)["execution"].plan
    assert [(s.id, s.agents) for s in plan.steps] == [
        (f"r{n}-{phase}", PLAYERS) for n in (1, 2) for phase in ("talk", "propose", "vote")]
    assert plan.after_activation and plan.after_activation.startswith("experiments.scenarios.")
    assert team.turns_per_round() == 3


def test_a_tool_reads_its_round_and_phase_from_the_schedule(tmp_path):
    def script(agent: str, turn: int) -> ModelOutput:
        return call("where") if turn < 3 else reply("done")
    log = run(tmp_path, script, rounds=1)
    assert store_value(log, "seen") == [f"{agent}:r1-{phase}" for phase in
                                        ("talk", "propose", "vote") for agent in PLAYERS]


def store_value(log, field: str):
    return sample(log).store[f"Game:{field}"]


def test_a_deal_ends_the_run_and_the_referee_says_so(tmp_path):
    log = run(tmp_path, honest(accept_in=2))
    assert speakers(log) == list(PLAYERS) * 6  # two rounds of three phases, then the end
    assert sample(log).store[RUN_END_KEY] == "deal in round 2"
    assert referee_posts(log) == [
        "Round 1 of 3, propose phase. Call propose now.",
        "Round 1 of 3, vote phase. Proposals: alice alice's split 1; bob bob's split 1. "
        "Call vote now.",
        "Round 1: no deal (votes [False, False]).",
        "Round 2 of 3, propose phase. Call propose now.",
        "Round 2 of 3, vote phase. Proposals: alice alice's split 2; bob bob's split 2. "
        "Call vote now.",
        "Round 2: deal.",
    ]


def test_each_player_sees_each_referee_post_once(tmp_path):
    log = run(tmp_path, honest(accept_in=2))
    for agent in PLAYERS:
        inputs = ["\n".join(m.text for m in e.input) for e in model_events(log)
                  if speaker(e.input) == agent]
        for text in referee_posts(log)[:-1]:  # the last one comes after the last turn
            assert sum(text in seen for seen in inputs) == 1, (agent, text)


def test_the_last_round_is_resolved_with_no_deal(tmp_path):
    log = run(tmp_path, honest(accept_in=None))
    assert speakers(log) == list(PLAYERS) * 9
    assert RUN_END_KEY not in sample(log).store
    assert referee_posts(log)[-1] == "Round 3: no deal (votes [False, False])."
    assert not any(post.startswith("Round 4") for post in referee_posts(log))


def test_a_move_in_the_wrong_phase_is_refused_and_the_game_stays_in_step(tmp_path):
    def script(agent: str, turn: int) -> ModelOutput:
        if agent == "bob" and turn == 0:
            return call("vote", accept=True)  # in round 1's talk phase
        return honest(accept_in=1)(agent, turn)
    log = run(tmp_path, script)
    assert tool_errors(log) == ["Round 1 is in its talk phase; this is for the vote phase."]
    assert store_value(log, "votes") == {"1:alice": True, "1:bob": True}
    assert sample(log).store[RUN_END_KEY] == "deal in round 1"


def test_a_missing_vote_still_resolves_the_round(tmp_path):
    def script(agent: str, turn: int) -> ModelOutput:
        if agent == "bob" and round_of(turn) == (1, "vote"):
            return reply("I'll think about it.")  # no vote in round 1
        return honest(accept_in=2)(agent, turn)
    log = run(tmp_path, script)
    assert referee_posts(log)[2] == "Round 1: no deal (votes [False, None])."
    assert sample(log).store[RUN_END_KEY] == "deal in round 2"
