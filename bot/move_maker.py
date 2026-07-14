"""
Move maker for chess.com.
Converts UCI moves to board clicks using Playwright with human-like mouse movement.

Anti-detection features:
- Bézier curve mouse trajectories (not straight lines)
- Random pixel offset from square center
- Simulated press time (mousedown → delay → mouseup)
- Variable movement speed
"""

import math
import random
import logging
import asyncio
import chess

logger = logging.getLogger(__name__)


def _bezier_point(t, p0, p1, p2, p3):
    """Calculate a point on a cubic Bézier curve at parameter t (0..1)."""
    u = 1.0 - t
    return (
        u * u * u * p0[0] + 3 * u * u * t * p1[0] + 3 * u * t * t * p2[0] + t * t * t * p3[0],
        u * u * u * p0[1] + 3 * u * u * t * p1[1] + 3 * u * t * t * p2[1] + t * t * t * p3[1],
    )


def _generate_bezier_path(start, end, num_points=25):
    """
    Generate a human-like mouse path using a cubic Bézier curve
    with randomized control points.

    Real human mouse movement has:
    - Slight curvature (not a perfectly straight line)
    - Speed variation (fast in the middle, slow at start/end)
    - Small jitter
    """
    sx, sy = start
    ex, ey = end

    # Distance between points
    dist = math.hypot(ex - sx, ey - sy)

    # Generate 2 random control points for the cubic Bézier
    # Control points are offset perpendicular to the direct path
    dx, dy = ex - sx, ey - sy
    # Perpendicular direction
    perp_x, perp_y = -dy, dx
    perp_len = math.hypot(perp_x, perp_y) or 1.0
    perp_x /= perp_len
    perp_y /= perp_len

    # Random curvature magnitude (proportional to distance, but capped)
    curvature = random.uniform(-0.15, 0.15) * dist
    curvature2 = random.uniform(-0.1, 0.1) * dist

    # Control point 1: ~30% along the line, offset perpendicular
    cp1 = (
        sx + dx * 0.3 + perp_x * curvature,
        sy + dy * 0.3 + perp_y * curvature,
    )
    # Control point 2: ~70% along the line, offset perpendicular
    cp2 = (
        sx + dx * 0.7 + perp_x * curvature2,
        sy + dy * 0.7 + perp_y * curvature2,
    )

    # Generate points along the curve with non-uniform spacing
    # (slow at start/end, fast in middle — simulates acceleration/deceleration)
    points = []
    for i in range(num_points):
        # Ease-in-out function for natural speed profile
        raw_t = i / (num_points - 1)
        t = _ease_in_out(raw_t)

        px, py = _bezier_point(t, (sx, sy), cp1, cp2, (ex, ey))

        # Add tiny jitter to each point (1-2 pixel noise)
        jitter_x = random.gauss(0, 0.8)
        jitter_y = random.gauss(0, 0.8)

        points.append((px + jitter_x, py + jitter_y))

    # Make sure the last point is exactly the target
    points[-1] = (ex, ey)

    return points


def _ease_in_out(t):
    """Smooth ease-in-out curve for natural mouse speed profile."""
    if t < 0.5:
        return 2.0 * t * t
    else:
        return -1.0 + (4.0 - 2.0 * t) * t


def _random_offset(max_pixels=4):
    """
    Generate a random offset from the center of a square.
    Humans never click exactly at the center — they're always slightly off.
    """
    # Use Gaussian distribution (most clicks near center, few at edges)
    ox = random.gauss(0, max_pixels * 0.4)
    oy = random.gauss(0, max_pixels * 0.4)
    # Clamp to avoid going outside the square
    ox = max(-max_pixels, min(max_pixels, ox))
    oy = max(-max_pixels, min(max_pixels, oy))
    return ox, oy


