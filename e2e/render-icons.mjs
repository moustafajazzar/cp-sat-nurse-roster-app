// Renders frontend/public/favicon.svg, the app's icon, to the PNGs the web app manifest and iOS ask for, into frontend/public. Run again after
// changing the SVG:  cd e2e && node render-icons.mjs
// The "any" icons are rounded, transparent at the corners; the maskable one and Apple's are the full square, which the
// launcher or iOS masks itself.
import { readFileSync } from "node:fs"
import { chromium } from "@playwright/test"

const svg = readFileSync(new URL("../frontend/public/favicon.svg", import.meta.url), "utf8")
const out = (name) => new URL(`../frontend/public/${name}`, import.meta.url).pathname
const browser = await chromium.launch()
for (const [name, size, rounded] of [["icon-192.png", 192, true], ["icon-512.png", 512, true], ["icon-maskable-512.png", 512, false], ["apple-touch-icon.png", 180, false]]) {
  const page = await browser.newPage({ viewport: { width: size, height: size } })
  const radius = rounded ? `${Math.round(size * 0.1875)}px` : "0"
  await page.setContent(`<style>html,body{margin:0;background:transparent}div{width:${size}px;height:${size}px;border-radius:${radius};overflow:hidden}svg{display:block;width:100%;height:100%}</style><div>${svg}</div>`)
  await page.screenshot({ path: out(name), omitBackground: true })
  await page.close()
}
await browser.close()
