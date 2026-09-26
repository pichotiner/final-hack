// Standalone synthetic preview acceptance, not a production API/browser A04.
// node scripts/check-production-preview.mjs file:///absolute/path/pi01-preview.html
import assert from "node:assert/strict";
import { chromium } from "playwright";

const url = process.argv[2];
if (!url || !url.startsWith("file://")) throw new Error("Supply the generated local preview file URL");
const browser = await chromium.launch({ headless: true });
try {
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", error => errors.push(error.message));
  await page.addInitScript(() => {
    window.__starts = 0;
    const original = OscillatorNode.prototype.start;
    OscillatorNode.prototype.start = function (...args) {
      window.__starts += 1;
      return original.apply(this, args);
    };
  });
  await page.goto(url);
  assert.match(await page.locator("body").innerText(), /СИНТЕТИЧЕСКИЙ ЭКСПЕРИМЕНТ/);
  const alarm = async () => JSON.parse(await page.locator("#alarm").innerText());
  assert.deepEqual((await alarm()).active, ["warning-1", "warning-2"]);
  assert.equal(await page.evaluate(() => window.__starts), 0);
  await page.locator("#sound").click();
  await page.waitForFunction(() => window.__starts === 2);
  await page.locator("#sound").click();
  assert.equal(await page.evaluate(() => window.__starts), 2);
  await page.locator("#ack").click();
  assert.deepEqual((await alarm()).active, ["warning-1", "warning-2"]);
  assert.deepEqual((await alarm()).sound_tokens, ["warning-2"]);
  await page.locator("#offline").click();
  assert.equal((await alarm()).light, "unavailable");
  await page.locator("#sound").click();
  assert.equal(await page.evaluate(() => window.__starts), 2);
  await page.reload();
  assert.equal(await page.evaluate(() => window.__starts), 0);
  assert.deepEqual(errors, []);
  console.log("PASS synthetic preview: two episodes; two oscillator starts; no repeat; ACK keeps open; offline; reload silent");
} finally {
  await browser.close();
}
