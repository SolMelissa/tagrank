"""Tournament / Bracket Mode: single-elimination brackets over a randomly-seeded subset
of the active pool, using the same pairwise comparison flow as a normal session.

Seeding is random, not top-N: pitting only already-favored files against each other would
teach TrueSkill nothing about the wider pool (see the "Fun Features" plan's rationale).
"""

from __future__ import annotations

import random
from dataclasses import dataclass, field

from tagrank import badges


def _bracket_size(pool_size: int, max_size: int) -> int:
    """Largest power of 2 that is <= min(pool_size, max_size), minimum 2."""
    capped = max(2, min(pool_size, max_size))
    size = 1
    while size * 2 <= capped:
        size *= 2
    return size


@dataclass
class Match:
    round_index: int
    slot: int
    left_id: int | None
    right_id: int | None
    winner_id: int | None = None


@dataclass
class Tournament:
    entrants: list[int]
    bracket_id_to_hash: dict[int, str] = field(default_factory=dict)
    rounds: list[list[Match]] = field(default_factory=list)
    current_round: int = 0

    @property
    def champion_id(self) -> int | None:
        if self.rounds and all(m.winner_id is not None for m in self.rounds[-1]):
            return self.rounds[-1][0].winner_id
        return None

    @property
    def is_complete(self) -> bool:
        return self.champion_id is not None

    def pending_match(self) -> Match | None:
        for round_matches in self.rounds:
            for match in round_matches:
                if match.winner_id is None and match.left_id is not None and match.right_id is not None:
                    return match
        return None

    def record_winner(self, match: Match, winner_id: int) -> None:
        match.winner_id = winner_id
        round_matches = self.rounds[match.round_index]
        if all(m.winner_id is not None for m in round_matches):
            self.current_round = min(match.round_index + 1, len(self.rounds) - 1)
        if match.round_index + 1 >= len(self.rounds):
            return
        next_round = self.rounds[match.round_index + 1]
        next_match = next_round[match.slot // 2]
        if match.slot % 2 == 0:
            next_match.left_id = winner_id
        else:
            next_match.right_id = winner_id


def start_tournament(pool_ids: list[int], max_size: int, id_to_hash: dict[int, str] | None = None) -> Tournament:
    """Draw a random `_bracket_size(...)`-sized subset of pool_ids and lay out an empty
    single-elimination bracket (byes are impossible by construction: size is always a
    power of 2 no larger than the entrant count)."""
    size = _bracket_size(len(pool_ids), max_size)
    entrants = random.sample(pool_ids, k=size)

    num_rounds = size.bit_length() - 1  # log2(size)
    rounds: list[list[Match]] = []
    round_size = size // 2
    for round_index in range(num_rounds):
        matches = [Match(round_index=round_index, slot=slot, left_id=None, right_id=None) for slot in range(round_size)]
        rounds.append(matches)
        round_size //= 2

    for slot, match in enumerate(rounds[0]):
        match.left_id = entrants[slot * 2]
        match.right_id = entrants[slot * 2 + 1]

    return Tournament(entrants=entrants, bracket_id_to_hash=id_to_hash or {}, rounds=rounds)


def finish_tournament(tournament: Tournament, seed_tag: str | None = None) -> None:
    """Award tournament-linked badges once a champion is crowned. Called once, when
    tournament.is_complete becomes True. Picture badges are event-driven (not stat
    conditions), so they're awarded directly via badges.award_manual/bump_tournament_stat -
    the tag-side "Grand Champion" badge, if a seed_tag is given, is awarded the same way."""
    champion_id = tournament.champion_id
    if champion_id is None:
        return

    champion_hash = tournament.bracket_id_to_hash.get(champion_id)
    if champion_hash:
        badges.bump_tournament_stat("picture", champion_hash, won_bracket=True, reached_final=True)
        badges.award_manual("picture", champion_hash, "tournament_champion")
        if seed_tag:
            badges.award_manual("tag", seed_tag, "grand_champion")

    final_match = tournament.rounds[-1][0]
    for finalist_id in (final_match.left_id, final_match.right_id):
        if finalist_id is None or finalist_id == champion_id:
            continue
        finalist_hash = tournament.bracket_id_to_hash.get(finalist_id)
        if finalist_hash:
            badges.bump_tournament_stat("picture", finalist_hash, won_bracket=False, reached_final=True)


def check_bracket_buster(tournament: Tournament, match: Match, winner_id: int, mu_by_id: dict[int, float], sigma_by_id: dict[int, float]) -> None:
    """Awards 'Bracket Buster' when a low-mu entrant beats a much higher-mu opponent within
    a single bracket match - an upset within the tournament itself, distinct from the
    session-wide Underdog Alert."""
    loser_id = match.right_id if winner_id == match.left_id else match.left_id
    if loser_id is None:
        return
    winner_mu, loser_mu = mu_by_id.get(winner_id), mu_by_id.get(loser_id)
    loser_sigma = sigma_by_id.get(loser_id)
    if winner_mu is None or loser_mu is None or not loser_sigma:
        return
    if (winner_mu - loser_mu) / loser_sigma >= badges.UPSET_SIGMA_MULTIPLE:
        winner_hash = tournament.bracket_id_to_hash.get(winner_id)
        if winner_hash:
            badges.award_manual("picture", winner_hash, "bracket_buster")
