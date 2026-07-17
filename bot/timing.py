"""
Human timing module for chess.com bot.

The default behavior only delays the move that the engine already selected.
It does not weaken moves and does not alter engine search time unless the
optional move-changing settings are explicitly enabled.
"""

import asyncio
import logging
import random

import chess

logger = logging.getLogger(__name__)

_PIECE_VALUES = {
    chess.PAWN: 1.0,
    chess.KNIGHT: 3.0,
    chess.BISHOP: 3.0,
    chess.ROOK: 5.0,
    chess.QUEEN: 9.0,
    chess.KING: 0.0,
}


def _clamp(value, min_val, max_val):
    return max(min_val, min(max_val, value))


def _gaussian_delay(center, sigma, min_val=0.1, max_val=10.0):
    """
    Generate a delay following a normal distribution.
    Most delays cluster near center, with occasional faster/slower moves.
    """
    delay = random.gauss(center, sigma)
    return _clamp(delay, min_val, max_val)


def _piece_value(piece):
    if piece is None:
        return 0.0
    return _PIECE_VALUES.get(piece.piece_type, 0.0)


def _hanging_summary(board):
    """
    Estimate attacked and under-defended pieces.
    This is a rough proxy for tactical complexity.
    """
    hanging = 0
    material = 0.0
    high_value = False

    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue

        attackers = board.attackers(not piece.color, square)
        if not attackers:
            continue

        defenders = board.attackers(piece.color, square)
        if not defenders or len(attackers) > len(defenders):
            hanging += 1
            material += _piece_value(piece)
            high_value = high_value or piece.piece_type in (chess.ROOK, chess.QUEEN)

    return hanging, material, high_value


def _count_hanging_pieces(board):
    """
    Estimate the number of attacked and under-defended pieces.
    This is a rough proxy for tactical complexity.
    """
    hanging, _, _ = _hanging_summary(board)
    return hanging


def _capture_value(board, move):
    if board.is_en_passant(move):
        return _PIECE_VALUES[chess.PAWN]

    captured = board.piece_at(move.to_square)
    return _piece_value(captured)


def _nearby_squares(square):
    file_idx = chess.square_file(square)
    rank_idx = chess.square_rank(square)

    for file_delta in (-1, 0, 1):
        for rank_delta in (-1, 0, 1):
            next_file = file_idx + file_delta
            next_rank = rank_idx + rank_delta
            if 0 <= next_file <= 7 and 0 <= next_rank <= 7:
                yield chess.square(next_file, next_rank)


def _king_pressure(board, color):
    king_square = board.king(color)
    if king_square is None:
        return 0

    pressure = 0
    enemy = not color
    for square in _nearby_squares(king_square):
        pressure += min(2, len(board.attackers(enemy, square)))

    return pressure


def _forcing_move_summary(board, legal_moves):
    captures = 0
    checks = 0
    promotions = 0
    max_capture_value = 0.0

    for move in legal_moves:
        if board.is_capture(move):
            captures += 1
            max_capture_value = max(max_capture_value, _capture_value(board, move))

        if move.promotion:
            promotions += 1

        board.push(move)
        try:
            if board.is_check():
                checks += 1
        finally:
            board.pop()

    return captures, checks, promotions, max_capture_value


def _is_simple_recapture(board, legal_moves=None):
    """
    Check if the position likely calls for a simple recapture.
    """
    if not board.move_stack:
        return False

    last_move = board.peek()
    if not board.is_capture(last_move):
        return False

    to_square = last_move.to_square
    moves = legal_moves if legal_moves is not None else board.legal_moves
    return any(move.to_square == to_square and board.is_capture(move) for move in moves)


def build_position_metrics(board):
    """Compute per-position values once and reuse them for timing decisions."""
    legal_moves = tuple(board.legal_moves)
    hanging, hanging_material, high_value_hanging = _hanging_summary(board)
    captures, checks, promotions, max_capture_value = _forcing_move_summary(board, legal_moves)

    return {
        "legal_moves": legal_moves,
        "legal_move_count": len(legal_moves),
        "fullmove_number": board.fullmove_number,
        "ply": len(board.move_stack),
        "hanging": hanging,
        "hanging_material": hanging_material,
        "high_value_hanging": high_value_hanging,
        "piece_count": len(board.piece_map()),
        "in_check": board.is_check(),
        "capture_moves": captures,
        "check_moves": checks,
        "promotion_moves": promotions,
        "max_capture_value": max_capture_value,
        "king_pressure": _king_pressure(board, board.turn),
        "simple_recapture": _is_simple_recapture(board, legal_moves),
    }


