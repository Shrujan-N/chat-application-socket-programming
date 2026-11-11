/* Browser chat feature completion: typing, group controls, edit/delete, group search and replies. */
(() => {
    'use strict';

    const style = document.createElement('style');
    style.textContent = `
        #browser-typing-indicator { min-height: 0; padding: 0 16px; color: #8fd3f4; font-size: 12px; }
        #browser-typing-indicator[hidden] { display: none; }
        .browser-message-actions { display:flex; flex-wrap:wrap; gap:5px; margin-top:4px; }
        .browser-message-actions button { padding:3px 7px; font-size:10px; background:#263449; color:#dbe7f5; border:1px solid #3b4a60; border-radius:4px; }
        .browser-message-actions button:hover { background:#344761; }
        .browser-message-actions .danger { color:#ffb4b4; }
        .browser-edited-label { color:#98a8bd; font-size:10px; margin-left:4px; }
        #browser-group-dialog { width:min(520px, calc(100% - 28px)); max-height:88vh; overflow:auto; margin:auto; padding:20px; border:1px solid #40516a; border-radius:10px; background:#172233; color:#f3f6fb; }
        #browser-group-dialog::backdrop { background:rgba(0,0,0,.72); }
        #browser-group-dialog input, #browser-group-dialog select { width:100%; box-sizing:border-box; margin:5px 0 12px; padding:9px; border:1px solid #40516a; border-radius:6px; background:#0d1522; color:#fff; }
        #browser-group-dialog label { display:block; color:#c2cede; font-size:12px; }
        #browser-group-dialog button { padding:7px 10px; margin:3px; border:0; border-radius:5px; cursor:pointer; color:white; background:#405875; }
        #browser-group-dialog .primary { background:#23844c; }
        #browser-group-dialog .danger { background:#8e3941; }
        #browser-group-dialog .member-row { display:flex; align-items:center; gap:8px; padding:8px 2px; border-bottom:1px solid #2c3a50; font-size:13px; }
        #browser-group-dialog .member-name { flex:1; min-width:0; overflow-wrap:anywhere; }
        #browser-group-dialog .role { color:#9eb1c9; font-size:11px; }
        #browser-group-dialog .dialog-actions { display:flex; justify-content:flex-end; gap:6px; margin-top:16px; }
        .browser-search-muted { color:#94a4b8; font-size:11px; }
    `;
    document.head.appendChild(style);

    const chatArea = document.getElementById('chat-area');
    const messageList = document.getElementById('messages-list');
    const typingIndicator = document.createElement('div');
    typingIndicator.id = 'browser-typing-indicator';
    typingIndicator.setAttribute('role', 'status');
    typingIndicator.setAttribute('aria-live', 'polite');
    typingIndicator.hidden = true;
    if (chatArea && messageList) chatArea.insertBefore(typingIndicator, messageList);

    const groupManageButton = document.createElement('button');
    groupManageButton.id = 'browser-group-manage-button';
    groupManageButton.type = 'button';
    groupManageButton.textContent = '⚙ Group';
    groupManageButton.title = 'View members and manage group settings';
    groupManageButton.style.cssText = 'padding:5px 9px;font-size:12px;background:#596579;display:none;';
    const headerActions = document.getElementById('chat-header-actions');
    if (headerActions) headerActions.appendChild(groupManageButton);

    const groupDialog = document.createElement('dialog');
    groupDialog.id = 'browser-group-dialog';
    groupDialog.innerHTML = `
        <h2 style="margin-top:0">Group settings</h2>
        <p class="browser-search-muted" id="browser-group-permission"></p>
        <label for="browser-group-name">Group name</label>
        <input id="browser-group-name" maxlength="80" autocomplete="off">
        <label for="browser-group-icon">Group icon</label>
        <input id="browser-group-icon" maxlength="16" autocomplete="off">
        <div id="browser-group-edit-actions"><button type="button" class="primary" id="browser-group-save">Save group details</button></div>
        <hr style="border:0;border-top:1px solid #34445b;margin:16px 0">
        <h3 style="font-size:15px">Members</h3>
        <div id="browser-group-members"></div>
        <div id="browser-group-add-area">
            <label for="browser-group-add-member">Add a registered user</label>
            <select id="browser-group-add-member"><option value="">Choose a user…</option></select>
            <button type="button" class="primary" id="browser-group-add-button">Add member</button>
        </div>
        <p id="browser-group-error" role="alert" style="color:#ffadad;min-height:18px"></p>
        <div class="dialog-actions"><button type="button" id="browser-group-close">Close</button></div>
    `;
    document.body.appendChild(groupDialog);

    let groupDialogMembers = [];
    let typingStopTimer = null;
    let typingContext = null;
    let typingActive = false;
    const remoteTyping = new Map();
    let groupSearchSequence = 0;

    function groupEndpoint(suffix = '') {
        return `/api/groups/${encodeURIComponent(activeGroupId)}${suffix}`;
    }

    function setGroupError(message = '') {
        document.getElementById('browser-group-error').textContent = message;
    }

    async function groupRequest(method, url, body) {
        const response = await fetch(url, {
            method,
            headers: body === undefined ? {} : { 'Content-Type': 'application/json' },
            body: body === undefined ? undefined : JSON.stringify(body)
        });
        return readJsonResult(response);
    }

    function refreshGroupInMemory(group) {
        if (!group) return;
        const index = lastGroups.findIndex(item => item.group_id === group.group_id);
        if (index >= 0) lastGroups[index] = { ...lastGroups[index], ...group };
        if (activeGroupId === group.group_id) activeGroup = { ...(activeGroup || {}), ...group };
        renderContactsList();
        updateChatHeader();
    }

    async function openGroupSettings() {
        if (!activeGroupId || !activeGroup) return;
        setGroupError('');
        const groupId = activeGroupId;
        try {
            const data = await groupRequest('GET', groupEndpoint('/members'));
            if (groupId !== activeGroupId) return;
            groupDialogMembers = Array.isArray(data.members) ? data.members : [];
            const isAdmin = activeGroup?.my_role === 'admin' || groupDialogMembers.some(m => m.username === currentUser && m.role === 'admin');
            document.getElementById('browser-group-permission').textContent = isAdmin
                ? 'You are a group admin. You can change group details, manage members and assign admins.'
                : 'You can view this group’s members. Only admins can change group details or membership.';
            document.getElementById('browser-group-name').value = activeGroup.name || '';
            document.getElementById('browser-group-icon').value = activeGroup.icon || '👥';
            document.getElementById('browser-group-name').disabled = !isAdmin;
            document.getElementById('browser-group-icon').disabled = !isAdmin;
            document.getElementById('browser-group-edit-actions').hidden = !isAdmin;
            document.getElementById('browser-group-add-area').hidden = !isAdmin;
            renderGroupSettingsMembers(isAdmin);
            if (isAdmin) await loadGroupAddCandidates(groupId);
            groupDialog.showModal();
        } catch (error) {
            showToast(error.message || 'Could not load group settings.');
        }
    }

    function renderGroupSettingsMembers(isAdmin) {
        const container = document.getElementById('browser-group-members');
        container.replaceChildren();
        if (!groupDialogMembers.length) {
            const empty = document.createElement('p');
            empty.className = 'browser-search-muted';
            empty.textContent = 'No members found.';
            container.appendChild(empty);
            return;
        }
        groupDialogMembers.forEach(member => {
            const row = document.createElement('div');
            row.className = 'member-row';
            const name = document.createElement('span');
            name.className = 'member-name';
            name.textContent = member.username === currentUser ? `${member.username} (you)` : member.username;
            const role = document.createElement('span');
            role.className = 'role';
            role.textContent = member.role === 'admin' ? 'Admin' : 'Member';
            row.append(name, role);
            if (isAdmin && member.username !== currentUser) {
                if (member.role === 'admin' && member.username !== activeGroup?.creator) {
                    const demote = document.createElement('button');
                    demote.type = 'button';
                    demote.textContent = 'Remove admin';
                    demote.addEventListener('click', () => changeMemberRole(member.username, false));
                    row.appendChild(demote);
                } else if (member.role !== 'admin') {
                    const promote = document.createElement('button');
                    promote.type = 'button';
                    promote.textContent = 'Make admin';
                    promote.addEventListener('click', () => changeMemberRole(member.username, true));
                    row.appendChild(promote);
                }
                if (member.username !== activeGroup?.creator) {
                    const remove = document.createElement('button');
                    remove.type = 'button';
                    remove.className = 'danger';
                    remove.textContent = 'Remove';
                    remove.addEventListener('click', () => removeGroupMember(member.username));
                    row.appendChild(remove);
                }
            }
            container.appendChild(row);
        });
    }

    async function loadGroupAddCandidates(expectedGroupId) {
        const select = document.getElementById('browser-group-add-member');
        select.replaceChildren(new Option('Choose a user…', ''));
        const response = await fetch('/api/users');
        const data = await readJsonResult(response);
        if (expectedGroupId !== activeGroupId) return;
        const existing = new Set(groupDialogMembers.map(member => member.username));
        (data.users || []).filter(name => name && !existing.has(name)).sort((a, b) => a.localeCompare(b)).forEach(name => {
            select.add(new Option(`@${name}`, name));
        });
    }

    async function reloadGroupSettings() {
        if (!activeGroupId || !groupDialog.open) return;
        const id = activeGroupId;
        const data = await groupRequest('GET', groupEndpoint('/members'));
        if (id !== activeGroupId) return;
        groupDialogMembers = data.members || [];
        const isAdmin = groupDialogMembers.some(member => member.username === currentUser && member.role === 'admin');
        if (activeGroup) activeGroup.my_role = isAdmin ? 'admin' : 'member';
        renderGroupSettingsMembers(isAdmin);
        if (isAdmin) await loadGroupAddCandidates(id);
    }

    async function saveGroupDetails() {
        setGroupError('');
        const name = document.getElementById('browser-group-name').value.trim();
        const icon = document.getElementById('browser-group-icon').value.trim();
        if (!name || !icon) return setGroupError('Enter both a group name and an icon.');
        try {
            const updated = await groupRequest('PATCH', groupEndpoint(''), { name, icon });
            refreshGroupInMemory(updated.group);
            showToast('Group details updated.');
        } catch (error) {
            setGroupError(error.message || 'Could not update group details.');
        }
    }

    async function addGroupMember() {
        setGroupError('');
        const select = document.getElementById('browser-group-add-member');
        const username = select.value;
        if (!username) return setGroupError('Choose a user to add.');
        try {
            await groupRequest('POST', groupEndpoint('/members'), { username });
            await reloadGroupSettings();
            await loadContacts();
            showToast(`@${username} added to the group.`);
        } catch (error) {
            setGroupError(error.message || 'Could not add that member.');
        }
    }

    async function removeGroupMember(username) {
        if (!confirm(`Remove @${username} from this group?`)) return;
        setGroupError('');
        try {
            await groupRequest('DELETE', groupEndpoint(`/members/${encodeURIComponent(username)}`));
            await reloadGroupSettings();
            await loadContacts();
            showToast(`@${username} removed from the group.`);
        } catch (error) {
            setGroupError(error.message || 'Could not remove that member.');
        }
    }

    async function changeMemberRole(username, promote) {
        setGroupError('');
        try {
            if (promote) {
                await groupRequest('POST', groupEndpoint('/admins'), { username });
            } else {
                await groupRequest('DELETE', groupEndpoint(`/admins/${encodeURIComponent(username)}`));
            }
            await reloadGroupSettings();
            await loadContacts();
            showToast(promote ? `@${username} is now an admin.` : `@${username} is now a member.`);
        } catch (error) {
            setGroupError(error.message || 'Could not change the member role.');
        }
    }

    groupManageButton.addEventListener('click', openGroupSettings);
    document.getElementById('browser-group-close').addEventListener('click', () => groupDialog.close());
    document.getElementById('browser-group-save').addEventListener('click', saveGroupDetails);
    document.getElementById('browser-group-add-button').addEventListener('click', addGroupMember);

    const originalUpdateChatHeader = updateChatHeader;
    updateChatHeader = function () {
        originalUpdateChatHeader();
        groupManageButton.style.display = activeGroupId ? 'inline-block' : 'none';
    };

    // Group members and metadata are refreshed for every participant's open browser.
    function handleGroupFeatureEvent(data) {
        if (!data.group_id) return;
        const groupId = data.group_id;
        if (data.type === 'group_updated') {
            refreshGroupInMemory({ group_id: groupId, name: data.name, icon: data.icon });
        }
        if (data.type === 'group_member_removed' && data.username === currentUser && activeGroupId === groupId) {
            activeGroupId = null;
            activeGroup = null;
            cancelReply();
            document.getElementById('messages-list').replaceChildren();
            updateChatHeader();
            if (groupDialog.open) groupDialog.close();
            showToast('You were removed from this group.');
        }
        loadContacts().catch(() => {});
        if (activeGroupId === groupId) {
            fetch(`/api/groups/${encodeURIComponent(groupId)}/members`)
                .then(readJsonResult)
                .then(result => {
                    if (activeGroupId !== groupId) return;
                    activeGroup.members = result.members || [];
                    activeGroup.member_count = activeGroup.members.length;
                    activeGroup.my_role = activeGroup.members.find(item => item.username === currentUser)?.role || 'member';
                    updateChatHeader();
                    if (groupDialog.open) reloadGroupSettings().catch(() => {});
                }).catch(() => {});
        }
    }

    // Typing events are transient and are never persisted as messages.
    function sendTypingFrame(active, context = typingContext) {
        if (!context || !wsOpen()) return;
        try {
            ws.send(JSON.stringify({
                type: active ? 'typing' : 'typing_stop',
                recipient: context.recipient || undefined,
                group_id: context.group_id || undefined
            }));
        } catch (error) {
            console.warn('Could not send typing state:', error);
        }
    }

    function stopOwnTyping() {
        clearTimeout(typingStopTimer);
        typingStopTimer = null;
        if (typingActive) sendTypingFrame(false);
        typingActive = false;
        typingContext = null;
    }

    function notifyTyping() {
        const input = document.getElementById('message-input');
        const content = input?.value || '';
        if (!content.trim() || (!activeContact && !activeGroupId)) {
            if (typingActive) stopOwnTyping();
            return;
        }
        const nextContext = activeGroupId ? { group_id: activeGroupId } : { recipient: activeContact };
        const nextKey = nextContext.group_id ? `g:${nextContext.group_id}` : `u:${nextContext.recipient}`;
        const previousKey = typingContext?.group_id ? `g:${typingContext.group_id}` : `u:${typingContext?.recipient}`;
        if (typingActive && previousKey !== nextKey) stopOwnTyping();
        typingContext = nextContext;
        if (!typingActive) {
            sendTypingFrame(true, typingContext);
            typingActive = true;
        }
        clearTimeout(typingStopTimer);
        typingStopTimer = setTimeout(stopOwnTyping, 1800);
    }

    function renderRemoteTyping() {
        const names = [...remoteTyping.keys()];
        if (!names.length) {
            typingIndicator.hidden = true;
            typingIndicator.textContent = '';
            return;
        }
        const shown = names.slice(0, 2).map(name => displayNameOf(name));
        typingIndicator.textContent = names.length === 1
            ? `${shown[0]} is typing…`
            : names.length === 2 ? `${shown[0]} and ${shown[1]} are typing…` : `${shown[0]} and others are typing…`;
        typingIndicator.hidden = false;
    }

    function handleTypingEvent(data) {
        const inThisChat = data.group_id
            ? activeGroupId === data.group_id
            : (!activeGroupId && activeContact === data.username && data.recipient === currentUser);
        if (!inThisChat || !data.username || data.username === currentUser) return;
        clearTimeout(remoteTyping.get(data.username));
        remoteTyping.delete(data.username);
        if (data.active === false) {
            renderRemoteTyping();
            return;
        }
        const timer = setTimeout(() => {
            remoteTyping.delete(data.username);
            renderRemoteTyping();
        }, 2600);
        remoteTyping.set(data.username, timer);
        renderRemoteTyping();
    }

    document.getElementById('message-input')?.addEventListener('input', notifyTyping);
    const originalSendMessage = sendMessage;
    sendMessage = function () {
        stopOwnTyping();
        return originalSendMessage();
    };
    const originalSelectContact = selectContact;
    selectContact = function (...args) {
        stopOwnTyping();
        remoteTyping.forEach(timer => clearTimeout(timer));
        remoteTyping.clear();
        renderRemoteTyping();
        return originalSelectContact(...args);
    };
    const originalSelectGroup = selectGroup;
    selectGroup = function (...args) {
        stopOwnTyping();
        remoteTyping.forEach(timer => clearTimeout(timer));
        remoteTyping.clear();
        renderRemoteTyping();
        return originalSelectGroup(...args);
    };

    // The original UI already creates a reply action for private messages. Add it for
    // groups, plus edit and per-user/everyone delete actions for all messages.
    const originalAppendMessageUI = appendMessageUI;
    appendMessageUI = function (...args) {
        originalAppendMessageUI(...args);
        const [sender, content, time, status, msgId, replyPreview, mentionedUsers, reactions, edited = false, deletedEveryone = false] = args;
        if (!msgId) return;
        const metadata = renderedMessages.get(msgId);
        if (metadata) {
            metadata.edited = Boolean(edited);
            metadata.deleted_everyone = Boolean(deletedEveryone);
            metadata.group_id = activeGroupId || null;
            metadata.recipient = activeContact || null;
        }
        const node = document.getElementById(`msg-${msgId}`);
        if (!node) return;
        const footer = node.children[1];
        if (edited && footer && !footer.querySelector('.browser-edited-label')) {
            const label = document.createElement('span');
            label.className = 'browser-edited-label';
            label.textContent = 'edited';
            footer.appendChild(label);
        }
        const row = node.querySelector('.message-action-row');
        if (deletedEveryone) {
            if (row) row.remove();
            const reactionsNode = node.querySelector('.reactions-container');
            if (reactionsNode) reactionsNode.remove();
            return;
        }
        if (!row) return;
        row.classList.add('browser-message-actions');
        if (activeGroupId && !row.querySelector('.message-reply-btn')) {
            const reply = document.createElement('button');
            reply.type = 'button';
            reply.className = 'message-reply-btn';
            reply.textContent = '↩ Reply';
            reply.addEventListener('click', event => {
                event.stopPropagation();
                startReplyToMessage(msgId);
            });
            row.prepend(reply);
        }
        if (sender === currentUser) {
            const edit = document.createElement('button');
            edit.type = 'button';
            edit.textContent = '✎ Edit';
            edit.addEventListener('click', () => editMessageInBrowser(msgId));
            row.appendChild(edit);
        }
        const deleteMe = document.createElement('button');
        deleteMe.type = 'button';
        deleteMe.className = 'danger';
        deleteMe.textContent = 'Delete for me';
        deleteMe.addEventListener('click', () => deleteMessageInBrowser(msgId, 'me'));
        row.appendChild(deleteMe);
        if (sender === currentUser) {
            const deleteEveryone = document.createElement('button');
            deleteEveryone.type = 'button';
            deleteEveryone.className = 'danger';
            deleteEveryone.textContent = 'Delete for everyone';
            deleteEveryone.addEventListener('click', () => deleteMessageInBrowser(msgId, 'everyone'));
            row.appendChild(deleteEveryone);
        }
    };

    function startReplyToMessageInAnyChat(msgId) {
        const message = renderedMessages.get(msgId);
        if (!message || (!activeContact && !activeGroupId)) return;
        replyingTo = {
            msg_id: message.msg_id,
            sender: message.sender,
            content: message.content,
            timestamp: message.timestamp
        };
        document.getElementById('reply-composer-title').textContent = `Replying to ${message.sender}`;
        document.getElementById('reply-composer-text').textContent = replyContentText(message.content).slice(0, 180);
        document.getElementById('reply-composer-preview').hidden = false;
        document.getElementById('message-input').focus();
    }
    startReplyToMessage = startReplyToMessageInAnyChat;

    async function editMessageInBrowser(msgId) {
        const message = renderedMessages.get(msgId);
        if (!message || message.sender !== currentUser || message.deleted_everyone) return;
        const updatedText = prompt('Edit your message:', message.content || '');
        if (updatedText === null) return;
        const text = updatedText.trim();
        if (!text) return showToast('A message cannot be empty.');
        try {
            const groupId = activeGroupId || message.group_id || null;
            const recipient = groupId ? null : (activeContact || message.recipient || null);
            const encrypted = window.E2E ? await E2E.encryptText(text, recipient, groupId) : text;
            const hash = await hashMessageContent(encrypted);
            await groupRequest('PATCH', `/api/messages/${encodeURIComponent(msgId)}`, {
                content: encrypted,
                integrity_hash: hash
            });
            applyMessageEdit({ msg_id: msgId, content: text, edited: true });
            showToast('Message edited.');
        } catch (error) {
            showToast(error.message || 'Could not edit message.');
        }
    }

    async function deleteMessageInBrowser(msgId, scope) {
        const promptText = scope === 'everyone'
            ? 'Delete this message for everyone? This cannot be undone.'
            : 'Delete this message only from your chat?';
        if (!confirm(promptText)) return;
        try {
            await groupRequest('DELETE', `/api/messages/${encodeURIComponent(msgId)}?scope=${scope}`);
            applyMessageDelete({ msg_id: msgId, scope, deleted_by: currentUser });
            showToast(scope === 'everyone' ? 'Message deleted for everyone.' : 'Message deleted for you.');
        } catch (error) {
            showToast(error.message || 'Could not delete message.');
        }
    }

    function applyMessageEdit(data) {
        const metadata = renderedMessages.get(data.msg_id);
        if (metadata) {
            metadata.content = data.content;
            metadata.edited = true;
        }
        const node = document.getElementById(`msg-${data.msg_id}`);
        if (!node) return;
        let contentNode = node.querySelector('.message-content');
        if (!contentNode) {
            const body = node.firstElementChild;
            contentNode = body?.lastElementChild;
        }
        if (contentNode) contentNode.innerHTML = data.content;
        const footer = node.children[1];
        if (footer && !footer.querySelector('.browser-edited-label')) {
            const label = document.createElement('span');
            label.className = 'browser-edited-label';
            label.textContent = 'edited';
            footer.appendChild(label);
        }
    }

    function applyMessageDelete(data) {
        const node = document.getElementById(`msg-${data.msg_id}`);
        if (data.scope === 'me') {
            if (node) node.remove();
            renderedMessages.delete(data.msg_id);
            return;
        }
        const metadata = renderedMessages.get(data.msg_id);
        if (metadata) {
            metadata.content = '[This message was deleted]';
            metadata.deleted_everyone = true;
            metadata.edited = false;
        }
        if (!node) return;
        const body = node.firstElementChild;
        if (body) {
            body.replaceChildren();
            const content = document.createElement('div');
            content.className = 'message-content';
            content.textContent = '[This message was deleted]';
            body.appendChild(content);
        }
        node.querySelector('.message-action-row')?.remove();
        node.querySelector('.reactions-container')?.remove();
    }

    // Group search results and private/group message matches are rendered from the
    // authenticated API. E2E-encrypted hits are decrypted in this browser when possible.
    function safeHighlight(value, query) {
        const escaped = escapeHtml(String(value ?? ''));
        const escapedQuery = String(query).replace(/[.*+?^${}()|[\]\\]/g, '\\$&');
        if (!escapedQuery) return escaped;
        return escaped.replace(new RegExp(`(${escapedQuery})`, 'ig'), '<mark style="background:#ffc107;color:#000;padding:0 2px;border-radius:2px">$1</mark>');
    }

    const originalHandleSearch = handleSearch;
    let completeSearchTimer = null;

    async function openSearchMessage(message) {
        if (message.conversation_type === 'group') {
            const group = lastGroups.find(entry => entry.group_id === message.group_id) || {
                group_id: message.group_id, name: message.group_name || 'Group', icon: '👥'
            };
            selectGroup(group);
        } else {
            const peer = message.sender === currentUser ? message.recipient : message.sender;
            selectContact(peer);
        }

        // History loading includes a round-trip and E2E decryption. Wait for the
        // target node instead of assuming it will always finish within 350 ms.
        for (let attempt = 0; attempt < 40; attempt++) {
            const stillInConversation = message.conversation_type === 'group'
                ? activeGroupId === message.group_id
                : activeContact === (message.sender === currentUser ? message.recipient : message.sender);
            if (!stillInConversation) return;
            if (document.getElementById(`msg-${message.msg_id}`)) {
                jumpToMessage(message.msg_id);
                return;
            }
            await new Promise(resolve => window.setTimeout(resolve, 100));
        }
        showToast('The matching message could not be opened. It may have been removed.');
    }

    async function performCompleteSearch(query, requestId) {
        const list = document.getElementById('contacts-list');
        try {
            const response = await fetch(`/api/search/complete?q=${encodeURIComponent(query)}`);
            const data = await readJsonResult(response);
            if (requestId !== groupSearchSequence) return;

            const messageCandidates = data.messages || [];
            if (window.E2E) {
                // Limit concurrent WebCrypto work so search remains responsive.
                for (let offset = 0; offset < messageCandidates.length; offset += 20) {
                    const batch = messageCandidates.slice(offset, offset + 20);
                    await Promise.all(batch.map(async item => {
                        try { await E2E.decryptMessageObject(item, currentUser, item.group_id || null); }
                        catch (error) { console.warn('Could not decrypt a search candidate:', error); }
                    }));
                    if (requestId !== groupSearchSequence) return;
                }
            }
            const normalizedQuery = query.toLocaleLowerCase();
            const messages = messageCandidates.filter(item => !item.deleted_everyone
                && String(item.content || '').toLocaleLowerCase().includes(normalizedQuery));
            if (requestId !== groupSearchSequence) return;
            list.replaceChildren();

            function section(title, color) {
                const header = document.createElement('div');
                header.textContent = title;
                header.style.cssText = `padding:6px 10px;font-size:11px;font-weight:bold;color:${color};background:#1e293b;border-radius:4px;margin:6px 0;`;
                list.appendChild(header);
            }
            if ((data.groups || []).length) {
                section(`GROUPS (${data.groups.length})`, '#8fd3f4');
                data.groups.forEach(group => {
                    const item = document.createElement('div');
                    item.className = 'contact-item';
                    const title = document.createElement('div');
                    title.innerHTML = `${escapeHtml(group.icon || '👥')} ${safeHighlight(group.name, query)}`;
                    const sub = document.createElement('div');
                    sub.className = 'browser-search-muted';
                    sub.textContent = `${group.member_count} members`;
                    item.append(title, sub);
                    item.addEventListener('click', () => selectGroup(group));
                    list.appendChild(item);
                });
            }
            if ((data.contacts || []).length) {
                section(`CONTACTS (${data.contacts.length})`, '#ffc107');
                data.contacts.forEach(contact => {
                    const item = document.createElement('div');
                    item.className = 'contact-item';
                    item.innerHTML = `👤 ${safeHighlight(contact, query)}`;
                    item.addEventListener('click', () => selectContact(contact));
                    list.appendChild(item);
                });
            }
            if (messages.length) {
                section(`MESSAGES FOUND (${messages.length})`, '#53bdeb');
                messages.forEach(message => {
                    const item = document.createElement('div');
                    item.className = 'contact-item';
                    item.style.fontSize = '12px';
                    const heading = document.createElement('div');
                    heading.style.cssText = 'display:flex;justify-content:space-between;color:#8696a0;font-size:10px;gap:8px;';
                    const chat = document.createElement('span');
                    chat.textContent = message.conversation_type === 'group'
                        ? `👥 ${message.group_name || 'Group chat'}`
                        : `💬 Chat with: ${message.sender === currentUser ? message.recipient : message.sender}`;
                    const time = document.createElement('span');
                    time.textContent = message.timestamp || '';
                    heading.append(chat, time);
                    const body = document.createElement('div');
                    body.style.cssText = 'margin-top:4px;color:#fff;word-break:break-word;';
                    body.innerHTML = safeHighlight(message.content, query);
                    item.append(heading, body);
                    item.addEventListener('click', () => openSearchMessage(message));
                    list.appendChild(item);
                });
            }
            if (!(data.groups || []).length && !(data.contacts || []).length && !messages.length) {
                const empty = document.createElement('div');
                empty.style.cssText = 'padding:15px;color:#8696a0;font-size:12px;text-align:center;';
                empty.textContent = 'No matching contacts, groups or messages found.';
                list.appendChild(empty);
            }
            if (window.E2E && data.encrypted_search_candidate_limit) {
                const note = document.createElement('div');
                note.className = 'browser-search-muted';
                note.style.cssText = 'padding:8px 10px;line-height:1.4;';
                note.textContent = `Encrypted message search checks the latest ${data.encrypted_search_candidate_limit} accessible messages.`;
                list.appendChild(note);
            }
        } catch (error) {
            if (requestId !== groupSearchSequence) return;
            console.error('Complete search failed:', error);
            showToast(error.message || 'Search failed.');
        }
    }

    handleSearch = function () {
        const query = document.getElementById('global-search').value.trim();
        clearTimeout(completeSearchTimer);
        if (!query) {
            groupSearchSequence++;
            return originalHandleSearch();
        }
        const requestId = ++groupSearchSequence;
        completeSearchTimer = window.setTimeout(
            () => performCompleteSearch(query, requestId), 220
        );
    };
    // WebSocket message frames need to be integrity-checked before edited text is shown.
    const originalHandleIncomingSignal = handleIncomingSignal;
    handleIncomingSignal = function (data) {
        if (data?.type !== 'message_edit') return originalHandleIncomingSignal(data);
        incomingIntegrityChain = incomingIntegrityChain.then(async () => {
            const validHash = typeof data.integrity_hash === 'string'
                && await hashMessageContent(data.content) === data.integrity_hash;
            if (!validHash) {
                showToast('An edited message failed integrity verification.');
                return;
            }
            if (window.E2E) await E2E.decryptMessageObject(data, currentUser, data.group_id || null);
            handleIncomingSignalVerified(data);
        }).catch(error => {
            console.error('Could not verify an edited message:', error);
            showToast('An edited message could not be verified.');
        });
    };

    const originalHandleIncomingSignalVerified = handleIncomingSignalVerified;
    handleIncomingSignalVerified = function (data) {
        if (data?.type === 'typing') return handleTypingEvent(data);
        if (data?.type === 'message_edit') return applyMessageEdit(data);
        if (data?.type === 'message_delete') return applyMessageDelete(data);
        if (data?.type === 'group_updated' || data?.type === 'group_member_added'
            || data?.type === 'group_member_removed' || data?.type === 'group_admin_changed') {
            return handleGroupFeatureEvent(data);
        }
        return originalHandleIncomingSignalVerified(data);
    };

    // Update history and sync rendering to preserve reply previews, edited flags and
    // delete-for-everyone state. These source hooks are idempotent and isolated.
    // A short-lived observer adds persisted message state after history DOM insertion.
    const historyObserver = new MutationObserver(records => {
        for (const record of records) {
            for (const node of record.addedNodes) {
                if (!(node instanceof HTMLElement) || !node.id?.startsWith('msg-')) continue;
                const msgId = node.id.slice(4);
                const meta = renderedMessages.get(msgId);
                if (meta?.deleted_everyone) applyMessageDelete({ msg_id: msgId, scope: 'everyone' });
                if (meta?.edited) {
                    const footer = node.children[1];
                    if (footer && !footer.querySelector('.browser-edited-label')) {
                        const label = document.createElement('span');
                        label.className = 'browser-edited-label';
                        label.textContent = 'edited';
                        footer.appendChild(label);
                    }
                }
            }
        }
    });
    if (messageList) historyObserver.observe(messageList, { childList: true });

    // The main page sends group reply refs alongside the queued message. The server
    // rejects references to messages outside the same group.
})();
