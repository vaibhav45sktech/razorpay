"""Generate six original isometric line-art illustrations (SVG) for the
CampusHood concept section. Wireframe style: thin white strokes, one neon
accent per drawing, faint 'ghost' outlines for depth."""
import math

C30, S30 = math.cos(math.radians(30)), math.sin(math.radians(30))
W, H = 240, 200


def iso(x, y, z=0.0, ox=120, oy=120):
    return ox + (x - y) * C30, oy + (x + y) * S30 - z


def pts(seq, **kw):
    return " ".join(f"{X:.1f},{Y:.1f}" for X, Y in (iso(*p, **kw) for p in seq))


def poly(seq, cls="", **kw):
    c = f' class="{cls}"' if cls else ""
    return f'<polygon{c} pathLength="1" points="{pts(seq, **kw)}"/>'


def line(a, b, cls="", **kw):
    (x1, y1), (x2, y2) = iso(*a, **kw), iso(*b, **kw)
    c = f' class="{cls}"' if cls else ""
    return f'<line{c} pathLength="1" x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}"/>'


def box(x, y, z, dx, dy, dz, top_cls="", side_cls="", **kw):
    """Three visible faces of an axis-aligned box (viewer at +x,+y,+z)."""
    top = [(x, y, z + dz), (x + dx, y, z + dz), (x + dx, y + dy, z + dz), (x, y + dy, z + dz)]
    right = [(x + dx, y, z), (x + dx, y + dy, z), (x + dx, y + dy, z + dz), (x + dx, y, z + dz)]
    front = [(x, y + dy, z), (x + dx, y + dy, z), (x + dx, y + dy, z + dz), (x, y + dy, z + dz)]
    return poly(right, side_cls, **kw) + poly(front, side_cls, **kw) + poly(top, top_cls, **kw)


def ellipse(cx, cy, z, r, cls="", **kw):
    X, Y = iso(cx, cy, z, **kw)
    c = f' class="{cls}"' if cls else ""
    return f'<ellipse{c} pathLength="1" cx="{X:.1f}" cy="{Y:.1f}" rx="{r * math.sqrt(2) * C30:.1f}" ry="{r * math.sqrt(2) * S30:.1f}"/>'


def coin(cx, cy, z, r, h, cls="", **kw):
    """A short cylinder: top ellipse, bottom half-ellipse, two verticals."""
    out = []
    X, Y = iso(cx, cy, z, **kw)
    rx, ry = r * math.sqrt(2) * C30, r * math.sqrt(2) * S30
    c = f' class="{cls}"' if cls else ""
    # bottom front arc (visible half) + sides
    out.append(f'<path{c} pathLength="1" d="M{X - rx:.1f},{Y:.1f} A{rx:.1f},{ry:.1f} 0 0 0 {X + rx:.1f},{Y:.1f}"/>')
    out.append(f'<line{c} pathLength="1" x1="{X - rx:.1f}" y1="{Y:.1f}" x2="{X - rx:.1f}" y2="{Y - h:.1f}"/>')
    out.append(f'<line{c} pathLength="1" x1="{X + rx:.1f}" y1="{Y:.1f}" x2="{X + rx:.1f}" y2="{Y - h:.1f}"/>')
    out.append(f'<ellipse{c} pathLength="1" cx="{X:.1f}" cy="{Y - h:.1f}" rx="{rx:.1f}" ry="{ry:.1f}"/>')
    return "".join(out)


def svg(body, title):
    return (f'<svg class="ill" viewBox="0 0 {W} {H}" role="img" aria-label="{title}" focusable="false">'
            f'{body}</svg>')


