"""Create a detailed, editable 2BHK concept plan in the active AutoCAD install."""

from __future__ import annotations

import math
from pathlib import Path

import pythoncom
import pywintypes
import win32com.client


OUT = Path.home() / "Desktop" / "2BHK_Detailed_Plan.dwg"


def point(x: float, y: float, z: float = 0.0):
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, (float(x), float(y), float(z))
    )


def coords(values):
    return win32com.client.VARIANT(
        pythoncom.VT_ARRAY | pythoncom.VT_R8, tuple(float(v) for v in values)
    )


pythoncom.CoInitialize()
clsid = pywintypes.IID("{8B4929F8-076F-4AEC-AFEE-8928747B7AE3}")
unknown = pythoncom.GetActiveObject(clsid)
dispatch = unknown.QueryInterface(pythoncom.IID_IDispatch)
app = win32com.client.Dispatch(dispatch)
app.Visible = True
doc = app.Documents.Add()
ms = doc.ModelSpace

doc.SetVariable("INSUNITS", 4)  # millimetres
doc.SetVariable("LUNITS", 2)
doc.SetVariable("LUPREC", 0)


def layer(name: str, color: int, lineweight: int | None = None):
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


for args in [
    ("A-WALL", 7, 50),
    ("A-DOOR", 2, 25),
    ("A-WINDOW", 4, 25),
    ("A-FURN", 8, 18),
    ("A-FIXTURE", 3, 18),
    ("A-TEXT", 7, 18),
    ("A-DIMS", 6, 18),
    ("A-ROOM", 9, 9),
    ("A-NOTES", 5, 18),
]:
    layer(*args)


def line(x1, y1, x2, y2, lay="A-FURN"):
    obj = ms.AddLine(point(x1, y1), point(x2, y2))
    obj.Layer = lay
    return obj


def polyline(points, lay="A-FURN", closed=False, width=0.0):
    flat = [value for xy in points for value in xy]
    obj = ms.AddLightWeightPolyline(coords(flat))
    obj.Layer = lay
    obj.Closed = closed
    if width:
        obj.ConstantWidth = width
    return obj


def rect(x1, y1, x2, y2, lay="A-FURN"):
    return polyline([(x1, y1), (x2, y1), (x2, y2), (x1, y2)], lay, True)


def circle(x, y, radius, lay="A-FURN"):
    obj = ms.AddCircle(point(x, y), radius)
    obj.Layer = lay
    return obj


def text(value, x, y, height=180, lay="A-TEXT", rotation=0.0):
    obj = ms.AddText(value, point(x, y), height)
    obj.Layer = lay
    obj.Rotation = rotation
    return obj


def wall(x1, y1, x2, y2, thickness=100):
    return polyline([(x1, y1), (x2, y2)], "A-WALL", False, thickness)


def door(hx, hy, width, closed_angle, open_angle):
    ex = hx + width * math.cos(open_angle)
    ey = hy + width * math.sin(open_angle)
    line(hx, hy, ex, ey, "A-DOOR")
    start, end = sorted((closed_angle, open_angle))
    arc = ms.AddArc(point(hx, hy), width, start, end)
    arc.Layer = "A-DOOR"
    circle(hx, hy, 35, "A-DOOR")


def window_horizontal(x1, x2, y):
    for offset in (-45, 0, 45):
        line(x1, y + offset, x2, y + offset, "A-WINDOW")
    line(x1, y - 90, x1, y + 90, "A-WINDOW")
    line(x2, y - 90, x2, y + 90, "A-WINDOW")


def window_vertical(x, y1, y2):
    for offset in (-45, 0, 45):
        line(x + offset, y1, x + offset, y2, "A-WINDOW")
    line(x - 90, y1, x + 90, y1, "A-WINDOW")
    line(x - 90, y2, x + 90, y2, "A-WINDOW")


def room_label(name, size, x, y):
    text(name, x, y + 110, 210)
    text(size, x, y - 140, 130, "A-ROOM")


