"""Generate a 1080x1350 social-media leaderboard graphic.

Modes (--mode):
  yappers     — most floor.speech events  ("Top 5 Yappers")
  bill_mills  — most bill.sponsored events ("Top 5 Bill Mills")

Pulls counts from the `events` table over the last N days, joins
legislators for names, and layers committee leadership titles on top.
Chamber leadership (Majority/Minority Leader, Whip, Speaker) is not in
committee_assignments — those seats are hand-mapped here so the chart
isn't visually dishonest about the most prominent roles.

Output: out/<mode>-<YYYY-MM-DD>.png
"""
from __future__ import annotations

import argparse
from datetime import date
from pathlib import Path

import io
import urllib.request

from PIL import Image, ImageDraw, ImageFont

from conductor.politics import committees_sync as cm, entities, photos as photos_mod
from conductor.store import Store

MODES = {
    "yappers": {
        "event_type": "floor.speech",
        "eyebrow": "TALLYHQ · CONGRESSIONAL FLOOR LOG",
        "title": "Top 5 Yappers",
        "subtitle": "Most floor speeches · last {days} days · 119th Congress",
        "count_label": "speeches",
    },
    "bill_mills": {
        "event_type": "bill.sponsored",
        "eyebrow": "TALLYHQ · BILL SPONSORSHIP LOG",
        "title": "Top 5 Bill Mills",
        "subtitle": "Most bills introduced as primary sponsor · last {days} days · 119th Congress",
        "count_label": "bills",
    },
}

# Hand-mapped chamber leadership (not in committee_assignments)
CHAMBER_LEADERSHIP = {
    "T000250": ("Senate Majority Leader", "chair"),
    "S000148": ("Senate Minority Leader", "chair"),
    "B001261": ("Senate Majority Whip", "chair"),
    "D000563": ("Senate Minority Whip", "chair"),
    "S001172": ("Speaker of the House", "chair"),
    "J000299": ("House Minority Leader", "chair"),
    "S000244": ("House Majority Leader", "chair"),
}

W, H = 1080, 1350
BG = (251, 250, 247)
INK = (32, 30, 28)
INK_MUTED = (110, 105, 96)
INK_FAINT = (170, 165, 155)
GOLD = (217, 156, 0)
GOLD_BG = (255, 244, 214)
SILVER = (111, 111, 111)
SILVER_BG = (236, 236, 236)
RULE = (220, 215, 205)
PARTY_R = (190, 50, 60)
PARTY_D = (40, 90, 165)
PARTY_I = (110, 105, 96)

GEORGIA_B = "C:/Windows/Fonts/georgiab.ttf"
GEORGIA = "C:/Windows/Fonts/georgia.ttf"
GEORGIA_I = "C:/Windows/Fonts/georgiai.ttf"
SEGOE_B = "C:/Windows/Fonts/seguibl.ttf"
SEGOE = "C:/Windows/Fonts/segoeuib.ttf"


def font(path: str, size: int) -> ImageFont.FreeTypeFont:
    return ImageFont.truetype(path, size)


def party_color(party: str | None) -> tuple[int, int, int]:
    if not party:
        return PARTY_I
    p = party[0].upper()
    return {"R": PARTY_R, "D": PARTY_D}.get(p, PARTY_I)


def role_for(store: Store, bg: str) -> tuple[str | None, str | None]:
    """Returns (display_text, role_kind) where role_kind ∈ {chair, ranking, subchair, subranking, None}."""
    if bg in CHAMBER_LEADERSHIP:
        return CHAMBER_LEADERSHIP[bg]
    rec = cm.leadership_roles(store, [bg]).get(bg)
    if rec is None:
        return (None, None)
    role = rec["role"]
    title = rec["title"]
    cname = rec["committee_name"]
    if role == "chair":
        text = f"Chair · {cname}"
    elif role == "ranking":
        text = f"Ranking · {cname}"
    elif role == "subchair":
        text = f"Subcomm. Chair · {cname}"
    else:
        text = f"Subcomm. Ranking · {cname}"
    return (text, role)