# ---------------------------------------------------------------- 1. ten members, one pool
def ill_pool():
    out = []
    R = 62
    # ten coins on an isometric circle around a centre stack
    for i in range(10):
        a = math.radians(-90 + i * 36)
        cx, cy = R * math.cos(a), R * math.sin(a)
        out.append(line((cx, cy, 0), (0, 0, 0), "ghost"))
    for i in range(10):
        a = math.radians(-90 + i * 36)
        cx, cy = R * math.cos(a), R * math.sin(a)
        # draw back coins first (smaller screen y first)
        out.append((iso(cx, cy)[1], coin(cx, cy, 0, 9, 6)))
    out.sort(key=lambda t: t[0] if isinstance(t, tuple) else -1e9)
    body = "".join(t[1] if isinstance(t, tuple) else t for t in out)
    # centre: a stack of three coins, neon top
    body += coin(0, 0, 0, 17, 8) + coin(0, 0, 8, 17, 8) + coin(0, 0, 16, 17, 8, "neon")
    X, Y = iso(0, 0, 24)
    body += f'<text class="glyph neon-fill halo" x="{X:.1f}" y="{Y + 5:.1f}">₹</text>'
    return svg(body, "Ten members around one pool")


# ---------------------------------------------------------------- 2. one draw each round (calendar)
def ill_draw():
    body = ""
    cw, ch, cols, rows = 26, 26, 5, 2
    x0, y0 = -cols * cw / 2, -rows * ch / 2 + 10
    # slab
    body += box(x0 - 8, y0 - 8, -6, cols * cw + 16, rows * ch + 16, 6, "", "ghost")
    # grid
    for r in range(rows + 1):
        body += line((x0, y0 + r * ch, 0), (x0 + cols * cw, y0 + r * ch, 0), "ghost")
    for c in range(cols + 1):
        body += line((x0 + c * cw, y0, 0), (x0 + c * cw, y0 + rows * ch, 0), "ghost")
    # first three cells drawn: a diagonal tick
    for i in range(3):
        cx, cy = x0 + i * cw, y0
        body += line((cx + 6, cy + 13, 0), (cx + 11, cy + 19, 0)) + line((cx + 11, cy + 19, 0), (cx + 20, cy + 7, 0))
    # the current round: raised block with neon top
    cx, cy = x0 + 3 * cw, y0
    body += box(cx + 2, cy + 2, 0, cw - 4, ch - 4, 18, "neon", "")
    # arrow up-right from the block to a coin
    X1, Y1 = iso(cx + cw / 2, cy + ch / 2, 22)
    body += f'<path class="neon" pathLength="1" d="M{X1:.1f},{Y1:.1f} C{X1 + 10:.1f},{Y1 - 30:.1f} {X1 + 30:.1f},{Y1 - 40:.1f} {X1 + 44:.1f},{Y1 - 52:.1f}"/>'
    body += f'<path class="neon" pathLength="1" d="M{X1 + 34:.1f},{Y1 - 50:.1f} L{X1 + 44:.1f},{Y1 - 52:.1f} L{X1 + 41:.1f},{Y1 - 42:.1f}"/>'
    # a coin at the arrow's tip (screen space so it lands where the arrow ends)
    Xc, Yc = X1 + 58, Y1 - 62
    body += (f'<path pathLength="1" d="M{Xc - 14:.1f},{Yc:.1f} A14,8 0 0 0 {Xc + 14:.1f},{Yc:.1f}"/>'
             f'<line pathLength="1" x1="{Xc - 14:.1f}" y1="{Yc:.1f}" x2="{Xc - 14:.1f}" y2="{Yc - 7:.1f}"/>'
             f'<line pathLength="1" x1="{Xc + 14:.1f}" y1="{Yc:.1f}" x2="{Xc + 14:.1f}" y2="{Yc - 7:.1f}"/>'
             f'<ellipse pathLength="1" cx="{Xc:.1f}" cy="{Yc - 7:.1f}" rx="14" ry="8"/>')
    return svg(body, "One member draws each round")


