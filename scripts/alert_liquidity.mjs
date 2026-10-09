// Read-only bridge for the Python pipeline; no credentials or order endpoints.
import { alertLiquidity } from '../site/worker/alert-liquidity.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const games = JSON.parse(input);
const results = {};
let next = 0;
const deadline = Date.now() + 25000;
const deadlineSignal = AbortSignal.timeout(25000);
const boundedFetch = (url, options) => fetch(url, { ...options,
  signal: AbortSignal.any([deadlineSignal, options.signal]) });
await Promise.all(Array.from({ length: 4 }, async () => {
  while (next < games.length && Date.now() < deadline) {
    const game = games[next++];
    results[game.game_id] = await alertLiquidity(game, boundedFetch);
  }
}));
process.stdout.write(JSON.stringify(results));
