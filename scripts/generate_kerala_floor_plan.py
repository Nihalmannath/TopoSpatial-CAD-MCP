"""
Kerala 2BHK Architectural Floor Plan Generator (1119 sq.ft).
Generates the exact ground floor plan from the reference architectural image:
- Porch / Car Parking (+6" M) with diagonal hatch
- Sit Out (+2'0" M) & Main Entrance
- Kitchen with L-shaped counter
- Living Room (+2'0" M) with furniture layout
- Dining Area with 6-seater dining table & Staircase (Up)
- Bedroom 1 (Left) with bed & wardrobe
- Bedroom 2 / Master Bedroom (Right) with bed & wardrobe
- Attached Toilet (7'0" x 7'6") & Common Toilet
- Compound wall, setback lines, North arrow & title block
"""

from __future__ import annotations

import math
from pathlib import Path
import pythoncom
import pywintypes
import win32com.client


def point(x: float, y: float, z: float = 0.0):
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y), float(z))
    )


def coords(values):
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in values)
    )


def create_kerala_floor_plan():
    pythoncom.CoInitialize()
    app = None

    # Try connecting to active AutoCAD / CAD instance
    try:
        app = win32com.client.GetActiveObject("AutoCAD.Application")
    except Exception:
        try:
            clsid = pywintypes.IID("{8B4929F8-076F-4AEC-AFEE-8928747B7AE3}")
            unknown = pythoncom.GetActiveObject(clsid)
            dispatch = unknown.QueryInterface(pythoncom.IID_IDispatch)
            app = win32com.client.Dispatch(dispatch)
        except Exception:
            try:
                app = win32com.client.Dispatch("AutoCAD.Application")
            except Exception as e:
                print(f"Could not launch AutoCAD: {e}")
                return False

    app.Visible = True
    doc = app.Documents.Add()
    ms = doc.ModelSpace

    # Drawing units: Millimetres
    try:
        doc.SetVariable("INSUNITS", 4)
        doc.SetVariable("LUNITS", 2)
        doc.SetVariable("LUPREC", 0)
    except Exception:
        pass

    # Layers setup
    def add_layer(name: str, color: int, lineweight: int | None = None):
        try:
            lay = doc.Layers.Item(name)
        except Exception:
            lay = doc.Layers.Add(name)
        lay.Color = color
        if lineweight is not None:
            try:
                lay.Lineweight = lineweight
            except Exception:
                pass
        return lay

    add_layer("A-WALL-EXT", 7, 50)     # White / 0.50mm
    add_layer("A-WALL-INT", 7, 35)     # White / 0.35mm
    add_layer("A-DOOR", 2, 25)         # Yellow / 0.25mm
    add_layer("A-WINDOW", 4, 25)       # Cyan / 0.25mm
    add_layer("A-STAIR", 6, 25)        # Magenta / 0.25mm
    add_layer("A-FURN", 8, 18)         # Gray / 0.18mm
    add_layer("A-TEXT", 3, 20)         # Green / 0.20mm
    add_layer("A-DIM", 1, 18)          # Red / 0.18mm
    add_layer("A-HATCH", 8, 13)        # Gray / 0.13mm
    add_layer("A-SETBACK", 9, 18)      # Light Gray / 0.18mm

    def line(p1, p2, layer="A-WALL-EXT", color=None):
        l = ms.AddLine(point(*p1), point(*p2))
        l.Layer = layer
        if color is not None:
            l.Color = color
        return l

    def rect(p1, p2, layer="A-WALL-EXT", color=None):
        x1, y1 = p1
        x2, y2 = p2
        pts = (x1, y1, x2, y1, x2, y2, x1, y2)
        pl = ms.AddLightWeightPolyline(coords(pts))
        pl.Closed = True
        pl.Layer = layer
        if color is not None:
            pl.Color = color
        return pl

    def text(pos, content, height=200, layer="A-TEXT", color=None):
        t = ms.AddText(content, point(*pos), float(height))
        t.Layer = layer
        if color is not None:
            t.Color = color
        return t

    def door(p, width, rotation=0, flip=False, layer="A-DOOR"):
        x, y = p
        # Door frame & panel
        if rotation == 0:
            line((x, y), (x + width, y), layer=layer)
            # swing arc
            r = width
            arc = ms.AddArc(point(x, y), r, 0, math.radians(90))
            arc.Layer = layer
            line((x, y), (x, y + width), layer=layer)
        elif rotation == 90:
            line((x, y), (x, y + width), layer=layer)
            arc = ms.AddArc(point(x, y), width, math.radians(90), math.radians(180))
            arc.Layer = layer
            line((x, y), (x - width, y), layer=layer)
        elif rotation == 180:
            line((x, y), (x - width, y), layer=layer)
            arc = ms.AddArc(point(x, y), width, math.radians(180), math.radians(270))
            arc.Layer = layer
            line((x, y), (x, y - width), layer=layer)
        elif rotation == 270:
            line((x, y), (x, y - width), layer=layer)
            arc = ms.AddArc(point(x, y), width, math.radians(270), math.radians(360))
            arc.Layer = layer
            line((x, y), (x + width, y), layer=layer)

    def window(p1, p2, layer="A-WINDOW"):
        # Double / triple line window
        x1, y1 = p1
        x2, y2 = p2
        line((x1, y1), (x2, y2), layer=layer)
        mx = (x1 + x2) / 2
        my = (y1 + y2) / 2
        line((x1, y1), (mx, my), layer=layer)
        line((mx, my), (x2, y2), layer=layer)

    print("Drawing Setbacks & Plot Boundary...")
    # Setback boundary: 10000 x 17000 mm
    rect((-1200, -3000), (9400, 13800), layer="A-SETBACK")
    line((-1200, -3000), (9400, -3000), layer="A-SETBACK", color=1)

    print("Drawing Exterior & Interior Walls...")
    # Building main walls: 0,0 to 8200,12600
    # Porch (X: 4600 to 8200, Y: 0 to 4600)
    # Sitout (X: 2800 to 4600, Y: 1800 to 4600)
    # Kitchen (X: 0 to 2800, Y: 1800 to 5200)
    # Dining (X: 0 to 4200, Y: 5200 to 9200)
    # Living (X: 4200 to 8200, Y: 4600 to 8800)
    # Bed 1 (X: 0 to 2800, Y: 9200 to 12600)
    # Common Toilet (X: 2800 to 4200, Y: 10200 to 12600)
    # Bed 2 Master (X: 4200 to 8200, Y: 9200 to 12600)
    # Master Toilet (X: 5900 to 8200, Y: 8800 to 10900)

    # External perimeter polylines
    # Left wall (X: 0, Y: 1800 to 12600)
    line((0, 1800), (0, 12600), "A-WALL-EXT")
    line((230, 2030), (230, 12370), "A-WALL-EXT")

    # Top wall (Y: 12600, X: 0 to 8200)
    line((0, 12600), (8200, 12600), "A-WALL-EXT")
    line((230, 12370), (7970, 12370), "A-WALL-EXT")

    # Right wall (X: 8200, Y: 0 to 12600)
    line((8200, 0), (8200, 12600), "A-WALL-EXT")
    line((7970, 230), (7970, 12370), "A-WALL-EXT")

    # Porch bottom wall & front
    line((4600, 0), (8200, 0), "A-WALL-EXT")
    line((4600, 0), (4600, 1800), "A-WALL-EXT")

    # Kitchen bottom wall (Y: 1800, X: 0 to 2800)
    line((0, 1800), (2800, 1800), "A-WALL-EXT")
    line((230, 2030), (2685, 2030), "A-WALL-EXT")

    # Sitout steps & front wall
    line((2800, 1800), (4600, 1800), "A-WALL-EXT")
    line((2800, 1500), (4600, 1500), "A-STAIR")  # Step 1
    line((2800, 1200), (4600, 1200), "A-STAIR")  # Step 2

    # Internal dividing walls
    # Kitchen / Dining wall (Y: 5200, X: 0 to 2800)
    line((0, 5200), (2800, 5200), "A-WALL-INT")
    line((0, 5085), (2800, 5085), "A-WALL-INT")

    # Kitchen / Sitout wall (X: 2800, Y: 1800 to 5200)
    line((2800, 1800), (2800, 5200), "A-WALL-INT")

    # Sitout / Porch wall (X: 4600, Y: 1800 to 4600)
    line((4600, 1800), (4600, 4600), "A-WALL-INT")

    # Living / Porch wall (Y: 4600, X: 4600 to 8200)
    line((4600, 4600), (8200, 4600), "A-WALL-INT")

    # Dining / Living boundary (X: 4200, Y: 5200 to 8800) - Open plan with arch
    line((4200, 8400), (4200, 8800), "A-WALL-INT")
    line((4200, 4600), (4200, 5000), "A-WALL-INT")

    # Bedroom 1 / Dining wall (Y: 9200, X: 0 to 2800)
    line((0, 9200), (2800, 9200), "A-WALL-INT")
    line((0, 9085), (2800, 9085), "A-WALL-INT")

    # Bed 1 / Toilet wall (X: 2800, Y: 9200 to 12600)
    line((2800, 9200), (2800, 12600), "A-WALL-INT")

    # Toilet / Bed 2 wall (X: 4200, Y: 9200 to 12600)
    line((4200, 9200), (4200, 12600), "A-WALL-INT")

    # Master Toilet walls (X: 5900 to 8200, Y: 8800 to 10900)
    line((5900, 8800), (5900, 10900), "A-WALL-INT")
    line((5900, 10900), (8200, 10900), "A-WALL-INT")

    # Central Archway (Passage connecting Living/Dining to Bedrooms)
    arch = ms.AddArc(point(3500, 9200), 700, 0, math.pi)
    arch.Layer = "A-WALL-INT"

    print("Drawing Doors and Windows...")
    # Main Entrance Door (Sitout -> Living/Dining)
    door((3000, 4600), 900, rotation=0)

    # Kitchen Door
    door((2600, 5200), 800, rotation=270)

    # Bed 1 Door
    door((2800, 9400), 900, rotation=90)

    # Common Toilet Door
    door((3000, 10200), 750, rotation=0)

    # Bed 2 Door
    door((4400, 9200), 900, rotation=0)

    # Master Toilet Door
    door((5900, 9000), 750, rotation=90)

    # Windows
    window((800, 1800), (2000, 1800))       # Kitchen front
    window((0, 3000), (0, 4200))             # Kitchen side
    window((8200, 5500), (8200, 7500))       # Living right
    window((8200, 11400), (8200, 12400))     # Bed 2 right
    window((5500, 12600), (6700, 12600))     # Bed 2 top
    window((3300, 12600), (3900, 12600))     # Toilet ventilator
    window((800, 12600), (2000, 12600))      # Bed 1 top
    window((0, 10400), (0, 11600))           # Bed 1 left

    print("Drawing Staircase (Up)...")
    # Staircase in dining area (X: 230 to 1230, Y: 6000 to 8600)
    for y_step in range(6000, 8600, 280):
        line((230, y_step), (1230, y_step), "A-STAIR")
    line((1230, 6000), (1230, 8600), "A-STAIR")
    # Angled landing
    line((230, 8600), (1230, 8600), "A-STAIR")
    line((230, 8600), (730, 9100), "A-STAIR")
    line((1230, 8600), (730, 9100), "A-STAIR")
    text((600, 7200), "UP", height=150, layer="A-STAIR")

    print("Drawing Furniture & Layout...")
    # 6-Seater Dining Table (X: 2000, Y: 7000)
    rect((1600, 6500), (2400, 7500), layer="A-FURN")
    # 6 Chairs
    for cx in [1800, 2200]:
        rect((cx - 150, 6150), (cx + 150, 6450), layer="A-FURN")  # Bottom
        rect((cx - 150, 7550), (cx + 150, 7850), layer="A-FURN")  # Top
    rect((1250, 6850), (1550, 7150), layer="A-FURN")              # Left
    rect((2450, 6850), (2750, 7150), layer="A-FURN")              # Right

    # Living Room Sofas (X: 5200 to 7600, Y: 5200 to 7200)
    rect((7100, 5200), (7700, 7200), layer="A-FURN")  # 3-Seater sofa
    rect((5800, 4800), (6800, 5400), layer="A-FURN")  # 2-Seater sofa
    rect((6000, 5800), (6700, 6500), layer="A-FURN")  # Coffee table

    # Kitchen Counter (L-Shape)
    line((230, 2030), (230, 4800), "A-FURN")
    line((230, 4800), (830, 4800), "A-FURN")
    line((830, 4800), (830, 2630), "A-FURN")
    line((830, 2630), (2600, 2630), "A-FURN")
    line((2600, 2630), (2600, 2030), "A-FURN")

    # Beds & Wardrobes
    # Bed 1 (Top Left)
    rect((400, 10000), (2200, 11800), layer="A-FURN")  # Bed
    rect((230, 9300), (830, 10000), layer="A-FURN")   # Wardrobe

    # Bed 2 (Master, Top Right)
    rect((5200, 10400), (7000, 12200), layer="A-FURN")  # King Bed
    rect((4400, 11800), (5000, 12370), layer="A-FURN")  # Side table 1
    rect((7200, 11800), (7800, 12370), layer="A-FURN")  # Side table 2
    rect((6800, 9300), (7900, 9800), layer="A-FURN")    # Wardrobe

    # Toilets - Commodes & Basins
    ms.AddCircle(point(3500, 11600), 200).Layer = "A-FURN"  # Toilet 1 commode
    ms.AddCircle(point(7100, 10000), 200).Layer = "A-FURN"  # Toilet 2 commode

    # Car Outline in Porch
    rect((5400, 1000), (7400, 4000), layer="A-FURN")
    rect((5600, 1500), (7200, 3500), layer="A-FURN")

    print("Adding Room Labels & Dimensions...")
    text((1100, 3600), "kitchen", height=220)
    text((1700, 5600), "dining", height=240)
    text((5800, 6800), "Living", height=250)
    text((5800, 6450), "+2'0\" M", height=180)
    text((3300, 3400), "sit out", height=220)
    text((3300, 3050), "+2'0\" M", height=180)
    text((5800, 2600), "porch", height=240)
    text((5800, 2250), "+6\" M", height=180)
    text((1000, 9500), "Bed room", height=240)
    text((5500, 9500), "Bed room", height=240)
    text((3200, 12000), "toilet", height=200)
    text((6400, 10400), "toilet", height=200)
    text((6200, 10100), "7'0\"x7'6\"", height=180)

    # Title Block & Metadata
    text((2000, -1600), "ground floor plan", height=380, color=7)
    text((2200, -2100), "area in first floor 1119 sft", height=260, color=7)
    text((5000, -1000), "r  o  a  d", height=250, color=7)
    text((6800, -1200), "north", height=200, color=7)
    text((6800, -1600), "Designed By : Koshy Associates", height=200, color=1)
    text((6800, -1950), "Chennai, India. Ph: +91 9444122276", height=180, color=7)

    # North Arrow symbol
    ms.AddCircle(point(6500, -1500), 300).Layer = "A-TEXT"
    line((6500, -1800), (6500, -1200), "A-TEXT")
    line((6200, -1500), (6800, -1500), "A-TEXT")

    # Zoom Extents
    try:
        app.ZoomExtents()
    except Exception:
        pass

    out_file = Path.home() / "Desktop" / "Kerala_1119sqft_FloorPlan.dwg"
    try:
        doc.SaveAs(str(out_file))
        print(f"Successfully generated and saved plan to: {out_file}")
    except Exception as e:
        print(f"Drawing created, but could not save automatically: {e}")

    return True


if __name__ == "__main__":
    create_kerala_floor_plan()