# ---------------------------------------------------------------- 3. the agent picks your round (timeline + savings curve)
def ill_timing():
    body = ""
    n, sw, gap = 10, 15, 4
    x0 = -(n * (sw + gap)) / 2 + 6
    y0 = -6
    kw = dict(oy=112)
    for i in range(n):
        x = x0 + i * (sw + gap)
        if i < 3:      # already drawn: flat and faint
            body += box(x, y0, 0, sw, 20, 3, "ghost", "ghost", **kw)
        elif i == 6:   # the agent's pick: tall, neon top
            body += box(x, y0, 0, sw, 20, 30, "neon", "", **kw)
        else:
            body += box(x, y0, 0, sw, 20, 8, "", "ghost", **kw)
    # a floating "need" note (small card with !) linked to the pick by a dotted line
    nx, ny, nz = x0 + 8 * (sw + gap) + 4, y0 - 34, 46
    body += box(nx, ny, nz, 26, 18, 2, "", "ghost", **kw)
    Xn, Yn = iso(nx + 13, ny + 9, nz + 2, **kw)
    body += f'<text class="glyph small" x="{Xn:.1f}" y="{Yn + 4:.1f}">!</text>'
    Xp, Yp = iso(x0 + 6 * (sw + gap) + sw / 2, y0 + 10, 30, **kw)
    body += f'<line class="dots" pathLength="1" x1="{Xn:.1f}" y1="{Yn + 10:.1f}" x2="{Xp:.1f}" y2="{Yp - 30:.1f}"/>'
    # flag on the pick
    body += f'<line class="neon" pathLength="1" x1="{Xp:.1f}" y1="{Yp:.1f}" x2="{Xp:.1f}" y2="{Yp - 34:.1f}"/>'
    body += f'<polygon class="neon neon-fill" pathLength="1" points="{Xp:.1f},{Yp - 34:.1f} {Xp + 22:.1f},{Yp - 29:.1f} {Xp:.1f},{Yp - 22:.1f}"/>'
    return svg(body, "The agent times your draw")


# ---------------------------------------------------------------- 4. it plans, you tap (sheet + button)
def ill_tap():
    body = ""
    # a sheet lying flat, with text lines
    body += box(-60, -40, -3, 110, 80, 3, "", "ghost")
    for i, ln in enumerate([50, 62, 40]):
        body += line((-48, -12 + i * 14, 0), (-48 + ln, -12 + i * 14, 0), "ghost")
    # a raised round button on the sheet's right: neon top with a check
    body += coin(30, 20, 0, 18, 14, "")
    body += ellipse(30, 20, 14, 18, "neon")
    X, Y = iso(30, 20, 14)
    body += f'<polyline class="neon" pathLength="1" points="{X - 9:.1f},{Y:.1f} {X - 3:.1f},{Y + 5:.1f} {X + 10:.1f},{Y - 6:.1f}"/>'
    # the proposed amount as the sheet's headline row
    Xg, Yg = iso(-30, -30, 0)
    body += f'<text class="glyph" x="{Xg:.1f}" y="{Yg + 5:.1f}">₹500</text>'
    return svg(body, "It plans, you tap")


