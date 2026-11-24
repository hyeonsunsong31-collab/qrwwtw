// {{TITLE}} -- web server (Express): serves public/ and a small JSON API
const express = require('express');
const path = require('path');

const app = express();
app.use(express.json());
app.use(express.static(path.join(__dirname, 'public')));

app.get('/api/hello', (req, res) => {
  res.json({ message: 'Hello from {{TITLE}}', time: new Date().toISOString() });
});

const port = Number(process.env.PORT) || 3000;
const host = process.env.HOST || '127.0.0.1';
app.listen(port, host, () => console.log(`listening on http://${host}:${port}`));
