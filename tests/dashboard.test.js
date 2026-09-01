"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const test = require("node:test");
const vm = require("node:vm");

const html = fs.readFileSync(
  path.join(__dirname, "../src/delta_exchange_mcp/skills_data/pnl-analytics/assets/dashboard.html"),
  "utf8"
);

function sourceOf(name, nextMarker) {
  const start = html.indexOf("  function " + name + "(");
  const end = html.indexOf(nextMarker, start);
  assert.notEqual(start, -1);
  assert.notEqual(end, -1);
  return html.slice(start, end);
}

test("dashboard program compiles", () => {
  const source = html.split("<script>\n", 2)[1].split("</script>", 1)[0];
  assert.doesNotThrow(() => new vm.Script(source));
});

test("money formatting preserves sub-cent charges", () => {
  const context = {};
  vm.runInNewContext(sourceOf("num", "\n  function pct"), context);

  assert.equal(context.money(0.004), "$0.004");
  assert.equal(context.money(0.00000007), "$0.00000007");
  assert.equal(context.money(12), "$12.00");
});

test("calendar positions sparse days from their UTC dates", () => {
  const context = {
    C: { pos: "green", neg: "red", dim: "gray" },
    empty: (message) => message,
    esc: String,
    money: String,
    svg: (_width, _height, inner) => inner,
  };
  vm.runInNewContext(sourceOf("calendar", "\n\n  // Correlation matrix"), context);
  const result = context.calendar(
    [
      { date: "2026-08-03", pnl: 1, trades: 1 },
      { date: "2026-08-05", pnl: 2, trades: 1 },
    ],
    "calendar"
  );

  assert.match(result, /x="16" y="8"/);
  assert.match(result, /x="16" y="40"/);
  assert.doesNotMatch(result, /x="16" y="24"/);
});

test("dashboard distinguishes unavailable funding from fetched zero", () => {
  assert.match(html, /fundingKnown = head\.funding !== null/);
  assert.match(html, /Funding was not fetched for this report\./);
  assert.doesNotMatch(html, /money\(\(\+head\.net_pnl \|\| 0\) \+ \(\+head\.funding \|\| 0\)\)/);
});

test("dashboard keeps other unavailable values out of numeric output", () => {
  assert.match(html, /ch\.trades_to_cover !== null/);
  assert.match(html, /Open positions were not fetched for this report\./);
});
