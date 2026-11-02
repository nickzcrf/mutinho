// Números das instalações. Só responde com a senha (variável ADMIN_TOKEN, definida na Vercel).
const crypto = require('crypto');
const { pipeline } = require('./_redis');

function pairs(flat) {
  const out = {};
  for (let i = 0; i < (flat || []).length; i += 2) out[flat[i]] = Number(flat[i + 1]);
  return out;
}

function same(a, b) {
  const x = crypto.createHash('sha256').update(String(a)).digest();
  const y = crypto.createHash('sha256').update(String(b)).digest();
  return crypto.timingSafeEqual(x, y);
}

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  res.setHeader('X-Robots-Tag', 'noindex');
  const admin = process.env.ADMIN_TOKEN;
  if (!admin || admin.length < 8) return res.status(500).json({ error: 'ADMIN_TOKEN não configurado' });

  const auth = String(req.headers.authorization || '');
  const given = auth.startsWith('Bearer ') ? auth.slice(7) : '';
  if (!given || !same(given, admin)) {
    await new Promise((r) => setTimeout(r, 400)); // atrasa tentativas de adivinhar
    return res.status(401).json({ error: 'senha incorreta' });
  }

  try {
    const [total, byDay, byVersion, byOs] = await pipeline([
      ['SCARD', 'installs'],
      ['HGETALL', 'by_day'],
      ['HGETALL', 'by_version'],
      ['HGETALL', 'by_os'],
    ]);
    return res.status(200).json({ total, by_day: pairs(byDay), by_version: pairs(byVersion), by_os: pairs(byOs) });
  } catch (e) {
    return res.status(500).json({ error: 'banco indisponível' });
  }
};
