// =============================================================================
// MIT License
// Copyright (c) 2026 Aparavi Software AG
//
// Permission is hereby granted, free of charge, to any person obtaining a copy
// of this software and associated documentation files (the "Software"), to deal
// in the Software without restriction, including without limitation the rights
// to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
// copies of the Software, and to permit persons to whom the Software is
// furnished to do so, subject to the following conditions:
//
// The above copyright notice and this permission notice shall be included in
// all copies or substantial portions of the Software.
//
// THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
// IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
// FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
// AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
// LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
// OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
// SOFTWARE.
// =============================================================================

/**
 * Pure helpers for the local engine's connection discovery file.
 *
 * `--port=0` gives the local engine a fresh OS-assigned port every time it
 * (re)starts, and that port previously lived only in this VS Code window's
 * in-memory state (plus, since #1413/#1492, the workspace `.env`). Neither
 * helps a process that isn't this specific window's own connect flow: a
 * long-running external script keeps whatever URI it read at its own
 * startup and has no signal that the engine restarted underneath it, and a
 * script not tied to this workspace has no `.env` to read at all. The only
 * previously-documented way to find the live port was `lsof` against the
 * engine's process name.
 *
 * This writes the resolved URI to a small, fixed, workspace-independent file
 * under the engine's own install directory (`getUserConfigDir()` from
 * `../config/config-migration`, i.e. `~/Library/Application Support/RocketRide`
 * on macOS, already the canonical "where does the local engine live"
 * directory that `version.json`/`engine-<pid>.pid` also live in) so any
 * process on the machine can find the currently-running local engine without
 * being the one that started it.
 *
 * Scope/limitation, deliberately: this is a single "last-connected" file, not
 * a registry of every concurrently-running engine across multiple VS Code
 * windows. For the reported problem -- one developer, one local engine,
 * losing track of it across reloads -- "last connected" is exactly the
 * right answer. Disambiguating multiple simultaneous local engines is a
 * separate, harder problem nobody asked for here; `pid` is included so a
 * consumer that cares can at least check the writer is still alive.
 *
 * These functions are deliberately free of any `vscode` import so they can
 * be unit-tested standalone with `node:test` (see connectionDiscovery.test.ts).
 * The actual filesystem read/write lives in EngineLocal, which already uses
 * `fs` synchronously for its PID files.
 *
 * Canonical schema, kept in sync by hand across this file and the read side
 * (`packages/client-python/src/rocketride/_connection_discovery.py`; there is
 * no TypeScript reader yet) -- see `ConnectionDiscoveryInfo` below for the
 * fields and `parseConnectionDiscovery`/`isLoopbackDiscoveryUri` for exactly
 * what a reader must validate before trusting a parsed file. There used to be
 * a third field, `apiKey`, hardcoded to the local-mode default; it was
 * removed (see #1851 review) because a credential-shaped field that is never
 * actually a credential invites the next reader to trust it as one.
 */

import * as path from 'path';

/** Shape of the connection discovery file's contents. */
export interface ConnectionDiscoveryInfo {
	uri: string;
	/** PID of the writer, so a stale entry left behind by a crash (no clean
	 * `stop()`) can be told apart from a live one. */
	pid: number;
	updatedAt: string;
}

/** Filename of the discovery file within the engine's install directory. */
export const CONNECTION_DISCOVERY_FILENAME = 'connection.json';

/**
 * Path to the discovery file given the local engine's install directory
 * (the same directory `EngineInstaller`/`engine-<pid>.pid` already use).
 */
export function connectionDiscoveryPath(engineDir: string): string {
	return path.join(engineDir, CONNECTION_DISCOVERY_FILENAME);
}

/** Serializes discovery info to the file's on-disk JSON text. */
export function serializeConnectionDiscovery(info: ConnectionDiscoveryInfo): string {
	return JSON.stringify(info, null, 2) + '\n';
}

/** True for an absolute `http(s)://` URI -- the only shape this file is ever
 * meant to carry (a bare host:port or a `ws(s)://` URI is not a mistake this
 * writer makes, so reject it rather than guess at normalizing it). */
function isAbsoluteHttpUri(uri: string): boolean {
	if (!uri) return false;
	try {
		return new URL(uri).protocol === 'http:' || new URL(uri).protocol === 'https:';
	} catch {
		return false;
	}
}

/**
 * True when `uri`'s host is loopback (`localhost`, `127.0.0.1`, or `::1`).
 *
 * Discovery only ever means "a local engine on this machine" -- the writer
 * never emits anything else. A reader MUST call this (or the equivalent
 * check on its own side) before adopting a discovered URI or any credential
 * alongside it: without it, a discovery file naming an attacker-controlled
 * host would redirect a client's real API key there. `new URL().hostname`
 * canonicalizes alternate IPv4/IPv6 loopback spellings (octal/decimal
 * octets, `::0:1`, etc.) to their standard form, so a plain equality check
 * against the three spellings below is not fooled by those.
 */
export function isLoopbackDiscoveryUri(uri: string): boolean {
	let hostname: string;
	try {
		hostname = new URL(uri).hostname;
	} catch {
		return false;
	}
	return hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '[::1]';
}

/**
 * Parses the discovery file's text, returning `null` for anything that isn't
 * a well-formed, structurally valid `ConnectionDiscoveryInfo` (missing file,
 * invalid JSON, a shape from some future/incompatible version, a non-http(s)
 * URI, or a non-positive/non-integer pid) rather than throwing -- callers on
 * the read side should treat this purely as an optional hint.
 *
 * This only checks that the file is well-formed. It does NOT check that the
 * URI is loopback -- callers must additionally call
 * `isLoopbackDiscoveryUri()` before trusting the result for anything
 * connection-relevant (see its doc comment).
 */
export function parseConnectionDiscovery(text: string): ConnectionDiscoveryInfo | null {
	let data: unknown;
	try {
		data = JSON.parse(text);
	} catch {
		return null;
	}
	if (!data || typeof data !== 'object') return null;
	const record = data as Record<string, unknown>;
	const uri = record.uri;
	const pid = record.pid;
	if (typeof uri !== 'string' || !isAbsoluteHttpUri(uri)) return null;
	if (typeof pid !== 'number' || !Number.isSafeInteger(pid) || pid <= 0) return null;
	return {
		uri,
		pid,
		updatedAt: typeof record.updatedAt === 'string' ? record.updatedAt : '',
	};
}