def bed(x, y, width=1800, depth=2000):
    rect(x, y, x + width, y + depth)
    line(x, y + 450, x + width, y + 450)
    rect(x + 100, y + 80, x + width / 2 - 50, y + 400)
    rect(x + width / 2 + 50, y + 80, x + width - 100, y + 400)
    line(x + width / 2, y + 450, x + width / 2, y + depth)


def wardrobe(x1, y1, x2, y2):
    rect(x1, y1, x2, y2)
    line(x1, y1, x2, y2)
    line(x1, y2, x2, y1)


def aligned_dim(x1, y1, x2, y2, tx, ty):
    obj = ms.AddDimAligned(point(x1, y1), point(x2, y2), point(tx, ty))
    obj.Layer = "A-DIMS"
    try:
        obj.TextHeight = 180
        obj.ArrowheadSize = 140
    except Exception:
        pass
    return obj


# Exterior wall, with a 1000 mm main-door opening in the south wall.
wall(0, 0, 900, 0, 200)
wall(1900, 0, 12000, 0, 200)
wall(12000, 0, 12000, 9000, 200)
wall(12000, 9000, 0, 9000, 200)
wall(0, 9000, 0, 0, 200)

# Front-zone partitions and utility divider.
wall(4800, 0, 4800, 1050)
wall(4800, 1950, 4800, 4400)
wall(7600, 0, 7600, 1250)
wall(7600, 2150, 7600, 4400)
wall(7600, 3250, 8500, 3250)
wall(9400, 3250, 12000, 3250)

# Main horizontal partition. Openings lead to bedroom 1, passage and master.
for x1, x2 in [(0, 900), (1800, 6000), (6900, 8200), (9100, 12000)]:
    wall(x1, 4400, x2, 4400)

# Rear partitions: bedrooms, central passage/service rooms, attached toilet.
wall(4100, 4400, 4100, 5200)
wall(4100, 6100, 4100, 9000)
wall(7600, 4400, 7600, 5200)
wall(7600, 6100, 7600, 9000)
wall(4100, 6500, 4550, 6500)
wall(5350, 6500, 6200, 6500)
wall(7000, 6500, 7600, 6500)
wall(5800, 6500, 5800, 9000)
wall(10200, 6800, 10200, 7350)
wall(10200, 8150, 10200, 9000)
wall(10200, 6800, 12000, 6800)

# Doors: entry, internal rooms, utility, toilets and store/puja.
door(900, 0, 1000, 0, math.pi / 2)
door(4800, 1050, 900, math.pi / 2, 0)
door(7600, 1250, 900, math.pi / 2, math.pi)
door(8500, 3250, 900, 0, math.pi / 2)
door(900, 4400, 900, 0, math.pi / 2)
door(6000, 4400, 900, 0, math.pi / 2)
door(8200, 4400, 900, 0, math.pi / 2)
door(4100, 5200, 900, math.pi / 2, 0)
door(7600, 5200, 900, math.pi / 2, math.pi)
door(4550, 6500, 800, 0, math.pi / 2)
door(6200, 6500, 800, 0, math.pi / 2)
door(10200, 7350, 800, math.pi / 2, math.pi)

# Exterior windows and ventilators.
window_horizontal(2500, 4000, 0)
window_horizontal(8200, 10100, 0)
window_horizontal(900, 2500, 9000)
window_horizontal(8100, 9700, 9000)
window_horizontal(10600, 11400, 9000)
window_vertical(0, 1800, 3300)
window_vertical(0, 6200, 7700)
window_vertical(12000, 900, 2400)
window_vertical(12000, 5000, 6100)
window_vertical(12000, 7400, 8200)

# Living furniture.
rect(500, 800, 1050, 3000)
rect(1050, 800, 3000, 1350)
rect(1750, 1900, 3000, 2600)
rect(3400, 900, 3600, 3100)
text("TV", 3650, 1900, 140, "A-FURN", math.pi / 2)

# Dining table and six chairs.
rect(5450, 1350, 6950, 2650)
for cx, cy in [(5700, 1100), (6700, 1100), (5700, 2900), (6700, 2900), (5200, 1700), (7200, 2200)]:
    circle(cx, cy, 180)

