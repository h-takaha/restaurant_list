/**
 * resolve-map.mjs — Google マップ共有 URL → 店名 / 座標
 * ESM, no external dependencies, Node v18+
 *
 * maps.app.goo.gl の短縮 URL を展開し、リダイレクト先の長い URL
 * （必要なら HTML 本文）から店名と緯度経度を取り出す。
 *
 * 住所を介さないので、住所が「未確認」の店でも地図に出せる。
 *
 * 使い方:
 *   node resolve-map.mjs <url> [<url> ...]
 * 出力: 1 行 1 件の JSON
 */

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

/** 日本の範囲に収まっているか */
export function validateJapanCoords(lat, lng) {
  return lat >= 20 && lat <= 46 && lng >= 122 && lng <= 154;
}

/**
 * Google マップの長い URL / HTML から座標を取り出す。
 * 優先度順に試す:
 *   1. !3d<lat>!4d<lng>  … ピンそのものの座標（最も正確）
 *   2. /@<lat>,<lng>     … 地図表示の中心。ピンとは僅かにずれることがある
 *   3. [null,null,<lat>,<lng>] … HTML 内の初期化データ
 */
export function extractCoords(text) {
  const patterns = [
    { re: /!3d(-?\d+(?:\.\d+)?)!4d(-?\d+(?:\.\d+)?)/, source: '!3d!4d' },
    { re: /\/@(-?\d+(?:\.\d+)?),(-?\d+(?:\.\d+)?)/,   source: '@' },
    { re: /null,null,(-?\d+\.\d+),(-?\d+\.\d+)\]/,    source: 'init' },
  ];
  for (const { re, source } of patterns) {
    const m = text.match(re);
    if (!m) continue;
    const lat = Number(m[1]);
    const lng = Number(m[2]);
    if (Number.isFinite(lat) && Number.isFinite(lng) && validateJapanCoords(lat, lng)) {
      return { lat, lng, source };
    }
  }
  return null;
}

/** 長い URL の /maps/place/<名前>/ から店名を取り出す */
export function extractPlaceName(url) {
  const m = url.match(/\/maps\/place\/([^/@?]+)/);
  if (!m) return null;
  try {
    return decodeURIComponent(m[1].replace(/\+/g, ' ')).trim() || null;
  } catch {
    return m[1].replace(/\+/g, ' ').trim() || null;
  }
}

/**
 * 短縮 URL を解決して { name, lat, lng, source, finalUrl } を返す。
 * 座標が取れなければ coords 系が null になる。
 */
export async function resolveMapUrl(shortUrl, { timeoutMs = 20_000 } = {}) {
  const res = await fetch(shortUrl, {
    redirect: 'follow',
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      'User-Agent': UA,
      // 同意ページに飛ばされにくくする
      'Accept-Language': 'ja,en;q=0.8',
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
  });

  const finalUrl = res.url;
  const out = {
    input: shortUrl,
    finalUrl,
    status: res.status,
    name: extractPlaceName(finalUrl),
    lat: null,
    lng: null,
    source: null,
  };

  // まず URL から取る（本文を読まずに済むので速い）
  let coords = extractCoords(finalUrl);

  // URL に無ければ HTML 本文を見る
  let body = '';
  if (!coords) {
    body = await res.text();
    coords = extractCoords(body);
    if (!out.name) {
      const t = body.match(/<meta content="([^"]+)" itemprop="name">/)
             || body.match(/<title>([^<]+)<\/title>/);
      if (t) out.name = t[1].replace(/\s*-\s*Google\s*マップ.*$/i, '').trim() || null;
    }
  } else {
    // 本文は読まずに捨てる
    await res.arrayBuffer().catch(() => {});
  }

  if (coords) {
    out.lat = coords.lat;
    out.lng = coords.lng;
    out.source = coords.source;
  } else {
    out.bodyLength = body.length;
    out.bodyHead = body.slice(0, 300);
  }

  return out;
}

// ── CLI ──────────────────────────────────────────────────────────────────────

const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;

if (isMain) {
  const urls = process.argv.slice(2);
  if (urls.length === 0) {
    console.error('使い方: node resolve-map.mjs <url> [<url> ...]');
    process.exit(1);
  }
  // --debug: 座標がどこに埋まっているか本文を実測する
  if (urls[0] === '--debug') {
    for (const u of urls.slice(1)) {
      const res = await fetch(u, {
        redirect: 'follow',
        headers: { 'User-Agent': UA, 'Accept-Language': 'ja,en;q=0.8' },
      });
      const body = await res.text();
      console.log(`\n===== ${u}`);
      console.log(`finalUrl: ${res.url}\n`);

      // 日本の緯度っぽい数値の周辺を出す
      const re = /(?<![\d.])(3[0-9]|4[0-5])\.\d{4,}(?![\d])/g;
      const seen = new Set();
      let m, shown = 0;
      while ((m = re.exec(body)) && shown < 12) {
        const ctx = body.slice(Math.max(0, m.index - 90), m.index + 90).replace(/\s+/g, ' ');
        if (seen.has(ctx)) continue;
        seen.add(ctx);
        shown++;
        console.log(`[lat候補 ${m[0]}] …${ctx}…\n`);
      }

      for (const key of ['itemprop="latitude"', 'APP_INITIALIZATION_STATE', 'og:image', '"latitude"']) {
        const i = body.indexOf(key);
        console.log(`${key}: ${i < 0 ? 'なし' : '…' + body.slice(i - 60, i + 200).replace(/\s+/g, ' ') + '…'}\n`);
      }
    }
    process.exit(0);
  }

  let failed = 0;
  for (const u of urls) {
    try {
      const r = await resolveMapUrl(u);
      if (r.lat == null) failed++;
      console.log(JSON.stringify(r, null, 2));
    } catch (err) {
      failed++;
      console.log(JSON.stringify({ input: u, error: err.message }, null, 2));
    }
  }
  process.exit(failed === urls.length ? 1 : 0);
}
