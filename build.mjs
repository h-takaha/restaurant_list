/**
 * build.mjs — restaurants.md → docs/data.json
 * ESM, no external dependencies, Node v18+
 *
 * 座標の手動修正について:
 *   docs/data.json の lat / lng フィールドを直接書き換えてください。
 *   キャッシュキーは "店名:::住所" なので、住所列が変わらない限り
 *   次回ビルドでも手動で書き換えた座標がそのまま使われます。
 *   住所を変更した場合のみ再取得されます。
 *
 * 使い方: node build.mjs
 */

import { readFileSync, writeFileSync, existsSync } from 'fs';
import { fileURLToPath } from 'url';
import { dirname, join } from 'path';

const __dirname = dirname(fileURLToPath(import.meta.url));
const MD_PATH   = join(__dirname, 'restaurants.md');
const JSON_PATH = join(__dirname, 'docs', 'data.json');

// ── helpers ──────────────────────────────────────────────────────────────────

/** Parse a Markdown link `[label](url)` → { url, label }. Returns null if not a link. */
function parseMdLink(text) {
  const m = text.trim().match(/^\[([^\]]*)\]\(([^)]+)\)$/);
  if (!m) return null;
  return { label: m[1], url: m[2] };
}

/** Sleep ms milliseconds */
function sleep(ms) {
  return new Promise(resolve => setTimeout(resolve, ms));
}

/** Validate lat/lng are within Japan bounding box */
function validateJapanCoords(lat, lng) {
  return lat >= 20 && lat <= 46 && lng >= 122 && lng <= 154;
}

/** Geocode an address using 国土地理院 AddressSearch API */
async function geocode(address) {
  const url = `https://msearch.gsi.go.jp/address-search/AddressSearch?q=${encodeURIComponent(address)}`;
  const res = await fetch(url, {
    signal: AbortSignal.timeout(10_000),
    headers: {
      'User-Agent': 'restaurant-list-map/1.0',
    },
  });
  if (!res.ok) throw new Error(`HTTP ${res.status}`);
  const data = await res.json();
  if (!Array.isArray(data) || data.length === 0) throw new Error('結果なし');
  const [lng, lat] = data[0].geometry.coordinates; // GeoJSON: [経度, 緯度]（順序注意）
  if (!validateJapanCoords(lat, lng)) {
    throw new Error(`座標が日本範囲外: lat=${lat}, lng=${lng}`);
  }
  return { lat, lng };
}

// ── parse restaurants.md ─────────────────────────────────────────────────────

const mdText = readFileSync(MD_PATH, 'utf8');
const lines = mdText.split('\n');
const tableLines = lines.filter(l => l.trim().startsWith('|'));

if (tableLines.length < 3) {
  console.error('restaurants.md に有効なテーブルが見つかりませんでした。');
  process.exit(1);
}

// 1行目: ヘッダ, 2行目: 区切り線, 3行目以降: データ
const dataLines = tableLines.slice(2);

const restaurants = dataLines
  .map(line => {
    const cells = line.trim().replace(/^\|/, '').replace(/\|$/, '').split('|').map(c => c.trim());
    if (cells.length < 9) return null;
    const [name, genre, area, address, hpRaw, recommended, status, rating, memo] = cells;
    const hpLink = parseMdLink(hpRaw);
    return {
      name,
      genre,
      area,
      address,
      hpUrl:   hpLink ? hpLink.url   : null,
      hpLabel: hpLink ? hpLink.label : (hpRaw && hpRaw !== '-' ? hpRaw : null),
      recommended: recommended && recommended !== '-' ? recommended : null,
      status,
      rating,
      memo,
      lat: null,
      lng: null,
    };
  })
  .filter(Boolean);

if (restaurants.length === 0) {
  console.error('restaurants.md にデータ行がありませんでした。');
  process.exit(1);
}

// ── load existing cache ───────────────────────────────────────────────────────

/**
 * キャッシュキー: "店名:::住所"
 * 住所が変わると再取得される。住所が同じなら手動修正値も保持される。
 */
function cacheKey(name, address) {
  return `${name}:::${address}`;
}

let cacheMap = new Map();
if (existsSync(JSON_PATH)) {
  try {
    const arr = JSON.parse(readFileSync(JSON_PATH, 'utf8'));
    if (Array.isArray(arr)) {
      for (const r of arr) {
        // 座標が入っているものだけキャッシュする。取得失敗は次回再挑戦させる
        if (r.lat != null && r.lng != null) {
          cacheMap.set(cacheKey(r.name, r.address), { lat: r.lat, lng: r.lng });
        }
      }
    }
  } catch {
    // キャッシュ破損は無視して再取得
  }
}

// ── geocode (with cache) ──────────────────────────────────────────────────────

const results = [];
let countCached  = 0;
let countFetched = 0;
let countFailed  = 0;
let firstRequest = true;

for (const r of restaurants) {
  const key = cacheKey(r.name, r.address);

  if (cacheMap.has(key)) {
    // キャッシュヒット（lat/lng が null でも「取得済み」として扱う）
    const cached = cacheMap.get(key);
    r.lat = cached.lat;
    r.lng = cached.lng;
    const coordStr = r.lat != null
      ? `lat=${r.lat.toFixed(5)}, lng=${r.lng.toFixed(5)}`
      : '座標なし';
    console.log(`[キャッシュ利用] ${r.name}: ${coordStr}`);
    countCached++;
    results.push(r);
    continue;
  }

  // 新規ジオコーディング
  if (!r.address || r.address === '-') {
    console.log(`[取得失敗]  ${r.name}: 住所が未設定`);
    countFailed++;
    results.push(r);
    continue;
  }

  // リクエスト間に 1 秒待つ
  if (!firstRequest) await sleep(1000);
  firstRequest = false;

  try {
    const { lat, lng } = await geocode(r.address);
    r.lat = lat;
    r.lng = lng;
    console.log(`[新規取得（座標つき）] ${r.name}: lat=${lat.toFixed(5)}, lng=${lng.toFixed(5)}`);
    countFetched++;
  } catch (err) {
    r.lat = null;
    r.lng = null;
    console.log(`[取得失敗]  ${r.name}: ${err.message} (住所: "${r.address}")`);
    countFailed++;
  }

  results.push(r);
}

// ── write docs/data.json (LF 固定) ───────────────────────────────────────────

// JSON.stringify は \r を含まないので LF に統一するため replace は不要だが明示する
const json = JSON.stringify(results, null, 2).replace(/\r\n/g, '\n') + '\n';
writeFileSync(JSON_PATH, json, { encoding: 'utf8' });

console.log('');
console.log(`完了: ${results.length} 件 / キャッシュ ${countCached} 件 / 新規取得 ${countFetched} 件 / 取得失敗 ${countFailed} 件`);
console.log(`出力: ${JSON_PATH}`);
