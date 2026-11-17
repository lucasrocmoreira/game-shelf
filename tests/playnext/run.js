/* Play next golden test.
   Serves site/ with a frozen copy of the data (tests/playnext/fixtures), runs the page's own Play next
   on every console (first round plus 10 "Replace all three" rounds) and writes a readable golden file,
   then checks each of Lucas's preferences. Usage:
     node tests/playnext/run.js [out.txt]        (default: tests/playnext/picks.golden.txt)
   Exit code 1 if an assertion fails. Needs Playwright (preinstalled where Claude works). */
const fs = require("fs"), path = require("path"), zlib = require("zlib");
const {chromium} = require("playwright");
const ROOT = path.resolve(__dirname, "../.."), FIX = path.join(__dirname, "fixtures");
const OUT = process.argv[2] || path.join(__dirname, "picks.golden.txt");
const CONSOLES = ["ps5", "ps3", "vita", "wii", "switch2", "ds", "3ds", "steam"];
const ROUNDS = 10;

(async () => {
  const fixture = name => zlib.gunzipSync(fs.readFileSync(path.join(FIX, name + ".json.gz")));
  const browser = await chromium.launch();
  const page = await browser.newPage();
  const errors = [];
  page.on("pageerror", e => errors.push(e.message));
  await page.route("**/*", route => {
    const u = new URL(route.request().url());
    if (u.hostname !== "shelf.test") return route.abort();  // no network: GitHub, images, fonts
    const f = u.pathname.replace(/^\//, "") || "index.html";
    const m = f.match(/^(data|psplus|franchises)\.json$/);
    if (m) return route.fulfill({body: fixture(m[1]), contentType: "application/json"});
    const p = path.join(ROOT, "site", f);
    if (fs.existsSync(p) && fs.statSync(p).isFile())
      return route.fulfill({body: fs.readFileSync(p), contentType: f.endsWith(".html") ? "text/html" : "application/json"});
    return route.fulfill({status: 404, body: ""});
  });
  await page.goto("http://shelf.test/index.html");
  await page.waitForFunction(() => typeof FR !== "undefined" && FR.data && VIEWS.library && VIEWS.library.data, null, {timeout: 20000});

  const lines = [], fails = [];
  for (const v of CONSOLES) {
    const res = await page.evaluate(async ({v, ROUNDS}) => {
      await showView(v);
      const t0 = performance.now();
      openSuggest("restart");
      const ms = Math.round(performance.now() - t0);
      const fmt = p => {
        const g = p.game, h = typeof beat === "function" ? beat(g) : null;
        return {title: g.title, key: g.key, score: avg(g), year: g.orig_year || g.year, hours: h,
                series: seriesOf(g).slice(0, 2), style: typeof styleOf === "function" ? styleOf(g) : null,
                why: p.why, earlier: p.earlier ? p.earlier.game.title + " [" + p.earlier.game.platforms.join("/") + "]" : null,
                better: p.better ? p.better.title + " [" + p.better.platforms.join("/") + "]" : null,
                lacking: p.lacking || null, notOwned: p.notOwned || null};
      };
      const check = () => typeof checkPicks === "function" ? checkPicks(SUGGEST.slots) : [];
      const rounds = [SUGGEST.slots.filter(Boolean).map(fmt)], problems = check();
      for (let r = 0; r < ROUNDS; r++) { openSuggest("all"); rounds.push(SUGGEST.slots.filter(Boolean).map(fmt)); problems.push(...check()); }
      document.getElementById("dlg") && document.getElementById("dlg").close && document.getElementById("dlg").close();
      const ctx = typeof playContext === "function" ? playContext() : null;
      return {rounds, ms, problems: [...new Set(problems)], ctx: ctx ? {playing: ctx.playing.map(g => g.title), recent: ctx.recent.map(g => g.title)} : null};
    }, {v, ROUNDS});
    lines.push(`=== ${v}  (first round ${res.ms} ms)`);
    if (res.ctx) lines.push(`context: playing ${res.ctx.playing.join(", ") || "-"} | recent ${res.ctx.recent.join(", ") || "-"}`);
    res.rounds.forEach((picks, i) => {
      lines.push(`-- ${i ? "replace all #" + i : "first round"}`);
      for (const p of picks) {
        lines.push(`  ${p.title} [${p.score}] ${p.year || "?"} ${p.hours != null ? p.hours + "h" : "?h"}${p.style ? " " + p.style : ""}${p.series.length ? " {" + p.series.join(", ") + "}" : ""}`);
        if (i === 0) for (const w of p.why) lines.push(`      · ${w}`);
        if (p.earlier) lines.push(`      > but play first: ${p.earlier}`);
        if (p.better) lines.push(`      > better version: ${p.better}`);
        if (p.lacking) lines.push(`      > ${p.lacking}`);
        if (i === 0 && p.notOwned) lines.push(`      > ${p.notOwned}`);
      }
    });
    // Assertions that hold for any algorithm version
    const first = res.rounds[0];
    if (first.length && new Set(first.map(p => p.key)).size !== first.length) fails.push(`${v}: duplicate pick`);
    fails.push(...res.problems);
    if (res.ms > 1500) fails.push(`${v}: first round took ${res.ms} ms`);
    lines.push("");
  }
  if (errors.length) fails.push("page errors: " + errors.slice(0, 3).join(" | "));
  fs.writeFileSync(OUT, lines.join("\n") + "\n");
  console.log(`wrote ${OUT}`);
  if (fails.length) { console.log("FAILED:\n  " + fails.join("\n  ")); process.exitCode = 1; }
  else console.log("all checks passed");
  await browser.close();
})();
