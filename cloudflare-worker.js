// Cloudflare Worker: обратный прокси к Gemini API.
// Скрипт ходит в Gemini через этот Worker (GEMINI_BASE_URL=https://<имя>.<аккаунт>.workers.dev),
// а Worker пересылает запрос в Google со своего egress-IP (обычно в поддерживаемом регионе).
// Так IP агента (РФ) не участвует — Google видит адрес Cloudflare.
//
// Деплой: Cloudflare Dashboard → Workers & Pages → Create → Worker → вставить этот код → Deploy.
// (Инструкция целиком — в README, раздел «Обход гео-блокировки через Cloudflare».)

const UPSTREAM = "https://generativelanguage.googleapis.com";

// (необязательно) простой секрет-гейт, чтобы Worker не был открытым прокси.
// Задай значение и добавь тот же секрет в заголовок x-proxy-secret на стороне клиента.
// Пусто = гейт выключен.
const PROXY_SECRET = "";

export default {
  async fetch(request) {
    // Разрешаем только пути Gemini API.
    const url = new URL(request.url);
    if (!url.pathname.startsWith("/v1")) {
      return new Response("Not found", { status: 404 });
    }

    if (PROXY_SECRET && request.headers.get("x-proxy-secret") !== PROXY_SECRET) {
      return new Response("Forbidden", { status: 403 });
    }

    const target = UPSTREAM + url.pathname + url.search;
    const headers = new Headers(request.headers);
    headers.delete("host");
    headers.delete("x-proxy-secret");

    const upstreamResp = await fetch(target, {
      method: request.method,
      headers,
      body: ["GET", "HEAD"].includes(request.method) ? undefined : request.body,
    });

    // Стримим ответ обратно как есть.
    return new Response(upstreamResp.body, {
      status: upstreamResp.status,
      headers: upstreamResp.headers,
    });
  },
};