# Kitchen counters, sink, hob and refrigerator.
rect(7900, 300, 11700, 900, "A-FIXTURE")
rect(11100, 900, 11700, 2850, "A-FIXTURE")
rect(8200, 430, 9200, 780, "A-FIXTURE")
circle(8500, 605, 110, "A-FIXTURE")
circle(8850, 605, 110, "A-FIXTURE")
for sx in (9900, 10200, 10500, 10800):
    circle(sx, 600, 95, "A-FIXTURE")
rect(10600, 2050, 11550, 2800, "A-FIXTURE")
text("FRIDGE", 10700, 2380, 120, "A-FIXTURE")

# Utility appliances.
circle(8250, 3800, 380, "A-FIXTURE")
text("WM", 8120, 3740, 120, "A-FIXTURE")
rect(10100, 3450, 11600, 4150, "A-FIXTURE")

# Bedrooms.
bed(650, 5750, 1800, 2000)
wardrobe(3000, 5200, 3850, 8500)
rect(450, 5150, 950, 5600)
rect(2550, 5150, 3050, 5600)

bed(7900, 4900, 2000, 2100)
wardrobe(7900, 7900, 9800, 8650)
rect(10300, 5000, 11600, 5600)
text("DRESSER", 10420, 5210, 120)

# Toilet fixtures and service cupboard.
for x, y in [(5000, 8150), (11000, 8200)]:
    rect(x - 260, y - 380, x + 260, y + 380, "A-FIXTURE")
    circle(x, y - 100, 170, "A-FIXTURE")
for x, y in [(4700, 7000), (10600, 7150)]:
    rect(x, y, x + 700, y + 450, "A-FIXTURE")
    circle(x + 350, y + 220, 130, "A-FIXTURE")
wardrobe(6100, 7000, 7350, 8500)

# Room names and clear-size notes.
room_label("LIVING", "4.6 x 4.2 m", 1800, 3550)
room_label("DINING", "2.7 x 4.2 m", 5500, 3550)
room_label("KITCHEN", "4.2 x 3.1 m", 9000, 1700)
room_label("UTILITY", "4.2 x 1.0 m", 9000, 3700)
room_label("BEDROOM 1", "3.9 x 4.5 m", 1200, 8200)
room_label("PASSAGE", "3.4 x 2.0 m", 5150, 5550)
room_label("COMMON TOILET", "1.6 x 2.4 m", 4250, 8650)
room_label("STORE / PUJA", "1.7 x 2.4 m", 6000, 8650)
room_label("MASTER BEDROOM", "4.3 x 4.5 m", 8050, 7450)
room_label("ATT. TOILET", "1.7 x 2.1 m", 10300, 8650)

# Overall and major setting-out dimensions.
aligned_dim(0, 0, 12000, 0, 6000, -700)
aligned_dim(0, 0, 0, 9000, -700, 4500)
aligned_dim(0, 9000, 4100, 9000, 2050, 9550)
aligned_dim(4100, 9000, 7600, 9000, 5850, 9550)
aligned_dim(7600, 9000, 12000, 9000, 9800, 9550)
aligned_dim(12000, 0, 12000, 4400, 12600, 2200)
aligned_dim(12000, 4400, 12000, 9000, 12600, 6700)

# North arrow, title and project notes.
line(13200, 7100, 13200, 8500, "A-NOTES")
polyline([(12950, 8150), (13200, 8600), (13450, 8150)], "A-NOTES", True)
text("N", 13120, 8750, 260, "A-NOTES")
text("DETAILED 2BHK CONCEPT PLAN", 0, -1450, 300, "A-NOTES")
text("ALL DIMENSIONS ARE IN MILLIMETRES", 0, -1850, 180, "A-NOTES")
text("200 mm EXTERNAL WALLS / 100 mm INTERNAL WALLS", 0, -2150, 150, "A-NOTES")
text("CONCEPT DRAWING - VERIFY STRUCTURE, SERVICES AND LOCAL CODES BEFORE CONSTRUCTION", 0, -2450, 130, "A-NOTES")

doc.Regen(1)
app.ZoomExtents()
doc.SaveAs(str(OUT))
print(f"CREATED={OUT}")
print(f"DOCUMENT={doc.Name}")
print(f"ENTITIES={ms.Count}")

