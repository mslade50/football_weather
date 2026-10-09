// Each request pins a content-addressed generation. Mutable operational receipts
// are explicitly outside that weather/model generation.
const NAMES = new Set(['meta.json', 'games_nfl.json', 'games_cfb.json', 'board.json', 'history.json',
  'wx_history.json', 'alerts_feed.json', 'status.json', 'backtest.json']);
const hex = bytes => Array.from(new Uint8Array(bytes), b => b.toString(16).padStart(2, '0')).join('');
const sha = async body => hex(await crypto.subtle.digest('SHA-256', body));
async function bytes(object) {
  if (object.arrayBuffer) return new Uint8Array(await object.arrayBuffer());
  return new TextEncoder().encode(await object.text());
}
function fromBytes(body) {
  return { body, json: async () => JSON.parse(new TextDecoder().decode(body)),
    text: async () => new TextDecoder().decode(body), arrayBuffer: async () => body.buffer };
}
export async function pinPublication(bucket, requestedGeneration = null) {
  const obj = await bucket.get('board/meta.json');
  if (!obj) return { bucket, meta: {}, verified: false };
  let meta = await obj.json(), manifest;
  if (!meta.publication && !requestedGeneration) return { bucket, meta: { ...meta, publication_status: 'legacy_unverified' }, verified: false };
  const generation = requestedGeneration || meta.publication?.generation;
  if (!/^[a-f0-9]{64}$/.test(generation || '')) throw new Error('Publication generation invalid');
  const prefix = `board/generations/${generation}/`;
  const m = await bucket.get(prefix + 'manifest.json');
  if (!m) throw new Error('Publication manifest missing');
  const raw = await bytes(m);
  if (await sha(raw) !== generation) throw new Error('Publication manifest checksum mismatch');
  manifest = JSON.parse(new TextDecoder().decode(raw));
  if (manifest.schema_version !== 1 || !manifest.objects || !['meta.json', 'games_nfl.json', 'games_cfb.json'].every(n => manifest.objects[n])
      || Object.keys(manifest.objects).some(n => !NAMES.has(n))) throw new Error('Publication manifest incomplete');
  const read = async name => {
    const receipt = manifest.objects[name];
    if (!receipt) return null;
    const object = await bucket.get(prefix + name);
    if (!object) throw new Error(`Publication object missing: ${name}`);
    const body = await bytes(object);
    if (body.length !== receipt.bytes || await sha(body) !== receipt.sha256) throw new Error(`Publication checksum mismatch: ${name}`);
    return fromBytes(body);
  };
  const canonical = await (await read('meta.json')).json();
  if (canonical.run_id !== manifest.run_id || canonical.git_sha !== manifest.git_sha
      || (!requestedGeneration && (meta.run_id !== canonical.run_id || meta.git_sha !== canonical.git_sha
        || meta.publication.manifest_key !== prefix + 'manifest.json' || meta.publication.manifest_sha256 !== generation)))
    throw new Error('Publication identity mismatch');
  meta = { ...canonical, publication: { schema_version: 1, generation, manifest_key: prefix + 'manifest.json', manifest_sha256: generation },
    publication_status: 'manifest_verified' };
  const deliveryFeed = async () => {
    const sealed = await read('alerts_feed.json');
    if (requestedGeneration) return sealed; // An archived generation stays immutable.
    const live = await bucket.get('board/alerts_live_feed.json');
    if (!live) return sealed;
    const payload = await live.json();
    if (payload?.meta?.run_id !== meta.run_id) return sealed;
    if (!Array.isArray(payload.alerts)) throw new Error('Delivery receipt stream invalid');
    return fromBytes(new TextEncoder().encode(JSON.stringify({ ...payload, delivery_stream_status: 'current_run_receipts' })));
  };
  return { meta, verified: true, bucket: {
    get: async key => key === 'board/meta.json' ? fromBytes(new TextEncoder().encode(JSON.stringify(meta)))
      : key === 'board/alerts_feed.json' ? deliveryFeed()
      : key.startsWith('board/') && NAMES.has(key.slice(6)) ? read(key.slice(6)) : bucket.get(key),
    put: (...args) => bucket.put(...args),
  } };
}
