document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('settingsForm');
    const endpointEl = document.getElementById('endpoint');
    const apiKeyEl = document.getElementById('apiKey');
    const secretEl = document.getElementById('secret');
    const statusMsg = document.getElementById('statusMessage');

    // Load saved options
    chrome.storage.sync.get(['endpoint', 'apiKey', 'secret'], (items) => {
        if (items.endpoint) endpointEl.value = items.endpoint;
        if (items.apiKey) apiKeyEl.value = items.apiKey;
        if (items.secret) secretEl.value = items.secret;
    });

    // Save options
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const endpoint = endpointEl.value.trim();
        const apiKey = apiKeyEl.value.trim();
        const secret = secretEl.value.trim();

        chrome.storage.sync.set({
            endpoint,
            apiKey,
            secret
        }, () => {
            // Show success message
            statusMsg.textContent = 'Settings saved successfully!';
            statusMsg.classList.add('show');

            setTimeout(() => {
                statusMsg.classList.remove('show');
            }, 3000);
        });
    });
});
