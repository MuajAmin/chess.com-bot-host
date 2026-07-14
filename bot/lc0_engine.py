"""
Lc0 (Leela Chess Zero) engine wrapper.
Uses python-chess UCI interface with CPU-optimized settings.

Key optimization: With Maia weights, uses --nodes=1 (policy network only)
which gives instant human-like moves with near-zero CPU load.
"""

import logging
import chess
import chess.engine

logger = logging.getLogger(__name__)


class Lc0Engine:
    """
    Wrapper around Lc0 chess engine using UCI protocol.

    For Maia weights: Uses nodes=1 (policy-only, no search tree).
    This is optimal because Maia is a human-move prediction model —
    its first suggestion IS the most human-like move, deeper search
    actually makes it LESS human.

    For standard Lc0 weights: Uses configurable nodes/time limit.
    """

    def __init__(self, config):
        self.config = config
        self._engine = None
        self._is_maia = self._detect_maia()

    def _detect_maia(self):
        """Decide whether to use Maia policy-only mode."""
        if self.config.engine_type == "maia":
            logger.info("engine.type=maia; using nodes=1 (policy-only mode).")
            return True
        if self.config.engine_type == "lc0":
            logger.info("engine.type=lc0; using standard time-based search.")
            return False

        # auto: detect from weights filename for backward compatibility.
        weights = self.config.engine_weights.lower()
        is_maia = "maia" in weights
        if is_maia:
            logger.info("Maia weights detected — using nodes=1 (policy-only mode).")
        return is_maia

    async def start(self):
        """Start the Lc0 engine process."""
        logger.info("Starting Lc0 engine...")
        logger.info("  Path:    %s", self.config.engine_path)
        logger.info("  Weights: %s", self.config.engine_weights)
        logger.info("  Backend: %s", self.config.engine_backend)
        logger.info("  Threads: %s", self.config.engine_threads)
        logger.info("  Maia:    %s", self._is_maia)

        try:
            cmd = [
                self.config.engine_path,
                f"--weights={self.config.engine_weights}",
                f"--backend={self.config.engine_backend}",
                f"--threads={self.config.engine_threads}",
            ]

            # Start engine via UCI protocol (async)
            _, self._engine = await chess.engine.popen_uci(cmd)

            # Configure engine options
            options = {}

            # NNCache — limit for RAM savings
            options["NNCacheSize"] = self.config.engine_nn_cache_size

            # For Maia: limit nodes to 1 (policy output only)
            # This gives the most human-like move with near-zero CPU
            if self._is_maia:
                options["MaxCollisionEvents"] = 1

            try:
                await self._engine.configure(options)
            except chess.engine.EngineError as e:
                logger.warning("Some engine options not supported: %s", e)

            logger.info("Lc0 engine started successfully.")
            return True

        except FileNotFoundError:
            logger.error("Lc0 binary not found at: %s", self.config.engine_path)
            return False
        except Exception as e:
            logger.error("Failed to start Lc0 engine: %s", e)
            return False

    async def get_best_move(self, board, time_limit=None):
        """
        Get the best move for the given position.

        For Maia weights: Uses nodes=1 limit (instant, CPU-friendly).
        For standard weights: Uses time-based limit.

        Args:
            board: python-chess Board object
            time_limit: Time in seconds (ignored for Maia, uses nodes instead)

        Returns:
            chess.Move object or None
        """
        if self._engine is None:
            logger.error("Engine not started.")
            return None

        try:
            if self._is_maia:
                # Maia: nodes=1 — policy network prediction only
                # Near-instant, ~0% CPU spike
                limit = chess.engine.Limit(nodes=1)
            else:
                # Standard weights: time-based search
                if time_limit is None:
                    time_limit = self.config.engine_time_per_move
                limit = chess.engine.Limit(time=time_limit)

            result = await self._engine.play(board, limit)
            move = result.move

            if move:
                logger.info("Lc0 move: %s (%s)", board.san(move),
                           "nodes=1" if self._is_maia else f"time={time_limit}s")
            return move

        except chess.engine.EngineTerminatedError:
            logger.error("Engine terminated unexpectedly!")
            return None
        except Exception as e:
            logger.error("Engine error: %s", e)
            return None

    async def get_top_moves(self, board, count=3, time_limit=None):
        """
        Get the top N moves for blunder injection.

        For Maia: Uses nodes=10 (slight search to get alternatives).
        For standard: Uses time-based multi-PV.

        Returns:
            List of (move, score) tuples
        """
        if self._engine is None:
            return []

        try:
            if self._is_maia:
                limit = chess.engine.Limit(nodes=10)
            else:
                if time_limit is None:
                    time_limit = self.config.engine_time_per_move
                limit = chess.engine.Limit(time=time_limit)

            results = []

            with await self._engine.analysis(board, limit, multipv=count) as analysis:
                async for info in analysis:
                    if "pv" in info and info["pv"]:
                        move = info["pv"][0]
                        score = info.get("score")
                        results.append((move, score))
                    if len(results) >= count:
                        break

            return results

        except Exception as e:
            logger.warning("Multi-PV analysis failed, falling back to single move: %s", e)
            best = await self.get_best_move(board, time_limit)
            return [(best, None)] if best else []

    async def close(self):
        """Stop the engine and free resources."""
        if self._engine:
            try:
                await self._engine.quit()
                logger.info("Lc0 engine stopped.")
            except Exception as e:
                logger.warning("Error stopping engine: %s", e)
            finally:
                self._engine = None

    @property
    def is_running(self):
        return self._engine is not None
