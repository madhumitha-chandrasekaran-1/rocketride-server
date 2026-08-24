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

// The shell tsconfig deliberately keeps the browser surface node-free
// ("types": []); this co-located node:test suite opts back in explicitly.
/// <reference types="node" />

/**
 * Regression tests for the Cloud Pipeline Builder stale-content bug
 * (rocketride-org/rocketride-server#2036): `check_connection` -- no, this is
 * `Documents.openDocument` trusting an already-present, clean cache entry
 * forever, even when it no longer matches the backing store. Proven on
 * cloud.rocketride.ai: store v1, open it, overwrite the stored file to v2 via
 * the fs API directly, then close+reopen or hard-reload the browser -- the
 * editor kept showing v1. Publishing the same bytes under a NEW filename
 * showed v2 immediately, which is what pointed at a name-keyed cache rather
 * than a propagation delay.
 *
 * The one case worth protecting is a document with genuine unsaved local
 * edits (`dirty: true`) -- that content must never be silently replaced by
 * whatever the store currently holds.
 */

import assert from 'node:assert/strict';
import test from 'node:test';
import { Documents, type IVirtualFileSystem, type DocumentsState, type WorkspaceBinding } from './Documents';

/** In-memory VFS backed by a mutable Map, so tests can simulate an external
 * write to the store between two `openDocument` calls. */
function makeFakeVfs(initial: Record<string, unknown> = {}): { vfs: IVirtualFileSystem; store: Map<string, unknown>; failNextRead: Set<string> } {
	const store = new Map<string, unknown>(Object.entries(initial));
	const failNextRead = new Set<string>();
	const vfs: IVirtualFileSystem = {
		list: async () => [],
		read: async (path: string) => {
			if (failNextRead.has(path)) {
				failNextRead.delete(path);
				throw new Error('simulated read failure');
			}
			return store.has(path) ? store.get(path) : null;
		},
		write: async (path: string, content: unknown) => {
			store.set(path, content);
		},
		rename: async () => undefined,
		delete: async () => undefined,
		mkdir: async () => undefined,
	};
	return { vfs, store, failNextRead };
}

/** A DocumentsState as it would come back from persisted workspace appState:
 * a document entry with no active editor referencing it (`editorCount: 0`)
 * and marked clean -- exactly what a hard browser reload restores. */
function makePersistedState(uri: string, content: unknown): DocumentsState {
	return {
		documents: { [uri]: { uri, content, dirty: false, version: 1, editorCount: 0, isNew: false } },
		editors: {},
		groups: { 'group-1': { id: 'group-1', editorIds: [], activeEditorIndex: -1 } },
		rootNode: { type: 'leaf', id: 'group-1', groupId: 'group-1' },
		activeGroupId: 'group-1',
	};
}

function makeWorkspace(state: DocumentsState): WorkspaceBinding {
	return {
		appState: { documents: state },
		updateAppState: () => {},
	};
}

test('opening a document for the first time reads from the VFS', async () => {
	const { vfs } = makeFakeVfs({ 'a.pipe': 'v1' });
	const docs = new Documents(vfs);
	await docs.openDocument('a.pipe');
	assert.equal(docs.getDocument('a.pipe')?.content, 'v1');
});

test('closing the last editor of a clean document evicts it, so a later reopen sees an external change', async () => {
	const { vfs, store } = makeFakeVfs({ 'a.pipe': 'v1' });
	const docs = new Documents(vfs);
	await docs.openDocument('a.pipe');
	const [editorId] = Object.keys(docs.getState().editors);
	docs.closeEditor(editorId!);
	assert.equal(docs.getDocument('a.pipe'), undefined, 'a clean, unreferenced document should be evicted on close');

	store.set('a.pipe', 'v2');
	await docs.openDocument('a.pipe');
	assert.equal(docs.getDocument('a.pipe')?.content, 'v2');
});

test('#2036: a clean document restored from a persisted session is re-read, not trusted forever', async () => {
	// Simulates surviving a hard browser reload: the constructor restores
	// documents.['a.pipe'] from persisted appState with the OLD content and
	// editorCount: 0 (no live editor references it yet in this fresh session).
	const { vfs } = makeFakeVfs({ 'a.pipe': 'v2' }); // the store was updated externally since persistence
	const docs = new Documents(vfs, makeWorkspace(makePersistedState('a.pipe', 'v1')));

	// Sanity: the persisted (stale) content is there before any open happens.
	assert.equal(docs.getDocument('a.pipe')?.content, 'v1');

	await docs.openDocument('a.pipe');

	assert.equal(docs.getDocument('a.pipe')?.content, 'v2', 'open must re-read a clean document rather than trust the persisted cache');
});

test('a document with unsaved edits is never silently replaced by the store', async () => {
	const { vfs, store } = makeFakeVfs({ 'a.pipe': 'v1' });
	const docs = new Documents(vfs);
	await docs.openDocument('a.pipe');
	docs.updateContent('a.pipe', 'my unsaved edit');
	assert.equal(docs.getDocument('a.pipe')?.dirty, true);

	// An external write happens while the user has unsaved local changes.
	store.set('a.pipe', 'v2 from someone else');

	// Open the same document in a second pane (bypasses the "already open in
	// this group" short-circuit, exercising the same-uri-different-group path).
	const secondGroup = docs.splitGroup('group-1', 'horizontal');
	await docs.openDocument('a.pipe', secondGroup);

	assert.equal(docs.getDocument('a.pipe')?.content, 'my unsaved edit', 'dirty content must not be clobbered by a fresh read');
});

test('a failed re-read falls back to the previously cached content instead of clearing it', async () => {
	const { vfs, store, failNextRead } = makeFakeVfs({ 'a.pipe': 'v1' });
	const docs = new Documents(vfs);
	await docs.openDocument('a.pipe');
	const [editorId] = Object.keys(docs.getState().editors);

	// Re-open the same document from a persisted-like state with editorCount 0
	// (simulating a fresh session) so the read path is exercised again, but
	// this time the read throws (e.g. a transient network blip).
	docs.closeEditor(editorId!); // fresh close, doc would normally be evicted...
	// ...so seed it back as if freshly restored, to isolate the failure path
	// from the eviction behavior already covered above.
	const docs2 = new Documents(vfs, makeWorkspace(makePersistedState('a.pipe', 'v1')));
	failNextRead.add('a.pipe');
	store.set('a.pipe', 'v2'); // irrelevant: the read will throw before reaching this

	await docs2.openDocument('a.pipe');

	assert.equal(docs2.getDocument('a.pipe')?.content, 'v1', 'a failed read must not wipe out the last known-good content');
	assert.equal(docs2.getDocument('a.pipe')?.isNew, false, 'a document recovered from cache after a failed read is not "new"');
});
