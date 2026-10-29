// Static files only. There is deliberately no backend to mutate or GPU to halt.
const http = require('node:http');
const fs = require('node:fs');
const path = require('node:path');
const root = path.resolve(__dirname, '../../backend/app/static');
http.createServer((req, res) => {
  const url = new URL(req.url, 'http://localhost');
  if (url.pathname.startsWith('/api/')) { res.writeHead(501); res.end('API must be mocked'); return; }
  const relative = url.pathname.startsWith('/static/') ? url.pathname.slice(8) : 'index.html';
  const target = path.resolve(root, relative);
  if (!target.startsWith(root + '/')) { res.writeHead(403); res.end(); return; }
  fs.readFile(target, (error, data) => {
    if (error) { res.writeHead(404); res.end(); return; }
    res.setHeader('Content-Type', ({ '.html': 'text/html', '.js': 'application/javascript', '.css': 'text/css' })[path.extname(target)] || 'application/octet-stream');
    res.end(data);
  });
}).listen(18341, '127.0.0.1');
