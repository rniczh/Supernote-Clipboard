// Draws the 1024 px app icon: a white page with a dashed selection on a black squircle.
// Usage: make_icon OUTPUT.png

import AppKit

let side = 1024
guard let rep = NSBitmapImageRep(
    bitmapDataPlanes: nil, pixelsWide: side, pixelsHigh: side, bitsPerSample: 8, samplesPerPixel: 4,
    hasAlpha: true, isPlanar: false, colorSpaceName: .deviceRGB, bytesPerRow: 0, bitsPerPixel: 0
) else { fatalError("cannot create bitmap") }

NSGraphicsContext.saveGraphicsState()
NSGraphicsContext.current = NSGraphicsContext(bitmapImageRep: rep)

// Background squircle on Apple's icon grid (824 px body inside the 1024 px canvas).
let body = NSRect(x: 100, y: 100, width: 824, height: 824)
let background = NSBezierPath(roundedRect: body, xRadius: 185, yRadius: 185)
NSGradient(starting: NSColor(white: 0.24, alpha: 1), ending: NSColor(white: 0.06, alpha: 1))!
    .draw(in: background, angle: -90)

// The note page, with a few handwritten-looking lines.
let page = NSRect(x: 262, y: 200, width: 500, height: 624)
NSColor(white: 0.97, alpha: 1).setFill()
NSBezierPath(roundedRect: page, xRadius: 28, yRadius: 28).fill()
NSColor(white: 0.55, alpha: 1).setStroke()
for (i, width) in [300.0, 360.0, 240.0].enumerated() {
    let y = 730.0 - Double(i) * 58
    let line = NSBezierPath()
    line.move(to: NSPoint(x: 322, y: y))
    line.line(to: NSPoint(x: 322 + width, y: y))
    line.lineWidth = 16
    line.lineCapStyle = .round
    line.stroke()
}

// The pasted screenshot: a small landscape picture.
let picture = NSRect(x: 322, y: 280, width: 380, height: 250)
NSColor(white: 0.12, alpha: 1).setFill()
NSBezierPath(roundedRect: picture, xRadius: 14, yRadius: 14).fill()
NSColor.white.setFill()
NSBezierPath(ovalIn: NSRect(x: 382, y: 430, width: 56, height: 56)).fill()
let mountains = NSBezierPath()
mountains.move(to: NSPoint(x: 340, y: 300))
mountains.line(to: NSPoint(x: 470, y: 420))
mountains.line(to: NSPoint(x: 540, y: 360))
mountains.line(to: NSPoint(x: 600, y: 410))
mountains.line(to: NSPoint(x: 684, y: 300))
mountains.close()
mountains.fill()

// The dashed capture selection around the picture.
let selection = NSBezierPath(roundedRect: picture.insetBy(dx: -34, dy: -34), xRadius: 22, yRadius: 22)
selection.lineWidth = 14
selection.setLineDash([44, 26], count: 2, phase: 0)
NSColor(calibratedRed: 0.16, green: 0.52, blue: 1.0, alpha: 1).setStroke()
selection.stroke()

NSGraphicsContext.restoreGraphicsState()

guard let png = rep.representation(using: .png, properties: [:]) else { fatalError("cannot encode PNG") }
try! png.write(to: URL(fileURLWithPath: CommandLine.arguments[1]))
