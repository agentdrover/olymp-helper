// Run the exam HTML script with a delayed page and a skewed local clock.
"use strict";

const assert = require("node:assert/strict");
const fs = require("node:fs");
const path = require("node:path");
const vm = require("node:vm");

const template = path.join(__dirname, "..", "..", "app", "templates", "child", "exam_run.html");
const html = fs.readFileSync(template, "utf8");
const script = html.slice(html.indexOf("<script>") + 8, html.indexOf("</script>"));

const serverNow = Date.parse("2026-01-01T10:00:00Z");
const deadline = serverNow + 60000;
let elapsed = 10000;
class LocalDate extends Date {
  static now() { return serverNow + 300000 + elapsed; }
}
const left = {
  dataset: {
    deadline: new Date(deadline).toISOString(),
    now: new Date(serverNow).toISOString(),
  },
  textContent: "",
};
let tick;
let backgroundRequests = 0;
const callbacks = new Map();
const windowCallbacks = new Map();
const controls = [{disabled: false}, {disabled: false}];
const form = {
  addEventListener(type, callback) { callbacks.set(type, callback); },
  querySelectorAll() { return controls; },
};
const location = {href: "", reloads: 0, reload() { this.reloads++; }};
let disableAfterSubmit;
let confirmations = 0;
vm.runInNewContext(script, {
  Date: LocalDate,
  document: {getElementById: id => id === "exam-left" ? left : form},
  performance: {
    now: () => elapsed,
    getEntriesByType: () => [{requestStart: 0, responseStart: 10000}],
  },
  setInterval: callback => { tick = callback; },
  clearInterval() {},
  setTimeout: callback => { disableAfterSubmit = callback; },
  confirm: () => { confirmations++; return false; },
  location,
  window: {addEventListener(type, callback) { windowCallbacks.set(type, callback); }},
  fetch() { backgroundRequests++; },
});
tick();
assert.equal(left.textContent, "0:50", "timer should count the ten seconds spent delivering the page");
elapsed += 50001;
tick();
assert.equal(left.textContent, "0:00");
assert.equal(backgroundRequests, 0);
assert.equal(location.href, "", "the estimate alone must not close the form early");

function submit(id) {
  const event = {
    submitter: {id},
    prevented: false,
    preventDefault() { this.prevented = true; },
  };
  callbacks.get("submit")(event);
  return !event.prevented;
}
assert.equal(submit("exam-submit"), false);
assert.equal(confirmations, 1);
assert.equal(submit("save"), true, "a normal POST may still reach the server in time");
assert.equal(submit("save"), false, "the same page must not send a second POST");
assert(controls.every(control => !control.disabled), "the initial POST must include the fields");
disableAfterSubmit();
assert(controls.every(control => control.disabled));
windowCallbacks.get("pageshow")({persisted: true});
assert.equal(location.reloads, 1);
assert.equal(backgroundRequests, 0);
