/**
 * resolve-map.mjs — Google マップ共有 URL → 店名 / feature ID
 * ESM, no external dependencies, Node v18+
 *
 * routine（クラウド上の Claude）の実行環境は Google ドメインが egress ポリシーで
 * 遮断されていて、短縮 URL を一切開けない。そこで解決だけ Actions に寄せる。
 *
 * ■ 取れるもの: 店名（Google マップに登録された正式表記）と feature ID
 * ■ 取れないもの: 座標
 *
 *   共有 URL のリダイレクト先 HTML に入っている座標は、ページを開いた
 *   クライアント（＝Actions ランナー）の位置であって、店の位置ではない。
 *   店の座標は JS 描画後の XHR で来るため、HTML には無い。
 *   実測値: 39.02679945, -77.844326（バージニア州＝ランナーの所在地）。
 *
 *   したがって座標は従来どおり build.mjs が住所から国土地理院で引く。
 *   このスクリプトの役目は「件名だけでは店を特定しきれないときに、
 *   Google マップ上の正式な店名を確定させる」こと。
 *
 * 使い方:
 *   node resolve-map.mjs <url> [<url> ...]
 */

const UA =
  'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 ' +
  '(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36';

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

/** data=!...!1s0x<hex>:0x<hex> から feature ID を取り出す */
export function extractFeatureId(url) {
  const m = url.match(/!1s(0x[0-9a-f]+:0x[0-9a-f]+)/i);
  return m ? m[1] : null;
}

/** 短縮 URL を解決して { name, featureId, finalUrl } を返す */
export async function resolveMapUrl(shortUrl, { timeoutMs = 20_000 } = {}) {
  const res = await fetch(shortUrl, {
    redirect: 'follow',
    signal: AbortSignal.timeout(timeoutMs),
    headers: {
      'User-Agent': UA,
      'Accept-Language': 'ja,en;q=0.8',
      Accept: 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    },
  });

  // 本文は使わないので読まずに捨てる
  await res.arrayBuffer().catch(() => {});

  const finalUrl = res.url;
  return {
    input: shortUrl,
    status: res.status,
    name: extractPlaceName(finalUrl),
    featureId: extractFeatureId(finalUrl),
    finalUrl,
  };
}

// ── CLI ──────────────────────────────────────────────────────────────────────

const isMain = process.argv[1] && import.meta.url === `file://${process.argv[1]}`;

if (isMain) {
  const urls = process.argv.slice(2);
  if (urls.length === 0) {
    console.error('使い方: node resolve-map.mjs <url> [<url> ...]');
    process.exit(1);
  }
  let resolved = 0;
  for (const u of urls) {
    try {
      const r = await resolveMapUrl(u);
      if (r.name) resolved++;
      console.log(JSON.stringify(r, null, 2));
    } catch (err) {
      console.log(JSON.stringify({ input: u, error: err.message }, null, 2));
    }
  }
  console.log(`\n店名を特定: ${resolved} / ${urls.length} 件`);
  console.log('※ 座標はここでは取れない（HTML の座標はランナーの位置）。住所から build.mjs が引く。');
  process.exit(resolved === 0 ? 1 : 0);
}