class MoveMaker:
    """
    Makes moves on chess.com board with human-like mouse behavior.

    Uses Bézier curves for mouse movement, random offsets from center,
    and simulated press time to avoid telemetry-based detection.
    """

    def __init__(self, page, is_white=True):
        self.page = page
        self.is_white = is_white
        # Track last mouse position for continuous movement
        self._last_mouse_x = None
        self._last_mouse_y = None

    def set_color(self, is_white):
        """Set which color we're playing (affects coordinate mapping)."""
        self.is_white = is_white

    async def make_move(self, move):
        """
        Make a move on the chess.com board with human-like mouse behavior.

        Args:
            move: chess.Move object (e.g., Move.from_uci("e2e4"))
        """
        from_square = move.from_square
        to_square = move.to_square

        from_name = chess.square_name(from_square)
        to_name = chess.square_name(to_square)
        logger.info("Making move: %s → %s", from_name, to_name)

        # Get board element bounding box
        board_box = await self._get_board_bbox()
        if board_box is None:
            logger.error("Could not find board element!")
            return False

        # Calculate pixel coordinates with random offset
        from_center = self._square_to_pixels(from_square, board_box)
        to_center = self._square_to_pixels(to_square, board_box)

        off1 = _random_offset(max_pixels=4)
        off2 = _random_offset(max_pixels=4)

        from_x = from_center[0] + off1[0]
        from_y = from_center[1] + off1[1]
        to_x = to_center[0] + off2[0]
        to_y = to_center[1] + off2[1]

        try:
            # Move mouse to source square with Bézier curve
            start_pos = self._get_current_mouse_pos(board_box)
            await self._move_mouse_bezier(start_pos, (from_x, from_y))

            # Click source square with simulated press time
            await self._human_click(from_x, from_y)

            # Brief pause (human reaction between click and drag)
            await asyncio.sleep(random.uniform(0.05, 0.15))

            # Move mouse to target square with Bézier curve
            await self._move_mouse_bezier((from_x, from_y), (to_x, to_y))

            # Click destination square with simulated press time
            await self._human_click(to_x, to_y)

            # Update last mouse position
            self._last_mouse_x = to_x
            self._last_mouse_y = to_y

            # Handle pawn promotion
            if move.promotion:
                await asyncio.sleep(random.uniform(0.1, 0.3))
                await self._handle_promotion(move.promotion, board_box)

            logger.info("Move executed: %s%s", from_name, to_name)
            return True

        except Exception as e:
            logger.error("Failed to make move %s%s: %s", from_name, to_name, e)
            return False

    async def _move_mouse_bezier(self, start, end):
        """
        Move mouse from start to end along a Bézier curve path.
        Includes variable speed (slow start/end, fast middle).
        """
        dist = math.hypot(end[0] - start[0], end[1] - start[1])

        # More points for longer distances (smoother curve)
        num_points = max(15, min(40, int(dist / 8)))
        path = _generate_bezier_path(start, end, num_points)

        # Base time for the full movement (longer distance = more time)
        base_time_ms = random.uniform(80, 200) + dist * random.uniform(0.3, 0.6)

        for i, (px, py) in enumerate(path):
            await self.page.mouse.move(px, py)

            # Variable delay between points (slow at start/end)
            if i < len(path) - 1:
                progress = i / len(path)
                # Faster in the middle, slower at edges
                if progress < 0.2 or progress > 0.8:
                    delay = base_time_ms / num_points * random.uniform(1.2, 2.0)
                else:
                    delay = base_time_ms / num_points * random.uniform(0.5, 1.0)

                await asyncio.sleep(delay / 1000.0)

    async def _human_click(self, x, y):
        """
        Simulate a human mouse click with realistic press duration.
        Humans hold the button for 50-150ms (not instant).
        """
        # Mouse down
        await self.page.mouse.down()

        # Hold for a realistic duration
        press_duration = random.uniform(0.04, 0.12)
        await asyncio.sleep(press_duration)

        # Mouse up
        await self.page.mouse.up()

    def _get_current_mouse_pos(self, board_box):
        """Get current mouse position, or a random starting point."""
        if self._last_mouse_x is not None:
            return (self._last_mouse_x, self._last_mouse_y)

        # First move — start from a random position near the board
        return (
            board_box['x'] + board_box['width'] * random.uniform(0.3, 0.7),
            board_box['y'] + board_box['height'] + random.uniform(20, 60),
        )

    async def _get_board_bbox(self):
        """Get the bounding box of the chess board element."""
        try:
            board_selectors = [
                'wc-chess-board',
                '.board',
                'chess-board',
                '#board-single',
                '#board-layout-main',
            ]

            for selector in board_selectors:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    bbox = await el.first.bounding_box()
                    if bbox and bbox['width'] > 100:
                        logger.debug(
                            "Board found (%s): x=%.0f y=%.0f w=%.0f h=%.0f",
                            selector, bbox['x'], bbox['y'],
                            bbox['width'], bbox['height'],
                        )
                        return bbox

            logger.error("Board element not found with any selector!")
            return None

        except Exception as e:
            logger.error("Error getting board bbox: %s", e)
            return None

    def _square_to_pixels(self, square, board_box):
        """
        Convert a chess square index to pixel coordinates (center of square).

        Args:
            square: chess.Square (0=a1, 63=h8)
            board_box: Board bounding box dict

        Returns:
            (x, y) pixel coordinates (center of square)
        """
        file_idx = chess.square_file(square)  # 0-7 (a-h)
        rank_idx = chess.square_rank(square)   # 0-7 (1-8)

        square_w = board_box['width'] / 8
        square_h = board_box['height'] / 8

        if self.is_white:
            pixel_x = board_box['x'] + (file_idx + 0.5) * square_w
            pixel_y = board_box['y'] + (7 - rank_idx + 0.5) * square_h
        else:
            pixel_x = board_box['x'] + (7 - file_idx + 0.5) * square_w
            pixel_y = board_box['y'] + (rank_idx + 0.5) * square_h

        return pixel_x, pixel_y

    async def _handle_promotion(self, promotion_piece, board_box):
        """
        Handle pawn promotion dialog with human-like interaction.
        """
        piece_map = {
            chess.QUEEN: 'q',
            chess.ROOK: 'r',
            chess.BISHOP: 'b',
            chess.KNIGHT: 'n',
        }
        piece_char = piece_map.get(promotion_piece, 'q')

        try:
            # Wait for promotion dialog
            await self.page.wait_for_selector(
                '.promotion-area, .promotion-window, [class*="promotion"]',
                timeout=3000,
            )
            await asyncio.sleep(random.uniform(0.2, 0.5))

            # Find the promotion piece element
            selectors = [
                f'.promotion-piece[data-piece="{piece_char}"]',
                f'[class*="promotion"][class*="{piece_char}"]',
            ]

            for selector in selectors:
                el = self.page.locator(selector)
                if await el.count() > 0:
                    bbox = await el.first.bounding_box()
                    if bbox:
                        # Click with offset and Bézier
                        target_x = bbox['x'] + bbox['width'] / 2 + _random_offset(2)[0]
                        target_y = bbox['y'] + bbox['height'] / 2 + _random_offset(2)[1]
                        current = (self._last_mouse_x or target_x - 30, self._last_mouse_y or target_y - 30)
                        await self._move_mouse_bezier(current, (target_x, target_y))
                        await self._human_click(target_x, target_y)
                        logger.info("Promotion: selected %s", chess.piece_name(promotion_piece))
                        return

            # Fallback: click first promotion option
            fallback = self.page.locator('.promotion-piece').first
            if await fallback.count() > 0:
                await fallback.click()
                logger.warning("Promotion: used fallback click")

        except Exception as e:
            logger.warning("Promotion handling failed: %s", e)
