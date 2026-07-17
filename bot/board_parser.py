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
import asyncio
import re
import time
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

    def __init__(self, page, username=""):
        self.page = page
        self.username = username or ""
        self._detected_username = ""
        self._our_color = None
        self._bot_color = None
        self._bot_side = None
        self._board_orientation = None
        self._last_fen = None
        self._last_board = None  # Cache full Board object from move replay
        self._last_clean_moves = ()
        self._move_count = 0
        self._strategy_used = None

    @property
    def our_color(self):
        return self._our_color

    @property
    def bot_color(self):
        return self._bot_color

    @property
    def bot_side(self):
        return self._bot_side

    @property
    def is_white(self):
        return self._our_color == chess.WHITE

    @property
    def board_orientation(self):
        return self._board_orientation

    @property
    def is_board_white_bottom(self):
        if self._board_orientation is None:
            return self.is_white
        return self._board_orientation == chess.WHITE

    def _set_colors(self, bot_color=None, board_orientation=None, bot_side=None):
        if bot_color in ("white", "black"):
            self._bot_color = bot_color
            self._our_color = chess.WHITE if bot_color == "white" else chess.BLACK

        if board_orientation in ("white", "black"):
            self._board_orientation = (
                chess.WHITE if board_orientation == "white" else chess.BLACK
            )

        if bot_side in ("top", "bottom"):
            self._bot_side = bot_side

    def _color_name(self, color):
        if color == chess.WHITE:
            return "white"
        if color == chess.BLACK:
            return "black"
        return "unknown"

    def _opposite_color(self, color):
        if color == chess.WHITE:
            return chess.BLACK
        if color == chess.BLACK:
            return chess.WHITE
        return None

    def _current_bot_side(self):
        if self._bot_side in ("top", "bottom"):
            return self._bot_side
        if self._our_color is not None and self._board_orientation is not None:
            return "bottom" if self._our_color == self._board_orientation else "top"
        return None

    def force_bot_color(self, bot_color, reason="manual override"):
        """Force the bot color and keep board-side dependent consumers in sync."""
        if bot_color not in ("white", "black"):
            return False

        previous = self._bot_color or self._color_name(self._our_color)
        self._set_colors(bot_color=bot_color)

        if self._board_orientation is not None:
            self._bot_side = (
                "bottom" if self._our_color == self._board_orientation else "top"
            )

        logger.warning(
            "Switching bot color from %s to %s (%s). Board orientation: %s bottom; bot side: %s",
            previous,
            bot_color,
            reason,
            self._color_name(self._board_orientation),
            self._bot_side or "unknown",
        )
        return True

    async def recover_color_after_invalid_move(self, move, board=None, controller_result=None):
        """
        Recover from an invalid first move caused by a wrong color decision.

        This deliberately does not flip color for every failed click. It reacts
        when Chess.com's controller rejects the UCI move, or when the controller
        accepts it but the board still does not change. Blind color flipping is
        limited to the opening where a wrong initial color is the realistic
        failure mode.
        """
        result = controller_result or {}
        reason = (result.get("reason") or "").lower()
        controller_ok_without_position_change = result.get("ok") is True
        controller_rejected_as_illegal = "not in legal moves" in reason

        if not controller_rejected_as_illegal and not controller_ok_without_position_change:
            return False

        current = self._bot_color or self._color_name(self._our_color)
        if current not in ("white", "black"):
            return False

        move_uci = move.uci() if move else "unknown"
        trusted_methods = {
            "game.getOptions().isPlayerBlack",
            "game.getOptions().playing-color",
            "game.getPlayingAs()",
        }

        snapshot = await self._read_game_controller_snapshot()
        if snapshot and snapshot.get("color") in ("white", "black"):
            method = snapshot.get("colorMethod")
            page_color = snapshot["color"]
            if method in trusted_methods and page_color != current:
                self._set_colors(
                    bot_color=page_color,
                    board_orientation=snapshot.get("orientation"),
                    bot_side=snapshot.get("botSide"),
                )
                if self._board_orientation is not None and self._bot_side is None:
                    self._bot_side = (
                        "bottom" if self._our_color == self._board_orientation else "top"
                    )
                logger.warning(
                    "Recovered bot color from controller after invalid move %s: %s -> %s",
                    move_uci,
                    current,
                    page_color,
                )
                return True

        if board is not None and (board.fullmove_number > 2 or self._move_count > 4):
            logger.warning(
                "Move %s failed after controller fallback, but not flipping color after opening. "
                "Controller result: %s",
                move_uci,
                result,
            )
            return False

        next_color = "black" if current == "white" else "white"
        reason_text = (
            result.get("reason") or
            "controller accepted move but board position did not change"
        )
        return self.force_bot_color(
            next_color,
            f"failed opening move {move_uci}: {reason_text}",
        )

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

    async def _read_game_controller_snapshot(self):
        """
        Read Chess.com's board controller state using the same API surface Mint.js uses.

        Mint.js initializes from either WC-CHESS-BOARD or CHESS-BOARD and then reads
        chessboard.game.getOptions().isPlayerBlack. Query both tags here so black
        games do not fall through to the old WHITE default when only chess-board is
        present.
        """
        try:
            return await self.page.evaluate("""
                (botUsername) => {
                    const result = {
                        selector: null,
                        tagName: null,
                        color: null,
                        colorMethod: null,
                        orientation: null,
                        orientationMethod: null,
                        botUsername: null,
                        botSide: null,
                        topPlayer: null,
                        bottomPlayer: null,
                        turn: null,
                        fen: null,
                        debug: {}
                    };

                    const selectors = [
                        'wc-chess-board',
                        'chess-board',
                        '.board',
                        '#board-single'
                    ];
                    const seen = new Set();
                    const candidates = [];

                    for (const selector of selectors) {
                        for (const el of document.querySelectorAll(selector)) {
                            if (!el || seen.has(el)) continue;
                            seen.add(el);

                            const rect = el.getBoundingClientRect();
                            const visible = rect.width >= 100 && rect.height >= 100;
                            candidates.push({
                                el,
                                selector,
                                tagName: el.tagName,
                                visible,
                                area: rect.width * rect.height,
                                hasGame: !!el.game
                            });
                        }
                    }

                    candidates.sort((a, b) => {
                        const aScore = (a.visible ? 4 : 0) + (a.hasGame ? 2 : 0);
                        const bScore = (b.visible ? 4 : 0) + (b.hasGame ? 2 : 0);
                        if (aScore !== bScore) return bScore - aScore;
                        return b.area - a.area;
                    });

                    result.debug.candidates = candidates.map((c) => ({
                        selector: c.selector,
                        tagName: c.tagName,
                        visible: c.visible,
                        hasGame: c.hasGame,
                        area: Math.round(c.area)
                    }));

                    const picked = candidates.find((c) => c.hasGame && c.visible) ||
                                   candidates.find((c) => c.hasGame) ||
                                   candidates.find((c) => c.visible) ||
                                   candidates[0];
                    if (!picked) return result;

                    const board = picked.el;
                    result.selector = picked.selector;
                    result.tagName = picked.tagName;

                    function colorFromValue(value) {
                        if (value === 1 || value === '1') return 'white';
                        if (value === 2 || value === '2') return 'black';
                        const text = (value || '').toString().trim().toLowerCase();
                        if (text === 'w' || text === 'white') return 'white';
                        if (text === 'b' || text === 'black') return 'black';
                        return null;
                    }

                    function oppositeColor(color) {
                        if (color === 'white') return 'black';
                        if (color === 'black') return 'white';
                        return null;
                    }

                    function normalizeName(value) {
                        return (value || '')
                            .toString()
                            .trim()
                            .toLowerCase()
                            .replace(/^@+/, '')
                            .replace(/[^a-z0-9_-]/g, '');
                    }

                    function debugValue(value) {
                        if (value === null || value === undefined) return value;
                        const type = typeof value;
                        if (type === 'string' || type === 'number' || type === 'boolean') {
                            return value;
                        }
                        return Object.prototype.toString.call(value);
                    }

                    function isGenericUsername(value) {
                        const name = normalizeName(value);
                        return (
                            !name ||
                            name === 'opponent' ||
                            name === 'player' ||
                            name === 'guest' ||
                            name === 'anonymous' ||
                            name === 'white' ||
                            name === 'black' ||
                            name === 'true' ||
                            name === 'false' ||
                            name === 'null' ||
                            name === 'undefined'
                        );
                    }

                    function addUsernameCandidate(candidates, rawValue, source, score) {
                        const rawText = (rawValue || '').toString().trim();
                        if (!rawText || rawText.length > 120) return;
                        const sourceText = (source || '').toString().toLowerCase();

                        let displayName = null;
                        const directMatch = rawText.match(/^@?([A-Za-z0-9_-]{3,25})$/);
                        const labelMatch = rawText.match(
                            /^@?([A-Za-z0-9_-]{3,25})(?:'s)?\\s+(?:profile|avatar|account)$/i
                        );
                        const hrefMatch = rawText.match(/\\/(?:member|user)\\/([A-Za-z0-9_-]{3,25})/i);

                        if (directMatch) displayName = directMatch[1];
                        else if (labelMatch) displayName = labelMatch[1];
                        else if (hrefMatch) displayName = hrefMatch[1];
                        else return;

                        const name = normalizeName(displayName);
                        if (!name || isGenericUsername(name)) return;
                        if (/^\\d+$/.test(name) && sourceText.includes('storage')) return;
                        candidates.push({ name, displayName, source, score });
                    }

                    function readPath(root, path) {
                        let value = root;
                        for (const key of path) {
                            if (value === null || value === undefined) return null;
                            value = value[key];
                        }
                        return value;
                    }

                    function addUsernamesFromText(candidates, text, source, score) {
                        const value = (text || '').toString();
                        if (!value || value.length > 5000) return;

                        const quotedNameRe =
                            /"(?:username|userName|user_name|login|memberName)"\\s*:\\s*"([A-Za-z0-9_-]{3,25})"/g;
                        let match = null;
                        while ((match = quotedNameRe.exec(value)) !== null) {
                            addUsernameCandidate(candidates, match[1], source, score);
                        }

                    }

                    function inferLoggedInUsername() {
                        const candidates = [];

                        addUsernameCandidate(candidates, botUsername, 'config.username', 100);

                        const globalPaths = [
                            ['chesscom', 'user', 'username'],
                            ['chesscom', 'currentUser', 'username'],
                            ['ChessCom', 'user', 'username'],
                            ['ChessCom', 'currentUser', 'username'],
                            ['currentUser', 'username'],
                            ['user', 'username'],
                            ['__CHESSCOM_USER__', 'username']
                        ];

                        for (const path of globalPaths) {
                            try {
                                addUsernameCandidate(
                                    candidates,
                                    readPath(window, path),
                                    'window.' + path.join('.'),
                                    90
                                );
                            } catch (e) {}
                        }

                        const accountSelectors = [
                            '[data-current-user]',
                            '[data-logged-in-user]',
                            '[data-auth-user]',
                            '[data-user]',
                            '[data-username][class*="account"]',
                            '[data-username][class*="profile"]',
                            '[data-username][class*="menu"]',
                            '[class*="user-menu"] [data-username]',
                            '[class*="user-menu"] a[href*="/member/"]',
                            '[class*="profile"] a[href*="/member/"]',
                            '[aria-label*="profile" i][href*="/member/"]',
                            '[title*="profile" i][href*="/member/"]'
                        ];

                        for (const selector of accountSelectors) {
                            try {
                                for (const el of document.querySelectorAll(selector)) {
                                    addUsernameCandidate(
                                        candidates,
                                        el.getAttribute('data-current-user') ||
                                            el.getAttribute('data-logged-in-user') ||
                                            el.getAttribute('data-auth-user') ||
                                            el.getAttribute('data-user') ||
                                            el.getAttribute('data-username') ||
                                            el.getAttribute('data-user-name') ||
                                            el.getAttribute('aria-label') ||
                                            el.getAttribute('title') ||
                                            el.textContent,
                                        'account-dom:' + selector,
                                        75
                                    );

                                    const href = el.getAttribute('href') || '';
                                    const hrefMatch = href.match(/\\/(?:member|user)\\/([^/?#]+)/i);
                                    if (hrefMatch) {
                                        try {
                                            addUsernameCandidate(
                                                candidates,
                                                decodeURIComponent(hrefMatch[1]),
                                                'account-link:' + selector,
                                                75
                                            );
                                        } catch (e) {
                                            addUsernameCandidate(
                                                candidates,
                                                hrefMatch[1],
                                                'account-link:' + selector,
                                                75
                                            );
                                        }
                                    }
                                }
                            } catch (e) {}
                        }

                        for (const storageName of ['localStorage', 'sessionStorage']) {
                            try {
                                const storage = window[storageName];
                                if (!storage) continue;
                                for (let i = 0; i < storage.length; i++) {
                                    const key = storage.key(i) || '';
                                    const value = storage.getItem(key) || '';
                                    if (/country|restricted|expiry|expires|timestamp|time|date|flag/i.test(key)) {
                                        continue;
                                    }
                                    if (/(username|user_name|account|member|auth|profile|login)/i.test(key)) {
                                        addUsernamesFromText(
                                            candidates,
                                            value,
                                            storageName + ':' + key.slice(0, 60),
                                            85
                                        );
                                    }
                                }
                            } catch (e) {}
                        }

                        candidates.sort((a, b) => b.score - a.score);
                        result.debug.loggedInUsernameCandidates = candidates.slice(0, 6);
                        return candidates[0] || null;
                    }

                    const configuredBotName = normalizeName(botUsername);
                    const inferredBot = inferLoggedInUsername();
                    if (inferredBot) {
                        result.botUsername = inferredBot.displayName || inferredBot.name;
                        result.debug.botUsernameSource = inferredBot.source;
                    }

                    const game = board.game;
                    if (game) {
                        try {
                            if (game.getOptions) {
                                const opts = game.getOptions();
                                if (opts) {
                                    result.debug.isPlayerBlack = debugValue(opts.isPlayerBlack);
                                    result.debug.isWhiteOnBottom = debugValue(opts.isWhiteOnBottom);
                                    result.debug.flipped = debugValue(opts.flipped);
                                    result.debug.playingAs = debugValue(opts.playingAs);
                                    result.debug.playerColor = debugValue(opts.playerColor);
                                    result.debug.isPlayerBlackType = typeof opts.isPlayerBlack;

                                    // Handle boolean, numeric (0/1), and truthy/falsy isPlayerBlack
                                    if (typeof opts.isPlayerBlack === 'boolean') {
                                        result.color = opts.isPlayerBlack ? 'black' : 'white';
                                        result.colorMethod = 'game.getOptions().isPlayerBlack';
                                    } else if (opts.isPlayerBlack === 1 || opts.isPlayerBlack === '1') {
                                        result.color = 'black';
                                        result.colorMethod = 'game.getOptions().isPlayerBlack(numeric)';
                                    } else if (opts.isPlayerBlack === 0 || opts.isPlayerBlack === '0') {
                                        result.color = 'white';
                                        result.colorMethod = 'game.getOptions().isPlayerBlack(numeric)';
                                    }

                                    if (typeof opts.isWhiteOnBottom === 'boolean') {
                                        result.orientation = opts.isWhiteOnBottom ? 'white' : 'black';
                                        result.orientationMethod = 'game.getOptions().isWhiteOnBottom';
                                    } else if (opts.isWhiteOnBottom === 1 || opts.isWhiteOnBottom === '1') {
                                        result.orientation = 'white';
                                        result.orientationMethod = 'game.getOptions().isWhiteOnBottom(numeric)';
                                    } else if (opts.isWhiteOnBottom === 0 || opts.isWhiteOnBottom === '0') {
                                        result.orientation = 'black';
                                        result.orientationMethod = 'game.getOptions().isWhiteOnBottom(numeric)';
                                    }

                                    if (!result.color) {
                                        const optionColor =
                                            colorFromValue(opts.playingAs) ||
                                            colorFromValue(opts.playerColor) ||
                                            colorFromValue(opts.userColor);
                                        if (optionColor) {
                                            result.color = optionColor;
                                            result.colorMethod = 'game.getOptions().playing-color';
                                        }
                                    }
                                }
                            }
                        } catch (e) {
                            result.debug.getOptionsError = e.message;
                        }

                        // Fallback: try board-level options (some builds store it directly)
                        if (!result.color) {
                            try {
                                const boardOpts = board.options || board.getOptions?.() || {};
                                const bIsBlack = boardOpts.isPlayerBlack;
                                result.debug.boardOptionsIsPlayerBlack = debugValue(bIsBlack);
                                if (bIsBlack === true || bIsBlack === 1 || bIsBlack === '1') {
                                    result.color = 'black';
                                    result.colorMethod = 'board.options.isPlayerBlack';
                                } else if (bIsBlack === false || bIsBlack === 0 || bIsBlack === '0') {
                                    result.color = 'white';
                                    result.colorMethod = 'board.options.isPlayerBlack';
                                }
                            } catch (e) {
                                result.debug.boardOptionsError = e.message;
                            }
                        }

                        // Fallback: data-player-color attribute
                        if (!result.color) {
                            const dataColor = board.getAttribute('data-player-color')
                                || board.getAttribute('data-color');
                            if (dataColor) {
                                result.debug.dataPlayerColor = dataColor;
                                const dColor = colorFromValue(dataColor);
                                if (dColor) {
                                    result.color = dColor;
                                    result.colorMethod = 'board[data-player-color]';
                                }
                            }
                        }

                        try {
                            if (!result.color && game.getPlayingAs) {
                                const playingAs = game.getPlayingAs();
                                result.debug.getPlayingAs = debugValue(playingAs);
                                const playingColor = colorFromValue(playingAs);
                                if (playingColor) {
                                    result.color = playingColor;
                                    result.colorMethod = 'game.getPlayingAs()';
                                }
                            }
                        } catch (e) {
                            result.debug.getPlayingAsError = e.message;
                        }

                        try {
                            if (game.getTurn) {
                                const turn = game.getTurn();
                                result.debug.getTurn = debugValue(turn);
                                if (turn === 1 || turn === '1') result.turn = 'w';
                                else if (turn === 2 || turn === '2') result.turn = 'b';
                                else {
                                    const turnColor = colorFromValue(turn);
                                    if (turnColor) result.turn = turnColor === 'white' ? 'w' : 'b';
                                }
                            }
                        } catch (e) {
                            result.debug.getTurnError = e.message;
                        }

                        try {
                            if (game.getFEN) result.fen = game.getFEN();
                            else if (game.fen) {
                                result.fen = typeof game.fen === 'function'
                                    ? game.fen()
                                    : game.fen;
                            }
                        } catch (e) {
                            result.debug.getFENError = e.message;
                        }
                    }

                    if (!result.orientation) {
                        const orientation =
                            board.orientation ||
                            board.getAttribute('orientation') ||
                            (board.dataset ? board.dataset.orientation : '') ||
                            '';
                        result.debug.orientation = debugValue(orientation);
                        const orientationColor = colorFromValue(orientation);
                        if (orientationColor) {
                            result.orientation = orientationColor;
                            result.orientationMethod = 'board.orientation';
                        }
                    }

                    if (!result.orientation && typeof board.flipped === 'boolean') {
                        result.debug.boardFlipped = board.flipped;
                        result.orientation = board.flipped ? 'black' : 'white';
                        result.orientationMethod = 'board.flipped';
                    }

                    if (!result.orientation) {
                        const boardClass = (
                            board.getAttribute('class') ||
                            board.className ||
                            ''
                        ).toString().toLowerCase();
                        result.debug.boardClass = boardClass.slice(0, 200);

                        if (
                            board.hasAttribute('flipped') ||
                            boardClass.includes('flipped') ||
                            boardClass.includes('orientation-black') ||
                            boardClass.includes('black-bottom')
                        ) {
                            result.orientation = 'black';
                            result.orientationMethod = 'board.class-flipped';
                        } else if (
                            boardClass.includes('orientation-white') ||
                            boardClass.includes('white-bottom')
                        ) {
                            result.orientation = 'white';
                            result.orientationMethod = 'board.class-orientation';
                        }
                    }

                    if (!result.orientation) {
                        const coordEls = board.querySelectorAll(
                            '[class*="coordinate"], [class*="notation"], ' +
                            '.coords-rank text, .coords-rank span, ' +
                            'svg text, .board-coordinates span'
                        );
                        let rank1Y = null;
                        let rank8Y = null;
                        for (const el of coordEls) {
                            const txt = (el.textContent || '').trim();
                            const rect = el.getBoundingClientRect();
                            const midY = rect.top + rect.height / 2;
                            if (txt === '1') rank1Y = midY;
                            if (txt === '8') rank8Y = midY;
                        }
                        result.debug.orientationRank1Y = rank1Y;
                        result.debug.orientationRank8Y = rank8Y;
                        if (rank1Y !== null && rank8Y !== null) {
                            result.orientation = rank1Y > rank8Y ? 'white' : 'black';
                            result.orientationMethod = 'coordinates';
                        }
                    }

                    if (!result.orientation) {
                        const pieces = board.querySelectorAll('.piece');
                        let wkY = null;
                        let bkY = null;
                        for (const piece of pieces) {
                            const cls = (piece.className || '').toString();
                            const rect = piece.getBoundingClientRect();
                            const midY = rect.top + rect.height / 2;
                            if (cls.includes('wk')) wkY = midY;
                            if (cls.includes('bk')) bkY = midY;
                        }
                        result.debug.whiteKingY = wkY;
                        result.debug.blackKingY = bkY;

                        if (wkY !== null && bkY !== null) {
                            result.orientation = wkY > bkY ? 'white' : 'black';
                            result.orientationMethod = 'king-screen-position';
                        }
                    }

                    let botName = configuredBotName || normalizeName(result.botUsername);
                    {
                        const boardRect = board.getBoundingClientRect();
                        const boardMidY = boardRect.top + boardRect.height / 2;
                        const leftLimit = boardRect.left - boardRect.width * 0.8;
                        const rightLimit = boardRect.right + boardRect.width * 0.8;
                        const topLimit = boardRect.top - boardRect.height * 0.8;
                        const bottomLimit = boardRect.bottom + boardRect.height * 0.8;

                        function playerNameData(el) {
                            const rawValues = [
                                el.getAttribute('data-username'),
                                el.getAttribute('data-player-username'),
                                el.getAttribute('data-user-name'),
                                el.getAttribute('username'),
                                el.getAttribute('aria-label'),
                                el.getAttribute('title'),
                                el.textContent
                            ];
                            const href = el.getAttribute('href') || '';
                            const hrefMatch = href.match(/\\/(?:member|user)\\/([^/?#]+)/i);
                            if (hrefMatch) {
                                try {
                                    rawValues.push(decodeURIComponent(hrefMatch[1]));
                                } catch (e) {
                                    rawValues.push(hrefMatch[1]);
                                }
                            }

                            const names = rawValues.map(normalizeName).filter(Boolean);
                            const displayName = rawValues
                                .map((value) => (value || '').toString().trim())
                                .find((value) => normalizeName(value)) || '';

                            return { names, displayName };
                        }

                        function nameMatches(names, target) {
                            return names.some((name) => (
                                name === target ||
                                (target.length >= 3 && name.includes(target)) ||
                                (name.length >= 3 && target.includes(name))
                            ));
                        }

                        const playerSelectors = [
                            '[data-username]',
                            '[data-player-username]',
                            '[data-user-name]',
                            '.user-username',
                            '.user-username-component',
                            '[class*="user-username"]',
                            '[class*="username"]',
                            '[class*="user-name"]',
                            '[class*="player"] a[href*="/member/"]',
                            '[class*="player"] a[href*="/user/"]',
                            'a[href*="/member/"]',
                            'a[href*="/user/"]'
                        ];
                        const seenPlayers = new Set();
                        const playerCandidates = [];

                        for (const selector of playerSelectors) {
                            for (const el of document.querySelectorAll(selector)) {
                                if (!el || seenPlayers.has(el)) continue;
                                seenPlayers.add(el);

                                const rect = el.getBoundingClientRect();
                                if (rect.width <= 0 || rect.height <= 0) continue;
                                const midX = rect.left + rect.width / 2;
                                const midY = rect.top + rect.height / 2;
                                if (
                                    midX < leftLimit || midX > rightLimit ||
                                    midY < topLimit || midY > bottomLimit
                                ) {
                                    continue;
                                }

                                const nameData = playerNameData(el);
                                if (nameData.names.length === 0) continue;

                                const side = midY > boardMidY ? 'bottom' : 'top';
                                const distance = side === 'bottom'
                                    ? Math.abs(rect.top - boardRect.bottom)
                                    : Math.abs(boardRect.top - rect.bottom);

                                playerCandidates.push({
                                    username: nameData.displayName.slice(0, 80),
                                    normalizedNames: nameData.names.slice(0, 4),
                                    text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 120),
                                    tagName: el.tagName,
                                    className: (el.className || '').toString().slice(0, 120),
                                    side,
                                    distance
                                });
                            }
                        }

                        playerCandidates.sort((a, b) => a.distance - b.distance);
                        result.debug.playerCandidates = playerCandidates.slice(0, 8);

                        function bestPlayerForSide(side) {
                            const player = playerCandidates.find((candidate) => candidate.side === side);
                            if (!player) return null;
                            return {
                                username: player.username || player.normalizedNames[0] || '',
                                side: player.side,
                                text: player.text,
                                className: player.className
                            };
                        }

                        result.topPlayer = bestPlayerForSide('top');
                        result.bottomPlayer = bestPlayerForSide('bottom');

                        function inferBotFromGenericPanel() {
                            const topName = result.topPlayer
                                ? normalizeName(result.topPlayer.username || result.topPlayer.text)
                                : '';
                            const bottomName = result.bottomPlayer
                                ? normalizeName(result.bottomPlayer.username || result.bottomPlayer.text)
                                : '';
                            const topGeneric = isGenericUsername(topName);
                            const bottomGeneric = isGenericUsername(bottomName);

                            if (topGeneric && bottomName && !bottomGeneric) {
                                botName = bottomName;
                                result.botUsername =
                                    result.bottomPlayer.username || result.bottomPlayer.text;
                                result.debug.botUsernameSource = 'player-panel-generic-top';
                                return true;
                            } else if (bottomGeneric && topName && !topGeneric) {
                                botName = topName;
                                result.botUsername =
                                    result.topPlayer.username || result.topPlayer.text;
                                result.debug.botUsernameSource = 'player-panel-generic-bottom';
                                return true;
                            }

                            return false;
                        }

                        if (!botName) inferBotFromGenericPanel();

                        if (botName) {
                            let matches = playerCandidates.filter(
                                (candidate) => nameMatches(candidate.normalizedNames, botName)
                            );
                            matches.sort((a, b) => a.distance - b.distance);

                            if (matches.length === 0 && !configuredBotName) {
                                if (inferBotFromGenericPanel()) {
                                    matches = playerCandidates.filter(
                                        (candidate) => nameMatches(candidate.normalizedNames, botName)
                                    );
                                    matches.sort((a, b) => a.distance - b.distance);
                                }
                            }

                            result.debug.playerIdentityMatches = matches.slice(0, 4);

                            if (matches.length > 0) {
                                const side = matches[0].side;
                                result.botSide = side;

                                if (!result.color && result.orientation) {
                                    result.color = side === 'bottom'
                                        ? result.orientation
                                        : oppositeColor(result.orientation);
                                    result.colorMethod = 'player-panel-username';
                                }

                                if (!result.orientation && result.color) {
                                    result.orientation = side === 'bottom'
                                        ? result.color
                                        : oppositeColor(result.color);
                                    result.orientationMethod = 'player-panel-username+color';
                                }
                            }
                        }
                    }

                    if (!result.turn && result.fen) {
                        const parts = result.fen.split(' ');
                        if (parts.length >= 2 && (parts[1] === 'w' || parts[1] === 'b')) {
                            result.turn = parts[1];
                        }
                    }

                    return result;
                }
            """, self.username)
        except Exception as e:
            logger.debug("Game controller snapshot failed: %s", e)
            return None

    async def wait_for_game_identity_ready(self, timeout_ms=15000):
        """Wait for the board plus enough player/orientation data to detect color."""
        started_at = time.monotonic()
        board_ready = await self.wait_for_board_ready(timeout_ms=timeout_ms)
        if not board_ready:
            return False

        remaining = max(1.0, (timeout_ms / 1000) - (time.monotonic() - started_at))
        deadline = time.monotonic() + remaining
        last_debug = None

        while time.monotonic() < deadline:
            snapshot = await self._read_game_controller_snapshot()
            if snapshot:
                last_debug = snapshot.get("debug", {})
                has_board = bool(snapshot.get("selector"))
                has_direct_color = snapshot.get("color") in ("white", "black")
                has_orientation = snapshot.get("orientation") in ("white", "black")
                has_player_info = bool(snapshot.get("topPlayer") or snapshot.get("bottomPlayer"))
                has_bot_side = snapshot.get("botSide") in ("top", "bottom")

                if has_board and (has_direct_color or (has_orientation and has_player_info) or has_bot_side):
                    return True

            await asyncio.sleep(0.25)

        logger.warning("Player/orientation info did not fully load before timeout: %s", last_debug)
        return False

    def _player_label(self, player):
        if not player:
            return "unknown"
        return player.get("username") or player.get("text") or "unknown"

    def _bot_username_label(self, snapshot):
        snapshot = snapshot or {}
        return (
            snapshot.get("botUsername") or
            self._detected_username or
            self.username or
            "unknown"
        )

    def _log_color_detection(self, snapshot, color_method, orientation_method):
        snapshot = snapshot or {}
        logger.info("Bot username: %s", self._bot_username_label(snapshot))
        logger.info("Top player: %s", self._player_label(snapshot.get("topPlayer")))
        logger.info("Bottom player: %s", self._player_label(snapshot.get("bottomPlayer")))
        logger.info(
            "Board orientation: %s bottom (method: %s)",
            self._color_name(self._board_orientation),
            orientation_method or "unknown",
        )
        logger.info(
            "Detected bot color: %s (method: %s, bot side: %s)",
            self._bot_color or self._color_name(self._our_color),
            color_method or "unknown",
            self._bot_side or "unknown",
        )

    async def detect_our_color(self):
        """Detect which color the bot is playing without assuming White is bottom.

        Uses a retry loop (up to 3 attempts) because Chess.com loads game state
        asynchronously and the first snapshot read may return incomplete data.
        """
        max_attempts = 3
        snapshot = None
        color_method = None
        orientation_method = None

        try:
            identity_ready = await self.wait_for_game_identity_ready()
            if not identity_ready:
                logger.warning(
                    "wait_for_game_identity_ready timed out — will retry color detection anyway"
                )

            for attempt in range(1, max_attempts + 1):
                snapshot = await self._read_game_controller_snapshot()

                if snapshot:
                    if snapshot.get("botUsername"):
                        self._detected_username = snapshot["botUsername"]

                    orientation = snapshot.get("orientation")
                    orientation_method = snapshot.get("orientationMethod")
                    bot_side = snapshot.get("botSide")
                    bot_color = snapshot.get("color")
                    color_method = snapshot.get("colorMethod")

                    if bot_color not in ("white", "black"):
                        if orientation in ("white", "black") and bot_side in ("top", "bottom"):
                            bot_color = (
                                orientation
                                if bot_side == "bottom"
                                else ("black" if orientation == "white" else "white")
                            )
                            color_method = "board-orientation+username-position"

                    if orientation not in ("white", "black"):
                        if bot_color in ("white", "black") and bot_side in ("top", "bottom"):
                            orientation = (
                                bot_color
                                if bot_side == "bottom"
                                else ("black" if bot_color == "white" else "white")
                            )
                            orientation_method = "bot-color+username-position"

                    if bot_color in ("white", "black"):
                        self._set_colors(bot_color=bot_color, board_orientation=orientation, bot_side=bot_side)

                        if self._board_orientation is None:
                            self._set_colors(board_orientation=bot_color)
                            orientation_method = orientation_method or "bot-color-fallback"

                        self._log_color_detection(snapshot, color_method, orientation_method)
                        logger.info("Color detected on attempt %d/%d", attempt, max_attempts)
                        return self._our_color

                # Not detected yet — log and retry
                if attempt < max_attempts:
                    debug = snapshot.get("debug", {}) if snapshot else {}
                    logger.info(
                        "Color detection attempt %d/%d: no color found. "
                        "isPlayerBlack=%s (type=%s), orientation=%s, botSide=%s. Retrying in 1s...",
                        attempt, max_attempts,
                        debug.get("isPlayerBlack"),
                        debug.get("isPlayerBlackType", "n/a"),
                        snapshot.get("orientation") if snapshot else None,
                        snapshot.get("botSide") if snapshot else None,
                    )
                    await asyncio.sleep(1.0)

            # All attempts exhausted
            self._log_failed_color_detection(snapshot)
            logger.warning(
                "Color detection: all %d attempts failed. Defaulting to WHITE.",
                max_attempts,
            )
            self._set_colors(bot_color="white", board_orientation="white", bot_side="bottom")
            self._log_color_detection(snapshot, "default-white", "default-white")
            return self._our_color

        except Exception as e:
            logger.warning("Color detection failed, defaulting to WHITE: %s", e, exc_info=True)
            self._set_colors(bot_color="white", board_orientation="white", bot_side="bottom")
            self._log_color_detection(snapshot, "exception-default-white", "exception-default-white")
            return self._our_color

    def _log_failed_color_detection(self, snapshot):
        """Log comprehensive diagnostic info when color detection fails."""
        if not snapshot:
            logger.warning("Color detection diagnostic: snapshot is None/empty")
            return

        debug = snapshot.get("debug", {})
        logger.warning("=== COLOR DETECTION FAILURE DIAGNOSTICS ===")
        logger.warning("  Selector: %s | Tag: %s", snapshot.get("selector"), snapshot.get("tagName"))
        logger.warning("  color=%s (method=%s)", snapshot.get("color"), snapshot.get("colorMethod"))
        logger.warning("  orientation=%s (method=%s)", snapshot.get("orientation"), snapshot.get("orientationMethod"))
        logger.warning("  botSide=%s | botUsername=%s", snapshot.get("botSide"), snapshot.get("botUsername"))
        logger.warning("  topPlayer=%s", snapshot.get("topPlayer"))
        logger.warning("  bottomPlayer=%s", snapshot.get("bottomPlayer"))
        logger.warning("  isPlayerBlack=%s (type=%s)", debug.get("isPlayerBlack"), debug.get("isPlayerBlackType"))
        logger.warning("  isWhiteOnBottom=%s", debug.get("isWhiteOnBottom"))
        logger.warning("  playingAs=%s | playerColor=%s", debug.get("playingAs"), debug.get("playerColor"))
        logger.warning("  getPlayingAs=%s", debug.get("getPlayingAs"))
        logger.warning("  boardOptionsIsPlayerBlack=%s", debug.get("boardOptionsIsPlayerBlack"))
        logger.warning("  dataPlayerColor=%s", debug.get("dataPlayerColor"))
        logger.warning("  candidates=%s", debug.get("candidates"))
        logger.warning("  playerCandidates=%s", debug.get("playerCandidates"))
        logger.warning("  playerIdentityMatches=%s", debug.get("playerIdentityMatches"))
        logger.warning("  loggedInUsernameCandidates=%s", debug.get("loggedInUsernameCandidates"))
        logger.warning("  botUsernameSource=%s", debug.get("botUsernameSource"))
        logger.warning("  Errors: opts=%s, playingAs=%s, boardOpts=%s",
                       debug.get("getOptionsError"), debug.get("getPlayingAsError"),
                       debug.get("boardOptionsError"))
        logger.warning("=== END DIAGNOSTICS ===")

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
        Use the normal starting position when a new game has no moves yet.

        Chess.com often renders an empty move list before the first move.
        Move replay is still the primary strategy after the first ply appears.

        Works for both White (waiting for our first move) and Black (waiting
        for opponent's first move).
        """
        if self._last_clean_moves or self._move_count > 0:
            return None
        if self._our_color is None:
            return None
        if not await self._has_visible_game_board():
            return None

        turn = await self._detect_turn()
        if turn != chess.WHITE:
            # If turn isn't White, the game has already started — moves should be visible
            return None

        board = chess.Board()
        color_name = "White" if self._our_color == chess.WHITE else "Black"
        logger.info(
            "No move history yet; using standard starting position (playing as %s, White to move).",
            color_name,
        )
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
            snapshot = await self._read_game_controller_snapshot()
            fen = snapshot.get("fen") if snapshot else None
            if fen and "/" in fen:
                logger.debug(
                    "JS state FEN from %s: %s",
                    snapshot.get("selector", "game-controller"),
                    fen[:60],
                )
                if " " in fen:
                    return fen
                turn = await self._detect_turn()
                return f"{fen} {'w' if turn == chess.WHITE else 'b'} KQkq - 0 1"

            fen = await self.page.evaluate("""
                () => {
                    // Method A: board component's game property
                    const board = document.querySelector('wc-chess-board, chess-board');
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
            snapshot = await self._read_game_controller_snapshot()
            fen = snapshot.get("fen") if snapshot else None
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

            fen = await self.page.evaluate("""
                () => {
                    const board = document.querySelector('wc-chess-board, chess-board');
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
            snapshot = await self._read_game_controller_snapshot()
            turn_result = snapshot.get("turn") if snapshot else None
            if turn_result == 'w':
                return chess.WHITE
            elif turn_result == 'b':
                return chess.BLACK

            turn_result = await self.page.evaluate("""
                () => {
                    const board = document.querySelector('wc-chess-board, chess-board');
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
                        const boardEl = document.querySelector('wc-chess-board, chess-board, .board');
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
                bottom_color = self._board_orientation
                if bottom_color is None and self._our_color is not None:
                    bot_side = self._current_bot_side()
                    if bot_side == "bottom":
                        bottom_color = self._our_color
                    elif bot_side == "top":
                        bottom_color = self._opposite_color(self._our_color)

                top_color = self._opposite_color(bottom_color)

                if turn_data.get("bottomActive"):
                    if bottom_color is not None:
                        return bottom_color
                elif turn_data.get("topActive"):
                    if top_color is not None:
                        return top_color

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

    async def wait_for_position_change(self, previous_fen, timeout_sec=6.0):
        """
        Wait briefly after a click until Chess.com exposes a changed position.

        Mouse events can return before the move list/FEN has updated. Without this
        guard the game loop can read the old position again and try the same move
        twice, as seen with repeated e2e4 in the runtime log.
        """
        deadline = time.monotonic() + timeout_sec
        previous_position = previous_fen.split(" ", 1)[0] if previous_fen else None

        while time.monotonic() < deadline:
            await asyncio.sleep(0.25)
            board = await self.get_full_board()
            if board is None:
                continue

            current_fen = board.fen()
            current_position = current_fen.split(" ", 1)[0]
            if current_fen != previous_fen and current_position != previous_position:
                return True

            if self._our_color is not None and board.turn != self._our_color:
                return True

        return False

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
                    const boardEl = document.querySelector('wc-chess-board, chess-board, .board');
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

                    const bottom = clocks.find(c => c.isBottom);
                    const top = clocks.find(c => !c.isBottom);

                    if (bottom && top) {
                        return {
                            bottom_time: bottom.seconds,
                            top_time: top.seconds,
                            bottom_text: bottom.text,
                            top_text: top.text
                        };
                    }

                    return null;
                }
            """)

            if clock_data:
                bot_side = self._current_bot_side()
                if bot_side == "top":
                    our_time = clock_data["top_time"]
                    opp_time = clock_data["bottom_time"]
                else:
                    if bot_side is None:
                        logger.debug("Bot side unknown while reading clocks; using bottom clock fallback.")
                    our_time = clock_data["bottom_time"]
                    opp_time = clock_data["top_time"]

                logger.debug(
                    "Clock: our=%.1fs, opp=%.1fs (bot_side=%s)",
                    our_time, opp_time, bot_side or "bottom-fallback",
                )
                return {"our_time": our_time, "opp_time": opp_time}

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
