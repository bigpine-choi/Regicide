"use strict";
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../..');
const fixtures = [
  {state:JSON.parse(fs.readFileSync(path.join(__dirname,'morphy/actual-state.json'),'utf8')),
   white:'백의 오프닝 · Ruy Lopez Opening',black:'흑의 대응 · Morphy Defense'},
  {state:JSON.parse(fs.readFileSync(path.join(__dirname,'sicilian-position/actual-state.json'),'utf8')),
   white:'백의 오프닝 · 현재 배치: Open Sicilian',black:'흑의 대응 · 현재 배치: Sicilian Defense'}
];
const source = fs.readFileSync(path.join(root,'tests/opening_guide_web_tests.js'),'utf8');
const actualCases = `
test("real backend side payloads render through shipped browser client", async () => {
  for (const fixture of fixtures) {
    const ui = await browser(fixture.state);
    assert.equal(ui.element("opening-white").textContent, fixture.white);
    assert.equal(ui.element("opening-black").textContent, fixture.black);
    assert.equal(ui.requests.length, 1);
  }
});
`;
new Function('require','__dirname','fixtures',source.replace('(async () => {',actualCases+'\n(async () => {'))(
  require,path.join(root,'tests'),fixtures);
