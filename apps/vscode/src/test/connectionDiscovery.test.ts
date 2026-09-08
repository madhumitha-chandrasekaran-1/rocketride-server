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

import test from 'node:test';
import assert from 'node:assert/strict';
import {
	connectionDiscoveryPath,
	parseConnectionDiscovery,
	serializeConnectionDiscovery,
	isLoopbackDiscoveryUri,
	CONNECTION_DISCOVERY_FILENAME,
	type ConnectionDiscoveryInfo,
} from '../engine/local/connectionDiscovery';

const INFO: ConnectionDiscoveryInfo = {
	uri: 'http://localhost:54321',
	pid: 4242,
	updatedAt: '2026-08-05T12:00:00.000Z',
};

// --- connectionDiscoveryPath --------------------------------------------------

test('builds the discovery path under the given engine directory', () => {
	assert.equal(
		connectionDiscoveryPath('/Users/dev/Library/Application Support/RocketRide/engine'),
		`/Users/dev/Library/Application Support/RocketRide/engine/${CONNECTION_DISCOVERY_FILENAME}`,
	);
});

// --- serializeConnectionDiscovery / parseConnectionDiscovery round-trip ------

test('round-trips a well-formed info object', () => {
	const parsed = parseConnectionDiscovery(serializeConnectionDiscovery(INFO));
	assert.deepEqual(parsed, INFO);
});

test('serialized output is valid, human-readable JSON ending in a newline', () => {
	const text = serializeConnectionDiscovery(INFO);
	assert.ok(text.endsWith('\n'));
	assert.deepEqual(JSON.parse(text), INFO);
});

test('defaults a missing updatedAt to an empty string on parse', () => {
	const parsed = parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri, pid: INFO.pid }));
	assert.deepEqual(parsed, { uri: INFO.uri, pid: INFO.pid, updatedAt: '' });
});

test('a serialized file no longer carries an apiKey field', () => {
	const text = serializeConnectionDiscovery(INFO);
	assert.equal(JSON.parse(text).apiKey, undefined);
});

// --- parseConnectionDiscovery must never throw on bad input ------------------

test('returns null for invalid JSON', () => {
	assert.equal(parseConnectionDiscovery('not json'), null);
});

test('returns null for JSON that is not an object', () => {
	assert.equal(parseConnectionDiscovery('42'), null);
	assert.equal(parseConnectionDiscovery('"a string"'), null);
	assert.equal(parseConnectionDiscovery('null'), null);
	assert.equal(parseConnectionDiscovery('[]'), null);
});

test('returns null when uri is missing, not a string, or not an absolute http(s) URI', () => {
	assert.equal(parseConnectionDiscovery(JSON.stringify({ pid: 1 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: 123, pid: 1 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: '', pid: 1 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: 'localhost:54321', pid: 1 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: 'ws://localhost:54321', pid: 1 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: 'not a uri', pid: 1 })), null);
});

test('returns null when pid is missing, not a number, zero, negative, or fractional', () => {
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri, pid: '4242' })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri, pid: 0 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri, pid: -4242 })), null);
	assert.equal(parseConnectionDiscovery(JSON.stringify({ uri: INFO.uri, pid: 42.5 })), null);
});

test('ignores unknown extra fields from a future file version', () => {
	const parsed = parseConnectionDiscovery(
		JSON.stringify({ ...INFO, someFutureField: 'ignore me' }),
	);
	assert.deepEqual(parsed, INFO);
});

test('ignores a legacy apiKey field from a pre-#1851-fix file', () => {
	const parsed = parseConnectionDiscovery(JSON.stringify({ ...INFO, apiKey: 'MYAPIKEY' }));
	assert.deepEqual(parsed, INFO);
});

// --- isLoopbackDiscoveryUri ---------------------------------------------------

test('accepts the standard loopback spellings', () => {
	assert.ok(isLoopbackDiscoveryUri('http://localhost:54321'));
	assert.ok(isLoopbackDiscoveryUri('http://127.0.0.1:54321'));
	assert.ok(isLoopbackDiscoveryUri('http://[::1]:54321'));
});

test('accepts alternate loopback encodings that normalize to the standard form', () => {
	assert.ok(isLoopbackDiscoveryUri('http://0177.0.0.1:54321')); // octal
	assert.ok(isLoopbackDiscoveryUri('http://2130706433:54321')); // decimal
	assert.ok(isLoopbackDiscoveryUri('http://127.1:54321')); // shortened
	assert.ok(isLoopbackDiscoveryUri('http://[0:0:0:0:0:0:0:1]:54321')); // expanded IPv6
});

test('rejects a non-loopback host', () => {
	assert.equal(isLoopbackDiscoveryUri('http://attacker.example.com:54321'), false);
	assert.equal(isLoopbackDiscoveryUri('http://192.168.1.5:54321'), false);
});

test('rejects a malformed URI rather than throwing', () => {
	assert.equal(isLoopbackDiscoveryUri('not a uri'), false);
	assert.equal(isLoopbackDiscoveryUri(''), false);
});
