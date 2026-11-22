#!/usr/bin/env node
'use strict';

var assert = require('assert');
var fs = require('fs');
var path = require('path');
var vm = require('vm');

var root = path.join(__dirname, '..', '..');
var health = fs.readFileSync(path.join(root, 'dashboard/static/js/health.js'), 'utf8');
var events = fs.readFileSync(path.join(root, 'dashboard/static/js/tauri_events.js'), 'utf8');
var backend = fs.readFileSync(path.join(root, 'crates/ratspeak-tauri/src/commands/ble.rs'), 'utf8');

var labelStart = health.indexOf('function _resolveBlePeerLabel');
var labelEnd = health.indexOf('\nfunction _blePeerRepresentativeScore', labelStart);
var labelContext = { PeersCache: { get: function() { return null; } } };
vm.runInNewContext(health.slice(labelStart, labelEnd), labelContext);
assert.deepStrictEqual(
    JSON.parse(JSON.stringify(labelContext._resolveBlePeerLabel({ address: 'AA:BB:CC:DD:EE:FF' }))),
    { label: 'Identifying peer\u2026', title: 'Awaiting a signed identity announce' },
    'an unauthenticated transport address must never be rendered as peer identity'
);

var visibleStart = health.indexOf('function _blePeerRepresentativeScore');
var visibleEnd = health.indexOf('\nfunction emptyInterfaceConfigPayload', visibleStart);
var visibleContext = {
    window: {
        _blePeers: {
            transportOnly: {
                address: 'AA:BB:CC:DD:EE:FF', connected: true,
                identity_hash: '', routable: false
            },
            verified: {
                address: '11:22:33:44:55:66', connected: true,
                identity_hash: '11111111111111111111111111111111', routable: true,
                protocol: 'Ratspeak'
            }
        }
    }
};
vm.runInNewContext(health.slice(visibleStart, visibleEnd), visibleContext);
assert.strictEqual(visibleContext.window._bleVisiblePeersFromCache().length, 1,
    'only signed, routable peers may appear in the active peer list');

// A ble-reticulum peripheral (an RTNode) names itself in the Bluetooth
// handshake. It is a relay row under that name, never a messaging peer.
var nodeHash = '22222222222222222222222222222222';
visibleContext.window._blePeers = {
    transportOnly: { address: 'AA:BB:CC:DD:EE:FF', connected: true, identity_hash: '', routable: false },
    relay: {
        address: 'C0:00:00:00:00:01', connected: true, identity_hash: '', routable: false,
        node_identity_hash: nodeHash, protocol: 'Columba'
    },
    sameRelaySecondLink: {
        address: 'C0:00:00:00:00:02', connected: true, identity_hash: '', routable: false,
        node_identity_hash: nodeHash, protocol: 'Columba'
    },
    goneRelay: {
        address: 'C0:00:00:00:00:03', connected: false, identity_hash: '', routable: false,
        node_identity_hash: '44444444444444444444444444444444'
    },
    phone: {
        address: '11:22:33:44:55:66', connected: true, routable: true,
        identity_hash: '11111111111111111111111111111111',
        node_identity_hash: '33333333333333333333333333333333', protocol: 'Columba'
    }
};
var withNodes = JSON.parse(JSON.stringify(visibleContext.window._bleVisiblePeersFromCache()));
assert.strictEqual(withNodes.length, 2, 'one verified peer and one node; a bare GATT link stays hidden');
var relayRow = withNodes.filter(function(p) { return !p.identity_hash; })[0];
assert.strictEqual(relayRow.node_identity_hash, nodeHash);
assert.deepStrictEqual(relayRow.addresses.slice().sort(), ['C0:00:00:00:00:01', 'C0:00:00:00:00:02'],
    'two links to one node are one row');
assert.strictEqual(withNodes.filter(function(p) { return !!p.identity_hash; })[0].identity_hash,
    '11111111111111111111111111111111', 'a node that announced is listed by its signed identity');

var nodeLabel = JSON.parse(JSON.stringify(labelContext._resolveBlePeerLabel({ node_identity_hash: nodeHash })));
assert.strictEqual(nodeLabel.label.indexOf('Reticulum node '), 0);
assert.strictEqual(nodeLabel.title, 'Named by its Bluetooth handshake, not verified');
assert.strictEqual(
    JSON.parse(JSON.stringify(labelContext._resolveBlePeerLabel({
        identity_hash: '11111111111111111111111111111111', node_identity_hash: nodeHash
    }))).title,
    '11111111111111111111111111111111',
    'a signed identity always labels the row, whatever the node called itself'
);

assert(events.includes("provisional_identity_hash: provisionalIdentity"));
assert(events.includes("routable: !!identity && data.routable !== false"));
assert(events.includes("p.routable !== true"));
assert(!health.includes("return { label: addr, title: addr }"));
assert(backend.includes('BLE_IDENTITY_ANNOUNCE_RETRY_DELAYS_SECS'));
assert(backend.includes('"readiness": if routable { "routable" } else { "connected" }'));
assert(backend.includes('"readiness": "routable"'));
assert(backend.includes('verified_ble_peer_rows(address_to_identity)'));
assert(events.includes("node_identity_hash: data.node_identity_hash || ''"));
assert(backend.includes('"node_identity_hash": node_identity_hash'));
assert(backend.includes('ble_node_rows(address_to_identity, node_identity_by_address)'));

console.log('BLE peer readiness tests passed');
