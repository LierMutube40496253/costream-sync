// Far-apart simulator (docs/DESIGN.md, "Simulator").
//   node demo/sim/run.js demo/sim/scenarios/copenhagen_sydney.json
// Starts the real sync server, runs the virtual users in real time, serves a live graph
// at http://localhost:8790, and checks the scenario's pass/fail windows against ground
// truth (the fake players know exactly which frame each user sees).
const fs = require('node:fs');
const http = require('node:http');
const path = require('node:path');
const { spawn } = require('node:child_process');
const { VirtualUser } = require('./virtual_user.js');

const ROOT = path.resolve(__dirname, '..', '..');
const OUT = path.join(__dirname, 'out');
const SERVER_PORT = 8766;
const GRAPH_PORT = 8790;
const K = 1792504509.25;   // the stream's broadcast stamp (wall time = player time + K)

const trueNow = () => (performance.timeOrigin + performance.now()) / 1000;
const sleep = (ms) => new Promise((r) => setTimeout(r, ms));

// The project's venv (Windows or Linux/macOS layout), else the Python on PATH (e.g. in CI).
function python() {
  if (process.env.COSTREAM_PYTHON) return process.env.COSTREAM_PYTHON;
  for (const p of [path.join(ROOT, '.venv', 'Scripts', 'python.exe'), path.join(ROOT, '.venv', 'bin', 'python')]) {
    if (fs.existsSync(p)) return p;
  }
  return process.platform === 'win32' ? 'python' : 'python3';
}

function startServer() {
  const exe = python();
  const proc = spawn(exe, ['-m', 'uvicorn', 'server.app:app', '--port', String(SERVER_PORT), '--log-level', 'warning'],
                     { cwd: ROOT, stdio: ['ignore', 'inherit', 'inherit'] });
  return proc;
}

async function waitForServer() {
  for (let i = 0; i < 50; i++) {
    const ok = await new Promise((res) => {
      const ws = new WebSocket(`ws://localhost:${SERVER_PORT}/ws`);
      ws.onopen = () => { ws.close(); res(true); };
      ws.onerror = () => res(false);
    });
    if (ok) return;
    await sleep(200);
  }
  throw new Error('sync server did not start');
}

function graphServer(scenario, clients) {
  const html = fs.readFileSync(path.join(__dirname, 'graph.html'), 'utf8');
  const srv = http.createServer((req, res) => {
    if (req.url === '/events') {
      res.writeHead(200, { 'Content-Type': 'text/event-stream', 'Cache-Control': 'no-cache', Connection: 'keep-alive' });
      res.write(`event: meta\ndata: ${JSON.stringify({ name: scenario.name, users: scenario.users.map((u) => u.name), checks: scenario.checks })}\n\n`);
      clients.add(res);
      req.on('close', () => clients.delete(res));
    } else {
      res.writeHead(200, { 'Content-Type': 'text/html; charset=utf-8' });
      res.end(html);
    }
  });
  srv.listen(GRAPH_PORT);
  return srv;
}

function evaluate(scenario, rows) {
  const results = [];
  for (const chk of scenario.checks || []) {
    const window = rows.filter((r) => r.t >= chk.from && r.t <= chk.to);
    let worst = 0, n = 0, missing = 0;
    for (const r of window) {
      const ds = chk.users.map((u) => r.users[u] && r.users[u].trueDelay);
      if (ds.some((d) => d == null)) { missing++; continue; }
      worst = Math.max(worst, (Math.max(...ds) - Math.min(...ds)) * 1000);
      n++;
    }
    const pass = n > 0 && missing === 0 && worst <= chk.maxOffsetMs;
    results.push({ ...chk, samples: n, worstMs: +worst.toFixed(1), pass });
  }
  return results;
}

