"""
Standalone color detection for Chess.com live games.

The important rule here is to avoid "coordinates-only" color decisions. Chess.com
can expose coordinate labels that look white-oriented even when the playable board
state is not ready. The strongest generic signal at game start is:

    active clock side + side of the logged-in player + current turn

If the top clock is active while it is White's turn and the bot is the bottom
player, the bot is Black. That does not require piece DOM classes to be present.
"""

import asyncio
import logging

logger = logging.getLogger(__name__)


def opposite_color(color):
    if color == "white":
        return "black"
    if color == "black":
        return "white"
    return None


def color_from_side_and_orientation(side, orientation):
    if side not in ("top", "bottom") or orientation not in ("white", "black"):
        return None
    return orientation if side == "bottom" else opposite_color(orientation)


AUTO_COLOR_JS = """
(botUsername) => {
    const result = {
        selector: null,
        tagName: null,
        botUsername: null,
        color: null,
        colorMethod: null,
        orientation: null,
        orientationMethod: null,
        orientationConfidence: null,
        botSide: null,
        topPlayer: null,
        bottomPlayer: null,
        activeClockSide: null,
        turn: null,
        turnMethod: null,
        panelColor: null,
        panelColorMethod: null,
        debug: {}
    };

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
        if (type === 'string' || type === 'number' || type === 'boolean') return value;
        return Object.prototype.toString.call(value);
    }

    function colorFromText(value) {
        const text = (value || '').toString().trim().toLowerCase();
        if (text === 'w' || text === 'white') return 'white';
        if (text === 'b' || text === 'black') return 'black';
        if (text === '1') return 'white';
        if (text === '2') return 'black';
        return null;
    }

    function oppositeColor(color) {
        if (color === 'white') return 'black';
        if (color === 'black') return 'white';
        return null;
    }

    function colorFromOrientationAndSide(orientation, side) {
        if (!orientation || !side) return null;
        return side === 'bottom' ? orientation : oppositeColor(orientation);
    }

    function classText(el) {
        return (el && el.className ? el.className.toString() : '');
    }

    function textAround(el) {
        const parts = [];
        let node = el;
        for (let depth = 0; node && depth < 5; depth++, node = node.parentElement) {
            parts.push(classText(node));
            for (const attr of ['data-color', 'data-player-color', 'data-side', 'aria-label', 'title']) {
                const value = node.getAttribute ? node.getAttribute(attr) : '';
                if (value) parts.push(value);
            }
        }
        return parts.join(' ').toLowerCase();
    }

    function explicitPanelColor(el) {
        let node = el;
        for (let depth = 0; node && depth < 8; depth++, node = node.parentElement) {
            for (const attr of ['data-color', 'data-player-color', 'player-color']) {
                const value = node.getAttribute ? node.getAttribute(attr) : '';
                const color = colorFromText(value);
                if (color) return { color, method: attr };
            }

            const cls = classText(node).toLowerCase();
            if (/(^|[-_\\s])(player|clock|user|board)?[-_\\s]*white($|[-_\\s])/.test(cls)) {
                return { color: 'white', method: 'panel-class' };
            }
            if (/(^|[-_\\s])(player|clock|user|board)?[-_\\s]*black($|[-_\\s])/.test(cls)) {
                return { color: 'black', method: 'panel-class' };
            }
            if (/(^|[-_\\s])white[-_\\s]*(player|clock|user|board)($|[-_\\s])/.test(cls)) {
                return { color: 'white', method: 'panel-class' };
            }
            if (/(^|[-_\\s])black[-_\\s]*(player|clock|user|board)($|[-_\\s])/.test(cls)) {
                return { color: 'black', method: 'panel-class' };
            }
        }
        return null;
    }

    function chooseBoard() {
        const selectors = ['wc-chess-board', 'chess-board', '.board', '#board-single'];
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
        result.debug.boardCandidates = candidates.map((c) => ({
            selector: c.selector,
            tagName: c.tagName,
            visible: c.visible,
            hasGame: c.hasGame,
            area: Math.round(c.area)
        })).slice(0, 8);
        return candidates.find((c) => c.hasGame && c.visible) ||
            candidates.find((c) => c.hasGame) ||
            candidates.find((c) => c.visible) ||
            candidates[0] ||
            null;
    }

    const picked = chooseBoard();
    if (!picked) return result;

    const board = picked.el;
    const boardRect = board.getBoundingClientRect();
    const boardMidY = boardRect.top + boardRect.height / 2;
    result.selector = picked.selector;
    result.tagName = picked.tagName;

    function roots() {
        const values = [board];
        if (board.shadowRoot) values.unshift(board.shadowRoot);
        values.push(document);
        return values;
    }

    function queryAllEverywhere(selector) {
        const seen = new Set();
        const values = [];
        for (const root of roots()) {
            try {
                for (const el of root.querySelectorAll(selector)) {
                    if (!el || seen.has(el)) continue;
                    seen.add(el);
                    values.push(el);
                }
            } catch (e) {}
        }
        return values;
    }

    function setOrientation(orientation, method, confidence) {
        if (!result.orientation && orientation) {
            result.orientation = orientation;
            result.orientationMethod = method;
            result.orientationConfidence = confidence;
        }
    }

    try {
        const game = board.game;
        if (game && game.getOptions) {
            const opts = game.getOptions();
            result.debug.isPlayerBlack = opts ? debugValue(opts.isPlayerBlack) : null;
            result.debug.isWhiteOnBottom = opts ? debugValue(opts.isWhiteOnBottom) : null;
            result.debug.playingAs = opts ? debugValue(opts.playingAs) : null;
            if (opts && typeof opts.isWhiteOnBottom === 'boolean') {
                setOrientation(opts.isWhiteOnBottom ? 'white' : 'black', 'game.getOptions().isWhiteOnBottom', 'high');
            }
            if (opts && (opts.playingAs || opts.playerColor || opts.userColor)) {
                const color = colorFromText(opts.playingAs) ||
                    colorFromText(opts.playerColor) ||
                    colorFromText(opts.userColor);
                if (color) {
                    result.color = color;
                    result.colorMethod = 'game.getOptions().playing-color';
                }
            }
        }
        if (!result.color && game && game.getPlayingAs) {
            const playingAs = game.getPlayingAs();
            result.debug.getPlayingAs = debugValue(playingAs);
            const color = colorFromText(playingAs);
            if (color) {
                result.color = color;
                result.colorMethod = 'game.getPlayingAs()';
            }
        }
        if (game && game.getTurn) {
            const turn = game.getTurn();
            result.debug.getTurn = debugValue(turn);
            if (turn === 1 || turn === '1') {
                result.turn = 'w';
                result.turnMethod = 'game.getTurn';
            } else if (turn === 2 || turn === '2') {
                result.turn = 'b';
                result.turnMethod = 'game.getTurn';
            } else {
                const turnColor = colorFromText(turn);
                if (turnColor) {
                    result.turn = turnColor === 'white' ? 'w' : 'b';
                    result.turnMethod = 'game.getTurn';
                }
            }
        }
        if (!result.turn && game && game.getFEN) {
            const fen = game.getFEN();
            result.debug.fen = debugValue(fen);
            const parts = (fen || '').split(' ');
            if (parts[1] === 'w' || parts[1] === 'b') {
                result.turn = parts[1];
                result.turnMethod = 'game.getFEN';
            }
        }
    } catch (e) {
        result.debug.controllerError = e.message;
    }

    if (!result.turn) {
        const moveText = Array.from(document.querySelectorAll(
            '[data-ply], [class*="move-text"], vertical-move-list, wc-move-list'
        )).map((el) => (el.textContent || '').trim()).join(' ');
        result.debug.moveTextLength = moveText.length;
        if (!/[a-h][1-8]|[KQRBN]?[a-h]?[1-8]?x?[a-h][1-8]|O-O/.test(moveText)) {
            result.turn = 'w';
            result.turnMethod = 'empty-move-list';
        }
    }

    function detectOrientation() {
        const squareA1 = queryAllEverywhere('[data-square="a1"]').find((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        });
        const squareA8 = queryAllEverywhere('[data-square="a8"]').find((el) => {
            const rect = el.getBoundingClientRect();
            return rect.width > 0 && rect.height > 0;
        });
        if (squareA1 && squareA8) {
            const a1Y = squareA1.getBoundingClientRect().top;
            const a8Y = squareA8.getBoundingClientRect().top;
            result.debug.dataSquareA1Y = a1Y;
            result.debug.dataSquareA8Y = a8Y;
            setOrientation(a1Y > a8Y ? 'white' : 'black', 'data-square-position', 'high');
            return;
        }

        const squareEls = queryAllEverywhere('[class*="square-"], [data-piece], [data-figurine]');
        let wkY = null;
        let bkY = null;
        let bottomWhite = 0;
        let bottomBlack = 0;
        let topWhite = 0;
        let topBlack = 0;
        for (const el of squareEls) {
            const cls = classText(el).toLowerCase();
            const pieceText = [
                el.getAttribute ? el.getAttribute('data-piece') : '',
                el.getAttribute ? el.getAttribute('data-figurine') : '',
                cls
            ].join(' ').toLowerCase();
            let pieceColor = null;
            let isKing = false;
            if (/\\bwk\\b|white[-_\\s]*king|king[-_\\s]*white/.test(pieceText)) {
                pieceColor = 'white';
                isKing = true;
            } else if (/\\bbk\\b|black[-_\\s]*king|king[-_\\s]*black/.test(pieceText)) {
                pieceColor = 'black';
                isKing = true;
            } else if (/\\bw[pnbrqk]\\b|white[-_\\s]*(pawn|knight|bishop|rook|queen|king)/.test(pieceText)) {
                pieceColor = 'white';
            } else if (/\\bb[pnbrqk]\\b|black[-_\\s]*(pawn|knight|bishop|rook|queen|king)/.test(pieceText)) {
                pieceColor = 'black';
            }
            if (!pieceColor) continue;
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const midY = rect.top + rect.height / 2;
            if (isKing && pieceColor === 'white') wkY = midY;
            if (isKing && pieceColor === 'black') bkY = midY;
            if (midY > boardMidY) {
                if (pieceColor === 'white') bottomWhite++;
                else bottomBlack++;
            } else {
                if (pieceColor === 'white') topWhite++;
                else topBlack++;
            }
        }
        result.debug.squarePieceCount = squareEls.length;
        result.debug.whiteKingY = wkY;
        result.debug.blackKingY = bkY;
        result.debug.bottomWhite = bottomWhite;
        result.debug.bottomBlack = bottomBlack;
        result.debug.topWhite = topWhite;
        result.debug.topBlack = topBlack;
        if (wkY !== null && bkY !== null) {
            setOrientation(wkY > bkY ? 'white' : 'black', 'square-class-king-position', 'high');
            return;
        }
        if (bottomWhite + bottomBlack >= 4) {
            setOrientation(bottomWhite > bottomBlack ? 'white' : 'black', 'piece-distribution', 'medium');
            return;
        }

        const boardClass = classText(board).toLowerCase();
        result.debug.boardClass = boardClass.slice(0, 160);
        if (
            board.hasAttribute('flipped') ||
            boardClass.includes('flipped') ||
            boardClass.includes('orientation-black') ||
            boardClass.includes('black-bottom')
        ) {
            setOrientation('black', 'board.class-flipped', 'medium');
            return;
        }
        if (boardClass.includes('orientation-white') || boardClass.includes('white-bottom')) {
            setOrientation('white', 'board.class-orientation', 'medium');
            return;
        }

        const coordEls = queryAllEverywhere(
            '[class*="coordinate"], [class*="notation"], svg text, .board-coordinates span'
        );
        let rank1Y = null;
        let rank8Y = null;
        for (const el of coordEls) {
            const txt = (el.textContent || '').trim();
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const midX = rect.left + rect.width / 2;
            const midY = rect.top + rect.height / 2;
            if (
                midX < boardRect.left - boardRect.width * 0.2 ||
                midX > boardRect.right + boardRect.width * 0.2 ||
                midY < boardRect.top - boardRect.height * 0.2 ||
                midY > boardRect.bottom + boardRect.height * 0.2
            ) {
                continue;
            }
            if (txt === '1') rank1Y = midY;
            if (txt === '8') rank8Y = midY;
        }
        result.debug.orientationRank1Y = rank1Y;
        result.debug.orientationRank8Y = rank8Y;
        if (rank1Y !== null && rank8Y !== null) {
            setOrientation(rank1Y > rank8Y ? 'white' : 'black', 'coordinates', 'low');
        }
    }

    function extractNames(el) {
        const rawValues = [
            el.getAttribute ? el.getAttribute('data-username') : '',
            el.getAttribute ? el.getAttribute('data-player-username') : '',
            el.getAttribute ? el.getAttribute('data-user-name') : '',
            el.getAttribute ? el.getAttribute('username') : '',
            el.getAttribute ? el.getAttribute('aria-label') : '',
            el.getAttribute ? el.getAttribute('title') : '',
            el.textContent || ''
        ];
        const href = el.getAttribute ? (el.getAttribute('href') || '') : '';
        const hrefMatch = href.match(/\\/(?:member|user)\\/([^/?#]+)/i);
        if (hrefMatch) rawValues.push(hrefMatch[1]);

        const names = new Set();
        for (const raw of rawValues) {
            const text = (raw || '').toString();
            const direct = normalizeName(text);
            if (direct && direct.length >= 3 && direct.length <= 25) names.add(direct);
            for (const token of text.match(/[A-Za-z0-9_-]{3,25}/g) || []) {
                const name = normalizeName(token);
                if (
                    name &&
                    ![
                        'opponent', 'player', 'guest', 'anonymous', 'white', 'black',
                        'rating', 'rapid', 'blitz', 'bullet', 'min', 'challenge'
                    ].includes(name) &&
                    !/^\\d+$/.test(name)
                ) {
                    names.add(name);
                }
            }
        }
        return Array.from(names);
    }

    function detectPlayers() {
        const selectors = [
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
            '[class*="player"]',
            'a[href*="/member/"]',
            'a[href*="/user/"]'
        ];
        const leftLimit = boardRect.left - boardRect.width * 0.9;
        const rightLimit = boardRect.right + boardRect.width * 0.9;
        const topLimit = boardRect.top - boardRect.height * 0.9;
        const bottomLimit = boardRect.bottom + boardRect.height * 0.9;
        const seen = new Set();
        const players = [];

        for (const selector of selectors) {
            for (const el of document.querySelectorAll(selector)) {
                if (!el || seen.has(el)) continue;
                seen.add(el);
                const rect = el.getBoundingClientRect();
                if (rect.width <= 0 || rect.height <= 0) continue;
                const midX = rect.left + rect.width / 2;
                const midY = rect.top + rect.height / 2;
                if (midX < leftLimit || midX > rightLimit || midY < topLimit || midY > bottomLimit) {
                    continue;
                }
                const names = extractNames(el);
                if (!names.length) continue;
                const side = midY > boardMidY ? 'bottom' : 'top';
                const distance = side === 'bottom'
                    ? Math.abs(rect.top - boardRect.bottom)
                    : Math.abs(boardRect.top - rect.bottom);
                const colorData = explicitPanelColor(el);
                players.push({
                    names,
                    username: names[0],
                    text: (el.textContent || '').trim().replace(/\\s+/g, ' ').slice(0, 100),
                    side,
                    distance,
                    color: colorData ? colorData.color : null,
                    colorMethod: colorData ? colorData.method : null,
                    classHint: textAround(el).slice(0, 160)
                });
            }
        }
        players.sort((a, b) => a.distance - b.distance);
        result.debug.playerCandidates = players.slice(0, 10);

        function bestSide(side) {
            const player = players.find((candidate) => candidate.side === side);
            if (!player) return null;
            return {
                username: player.username,
                side: player.side,
                text: player.text,
                color: player.color,
                colorMethod: player.colorMethod
            };
        }
        result.topPlayer = bestSide('top');
        result.bottomPlayer = bestSide('bottom');

        const botName = normalizeName(botUsername);
        if (!botName) return;
        result.botUsername = botUsername;
        const matches = players.filter((candidate) => candidate.names.some((name) => (
            name === botName ||
            (botName.length >= 3 && name.includes(botName)) ||
            (name.length >= 3 && botName.includes(name))
        )));
        result.debug.playerIdentityMatches = matches.slice(0, 5);
        if (!matches.length) return;
        const match = matches[0];
        result.botSide = match.side;
        if (match.color) {
            result.panelColor = match.color;
            result.panelColorMethod = match.colorMethod || 'player-panel-explicit-color';
        }
    }

    function detectActiveClockSide() {
        const clockEls = Array.from(document.querySelectorAll(
            '.clock-component, [class*="clock-time"], [class*="clock"], [class*="timer"]'
        ));
        const clocks = [];
        for (const el of clockEls) {
            const rect = el.getBoundingClientRect();
            if (rect.width <= 0 || rect.height <= 0) continue;
            const midX = rect.left + rect.width / 2;
            const midY = rect.top + rect.height / 2;
            if (
                midX < boardRect.left - boardRect.width * 1.1 ||
                midX > boardRect.right + boardRect.width * 1.1 ||
                midY < boardRect.top - boardRect.height * 1.1 ||
                midY > boardRect.bottom + boardRect.height * 1.1
            ) {
                continue;
            }
            const text = [classText(el), el.getAttribute('aria-label') || '', el.getAttribute('title') || '']
                .join(' ')
                .toLowerCase();
            const active = (
                text.includes('active') ||
                text.includes('running') ||
                text.includes('ticking') ||
                text.includes('clock-player-turn') ||
                text.includes('clock-current')
            );
            if (!active) continue;
            clocks.push({
                side: midY > boardMidY ? 'bottom' : 'top',
                className: classText(el).slice(0, 120),
                text: (el.textContent || '').trim().slice(0, 40)
            });
        }
        result.debug.activeClockCandidates = clocks.slice(0, 6);
        if (clocks.length) result.activeClockSide = clocks[0].side;
    }

    detectOrientation();
    detectPlayers();
    detectActiveClockSide();

    if (result.panelColor && result.botSide) {
        result.color = result.panelColor;
        result.colorMethod = 'player-panel-explicit-color';
        if (!result.orientation) {
            result.orientation = result.botSide === 'bottom'
                ? result.panelColor
                : oppositeColor(result.panelColor);
            result.orientationMethod = 'player-panel-explicit-color';
            result.orientationConfidence = 'high';
        }
    }

    if (result.activeClockSide && result.botSide && result.turn) {
        const activeColor = result.turn === 'w' ? 'white' : 'black';
        const clockColor = result.botSide === result.activeClockSide
            ? activeColor
            : oppositeColor(activeColor);
        if (result.color && result.color !== clockColor) {
            result.debug.colorConflict = {
                previousColor: result.color,
                previousMethod: result.colorMethod,
                clockColor,
                activeClockSide: result.activeClockSide,
                botSide: result.botSide,
                turn: result.turn
            };
        }
        result.color = clockColor;
        result.colorMethod = 'active-clock+turn';
        if (!result.orientation) {
            result.orientation = result.botSide === 'bottom'
                ? result.color
                : oppositeColor(result.color);
            result.orientationMethod = 'active-clock+turn';
            result.orientationConfidence = 'high';
        }
    }

    if (
        !result.color &&
        result.botSide &&
        result.orientation &&
        result.orientationConfidence !== 'low'
    ) {
        result.color = colorFromOrientationAndSide(result.orientation, result.botSide);
        result.colorMethod = 'board-orientation+username-position';
    }

    result.debug.resolution = {
        color: result.color,
        colorMethod: result.colorMethod,
        orientation: result.orientation,
        orientationMethod: result.orientationMethod,
        orientationConfidence: result.orientationConfidence,
        botSide: result.botSide,
        activeClockSide: result.activeClockSide,
        turn: result.turn,
        turnMethod: result.turnMethod
    };

    return result;
}
"""


async def read_auto_color_snapshot(page, username=""):
    """Read one auto-color snapshot from the current Chess.com page."""
    try:
        return await page.evaluate(AUTO_COLOR_JS, username or "")
    except Exception as exc:
        logger.debug("Auto color snapshot failed: %s", exc)
        return None


async def detect_auto_color(page, username="", attempts=5, delay=0.5):
    """
    Detect bot color from page state.

    Returns the best snapshot. A snapshot with color == "white"/"black" is safe
    to apply. If no safe color is found, the last snapshot is returned for
    diagnostics.
    """
    last_snapshot = None
    for attempt in range(1, attempts + 1):
        snapshot = await read_auto_color_snapshot(page, username)
        if snapshot:
            snapshot["attempt"] = attempt
            last_snapshot = snapshot
            if snapshot.get("color") in ("white", "black"):
                return snapshot
        if attempt < attempts:
            await asyncio.sleep(delay)
    return last_snapshot
