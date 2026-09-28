"""Sanity-check the captured GUI screenshot: is the interface really drawn?"""
from PIL import Image

image = Image.open(r"C:\Users\neuro\.dsh\translator\runtime\artifacts\gui_running.png").convert("RGB")
width, height = image.size
print(f"size: {width}x{height}")

bands = {
    "toolbar (top)": (0, 0, width, 80),
    "preview area (left)": (0, 100, int(width * 0.55), height - 240),
    "status panel (right)": (int(width * 0.55), 100, width, 560),
    "tabs (bottom)": (0, height - 240, width, height),
}
for name, box in bands.items():
    region = image.crop(box)
    colors = region.getcolors(maxcolors=1000000) or []
    light = sum(count for count, color in colors if min(color) > 180)
    accent = sum(count for count, color in colors if color[2] > 180 and color[0] < 140)
    print(f"  {name:<22} colours={len(colors):<6} light(text)px={light:<7} accent(blue)px={accent}")
