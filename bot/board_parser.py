"""
Board parser for chess.com.
Reads the board state using multiple strategies for robustness:
1. Move list replay (MOST RELIABLE — reconstructs from SAN move history)
2. Internal JS game state (stable but can break on bundle updates)
3. data-square attributes on DOM elements
4. CSS class fallback (least reliable but widest compatibility)

Castling rights, en-passant, and move counters are ALL correct when
using Strategy 1 (move replay). Other strategies fall back to inferring
from piece positions.

NOTE: React fiber tree walk is deliberately NOT used here — chess.com's
minified bundle randomizes fiber property names on every deploy.
"""

import logging
import re
import chess

logger = logging.getLogger(__name__)

# Chess.com piece class mapping → python-chess FEN symbols
PIECE_CLASS_MAP = {
    "wp": "P", "wn": "N", "wb": "B", "wr": "R", "wq": "Q", "wk": "K",
    "bp": "p", "bn": "n", "bb": "b", "br": "r", "bq": "q", "bk": "k",
}

# Reverse mapping for data-piece attributes (lowercase single char)
PIECE_CHAR_MAP = {
    "P": "P", "N": "N", "B": "B", "R": "R", "Q": "Q", "K": "K",
    "p": "p", "n": "n", "b": "b", "r": "r", "q": "q", "k": "k",
}

# SAN cleaning regex — strips move numbers, annotations, NAGs
_SAN_CLEAN_RE = re.compile(r'[?!+#]+$')


