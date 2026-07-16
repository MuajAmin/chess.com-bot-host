"""
Humanizer module for chess.com bot.
Makes bot behavior indistinguishable from a real human player.

Features:
- Gaussian distribution delays (bell curve, not uniform)
- Tactical complexity-aware delay scaling
- Position-dependent blunder rates (complex = more blunders)
- Premove simulation for obvious recaptures
- Game phase awareness (opening theory = fast, middlegame = slow)
"""

import random
import logging
import asyncio
import chess

logger = logging.getLogger(__name__)


def _gaussian_delay(center, sigma, min_val=0.1, max_val=10.0):
    """
    Generate a delay following Gaussian (normal) distribution.
    More realistic than uniform random — most delays cluster around center.
    """
    delay = random.gauss(center, sigma)
    return max(min_val, min(max_val, delay))


def _count_hanging_pieces(board):
    """
    Estimate the number of hanging (undefended attacked) pieces.
    A rough proxy for tactical complexity.
    """
    hanging = 0
    for square in chess.SQUARES:
        piece = board.piece_at(square)
        if piece is None:
            continue

        # Check if this piece is attacked by the opponent
        attackers = board.attackers(not piece.color, square)
        if not attackers:
            continue

        # Check if it's defended
        defenders = board.attackers(piece.color, square)
        if not defenders:
            hanging += 1
        elif len(attackers) > len(defenders):
            hanging += 1  # Overloaded defense

    return hanging


def _is_simple_recapture(board, legal_moves=None):
    """
    Check if the position likely calls for a simple recapture.
    (Last move was a capture and there's an obvious retake.)
    """
    if not board.move_stack:
        return False

    last_move = board.peek()
    # Check if last move was a capture and the piece is still capturable
    if board.is_capture(last_move):
        # Check if we can recapture on that square
        to_square = last_move.to_square
        moves = legal_moves if legal_moves is not None else board.legal_moves
        for move in moves:
            if move.to_square == to_square and board.is_capture(move):
                return True
    return False


def build_position_metrics(board):
    """Compute expensive per-position values once and reuse them."""
    legal_moves = tuple(board.legal_moves)
    return {
        "legal_moves": legal_moves,
        "legal_move_count": len(legal_moves),
        "hanging": _count_hanging_pieces(board),
        "piece_count": len(board.piece_map()),
        "in_check": board.is_check(),
        "simple_recapture": _is_simple_recapture(board, legal_moves),
    }


