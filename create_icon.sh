#!/bin/bash
# Creates AppIcon.icns for GIF Screen Recorder
# Requires: brew install librsvg   OR   pip3 install cairosvg
set -e
echo "Creating app icon..."

cat > /tmp/sr_icon.svg << 'SVGEOF'
<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 1024 1024" width="1024" height="1024">
  <!-- Black bezel — fills canvas edge to edge with small margin -->
  <rect x="20" y="110" width="984" height="800" rx="42" fill="#000000"/>
  <!-- Screen body -->
  <rect x="36" y="126" width="952" height="768" rx="32" fill="#1c1c1e"/>
  <!-- Island — flush with top bezel -->
  <rect x="462" y="110" width="100" height="42" rx="10" fill="#000000"/>
  <!-- Display surface -->
  <rect x="52" y="168" width="920" height="700" rx="20" fill="#111111"/>

  <!-- Rec ring: cx=360 cy=520 r=200 stroke=34
       ring inner edge = 200-17=183, dot r=123, gap=60
       dent height = 60, width = 123+18=141 from center -->
  <circle cx="360" cy="520" r="200" fill="none" stroke="#ffffff" stroke-width="34"/>
  <circle cx="360" cy="520" r="123" fill="#ffffff"/>
  <rect x="360" y="490" width="141" height="60" rx="8" fill="#111111"/>

  <!-- IF -->
  <text x="610" y="608"
    font-family="-apple-system, Helvetica Neue, Helvetica, sans-serif"
    font-weight="700" font-size="260" fill="#ffffff">I</text>
  <text x="752" y="608"
    font-family="-apple-system, Helvetica Neue, Helvetica, sans-serif"
    font-weight="700" font-size="260" fill="#ffffff">F</text>

  <!-- RECORD — right-aligned to end of F (~970), below IF -->
  <text x="575" y="690"
    font-family="-apple-system, Helvetica Neue, Helvetica, sans-serif"
    font-weight="400" font-size="62" letter-spacing="10"
    fill="#ffffff">RECORD</text>
</svg>
SVGEOF

ICONSET="/tmp/AppIcon.iconset"
mkdir -p "$ICONSET"

if command -v rsvg-convert &>/dev/null; then
    do_convert() { rsvg-convert -w $1 -h $1 /tmp/sr_icon.svg -o "$2"; }
elif python3 -c "import cairosvg" 2>/dev/null; then
    do_convert() { python3 -c "import cairosvg; cairosvg.svg2png(url='/tmp/sr_icon.svg',write_to='$2',output_width=$1,output_height=$1)"; }
else
    echo "❌  Install librsvg or cairosvg:"; echo "    brew install librsvg"; exit 1
fi

for SIZE in 16 32 64 128 256 512 1024; do
    do_convert $SIZE "$ICONSET/icon_${SIZE}x${SIZE}.png"
done
cp "$ICONSET/icon_32x32.png"     "$ICONSET/icon_16x16@2x.png"
cp "$ICONSET/icon_64x64.png"     "$ICONSET/icon_32x32@2x.png"
cp "$ICONSET/icon_256x256.png"   "$ICONSET/icon_128x128@2x.png"
cp "$ICONSET/icon_512x512.png"   "$ICONSET/icon_256x256@2x.png"
cp "$ICONSET/icon_1024x1024.png" "$ICONSET/icon_512x512@2x.png"
iconutil -c icns "$ICONSET" -o "AppIcon.icns"
rm -rf "$ICONSET" /tmp/sr_icon.svg
echo "✅  AppIcon.icns created"