class HumanTiming:
    """
    Apply human-like timing without changing the chosen move.

    Timing model:
    - Uses a bell-curve distribution instead of flat random delays.
    - Speeds up obvious recaptures, forced moves, openings, and time trouble.
    - Slows down complex middlegame positions and tactical positions.
    - Respects configured delay_min/delay_max as the normal timing range.
    """

    def __init__(self, config):
        self.config = config
        self.reset()

    def reset(self):
        """Reset state for a new game."""
        self._move_number = 0
        self._total_think_time = 0.0
        self._tempo_bias = random.uniform(0.86, 1.14)
        self._opening_bias = random.uniform(0.72, 0.95)
        self._hesitation_chance = random.uniform(0.04, 0.10)
        self._base_time = None
        self._increment = None
        self._our_time = None
        self._opp_time = None
        self._last_opp_time = None
        self._last_opponent_think = None
        self._last_delay = None

    def set_time_control(self, base_time, increment):
        """
        Set the time control for the current game.

        Args:
            base_time: Base time in seconds, such as 600 for 10 minutes.
            increment: Increment in seconds, such as 0, 2, or 5.
        """
        self._base_time = base_time
        self._increment = increment
        logger.info("Time control set: %s+%s", base_time, increment)

    def update_clocks(self, our_time, opp_time):
        """
        Update current clock readings for time-pressure-aware delays.

        Args:
            our_time: Our remaining time in seconds.
            opp_time: Opponent's remaining time in seconds.
        """
        if self._last_opp_time is not None and opp_time is not None:
            spent = self._last_opp_time - opp_time
            increment = self._increment or 0
            if increment:
                spent += increment
            if spent >= 0:
                self._last_opponent_think = spent

        self._our_time = our_time
        self._opp_time = opp_time
        self._last_opp_time = opp_time

    async def apply_delay(self, board, metrics=None):
        """
        Wait a human-like amount of time before making a move.
        The engine move is already selected before this method is called.
        """
        if not self.config.timing_enabled:
            return

        self._move_number += 1
        if metrics is None:
            metrics = build_position_metrics(board)

        if self._should_premove(metrics):
            delay = self._calculate_premove_delay(metrics)
            logger.debug(
                "Timing premove delay: %.2fs (move #%d, legal=%d, criticality=%.2f)",
                delay,
                self._move_number,
                metrics["legal_move_count"],
                self._criticality(metrics),
            )
            await asyncio.sleep(delay)
            self._remember_delay(delay)
            return

        delay = self._calculate_delay(metrics)
        self._total_think_time += delay

        logger.debug(
            "Timing delay: %.2fs (move #%d, total_delay: %.1fs, criticality=%.2f)",
            delay,
            self._move_number,
            self._total_think_time,
            self._criticality(metrics),
        )
        await asyncio.sleep(delay)
        self._remember_delay(delay)

    def _remember_delay(self, delay):
        self._last_delay = delay

    def _normal_delay_bounds(self):
        min_delay = max(0.0, float(self.config.timing_delay_min))
        max_delay = max(min_delay, float(self.config.timing_delay_max))
        return min_delay, max_delay

    def _calculate_premove_delay(self):
        min_delay, max_delay = self._normal_delay_bounds()
        upper = min(max_delay, max(0.18, min_delay + 0.25))
        lower = min(0.08, upper)
        return random.uniform(lower, upper)

    def _should_premove(self, metrics):
        """Decide if this should be a near-instant response."""
        if metrics["legal_move_count"] == 1:
            return random.random() < 0.70

        if metrics["simple_recapture"]:
            chance = min(0.65, self.config.timing_premove_chance * 3)
            return random.random() < chance

        return random.random() < self.config.timing_premove_chance

    def _calculate_delay(self, metrics):
        """
        Calculate a delay using a position-aware bell curve.
        """
        min_delay, max_delay = self._normal_delay_bounds()
        span = max(0.1, max_delay - min_delay)

        legal_moves = metrics["legal_move_count"]
        hanging = metrics["hanging"]
        piece_count = metrics["piece_count"]

        if self._move_number <= 6:
            phase_ratio = random.uniform(0.18, 0.35)
        elif self._move_number <= 14:
            phase_ratio = random.uniform(0.30, 0.55)
        elif piece_count > 14:
            phase_ratio = random.uniform(0.45, 0.78)
        elif piece_count <= 10:
            phase_ratio = random.uniform(0.30, 0.58)
        else:
            phase_ratio = random.uniform(0.36, 0.65)

        center = min_delay + span * phase_ratio
        sigma = max(0.06, span * 0.16)

        if legal_moves > 35:
            center *= 1.35
            sigma *= 1.20
        elif legal_moves > 25:
            center *= 1.15
        elif legal_moves <= 8:
            center *= 0.78

        if hanging >= 3:
            center *= 1.45 if random.random() > 0.25 else 0.62
            sigma *= 1.25
        elif hanging >= 1:
            center *= 1.15

        if metrics["in_check"]:
            center *= 0.62 if random.random() < 0.65 else 1.18

        if self._last_opponent_think is not None:
            if self._last_opponent_think < 0.8:
                center *= 0.78
            elif self._last_opponent_think > 8:
                center *= 1.08

        center *= self._clock_pressure_factor()

        if self._move_number > 40:
            sigma *= 1.35

        delay = _gaussian_delay(center, sigma, min_delay, max_delay)

        if self._last_delay is not None and abs(delay - self._last_delay) < 0.08:
            delay += random.uniform(-0.18, 0.22)
            delay = _clamp(delay, min_delay, max_delay)

        return delay

    def _clock_pressure_factor(self):
        if self._our_time is None:
            return 1.0

        our_time = max(0.0, float(self._our_time))
        base_time = max(1.0, float(self._base_time or our_time or 1.0))
        increment = max(0.0, float(self._increment or 0.0))
        ratio = our_time / base_time

        if our_time <= 5:
            factor = 0.16
        elif our_time <= 10:
            factor = 0.24
        elif our_time <= 30:
            factor = 0.42
        elif ratio < 0.15:
            factor = 0.58
        elif ratio > 0.70 and self._move_number <= 10:
            factor = 1.08
        else:
            factor = 1.0

        if increment >= 2 and our_time < 60:
            factor += 0.12

        return _clamp(factor, 0.15, 1.18)

    def should_blunder(self, board=None, metrics=None):
        """
        Decide if a suboptimal move should be played.

        This is disabled by default so human timing does not change moves.
        """
        if not self.config.humanizer_change_moves:
            return False

        if self._move_number <= 6:
            return False

        base_chance = self.config.humanizer_blunder_chance
        if base_chance <= 0:
            return False

        if board:
            if metrics is None:
                metrics = build_position_metrics(board)
            hanging = metrics["hanging"]
            legal_moves = metrics["legal_move_count"]
            piece_count = metrics["piece_count"]

            if hanging >= 2:
                base_chance *= 1.8
            elif hanging >= 1:
                base_chance *= 1.3

            if legal_moves > 30:
                base_chance *= 1.4

            if piece_count <= 8:
                base_chance *= 0.5

        if self._move_number > 35:
            base_chance *= 1.3
        if self._move_number > 50:
            base_chance *= 1.6

        roll = random.random()
        if roll < base_chance:
            logger.info(
                "Move change triggered (roll=%.3f < chance=%.3f, move #%d, hanging=%d)",
                roll,
                base_chance,
                self._move_number,
                metrics["hanging"] if metrics else 0,
            )
            return True
        return False

    def pick_blunder_move(self, top_moves, board):
        """
        Pick a suboptimal but legal move from engine alternatives.
        Only used when humanizer.change_moves is enabled.
        """
        if len(top_moves) <= 1:
            return top_moves[0][0] if top_moves else None

        chosen_idx = 2 if len(top_moves) >= 3 and random.random() < 0.25 else 1
        move = top_moves[chosen_idx][0]
        logger.info(
            "Move change: %s (rank #%d) instead of %s",
            board.san(move),
            chosen_idx + 1,
            board.san(top_moves[0][0]),
        )
        return move

    def get_engine_time_adjustment(self, board, metrics=None):
        """
        Return a multiplier for engine search time.

        Disabled by default because changing search time can change the move.
        """
        if not self.config.humanizer_adjust_engine_time:
            return 1.0

        if metrics is None:
            metrics = build_position_metrics(board)
        legal_moves = metrics["legal_move_count"]

        if legal_moves <= 3:
            return 0.3
        if legal_moves <= 10:
            return 0.6
        if legal_moves <= 25:
            return 1.0
        return 1.3


# Backward compatibility for existing imports and external scripts.
Humanizer = HumanTiming