# ---------------------------------------------------------------- 5. it asks before it spends (card + gate)
def ill_gate():
    body = ""
    kw = dict(ox=112, oy=118)
    # ground line the gate guards
    body += line((-10, -40, 0), (-10, 70, 0), "dots", **kw)
    # the card, sliding in from the left: slab, chip, stripe, motion lines
    body += box(-92, -6, 0, 74, 46, 4, "", "ghost", **kw)
    body += poly([(-84, 2, 4), (-70, 2, 4), (-70, 12, 4), (-84, 12, 4)], "ghost", **kw)
    body += line((-84, 28, 4), (-36, 28, 4), "ghost", **kw)
    for d in (0, 9, 18):
        body += line((-112 - d, 10, 2), (-100 - d, 10, 2), "ghost", **kw)
    # boom barrier: post, pivot, raised bar (neon)
    body += box(-10, -12, 0, 12, 12, 46, "", "", **kw)
    Xp, Yp = iso(-4, -6, 46, **kw)
    body += f'<circle class="neon" pathLength="1" cx="{Xp:.1f}" cy="{Yp:.1f}" r="4"/>'
    BX, BY = 30, 56   # bar vector (tilted, raised)
    body += f'<line class="neon" pathLength="1" x1="{Xp:.1f}" y1="{Yp:.1f}" x2="{Xp + BX:.1f}" y2="{Yp - BY:.1f}"/>'
    body += f'<line class="neon" pathLength="1" x1="{Xp + 5:.1f}" y1="{Yp + 2:.1f}" x2="{Xp + BX + 5:.1f}" y2="{Yp - BY + 2:.1f}"/>'
    for k in range(1, 6):   # stripes on the bar
        t = k / 6
        body += f'<line class="neon" pathLength="1" x1="{Xp + BX * t:.1f}" y1="{Yp - BY * t:.1f}" x2="{Xp + 5 + BX * t:.1f}" y2="{Yp - BY * t + 2:.1f}"/>'
    # the gate's question: a speech bubble with ? floating right of the bar
    bx, by = Xp + 64, Yp - 44
    body += (f'<path pathLength="1" d="M{bx - 16:.1f},{by - 14:.1f} h32 a6,6 0 0 1 6,6 v16 a6,6 0 0 1 -6,6 h-18 l-8,8 v-8 h-6 '
             f'a6,6 0 0 1 -6,-6 v-16 a6,6 0 0 1 6,-6 z"/>')
    body += f'<text class="glyph" x="{bx:.1f}" y="{by + 7:.1f}">?</text>'
    # three tier marks on the ground beyond the gate: allow / ask / refuse
    for i, x in enumerate((12, 30, 48)):
        body += box(x, 40, 0, 12, 12, 4 + i * 8, "neon" if i == 1 else "", "ghost", **kw)
    return svg(body, "It asks before it spends")


# ---------------------------------------------------------------- 6. it can look, never touch (locked safe + chained blocks)
def ill_lock():
    body = ""
    kw = dict(ox=124, oy=124)
    # the safe
    body += box(-30, -30, 0, 66, 66, 62, "", "", **kw)
    body += poly([(-22, 36, 8), (30, 36, 8), (30, 36, 54), (-22, 36, 54)], "ghost", **kw)
    # padlock, drawn in screen space centred on the door
    Xd, Yd = iso(4, 36, 31, **kw)
    body += f'<rect class="neon" pathLength="1" x="{Xd - 9:.1f}" y="{Yd - 4:.1f}" width="18" height="15" rx="2"/>'
    body += f'<path class="neon" pathLength="1" d="M{Xd - 5:.1f},{Yd - 4:.1f} v-6 a5,5 0 0 1 10,0 v6"/>'
    body += f'<circle class="neon neon-fill" pathLength="1" cx="{Xd:.1f}" cy="{Yd + 3:.1f}" r="1.6"/>'
    # the eye above: it can look
    X, Y = iso(3, 3, 114, **kw)
    body += f'<path pathLength="1" d="M{X - 24:.1f},{Y:.1f} q24,-18 48,0 q-24,18 -48,0 z"/>'
    body += f'<circle pathLength="1" cx="{X:.1f}" cy="{Y:.1f}" r="5.5"/>'
    body += f'<circle class="neon-fill neon" pathLength="1" cx="{X:.1f}" cy="{Y:.1f}" r="2"/>'
    # hash chain of audit blocks along the front-left, linked
    for i in range(4):
        x = -80 + i * 19
        body += box(x, 56, 0, 12, 12, 12, "ghost", "ghost", **kw)
        if i < 3:
            body += line((x + 12, 62, 6), (x + 19, 62, 6), "ghost", **kw)
    return svg(body, "It can look, never touch")


ILLS = {
    "pool": ill_pool(), "draw": ill_draw(), "timing": ill_timing(),
    "tap": ill_tap(), "gate": ill_gate(), "lock": ill_lock(),
}

if __name__ == "__main__":
    import json, sys
    json.dump(ILLS, open(sys.argv[1], "w"))
    print({k: len(v) for k, v in ILLS.items()})
