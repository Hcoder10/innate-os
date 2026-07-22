// Tests for loadQueue.ts's LoadQueue -- zero dependencies, plain node:
//   node --experimental-strip-types src/loadQueue.test.js   (npm test)
// The flag makes this run on Node 22.6+ (type stripping is unflagged from
// 22.18). Only the pure concurrency/progress core is exercised (queuedGLB needs
// a real GLTFLoader).

import assert from "node:assert/strict";
import { LoadQueue } from "./loadQueue.ts";

let passed = 0;
/** @param {string} name @param {() => Promise<void>} fn */
async function test(name, fn) {
  await fn();
  passed += 1;
  console.log(`ok - ${name}`);
}

/** A promise plus its resolve/reject, for hand-driving job completion. */
function deferred() {
  let resolve, reject;
  const promise = new Promise((res, rej) => {
    resolve = res;
    reject = rej;
  });
  return { promise, resolve, reject };
}

/** Flush the microtask + timer queues so queued jobs get a chance to start. */
const tick = () => new Promise((r) => setTimeout(r, 0));

await test("never runs more than `limit` jobs at once", async () => {
  const q = new LoadQueue(2);
  let active = 0;
  let max = 0;
  const gate = deferred();
  const jobs = [0, 1, 2, 3, 4].map(() =>
    q.add(async () => {
      active += 1;
      max = Math.max(max, active);
      await gate.promise;
      active -= 1;
    }),
  );
  await tick();
  assert.equal(active, 2, "exactly `limit` in flight while the rest wait");
  gate.resolve();
  await Promise.all(jobs);
  assert.equal(max, 2, "never exceeded `limit` as the queue drained");
});

await test("dequeues in FIFO order", async () => {
  const q = new LoadQueue(1);
  const order = [];
  const gates = [0, 1, 2].map(() => deferred());
  const jobs = [0, 1, 2].map((i) =>
    q.add(async () => {
      order.push(i);
      await gates[i].promise;
    }),
  );
  await tick();
  assert.deepEqual(order, [0], "only the first started");
  gates[0].resolve();
  await tick();
  assert.deepEqual(order, [0, 1]);
  gates[1].resolve();
  await tick();
  assert.deepEqual(order, [0, 1, 2]);
  gates[2].resolve();
  await Promise.all(jobs);
});

await test("aggregates across jobs, dedups repeats, and never conflates two jobs", async () => {
  const seen = [];
  const q = new LoadQueue(3, (p) => seen.push(p));
  const gA = deferred();
  const gB = deferred();
  let repA, repB;
  // Both jobs fetch the SAME url -- they must still track independently.
  const a = q.add(async (report) => {
    repA = report;
    await gA.promise;
  });
  const b = q.add(async (report) => {
    repB = report;
    await gB.promise;
  });
  await tick();

  repA(30, 100);
  repB(40, 100);
  let last = seen[seen.length - 1];
  assert.equal(last.loaded, 70, "loaded sums both jobs (not conflated by shared url)");
  assert.equal(last.total, 200, "total sums both sizes (not one)");

  repA(90, 100); // same job again: replaces, not adds
  last = seen[seen.length - 1];
  assert.equal(last.loaded, 130, "dedup: 90 (a) + 40 (b), not 30+90+40");
  assert.equal(last.total, 200);

  gA.resolve();
  gB.resolve();
  await Promise.all([a, b]);
});

await test("setEstimatedTotal seeds the denominator and acts as a floor", async () => {
  const seen = [];
  const q = new LoadQueue(3, (p) => seen.push(p));
  q.setEstimatedTotal(500);
  assert.deepEqual(seen[seen.length - 1], { loaded: 0, total: 500 }, "bar has width before any bytes");

  const g = deferred();
  let rep;
  const job = q.add(async (report) => {
    rep = report;
    await g.promise;
  });
  await tick();
  rep(100, 100); // real size below the estimate
  assert.equal(seen[seen.length - 1].total, 500, "estimate stays the floor until real sizes exceed it");
  g.resolve();
  await job;
});

await test("a rejected job frees its slot", async () => {
  const q = new LoadQueue(1);
  const first = q
    .add(async () => {
      throw new Error("boom");
    })
    .then(
      () => "resolved",
      (e) => e.message,
    );
  let ranSecond = false;
  const second = q.add(async () => {
    ranSecond = true;
  });
  assert.equal(await first, "boom");
  await second;
  assert.equal(ranSecond, true, "the next job ran after the first rejected");
});

await test("cancel() drops pending jobs but lets in-flight ones finish", async () => {
  const q = new LoadQueue(1);
  const g = deferred();
  let ranFirst = false;
  let ranSecond = false;
  const first = q.add(async () => {
    ranFirst = true;
    await g.promise;
  });
  q.add(async () => {
    ranSecond = true;
  }); // pending behind the first (limit 1)
  await tick();
  q.cancel();
  g.resolve();
  await first;
  await tick();
  assert.equal(ranFirst, true, "the in-flight job still finished");
  assert.equal(ranSecond, false, "the cancelled pending job never ran");
});

console.log(`\n${passed} passed`);
