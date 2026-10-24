/* Service worker for push notifications (FR-19).
 * Served from /sw.js (see main.py) so it can control the whole app.
 */
self.addEventListener('install', function () {
    self.skipWaiting();
});

self.addEventListener('activate', function (event) {
    event.waitUntil(self.clients.claim());
});

function readPayload(event) {
    if (!event.data) return {};
    try {
        return event.data.json();
    } catch (e) {
        return { type: 'message', title: 'New message', body: event.data.text() };
    }
}

self.addEventListener('push', function (event) {
    const data = readPayload(event);

    event.waitUntil((async function () {
        const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        const inFront = windows.some(function (w) { return w.visibilityState === 'visible' && w.focused; });

        // The user is looking at the chat right now: the message is already on screen.
        if (inFront && data.type === 'message') return;

        const options = {
            body: data.body || '',
            // One tag per message: if the same message arrives twice it replaces
            // the first notification instead of showing a duplicate.
            tag: data.tag || undefined,
            renotify: false,
            silent: false,                    // let the device play its notification sound
            vibrate: [200, 100, 200],
            timestamp: Date.now(),
            data: { sender: data.sender || '', msg_id: data.msg_id || '' }
        };
        if (data.icon) options.icon = data.icon;

        await self.registration.showNotification(data.title || 'New message', options);
    })());
});

self.addEventListener('notificationclick', function (event) {
    event.notification.close();
    const info = event.notification.data || {};
    const sender = typeof info.sender === 'string' ? info.sender : '';
    const validSender = /^[A-Za-z0-9_]{1,30}$/.test(sender);

    event.waitUntil((async function () {
        const windows = await self.clients.matchAll({ type: 'window', includeUncontrolled: true });
        for (const win of windows) {
            if ('focus' in win) {
                await win.focus();
                if (validSender) win.postMessage({ type: 'open_chat', sender: sender });
                return;
            }
        }
        // App is closed: open it and jump to the chat once the session is restored.
        if (self.clients.openWindow) {
            await self.clients.openWindow(validSender ? '/#chat=' + sender : '/');
        }
    })());
});