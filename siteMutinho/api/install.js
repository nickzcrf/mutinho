// Recebe o aviso único de "primeira abertura" do Mutinho e conta cada instalação uma vez.
// Guarda só: código aleatório, versão, sistema e dia. Nenhum IP é gravado.
const crypto = require('crypto');
const { pipeline } = require('./_redis');

const ID_RE = /^[A-Za-z0-9_-]{16,64}$/;
const VERSION_RE = /^[0-9][0-9A-Za-z.\-]{0,15}$/;
const OSES = ['Windows', 'Darwin', 'Linux'];
const LIMIT_PER_HOUR = 30; // por rede, só para barrar abuso

module.exports = async (req, res) => {
  res.setHeader('Cache-Control', 'no-store');
  if (req.method !== 'POST') return res.status(405).json({ error: 'metodo' });

  let body = req.body;
  if (typeof body === 'string') {
    try { body = JSON.parse(body); } catch (e) { body = null; }
  }
  if (!body || typeof body !== 'object') return res.status(400).json({ error: 'corpo' });

  const id = String(body.id || '');
  const version = String(body.version || '');
  const os = OSES.includes(body.os) ? body.os : 'Outro';
  if (!ID_RE.test(id) || !VERSION_RE.test(version)) return res.status(400).json({ error: 'dados' });

  try {
    // limite por rede: só guardamos um hash que expira em 1 hora
    const ip = String((req.headers['x-forwarded-for'] || '').split(',')[0] || 'desconhecido').trim();
    const who = crypto.createHash('sha256').update('mutinho:' + ip).digest('hex').slice(0, 24);
    const [count] = await pipeline([['INCR', 'rl:' + who], ['EXPIRE', 'rl:' + who, '3600', 'NX']]);
    if (count > LIMIT_PER_HOUR) return res.status(429).json({ error: 'muitos pedidos' });

    const [isNew] = await pipeline([['SADD', 'installs', id]]);
    if (isNew === 1) {
      const day = new Date().toISOString().slice(0, 10);
      await pipeline([
        ['HINCRBY', 'by_day', day, '1'],
        ['HINCRBY', 'by_version', version, '1'],
        ['HINCRBY', 'by_os', os, '1'],
      ]);
    }
    return res.status(200).json({ ok: true });
  } catch (e) {
    return res.status(500).json({ error: 'servidor' });
  }
};
