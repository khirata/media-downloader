document.addEventListener('DOMContentLoaded', () => {
    const form = document.getElementById('settingsForm');
    const endpointEl = document.getElementById('endpoint');
    const apiKeyEl = document.getElementById('apiKey');
    const statusMsg = document.getElementById('statusMessage');
    const versionEl = document.getElementById('versionValue');

    // The ISO-8601 build stamp lives in version_name; manifest "version" has to
    // stay numeric for Chrome, so it is only a fallback.
    const manifest = chrome.runtime.getManifest();
    versionEl.textContent = manifest.version_name || manifest.version;

    // Load saved options
    chrome.storage.sync.get(['endpoint', 'apiKey'], (items) => {
        if (items.endpoint) endpointEl.value = items.endpoint;
        if (items.apiKey) apiKeyEl.value = items.apiKey;
    });

    // Save options
    form.addEventListener('submit', (e) => {
        e.preventDefault();
        const endpoint = endpointEl.value.trim();
        const apiKey = apiKeyEl.value.trim();

        chrome.storage.sync.set({ endpoint, apiKey }, () => {
            // Show success message
            statusMsg.textContent = 'Settings saved successfully!';
            statusMsg.classList.add('show');

            setTimeout(() => {
                statusMsg.classList.remove('show');
            }, 3000);
        });
    });
});
