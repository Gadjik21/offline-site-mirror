const assert = require("node:assert/strict");
const puppeteer = require("/app/node_modules/puppeteer-core");

async function run() {
  const [, , origin, first, second] = process.argv;
  const browser = await puppeteer.launch({
    executablePath: "/usr/bin/brave-origin", headless: true,
    args: ["--no-sandbox", "--no-proxy-server"],
  });
  try {
    const page = await browser.newPage();
    await page.goto(origin, { waitUntil: "load" });
    await page.waitForFunction(() => document.body.dataset.label === "recorded");
    await page.waitForFunction(path => {
      const image = document.getElementById("hero");
      return new URL(image.currentSrc).pathname === path && image.naturalWidth > 0;
    }, {}, first);
    const frame = await (await page.$("#embedded")).contentFrame();
    await frame.waitForFunction(() => document.body.textContent.trim() === "one");
    await page.click("#next");
    await page.waitForFunction(path => {
      const image = document.getElementById("hero");
      return new URL(image.currentSrc).pathname === path && image.naturalWidth > 0;
    }, {}, second);
    await page.$eval("#lazy", el => el.scrollIntoView());
    await page.waitForFunction(() => document.getElementById("lazy").naturalWidth > 0);
    assert.equal(await page.$eval("body", node => getComputedStyle(node).color), "rgb(24, 69, 120)");
    await Promise.all([page.waitForNavigation(), page.click('a[href="/details/index.html"]')]);
    assert.equal(await page.$eval("h1", node => node.textContent), "Details");
    console.log(JSON.stringify({ images: 3, distinctQueryVariants: true, lazy: true, fetch: true, navigation: true }));
  } finally {
    await browser.close();
  }
}
run().catch(error => { console.error(error); process.exitCode = 1; });
