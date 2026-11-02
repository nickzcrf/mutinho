// Cliente mínimo do Upstash Redis (API REST). Sem dependências.
function config() {
  const url = process.env.UPSTASH_REDIS_REST_URL || process.env.KV_REST_API_URL;
  const token = process.env.UPSTASH_REDIS_REST_TOKEN || process.env.KV_REST_API_TOKEN;
  if (!url || !token) throw new Error('Banco não configurado');
  return { url: url.replace(/\/$/, ''), token };
}

// Executa vários comandos de uma vez: pipeline([["SADD","k","v"], ["SCARD","k"]]) -> [resultados]
async function pipeline(commands) {
  const { url, token } = config();
  const r = await fetch(url + '/pipeline', {
    method: 'POST',
    headers: { Authorization: 'Bearer ' + token, 'Content-Type': 'application/json' },
    body: JSON.stringify(commands),
  });
  if (!r.ok) throw new Error('Banco respondeu ' + r.status);
  const out = await r.json();
  return out.map((x) => {
    if (x.error) throw new Error(x.error);
    return x.result;
  });
}

module.exports = { pipeline };
