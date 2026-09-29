// Read-only bridge for the Python pipeline; no credentials or order endpoints.
import { alertLiquidity } from '../site/worker/alert-liquidity.js';
let input = '';
for await (const chunk of process.stdin) input += chunk;
const games = JSON.parse(input);
const results = {};
let next = 0;
await Promise.all(Array.from({ length: 2 }, async () => {
  while (next < games.length) {
    const game = games[next++];
    results[game.game_id] = await alertLiquidity(game);
  }
}));
process.stdout.write(JSON.stringify(results));