def fetch_photo(bioguide: str, diameter: int) -> Image.Image | None:
    """Download the unitedstates 225x275 portrait and crop to a centered
    square circle of `diameter` px. Returns RGBA Image with alpha mask
    applied so the canvas paste is round-edged. None on fetch failure."""
    url = photos_mod.resolve(bioguide, "225x275")
    if url == photos_mod.PLACEHOLDER:
        return None
    try:
        with urllib.request.urlopen(url, timeout=10) as resp:
            raw = resp.read()
    except Exception:
        return None
    src = Image.open(io.BytesIO(raw)).convert("RGBA")
    sw, sh = src.size
    crop_size = min(sw, sh)
    cx, cy = sw // 2, sh // 2
    box = (cx - crop_size // 2, cy - crop_size // 2,
           cx + crop_size // 2, cy + crop_size // 2)
    sq = src.crop(box).resize((diameter, diameter), Image.LANCZOS)
    mask = Image.new("L", (diameter, diameter), 0)
    md = ImageDraw.Draw(mask)
    md.ellipse((0, 0, diameter, diameter), fill=255)
    out = Image.new("RGBA", (diameter, diameter), (0, 0, 0, 0))
    out.paste(sq, (0, 0), mask=mask)
    return out


def ring_color(role_kind: str | None) -> tuple[int, int, int] | None:
    if role_kind in ("chair",):
        return GOLD
    if role_kind == "subchair":
        return (232, 181, 61)
    if role_kind == "ranking":
        return SILVER
    if role_kind == "subranking":
        return (168, 168, 168)
    return None


def truncate(draw: ImageDraw.ImageDraw, text: str, font_obj, max_w: int) -> str:
    """Hard-truncate text to fit within max_w pixels, append ellipsis."""
    if draw.textlength(text, font=font_obj) <= max_w:
        return text
    ell = "…"
    while text and draw.textlength(text + ell, font=font_obj) > max_w:
        text = text[:-1]
    return text.rstrip() + ell


def fetch_top_n(store: Store, event_type: str, days: int, n: int) -> list[dict]:
    rows = store.conn.execute(
        f"""
        SELECT entity_id, COUNT(*) AS n
        FROM events
        WHERE event_type = ?
          AND occurred_at >= CURRENT_DATE - INTERVAL '{days} days'
        GROUP BY entity_id
        ORDER BY n DESC
        LIMIT {n}
        """,
        [event_type],
    ).fetchall()
    out = []
    for entity_id, count in rows:
        bg = entity_id.split(":", 1)[1]
        ent = entities.get(store, bg)
        if ent is None:
            continue
        role_text, role_kind = role_for(store, bg)
        out.append({
            "bioguide": bg,
            "name": ent.full_name,
            "party": ent.party or "I",
            "state": ent.state or "",
            "chamber": ent.chamber or "",
            "count": int(count),
            "role_text": role_text,
            "role_kind": role_kind,
        })
    return out


def render(rows: list[dict], days: int, mode_cfg: dict, out_path: Path) -> None:
    img = Image.new("RGB", (W, H), BG)
    d = ImageDraw.Draw(img)

    # Top accent bar
    d.rectangle([(0, 0), (W, 12)], fill=INK)

    # Header
    pad = 64
    y = 88
    f_eyebrow = font(SEGOE, 22)
    d.text((pad, y), mode_cfg["eyebrow"], fill=INK_MUTED, font=f_eyebrow)
    y += 38

    f_title = font(GEORGIA_B, 96)
    d.text((pad, y), mode_cfg["title"], fill=INK, font=f_title)
    y += 110

    f_sub = font(GEORGIA_I, 30)
    d.text((pad, y), mode_cfg["subtitle"].format(days=days),
           fill=INK_MUTED, font=f_sub)
    y += 64

    # Divider
    d.rectangle([(pad, y), (W - pad, y + 2)], fill=RULE)
    y += 28

    # Rows
    f_rank = font(GEORGIA_B, 110)
    f_name = font(GEORGIA_B, 42)
    f_who = font(SEGOE, 24)
    f_role_chair = font(SEGOE_B, 22)
    f_count = font(GEORGIA_B, 80)
    f_count_lbl = font(SEGOE, 18)

    row_h = 168
    photo_diam = 124
    rank_x = pad
    photo_x = pad + 92
    name_x = photo_x + photo_diam + 28
    name_max_w = W - name_x - pad - 220
    count_right = W - pad
    f_rank_small = font(GEORGIA_B, 64)

    for i, r in enumerate(rows[:5]):
        y0 = y + i * row_h

        # Rank numeral (smaller now, photo is the visual anchor)
        rank_str = f"{i+1}"
        d.text((rank_x + 8, y0 + 32), rank_str, fill=GOLD, font=f_rank_small)

        # Photo with role-colored ring
        photo_y = y0 + 14
        photo = fetch_photo(r["bioguide"], photo_diam)
        ring = ring_color(r["role_kind"])
        if ring is not None:
            ring_pad = 6
            d.ellipse(
                [(photo_x - ring_pad, photo_y - ring_pad),
                 (photo_x + photo_diam + ring_pad, photo_y + photo_diam + ring_pad)],
                fill=ring,
            )
            d.ellipse(
                [(photo_x - 2, photo_y - 2),
                 (photo_x + photo_diam + 2, photo_y + photo_diam + 2)],
                fill=BG,
            )
        if photo is not None:
            img.paste(photo, (photo_x, photo_y), photo)
        else:
            d.ellipse(
                [(photo_x, photo_y), (photo_x + photo_diam, photo_y + photo_diam)],
                fill=(220, 215, 205),
            )

        # Name (truncated if too long)
        name_disp = truncate(d, r["name"], f_name, name_max_w)
        d.text((name_x, y0 + 16), name_disp, fill=INK, font=f_name)

        # Party-state line
        p = r["party"][0].upper() if r["party"] else "I"
        chamber_short = "Sen." if r["chamber"].lower() == "senate" else "Rep."
        who = f"{chamber_short} {p}-{r['state']}"
        d.text((name_x, y0 + 70), who, fill=party_color(r["party"]), font=f_who)

        # Role pill
        if r["role_text"]:
            role_y = y0 + 110
            kind = r["role_kind"]
            if kind in ("chair", "subchair"):
                bg_c, fg_c = GOLD_BG, (138, 90, 0)
            else:
                bg_c, fg_c = SILVER_BG, (60, 60, 60)
            txt = truncate(d, r["role_text"], f_role_chair, name_max_w - 24)
            tw = d.textlength(txt, font=f_role_chair)
            pill_pad_x = 14
            pill_x0 = name_x
            pill_x1 = name_x + int(tw) + pill_pad_x * 2
            d.rounded_rectangle(
                [(pill_x0, role_y), (pill_x1, role_y + 36)],
                radius=18, fill=bg_c,
            )
            d.text((pill_x0 + pill_pad_x, role_y + 5), txt, fill=fg_c, font=f_role_chair)

        # Speech count (right-aligned)
        count_str = f"{r['count']}"
        cw = d.textlength(count_str, font=f_count)
        d.text((count_right - cw, y0 + 22), count_str, fill=INK, font=f_count)

        lbl = mode_cfg["count_label"]
        lw = d.textlength(lbl, font=f_count_lbl)
        d.text((count_right - lw, y0 + 110), lbl, fill=INK_MUTED, font=f_count_lbl)

        # Row divider
        if i < 4:
            div_y = y0 + row_h - 12
            d.rectangle([(pad, div_y), (W - pad, div_y + 1)], fill=RULE)

    # Footer
    f_foot = font(SEGOE, 22)
    foot_y = H - 64
    d.text((pad, foot_y), "tallyhq.org", fill=INK, font=f_foot)
    today = date.today().isoformat()
    foot_right = f"Pulled {today}"
    rw = d.textlength(foot_right, font=f_foot)
    d.text((W - pad - rw, foot_y), foot_right, fill=INK_MUTED, font=f_foot)

    out_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(out_path, format="PNG", optimize=True)


def main() -> None:
    p = argparse.ArgumentParser()
    p.add_argument("--mode", choices=list(MODES.keys()), default="yappers")
    p.add_argument("--days", type=int, default=90)
    p.add_argument("--db", type=str, default=None,
                   help="path to conductor.duckdb (default: env / Store default)")
    p.add_argument("--out", type=str, default=None)
    args = p.parse_args()

    cfg = MODES[args.mode]
    store = Store(Path(args.db)) if args.db else Store()
    try:
        rows = fetch_top_n(store, cfg["event_type"], args.days, 5)
    finally:
        store.close()

    out = Path(args.out) if args.out else Path("out") / f"{args.mode}-{date.today().isoformat()}.png"
    render(rows, args.days, cfg, out)
    print(f"wrote {out}")
    for i, r in enumerate(rows[:5]):
        print(f"  #{i+1} {r['name']} ({r['party'][0]}-{r['state']}) — {r['count']} {cfg['count_label']}"
              f"{' | ' + r['role_text'] if r['role_text'] else ''}")


if __name__ == "__main__":
    main()
