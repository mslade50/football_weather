import test from 'node:test';
import assert from 'node:assert/strict';
import { sliceCost } from '../execution-preview.js';

test('Kalshi direct-member debit matches canonical golf six-decimal fee and four-decimal balance precision', () => {
  const cost = sliceCost(.3333, 1, .07, 1, 'kalshi_direct');
  assert.equal(cost.principal, 333300n);
  assert.equal(cost.total, 348900n);
  assert.equal(cost.fee, 15600n);
  assert.equal(sliceCost(.99, 1, .07, 1, 'kalshi_direct').total, 990700n);
});

test('Polymarket US cents use half-even and Novig native one-cent contracts use five-decimal half-up', () => {
  assert.equal(sliceCost(.5, 1, .1, 1, 'polymarket_us_cent_half_even').fee, 20000n); // .025 -> .02
  assert.equal(sliceCost(.5, 1, .14, 1, 'polymarket_us_cent_half_even').fee, 40000n); // .035 -> .04
  assert.equal(sliceCost(.5, 1, .03, .01, 'novig_5dp_half_up').fee, 80n); // .000075 -> .00008
  assert.equal(sliceCost(.4, 600, .03, .01, 'novig_5dp_half_up').total, 2443200n);
  assert.equal(sliceCost(.4, 1, 0, .01, 'novig_5dp_half_up').total, 4000n);
});