class Humanizer:
    """
    Makes the bot play with human-like timing and occasional errors.

    Delay model:
    - Uses Gaussian distribution (bell curve) centered on a base delay
    - Base delay scales with position complexity
    - Opening moves: fast (theory known)
    - Simple recaptures: very fast (premove territory)
    - Complex middlegame: slow (deep thinking)
    - Endgame: moderate

    Blunder model:
    - Base blunder rate from config
    - Increases in complex/tactical positions (humans blunder more when overwhelmed)
    - Decreases in simple positions
    - Increases late in game (fatigue simulation)
    """

    def __init__(self, config):
        self.config = config
        self._move_number = 0
        self._total_think_time = 0.0

    def reset(self):
        """Reset state for a new game."""
        self._move_number = 0
        self._total_think_time = 0.0
        self._base_time = None
        self._increment = None
        self._our_time = None
        self._opp_time = None

    def set_time_control(self, base_time, increment):
        """
        Set the time control for the current game.

        Args:
            base_time: Base time in seconds (e.g., 600 for 10 min)
            increment: Increment in seconds (e.g., 0, 2, 5)
        """
        self._base_time = base_time
        self._increment = increment
        logger.info("Time control set: %s+%s", base_time, increment)

    def update_clocks(self, our_time, opp_time):
        """
        Update current clock readings for time-pressure-aware delays.

        Args:
            our_time: Our remaining time in seconds
            opp_time: Opponent's remaining time in seconds
        """
        self._our_time = our_time
        self._opp_time = opp_time

    async def apply_delay(self, board, metrics=None):
        """
        Wait a human-like amount of time before making a move.
        Uses Gaussian distribution scaled by position characteristics.
        """
        if not self.config.humanizer_enabled:
            return

        self._move_number += 1
        if metrics is None:
            metrics = build_position_metrics(board)

        # Premove simulation — obvious recaptures or forced moves
        if self._should_premove(board, metrics):
            delay = random.uniform(0.05, 0.2)
            logger.debug("PREMOVE delay: %.2fs (move #%d)", delay, self._move_number)
            await asyncio.sleep(delay)
            return

        # Calculate position-aware delay
        delay = self._calculate_delay(board, metrics)
        self._total_think_time += delay

        logger.debug(
            "Delay: %.2fs (move #%d, total_think: %.1fs)",
            delay, self._move_number, self._total_think_time,
        )
        await asyncio.sleep(delay)

    def _should_premove(self, board, metrics=None):
        """Decide if this should be a premove (near-instant response)."""
        # Forced move (only 1 legal move) — humans premove these
        if metrics is None:
            metrics = build_position_metrics(board)
        if metrics["legal_move_count"] == 1:
            return random.random() < 0.7  # 70% chance of premove for forced moves

        # Simple recapture — humans often premove these
        if metrics["simple_recapture"]:
            return random.random() < self.config.humanizer_premove_chance * 3

        # Random premove chance (rare)
        return random.random() < self.config.humanizer_premove_chance

    def _calculate_delay(self, board, metrics=None):
        """
        Calculate delay using Gaussian distribution with position-aware scaling.

        The bell curve center shifts based on:
        - Game phase (opening/middlegame/endgame)
        - Position complexity (legal moves, hanging pieces)
        - Check status
        """
        if metrics is None:
            metrics = build_position_metrics(board)
        legal_moves = metrics["legal_move_count"]
        hanging = metrics["hanging"]
        piece_count = metrics["piece_count"]

        # --- Determine base delay center and sigma ---

        # Opening (first 8 moves) — fast, we "know theory"
        if self._move_number <= 8:
            center = random.uniform(0.2, 0.6)
            sigma = 0.15

        # Early middlegame (moves 9-15) — transitioning
        elif self._move_number <= 15:
            center = random.uniform(0.5, 1.2)
            sigma = 0.3

        # Deep middlegame — this is where humans think hardest
        elif piece_count > 14:
            center = random.uniform(0.8, 2.0)
            sigma = 0.5

        # Endgame (few pieces) — moderate, patterns are clearer
        elif piece_count <= 10:
            center = random.uniform(0.4, 1.0)
            sigma = 0.25

        # Simplified position
        else:
            center = random.uniform(0.5, 1.2)
            sigma = 0.3

        # --- Complexity adjustments ---

        # Many legal moves = complex position = think longer
        if legal_moves > 35:
            center *= 1.4
            sigma *= 1.2
        elif legal_moves > 25:
            center *= 1.15

        # Hanging pieces = tactical situation = think longer (or blitz through)
        if hanging >= 3:
            # Very tactical — either think hard or panic-move
            if random.random() < 0.3:
                center *= 0.5  # Panic fast move
            else:
                center *= 1.6  # Deep think
        elif hanging >= 1:
            center *= 1.2

        # In check — usually quick response
        if metrics["in_check"]:
            if random.random() < 0.5:
                center *= 0.4  # Quick escape
            else:
                center *= 1.3  # Careful think

        # Late game fatigue — slightly erratic timing
        if self._move_number > 40:
            sigma *= 1.5  # More variable timing when "tired"

        # Generate from Gaussian
        delay = _gaussian_delay(center, sigma, min_val=0.1, max_val=8.0)

        return delay

    def should_blunder(self, board=None, metrics=None):
        """
        Decide if the bot should play a suboptimal move.
        Blunder rate scales with position complexity (humans blunder
        more in complex/tactical positions).
        """
        if not self.config.humanizer_enabled:
            return False

        # No blunders in the opening (first 6 moves)
        if self._move_number <= 6:
            return False

        base_chance = self.config.humanizer_blunder_chance

        if board:
            if metrics is None:
                metrics = build_position_metrics(board)
            hanging = metrics["hanging"]
            legal_moves = metrics["legal_move_count"]
            piece_count = metrics["piece_count"]

            # Complex tactical positions — humans blunder more
            if hanging >= 2:
                base_chance *= 1.8
            elif hanging >= 1:
                base_chance *= 1.3

            # Many options — cognitive overload
            if legal_moves > 30:
                base_chance *= 1.4

            # Simple endgame — fewer blunders
            if piece_count <= 8:
                base_chance *= 0.5

        # Fatigue — more blunders in long games
        if self._move_number > 35:
            base_chance *= 1.3
        if self._move_number > 50:
            base_chance *= 1.6

        # Time pressure simulation
        if self._total_think_time > 120:  # "Running low on time"
            base_chance *= 1.5

        roll = random.random()
        if roll < base_chance:
            logger.info(
                "BLUNDER triggered (roll=%.3f < chance=%.3f, move #%d, hanging=%d)",
                roll, base_chance, self._move_number,
                metrics["hanging"] if metrics else 0,
            )
            return True
        return False

    def pick_blunder_move(self, top_moves, board):
        """
        Pick a suboptimal but reasonable move from the engine's top moves.

        Strategy: Usually pick 2nd best (common human error),
        occasionally 3rd best (bigger mistake in complex positions).
        """
        if len(top_moves) <= 1:
            return top_moves[0][0] if top_moves else None

        # Weight towards 2nd best (most common human error)
        if len(top_moves) >= 3 and random.random() < 0.25:
            chosen_idx = 2  # 3rd best — rarer, bigger blunder
        else:
            chosen_idx = 1  # 2nd best — common

        move = top_moves[chosen_idx][0]
        logger.info(
            "Blunder: %s (rank #%d) instead of %s",
            board.san(move), chosen_idx + 1,
            board.san(top_moves[0][0]),
        )
        return move

    def get_engine_time_adjustment(self, board, metrics=None):
        """
        Adjust engine thinking time based on position complexity.

        Returns:
            Multiplier for the base engine time
        """
        if not self.config.humanizer_enabled:
            return 1.0

        if metrics is None:
            metrics = build_position_metrics(board)
        legal_moves = metrics["legal_move_count"]

        if legal_moves <= 3:
            return 0.3   # Forced — minimal think
        if legal_moves <= 10:
            return 0.6
        if legal_moves <= 25:
            return 1.0
        return 1.3        # Complex — think more
