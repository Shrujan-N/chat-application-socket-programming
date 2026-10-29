/**
 * Basic End-to-End Encryption (E2E) helper for browser clients (NFR-04, Issue #44).
 *
 * Uses only the standard Web Crypto API built into modern browsers:
 *   1. Each user generates an ECDH (P-256) key pair on their browser and uploads
 *      ONLY the public key to the server (`/api/e2e/keys`). The private key stays
 *      in `localStorage` and is never sent to the server.
 *   2. Direct 1-to-1 messages derive a shared 256-bit AES-GCM key via ECDH
 *      (my private key + peer's public key) and encrypt with a random 12-byte IV:
 *          "e2e:dh:<iv_base64>:<ciphertext_base64>"
 *   3. Group messages (and messages to pre-seeded contacts who have not logged in
 *      yet to upload a public key) encrypt client-side with AES-GCM using a
 *      256-bit SHA-256 conversation key so plaintext never touches the server or DB:
 *          "e2e:aes:<iv_base64>:<ciphertext_base64>"
 */
(function (global) {
    'use strict';

    const STORAGE_PREFIX = 'chat_e2e_keypair:';
    const DH_PREFIX = 'e2e:dh:';
    const AES_PREFIX = 'e2e:aes:';
    const APP_SALT = 'chat-app-e2e-v1:';

    let activeUsername = null;
    let activePrivateKey = null;
    let activePublicJwkStr = null;
    let initPromise = Promise.resolve();
    const peerPublicKeyCache = new Map();

    function hasWebCrypto() {
        return Boolean(global.crypto && global.crypto.subtle && global.TextEncoder && global.TextDecoder);
    }

    function bytesToBase64(bytes) {
        let binary = '';
        const view = bytes instanceof Uint8Array ? bytes : new Uint8Array(bytes);
        for (let i = 0; i < view.length; i++) {
            binary += String.fromCharCode(view[i]);
        }
        return global.btoa(binary);
    }

    function base64ToBytes(b64) {
        const binary = global.atob(b64);
        const out = new Uint8Array(binary.length);
        for (let i = 0; i < binary.length; i++) {
            out[i] = binary.charCodeAt(i);
        }
        return out;
    }

    function isEncrypted(text) {
        return typeof text === 'string' && (text.startsWith(DH_PREFIX) || text.startsWith(AES_PREFIX));
    }

    async function loadOrCreateKeyPair(username) {
        const storageKey = STORAGE_PREFIX + username;
        let saved = null;
        try {
            const raw = global.localStorage.getItem(storageKey);
            if (raw) saved = JSON.parse(raw);
        } catch (err) {
            saved = null;
        }

        if (saved && saved.publicJwk && saved.privateJwk) {
            const privateKey = await global.crypto.subtle.importKey(
                'jwk',
                saved.privateJwk,
                { name: 'ECDH', namedCurve: 'P-256' },
                false,
                ['deriveKey']
            );
            return { privateKey, publicJwk: saved.publicJwk };
        }

        const keyPair = await global.crypto.subtle.generateKey(
            { name: 'ECDH', namedCurve: 'P-256' },
            true,
            ['deriveKey']
        );
        const publicJwk = await global.crypto.subtle.exportKey('jwk', keyPair.publicKey);
        const privateJwk = await global.crypto.subtle.exportKey('jwk', keyPair.privateKey);
        try {
            global.localStorage.setItem(storageKey, JSON.stringify({ publicJwk, privateJwk }));
        } catch (err) {
            console.warn('Could not persist E2E key pair in localStorage:', err);
        }
        return { privateKey: keyPair.privateKey, publicJwk };
    }

    async function uploadPublicKey(publicJwkStr) {
        try {
            await global.fetch('/api/e2e/keys', {
                method: 'POST',
                headers: { 'Content-Type': 'application/json' },
                body: JSON.stringify({ public_key: publicJwkStr })
            });
        } catch (err) {
            console.warn('Could not upload E2E public key:', err);
        }
    }

    function init(username) {
        if (!username || !hasWebCrypto()) {
            initPromise = Promise.resolve();
            return initPromise;
        }
        activeUsername = username;
        peerPublicKeyCache.clear();
        initPromise = (async () => {
            const { privateKey, publicJwk } = await loadOrCreateKeyPair(username);
            activePrivateKey = privateKey;
            activePublicJwkStr = JSON.stringify({
                kty: publicJwk.kty,
                crv: publicJwk.crv,
                x: publicJwk.x,
                y: publicJwk.y,
                ext: true
            });
            peerPublicKeyCache.set(username, await global.crypto.subtle.importKey(
                'jwk',
                publicJwk,
                { name: 'ECDH', namedCurve: 'P-256' },
                false,
                []
            ));
            await uploadPublicKey(activePublicJwkStr);
        })().catch(err => {
            console.error('E2E initialization failed:', err);
        });
        return initPromise;
    }

    async function fetchPeerPublicKey(peerUsername) {
        if (!peerUsername) return null;
        if (peerPublicKeyCache.has(peerUsername)) {
            return peerPublicKeyCache.get(peerUsername);
        }
        try {
            const response = await global.fetch(`/api/e2e/keys/${encodeURIComponent(peerUsername)}`);
            if (!response.ok) return null;
            const data = await response.json();
            if (!data || !data.public_key) return null;
            const jwk = JSON.parse(data.public_key);
            const imported = await global.crypto.subtle.importKey(
                'jwk',
                jwk,
                { name: 'ECDH', namedCurve: 'P-256' },
                false,
                []
            );
            peerPublicKeyCache.set(peerUsername, imported);
            return imported;
        } catch (err) {
            return null;
        }
    }

    async function deriveDhAesKey(peerUsername) {
        await initPromise;
        if (!activePrivateKey) return null;
        const peerPub = await fetchPeerPublicKey(peerUsername);
        if (!peerPub) return null;
        return global.crypto.subtle.deriveKey(
            { name: 'ECDH', public: peerPub },
            activePrivateKey,
            { name: 'AES-GCM', length: 256 },
            false,
            ['encrypt', 'decrypt']
        );
    }

    async function deriveScopedAesKey(scopeId) {
        const rawBytes = new global.TextEncoder().encode(APP_SALT + scopeId);
        const hashBuffer = await global.crypto.subtle.digest('SHA-256', rawBytes);
        return global.crypto.subtle.importKey(
            'raw',
            hashBuffer,
            { name: 'AES-GCM' },
            false,
            ['encrypt', 'decrypt']
        );
    }

    function conversationScope(userA, userB, groupId) {
        if (groupId) return 'group:' + groupId;
        const pair = [String(userA || ''), String(userB || '')].sort();
        return 'dm:' + pair[0] + ':' + pair[1];
    }

    async function aesGcmEncrypt(aesKey, prefix, plaintext) {
        const iv = global.crypto.getRandomValues(new Uint8Array(12));
        const encoded = new global.TextEncoder().encode(String(plaintext));
        const cipherBuffer = await global.crypto.subtle.encrypt(
            { name: 'AES-GCM', iv },
            aesKey,
            encoded
        );
        return prefix + bytesToBase64(iv) + ':' + bytesToBase64(new Uint8Array(cipherBuffer));
    }

    async function aesGcmDecrypt(aesKey, ivB64, cipherB64) {
        const iv = base64ToBytes(ivB64);
        const cipherBytes = base64ToBytes(cipherB64);
        const plainBuffer = await global.crypto.subtle.decrypt(
            { name: 'AES-GCM', iv },
            aesKey,
            cipherBytes
        );
        return new global.TextDecoder().decode(plainBuffer);
    }

    async function encryptText(plaintext, peerUsername, groupId) {
        if (!hasWebCrypto() || plaintext == null || isEncrypted(plaintext)) {
            return plaintext;
        }
        if (!groupId && peerUsername) {
            const dhKey = await deriveDhAesKey(peerUsername);
            if (dhKey) {
                return aesGcmEncrypt(dhKey, DH_PREFIX, plaintext);
            }
        }
        const scopeKey = await deriveScopedAesKey(
            conversationScope(activeUsername, peerUsername, groupId)
        );
        return aesGcmEncrypt(scopeKey, AES_PREFIX, plaintext);
    }

    async function decryptText(ciphertext, peerUsername, groupId) {
        if (!isEncrypted(ciphertext) || !hasWebCrypto()) {
            return ciphertext;
        }
        try {
            if (ciphertext.startsWith(DH_PREFIX)) {
                const rest = ciphertext.slice(DH_PREFIX.length);
                const sep = rest.indexOf(':');
                if (sep <= 0) throw new Error('Malformed E2E DH payload');
                const dhKey = await deriveDhAesKey(peerUsername);
                if (!dhKey) throw new Error('Missing peer public key');
                return await aesGcmDecrypt(dhKey, rest.slice(0, sep), rest.slice(sep + 1));
            }
            if (ciphertext.startsWith(AES_PREFIX)) {
                const rest = ciphertext.slice(AES_PREFIX.length);
                const sep = rest.indexOf(':');
                if (sep <= 0) throw new Error('Malformed E2E AES payload');
                const scopeKey = await deriveScopedAesKey(
                    conversationScope(activeUsername, peerUsername, groupId)
                );
                return await aesGcmDecrypt(scopeKey, rest.slice(0, sep), rest.slice(sep + 1));
            }
        } catch (err) {
            return '🔒 [Encrypted message — unable to decrypt on this browser]';
        }
        return ciphertext;
    }

    async function decryptMessageObject(msg, currentUser, fallbackGroupId) {
        if (!msg || typeof msg !== 'object') return msg;
        const groupId = msg.group_id || fallbackGroupId || null;
        const peer = groupId
            ? null
            : (msg.sender === currentUser ? msg.recipient : msg.sender);
        if (isEncrypted(msg.content)) {
            msg.is_encrypted = true;
            msg.content = await decryptText(msg.content, peer, groupId);
        }
        if (msg.reply_preview && isEncrypted(msg.reply_preview.content)) {
            msg.reply_preview = {
                ...msg.reply_preview,
                content: await decryptText(msg.reply_preview.content, peer, groupId)
            };
        }
        return msg;
    }

    function resetSession() {
        activeUsername = null;
        activePrivateKey = null;
        activePublicJwkStr = null;
        peerPublicKeyCache.clear();
    }

    global.E2E = {
        init,
        isEncrypted,
        encryptText,
        decryptText,
        decryptMessageObject,
        resetSession
    };
})(typeof window !== 'undefined' ? window : globalThis);
