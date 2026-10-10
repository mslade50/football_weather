// Public depth only; raw responses are saved before adapter parsing.
import { mkdir, writeFile, appendFile } from 'node:fs/promises';
import { createHash, randomUUID } from 'node:crypto';
import { join, resolve } from 'node:path';
import { pathToFileURL } from 'node:url';
import { alertLiquidity } from '../site/worker/alert-liquidity.js';
import { DEPTH_ADAPTERS, recordRawReceipt } from '../site/worker/execution-preview.js';

const HOSTS = new Set(['api.elections.kalshi.com', 'gateway.polymarket.us', 'api.novig.com']);
export async function collectResidentBatch(games, rawDir, fetchImpl = fetch, deadlineMs = 25000) {
  await mkdir(rawDir, { recursive: true });
  const controller = new AbortController(), results = {};
  const captures = new Set();
  let cursor = 0, timer;
  const captureFetch = async (url, options) => {
    const uri = new URL(url);
    if (uri.protocol !== 'https:' || !HOSTS.has(uri.hostname) || options.method !== 'GET'
        || options.headers?.Authorization) throw new Error('Public GET capture only');
    const started_at = new Date().toISOString();
    const response = await fetchImpl(url, { ...options, signal: AbortSignal.any([controller.signal, options.signal]) });
    const bytes = Buffer.from(await response.arrayBuffer());
    if (controller.signal.aborted) throw new Error('Capture deadline exceeded');
    if (bytes.length > 20 * 1024 * 1024) throw new Error('Provider response exceeds capture limit');
    const fetched_at = new Date().toISOString(), sha256 = createHash('sha256').update(bytes).digest('hex');
    const file = `${randomUUID()}.bin`;
    const capture = (async () => {
      await writeFile(join(rawDir, file), bytes, { flag: 'wx' });
      await appendFile(join(rawDir, 'manifest.jsonl'), JSON.stringify({ file, url: uri.toString(), started_at,
        fetched_at, status: response.status, bytes: bytes.length, sha256, age: response.headers.get('age') }) + '\n');
    })();
    captures.add(capture);
    await capture;
    const captured = new Response(bytes, { status: response.status, headers: response.headers });
    recordRawReceipt(captured, fetched_at);
    return captured;
  };
  const operation = Promise.all(Array.from({ length: Math.min(4, games.length) }, async () => {
    while (cursor < games.length && !controller.signal.aborted) {
      const game = games[cursor++], center = game.consensus?.total_now ?? 45.5;
      const refs = (game.execution_markets || []).filter(r => Object.hasOwn(DEPTH_ADAPTERS, r.book)
        && Number.isFinite(r.line) && r.line % 1 === .5)
        .sort((a, b) => Math.abs(a.line - center) - Math.abs(b.line - center) || a.book.localeCompare(b.book)).slice(0, 12);
      const quotes = refs.map(r => ({ ...((game.total_prices?.quotes || []).find(q => q.book === r.book
        && q.line === r.line && q.side === 'under') || {}), book: r.book, line: r.line, side: 'under', push_prob: 0 }));
      const snapshot = await alertLiquidity({ ...game, execution_markets: refs,
        total_prices: { ...game.total_prices, quotes } }, captureFetch, Date.now(), { fresh: true });
      if (controller.signal.aborted) break;
      results[game.game_id] = { ...snapshot, markets_checked: refs.length,
        markets_not_checked: Math.max(0, (game.execution_markets || []).length - refs.length) };
    }
  }));
  try {
    await Promise.race([operation, new Promise(resolveTimeout => {
      timer = setTimeout(() => { controller.abort(); resolveTimeout(); }, Math.min(25000, deadlineMs));
    })]);
    return results;
  } finally {
    clearTimeout(timer); controller.abort();
    await Promise.allSettled([...captures]);
  }
}

if (process.argv[1] && pathToFileURL(resolve(process.argv[1])).href === import.meta.url) {
  let input = '';
  for await (const chunk of process.stdin) input += chunk;
  const { games, raw_dir } = JSON.parse(input);
  if (!Array.isArray(games) || !raw_dir) throw new Error('Games and raw directory required');
  process.stdout.write(JSON.stringify(await collectResidentBatch(games, raw_dir)));
}
