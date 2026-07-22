#!/usr/bin/env node
/**
 * Imports exported profile JSON files (see profiles-export/) into a remote
 * BACnet simulator instance (e.g. the Azure-deployed one) via its existing
 * POST /profiles/import API — no redeploy needed, this endpoint already
 * exists on any running instance.
 *
 * Usage:
 *   node scripts/import-profiles-to-remote.mjs \
 *     --url http://<your-aci-fqdn-or-ip>:47900 \
 *     --user admin --pass <your-admin-password> \
 *     [file1.json file2.json ...]
 *
 * If no files are given, imports every *.json in scripts/profiles-export/.
 * Each file's name (minus .json, underscores -> spaces) becomes the
 * profile's display name; skips any name that already exists remotely.
 */

import { readFileSync, readdirSync } from 'fs';
import { basename, join, dirname } from 'path';
import { fileURLToPath } from 'url';

const __dirname = dirname(fileURLToPath(import.meta.url));

function arg(name) {
  const i = process.argv.indexOf(`--${name}`);
  return i !== -1 ? process.argv[i + 1] : undefined;
}

const url = arg('url');
const user = arg('user');
const pass = arg('pass');

if (!url || !user || !pass) {
  console.error('Usage: node import-profiles-to-remote.mjs --url <base-url> --user <username> --pass <password> [file.json ...]');
  process.exit(1);
}

let files = process.argv.slice(2).filter((a) => a.endsWith('.json'));
if (files.length === 0) {
  const exportDir = join(__dirname, 'profiles-export');
  files = readdirSync(exportDir).filter((f) => f.endsWith('.json')).map((f) => join(exportDir, f));
}

async function main() {
  console.log(`Logging in to ${url} as ${user}...`);
  const loginRes = await fetch(`${url}/auth/login`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ username: user, password: pass }),
  });
  if (!loginRes.ok) {
    const body = await loginRes.text();
    throw new Error(`Login failed (${loginRes.status}): ${body}`);
  }
  const { access_token } = await loginRes.json();

  const existingRes = await fetch(`${url}/profiles`, {
    headers: { Authorization: `Bearer ${access_token}` },
  });
  const existingNames = new Set((await existingRes.json()).map((p) => p.name));

  for (const file of files) {
    const name = basename(file).replace(/\.json$/i, '').replace(/[_-]+/g, ' ');
    if (existingNames.has(name)) {
      console.log(`Skipping "${name}" — already exists remotely`);
      continue;
    }
    const data = JSON.parse(readFileSync(file, 'utf8'));
    const res = await fetch(`${url}/profiles/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', Authorization: `Bearer ${access_token}` },
      body: JSON.stringify({ name, description: '', data }),
    });
    if (!res.ok) {
      console.error(`Failed to import "${name}": ${res.status} ${await res.text()}`);
      continue;
    }
    const result = await res.json();
    console.log(`Imported "${result.name}" — ${result.device_count} devices (id=${result.id})`);
  }
}

main().catch((err) => {
  console.error(err.message);
  process.exit(1);
});