class BoardParser:
    """
    Parses chess.com board to extract the current position as FEN.

    Uses a layered strategy (priority order):
    1. Move list replay — reads SAN moves from DOM, replays on python-chess Board
       → gives PERFECT FEN including castling, en-passant, halfmove clock
    2. Internal JS game state — reads from chess.com's wc-chess-board component
    3. data-square/data-piece attributes on DOM elements
    4. CSS class parsing (square-XY pattern) — last resort

    Strategy 1 is the only one that gets castling rights and en-passant correct.
    Strategies 2-4 can only determine piece placement (position part of FEN).
    """

    def __init__(self, page):
        self.page = page
        self._our_color = None
        self._last_fen = None
        self._last_board = None  # Cache full Board object from move replay
        self._last_clean_moves = ()
        self._move_count = 0
        self._strategy_used = None

    @property
    def our_color(self):
        return self._our_color

    @property
    def is_white(self):
        return self._our_color == chess.WHITE

    async def wait_for_board_ready(self, timeout_ms=15000):
        """Wait until the playable board is visible enough to parse and click."""
        try:
            await self.page.wait_for_function(
                """
                () => {
                    const board = document.querySelector(
                        'wc-chess-board, chess-board, .board, #board-single'
                    );
                    if (!board) return false;

                    const rect = board.getBoundingClientRect();
                    return rect.width >= 100 && rect.height >= 100;
                }
                """,
                timeout=timeout_ms,
            )
            return True
        except Exception as e:
            logger.warning("Board did not become ready within %.1fs: %s", timeout_ms / 1000, e)
            return False

    async def detect_our_color(self):
        """Detect which color we are playing.

        Uses multiple strategies in order of reliability:
        1. Board coordinate labels (which rank is at bottom)
        2. Piece position on the board (king file positions)
        3. Clock/player panel color classes
        4. Board flipped attribute/class
        5. Default to WHITE (last resort)
        """
        try:
            await self.wait_for_board_ready()

            color_result = await self.page.evaluate("""
                () => {
                    const result = {
                        method: null,
                        color: null,
                        debug: {}
                    };

                    // ── Strategy 0: Game controller API (from Mint.js) ──
                    // Chess.com's wc-chess-board has a .game controller with
                    // getOptions() that returns { isPlayerBlack, flipped, ... }
                    // This is the MOST RELIABLE method — same as Mint.js uses.
                    const wcBoard = document.querySelector('wc-chess-board');
                    if (wcBoard && wcBoard.game) {
                        try {
                            const opts = wcBoard.game.getOptions
                                ? wcBoard.game.getOptions()
                                : null;
                            if (opts) {
                                result.debug.isPlayerBlack = opts.isPlayerBlack;
                                result.debug.isWhiteOnBottom = opts.isWhiteOnBottom;
                                result.debug.flipped = opts.flipped;

                                if (typeof opts.isPlayerBlack === 'boolean') {
                                    result.method = 'game-controller-isPlayerBlack';
                                    result.color = opts.isPlayerBlack ? 'black' : 'white';
                                    return result;
                                }
                            }
                        } catch(e) {
                            result.debug.gameControllerError = e.message;
                        }
                    }

                    // ── Strategy 1: wc-chess-board component orientation ──
                    if (wcBoard) {
                        // Check the 'flipped' property (boolean on the custom element)
                        if (typeof wcBoard.flipped === 'boolean') {
                            result.method = 'wc-chess-board.flipped';
                            result.color = wcBoard.flipped ? 'black' : 'white';
                            result.debug.flippedProp = wcBoard.flipped;
                            return result;
                        }

                        // Check the 'orientation' property
                        const orient = wcBoard.orientation ||
                                       wcBoard.getAttribute('orientation') || '';
                        if (orient) {
                            result.debug.orientation = orient;
                            if (orient.toLowerCase() === 'black') {
                                result.method = 'wc-chess-board.orientation';
                                result.color = 'black';
                                return result;
                            } else if (orient.toLowerCase() === 'white') {
                                result.method = 'wc-chess-board.orientation';
                                result.color = 'white';
                                return result;
                            }
                        }
                    }

                    // ── Strategy 2: Board coordinate labels ──
                    const board = wcBoard || document.querySelector(
                        'chess-board, .board, #board-single'
                    );
                    if (board) {
                        const boardRect = board.getBoundingClientRect();
                        const boardMid = boardRect.top + boardRect.height / 2;

                        const coordEls = board.querySelectorAll(
                            '[class*="coordinate"], [class*="notation"], ' +
                            '.coords-rank text, .coords-rank span, ' +
                            'svg text, .board-coordinates span'
                        );

                        let rank1Y = null, rank8Y = null;
                        for (const el of coordEls) {
                            const txt = (el.textContent || '').trim();
                            const rect = el.getBoundingClientRect();
                            const midY = rect.top + rect.height / 2;
                            if (txt === '1') rank1Y = midY;
                            if (txt === '8') rank8Y = midY;
                        }
                        result.debug.rank1Y = rank1Y;
                        result.debug.rank8Y = rank8Y;

                        if (rank1Y !== null && rank8Y !== null) {
                            if (rank1Y > rank8Y) {
                                result.method = 'coordinates';
                                result.color = 'white';
                                return result;
                            } else {
                                result.method = 'coordinates';
                                result.color = 'black';
                                return result;
                            }
                        }
                    }

                    // ── Strategy 3: Piece positions (king Y comparison) ──
                    if (board) {
                        const pieces = board.querySelectorAll('.piece');
                        let wkY = null, bkY = null;
                        for (const p of pieces) {
                            const cls = (p.className || '').toString();
                            const rect = p.getBoundingClientRect();
                            const midY = rect.top + rect.height / 2;
                            if (cls.includes('wk')) wkY = midY;
                            if (cls.includes('bk')) bkY = midY;
                        }
                        result.debug.wkY = wkY;
                        result.debug.bkY = bkY;

                        if (wkY !== null && bkY !== null) {
                            if (wkY > bkY) {
                                result.method = 'king-position';
                                result.color = 'white';
                                return result;
                            } else {
                                result.method = 'king-position';
                                result.color = 'black';
                                return result;
                            }
                        }
                    }

                    // ── Strategy 4: Board flipped attribute/class ──
                    if (board) {
                        const norm = (v) => (v || '').toString().trim().toLowerCase();
                        const cls = norm(board.getAttribute('class') || board.className);
                        const hasFlipped = board.hasAttribute('flipped');
                        result.debug.boardClasses = cls.substring(0, 200);
                        result.debug.hasFlippedAttr = hasFlipped;

                        if (
                            hasFlipped ||
                            cls.includes('flipped') ||
                            cls.includes('orientation-black') ||
                            cls.includes('black-bottom')
                        ) {
                            result.method = 'board-flipped-class';
                            result.color = 'black';
                            return result;
                        }
                    }

                    // ── No strategy succeeded ──
                    return result;
                }
            """)

            if color_result and color_result.get("color") in ("white", "black"):
                self._our_color = (
                    chess.WHITE if color_result["color"] == "white" else chess.BLACK
                )
                logger.info(
                    "Detected: Playing as %s (method: %s, debug: %s)",
                    "WHITE" if self._our_color == chess.WHITE else "BLACK",
                    color_result.get("method", "unknown"),
                    color_result.get("debug", {}),
                )
                return self._our_color

            # No strategy worked — log diagnostic info and default to WHITE
            logger.warning(
                "Color detection: ALL strategies failed! Debug: %s. "
                "Defaulting to WHITE.",
                color_result.get("debug", {}) if color_result else "no result",
            )
            self._our_color = chess.WHITE
            return chess.WHITE

        except Exception as e:
            logger.warning("Color detection failed, defaulting to WHITE: %s", e)
            self._our_color = chess.WHITE
            return chess.WHITE

    async def get_board_fen(self):
        """
        Parse the board and return FULL FEN string (position + turn +
        castling + en-passant + halfmove + fullmove).

        Tries multiple strategies in order of reliability.
        """
        # Strategy 1: Move list replay (MOST RELIABLE — perfect FEN)
        board = await self._parse_from_move_replay()
        if board is not None:
            self._strategy_used = "move_replay"
            self._last_board = board
            self._last_fen = board.fen()
            return board.fen()

        return await self._get_board_fen_without_replay()

    async def _get_board_fen_without_replay(self):
        """Run only non-replay FEN strategies."""
        # Strategy 2: Internal JS game state
        fen = await self._parse_from_js_state()
        if fen:
            self._strategy_used = "js_state"
            self._last_fen = fen
            return fen

        # Strategy 3: data-square attributes on DOM
        fen = await self._parse_from_data_attributes()
        if fen:
            self._strategy_used = "data_attrs"
            self._last_fen = fen
            return fen

        # Strategy 4: CSS class fallback (square-XY pattern)
        fen = await self._parse_from_css_classes()
        if fen:
            self._strategy_used = "css_classes"
            self._last_fen = fen
            return fen

        start_board = await self._maybe_starting_board()
        if start_board is not None:
            self._strategy_used = "initial_position"
            self._last_board = start_board
            self._last_fen = start_board.fen()
            return self._last_fen

        await self._log_board_parse_snapshot()
        logger.warning("All board parsing strategies failed!")
        return self._last_fen

    async def _maybe_starting_board(self):
        """
        Use the normal starting position when a new white game has no moves yet.

        Chess.com often renders an empty move list before White's first move.
        Move replay is still the primary strategy after the first ply appears.
        """
        if self._last_clean_moves or self._move_count > 0:
            return None
        if self._our_color != chess.WHITE:
            return None
        if not await self._has_visible_game_board():
            return None

        turn = await self._detect_turn()
        if turn != chess.WHITE:
            return None

        board = chess.Board()
        logger.info("No move history yet; using standard starting position for White's first move.")
        return board

    async def _has_visible_game_board(self):
        try:
            return bool(await self.page.evaluate("""
                () => {
                    const board = document.querySelector(
                        'wc-chess-board, chess-board, .board, #board-single'
                    );
                    if (!board) return false;
                    const rect = board.getBoundingClientRect();
                    return rect.width >= 100 && rect.height >= 100;
                }
            """))
        except Exception:
            return False

    async def _log_board_parse_snapshot(self):
        try:
            snapshot = await self.page.evaluate("""
                () => {
                    const count = (selector) => document.querySelectorAll(selector).length;
                    const board = document.querySelector(
                        'wc-chess-board, chess-board, .board, #board-single'
                    );
                    const boardClass = board
                        ? ((board.getAttribute('class') || board.className || '').toString()).substring(0, 160)
                        : '';
                    return {
                        url: location.href,
                        boardFound: !!board,
                        boardClass,
                        pieceCount: count('.piece'),
                        dataSquareCount: count('[data-square]'),
                        dataPlyCount: count('[data-ply]'),
                        moveTextCount: count('.move-text-component, .move-node, .move-text'),
                    };
                }
            """)
            logger.warning("Board parse snapshot: %s", snapshot)
        except Exception as e:
            logger.debug("Board parse snapshot failed: %s", e)

    async def _parse_from_move_replay(self):
        """
        Strategy 1 (PRIMARY): Read SAN moves from chess.com's move list,
        replay them on a python-chess Board to reconstruct the exact game state.

        This is the MOST RELIABLE strategy because:
        - Move list DOM uses semantic elements that rarely change
        - Replaying gives PERFECT castling rights, en-passant, halfmove clock
        - Class-independent — doesn't depend on obfuscated CSS class names
        """
        try:
            # Extract SAN moves from chess.com's move list
            move_data = await self.page.evaluate("""
                () => {
                    const moves = [];

                    // Helper: extract only the SAN move text from a node,
                    // excluding nested time/clock elements (e.g. <span>1s</span>)
                    function getMoveText(node) {
                        function withFigurine(root, value) {
                            let text = (value || '').trim();
                            const figurine = root.querySelector('[data-figurine]');
                            const piece = figurine ? figurine.getAttribute('data-figurine') : '';
                            if (piece && !text.startsWith(piece)) {
                                text = piece + text;
                            }
                            return text.replace(/\\s+/g, '');
                        }

                        // Strategy: try to find a dedicated move-text child first
                        const moveSpan = node.querySelector(
                            '.node-highlight-content, [data-cy="move-san"], .move-san'
                        );
                        if (moveSpan) {
                            return withFigurine(moveSpan, moveSpan.textContent);
                        }

                        // Fallback: collect only direct text nodes (skip child elements
                        // which are often clock/time displays like "1s", "0.3s")
                        let directText = '';
                        for (const child of node.childNodes) {
                            if (child.nodeType === Node.TEXT_NODE) {
                                directText += child.textContent;
                            }
                        }
                        directText = directText.trim();
                        if (directText) return withFigurine(node, directText);

                        // Last resort: full textContent but strip time suffixes
                        let text = node.textContent.trim();
                        // Remove embedded time notations like " 1s", " 0.3s", " 12.5s"
                        text = text.replace(/\\s+\\d+\\.?\\d*s\\b/gi, '').trim();
                        return withFigurine(node, text);
                    }

                    // Method A: data-ply attribute elements (most reliable)
                    const plyNodes = document.querySelectorAll('[data-ply]');
                    if (plyNodes.length > 0) {
                        // Sort by ply number for correct order
                        const sorted = Array.from(plyNodes).sort(
                            (a, b) => parseInt(a.getAttribute('data-ply')) -
                                      parseInt(b.getAttribute('data-ply'))
                        );
                        for (const node of sorted) {
                            const text = getMoveText(node);
                            if (text && text !== '...' && !/^\\d+\\.$/.test(text)) {
                                moves.push(text);
                            }
                        }
                        if (moves.length > 0) return { source: 'data-ply', moves };
                    }

                    // Method B: move-text / move-node class elements
                    const moveNodes = document.querySelectorAll(
                        '.move-text-component, .move-node, .move-text'
                    );
                    for (const node of moveNodes) {
                        const text = getMoveText(node);
                        // Filter out move numbers (e.g., "1.", "2.")
                        if (text && !/^\\d+\\.?$/.test(text) && text !== '...') {
                            moves.push(text);
                        }
                    }
                    if (moves.length > 0) return { source: 'move-class', moves };

                    // Method C: Vertical move list (alternative layout)
                    const vertNodes = document.querySelectorAll(
                        '.vertical-move-list .move, [class*="move-list"] [class*="move"]'
                    );
                    for (const node of vertNodes) {
                        // Each move row might contain both white and black moves
                        const moveTexts = node.querySelectorAll(
                            '[class*="white"], [class*="black"], .move-text'
                        );
                        for (const mt of moveTexts) {
                            const text = getMoveText(mt);
                            if (text && !/^\\d+\\.?$/.test(text)) {
                                moves.push(text);
                            }
                        }
                    }
                    if (moves.length > 0) return { source: 'vertical', moves };

                    return null;
                }
            """)

            if not move_data or not move_data.get("moves"):
                logger.debug("Move list replay: no moves found in DOM")
                return None

            raw_moves = move_data["moves"]
            source = move_data["source"]
            clean_moves = tuple(
                san for san in (self._clean_san(raw) for raw in raw_moves)
                if san
            )
            if not clean_moves:
                # Log what was extracted but rejected — helps diagnose DOM issues
                if raw_moves:
                    logger.warning(
                        "Move list replay: %d raw moves extracted but ALL rejected "
                        "by _clean_san. Raw: %s",
                        len(raw_moves), raw_moves[:10],
                    )
                return None

            # Log when some moves got filtered (potential parsing issue)
            if len(clean_moves) != len(raw_moves):
                logger.debug(
                    "Move list replay: %d/%d raw moves survived cleaning. "
                    "Raw: %s → Clean: %s",
                    len(clean_moves), len(raw_moves),
                    raw_moves[:10], clean_moves[:10],
                )

            if self._last_board is not None and clean_moves == self._last_clean_moves:
                return self._last_board

            logger.debug(
                "Move list replay: found %d moves (source: %s)",
                len(clean_moves), source,
            )

            if (
                self._last_board is not None and
                self._last_clean_moves and
                clean_moves[:len(self._last_clean_moves)] == self._last_clean_moves
            ):
                board = self._last_board.copy(stack=True)
                start_index = len(self._last_clean_moves)
            else:
                board = chess.Board()
                start_index = 0

            for i, san in enumerate(clean_moves[start_index:], start=start_index):
                try:
                    board.push_san(san)
                except (chess.IllegalMoveError, chess.InvalidMoveError,
                        chess.AmbiguousMoveError) as e:
                    logger.warning(
                        "Move replay: invalid move '%s' at index %d: %s",
                        san, i, e,
                    )
                    # If we fail mid-replay, return what we have so far
                    # (better than nothing — at least castling rights are partially correct)
                    if board.move_stack:
                        logger.info(
                            "Move replay: partial replay (%d/%d moves applied)",
                            len(board.move_stack), len(clean_moves),
                        )
                        return board
                    return None

            self._last_board = board
            self._last_clean_moves = clean_moves
            self._move_count = len(clean_moves)
            logger.debug(
                "Move replay: full board reconstructed (%d moves, FEN: %s)",
                len(board.move_stack), board.fen()[:50],
            )
            return board

        except Exception as e:
            logger.debug("Move list replay failed: %s", e)
            return None

    # Regex to detect time/clock notations: "1s", "0.3s", "12s", "1.5s", etc.
    _TIME_NOTATION_RE = re.compile(r'^\d+\.?\d*s$', re.IGNORECASE)

    # Regex to validate that a cleaned string looks like a plausible SAN move.
    # Matches: e4, Nf3, Qxf7+, O-O, O-O-O#, Bxe5#, exd5, R1a3, etc.
    _PLAUSIBLE_SAN_RE = re.compile(
        r'^(?:O-O(?:-O)?[+#]?|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8](?:=[QRBN])?[+#]?)$'
    )

    def _clean_san(self, raw):
        """
        Clean a raw SAN string from chess.com DOM.

        Handles:
        - Move numbers: "1." "2." "12."
        - Annotations: "!!" "??" "!?" "?!"
        - Check/checkmate: "+" "#"
        - Ellipsis: "..." (black's move indicator)
        - Whitespace and newlines
        - NAG symbols
        - Time notations: "1s", "0.3s", "12s" (chess.com clock text)
        - Result strings: "1-0", "0-1", "½-½", "1/2-1/2", "*"
        - Pure numbers: "1", "23"
        """
        if not raw:
            return None

        san = raw.strip()

        # Skip pure move numbers ("1.", "2.", "12.")
        if re.match(r'^\d+\.+$', san):
            return None

        # Skip ellipsis (used to indicate black's move)
        if san in ('...', '…', '..'):
            return None

        # Skip time/clock notations ("1s", "0.3s", "12s", "1.5s")
        if self._TIME_NOTATION_RE.match(san):
            return None

        # Skip pure numbers ("1", "23") — sometimes clock seconds without suffix
        if re.match(r'^\d+$', san):
            return None

        # Skip game result strings
        if san in ('1-0', '0-1', '½-½', '1/2-1/2', '*'):
            return None

        # Remove embedded time notations from combined text
        # e.g. "e4 1s" → "e4", "Nf3 0.3s" → "Nf3"
        san = re.sub(r'\s+\d+\.?\d*s\b', '', san, flags=re.IGNORECASE)

        # Remove leading move number if embedded ("1.e4" → "e4", "12.Nf3" → "Nf3")
        san = re.sub(r'^\d+\.+\s*', '', san)

        # Remove trailing annotations but KEEP check/checkmate markers
        # "Nf3!!" → "Nf3", "Qxf7#" stays, "e4+" stays
        san = re.sub(r'[?!]+$', '', san)

        # Remove any remaining whitespace
        san = san.strip()

        if not san:
            return None

        # Final validation: reject anything that doesn't look like a chess move
        if not self._PLAUSIBLE_SAN_RE.match(san):
            logger.debug("Rejecting non-SAN text from move list: '%s'", san)
            return None

        return san

    async def _parse_from_js_state(self):
        """
        Strategy 2: Read position from chess.com's internal JS game state.
        Reads the data model directly — survives most DOM changes.

        NOTE: No fiber tree walk — chess.com's minified bundle randomizes
        fiber property names. Only uses documented/stable API surfaces.
        """
        try:
            fen = await self.page.evaluate("""
                () => {
                    // Method A: wc-chess-board component's game property
                    const board = document.querySelector('wc-chess-board');
                    if (board) {
                        // Try the component's internal game/position property
                        if (board.game && board.game.getFEN) {
                            return board.game.getFEN();
                        }
                        if (board.game && board.game.fen) {
                            return typeof board.game.fen === 'function'
                                ? board.game.fen()
                                : board.game.fen;
                        }
                        // Try position property
                        if (board.position) {
                            return typeof board.position === 'function'
                                ? board.position()
                                : board.position;
                        }
                    }

                    // Method B: Global game objects chess.com sometimes exposes
                    if (typeof window.game !== 'undefined' && window.game) {
                        if (window.game.getFEN) return window.game.getFEN();
                        if (window.game.fen) return window.game.fen();
                    }

                    // Method C: Check for LiveChess or similar global objects
                    if (typeof window.LiveChess !== 'undefined') {
                        try {
                            const lc = window.LiveChess;
                            if (lc.currentGame && lc.currentGame.getFEN) {
                                return lc.currentGame.getFEN();
                            }
                        } catch(e) {}
                    }

                    return null;
                }
            """)

            if fen and "/" in fen:
                logger.debug("JS state FEN: %s", fen[:60])
                # If it's a full FEN (with turn, castling etc.), return as-is
                if " " in fen:
                    return fen
                # Position-only FEN — need to infer turn
                turn = await self._detect_turn()
                # Without move replay, we can't know castling rights precisely
                # Use conservative "all castling available" as fallback
                return f"{fen} {'w' if turn == chess.WHITE else 'b'} KQkq - 0 1"

            return None

        except Exception as e:
            logger.debug("JS state parsing failed: %s", e)
            return None

    async def _parse_from_data_attributes(self):
        """
        Strategy 3: Read pieces from data-square and data-piece attributes.
        More stable than CSS classes since data attributes are semantic.
        """
        try:
            pieces = await self.page.evaluate("""
                () => {
                    const result = [];

                    // Try data-square attribute on piece elements
                    const pieceEls = document.querySelectorAll('[data-square]');
                    for (const el of pieceEls) {
                        const square = el.getAttribute('data-square');
                        const piece = el.getAttribute('data-piece');
                        if (square && piece) {
                            result.push({ square, piece, source: 'data-attr' });
                        }
                    }

                    if (result.length > 0) return result;

                    // Try pieces inside squares with data-square
                    const squares = document.querySelectorAll('[data-square]');
                    for (const sq of squares) {
                        const squareName = sq.getAttribute('data-square');
                        const pieceEl = sq.querySelector('[data-piece], .piece');
                        if (pieceEl) {
                            const piece = pieceEl.getAttribute('data-piece') ||
                                          pieceEl.getAttribute('data-type');
                            if (squareName && piece) {
                                result.push({ square: squareName, piece, source: 'nested' });
                            }
                        }
                    }

                    return result;
                }
            """)

            if not pieces or len(pieces) < 2:
                return None

            position = self._build_fen_from_named_squares(pieces)
            if position:
                turn = await self._detect_turn()
                castling = self._infer_castling_from_position(position)
                return f"{position} {'w' if turn == chess.WHITE else 'b'} {castling} - 0 1"
            return None

        except Exception as e:
            logger.debug("Data attribute parsing failed: %s", e)
            return None

    async def _parse_from_css_classes(self):
        """
        Strategy 4: Read pieces from CSS class names (square-XY pattern).
        Least reliable but most commonly available.
        """
        try:
            pieces = await self.page.evaluate("""
                () => {
                    const result = [];
                    const els = document.querySelectorAll('.piece');

                    for (const el of els) {
                        const classes = el.className.split(/\\s+/);
                        let pieceType = null;
                        let squareCoords = null;

                        for (const cls of classes) {
                            // Piece type: wp, wn, wb, wr, wq, wk, bp, bn, bb, br, bq, bk
                            if (/^[wb][pnbrqk]$/.test(cls)) {
                                pieceType = cls;
                            }
                            // Square: square-XY (1-indexed file, rank)
                            const sqMatch = cls.match(/^square-(\\d)(\\d)$/);
                            if (sqMatch) {
                                squareCoords = { file: parseInt(sqMatch[1]), rank: parseInt(sqMatch[2]) };
                            }
                            // Alternative: square-XY with 2+ digits (e.g., square-108 for h8 on 10x10?)
                            // Standard chess is always single digits 1-8
                        }

                        if (pieceType && squareCoords) {
                            result.push({ piece: pieceType, coords: squareCoords });
                        }
                    }
                    return result;
                }
            """)

            if not pieces or len(pieces) < 2:
                return None

            position = self._build_fen_from_coords(pieces)
            if position:
                turn = await self._detect_turn()
                castling = self._infer_castling_from_position(position)
                return f"{position} {'w' if turn == chess.WHITE else 'b'} {castling} - 0 1"
            return None

        except Exception as e:
            logger.debug("CSS class parsing failed: %s", e)
            return None

    def _infer_castling_from_position(self, position_fen):
        """
        Infer castling rights from piece positions.
        If king/rook are NOT on starting squares, that castling option is removed.
        This is a best-effort heuristic — only move replay gives perfect rights.
        """
        # Parse position to find king and rook locations
        rows = position_fen.split("/")
        if len(rows) != 8:
            return "KQkq"  # fallback

        # Expand FEN rows to 8-char strings
        def expand_row(row):
            expanded = ""
            for ch in row:
                if ch.isdigit():
                    expanded += "." * int(ch)
                else:
                    expanded += ch
            return expanded

        expanded = [expand_row(r) for r in rows]
        # rows[0] = rank 8, rows[7] = rank 1

        castling = ""

        # White: King on e1 (row 7, col 4), Rooks on a1 (row 7, col 0) and h1 (row 7, col 7)
        rank1 = expanded[7] if len(expanded) > 7 else ""
        if len(rank1) >= 8:
            if rank1[4] == 'K':  # White king on e1
                if rank1[7] == 'R':  # White rook on h1
                    castling += "K"
                if rank1[0] == 'R':  # White rook on a1
                    castling += "Q"

        # Black: King on e8 (row 0, col 4), Rooks on a8 (row 0, col 0) and h8 (row 0, col 7)
        rank8 = expanded[0] if len(expanded) > 0 else ""
        if len(rank8) >= 8:
            if rank8[4] == 'k':  # Black king on e8
                if rank8[7] == 'r':  # Black rook on h8
                    castling += "k"
                if rank8[0] == 'r':  # Black rook on a8
                    castling += "q"

        return castling if castling else "-"

    def _build_fen_from_coords(self, pieces):
        """Build FEN position from coordinate-based piece data (CSS class strategy)."""
        board_array = [[None for _ in range(8)] for _ in range(8)]

        for p in pieces:
            piece_code = p["piece"]
            coords = p["coords"]

            file_idx = coords["file"] - 1  # 1-8 → 0-7
            rank_idx = coords["rank"] - 1   # 1-8 → 0-7

            if 0 <= file_idx <= 7 and 0 <= rank_idx <= 7:
                fen_piece = PIECE_CLASS_MAP.get(piece_code)
                if fen_piece:
                    board_array[7 - rank_idx][file_idx] = fen_piece

        return self._array_to_fen(board_array)

    def _build_fen_from_named_squares(self, pieces):
        """Build FEN position from algebraic square names (data-attribute strategy)."""
        board_array = [[None for _ in range(8)] for _ in range(8)]

        for p in pieces:
            square_name = p["square"]  # e.g., "e4"
            piece_code = p["piece"]     # e.g., "wk" or "K"

            if len(square_name) != 2:
                continue

            file_char = square_name[0].lower()
            rank_char = square_name[1]

            if file_char < 'a' or file_char > 'h' or rank_char < '1' or rank_char > '8':
                continue

            file_idx = ord(file_char) - ord('a')  # 0-7
            rank_idx = int(rank_char) - 1          # 0-7

            # Map piece code to FEN character
            fen_piece = PIECE_CLASS_MAP.get(piece_code)
            if not fen_piece:
                fen_piece = PIECE_CHAR_MAP.get(piece_code)
            if fen_piece:
                board_array[7 - rank_idx][file_idx] = fen_piece

        return self._array_to_fen(board_array)

    def _array_to_fen(self, board_array):
        """Convert 8x8 board array to FEN position string."""
        fen_rows = []
        for row in board_array:
            fen_row = ""
            empty_count = 0
            for cell in row:
                if cell is None:
                    empty_count += 1
                else:
                    if empty_count > 0:
                        fen_row += str(empty_count)
                        empty_count = 0
                    fen_row += cell
            if empty_count > 0:
                fen_row += str(empty_count)
            fen_rows.append(fen_row)

        fen = "/".join(fen_rows)

        # Sanity check: FEN should have 8 rows
        if len(fen_rows) != 8:
            logger.warning("FEN has %d rows instead of 8!", len(fen_rows))
            return None

        return fen

    async def get_full_board(self):
        """
        Get a python-chess Board object representing the current position.

        Tries strategies in order:
        0. Game controller getFEN() (from Mint.js — most reliable)
        1. Move replay (reconstructs from SAN move history)
        2. FEN from other DOM strategies
        """
        # Strategy 0: Game controller API (Mint.js approach)
        try:
            fen = await self.page.evaluate("""
                () => {
                    const board = document.querySelector('wc-chess-board');
                    if (board && board.game && board.game.getFEN) {
                        return board.game.getFEN();
                    }
                    return null;
                }
            """)
            if fen and "/" in fen and " " in fen:
                try:
                    board = chess.Board(fen)
                    if board.is_valid():
                        self._strategy_used = "game_controller"
                        self._last_board = board
                        self._last_fen = fen
                        return board
                except Exception:
                    pass
        except Exception:
            pass

        # Strategy 1: Move replay — gives us a perfect Board object
        board = await self._parse_from_move_replay()
        if board is not None:
            self._strategy_used = "move_replay"
            self._last_board = board
            self._last_fen = board.fen()
            return board

        # Fallback to FEN-based parsing without retrying move replay.
        fen = await self._get_board_fen_without_replay()
        if fen is None:
            return None

        try:
            board = chess.Board(fen)
            return board
        except Exception as e:
            logger.error("Failed to create Board from FEN '%s': %s", fen, e)
            return None

    async def _detect_turn(self):
        """Detect whose turn it is using multiple methods."""
        try:
            # Method 0: Game controller API (Mint.js approach)
            # getTurn() returns 1 (White) or 2 (Black)
            # getFEN() returns the full FEN — turn is the 2nd field
            turn_result = await self.page.evaluate("""
                () => {
                    const board = document.querySelector('wc-chess-board');
                    if (board && board.game) {
                        // getTurn() returns 1=White, 2=Black
                        if (board.game.getTurn) {
                            const t = board.game.getTurn();
                            if (t === 1) return 'w';
                            if (t === 2) return 'b';
                        }
                        // Fallback: parse turn from FEN
                        if (board.game.getFEN) {
                            const fen = board.game.getFEN();
                            if (fen) {
                                const parts = fen.split(' ');
                                if (parts.length >= 2) return parts[1];
                            }
                        }
                    }
                    return null;
                }
            """)
            if turn_result == 'w':
                return chess.WHITE
            elif turn_result == 'b':
                return chess.BLACK

            # Method 1: Reuse SAN replay when possible.
            board = await self._parse_from_move_replay()
            if board is not None:
                return board.turn

            # Method 2: Check active clock via JS.
            turn_data = await self.page.evaluate("""
                () => {
                    // Check which clock has the 'active' or 'running' indicator
                    const clocks = document.querySelectorAll(
                        '[class*="clock"]'
                    );

                    let bottomActive = false;
                    let topActive = false;

                    for (const clock of clocks) {
                        const cls = (clock.className || '').toString();
                        const isActive = cls.includes('active') ||
                                        cls.includes('running') ||
                                        cls.includes('player-turn');

                        if (!isActive) continue;

                        // Determine if this clock is top or bottom
                        const rect = clock.getBoundingClientRect();
                        const boardEl = document.querySelector('wc-chess-board, .board');
                        if (boardEl) {
                            const boardRect = boardEl.getBoundingClientRect();
                            const boardMid = boardRect.top + boardRect.height / 2;
                            if (rect.top > boardMid) {
                                bottomActive = true;
                            } else {
                                topActive = true;
                            }
                        }
                    }

                    return { bottomActive, topActive };
                }
            """)

            if turn_data:
                if turn_data.get("bottomActive"):
                    return self._our_color
                elif turn_data.get("topActive"):
                    return chess.BLACK if self._our_color == chess.WHITE else chess.WHITE

            # Method 3: Count only text that looks like an actual move.
            raw_moves = await self.page.evaluate("""
                () => {
                    const collect = (nodes) => {
                        const moves = [];
                        for (const node of nodes) {
                            const text = (node.textContent || '').trim();
                            if (text && text !== '...' && !/^\\d+\\.?$/.test(text)) {
                                moves.push(text);
                            }
                        }
                        return moves;
                    };

                    const plyNodes = document.querySelectorAll('[data-ply]');
                    if (plyNodes.length > 0) {
                        return collect(Array.from(plyNodes).sort(
                            (a, b) => parseInt(a.getAttribute('data-ply')) -
                                      parseInt(b.getAttribute('data-ply'))
                        ));
                    }

                    return collect(document.querySelectorAll(
                        '.move-text-component, .move-node, .move-text'
                    ));
                }
            """)

            if raw_moves is not None:
                move_count = len([
                    san for san in (self._clean_san(raw) for raw in raw_moves)
                    if san
                ])
                return chess.WHITE if move_count % 2 == 0 else chess.BLACK

            return chess.WHITE

        except Exception as e:
            logger.warning("Turn detection failed: %s", e)
            return chess.WHITE

    async def is_our_turn(self):
        """Check if it's currently our turn to move."""
        turn = await self._detect_turn()
        return turn == self._our_color

    async def get_remaining_time(self):
        """
        Read remaining time from chess.com's clock elements.

        Returns:
            dict with 'our_time' and 'opp_time' in seconds, or None on failure.
        """
        try:
            clock_data = await self.page.evaluate("""
                () => {
                    function parseClockText(text) {
                        if (!text) return null;
                        text = text.trim();

                        // Format: "M:SS" or "H:MM:SS" or "S.d" (for <10 seconds)
                        const parts = text.split(':');
                        if (parts.length === 2) {
                            // M:SS
                            const mins = parseInt(parts[0]) || 0;
                            const secs = parseFloat(parts[1]) || 0;
                            return mins * 60 + secs;
                        } else if (parts.length === 3) {
                            // H:MM:SS
                            const hrs = parseInt(parts[0]) || 0;
                            const mins = parseInt(parts[1]) || 0;
                            const secs = parseFloat(parts[2]) || 0;
                            return hrs * 3600 + mins * 60 + secs;
                        } else if (parts.length === 1) {
                            // Just seconds (possibly with decimal)
                            return parseFloat(text) || null;
                        }
                        return null;
                    }

                    const clockEls = document.querySelectorAll(
                        '.clock-component, [class*="clock-time"], .clock-time-monospace'
                    );

                    const clocks = [];
                    const boardEl = document.querySelector('wc-chess-board, .board');
                    const boardRect = boardEl ? boardEl.getBoundingClientRect() : null;
                    const boardMid = boardRect ? boardRect.top + boardRect.height / 2 : 0;

                    for (const el of clockEls) {
                        const text = el.textContent;
                        const seconds = parseClockText(text);
                        if (seconds === null) continue;

                        const rect = el.getBoundingClientRect();
                        const isBottom = boardRect ? rect.top > boardMid : false;

                        clocks.push({ seconds, isBottom, text });
                    }

                    if (clocks.length < 2) return null;

                    // Bottom clock = our clock, Top clock = opponent's clock
                    const bottom = clocks.find(c => c.isBottom);
                    const top = clocks.find(c => !c.isBottom);

                    if (bottom && top) {
                        return { our_time: bottom.seconds, opp_time: top.seconds };
                    }

                    return null;
                }
            """)

            if clock_data:
                logger.debug(
                    "Clock: our=%.1fs, opp=%.1fs",
                    clock_data["our_time"], clock_data["opp_time"],
                )
                return clock_data

            return None

        except Exception as e:
            logger.debug("Clock reading failed: %s", e)
            return None

    async def detect_time_control(self):
        """
        Detect the time control of the current game (base time + increment).

        Returns:
            dict with 'base_time' (seconds) and 'increment' (seconds), or None.
        """
        try:
            tc_data = await self.page.evaluate("""
                () => {
                    // Method 1: Look for time control text in game info
                    const tcSelectors = [
                        '[class*="time-control"]',
                        '[class*="game-time"]',
                        '.time-selector-component',
                    ];

                    for (const sel of tcSelectors) {
                        const el = document.querySelector(sel);
                        if (el) {
                            const text = el.textContent.trim();
                            // Parse "3|0", "5|3", "10|0", "15|10", "3+0", "5+3"
                            const match = text.match(/(\\d+)\\s*[|+]\\s*(\\d+)/);
                            if (match) {
                                return {
                                    base_time: parseInt(match[1]) * 60,
                                    increment: parseInt(match[2])
                                };
                            }
                        }
                    }

                    // Method 2: Infer from initial clock values
                    const clockEls = document.querySelectorAll(
                        '.clock-component, [class*="clock-time"]'
                    );
                    if (clockEls.length >= 2) {
                        const times = [];
                        for (const el of clockEls) {
                            const text = el.textContent.trim();
                            const parts = text.split(':');
                            if (parts.length === 2) {
                                const mins = parseInt(parts[0]) || 0;
                                const secs = parseInt(parts[1]) || 0;
                                times.push(mins * 60 + secs);
                            }
                        }
                        if (times.length >= 2) {
                            const maxTime = Math.max(...times);
                            // Can't determine increment from clocks alone
                            return { base_time: maxTime, increment: 0 };
                        }
                    }

                    return null;
                }
            """)

            if tc_data:
                logger.info(
                    "Time control detected: %d+%d",
                    tc_data["base_time"] // 60, tc_data["increment"],
                )
            return tc_data

        except Exception as e:
            logger.debug("Time control detection failed: %s", e)
            return None

    async def get_last_opponent_move(self):
        """Get the last move played by reading highlighted squares."""
        try:
            highlights = await self.page.evaluate("""
                () => {
                    const result = [];
                    // Try highlight elements
                    const els = document.querySelectorAll(
                        '.highlight, [class*="highlight"]'
                    );
                    for (const el of els) {
                        // Check data-square attribute first
                        const sq = el.getAttribute('data-square');
                        if (sq) {
                            result.push(sq);
                            continue;
                        }
                        // Fallback to class parsing
                        const classes = el.className.split(/\\s+/);
                        for (const cls of classes) {
                            const match = cls.match(/^square-(\\d)(\\d)$/);
                            if (match) {
                                const file = String.fromCharCode(96 + parseInt(match[1]));
                                const rank = match[2];
                                result.push(file + rank);
                            }
                        }
                    }
                    return result;
                }
            """)

            if highlights and len(highlights) >= 2:
                return highlights
            return None

        except Exception as e:
            logger.warning("Could not detect last move: %s", e)
            return None