async function main() {
  const file = process.argv[2] || path.join(__dirname, 'scenarios', 'copenhagen_sydney.json');
  const scenario = JSON.parse(fs.readFileSync(file, 'utf8').replace(/^﻿/, ''));   // tolerate a BOM
  fs.mkdirSync(OUT, { recursive: true });
  const logLines = [];
  const t0 = trueNow();
  const log = (who, text) => {
    const line = `${(trueNow() - t0).toFixed(1).padStart(6)} ${who.padEnd(3)} ${text}`;
    logLines.push(line);
    console.log(line);
  };

  const server = startServer();
  const clients = new Set();
  const graph = graphServer(scenario, clients);
  let users = [];
  try {
    await waitForServer();
    log('sim', `scenario "${scenario.name}" - live graph at http://localhost:${GRAPH_PORT}`);
    const room = `sim-${Date.now()}`;
    users = scenario.users.map((u) => new VirtualUser(u, {
      url: `ws://localhost:${SERVER_PORT}/ws`, room, K, trueNow, streamSeed: 1, videoId: 'simVideo', log,
    }));
    users.forEach((u) => u.start());
    const byName = Object.fromEntries(users.map((u) => [u.name, u]));

    const start = trueNow();
    const pending = [...(scenario.events || [])].sort((a, b) => a.at - b.at);
    const rows = [];
    while (trueNow() - start < scenario.duration) {
      const t = trueNow() - start;
      while (pending.length && pending[0].at <= t) {
        const ev = pending.shift();
        log('sim', `event: ${ev.user} ${ev.do}${ev.arg != null ? ' ' + ev.arg : ''}`);
        byName[ev.user][ev.do](ev.arg);
      }
      const row = { t: +t.toFixed(2), users: {} };
      for (const u of users) {
        row.users[u.name] = {
          trueDelay: u.player.inAd() ? null : +u.player.trueDelay().toFixed(4),
          state: u.state.state, errMs: u.state.err == null ? null : +(u.state.err * 1000).toFixed(1),
          rate: u.player.rate, clockErrMs: u.client.clock ? +((u.localNow() + u.client.clock.offset - trueNow()) * 1000).toFixed(1) : null,
          // Reported live edge vs the true one (the fake player's liveEdge). Positive = we
          // claim a later edge than the real one (safe, but syncs later than needed).
          edgeErrMs: u.client.last ? +((u.client.last.edge - u.player.liveEdge) * 1000).toFixed(0) : null,
        };
      }
      rows.push(row);
      const msg = `data: ${JSON.stringify(row)}\n\n`;
      for (const c of clients) c.write(msg);
      await sleep(250);
    }

    const results = evaluate(scenario, rows);
    const header = ['t', ...users.flatMap((u) => [`${u.name}_true_delay`, `${u.name}_state`, `${u.name}_err_ms`, `${u.name}_rate`,
                                                  `${u.name}_clock_err_ms`, `${u.name}_edge_err_ms`])];
    const csv = [header.join(',')].concat(rows.map((r) => [r.t, ...users.flatMap((u) => {
      const x = r.users[u.name];
      return [x.trueDelay ?? '', x.state, x.errMs ?? '', x.rate, x.clockErrMs ?? '', x.edgeErrMs ?? ''];
    })].join(',')));
    const base = path.join(OUT, path.basename(file, '.json'));
    fs.writeFileSync(base + '.csv', csv.join('\n') + '\n');
    fs.writeFileSync(base + '.log', logLines.join('\n') + '\n');
    fs.writeFileSync(base + '.result.json', JSON.stringify(results, null, 2));

    console.log('\nchecks (offset = largest difference in what the users see, from ground truth):');
    for (const r of results) {
      console.log(`  ${r.pass ? 'PASS' : 'FAIL'}  ${r.name}: worst ${r.worstMs} ms (limit ${r.maxOffsetMs} ms, ${r.samples} samples, t ${r.from}-${r.to} s)`);
    }
    for (const u of users) {
      const errs = rows.slice(20).map((r) => r.users[u.name].edgeErrMs).filter((e) => e != null);   // rows are 0.25 s apart
      if (errs.length) {
        console.log(`  live edge reported by ${u.name}: error vs true ${Math.min(...errs)} to ${Math.max(...errs)} ms (after the first 5 s)`);
      }
    }
    const allPass = results.every((r) => r.pass);
    console.log(allPass ? '\nPASS' : '\nFAIL');
    for (const c of clients) c.write(`event: done\ndata: ${JSON.stringify(results)}\n\n`);
    process.exitCode = allPass ? 0 : 1;
  } finally {
    users.forEach((u) => u.stop());
    await sleep(300);
    server.kill();
    graph.close();
    for (const c of clients) c.end();
  }
}

main().catch((e) => { console.error(e); process.exitCode = 2; });
